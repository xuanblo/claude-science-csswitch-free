# -*- coding: utf-8 -*-
"""端到端验证宿主文件夹授权的管理 API。

在本进程内用线程起代理（不受父 shell 作业清理影响），测完自动回收。
需要 Claude Science daemon 已在运行，否则 daemon侧调用会报
"无法获取登录 nonce"。

    python tools/check_grants_api.py
"""
import json
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 只打本机，别继承系统 https_proxy
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({})))

from csswitch import config as C  # noqa: E402
from csswitch import sandbox as S  # noqa: E402

TEST_DIR = str(ROOT)
fails: list[str] = []


def start_proxy():
    """在本进程内用后台线程起代理，返回 (server 或 None, 说明)。

    不 spawn 子进程 —— bash 作业结束会连带清理子进程，导致刚启动就
    10061。线程随脚本生命周期，测完自动回收。
    """
    port = C.load_config()["settings"]["proxy_port"]
    # 用 socket 探活而不是 /health —— /health 走 path-secret 鉴权，
    # 没带 secret 必然 401，不能用来判断"代理是否在跑"
    if S._port_open(port):
        return None, f"复用已在运行的代理（端口 {port}）"

    from csswitch import proxy as proxy_mod
    from csswitch import server as srv_mod
    from csswitch import state as state_mod
    cur = C.load_config().get("current")
    if not cur or not proxy_mod.load_provider(cur)[0]:
        return None, "加载 provider 失败，先跑 csx add 配置一个"
    # 生产路径在 proxy.main() 里生成 secret/token，进程内起服务要自己补，
    # 否则 state.json 里的 token 为空，鉴权用例会误判
    state_mod.AUTH_SECRET = secrets.token_urlsafe(16)
    state_mod.ADMIN_TOKEN = secrets.token_urlsafe(24)
    try:
        httpd = srv_mod.serve(port)
    except RuntimeError as e:
        return None, f"端口 {port} 起不来：{e}"
    proxy_mod._write_state(port)
    threading.Thread(target=httpd.serve_forever, daemon=True,
                     name="test-proxy").start()
    for _ in range(20):
        time.sleep(0.25)
        if S._port_open(port):
            return httpd, f"已在本进程起代理（端口 {port}）"
    return None, f"代理起不来（端口 {port}）"


def check(label, cond, extra=""):
    print(f"  {'✓' if cond else '✗'} {label}{('  ' + str(extra)) if extra else ''}")
    if not cond:
        fails.append(label)


def call(method: str, path: str, payload=None, token: str | None = None):
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {}
    if token is not None:
        headers["x-csx-admin"] = token
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=body, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw or "{}")
        except ValueError:
            return e.code, {"detail": raw[:200]}


httpd, note = start_proxy()
print(f"代理: {note}")
if httpd is None and "复用" not in note:
    sys.exit(note)

state = C.load_state()
port = state.get("proxy_port") or C.load_config()["settings"]["proxy_port"]
BASE = f"http://127.0.0.1:{port}"
# 本进程起代理时 _write_state 会刷新 token，所以必须在起完之后读
token = C.load_state().get("admin_token") or ""
if not token:
    sys.exit("没有 admin_token，请先 csx start")

ss = S.sandbox_status()
if not ss.get("running"):
    print(f"⚠ Claude Science 未运行（{ss.get('detail') or '端口未监听'}），"
          "正在拉起（首次可能要 30~90 秒）...")
    cfg = C.load_config()
    r = S.start_sandbox(
        proxy_port=cfg["settings"]["proxy_port"],
        auth_secret=C.load_state().get("auth_secret") or "",
        port=cfg["settings"]["sandbox_port"], open_browser=False)
    ss = S.sandbox_status()
    if not ss.get("running"):
        print(f"✗拉起失败：{r.get('detail')}")
        print("  仍会跑本地校验用例（不依赖 daemon 的部分）")
    else:
        print(f"  ✓ 已就绪（端口 {ss['port']}）")

print("\n=== 1. 读取授权列表 ===")
code, d = call("GET", "/__admin/host-grants", token=token)
check("GET host-grants 200", code == 200, f"code={code}")
check("返回结构含 grants/home/sandboxActive",
      all(k in d for k in ("grants", "home", "sandboxActive")), list(d))
if d.get("detail"):
    print(f"  ⚠ daemon 未响应: {d['detail']}")
before = {g["hostPath"] for g in d.get("grants", [])}
SBX = bool(d.get("sandboxActive"))
print(f"  · 安全沙箱{'已启用（授权会真实落库）' if SBX else '已关闭（授权是空操作）'}")

# 沙箱关闭时 daemon 明确回 alreadyAllowed 且不落库，这是它的设计而非 bug
# （"The filesystem sandbox is not active, so this path is already accessible
#   to compute — no grant was needed or recorded"）
EXPECT_RECORDED = SBX

print("\n=== 2. 授权一个文件夹（ro） ===")
code, d = call("POST", "/__admin/host-grant/add",
               {"path": TEST_DIR, "mode": "ro"}, token)
check("POST add 200", code == 200, d.get("detail") or d)
check("recorded 字段如实反映是否落库",
      "recorded" in d, d.get("recorded"))
