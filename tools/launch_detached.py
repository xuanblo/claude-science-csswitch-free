# -*- coding: utf-8 -*-
"""脱离当前会话启动 CSswitch（Windows）。

问题：直接从终端跑 `csx start`，父 shell 一结束，子进程就被连带清理
（bash 作业控制 / Windows 作业对象都会传播终止）。本脚本用
DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP 双重脱离，并把父进程立刻
退出，返回后进程仍应存活。

用法：
    python tools/launch_detached.py           # 启动代理 + 沙箱
    python tools/launch_detached.py --check   # 只查状态
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
FLAGS = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW


def check():
    sys.path.insert(0, str(ROOT))
    from csswitch import config as C
    from csswitch import sandbox as S
    st = C.load_state()
    port = st.get("proxy_port") or C.load_config()["settings"]["proxy_port"]
    ss = S.sandbox_status()
    print(f"  代理127.0.0.1:{port} : "
          f"{'运行中' if S._port_open(port) else '未运行'}")
    print(f"  沙箱 Science 端口 {ss['port']} : "
          f"{'运行中' if ss['running'] else '未运行'}"
          f"{'  (' + ss['detail'] + ')' if ss.get('detail') else ''}")
    return S._port_open(port) and ss["running"]


def main():
    if "--check" in sys.argv:
        sys.exit(0 if check() else 1)

    log_dir = Path.home() / ".csswitch-win" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logf = open(log_dir / "launch.log", "a", encoding="utf-8")
    # 继承完整环境，只剥离代理变量（否则出站会被父环境代理劫持）。
    # 早前用精简 env 会导致 find_science_binary 找不到二进制 —— 不要这样做。
    import os
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY",
              "http_proxy", "https_proxy", "all_proxy", "ftp_proxy"):
        env.pop(k, None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT)
    p = subprocess.Popen([PY, str(ROOT / "csx.py"), "start"],
                         cwd=str(ROOT), env=env, stdout=logf, stderr=logf,
                         creationflags=FLAGS, close_fds=True)
    logf.close()
    print(f"已脱离启动 pid={p.pid}，日志: {log_dir / 'launch.log'}")
    print("等待 30 秒后自动复查存活情况 ...")
    time.sleep(30)
    sys.exit(0 if check() else 1)


if __name__ == "__main__":
    main()