"""命令行入口。"""

from __future__ import annotations

import json
import time

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import (
    CONFIG_PATH, COOKIE_PATH, DATA_DIR, Config, ensure_data_dir, load_state,
    read_runs, write_cookie,
)
from .runner import probe as run_probe, run_all
from .tasks import FAILED, UNSUPPORTED

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="绯狐C站日常任务 — civitai.red / civitai.com 每日任务一键执行器")
console = Console()


def _banner() -> None:
    console.print(Panel.fit(
        f"[bold red]绯狐C站日常任务[/] v{__version__}\n"
        f"数据目录: {DATA_DIR}",
        border_style="red",
    ))


@app.command()
def init(force: bool = typer.Option(False, "--force", help="覆盖已存在的配置")) -> None:
    """生成默认配置文件与数据目录。"""
    _banner()
    ensure_data_dir()
    if CONFIG_PATH.exists() and not force:
        console.print(f"[yellow]配置已存在[/] {CONFIG_PATH}（加 --force 覆盖）")
    else:
        cfg = Config()
        cfg.save()
        console.print(f"[green]已写入[/] {CONFIG_PATH}")
    console.print(f"Cookie 文件路径: {COOKIE_PATH}")
    console.print("\n下一步： [bold]civitai-daily login[/] 或把浏览器 Cookie 粘进上面那个文件。")


SESSION_COOKIE_NAMES = ("__Secure-next-auth.session-token",
                        "next-auth.session-token",
                        "__Host-next-auth.session-token")


def _launch_browser(p, channel: str, cfg: Config):
    """优先复用系统已经装好的 Chrome / Edge。

    这样就不必再跑 `playwright install chromium`（要额外下 100 多 MB），
    而且用的是你平时登录过的那个浏览器内核，过 Cloudflare 更顺。
    """
    candidates = [channel] if channel else ["chrome", "msedge"]
    last_err: Exception | None = None
    for ch in candidates:
        try:
            return p.chromium.launch(channel=ch, headless=cfg.headless)
        except Exception as exc:      # 该通道没装，换下一个
            last_err = exc
    try:
        return p.chromium.launch(headless=cfg.headless)   # 回退到自带 chromium
    except Exception:
        console.print("[red]启动浏览器失败：[/red] " + str(last_err))
        console.print("装了 playwright 但没装浏览器内核时，先跑一次： playwright install chromium")
        raise typer.Exit(code=1)


@app.command()
def login(
    timeout: int = typer.Option(300, help="等待登录成功的秒数"),
    channel: str = typer.Option("", help="浏览器通道：chrome / msedge；留空自动挑"),
) -> None:
    """打开真实浏览器手动登录，自动检测登录态并抓取 Cookie 落盘。

    需要可选依赖： pip install "civitai-daily[browser]"
    默认复用系统已装的 Chrome / Edge，不需要额外下载 Chromium。
    """
    cfg = Config.load()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        console.print("[red]未安装 playwright。[/red] 先执行：")
        console.print('  pip install "civitai-daily[browser]"')
        console.print("\n也可以手动复制 Cookie，写进： " + str(COOKIE_PATH))
        console.print("手动取法见 README 的「怎么拿 Cookie」一节。")
        raise typer.Exit(code=1)

    console.print(f"[cyan]正在打开 {cfg.base} …[/cyan]")
    cookie_str = ""
    found = False

    with sync_playwright() as p:
        browser = _launch_browser(p, channel, cfg)
        context = browser.new_context(user_agent=cfg.user_agent)
        page = context.new_page()
        page.goto(cfg.base, timeout=90000)
        console.print(
            "[yellow]请在打开的浏览器里完成登录（含 Cloudflare 人机校验）。[/yellow]\n"
            f"检测到 session cookie 就自动收工，最长等 {timeout} 秒。")

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                names = {c["name"] for c in context.cookies()}
            except Exception:
                break
            if any(n in names for n in SESSION_COOKIE_NAMES):
                found = True
                break
            page.wait_for_timeout(1500)

        cookies = context.cookies()
        browser.close()

    if not cookies:
        console.print("[red]一条 Cookie 都没抓到。[/red]")
        raise typer.Exit(code=1)

    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    write_cookie(cookie_str)
    console.print(f"已保存 {len(cookies)} 条 Cookie 到 {COOKIE_PATH}")

    if not found:
        console.print("[yellow]没等到 session cookie —— 有可能你还没登录完。[/yellow]"
                      "可以先 `status` 看看，或者重新跑一次 login。")
        raise typer.Exit(code=1)

    # 落盘之后立刻验一次，避免把一个已经失效的会话存进去还告诉你成功了
    cfg.cookie = cookie_str
    from .client import CivitaiClient
    with CivitaiClient(cfg, dry_run=True) as client:
        user = client.session_info()
    if user:
        console.print(f"[green]登录成功：{user.get('username')} (id={user.get('id')})[/green]")
        console.print("下一步： [bold]civitai-daily run --live[/bold]")
    else:
        console.print("[yellow]/api/auth/session 没返回用户对象。[/yellow] "
                      "多半是 Cloudflare 校验没过，或 Cookie 里缺 session token。")


