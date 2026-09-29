"""任务模块。

每个任务返回一个 TaskOutcome，runner 汇总成报告。任务本身不做 IO 调度，
只依赖 TaskContext 里的 client / state，便于单测与 dry-run。
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from .client import CivitaiClient

# 状态语义
DONE = "done"            # 已成功完成
PARTIAL = "partial"      # 部分完成（撞到上限 / 有单项失败）
SKIPPED = "skipped"      # 被配置关掉，或已无可用素材
UNSUPPORTED = "unsupported"  # 站点本就没有该机制 / 未找到可用端点
FAILED = "failed"        # 明确报错


@dataclass
class TaskOutcome:
    key: str
    label: str
    status: str
    detail: str = ""
    count: int = 0
    samples: list[str] = field(default_factory=list)


@dataclass
class TaskContext:
    client: CivitaiClient
    state: dict[str, Any]
    today: str
    dry_run: bool
    _progress: Callable[[str], None] | None = None

    def progress(self, msg: str) -> None:
        """把任务内部的细粒度进度透出去（界面靠它显示 12/30 这种计数）。"""
        if self._progress:
            try:
                self._progress(msg)
            except Exception:
                pass

    @property
    def day(self) -> dict[str, Any]:
        days = self.state.setdefault("days", {})
        return days.setdefault(self.today, {})

    @property
    def reacted_ids(self) -> set[int]:
        return set(self.state.setdefault("reacted_ids", []))

    def today_done(self, bucket: str) -> list:
        """今天的真实完成量。

        演练模式下恒为空：dry-run 有自己独立且小得多的预算，如果参照 LIVE 跑出来的
        真实记录（比如 LIVE 已浏览 30 条、而 dry-run 预算只有 5），remaining 会直接
        变成负数，演练就整个跳过了 —— 那 dry-run 也就失去了「看它会怎么做」的意义。
        """
        if self.dry_run:
            return []
        return self.day.setdefault(bucket, [])

    def mark_reacted(self, image_id: int) -> None:
        # dry-run 下不落盘：写请求根本没发出去，记进去重表会让下一次
        # LIVE 运行误以为已经点过，从而漏掉真实点赞。
        if self.dry_run:
            return
        ids = self.state.setdefault("reacted_ids", [])
        if image_id not in ids:
            ids.append(image_id)
        # 只保留最近 5000 条，避免无限增长
        if len(ids) > 5000:
            self.state["reacted_ids"] = ids[-5000:]

    def mark(self, bucket: str, value: int) -> None:
        if self.dry_run:
            return
        arr = self.day.setdefault(bucket, [])
        if value not in arr:
            arr.append(value)


# --------------------------------------------------------------------- 账号状态

def task_account_status(ctx: TaskContext) -> TaskOutcome:
    user = ctx.client.session_info()
    if not user:
        return TaskOutcome(
            "account_status", "登录态校验", FAILED,
            "未能从 /api/auth/session 取到用户对象：Cookie 可能已过期，"
            "或被站点风控页拦截。请重新导出 Cookie。",
        )
    name = user.get("username") or user.get("name") or "?"
    uid = user.get("id")
    buzz = ctx.client.buzz_account()
    detail = f"已登录：{name} (id={uid})"
    if isinstance(buzz, dict):
        balance = buzz.get("balance") or buzz.get("buzzBalance")
        if balance is not None:
            detail += f"，Buzz 余额 {balance}"
    ctx.day["account"] = {"id": uid, "username": name}
    return TaskOutcome("account_status", "登录态校验", DONE, detail)


# --------------------------------------------------------------------- 每日奖励

def task_daily_reward(ctx: TaskContext) -> TaskOutcome:
    """领取每日 Boost 奖励。

    对应 `buzz.claimDailyBoostReward` —— 无参 mutation，实测该端点存在
    （无登录态返回 401 而非 404）。这是站点上最接近传统「每日签到」的东西。
    """
    if not ctx.dry_run and ctx.day.get("daily_reward_claimed"):
        return TaskOutcome("daily_reward", "每日奖励领取", SKIPPED, "今日已领取")

    result = ctx.client.call("daily_reward", write=True)
    if result.status in (401, 403):
        return TaskOutcome(
            "daily_reward", "每日奖励领取", FAILED,
            "401/403：端点存在，但当前 Cookie 没有有效登录态。重新 `login` 后再试。",
        )
    if not result.ok:
        if not result.exists:
            return TaskOutcome(
                "daily_reward", "每日奖励领取", UNSUPPORTED,
                f"候选过程名全部不存在：{result.error[:140]}")
        return TaskOutcome("daily_reward", "每日奖励领取", FAILED, result.brief())

    if result.dry_run:
        return TaskOutcome("daily_reward", "每日奖励领取", DONE,
                           f"dry-run 演练：未真实调用 {result.endpoint}，"
                           "加 --live 才会真的领取")

    if not ctx.dry_run:
        # dry-run 下不落盘，否则下一次 LIVE 会被误跳过。
        ctx.day["daily_reward_claimed"] = True

    detail = f"命中 {result.endpoint}"
    data = result.data
    if isinstance(data, dict):
        for key in ("amount", "buzz", "reward", "total"):
            if data.get(key) is not None:
                detail += f"，发放 {data[key]} Buzz"
                break
        else:
            if data:
                detail += f"，返回 {str(data)[:120]}"
    return TaskOutcome("daily_reward", "每日奖励领取", DONE, detail)


def task_earn_potential(ctx: TaskContext) -> TaskOutcome:
    """查询今日 Buzz 收益潜力。

    `buzz.getEarnPotential` 是站点自己的「今天还能赚多少」报表。读它就等于
    读官方口径的每日任务进度，比我们本地的计数器可信得多。
    """
    res = ctx.client.call("earn_potential")
    if res.status in (401, 403):
        return TaskOutcome("earn_potential", "今日收益潜力", FAILED,
                           "401/403：端点存在但未登录，无法取官方进度")
    if not res.ok:
        return TaskOutcome("earn_potential", "今日收益潜力", UNSUPPORTED, res.brief())

    data = res.data
    ctx.day["earn_potential"] = data
    if isinstance(data, dict):
        nums = [f"{k}={v}" for k, v in data.items() if isinstance(v, (int, float))]
        detail = ("今日潜力：" + "，".join(nums[:6])) if nums else \
                 ("已取到潜力数据：" + json.dumps(data, ensure_ascii=False)[:200])
    else:
        detail = f"已取到潜力数据：{str(data)[:200]}"
    return TaskOutcome("earn_potential", "今日收益潜力", DONE, detail)


def task_challenge_daily(ctx: TaskContext) -> TaskOutcome:
    """读取每日挑战。

    `challenge.getDaily` 实测 200 且**不需要登录**，所以这里能拿到真实数据。
    但投稿环节要求真实生成图片（消耗自己的 Buzz 并等待队列），按能力矩阵
    交还给你手动完成，这里只把当日主题列出来。
    """
    res = ctx.client.call("challenge_daily")
    if not res.ok:
        return TaskOutcome("challenge_daily", "每日挑战", UNSUPPORTED, res.brief())

    data = res.data
    items: list[Any] = []
    if isinstance(data, dict):
        for key in ("items", "challenges", "daily", "data"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
    elif isinstance(data, list):
        items = data

    titles: list[str] = []
    for it in items[:6]:
        if isinstance(it, dict):
            t = it.get("title") or it.get("name") or it.get("theme")
            if t:
                titles.append(str(t))

    ctx.day["challenges"] = titles
    if titles:
        return TaskOutcome("challenge_daily", "每日挑战", DONE,
                           f"当日 {len(items)} 个挑战（投稿需你手动做）",
                           count=len(items), samples=titles)
    if items:
        return TaskOutcome("challenge_daily", "每日挑战", DONE,
                           f"取到 {len(items)} 条挑战数据，但未识别出标题字段",
                           count=len(items))
    return TaskOutcome("challenge_daily", "每日挑战", UNSUPPORTED,
                       f"端点可用（{res.endpoint}）但当前没有进行中的每日挑战")


# --------------------------------------------------------------------- 浏览

def task_browse(ctx: TaskContext) -> TaskOutcome:
    cfg = ctx.client.cfg
    budget = cfg.limits.dry_run_browses if ctx.dry_run else cfg.limits.max_browses
    done = ctx.today_done("browsed")
    remaining = budget - len(done)
    if remaining <= 0:
        return TaskOutcome("browse", "浏览图片/模型", SKIPPED, f"已达上限 {budget}")

    feed = ctx.client.fetch_image_ids(limit=max(24, remaining * 2))
    if not feed:
        return TaskOutcome("browse", "浏览图片/模型", UNSUPPORTED,
                           "图片流端点未取到数据，无法生成浏览行为")
    ctx.progress(f"0/{remaining}")
    ok = 0
    for item in feed:
        if ok >= remaining:
            break
        if item["id"] in done:
            continue
        res = ctx.client.browse(item["id"])
        if res.ok:
            ctx.mark("browsed", item["id"])
            ok += 1
            ctx.progress(f"{ok}/{remaining}")
    status = DONE if ok else FAILED
    return TaskOutcome("browse", "浏览图片/模型", status,
                       f"完成 {ok} 次浏览（上限 {budget}）", count=ok)


# --------------------------------------------------------------------- 点赞

def task_react_images(ctx: TaskContext) -> TaskOutcome:
    """给图片点 Like。

    注意：reaction.toggle 是**开关**。重复调用会取消已有点赞，
    所以本地必须记住点过的 ID，第二次运行不会把赞取消掉。
    """
    cfg = ctx.client.cfg
    budget = cfg.limits.max_likes
    already_today = ctx.today_done("liked")
    # 演练不参照永久去重表，否则第二次演练就没东西可演示了（也不会真发请求，无副作用）
    reacted = set() if ctx.dry_run else ctx.reacted_ids
    remaining = budget - len(already_today)
    if remaining <= 0:
        return TaskOutcome("react_images", "点赞图片", SKIPPED, f"今日已达上限 {budget}")

    feed = ctx.client.fetch_image_ids(limit=60)
    if not feed:
        return TaskOutcome("react_images", "点赞图片", UNSUPPORTED,
                           "图片流端点未取到数据，无法点赞")

    ok, fails = 0, 0
    samples: list[str] = []
    for item in feed:
        if ok >= remaining:
            break
        iid = item["id"]
        if iid in reacted or iid in already_today:
            continue
        res = ctx.client.react(iid)
        if res.ok:
            ctx.mark("liked", iid)
            ctx.mark_reacted(iid)
            ok += 1
            ctx.progress(f"{ok}/{remaining}")
            if item.get("username") and len(samples) < 5:
                samples.append(f"#{iid} @{item['username']}")
        else:
            fails += 1
            if res.status in (401, 403):
                break

    if ok and fails:
        return TaskOutcome("react_images", "点赞图片", PARTIAL,
                           f"成功 {ok}，失败 {fails}（上限 {budget}）",
                           count=ok, samples=samples)
    if ok:
        note = "（dry-run 演练，未真实点赞）" if ctx.dry_run else ""
        return TaskOutcome("react_images", "点赞图片", DONE,
                           f"成功 {ok} 次（今日上限 {budget}）{note}",
                           count=ok, samples=samples)
    return TaskOutcome("react_images", "点赞图片", FAILED,
                       f"全部失败（{fails} 次），首个错误：{res.brief() if fails else '无素材'}")


def task_react_models(ctx: TaskContext) -> TaskOutcome:
    """模型点赞。默认关闭：模型 reaction 与图片 reaction 是两套实体，
    误打会把别人的模型刷上不该有的热度，所以只在你显式打开时执行。"""
    if not ctx.dry_run:
        return TaskOutcome("react_models", "点赞模型", SKIPPED,
                           "需要指定具体模型 ID，默认不执行")
    return TaskOutcome("react_models", "点赞模型", SKIPPED, "未启用")


# --------------------------------------------------------------------- 关注

def task_follow_creators(ctx: TaskContext) -> TaskOutcome:
    cfg = ctx.client.cfg
    budget = cfg.limits.max_follows
    done = ctx.today_done("followed")
    remaining = budget - len(done)
    if remaining <= 0:
        return TaskOutcome("follow_creators", "关注创作者", SKIPPED, f"今日已达上限 {budget}")

    feed = ctx.client.fetch_image_ids(limit=60)
    if not feed:
        return TaskOutcome("follow_creators", "关注创作者", UNSUPPORTED,
                           "无法取得创作者列表")

    seen: set[int] = set()
    ok, fails = 0, 0
    samples: list[str] = []
    last_err = ""
    for item in feed:
        if ok >= remaining:
            break
        uid = item.get("userId")
        if not uid or uid in seen or uid in done:
            continue
        seen.add(uid)
        res = ctx.client.follow(int(uid))
        if res.ok:
            ctx.mark("followed", int(uid))
            ok += 1
            ctx.progress(f"{ok}/{remaining}")
            if item.get("username") and len(samples) < 5:
                samples.append(f"@{item['username']}")
        else:
            fails += 1
            last_err = res.brief()
            if res.status in (401, 403):
                break

    if ok:
        note = "（dry-run 演练，未真实关注）" if ctx.dry_run else ""
        return TaskOutcome("follow_creators", "关注创作者", DONE,
                           f"成功关注 {ok} 位（今日上限 {budget}）{note}",
                           count=ok, samples=samples)
    return TaskOutcome("follow_creators", "关注创作者", FAILED,
                       f"未能关注任何人。{last_err}")


# --------------------------------------------------------------------- 注册表

TASKS: dict[str, tuple[str, Callable[[TaskContext], TaskOutcome]]] = {
    "account_status": ("登录态校验", task_account_status),
    "daily_reward": ("每日奖励领取", task_daily_reward),
    "earn_potential": ("今日收益潜力", task_earn_potential),
    "challenge_daily": ("每日挑战", task_challenge_daily),
    "browse": ("浏览图片/模型", task_browse),
    "react_images": ("点赞图片", task_react_images),
    "react_models": ("点赞模型", task_react_models),
    "follow_creators": ("关注创作者", task_follow_creators),
}


def today_str() -> str:
    return _dt.date.today().isoformat()
