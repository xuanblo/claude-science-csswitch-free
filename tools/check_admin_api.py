# -*- coding: utf-8 -*-
"""验证管理 API（Web 控制台的全部后端接口）。

覆盖：templates / providers 列表 / 新增 / 更新 / 校验 / 删除 / settings。
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from csswitch import config as C  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18993
TOKEN = C.load_state().get("admin_token", "")
BASE = f"http://127.0.0.1:{PORT}"

PASS = FAIL = 0


def call(path, body=None, token=TOKEN):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"x-csx-admin": token} if token is not None else {}
    if data:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs,
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


print("--- 管理 API 验证 ---")

# 0) 鉴权：无 token 必须被拒（403 permission_error 格式）
code, r = call("/__admin/providers", token="")
rejected = (code == 403
            or r.get("type") == "error"
            or r.get("ok") is False
            or "token" in json.dumps(r).lower())
check("无令牌访问被拒", rejected, f"code={code} {str(r)[:80]}")

# 1) 模板清单
code, r = call("/__admin/templates")
kinds = r.get("kinds", [])
check("templates 返回类型清单", code == 200 and len(kinds) >= 8,
      f"got {len(kinds)}")
check("模板含 base_url/默认模型",
      bool(kinds) and all("base_url" in k and "default_model" in k
                          for k in kinds))
check("模板不含任何 key", all("key" not in k for k in kinds))

# 2) provider 列表
code, r = call("/__admin/providers")
check("providers 列表可读", code == 200 and r.get("ok"), str(r)[:100])
items = r.get("providers", [])
check("Key 仅以掩码返回",
      all("key" not in p and "key_masked" in p for p in items))
print(f"       现有{len(items)} 条: {[p['name'] for p in items]}")

# 3) 新增
NEW = "ui-test-provider"
code, r = call("/__admin/provider/save", {
    "name": NEW, "kind": "deepseek",
    "key": "sk-ui-test-not-real-0000",
    "base_url": "https://api.deepseek.com/anthropic",
    "model": "deepseek-v4-pro", "make_current": False})
check("新增 provider", code == 200 and r.get("ok")
      and r.get("action") == "created", str(r)[:150])
check("新增后返回掩码 Key", r.get("key_masked", "").endswith("0000"),
      r.get("key_masked"))

# 4) 列表里能看到
code, r = call("/__admin/providers")
found = [p for p in r.get("providers", []) if p["name"] == NEW]
check("新配置出现在列表", len(found) == 1)
check("列表不回明文 Key",
      bool(found) and "sk-ui-test-not-real-0000" not in json.dumps(r))

# 5) 更新（Key 留空=沿用）
code, r = call("/__admin/provider/save", {
    "name": NEW, "kind": "deepseek", "key": "",
    "model": "deepseek-v4-flash"})
check("更新 provider", code == 200 and r.get("action") == "updated",
      str(r)[:120])
cfg = C.load_config()
rec = C.find_provider(cfg, NEW)
check("更新后模型已变", rec and rec.get("model") == "deepseek-v4-flash",
      str(rec.get("model")) if rec else "no rec")
check("Key 沿用未被掩码覆盖",
      rec and rec.get("key") == "sk-ui-test-not-real-0000",
      "key 被误改")

# 6) 校验接口（占位 Key 预期不通过，但接口本身要通）
code, r = call("/__admin/verify", {"provider": NEW})
v = r.get("verify", {})
check("verify 接口可用", code == 200 and "ok" in v, str(v)[:120])

# 7) settings 读
code, r = call("/__admin/settings")
check("settings 可读", code == 200 and "proxy_port" in r.get("settings", {}))
check("settings 给出参考域名清单",
      len(r.get("trusted_domains", [])) >= 10,
      f"got {len(r.get('trusted_domains', []))}")

# 8) settings 写（端口校验）
code, r = call("/__admin/settings", {"settings": {"proxy_port": 70000}})
check("非法端口被拒", code == 400, f"got {code}")
code, r = call("/__admin/settings",
               {"settings": {"open_browser": True}})
check("合法设置可保存", code == 200 and r.get("ok"), str(r)[:100])

# 9) 删除
code, r = call("/__admin/provider/delete", {"provider": NEW})
check("删除 provider", code == 200 and r.get("ok"), str(r)[:100])
code, r = call("/__admin/providers")
check("删除后列表不含该项",
      not any(p["name"] == NEW for p in r.get("providers", [])))

print(f"\n管理 API: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)