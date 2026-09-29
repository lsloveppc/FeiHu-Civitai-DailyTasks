"""执行器：按配置编排任务、控制时间预算、汇总并落盘报告。"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .client import CivitaiClient
from .config import Config, append_run, load_state, save_state
from .tasks import (
    DONE, FAILED, PARTIAL, SKIPPED, UNSUPPORTED,
    TASKS, TaskContext, TaskOutcome, today_str,
)

# 执行顺序有意义：先校验登录、先领、再看挑战，最后才是刷素材的写操作。
DEFAULT_ORDER: list[str] = [
    "account_status",
    "daily_reward",
    "earn_potential",
    "challenge_daily",
    "browse",
    "react_images",
    "follow_creators",
]

STATUS_ICON = {
    DONE: "[OK]",
    PARTIAL: "[~ ]",
    SKIPPED: "[- ]",
    UNSUPPORTED: "[x ]",
    FAILED: "[!!]",
}


@dataclass
class RunReport:
    started_at: str
    finished_at: str = ""
    duration_s: float = 0.0
    site: str = ""
    dry_run: bool = True
    outcomes: list[TaskOutcome] = field(default_factory=list)
    stopped_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": round(self.duration_s, 1),
            "site": self.site,
            "dry_run": self.dry_run,
            "stopped_reason": self.stopped_reason,
            "outcomes": [asdict(o) for o in self.outcomes],
        }

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.outcomes:
            out[o.status] = out.get(o.status, 0) + 1
        return out

    def summary_lines(self) -> list[str]:
        lines = []
        for o in self.outcomes:
            icon = STATUS_ICON.get(o.status, "[??]")
            extra = f" | {'; '.join(o.samples)}" if o.samples else ""
            lines.append(f"{icon} {o.label} — {o.detail}{extra}")
        return lines


def _enabled(cfg: Config, key: str, only: Iterable[str] | None) -> bool:
    if only:
        return key in set(only)
    if key == "account_status":
        return True
    return bool(getattr(cfg.tasks, key, True))


def _emit(progress: Any, **info: Any) -> None:
    """把进度推给调用方。回调本身出问题不该影响任务执行。"""
    if not progress:
        return
    try:
        progress(info)
    except Exception:
        pass


def run_all(cfg: Config, *, dry_run: bool = True,
            only: Iterable[str] | None = None,
            verbose: bool = False,
            progress: Any = None) -> RunReport:
    report = RunReport(
        started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        site=cfg.base,
        dry_run=dry_run,
    )

    # 没有 Cookie 时，整轮跑下去只会得到一串 401。但显式 --only 指定的
    # 匿名只读任务（比如 challenge_daily）是能真出结果的，不该被一起拦掉。
    if not cfg.has_cookie and not only:
        report.outcomes.append(TaskOutcome(
            "account_status", "登录态校验", FAILED,
            "没有可用 Cookie。先执行 `civitai-daily login` 或把 Cookie 写进 data/cookies.txt。"))
        report.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
        return report

    state = load_state()

    # 先把要跑的任务定下来：进度分母必须一开始就确定，否则界面上的
    # "3/7" 会随跳过项来回跳。
    if only is None:
        for key in DEFAULT_ORDER:
            if not _enabled(cfg, key, only):
                report.outcomes.append(
                    TaskOutcome(key, TASKS[key][0], SKIPPED, "配置中已关闭"))
    planned = [k for k in DEFAULT_ORDER if _enabled(cfg, k, only)]
    total = len(planned)

    current: list[Any] = ["准备中", 0]

    def on_progress(msg: str) -> None:
        _emit(progress, stage="task_progress", label=current[0],
              index=current[1], total=total, detail=msg)

    ctx = TaskContext(client=None, state=state, today=today_str(),   # type: ignore[arg-type]
                      dry_run=dry_run, _progress=on_progress)

    t0 = time.monotonic()
    with CivitaiClient(cfg, dry_run=dry_run, verbose=verbose, state=state) as client:
        ctx.client = client
        _emit(progress, stage="start", label="准备中", index=0, total=total)

        for idx, key in enumerate(planned, 1):
            if time.monotonic() - t0 > cfg.limits.max_runtime_seconds:
                report.stopped_reason = (
                    f"达到时间预算 {cfg.limits.max_runtime_seconds}s，剩余任务跳过")
                break

            label, fn = TASKS[key]
            current[0], current[1] = label, idx
            _emit(progress, stage="task_start", label=label, index=idx,
                  total=total, key=key)

            try:
                outcome = fn(ctx)
            except Exception as exc:  # 单个任务炸掉不该拖垮整轮
                outcome = TaskOutcome(key, label, FAILED,
                                      f"任务异常: {type(exc).__name__}: {exc}")

            report.outcomes.append(outcome)
            _emit(progress, stage="task_done", label=label, index=idx,
                  total=total, key=key, status=outcome.status)

            # 登录态没过，后面的写操作没意义，直接收工。只在真正跑过
            # account_status 时才中断：--only 模式下用户可能本就不想校验，
            # 不该因此把指定任务一起吞掉。
            if key == "account_status" and outcome.status != DONE:
                break

        client.persist_hits()

    save_state(state)
    report.duration_s = time.monotonic() - t0
    report.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
    append_run(report.to_dict())
    return report


def probe(cfg: Config, *, verbose: bool = False) -> list[tuple[str, str, str]]:
    """只读探活：把每个动作的候选端点打一遍，回报真实可用情况。

    - 只读动作：只打 GET 候选，跳过其中混着的写候选。
    - 写动作（点赞/关注）：**仅在尚未配置 Cookie 时**做存在性探测。
      未登录状态下服务端先鉴权后执行，tRPC 直接回 401/403，这次请求不可能
      真的改动任何站点状态，却能问出「这个 procedure 到底存不存在」——
      返回 `No procedure found on path` 就是不存在。
      一旦配了 Cookie，就绝不再探写端点，避免探测变成一次真实写入。

    返回 [(action, endpoint, result_brief), ...]
    """
    from .endpoints import READONLY_ACTIONS, REGISTRY

    rows: list[tuple[str, str, str]] = []
    write_probe_allowed = not cfg.has_cookie
    with CivitaiClient(cfg, dry_run=False, verbose=verbose) as client:
        for action, candidates in REGISTRY.items():
            readonly = action in READONLY_ACTIONS
            for ep in candidates:
                is_write = ep.method.upper() != "GET"
                if readonly and is_write:
                    continue
                if is_write and not write_probe_allowed:
                    rows.append((action, ep.name,
                                 "SKIP 已有登录态，探写端点会真的写下去。"
                                 "想验证参数形态请跑 dry-run 的 run --only " + action))
                    continue
                res = client.send(ep)
                note = ""
                if is_write:
                    note = ("  ← 端点存在，只差登录态" if res.exists
                            else "  ← 该 procedure 不存在")
                label = ("存在性探测 " + ep.name) if is_write else ep.name
                rows.append((action, label, res.brief() + note))
                if res.ok or (is_write and res.exists):
                    break
    return rows
