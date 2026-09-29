"""配置与路径管理。

所有可变状态（config / cookies / 运行历史 / 去重记录）都落在 data/ 目录下，
方便备份、迁移和"删除即重置"。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

def _resolve_data_dir() -> Path:
    """数据目录解析。

    源码运行时放项目内 data/（便于备份、迁移、删除即重置）；用 pip 装成包时
    项目根会变成 site-packages，那里通常不可写，就退回用户主目录。
    环境变量 CIVITAI_DAILY_HOME 优先级最高。
    """
    env = os.environ.get("CIVITAI_DAILY_HOME")
    if env:
        return Path(env).expanduser()
    root = Path(__file__).resolve().parent.parent
    if (root / "pyproject.toml").exists():
        return root / "data"
    return Path.home() / ".civitai-daily"


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = _resolve_data_dir()

CONFIG_PATH = DATA_DIR / "config.yaml"
STATE_PATH = DATA_DIR / "state.json"
COOKIE_PATH = DATA_DIR / "cookies.txt"
RUNS_PATH = DATA_DIR / "runs.jsonl"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# 站点别名：.red 与 .com 是 Civitai 官方的两个主域名，后端同源。
KNOWN_SITES = ("https://civitai.red", "https://civitai.com")


@dataclass
class Limits:
    """自我保护型限速参数。Civitai 官方对站内 Buzz 刷取有风控与封禁策略，
    默认值刻意保守：宁可慢，不要把账号玩死。"""

    max_likes: int = 20
    max_follows: int = 10
    max_browses: int = 30
    # dry-run 只用来验证连通性，没必要把 30 次浏览跑满、白等两分钟。
    dry_run_browses: int = 5
    min_delay: float = 2.0
    max_delay: float = 5.0
    max_runtime_seconds: int = 600
    retries: int = 2


@dataclass
class TasksToggle:
    login_reward: bool = True      # buzz.claimDailyBoostReward
    earn_potential: bool = True    # buzz.getEarnPotential
    challenge_daily: bool = True   # challenge.getDaily
    browse: bool = True
    react_images: bool = True
    react_models: bool = False
    follow_creators: bool = True


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8787


@dataclass
class Config:
    site: str = "https://civitai.red"
    cookie: str = ""
    cookie_file: str = ""
    user_agent: str = DEFAULT_UA
    proxy: str = ""
    timeout: float = 30.0
    headless: bool = False

    limits: Limits = field(default_factory=Limits)
    tasks: TasksToggle = field(default_factory=TasksToggle)
    web: WebConfig = field(default_factory=WebConfig)

    # ---------- 加载 / 保存 ----------

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        p = Path(path) if path else CONFIG_PATH
        raw: dict[str, Any] = {}
        if p.exists():
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

        cfg = cls(
            site=str(raw.get("site", cls.site)).rstrip("/"),
            cookie=raw.get("cookie", "") or os.environ.get("CIVITAI_COOKIE", ""),
            cookie_file=raw.get("cookie_file", "") or "",
            user_agent=raw.get("user_agent") or DEFAULT_UA,
            proxy=raw.get("proxy", "") or "",
            timeout=float(raw.get("timeout", 30.0)),
            headless=bool(raw.get("headless", False)),
        )

        if isinstance(raw.get("limits"), dict):
            cfg.limits = Limits(**{k: v for k, v in raw["limits"].items()
                                   if k in Limits.__dataclass_fields__})
        if isinstance(raw.get("tasks"), dict):
            cfg.tasks = TasksToggle(**{k: v for k, v in raw["tasks"].items()
                                       if k in TasksToggle.__dataclass_fields__})
        if isinstance(raw.get("web"), dict):
            cfg.web = WebConfig(**{k: v for k, v in raw["web"].items()
                                   if k in WebConfig.__dataclass_fields__})

        # 环境变量兜底（方便 CI / 计划任务里注入）
        cfg.site = os.environ.get("CIVITAI_SITE", cfg.site).rstrip("/")

        # cookie 优先级：cookie 字段 > cookie_file > data/cookies.txt
        if not cfg.cookie:
            cf = Path(cfg.cookie_file) if cfg.cookie_file else COOKIE_PATH
            if cf.exists():
                cfg.cookie = cf.read_text(encoding="utf-8").strip()
        return cfg

    def save(self, path: Path | None = None) -> Path:
        p = Path(path) if path else CONFIG_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        # cookie 太敏感，不写进 config.yaml，单独落 cookies.txt
        data.pop("cookie", None)
        p.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return p

    # ---------- 便捷属性 ----------

    @property
    def base(self) -> str:
        return self.site.rstrip("/")

    @property
    def has_cookie(self) -> bool:
        return bool(self.cookie.strip())

    def cookie_header(self) -> str:
        return self.cookie.strip()


def ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def write_cookie(cookie: str) -> Path:
    ensure_data_dir()
    COOKIE_PATH.write_text(cookie.strip(), encoding="utf-8")
    return COOKIE_PATH


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        # utf-8-sig 顺手吃掉 BOM：PowerShell 的 Out-File -Encoding utf8 之类
        # 会写 BOM，普通 utf-8 读进来开头多个 \ufeff，json 解析直接失败。
        return json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # 绝对不能静默返回 {}：state 里存着 reacted_ids 去重表，
        # 丢了它下一次 LIVE 会把点过赞的图再点一遍 —— 而 reaction.toggle
        # 是开关语义，等于把自己的赞取消掉。宁可先备份再重建。
        try:
            broken = STATE_PATH.with_name(STATE_PATH.name + ".broken")
            STATE_PATH.replace(broken)
            print(f"[warn] state.json 解析失败（{exc}），已备份为 {broken.name} 并重建")
        except OSError:
            pass
        return {}


def save_state(state: dict[str, Any]) -> None:
    ensure_data_dir()
    prune_days(state)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def prune_days(state: dict[str, Any], keep: int = 30) -> None:
    """只保留最近 N 天的每日记录。

    days 每天新增一条（含当日挑战标题），长期跑下去会无限膨胀；
    reacted_ids 那类需要跨天保留的去重数据不在清理范围内。
    """
    days = state.get("days")
    if not isinstance(days, dict) or len(days) <= keep:
        return
    for key in sorted(days)[:-keep]:
        days.pop(key, None)


def append_run(record: dict[str, Any]) -> None:
    ensure_data_dir()
    with RUNS_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_runs(limit: int = 50) -> list[dict[str, Any]]:
    if not RUNS_PATH.exists():
        return []
    lines = RUNS_PATH.read_text(encoding="utf-8").splitlines()
    out: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
