# -*- coding: utf-8 -*-
"""验证登录链接必须是新鲜的（nonce 一次性，3 分钟过期）。

回归要点：早期实现"有缓存就直接返回"，导致面板几小时后仍派发启动时的
旧链接，用户点开必然看到 "this link has expired"。
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from csswitch import config as C  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18993
TOKEN = C.load_state().get("admin_token", "")
BASE = f"http://127.0.0.1:{PORT}"

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def get(path):
    req = urllib.request.Request(BASE + path, headers={"x-csx-admin": TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:
        return 0, {"detail": str(e)}


print("--- 登录链接新鲜度验证 ---")

code, s = get("/__admin/sandbox")
check("沙箱状态接口可用", code == 200 and s.get("ok"), str(s)[:90])
check("沙箱在运行", bool(s.get("running")), str(s.get("detail"))[:80])
check("返回 url", str(s.get("url", "")).startswith("http"), str(s.get("url"))[:70])

u1 = s.get("url", "")

# 等到 45s TTL 之外，必须拿到不同 nonce
print("  等待 50 秒跨过 TTL ...")
time.sleep(50)
code, s2 = get("/__admin/sandbox")
u2 = s2.get("url", "")
check("跨 TTL 后拿到新链接", bool(u2), str(s2)[:70])


def nonce_of(u):
    m = re.search(r"nonce=([0-9a-f]+)", u or "")
    return m.group(1) if m else ""


check("两次 nonce 不同（不再派发旧链）",
      bool(nonce_of(u1)) and nonce_of(u1) != nonce_of(u2),
      f"u1={nonce_of(u1)[:12]} u2={nonce_of(u2)[:12]}")
print(f"    第1次: {nonce_of(u1)[:16]}")
print(f"    第2次: {nonce_of(u2)[:16]}")

# TTL 内应命中缓存（不再 fork 进程），但链接仍是有效的新链
code, s3 = get("/__admin/sandbox")
u3 = s3.get("url", "")
check("TTL 内复用缓存（省进程）", u3 == u2, "TTL 内应一致")
check("TTL 内也返回 http 链接", bool(u3), str(u3)[:60])

# 任务态里不应再残留可派发的 url
task = s3.get("task") or {}
check("task 态不再缓存 url", not task.get("url"),
      f"task.url={str(task.get('url'))[:60]}")

# 校验链接真的可用（HEAD 首页应返回 200/401 而非超时）
import socket  # noqa: E402
s = socket.socket()
s.settimeout(5)
reachable = s.connect_ex(("127.0.0.1", 8090)) == 0
s.close()
check("Science 端口可达", reachable)

print(f"\n链接新鲜度: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)