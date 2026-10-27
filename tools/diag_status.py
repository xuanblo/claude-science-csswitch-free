# -*- coding: utf-8 -*-
"""诊断脚本：直接询问 claude-science status 的真实返回，定位判定失效原因。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from csswitch import config as C  # noqa: E402
from csswitch import sandbox as S  # noqa: E402

bin_path = S.find_science_binary()
data_dir = C.sandbox_data_dir()
print("bin_path :", bin_path)
print("data_dir :", data_dir, "exists=", data_dir.exists())
env = S.sandbox_env(C.load_config()["settings"]["proxy_port"], "x")
out = S._run(bin_path, ["status", "--data-dir", str(data_dir)], env, timeout=25)
print("returncode:", out.returncode)
print("stdout    :", repr((out.stdout or "")[:500]))
print("stderr    :", repr((out.stderr or "")[:500]))
print("_port_open(8090):", S._port_open(8090))
print("_our_sandbox_running:", S._our_sandbox_running(bin_path, 8090))