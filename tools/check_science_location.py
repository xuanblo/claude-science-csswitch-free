# -*- coding: utf-8 -*-
"""诊断 Claude Science 的环境到底装在哪、为什么。

**新电脑上装Science 时环境默认落C 盘的根因**：`claude-science` 自己的
帮助里写得很清楚：

    --data-dir DIR     Where state is stored (default ~/.claude-science)

Windows 上 ``~`` = ``%USERPROFILE%``，而 Node/Python 的 ``os.homedir()``
都读它。所以只要把 ``USERPROFILE``（和 ``HOME`` 双保险）指向D 盘，
Science 派生出的**全部**路径 —— 包括那个 10 GB 的 conda 环境 ——
都会跟着落到 D 盘。

本脚本用来在一台新电脑上确认：

1. 官方 ``%USERPROFILE%\\.claude-science`` 占多大（该不该清理）
2. ``USERPROFILE`` / ``HOME`` 现在指向哪里
3. CSswitch-Win 沙箱那份在哪个目录、占多大

    python tools/check_science_location.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GB = 1024 ** 3


def dir_size(p: Path, budget: int = 20000) -> tuple[int, int, bool]:
    """返回 (文件数, 总字节, 是否抽样)。

    conda 环境动辄十几万文件，全量遍历要几十秒。这里设预算：超过就按
    已扫到的部分估算并注明，避免诊断看起来像卡死。
    """
    n = size = 0
    for root, _dirs, files in os.walk(p, onerror=lambda e: None):
        for f in files:
            fp = Path(root) / f
            try:
                size += fp.stat().st_size
                n += 1
            except OSError:
                pass
        if n > budget:
            return n, size, True
    return n, size, False


def show(p: Path, label: str, limit: int = 3) -> None:
    if not p.is_dir():
        print(f"  ✗ {label}: {p} （不存在）")
        return
    n, size, partial = dir_size(p)
    note = "（已抽样，数值偏小）" if partial else ""
    print(f"  ✓ {label}: {p}")
    print(f"      {n:,} 个文件 / {size / GB:.2f} GB{note}")
    # 只列最大的几个子目录，帮你判断值不值得删
    subs = []
    for d in p.iterdir():
        if d.is_dir():
            _, s, _ = dir_size(d)
            subs.append((s, d.name))
    subs.sort(reverse=True)
    for s, name in subs[:limit]:
        print(f"        {name:<24} {s / GB:>7.2f} GB")


def main() -> int:
    print("=" * 62)
    print("Claude Science 环境位置诊断")
    print("=" * 62)

    up = os.environ.get("USERPROFILE", "")
    home = os.environ.get("HOME", "")
    print("\n[1] 当前环境变量")
    print(f"  USERPROFILE = {up or '(未设置)'}")
    print(f"  HOME        = {home or '(未设置)'}")

    # Python 在 import 时就缓存了 home，改了环境变量要新进程才反映
    from csswitch import config as C
    print(f"  os.homedir() = {Path.home()}   <-- Science 按它推导安装位置")

    drive = (up[:1] or "C").upper()
    if drive.upper() == "C":
        print(f"\n  ⚠ 当前 USERPROFILE 仍在 C 盘 —— 新装 Science 会把"
              f"\n    约 10 GB 的 conda 环境落到 C 盘。")
    else:
        print(f"\n  ✓ USERPROFILE 指向 {drive} 盘，新装的环境会落在那里。")

    print("\n[2] 官方目录（不经CSswitch-Win，直接跑 Science 时用）")
    show(Path.home() / ".claude-science", "官方 data-dir")

    print("\n[3] CSswitch-Win 沙箱目录（本工具管理的那一份）")
    show(C.sandbox_auth_dir().parent, "沙箱 auth_dir 根", limit=4)

    print("\n[4] 找到的 claude-science 可执行文件")
    exe = shutil.which("claude-science")
    if exe:
        print(f"  PATH: {exe}")
    from csswitch import sandbox as S
    b = S.find_science_binary(C.load_config())
    print(f"  实际使用: {b or '（未找到）'}")

    print("\n" + "=" * 62)
    print("新电脑上怎么让它装到 D 盘")
    print("-" * 62)
    print("""
根因：claude-science 的 --data-dir 默认是 ~/.claude-science，
     Windows 上 ~ = %USERPROFILE%。所以要改的是 USERPROFILE。

做法（不影响其他软件 —— 不要改系统级的 USERPROFILE）：
  在启动 Science 的那个快捷方式 / 脚本里，加两行环境变量：

      set "USERPROFILE=D:\\ClaudeScience-Data"
      set "HOME=D:\\ClaudeScience-Data"

  然后再首次启动 claude-science 安装命令，它就会把conda 环境
  （约 10 GB）装到 D 盘。

CSswitch-Win 自带的 便携启动.bat 已内置沙箱隔离（数据都在 D:\\CSSwitch-Win-Data），
所以用本工具启动的话无需额外操作。
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())