@app.command()
def cookie(
    show: bool = typer.Option(False, "--show", help="打印已保存 Cookie（注意脱敏）"),
    clear: bool = typer.Option(False, "--clear", help="删除已保存的 Cookie"),
) -> None:
    """查看 / 清除已保存的 Cookie。"""
    _banner()
    if clear:
        if COOKIE_PATH.exists():
            COOKIE_PATH.unlink()
            console.print("[green]已删除[/green] " + str(COOKIE_PATH))
        else:
            console.print("本来就没有保存过 Cookie。")
        return

    cfg = Config.load()
    if not cfg.has_cookie:
        console.print("[red]未配置 Cookie。[/red] 跑 `civitai-daily login`，"
                      "或按 README 手动取一份写进 " + str(COOKIE_PATH))
        return

    names = [part.split("=", 1)[0].strip()
             for part in cfg.cookie.split(";") if "=" in part]
    console.print(f"共 {len(names)} 条 Cookie：")
    for n in names:
        mark = " [green]<- 登录态[/green]" if n in SESSION_COOKIE_NAMES else ""
        console.print(f"  · {n}{mark}")
    has_cf = any(n == "cf_clearance" for n in names)
    console.print(f"cf_clearance: {'有' if has_cf else '[yellow]没有（可能被 Cloudflare 拦）[/yellow]'}")
    if show:
        head = cfg.cookie[:120]
        console.print("\n[dim]头 120 字符：" + head + " …[/dim]")


def _do_probe(verbose: bool = False) -> None:
    cfg = Config.load()
    if not cfg.has_cookie:
        console.print("[red]没有 Cookie，探活结果会全是 401。[/] 先跑 civitai-daily login")
    rows = run_probe(cfg, verbose=verbose)
    table = Table(title=f"端点探活 @ {cfg.base}", show_lines=False)
    table.add_column("动作", style="cyan", no_wrap=True)
    table.add_column("候选端点")
    table.add_column("结果", overflow="fold")
    for action, ep, brief in rows:
        if brief.startswith("OK"):
            style = "green"
        elif brief.startswith(("MISSING", "SKIP")):
            style = "dim"
        else:
            style = "yellow"
        table.add_row(action, ep, f"[{style}]{brief}[/]")
    console.print(table)


