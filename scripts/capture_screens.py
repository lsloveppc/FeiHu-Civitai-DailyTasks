#!/usr/bin/env python3
"""生成 README 用的界面截图。

需要面板正在运行（python -m civitai_daily.cli serve），以及系统已装 Chrome。

    python scripts/capture_screens.py [面板地址]

用 Playwright 而不是命令行 headless 截图，是因为这里要真实点击按钮 ——
「执行中」和「端点探活」两个状态只有交互之后才会出现。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "screenshots"
URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787/"
VIEWPORT = {"width": 1920, "height": 1200}


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("先装 playwright：pip install playwright")
        return 1

    OUT.mkdir(exist_ok=True)

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            browser = p.chromium.launch(headless=True)

        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
        # 「一键执行」会弹确认框，自动接受
        page.on("dialog", lambda d: d.accept())

        page.goto(URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2500)

        # 1) 主界面全貌
        page.screenshot(path=str(OUT / "01-overview.png"), full_page=True)
        print("  01-overview.png")

        # 2) 执行中：进度条与实时计数只在运行期间出现
        page.click("#btnRun")
        page.wait_for_timeout(6000)
        page.screenshot(path=str(OUT / "02-running.png"), full_page=False)
        print("  02-running.png")

        # 等这一轮跑完，再截探活（探活本身要打十几次请求，慢）
        page.wait_for_timeout(45000)
        page.click("#btnProbe")
        page.wait_for_timeout(90000)
        page.screenshot(path=str(OUT / "03-probe.png"), full_page=True)
        print("  03-probe.png")

        browser.close()
    print(f"\n输出目录: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
