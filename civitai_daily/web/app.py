"""Web 管理面板后端。

刻意做成单进程、无数据库、单账号：面板只是 CLI 的一层可视化外壳，
所有状态都读写 data/ 下那几个文件，CLI 与面板可以混用。
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from ..config import (
    COOKIE_PATH, DATA_DIR, Config, ensure_data_dir, load_state, read_runs,
    write_cookie,
)
from ..runner import probe as run_probe, run_all

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="civitai-daily panel", docs_url=None, redoc_url=None)

_lock = threading.Lock()
_run_state: dict[str, Any] = {
    "running": False,
    "mode": "",
    "started_at": "",
    "finished_at": "",
    "report": None,
    "error": "",
    # 实时进度：没有这几个字段时，界面在长任务期间完全不动，
    # 看上去就像死掉了（实测一轮全任务要跑 2 分多钟）。
    "progress": "",
    "index": 0,
    "total": 0,
    "task_log": [],
}


class RunRequest(BaseModel):
    live: bool = False
    only: list[str] | None = None


class CookieRequest(BaseModel):
    cookie: str


def _snapshot() -> dict[str, Any]:
    cfg = Config.load()
    state = load_state()
    return {
        "site": cfg.base,
        "has_cookie": cfg.has_cookie,
        "cookie_path": str(COOKIE_PATH),
        "data_dir": str(DATA_DIR),
        "limits": asdict(cfg.limits),
        "tasks": asdict(cfg.tasks),
        "endpoint_hits": state.get("endpoint_hits", {}),
        "days": state.get("days", {}),
        "run": _run_state,
    }


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/logo.png")
def logo_png() -> FileResponse:
    return FileResponse(STATIC_DIR / "logo.png")


@app.get("/favicon.png")
def favicon_png() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.png")


@app.get("/api/status")
def api_status() -> JSONResponse:
    return JSONResponse(_snapshot())


@app.get("/api/runs")
def api_runs(limit: int = 20) -> JSONResponse:
    return JSONResponse(read_runs(limit=limit))


@app.get("/api/probe")
def api_probe() -> JSONResponse:
    cfg = Config.load()
    rows = [{"action": a, "endpoint": e, "result": r} for a, e, r in run_probe(cfg)]
    return JSONResponse({"rows": rows})


@app.post("/api/cookie")
def api_cookie(req: CookieRequest) -> JSONResponse:
    if not req.cookie.strip():
        raise HTTPException(status_code=400, detail="Cookie 为空")
    ensure_data_dir()
    write_cookie(req.cookie)
    return JSONResponse({"ok": True, "saved_to": str(COOKIE_PATH)})


def _worker(live: bool, only: list[str] | None) -> None:
    def on_progress(info: dict[str, Any]) -> None:
        stage = info.get("stage", "")
        with _lock:
            if stage == "task_progress":
                # 任务内部的分步计数（例如 浏览 12/30），比只报任务名有用得多
                _run_state["progress"] = str(info.get("detail", ""))
            else:
                _run_state["progress"] = str(info.get("label", ""))
            _run_state["index"] = info.get("index", 0)
            _run_state["total"] = info.get("total", 0)
            if stage == "task_done":
                _run_state["task_log"].append({
                    "label": str(info.get("label", "")),
                    "status": str(info.get("status", "")),
                })

    try:
        cfg = Config.load()
        report = run_all(cfg, dry_run=not live, only=only, progress=on_progress)
        with _lock:
            _run_state["report"] = report.to_dict()
            _run_state["finished_at"] = report.finished_at
            _run_state["running"] = False
            _run_state["progress"] = "完成"
    except Exception as exc:  # 面板不能因为一次执行失败就死掉
        with _lock:
            _run_state["error"] = f"{type(exc).__name__}: {exc}"
            _run_state["running"] = False
            _run_state["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")


@app.post("/api/run")
def api_run(req: RunRequest) -> JSONResponse:
    with _lock:
        if _run_state["running"]:
            raise HTTPException(status_code=409, detail="已有任务在执行中")
        _run_state.update({
            "running": True,
            "mode": "live" if req.live else "dry-run",
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": "",
            "report": None,
            "error": "",
            "progress": "准备中",
            "index": 0,
            "total": 0,
            "task_log": [],
        })
    threading.Thread(target=_worker, args=(req.live, req.only), daemon=True).start()
    return JSONResponse({"ok": True, "mode": _run_state["mode"]})


@app.post("/api/run/reset")
def api_reset() -> JSONResponse:
    with _lock:
        _run_state.update({"running": False, "report": None, "error": "",
                           "started_at": "", "finished_at": "", "mode": "",
                           "progress": "", "index": 0, "total": 0,
                           "task_log": []})
    return JSONResponse({"ok": True})
