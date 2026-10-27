# -*- coding: utf-8 -*-
"""端到端验证「打开 Science 自动跳过 Sign in 页」。

复现的正是用户报的"点击打开没有跳过认证"：Science 的nonce 链接落地页只是
一个需要点击的表单（``id="signin-form"``），``window.open`` 只会停在
"Sign in"。本脚本验证中转页 ``/__admin/science-open`` 确实能拿到
``operon_auth`` cookie，并用它直接访问到真正的 Science 首页。

    python tools/check_auto_login.py
"""
import http.cookiejar
import json
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 只打本机，别继承系统 https_proxy（继承会绕出去报 10061）
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({})))

from csswitch import config as C  # noqa: E402
from csswitch import sandbox as S  # noqa: E402

fails: list[str] = []


def check(label, cond, extra=""):
    print(f"  {'✓' if cond else '✗'} {label}{('  ' + str(extra)) if extra else ''}")
    if not cond:
        fails.append(label)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_proxy():
    """在**独立端口**起一个加载当前代码的代理。

    故意不复用用户正在跑的那个实例：它加载的是改动前的代码，路由里没有
    science-open，测出来的 403 会误判成"实现有问题"。也**不**写 state.json，
    以免把用户实例的 proxy_port/admin_token 顶掉。
    """
    from csswitch import proxy as proxy_mod
    from csswitch import server as srv_mod
    from csswitch import state as state_mod

    cur = C.load_config().get("current")
    if not cur or not proxy_mod.load_provider(cur)[0]:
        sys.exit("加载 provider 失败，先跑 csx add 配置一个")
    # token 沿用 state 里的，这样测试脚本自己知道该带什么
    state_mod.AUTH_SECRET = secrets.token_urlsafe(16)
    state_mod.ADMIN_TOKEN = secrets.token_urlsafe(24)
    port = _free_port()
    httpd = srv_mod.serve(port)
    threading.Thread(target=httpd.serve_forever, daemon=True,
                     name="test-proxy").start()
    for _ in range(20):
        time.sleep(0.25)
        if S._port_open(port):
            return httpd, port
    return None, port


def get(url, headers=None):
    """不跟随重定向，拿到原始响应（含 Location / Set-Cookie）。"""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None

    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect)
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with op.open(req, timeout=60) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


httpd, port = start_proxy()
from csswitch import state as state_mod  # noqa: E402
token = state_mod.ADMIN_TOKEN
BASE = f"http://127.0.0.1:{port}"
print(f"代理: 已在测试端口 {port} 加载当前代码（不影响你正在运行的实例）")

ss = S.sandbox_status()
if not ss.get("running"):
    sys.exit(f"✗ Claude Science 未运行：{ss.get('detail') or '端口未监听'}")

# ---- 1. 基线：直接开nonce 链接会停在 Sign in -------------------------
print("\n[1] 基线复现：nonce 链接落地页需要点击")
url = S.current_url(max_age_s=0.0) if hasattr(S, "current_url") else None
from csswitch import lifecycle as L  # noqa: E402
nonce_url = L.current_url(max_age_s=0.0)
check("能取到 nonce 登录链接", bool(nonce_url), nonce_url[:60])
st, hdr, body = get(nonce_url.replace("localhost", "127.0.0.1", 1))
html = body.decode("utf-8", "replace")
check("nonce 链接 200", st == 200, st)
check("落地页确实是 Sign in 表单", 'id="signin-form"' in html,
      "光打开链接不会自动登录 → 这就是用户看到的现象")

# ---- 2. 中转页鉴权 --------------------------------------------------
print("\n[2] 中转页鉴权")
st, _, _ = get(f"{BASE}/__admin/science-open")
check("无 token 访问被拒(403)", st == 403, st)
st, _, _ = get(f"{BASE}/__admin/science-open?token=wrong")
check("错误 token 被拒(403)", st == 403, st)

# ---- 3. 从 localhost 访问中转页（cookie 域必须匹配）------------------
print("\n[3] 中转页签发 cookie")
st, hdr, _ = get(f"http://localhost:{port}/__admin/science-open?token={token}")
check("中转页返回 302 跳转", st == 302, st)
loc = hdr.get("Location", "")
dport = urllib.parse.urlparse(loc).port or 8090
check("跳到 daemon 首页", loc == f"http://localhost:{dport}/", loc)
cookies = hdr.get_all("Set-Cookie") or []
names = [c.split("=", 1)[0] for c in cookies]
check("下发 operon_auth", "operon_auth" in names, names)
check("下发 operon_csrf", "operon_csrf" in names, names)
csrf_cookie = next((c for c in cookies if c.startswith("operon_csrf")), "")
auth_cookie = next((c for c in cookies if c.startswith("operon_auth")), "")
check("operon_csrf 未被 HttpOnly 挡住(前端要读它填 x-operon-csrf)",
      "httponly" not in csrf_cookie.lower(), csrf_cookie[:70])
check("operon_auth 带 HttpOnly",
      "httponly" in auth_cookie.lower(), auth_cookie[:70])

# ---- 4. 用注入的 cookie 直接访问首页（这才是"跳过认证"的判据）--------
print("\n[4] 用中转页下发的 cookie 访问首页")
# 手动拼 Cookie 头：CookieJar.set_cookie 的 domain/path 参数写对很啰嗦，
# 而这里要验的恰恰是"浏览器会把这两条 cookie 发给 daemon"
cookie_header = "; ".join(c.split(";")[0] for c in cookies)
st, _, body = get(f"http://localhost:{dport}/", headers={"Cookie": cookie_header})
home = body.decode("utf-8", "replace")
check("首页 200（不再是 401）", st == 200, st)
check("不是 Sign in 页", 'id="signin-form"' not in home.lower())
check("是真正的 Science 页面", "<title>Claude Science</title>" in home)

# ---- 5. 裸访问仍应401（确认没有把安全边界改松）-----------------------
print("\n[5] 未登录时仍是 401（安全边界未放松）")
st, _, _ = get(f"http://localhost:{dport}/")
check("裸访问首页 401", st == 401, st)

# ---- 6. 面板确实指向中转页且不再用失效的 autologin 锚点--------------
print("\n[6] 面板前端")
panel = (ROOT / "csswitch" / "web" / "panel.html").read_text(
    encoding="utf-8")
check("openScience 走中转页", "/__admin/science-open?token=" in panel)
check("已移除失效的 #autologin 锚点", "#autologin" not in panel)
check("openScience 不再直接 window.open nonce 链接",
      "d.url + '#autologin'" not in panel)
check("裸 fetch 只有一处（api 内部）",
      panel.count("await fetch(") == 1,
      f"count={panel.count('await fetch(')}")

print("\n" + "=" * 52)
if fails:
    print(f"✗ {len(fails)} 项失败:")
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("✓ 全部通过：点「打开 Science」不再需要点 Sign in")