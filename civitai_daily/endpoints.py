"""端点注册表。

Civitai 前端是 Next.js + tRPC v10，同时保留一层 REST /api/v1。
不同时期 / 不同域名（civitai.red 与 civitai.com）上，同一动作的路径并不总是一致，
社区脚本也各写各的。所以这里不赌单一端点，而是给每个"动作"登记**候选链**：
运行时按顺序试，谁先返回业务成功就用谁，并把命中结果记进 state.json，
下次直接走命中项。

`probe` 命令会把整张表打一遍，把真实可用的端点固化下来。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PLACEHOLDER = "{}"


@dataclass
class Endpoint:
    """一个候选端点。

    kind:
      - "trpc": Civitai 的 tRPC 路由，path 由 name 推导
      - "rest": 普通 REST 路由，path 显式给出
      - "next": Next.js 内置路由（如 NextAuth session）
    """

    name: str
    kind: str = "trpc"
    method: str = "GET"
    path: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def resolve_path(self) -> str:
        if self.kind == "trpc":
            return f"/api/trpc/{self.name}"
        if self.path:
            return self.path
        return f"/api/v1/{self.name}"


def _trpc(name: str, method: str = "GET", payload: dict | None = None, note: str = "") -> Endpoint:
    return Endpoint(name=name, kind="trpc", method=method,
                    payload=payload or {}, note=note)


def _rest(path: str, method: str = "GET", payload: dict | None = None, note: str = "") -> Endpoint:
    return Endpoint(name=path, kind="rest", method=method, path=path,
                    payload=payload or {}, note=note)


# ---------------------------------------------------------------- 会话校验

SESSION_CANDIDATES: list[Endpoint] = [
    Endpoint(name="session", kind="next", method="GET", path="/api/auth/session",
             note="NextAuth 会话端点：未登录返回 {}，登录返回 user 对象"),
    _trpc("user.getSelf", "GET", note="部分分支上的当前用户查询"),
]


# ---------------------------------------------------------------- 图片流（点赞/浏览的素材来源）

IMAGE_FEED_CANDIDATES: list[Endpoint] = [
    _trpc("image.getInfinite", "GET", {
        "limit": 24,
        "sort": "Most Reactions",
        "period": "Day",
        "periodMode": "published",
        "browsingLevel": 1,
        "include": ["cosmetics"],
    }, note="站点首屏瀑布流的实际数据源"),
    _trpc("image.getImagesAsPostsInfinite", "GET", {"limit": 24},
          note="图帖流，部分时期替代 getInfinite"),
    _rest("/api/v1/images", "GET", {"limit": 24, "sort": "Most Reactions", "period": "Day"},
          note="公开 REST 图片列表，无需登录即可读"),
]


IMAGE_DETAIL_CANDIDATES: list[Endpoint] = [
    _trpc("image.get", "GET", {"id": 0},
          note="实测：端点存在（传 id=0 时回业务 404 No image with id 0）"),
    _rest("/api/v1/images/{id}", "GET", note="REST 单图详情，实测可用"),
]


# ---------------------------------------------------------------- 点赞 / 反应

REACT_CANDIDATES: list[Endpoint] = [
    _trpc("reaction.toggle", "POST", {"entityType": "image", "entityId": 0, "reaction": "Like"},
          note="主候选：tRPC 通用反应开关"),
    _trpc("reaction.toggle", "POST", {"imageId": 0, "reaction": "Like"},
          note="同一过程名的旧参数形态"),
    _rest("/api/v1/image-reactions/toggle", "POST", {"imageId": 0, "reaction": "Like"},
          note="早期 REST 反应端点"),
]


MODEL_REACT_CANDIDATES: list[Endpoint] = [
    _trpc("reaction.toggle", "POST", {"entityType": "model", "entityId": 0, "reaction": "Like"},
          note="给模型本体点赞"),
    _trpc("resourceReview.upsert", "POST", {"modelId": 0, "rating": 5},
          note="评分替代路径（会给模型打 5 星）"),
]


# ---------------------------------------------------------------- 关注

FOLLOW_CANDIDATES: list[Endpoint] = [
    _trpc("user.toggleFollow", "POST", {"targetUserId": 0}, note="主候选：切换关注状态"),
    _trpc("user.toggleFollow", "POST", {"userId": 0}, note="旧参数名"),
    _rest("/api/v1/user/follow", "POST", {"targetUserId": 0}, note="REST 关注端点"),
]


# ---------------------------------------------------------------- 每日领取

# 实测结论（无登录态）：user.getUserReward / user.getDailyReward /
# user.claimDailyReward / buzz.claimDailyReward / reward.claimDaily 全部返回
# `No procedure found on path` —— 这些名字都不存在。
#
# 真实名字来自开源仓库 src/server/routers/buzz.router.ts：
#   claimDailyBoostReward  —— 无参 mutation，这就是站点的「每日领取」
#   getClaimStatus / claim —— 需要一个 claim key（DB 表 buzzClaim.key）
#   getEarnPotential       —— 今日还能赚多少 Buzz，站点自己的任务进度表
DAILY_REWARD_CANDIDATES: list[Endpoint] = [
    _trpc("buzz.claimDailyBoostReward", "POST",
          note="真·每日奖励：无参 mutation"),
    _trpc("buzz.claimDailyBoost", "POST", note="过程名变体，待实测"),
    _trpc("buzz.claimDailyReward", "POST", note="过程名变体，实测不存在"),
]


BUZZ_CLAIM_STATUS_CANDIDATES: list[Endpoint] = [
    _trpc("buzz.getClaimStatus", "GET", {"id": "daily-buzz"},
          note="查询某个 claim key 的状态；key 可用 --claim-key 覆盖"),
]


BUZZ_CLAIM_TAKE_CANDIDATES: list[Endpoint] = [
    _trpc("buzz.claim", "POST", {"id": "daily-buzz"},
          note="领取 claim，仅当 getClaimStatus 返回 available 时才是有效操作"),
]


EARN_POTENTIAL_CANDIDATES: list[Endpoint] = [
    _trpc("buzz.getEarnPotential", "GET", {},
          note="今日 Buzz 收益潜力 —— 站点自己的「每日任务进度」"),
]


BUZZ_ACCOUNT_CANDIDATES: list[Endpoint] = [
    _trpc("buzz.getBuzzAccount", "GET",
          note="实测：端点存在，未登录返回 401 UNAUTHORIZED"),
    _trpc("buzz.getUserAccount", "GET", note="实测：同样存在"),
]


# ---------------------------------------------------------------- 回滚用（查询自己留下的痕迹）

MY_REACTIONS_CANDIDATES: list[Endpoint] = [
    _trpc("reaction.getMyImageReactions", "GET", {"imageIds": []},
          note="实测存在：必须传 imageIds 数组，返回 {图片ID: [反应名]}"),
]


FOLLOWING_CANDIDATES: list[Endpoint] = [
    _trpc("user.getFollowingUsers", "GET", {},
          note="实测 200 可用：返回我当前关注的 userId 列表"),
]


CHALLENGE_DAILY_CANDIDATES: list[Endpoint] = [
    _trpc("challenge.getDaily", "GET", note="每日挑战"),
]


# 实测结论：challenge.getInfinite 可用；getUserChallenges / getAll 均 404 不存在。
CHALLENGE_CANDIDATES: list[Endpoint] = [
    _trpc("challenge.getInfinite", "GET", {"limit": 20}, note="实测 200 可用"),
    _trpc("challenge.getDaily", "GET", note="每日挑战"),
    _trpc("challenge.getUserChallenges", "GET", note="实测不存在"),
    _trpc("challenge.getAll", "GET", note="实测不存在"),
    _rest("/api/v1/challenges", "GET", note="REST 备用"),
]


# ---------------------------------------------------------------- 汇总

REGISTRY: dict[str, list[Endpoint]] = {
    "session": SESSION_CANDIDATES,
    "image_feed": IMAGE_FEED_CANDIDATES,
    "image_detail": IMAGE_DETAIL_CANDIDATES,
    "react": REACT_CANDIDATES,
    "react_model": MODEL_REACT_CANDIDATES,
    "follow": FOLLOW_CANDIDATES,
    "buzz_account": BUZZ_ACCOUNT_CANDIDATES,
    "daily_reward": DAILY_REWARD_CANDIDATES,
    "claim_status": BUZZ_CLAIM_STATUS_CANDIDATES,
    "claim_take": BUZZ_CLAIM_TAKE_CANDIDATES,
    "earn_potential": EARN_POTENTIAL_CANDIDATES,
    "challenge_daily": CHALLENGE_DAILY_CANDIDATES,
    "challenge": CHALLENGE_CANDIDATES,
    "my_reactions": MY_REACTIONS_CANDIDATES,
    "following": FOLLOWING_CANDIDATES,
}

# 这些动作下的 GET 候选可以安全探测；其余动作里的 POST 候选只在没有登录态时
# 做存在性探测（未登录必被鉴权拦下，探测本身不可能改动站点状态）。
READONLY_ACTIONS = {"session", "image_feed", "image_detail", "buzz_account",
                    "challenge", "challenge_daily", "earn_potential",
                    "claim_status", "my_reactions", "following"}
