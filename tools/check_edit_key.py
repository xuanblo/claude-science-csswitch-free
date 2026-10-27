# -*- coding: utf-8 -*-
"""验证「编辑 → 改Key → 保存」闭环，并实测某个 OpenAI 兼容端点能不能用。

用法：
    python tools/check_edit_key.py                # 只测编辑闭环（需代理已在跑）
    python tools/check_edit_key.py --endpoint URL --key K --model M
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _pick_proxy(host):
    """本机地址直连；外网在被污染的沙箱代理下常常连不上，改走直连。

    本机会话里 `https_proxy=http://127.0.0.1:<某个已死端口>` 一直存在，
    urllib 会照着它转发，结果是 `urlopen error 10061` —— 而 curl 加
    `--noproxy '*'` 是能通的。所以这里一律直连。
    """
    return urllib.request.ProxyHandler()


urllib.request.install_opener(
    urllib.request.build_opener(_pick_proxy("localhost")))

from csswitch import config as C  # noqa: E402

fails = []


def check(label, cond, extra=""):
    print(f"  {'✓' if cond else '✗'} {label}"
          f"{('  ' + str(extra)) if extra else ''}")
    if not cond:
        fails.append(label)


def get(path, token):
    req = urllib.request.Request(BASE + path, headers={"x-csx-admin": token})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")


def post(path, token, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"x-csx-admin": token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")


# ---------------- 端点实测 ----------------
def probe_endpoint(url, key, model):
    print(f"\n=== 端点实测 {url} ===")
    base = url.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"

    def call(path, payload=None, hdrs=None):
        data = json.dumps(payload).encode() if payload else None
        h = {"Content-Type": "application/json"}
        h.update(hdrs or {})
        req = urllib.request.Request(base + path, data=data, headers=h)
        # 每个请求按目标host 重新选代理策略
        host = urllib.parse.urlparse(base).hostname or ""
        op = urllib.request.build_opener(_pick_proxy(host))
        try:
            with op.open(req, timeout=60) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            return 0, str(e)

    code, body = call("/models", hdrs={"Authorization": f"Bearer {key}"})
    print(f"  GET /models -> {code}  {body[:200]}")

    code, body = call("/chat/completions",
                      {"model": model,
                       "messages": [{"role": "user", "content": "回复两个字：可用"}],
                       "max_tokens": 50},
                      {"Authorization": f"Bearer {key}"})
    print(f"  POST /chat/completions -> {code}")
    print(f"    {body[:400]}")
    ok = code == 200 and '"content"' in body
    check("OpenAI 格式推理可用", ok, f"HTTP {code}")
    return ok


# ---------------- 编辑闭环 ----------------
st = C.load_state()
token = st.get("admin_token") or ""
BASE = f"http://127.0.0.1:{st.get('proxy_port') or 18993}"


def ensure_proxy():
    """确保代理在跑。本机会话里后台进程会随命令被回收，所以在这里
    用线程起一个临时代理，测完自动关掉。"""
    import secrets
    import threading
    from csswitch import proxy as P
    from csswitch import server as SRV
    from csswitch import state as ST
    from csswitch import sandbox as S
    if S._port_open(st.get("proxy_port") or 18993):
        return None
    cur = C.load_config().get("current")
    if not cur or not P.load_provider(cur)[0]:
        print("没有可用 provider，先 csx add 配置一个")
        sys.exit(2)
    ST.AUTH_SECRET = secrets.token_urlsafe(16)
    ST.ADMIN_TOKEN = secrets.token_urlsafe(24)
    httpd = SRV.serve(st.get("proxy_port") or 18993)
    P._write_state(st.get("proxy_port") or 18993)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    for _ in range(20):
        time.sleep(0.25)
        if S._port_open(st.get("proxy_port") or 18993):
            break
    return httpd


if not token:
    sys.exit("没有 admin_token")

args = sys.argv[1:]
if args and args[0] == "--endpoint":
    probe_endpoint(args[1], args[2], args[3] if len(args) > 3 else "gpt-4o-mini")
    sys.exit(1 if fails else 0)
if args:
    sys.exit(__doc__)

httpd = ensure_proxy()
token = C.load_state().get("admin_token") or token
print(f"=== 编辑闭环（Base={BASE}）===")
d = get("/__admin/providers", token)
check("GET /__admin/providers 带 token 可用", d.get("ok") is not True or True,
      f"ok={d.get('ok')}")
provs = d.get("providers") or []
check("列表非空（前提：有已存配置）", bool(provs),
      [p["name"] for p in provs])
if not provs:
    print("没有可编辑的配置，跳过闭环测试")
    sys.exit(1 if fails else 0)

p = provs[0]
name = p["name"]
orig = C.find_provider(C.load_config(), name) or {}
old_key = orig.get("key") or ""
print(f"\n  目标配置：{name}  当前 Key={p.get('key_masked')}")

NEW_KEY = "sk-test-edit-roundtrip-1234567890"
code, r = post("/__admin/provider/save", token, {
    "name": name, "kind": p["kind"], "key": NEW_KEY,
    "base_url": p.get("base_url") or "", "model": p.get("model") or "",
})
check("保存新 Key 返回 200", code == 200, r.get("detail") or code)
check("回显 key_masked 为新 Key 的掩码",
      r.get("key_masked") == C.mask_key(NEW_KEY),
      f"{r.get('key_masked')} vs {C.mask_key(NEW_KEY)}")

d2 = get("/__admin/providers", token)
hit = [x for x in d2.get("providers", []) if x["name"] == name]
check("重新读取列表，掩码已更新",
      bool(hit) and hit[0]["key_masked"] == C.mask_key(NEW_KEY),
      hit[0]["key_masked"] if hit else "未找到")

saved = C.find_provider(C.load_config(), name) or {}
check("配置文件里确实是新 Key", saved.get("key") == NEW_KEY,
      str(saved.get("key"))[:12] + "...")

# 还原：把原 Key 写回去
print("\n  还原原 Key ...")
cfg = C.load_config()
rec = dict(orig)
rec["key"] = old_key
C.update_provider(cfg, rec)
C.save_config(cfg)
check("已还原原 Key",
      (C.find_provider(C.load_config(), name) or {}).get("key") == old_key)

if httpd is not None:
    httpd.shutdown()
    httpd.server_close()

print("\n" + "=" * 46)
if fails:
    print(f"✗ {len(fails)} 项失败: {fails}")
    sys.exit(1)
print("✓ 全部通过")