check(f"recorded={EXPECT_RECORDED}（与沙箱状态一致）",
      d.get("recorded") is EXPECT_RECORDED,
      f"实际={d.get('recorded')} {d.get('detail') or ''}")

print("\n=== 3. 列表里能查到 ===")
code, d = call("GET", "/__admin/host-grants", token=token)
paths = {g["hostPath"] for g in d.get("grants", [])}
if EXPECT_RECORDED:
    check("已授权目录出现在列表", TEST_DIR in paths, sorted(paths))
else:
    check("沙箱关闭时列表保持为空（符合 daemon 语义）",
          TEST_DIR not in paths, sorted(paths))

print("\n=== 4. 浏览目录（沙箱开时只允许授权根与 $HOME） ===")
code, d = call("GET", "/__admin/host-browse?path=" + TEST_DIR.replace("\\", "%5C"),
               token=token)
if EXPECT_RECORDED:
    check("browse 已授权目录 200", code == 200,
          d.get("detail") or f"{len(d.get('entries', []))} 项")
    check("能看到 csswitch 子目录",
          any(e["name"] == "csswitch" for e in d.get("entries", [])))
else:
    check("沙箱关闭时 browse 拒绝未登记路径", code == 400, d.get("detail"))
code, d = call("GET", "/__admin/host-browse?path=D%3A%5C", token=token)
check("浏览未授权的盘根被拒", code == 400, d.get("detail"))

print("\n=== 5. 权限降级被拒（rw -> ro 会让 live bind 自相矛盾） ===")
if EXPECT_RECORDED and TEST_DIR in before:
    cur = [g for g in call("GET", "/__admin/host-grants", token=token)[1]["grants"]
           if g["hostPath"] == TEST_DIR]
    if cur and cur[0]["mode"] == "rw":
        code, d = call("POST", "/__admin/host-grant/add",
                       {"path": TEST_DIR, "mode": "ro"}, token)
        check("rw 降级为 ro -> 400 + 可操作提示",
              code == 400 and "撤销" in str(d.get("detail", "")),
              f"code={code} {str(d.get('detail'))[:80]}")
    else:
        print("  · 该目录当前是 ro，跳过降级用例")
else:
    print("  · 无既有 rw 授权，跳过降级用例")

print("\n=== 6. 错误输入被拒（本地校验，不依赖 daemon） ===")
for bad, why in ((r"D:\Paper\绝对不存在XYZ", "不存在路径"),
                 ("相对路径", "相对路径"),
                 (str(ROOT / "csx.py"), "文件而非目录"),
                 ("", "空路径")):
    code, d = call("POST", "/__admin/host-grant/add",
                   {"path": bad, "mode": "rw"}, token)
    check(f"{why} -> 400", code == 400, d.get("detail"))
code, d = call("POST", "/__admin/host-grant/add",
               {"path": TEST_DIR, "mode": "乱写"}, token)
check("非法 mode -> 400", code == 400, d.get("detail"))

print("\n=== 7. 撤销 ===")
code, d = call("POST", "/__admin/host-grant/remove", {"path": TEST_DIR}, token)
check("remove 200", code == 200, d.get("detail") or d)
code, d = call("GET", "/__admin/host-grants", token=token)
paths = {g["hostPath"] for g in d.get("grants", [])}
check("列表中已移除", TEST_DIR not in paths, sorted(paths))
# TEST_DIR 本身可能就在 before 里（上次运行留下的），要排除掉再比
others = before - {TEST_DIR}
check("其它原有授权未被动到", others <= paths,
      f"原有={sorted(others)} 现在={sorted(paths)}")

print("\n=== 8. 未鉴权被拒 ===")
try:
    urllib.request.urlopen(BASE + "/__admin/host-grants", timeout=10)
    check("无 token 应被拒", False, "竟然成功了")
except urllib.error.HTTPError as e:
    check("无 token -> 403", e.code == 403, f"code={e.code}")

print("\n=== 9. DELETE 动词不再 501 ===")
code, d = call("DELETE", "/__admin/host-grants", token=token)
check("DELETE 不再 501", code != 501, f"code={code}")

print("\n=== 10. 沙箱关闭时的语义（不依赖 daemon，纯配置判定） ===")
from csswitch import hostgrant as HG  # noqa: E402
saved = C.load_config()["settings"].get("sandbox_enabled", True)
try:
    for want in (True, False):
        cfg2 = C.load_config()
        cfg2["settings"]["sandbox_enabled"] = want
        C.save_config(cfg2)
        check(f"sandbox_enabled={want} -> sandbox_active()={want}",
              HG.sandbox_active() is want, HG.sandbox_active())
finally:
    cfg3 = C.load_config()
    cfg3["settings"]["sandbox_enabled"] = saved
    C.save_config(cfg3)
check("配置已还原", C.load_config()["settings"]["sandbox_enabled"] is saved, saved)

if httpd is not None:
    httpd.shutdown()
    httpd.server_close()

print("\n" + "=" * 46)
if fails:
    print(f"✗ {len(fails)} 项失败: {fails}")
    sys.exit(1)
print("✓ 全部通过")

