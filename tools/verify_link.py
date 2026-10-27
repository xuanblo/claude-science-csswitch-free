# -*- coding: utf-8 -*-
"""链路验证：不经浏览器，直接打代理验证转发路径是否完整。

对占位 Key 来说预期结果是上游 401 —— 这恰好证明
  Science 的推理格式 -> 代理 -> 上游地址 整条链路是通的，
  只是 Key 无效而已。
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from csswitch import config as C  # noqa: E402


def main():
    st = C.load_state()
    port = st.get("proxy_port") or C.load_config()["settings"]["proxy_port"]
    secret = st.get("auth_secret")
    if not secret:
        print("代理未运行（state.json 无 auth_secret），先执行 csx proxy-start")
        return 1
    base = f"http://127.0.0.1:{port}/{secret}"

    print(f"代理:127.0.0.1:{port}  secret 前4位={secret[:4]}***")

    # 1) /v1/models —— 看壳模型列表能否装配出来
    try:
        req = urllib.request.Request(f"{base}/v1/models",
                                    headers={"x-api-key": "x", "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
            ids = [m["id"] for m in d.get("data", [])]
            print(f"[1] /v1/models  OK  返回 {len(ids)} 个模型: {ids}")
    except urllib.error.HTTPError as e:
        print(f"[1] /v1/models  HTTP {e.code}: {e.read()[:160]}")
    except Exception as e:
        print(f"[1] /v1/models  失败: {e}")

    # 2) /v1/messages —— 真实推理请求（预期占位 Key -> 上游 401）
    body = {
        "model": "claude-opus-4-8", "max_tokens": 32,
        "messages": [{"role": "user", "content": "say hi"}],
    }
    try:
        req = urllib.request.Request(
            f"{base}/v1/messages", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "x-api-key": "placeholder", "anthropic-version": "2023-06-01"},
            method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"[2] /v1/messages OK  {r.read()[:200]}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        print(f"[2] /v1/messages 上游返回 HTTP {e.code}")
        print(f"    响应: {raw[:300]}")
        if e.code in (401, 403):
            print("    => 结论：转发链路完整（代理->上游成功），仅 Key 无效。")
            print("    => 换成真实 Key 即可用。")
        else:
            print("    => 需排查：链路或协议翻译可能有问题。")
    except Exception as e:
        print(f"[2] /v1/messages 失败: {e}")

    # 3) 鉴权护栏 —— 无 secret 应被拒
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models")
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"[3] 安全护栏 异常：裸路径竟然返回 {r.status}")
    except urllib.error.HTTPError as e:
        print(f"[3] 安全护栏  OK  裸路径被拒 HTTP {e.code}")
    return 0


if __name__ == "__main__":
    sys.exit(main())