# -*- coding: utf-8 -*-
"""验证 Web 面板的沙箱一键启停（模拟前端调用）。

流程：查状态 -> 停止 -> 等停止 -> 一键启动 -> 等就绪 -> 拿登录 URL。
注意：会真的停掉并重启沙箱 Science。
"""
import json
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


def call(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"x-csx-admin": TOKEN}
    if data:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=hdrs,
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"detail": str(e)}


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def wait_idle(timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        _, s = call("/__admin/sandbox")
        st = (s.get("task") or {}).get("state")
        if st in ("idle", "ready", "failed") and not s.get("running"):
            return s
        if st == "failed":
            return s
        time.sleep(2)
    return {}


print("--- 面板沙箱一键启停验证 ---")

# 1) 状态接口可用
code, s = call("/__admin/sandbox")
check("沙箱状态接口可用", code == 200 and s.get("ok"), str(s)[:100])
check("返回 task/url/running 三个关键字段",
      all(k in s for k in ("task", "url", "running")), str(sorted(s))[:120])
print(f"       当前: running={s.get('running')} state={(s.get('task') or {}).get('state')}")

# 2) 鉴权
code, r = call("/__admin/sandbox", token="") if False else (0, {})
req = urllib.request.Request(BASE + "/__admin/sandbox")
try:
    urllib.request.urlopen(req, timeout=10)
    rejected = False
except urllib.error.HTTPError as e:
    rejected = e.code == 403
except Exception:
    rejected = True
check("无令牌访问沙箱状态被拒", rejected)

# 3) 重复启动应被拒（已在运行时）
if s.get("running"):
    code, r = call("/__admin/sandbox/start", {"port": 8090,
                                              "open_browser": False})
    check("已运行时拒绝重复启动", code == 409 or r.get("ok") is False,
          f"code={code} {str(r)[:90]}")

    # 4) 停止
    print("       正在停止沙箱 ...")
    code, r = call("/__admin/sandbox/stop", {})
    check("停止请求被接受", code == 200 and r.get("ok"), str(r)[:90])
    time.sleep(6)
    _, s2 = call("/__admin/sandbox")
    check("停止后端口已释放", not s2.get("running"),
          f"still running, state={(s2.get('task') or {}).get('state')}")

    # 5) 一键启动
    print("       正在一键启动（不自动开浏览器）...")
    code, r = call("/__admin/sandbox/start", {"port": 8090,
                                              "open_browser": False})
    check("启动请求被接受", code == 200 and r.get("ok"), str(r)[:90])

    # 6) 轮询等就绪（首次可能要 90 秒）
    t0 = time.time()
    ready = None
    while time.time() - t0 < 150:
        _, s3 = call("/__admin/sandbox")
        t = s3.get("task") or {}
        print(f"       [{int(time.time()-t0):>3}s] "
              f"state={t.get('state'):<8} running={s3.get('running')}")
        if t.get("state") == "ready" and s3.get("running"):
            ready = s3
            break
        if t.get("state") == "failed":
            print(f"       失败详情: {t.get('detail')}")
            break
        time.sleep(3)
    check("沙箱一键启动成功并就绪", ready is not None,
          f"timeout/failed after {int(time.time()-t0)}s")
    if ready:
        check("返回可用的登录 URL",
              str(ready.get("url", "")).startswith("http"),
              str(ready.get("url"))[:90])
        print(f"       URL: {ready.get('url')}")
        print(f"       端口 {ready.get('port')}  pid={(ready.get('task') or {}).get('pid')}")

print(f"\n沙箱一键启停: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)