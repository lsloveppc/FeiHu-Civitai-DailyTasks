#!/usr/bin/env python3
"""打包发布 zip。

用法:
    python scripts/build_release.py [输出目录]

默认输出到桌面。会自动排除 data/（里面有 Cookie 与个人操作记录）、__pycache__、
本地验证产物，以及体积大但代码并不引用的原始素材。
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = "civitai_daily"

EXCLUDE_DIRS = {"__pycache__", "data", "_verify", ".git", ".venv", "venv",
                "build", "dist", ".pytest_cache"}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".broken"}
# logo-full.png 是原始素材，代码只用缩放后的 logo.png / favicon.png
EXCLUDE_FILES = {"logo-full.png"}
TOP_FILES = ["README.md", "使用说明.md", "LICENSE", ".gitignore",
             "config.example.yaml", "requirements.txt", "pyproject.toml"]


def version() -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"',
                  (ROOT / PKG / "__init__.py").read_text(encoding="utf-8"))
    if not m:
        raise SystemExit("读不到 __version__")
    return m.group(1)


def default_out_dir() -> Path:
    for cand in (Path.home() / "Desktop",
                 Path.home() / "OneDrive" / "Desktop"):
        if cand.is_dir():
            return cand
    return Path.home()


def build(out_dir: Path) -> Path:
    root = f"绯狐C站日常任务-V{version()}"
    out = out_dir / f"{root}.zip"
    if out.exists():
        out.unlink()

    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for sub in (PKG, "scripts", "screenshots"):
            for p in sorted((ROOT / sub).rglob("*")):
                if p.is_dir() or p.name in EXCLUDE_FILES:
                    continue
                if any(part in EXCLUDE_DIRS for part in p.parts):
                    continue
                if p.suffix in EXCLUDE_SUFFIX:
                    continue
                z.write(p, f"{root}/{p.relative_to(ROOT).as_posix()}")
                count += 1
        for name in TOP_FILES:
            f = ROOT / name
            if f.exists():
                z.write(f, f"{root}/{name}")
                count += 1
    print(f"打包完成: {out}")
    print(f"文件数 {count}  体积 {out.stat().st_size / 1024:.1f}KB")
    return out


def audit(zip_path: Path) -> bool:
    """确认包里没有混进凭据或临时产物。"""
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
    problems = []
    for n in names:
        low = n.lower()
        if "data/" in low or "cookie" in low:
            problems.append(f"疑似凭据: {n}")
        if "__pycache__" in low or n.endswith((".pyc", ".broken")):
            problems.append(f"临时产物: {n}")
    if problems:
        print("\n[!] 包内容检查未通过:")
        for p in problems:
            print("   ", p)
        return False
    print("\n内容检查: 未发现凭据 / 临时产物  ✓")
    return True


if __name__ == "__main__":
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else default_out_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    ok = audit(build(out_dir))
    raise SystemExit(0 if ok else 1)
