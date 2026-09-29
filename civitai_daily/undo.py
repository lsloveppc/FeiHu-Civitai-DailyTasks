"""回滚本工具对账号造成的改动。

设计原则两条：
1. **只碰自己记录过的对象** —— `state.reacted_ids`（本工具点过赞的图）和
   `state.days[*].followed`（本工具关注过的人）。用户自己手动做的操作不在范围内。
2. **动手前先向站点确认当前状态** —— `reaction.toggle` / `user.toggleFollow`
   都是开关语义，盲 toggle 会把用户原本就有的点赞/关注一起取消掉。
   所以流程是「查出仍然存在的痕迹 → 只撤这些 → 撤一个就从记录里摘一个」。

默认演练（dry_run），要真撤销必须显式 live=True。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .client import CivitaiClient
from .config import Config, load_state, save_state

CHUNK = 50


@dataclass
class UndoReport:
    kind: str
    recorded: int = 0
    checked: int = 0
    undone: int = 0
    already_gone: int = 0
    failed: int = 0
    live: bool = False
    details: list[str] = field(default_factory=list)

    def summary(self) -> str:
        head = "已撤销" if self.live else "演练（未真正撤销）"
        return (f"{head} {self.undone} 个 / 记录 {self.recorded} 个"
                f"（已不存在 {self.already_gone}，失败 {self.failed}）")


def _chunks(seq: list[int], size: int) -> Iterable[list[int]]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def undo_likes(cfg: Config, *, live: bool = False,
               limit: int = 0, verbose: bool = False) -> UndoReport:
    state = load_state()
    recorded: list[int] = [int(x) for x in state.get("reacted_ids", [])]
    rep = UndoReport(kind="likes", recorded=len(recorded), live=live)

    if not recorded:
        rep.details.append("记录里没有本工具点过的赞（reacted_ids 为空），无可撤销")
        return rep

    targets = recorded[:limit] if limit else recorded

    with CivitaiClient(cfg, dry_run=not live, verbose=verbose) as client:
        # 先问站点：这些图里哪些现在确实还挂着我们留下的 Like
        active: list[int] = []
        for chunk in _chunks(targets, CHUNK):
            mine = client.my_image_reactions(chunk)
            if mine is None:      # None 才是查询失败；{} 只是这批图都没有我们的赞
                rep.details.append(
                    f"反应状态查询失败（chunk 起始 {chunk[0]}），中止以免误撤")
                break
            active.extend(i for i in chunk if mine.get(str(i)))

        rep.checked = len(targets)
        rep.already_gone = len(targets) - len(active)
        rep.details.append(
            f"记录 {len(targets)} 张，其中当前仍挂着 Like 的 {len(active)} 张")

        done: list[int] = []
        for iid in active:
            res = client.react(iid)          # toggle -> 取消
            if res.ok:
                done.append(iid)
            else:
                rep.failed += 1
                if res.status in (401, 403):
                    rep.details.append("登录态失效，提前中止")
                    break

        rep.undone = len(done)

        if live and done:
            drop = set(done)
            state["reacted_ids"] = [i for i in recorded if i not in drop]
            save_state(state)
            rep.details.append(f"已从去重表摘除 {len(drop)} 条")
    return rep


def undo_follows(cfg: Config, *, live: bool = False,
                 limit: int = 0, verbose: bool = False) -> UndoReport:
    state = load_state()
    recorded: list[int] = []
    for day in state.get("days", {}).values():
        for uid in day.get("followed", []):
            if int(uid) not in recorded:
                recorded.append(int(uid))

    rep = UndoReport(kind="follows", recorded=len(recorded), live=live)
    if not recorded:
        rep.details.append("记录里没有本工具关注过的人，无可撤销")
        return rep

    targets = recorded[:limit] if limit else recorded

    with CivitaiClient(cfg, dry_run=not live, verbose=verbose) as client:
        current_list = client.following_ids()
        if current_list is None:
            rep.details.append("取不到当前关注列表（可能登录态失效），中止以免误撤")
            return rep
        current = set(current_list)

        active = [u for u in targets if u in current]
        rep.checked = len(targets)
        rep.already_gone = len(targets) - len(active)
        rep.details.append(
            f"记录 {len(targets)} 位，其中当前仍在关注中的 {len(active)} 位")

        done: list[int] = []
        for uid in active:
            res = client.follow(uid)         # toggle -> 取消关注
            if res.ok:
                done.append(uid)
            else:
                rep.failed += 1
                if res.status in (401, 403):
                    rep.details.append("登录态失效，提前中止")
                    break

        rep.undone = len(done)

        if live and done:
            drop = set(done)
            for day in state.get("days", {}).values():
                if "followed" in day:
                    day["followed"] = [u for u in day["followed"]
                                       if int(u) not in drop]
            save_state(state)
            rep.details.append(f"已从记录摘除 {len(drop)} 条")
    return rep