@app.command()
def probe(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """只读探活：把候选端点全打一遍，确认哪些真实可用。"""
    _do_probe(verbose=verbose)


@app.command()
def status() -> None:
    """查看登录态、今日完成情况、历史运行记录。"""
    _banner()
    cfg = Config.load()
    state = load_state()
    hits = state.get("endpoint_hits", {})

    info = Table(title="环境", show_header=False, box=None)
    info.add_row("站点", cfg.base)
    info.add_row("Cookie", "已配置" if cfg.has_cookie else "[red]未配置[/]")
    info.add_row("限速", f"{cfg.limits.min_delay}~{cfg.limits.max_delay}s / 请求")
    info.add_row("日上限", f"赞 {cfg.limits.max_likes} · 关注 {cfg.limits.max_follows} "
                           f"· 浏览 {cfg.limits.max_browses}")
    console.print(info)

    if hits:
        t = Table(title="已命中的端点")
        t.add_column("动作"); t.add_column("端点")
        for k, v in hits.items():
            t.add_row(k, v)
        console.print(t)

    days = state.get("days", {})
    if days:
        today = sorted(days)[-1]
        d = days[today]
        t2 = Table(title=f"最近一次任务日 {today}")
        t2.add_column("项"); t2.add_column("数量")
        for k in ("liked", "followed", "browsed"):
            t2.add_row(k, str(len(d.get(k, []))))
        console.print(t2)

    runs = read_runs(limit=5)
    if runs:
        t3 = Table(title="最近运行")
        for col in ("时间", "模式", "结果"):
            t3.add_column(col)
        for r in reversed(runs):
            counts = {}
            for o in r.get("outcomes", []):
                counts[o["status"]] = counts.get(o["status"], 0) + 1
            mode = "dry-run" if r.get("dry_run") else "LIVE"
            t3.add_row(r.get("finished_at", "?"), mode,
                       " ".join(f"{k}:{v}" for k, v in counts.items()))
        console.print(t3)
    if not cfg.has_cookie:
        console.print("\n[yellow]提示：没有 Cookie，先执行 civitai-daily login[/]")


@app.command()
def run(
    live: bool = typer.Option(False, "--live", help="真正发出写请求（点赞/关注）。不加则只演练。"),
    only: str = typer.Option("", "--only", help="只跑指定任务，逗号分隔"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    json_out: bool = typer.Option(False, "--json", help="以 JSON 输出报告"),
) -> None:
    """执行全部每日任务。默认 dry-run，不会改动站点任何状态。"""
    cfg = Config.load()
    only_list = [s.strip() for s in only.split(",") if s.strip()] or None
    if only_list is None and not cfg.has_cookie:
        # 没 Cookie 时整轮只会得到一串 401。这里直接把话说清楚，
        # 否则会跑到一半才用「Cookie 可能已过期」去暗示 —— 那会把人
        # 往"重新导出 Cookie"上带，而实际问题是压根没配。
        console.print("[red]没有可用 Cookie。[/red]先执行 [bold]civitai-daily login[/bold]，"
                      "或把浏览器里的 Cookie 整串粘进：")
        console.print(f"  {COOKIE_PATH}")
        raise typer.Exit(code=1)

    report = run_all(cfg, dry_run=not live, only=only_list, verbose=verbose)

    if json_out:
        console.print_json(json.dumps(report.to_dict(), ensure_ascii=False))
        return

    _banner()
    mode = "[red]LIVE（真实写操作）[/]" if live else "[green]dry-run（不修改任何状态）[/]"
    console.print(f"模式: {mode}   站点: {cfg.base}   耗时: {report.duration_s:.1f}s\n")
    for line in report.summary_lines():
        console.print(line)
    if report.stopped_reason:
        console.print(f"\n[yellow]{report.stopped_reason}[/]")

    boxed = [o for o in report.outcomes if o.status in (UNSUPPORTED, FAILED)]
    if boxed:
        console.print("\n[bold yellow]以下项目在当前站点/账号上无法自动完成：[/]")
        for o in boxed:
            console.print(f"  · [yellow]{o.label}[/]：{o.detail}")


@app.command()
def undo(
    likes: bool = typer.Option(False, "--likes", help="撤销本工具点过的赞"),
    follows: bool = typer.Option(False, "--follows", help="撤销本工具关注过的人"),
    everything: bool = typer.Option(False, "--all", help="两者都做"),
    live: bool = typer.Option(False, "--live", help="真的执行撤销；默认只演练"),
    limit: int = typer.Option(0, "--limit", help="最多处理多少个（0 = 全部）"),
) -> None:
    """撤销本工具对账号造成的改动。

    只处理**自己记录过的**对象（state.json 里的 reacted_ids 与 days.followed），
    而且动手前会先向站点确认这些痕迹现在是否还在 —— 不会误伤你自己手动点过的赞、
    关注过的人。默认演练，加 --live 才真的撤销。
    """
    _banner()
    if not (likes or follows or everything):
        console.print("请指定 --likes / --follows / --all 之一。")
        raise typer.Exit(code=1)

    cfg = Config.load()
    if not cfg.has_cookie:
        console.print("[red]没有 Cookie，撤销需要登录态。[/red]")
        raise typer.Exit(code=1)

    from .undo import undo_follows, undo_likes

    reports = []
    if likes or everything:
        reports.append(undo_likes(cfg, live=live, limit=limit))
    if follows or everything:
        reports.append(undo_follows(cfg, live=live, limit=limit))

    if not live:
        console.print("[yellow]演练模式：没有真正撤销任何东西。确认无误后加 --live。[/yellow]\n")

    for r in reports:
        title = "点赞" if r.kind == "likes" else "关注"
        console.print(f"[bold]{title}[/bold] — {r.summary()}")
        for d in r.details:
            console.print(f"    {d}")


@app.command()
def serve(
    host: str = typer.Option(None, help="监听地址，默认取配置"),
    port: int = typer.Option(None, help="监听端口，默认取配置"),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """启动 Web 管理面板。"""
    import uvicorn
    cfg = Config.load()
    h = host or cfg.web.host
    p = port or cfg.web.port
    console.print(Panel.fit(
        f"Web 面板: [bold red]http://{h}:{p}[/]\n"
        f"站点: {cfg.base}   Cookie: {'已配置' if cfg.has_cookie else '未配置'}",
        border_style="red"))
    uvicorn.run("civitai_daily.web.app:app", host=h, port=p, reload=reload, log_level="info")


@app.command()
def version() -> None:
    """显示版本。"""
    console.print(f"绯狐C站日常任务 {__version__}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
