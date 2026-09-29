"""HTTP 客户端：tRPC v10 batch 协议 + 多候选端点自动命中 + 限速。

设计要点
--------
1. 一个"动作"（action）对应一条候选端点链，逐个试，命中即停并落盘。
2. 写操作受 dry_run 与 limits 双重约束，默认不发出任何会改变站点状态的请求。
3. 所有请求之间插入随机间隔，并带完整的浏览器风格头，避免被当成明显的脚本流量。
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from .config import Config, load_state, save_state
from .endpoints import REGISTRY, Endpoint


class ApiError(RuntimeError):
    pass


class EndpointMissing(ApiError):
    """tRPC 明确回答 `No procedure found on path "xxx"`。

    这和「端点存在但这次业务失败」是两回事：前者该继续试下一个候选，
    后者说明候选是对的、只是参数或权限不对。探活报告必须把这两者分开，
    否则你会以为端点没写对，而去改一个本来正确的路径。
    """


@dataclass
class ApiResult:
    ok: bool
    status: int
    data: Any = None
    error: str = ""
    endpoint: str = ""
    elapsed_ms: int = 0
    raw_text: str = ""
    exists: bool = True
    dry_run: bool = False

    def brief(self) -> str:
        if self.ok:
            return f"OK {self.status} via {self.endpoint} ({self.elapsed_ms}ms)"
        if not self.exists:
            return f"MISSING {self.status} via {self.endpoint} :: {self.error[:140]}"
        return f"EXISTS/BLOCKED {self.status} via {self.endpoint} :: {self.error[:140]}"


@dataclass
class RateLimiter:
    min_delay: float = 2.0
    max_delay: float = 5.0
    _last: float = field(default=0.0, init=False)

    def wait(self) -> float:
        if self._last:
            target = random.uniform(self.min_delay, self.max_delay)
            elapsed = time.monotonic() - self._last
            if elapsed < target:
                time.sleep(target - elapsed)
        self._last = time.monotonic()
        return self._last


class CivitaiClient:
    def __init__(self, cfg: Config, *, dry_run: bool = True, verbose: bool = False,
                 state: dict[str, Any] | None = None):
        self.cfg = cfg
        self.dry_run = dry_run
        self.verbose = verbose
        self.limiter = RateLimiter(cfg.limits.min_delay, cfg.limits.max_delay)
        self.log: list[str] = []

        headers = {
            "User-Agent": cfg.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
            "Origin": cfg.base,
            "Referer": cfg.base + "/",
            "Content-Type": "application/json",
        }
        if cfg.cookie_header():
            headers["Cookie"] = cfg.cookie_header()

        kwargs: dict[str, Any] = {
            "base_url": cfg.base,
            "headers": headers,
            "timeout": cfg.timeout,
            "follow_redirects": True,
        }
        if cfg.proxy:
            kwargs["proxy"] = cfg.proxy
        self.http = httpx.Client(**kwargs)

        # 与 runner 共享同一个 state 对象，否则 persist_hits 与 save_state
        # 会互相覆盖，端点命中表会被吃掉。
        self._state = state if state is not None else load_state()
        self._hits: dict[str, str] = self._state.setdefault("endpoint_hits", {})

    # ------------------------------------------------------------------ 基础设施

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "CivitaiClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _say(self, msg: str) -> None:
        self.log.append(msg)
        if self.verbose:
            print(msg, flush=True)

    def persist_hits(self) -> None:
        self._state["endpoint_hits"] = self._hits
        save_state(self._state)

    # ------------------------------------------------------------------ tRPC 打包

    @staticmethod
    def _encode_batch(payload: dict[str, Any]) -> str:
        return quote(json.dumps({"0": {"json": payload}}, separators=(",", ":")))

    @staticmethod
    def _devalue_parse(serialized: str) -> Any:
        """解码 devalue.stringify 的扁平索引格式。

        Civitai 的部分 tRPC 过程（实测 image.getInfinite）返回的 data 不是
        JSON 对象，而是一段 devalue 序列化字符串：一个扁平数组，根部在 [0]，
        其余元素按索引被引用，负数是 undefined / hole / NaN 等哨兵：

            [{"items":1,"nextCursor":124}, [2,77], {"id":3,"name":4}, 144079756, "abc"]

        不解码就只能拿到一个字符串，图片流会整个空掉。
        """
        values = json.loads(serialized)
        if not isinstance(values, list):
            return values

        sentinel = {-1: None, -2: None, -3: float("nan"),
                    -4: float("inf"), -5: float("-inf"), -6: 0.0}
        memo: dict[int, Any] = {}

        def hydrate(index: Any, depth: int = 0) -> Any:
            if depth > 48 or isinstance(index, bool):
                return index
            if not isinstance(index, int):
                return index          # 字符串 / 浮点等字面量
            if index < 0:
                return sentinel.get(index)
            if index >= len(values):
                return None
            if index in memo:         # devalue 允许共享引用甚至成环
                return memo[index]
            value = values[index]
            if isinstance(value, list):
                out: list[Any] = []
                memo[index] = out
                out.extend(hydrate(v, depth + 1) for v in value)
                return out
            if isinstance(value, dict):
                obj: dict[str, Any] = {}
                memo[index] = obj
                for k, v in value.items():
                    obj[k] = hydrate(v, depth + 1)
                return obj
            memo[index] = value
            return value

        return hydrate(0)

    @staticmethod
    def _decode_payload(data: Any) -> Any:
        """data 可能是对象、{"json": ...} 包装，或 devalue 字符串，统一归一。"""
        if isinstance(data, dict) and "json" in data:
            data = data["json"]
        if isinstance(data, str) and data[:1] in ("[", "{"):
            try:
                return CivitaiClient._devalue_parse(data)
            except (json.JSONDecodeError, ValueError, RecursionError, TypeError):
                return data
        return data

    @staticmethod
    def _unwrap(obj: Any) -> Any:
        """把 tRPC 响应剥到业务数据层。

        tRPC v10 的错误负载是两层：{"error": {"json": {"message": ...}}}。
        少剥一层就只能看到一坨 JSON，判断不了到底是端点不存在还是权限不足。
        """
        if isinstance(obj, list):
            obj = obj[0] if obj else {}
        if isinstance(obj, dict):
            if obj.get("error"):
                err = obj["error"]
                inner = err.get("json", err) if isinstance(err, dict) else err
                if isinstance(inner, dict):
                    msg = inner.get("message") or json.dumps(inner, ensure_ascii=False)[:300]
                    code = (inner.get("data") or {}).get("httpStatus")
                else:
                    msg, code = str(inner), None
                if "No procedure found on path" in msg:
                    raise EndpointMissing(msg)
                raise ApiError(f"tRPC error {code}: {msg}")
            res = obj.get("result")
            if isinstance(res, dict):
                return CivitaiClient._decode_payload(res.get("data"))
        return obj

    # ------------------------------------------------------------------ 单次请求

    def send(self, ep: Endpoint, payload: dict[str, Any] | None = None,
             write: bool = False) -> ApiResult:
        """按端点定义发一次请求。write=True 表示这会改变站点状态。"""
        body = dict(ep.payload)
        if ep.kind == "rest" and "{id}" in ep.path:
            # REST 模板路径需要调用方通过 payload 提供 id
            path = ep.path.replace("{id}", str((payload or {}).get("id", "")))
        else:
            path = ep.resolve_path()
        if payload:
            body.update(payload)

        if ep.kind == "trpc":
            method = ep.method.upper()
            if method == "GET":
                url = f"{path}?batch=1&input={self._encode_batch(body)}"
                request_body = None
            else:
                url = f"{path}?batch=1"
                request_body = json.dumps({"0": {"json": body}}, separators=(",", ":"))
        elif ep.kind == "rest":
            method = ep.method.upper()
            if method == "GET" and body:
                from urllib.parse import urlencode
                url = f"{path}?{urlencode(body)}"
            else:
                url = path
            request_body = json.dumps(body) if method != "GET" else None
        else:  # next
            method = ep.method.upper()
            url = path
            request_body = None

        # 只要不是只读方法，就一律按写操作对待 —— 不能指望每个调用方都记得
        # 传 write=True。实测就因为漏传，把一次只读探测变成了真实的点赞 + 关注，
        # 事后得手工回滚。
        is_write = write or method.upper() not in ("GET", "HEAD")
        if is_write and self.dry_run:
            return ApiResult(ok=True, status=0, data={"dry_run": True},
                             endpoint=ep.name, error="",
                             raw_text="[dry-run] 未发出写请求", dry_run=True)

        self.limiter.wait()
        t0 = time.monotonic()
        try:
            resp = self.http.request(method, url, content=request_body)
        except httpx.HTTPError as exc:
            return ApiResult(ok=False, status=-1, error=f"网络错误: {exc}",
                             endpoint=ep.name,
                             elapsed_ms=int((time.monotonic() - t0) * 1000))
        elapsed = int((time.monotonic() - t0) * 1000)
        text = resp.text[:4000]

        # 错误响应也要解析：tRPC 把「端点不存在」和「权限不足」都塞在 body 里，
        # 只看 HTTP 状态码会把 404 与 401 的语义丢掉，候选链就没法正确降级。
        data: Any = None
        err = ""
        exists = True
        try:
            parsed: Any = resp.json()
        except (json.JSONDecodeError, ValueError):
            parsed = None

        if parsed is not None:
            try:
                data = self._unwrap(parsed)
            except EndpointMissing as exc:
                err, exists = str(exc), False
            except ApiError as exc:
                err = str(exc)
        elif resp.status_code >= 400:
            ctype = resp.headers.get("content-type", "")
            if resp.status_code == 404 and "text/html" in ctype:
                # Next.js 对未知路径统一回 HTML 404，可以据此判定路径不存在。
                err, exists = f"路径不存在（HTML 404）: {text[:120]}", False
            else:
                err = f"非 JSON 响应 {resp.status_code}: {text[:200]}"
        else:
            err = "响应不是 JSON（可能是风控页或重定向到登录）"

        if resp.status_code >= 400 and not err:
            err = text[:300]

        return ApiResult(ok=(not err) and resp.status_code < 400,
                         status=resp.status_code, data=data, error=err,
                         endpoint=ep.name, elapsed_ms=elapsed, raw_text=text,
                         exists=exists)

    # ------------------------------------------------------------------ 候选链调用

    def call(self, action: str, *, write: bool = False,
             payload: dict[str, Any] | None = None) -> ApiResult:
        """按候选链顺序尝试某个动作，命中即记住。"""
        candidates = REGISTRY.get(action)
        if not candidates:
            raise KeyError(f"未注册的动作: {action}")

        ordered = list(candidates)
        hit = self._hits.get(action)
        if hit:
            ordered.sort(key=lambda e: 0 if e.name == hit else 1)

        last: ApiResult | None = None
        for ep in ordered:
            res = self.send(ep, payload, write=write)
            if res.ok:
                if res.dry_run:
                    # 演练短路出来的"成功"没验证过任何东西，不能写进命中表，
                    # 否则会污染后续 LIVE 运行的端点选择。
                    return res
                if self._hits.get(action) != ep.name:
                    self._hits[action] = ep.name
                    self.persist_hits()
                    self._say(f"[endpoint] {action} -> {ep.name}")
                return res
            last = res
            # 401/403 说明登录态问题，换端点也没用，直接返回
            if res.status in (401, 403):
                return res
        return last or ApiResult(ok=False, status=-1, error="无候选端点",
                                 endpoint="<none>")

    # ------------------------------------------------------------------ 语义化封装

    def session_info(self) -> dict[str, Any] | None:
        res = self.call("session")
        if res.ok and isinstance(res.data, dict) and res.data.get("user"):
            return res.data["user"]
        return None

    def buzz_account(self) -> dict[str, Any] | None:
        res = self.call("buzz_account")
        return res.data if res.ok and isinstance(res.data, dict) else None

    def fetch_image_ids(self, limit: int = 24) -> list[dict[str, Any]]:
        """拉一批图片条目，用于点赞与浏览。"""
        res = self.call("image_feed", payload={"limit": limit})
        if not res.ok:
            return []
        data = res.data
        items: list[dict[str, Any]] = []
        if isinstance(data, dict):
            items = data.get("items") or data.get("images") or []
        elif isinstance(data, list):
            items = data
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            iid = it.get("id")
            if iid is None:
                continue
            out.append({
                "id": int(iid),
                "username": it.get("username") or (it.get("user") or {}).get("username"),
                "userId": it.get("userId") or (it.get("user") or {}).get("id"),
                "url": it.get("url"),
                "nsfwLevel": it.get("nsfwLevel"),
                "stats": it.get("stats") or {},
            })
        return out

    def my_image_reactions(self, image_ids: list[int]) -> dict[str, Any] | None:
        """查这些图当前挂在我账号上的 reaction，返回 {图片ID字符串: [反应名]}。

        回滚点赞之前必须先问这个：只有当某张图"现在确实还挂着我们留下的 Like"
        时才去取消，否则会把用户自己原本的点赞一起抹掉。

        查询失败返回 **None**，区别于「这些图都没有我的 reaction」的合法空结果 `{}` ——
        把两者混为一谈会让回滚逻辑误判并跳过后面的批次。
        """
        if not image_ids:
            return {}
        res = self.call("my_reactions", payload={"imageIds": list(image_ids)})
        if res.ok and isinstance(res.data, dict):
            return res.data
        return None

    def following_ids(self) -> list[int] | None:
        """我当前关注的所有 userId；取不到时返回 None（而不是空列表）。

        空列表是合法状态（一个都没关注），不能和失败混为一谈。
        """
        res = self.call("following", payload={})
        if res.ok and isinstance(res.data, list):
            return [int(x) for x in res.data if isinstance(x, int)]
        return None

    def react(self, image_id: int, reaction: str = "Like") -> ApiResult:
        return self.call("react", write=True,
                         payload={"entityId": image_id, "entityType": "image",
                                  "reaction": reaction, "imageId": image_id})

    def follow(self, user_id: int) -> ApiResult:
        return self.call("follow", write=True,
                         payload={"targetUserId": user_id, "userId": user_id})

    def browse(self, image_id: int) -> ApiResult:
        return self.call("image_detail", payload={"id": image_id})
