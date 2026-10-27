#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CSswitch 跨平台入口。

用法：
  python csx.py doctor          # 环境诊断
  python csx.py panel           # 打开 Web 控制台（图形化配置 + 一键启停）
  python csx.py add             # 新建第三方配置
  python csx.py list            # 列出配置
  python csx.py use <name>      # 切换当前配置
  python csx.py start           # 一键开始
  python csx.py stop            # 停止
  python csx.py official        # 官方 Claude 模式
  python csx.py status          # 状态总览

Windows 双击 便携启动.bat；macOS / Linux 运行 ./启动.sh。
Unix 首次运行会在 .venv 中自动补装依赖。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from csswitch.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
