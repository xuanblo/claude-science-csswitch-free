#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CSswitch-Win 离线自测套件（不依赖真实 Claude Science 与真实第三方 API）。

覆盖：
  A. HKDF（RFC 5869 向量 + 手写实现对拍 cryptography）
  B. v2 令牌加解密 roundtrip / 结构 / 防篡改
  C. 虚拟登录：文件布局、字段、幂等、修复、护栏
  D. 模型解析与协议翻译（纯函数）
  E. 代理整链（假上游）：鉴权、透传、翻译、流式、CONNECT fast-fail、
     管理 API 热切换、/v1/models
  F. CLI 增删改查（子进程，CSSWITCH_HOME 隔离）

运行： python tests/selftest.py
"""
import base64
import hashlib
import hmac
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from csswitch import cli as cli_mod  # noqa: E402
from csswitch import config as C  # noqa: E402
from csswitch import constants as K  # noqa: E402
from csswitch import proxy as proxy_mod  # noqa: E402
from csswitch import sandbox as S  # noqa: E402
from csswitch import server as SRV  # noqa: E402
from csswitch import lifecycle as LIFECYCLE  # noqa: E402

PASS = 0
FAIL = 0
FAILED = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  ✗ {name}  {detail}")


def section(title):
    print(f"\n── {title} " + "─" * max(0, 54 - len(title)))


# ================= A. HKDF =================

def hkdf_manual(ikm: bytes, salt: bytes, info: bytes, length: int) -> int:
    """RFC 5869 手写 HKDF-SHA256。"""
    hl = 32
    if not salt:
        salt = b"\x00" * hl
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm, t = b"", b""
    for i in range(1, (length + hl - 1) // hl + 1):
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        okm += t
    return okm[:length]


def test_hkdf():
    section("A. HKDF-SHA256（RFC 5869 向量 + 手写实现对拍）")
    # RFC 5869 Test Case 1（带 salt/info）
    ikm = b"\x0b" * 22
    salt = bytes(range(13))
    info = bytes(range(0xf0, 0xfa))
    okm = hkdf_manual(ikm, salt, info, 42)
    expect = ("3cb25f25faacd57a90434f64d0362f2a"
              "2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
              "34007208d5b887185865")
    check("RFC5869 TC1 手写 HKDF", okm.hex() == expect, okm.hex())

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    got = HKDF(algorithm=hashes.SHA256(), length=42, salt=salt,
               info=info).derive(ikm)
    check("cryptography HKDF == RFC 向量", got.hex() == expect, got.hex())

    # 本项目用法：salt=b""（等价 Node hkdfSync 的 Buffer.alloc(0)）
    from csswitch.oauth_forge import HKDF_INFO, derive_key
    key_b64 = base64.b64encode(b"K" * 32).decode()
    k1 = derive_key(key_b64)
    k2 = hkdf_manual(b"K" * 32, b"", HKDF_INFO, 32)
    check("derive_key == 手写 HKDF(salt=b'')", k1 == k2)
    k3 = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"",
              info=HKDF_INFO).derive(b"K" * 32)
    check("derive_key == cryptography(salt=b'')", k1 == k3)
    check("派生密钥 32 字节", len(k1) == 32)


# ================= B. v2 令牌 =================

def test_token_crypto():
    section("B. v2 令牌加密格式")
    from csswitch.oauth_forge import (AAD, encrypt_token_v2, decrypt_token_v2)
    key_b64 = base64.b64encode(os.urandom(32)).decode()
    blob = json.dumps({"email": "virtual@localhost.invalid"}).encode()
    enc = encrypt_token_v2(blob, key_b64)
    check("v2: 前缀", enc.startswith("v2:"))
    raw = base64.b64decode(enc[3:])
    check("IV(12)+密文+tag(16) 帧长", len(raw) == 12 + len(blob) + 16)
    check("roundtrip 解密", decrypt_token_v2(enc, key_b64) == blob)
    # 防篡改：改一个字节必须验签失败
    tampered = bytearray(raw)
    tampered[-1] ^= 0xFF
    bad = "v2:" + base64.b64encode(bytes(tampered)).decode()
    try:
        decrypt_token_v2(bad, key_b64)
        check("篡改密文解密必失败", False)
    except Exception:
        check("篡改密文解密必失败", True)
    # 错密钥必失败
    other = base64.b64encode(os.urandom(32)).decode()
    try:
        decrypt_token_v2(enc, other)
        check("错密钥解密必失败", False)
    except Exception:
        check("错密钥解密必失败", True)


# ================= C. 虚拟登录 =================

def test_virtual_login():
    section("C. 虚拟登录（布局/字段/幂等/修复/护栏）")
    from csswitch import oauth_forge as OF
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        real = td / "real" / ".claude-science"
        sbroot = td / "sandbox"
        auth = sbroot / "home" / ".claude-science"
        real.mkdir(parents=True)

        res, action = OF.ensure_virtual_login(auth, sandbox_root=sbroot,
                                              real_dir=real)
        check("首次 = CREATED", action == OF.LoginAction.CREATED)
        # 文件布局
        ek = auth / "encryption.key"
        check("encryption.key 存在", ek.is_file())
        keys = OF.parse_encryption_key(auth)
        check("四把钥匙齐全", all(k in keys for k in OF.KEY_NAMES))
        encs = list((auth / ".oauth-tokens").glob("*.enc"))
        check(".oauth-tokens 恰好一个 .enc", len(encs) == 1)
        org = json.loads((auth / "active-org.json").read_text())
        check("active-org org_uuid 合法 UUID", OF.looks_like_uuid(org["org_uuid"]))
        # 解出的 blob 字段
        blob = json.loads(OF.decrypt_token_v2(
            encs[0].read_text(), keys["OAUTH_ENCRYPTION_KEY"]))
        check("email 为假账号", blob["email"] == OF.VIRTUAL_EMAIL)
        check("token_expires_at 远期", blob["token_expires_at"].startswith("2099"))
        check("provider=claude_ai", blob["provider"] == "claude_ai")
        check("scopes 含 inference/profile/mcp",
              all(s in blob["scopes"] for s in
                  ("user:inference", "user:profile", "user:mcp_servers")))
        check("account_uuid 与文件名一致",
              encs[0].stem == res["account_uuid"])

        # 幂等：第二次 REUSED，身份不变
        res2, action2 = OF.ensure_virtual_login(auth, sandbox_root=sbroot,
                                                real_dir=real)
        check("二次 = REUSED", action2 == OF.LoginAction.REUSED)
        check("复用同 account_uuid", res2["account_uuid"] == res["account_uuid"])
        check("复用同 org_uuid", res2["org_uuid"] == res["org_uuid"])

        # 修复：删 active-org.json -> REPAIRED 且沿用原 org
        (auth / "active-org.json").unlink()
        res3, action3 = OF.ensure_virtual_login(auth, sandbox_root=sbroot,
                                                real_dir=real)
        check("缺 active-org = REPAIRED", action3 == OF.LoginAction.REPAIRED)
        check("修复沿用原 org_uuid", res3["org_uuid"] == res["org_uuid"])
        check("修复沿用原 account_uuid",
              res3["account_uuid"] == res["account_uuid"])

        # 护栏 2：假账号 email
        try:
            OF.resolve_guarded(auth, "real@gmail.com", sbroot, real)
            check("拒绝非假账号 email", False)
        except ValueError:
            check("拒绝非假账号 email", True)
        # 护栏 0：auth_dir 指向真实目录
        try:
            OF.resolve_guarded(real, OF.VIRTUAL_EMAIL, sbroot, real)
            check("拒绝写入真实目录", False)
        except ValueError:
            check("拒绝写入真实目录", True)
        # 护栏 0b：真实目录之子目录
        try:
            OF.resolve_guarded(real / "sub", OF.VIRTUAL_EMAIL, sbroot, real)
            check("拒绝写入真实目录子路径", False)
        except ValueError:
            check("拒绝写入真实目录子路径", True)
        # 护栏 1：沙箱外
        try:
            OF.resolve_guarded(td / "outside", OF.VIRTUAL_EMAIL, sbroot, real)
            check("拒绝沙箱外写入", False)
        except ValueError:
            check("拒绝沙箱外写入", True)


# ================= D. 模型解析与翻译 =================

def test_model_and_translation():
    section("D. 模型解析 / 协议翻译（纯函数）")
    os.environ["CSSWITCH_HOME"] = tempfile.mkdtemp(prefix="csx-test-")
    from csswitch import config as C
    from csswitch import proxy as PR

    cfg = C.load_config()
    rec = C.new_provider_record("t-ds", "deepseek", "k" * 20)
    C.add_provider(cfg, rec)
    PR.load_provider("t-ds")

    check("壳 id opus -> 默认模型(位置0)",
          PR.resolve_model("claude-opus-4-8") == "deepseek-v4-pro")
    check("壳 id haiku -> 位置1",
          PR.resolve_model("claude-haiku-4-5") == "deepseek-v4-flash")
    check("硬编码 sonnet-5 -> flash",
          PR.resolve_model("claude-sonnet-5") == "deepseek-v4-flash")
    check("日期后缀剥离",
          PR.resolve_model("claude-opus-4-8-20260101") == "deepseek-v4-pro")
    check("空名 -> 默认", PR.resolve_model("") == "deepseek-v4-pro")
    check("未知名 -> 默认", PR.resolve_model("gpt-4o") == "deepseek-v4-pro")

    # thinking 归一化
    b = {"tool_choice": {"type": "any"}, "thinking": {"type": "auto"}}
    PR.normalize_thinking(b)
    check("强制工具 -> thinking disabled",
          b["thinking"] == {"type": "disabled"})
    b2 = {"thinking": {"type": "auto"}}
    PR.normalize_thinking(b2)
    check("auto -> adaptive", b2["thinking"]["type"] == "adaptive")
    # max_tokens 夹取
    check("max_tokens 夹取到 65536",
          PR.clamp_max_tokens(999999, "deepseek-v4-pro") == 65536)

    # Anthropic -> OpenAI 翻译
    areq = {
        "model": "claude-opus-4-8",
        "max_tokens": 100,
        "system": [{"type": "text", "text": "你是助手"}],
        "messages": [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "好"},
                {"type": "tool_use", "id": "t1", "name": "bash",
                 "input": {"cmd": "ls"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": [{"type": "text", "text": "out"}]}]},
        ],
        "tools": [{"name": "bash", "description": "run",
                   "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "auto"},
    }
    oreq = PR.anthropic_to_openai(areq)
    check("system 提为首条", oreq["messages"][0]["role"] == "system")
    check("tool_use -> tool_calls",
          oreq["messages"][2]["tool_calls"][0]["function"]["name"] == "bash")
    check("tool_result -> role=tool",
          oreq["messages"][3]["role"] == "tool"
          and oreq["messages"][3]["tool_call_id"] == "t1")
    check("tools 翻译",
          oreq["tools"][0]["function"]["parameters"]["type"] == "object")
    check("tool_choice auto 直传", oreq["tool_choice"] == "auto")
    check("模型映射到真实 id", oreq["model"] == "deepseek-v4-pro")

    # OpenAI -> Anthropic 回转
    oresp = {"id": "x1", "choices": [{"message": {
        "content": "结果", "tool_calls": [
            {"id": "c1", "type": "function", "function": {
                "name": "bash", "arguments": "{\"cmd\":\"ls\"}"}}]},
        "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7}}
    aresp = PR.openai_to_anthropic(oresp, "claude-opus-4-8")
    types = [b["type"] for b in aresp["content"]]
    check("回转含 text+tool_use", types == ["text", "tool_use"])
    check("回转 stop_reason=tool_use", aresp["stop_reason"] == "tool_use")
    check("回转 usage", aresp["usage"]["input_tokens"] == 5)
    check("回转 tool input 解析",
          aresp["content"][1]["input"] == {"cmd": "ls"})


# ================= 假上游 =================

class MockUpstream(BaseHTTPRequestHandler):
    """假上游：记录请求，按模式返回 canned 响应。"""
    mode = "anthropic"
    received = []

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        MockUpstream.received.append(
            ("GET", self.path, {k.lower(): v for k, v in self.headers.items()}))
        if self.path.startswith("/v1/models") or self.path.startswith("/models"):
            self._json(200, {"data": [
                {"id": "up-model-a", "display_name": "Up A"},
                {"id": "up-model-b", "display_name": "Up B"}]})
        else:
            self._json(404, {"error": "nf"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        MockUpstream.received.append(
            ("POST", self.path, {k.lower(): v for k, v in self.headers.items()},
             json.loads(raw)))
        if MockUpstream.mode == "anthropic":
            if self.path.startswith("/v1/messages"):
                if json.loads(raw).get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for ev, data in [
                        ("message_start", {"type": "message_start"}),
                        ("content_block_delta", {"delta": {
                            "type": "text_delta", "text": "hello"}}),
                        ("message_stop", {"type": "message_stop"})]:
                        chunk = f"event: {ev}\ndata: {json.dumps(data)}\n\n".encode()
                        self.wfile.write(hex(len(chunk))[2:].encode()
                                         + b"\r\n" + chunk + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                else:
                    self._json(200, {"id": "msg_up", "type": "message",
                                     "role": "assistant",
                                     "model": "up-model-a",
                                     "content": [{"type": "text",
                                                  "text": "pong"}],
                                     "stop_reason": "end_turn",
                                     "usage": {"input_tokens": 1,
                                               "output_tokens": 1}})
            else:
                self._json(404, {"error": "nf"})
        else:  # openai
            self._json(200, {"id": "chatcmpl-x", "choices": [{"message": {
                "content": "openai-pong"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4}})


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http(method, url, body=None, headers=None, timeout=10, raw=False):
    req = urllib.request.Request(url, data=body, headers=headers or {},
                                 method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        return r.status, (data if raw else json.loads(data))


def test_proxy_chain():
    section("E. 代理整链（假上游）")
    from csswitch import config as C
    home = Path(tempfile.mkdtemp(prefix="csx-proxy-"))
    os.environ["CSSWITCH_HOME"] = str(home)

    mock_port = _free_port()
    proxy_port = _free_port()
    MockUpstream.mode = "anthropic"
    mock = ThreadingHTTPServer(("127.0.0.1", mock_port), MockUpstream)
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    # 造两条配置：A=deepseek(anthropic 透传) B=qwen(openai 翻译)，都指假上游
    cfg = C.load_config()
    rec_a = C.new_provider_record("p-anthropic", "deepseek", "KEY-AAA")
    rec_a["base_url"] = f"http://127.0.0.1:{mock_port}"
    rec_b = C.new_provider_record("p-openai", "qwen", "KEY-BBB")
    rec_b["base_url"] = f"http://127.0.0.1:{mock_port}"
    C.add_provider(cfg, rec_a)
    C.add_provider(cfg, rec_b)
    C.set_current(cfg, "p-anthropic")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        [sys.executable, "-m", "csswitch.proxy", "--port", str(proxy_port)],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, **S._process_options())
    try:
        # 等 state.json + health
        secret = None
        for _ in range(60):
            st = C.load_state()
            if st.get("proxy_port") == proxy_port and st.get("auth_secret"):
                secret = st["auth_secret"]
                break
            time.sleep(0.25)
        check("代理启动并写入 state", secret is not None)
        if not secret:
            return
        base = f"http://127.0.0.1:{proxy_port}/{secret}"

        # --- 鉴权 ---
        try:
            _http("GET", f"http://127.0.0.1:{proxy_port}/v1/models")
            check("无 secret 访问被拒 403", False)
        except urllib.error.HTTPError as e:
            check("无 secret 访问被拒 403", e.code == 403)

        # --- health ---
        st_code, body = _http("GET", f"{base}/health")
        check("health ok + provider", st_code == 200
              and body["provider"] == "p-anthropic", str(body))

        # --- /v1/models 壳 id 平铺 ---
        _, models = _http("GET", f"{base}/v1/models")
        ids = [m["id"] for m in models["data"]]
        disp = [m["display_name"] for m in models["data"]]
        check("models 壳 id 平铺", ids[:2] == ["claude-opus-4-8",
                                              "claude-haiku-4-5"], str(ids))
        check("models 显示真实模型名", disp[0] == "deepseek-v4-pro", str(disp))

        # --- anthropic 透传（非流式）---
        MockUpstream.received.clear()
        req = {"model": "claude-opus-4-8", "max_tokens": 100,
               "messages": [{"role": "user", "content": "hi"}],
               "stream": False}
        code, resp = _http("POST", f"{base}/v1/messages",
                           json.dumps(req).encode(),
                           {"Content-Type": "application/json",
                            "Authorization": "Bearer science-virtual-oauth",
                            "x-api-key": "science-virtual-key"})
        check("透传 200", code == 200 and resp["content"][0]["text"] == "pong")
        posts = [r for r in MockUpstream.received if r[0] == "POST"]
        check("上游收到 1 条 POST", len(posts) == 1)
        if posts:
            _, path, hdrs, body = posts[0]
            check("上游路径 /v1/messages", path.startswith("/v1/messages"))
            check("入站 Authorization 已剥离", "Authorization" not in hdrs)
            check("入站 x-api-key 已替换", hdrs.get("x-api-key") == "KEY-AAA")
            check("模型名已映射", body["model"] == "deepseek-v4-pro")
            check("anthropic-version 透传",
                  hdrs.get("anthropic-version") == "2023-06-01")

        # --- anthropic 透传（流式 SSE）---
        MockUpstream.received.clear()
        req["stream"] = True
        code, raw = _http("POST", f"{base}/v1/messages",
                          json.dumps(req).encode(),
                          {"Content-Type": "application/json"}, raw=True)
        text = raw.decode()
        check("流式 200", code == 200)
        check("流式含 message_start/message_stop",
              "event: message_start" in text and "event: message_stop" in text)
        check("流式含增量文本", "hello" in text)

        # --- CONNECT fast-fail ---
        with socket.create_connection(("127.0.0.1", proxy_port), timeout=5) as s:
            s.sendall(b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n"
                      b"Host: api.anthropic.com:443\r\n\r\n")
            data = s.recv(200).decode(errors="replace")
        check("CONNECT Anthropic 域名 -> 401", " 401 " in data, data[:60])
        # 普通域名 -> 隧道透传（用本机 mock 端口，避免依赖外网）
        with socket.create_connection(("127.0.0.1", proxy_port), timeout=5) as s:
            s.sendall(f"CONNECT 127.0.0.1:{mock_port} HTTP/1.1\r\n"
                      f"Host: 127.0.0.1:{mock_port}\r\n\r\n".encode())
            data = s.recv(200).decode(errors="replace")
        check("CONNECT 普通目标 -> 200 隧道", " 200 " in data, data[:60])

        # --- 管理 API 热切换 -> openai 翻译路径 ---
        st = C.load_state()
        admin = st["admin_token"]
        code, sw = _http("POST",
                         f"http://127.0.0.1:{proxy_port}/__admin/switch",
                         json.dumps({"provider": "p-openai"}).encode(),
                         {"Content-Type": "application/json",
                          "x-csx-admin": admin})
        check("热切换成功", code == 200 and sw.get("switched") is True, str(sw))
        _, st2 = _http("GET", f"{base}/health")
        check("health 显示新 provider", st2["provider"] == "p-openai")

        MockUpstream.mode = "openai"
        MockUpstream.received.clear()
        req2 = {"model": "claude-opus-4-8", "max_tokens": 50,
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"name": "bash", "description": "d",
                           "input_schema": {"type": "object"}}]}
        code, resp2 = _http("POST", f"{base}/v1/messages",
                            json.dumps(req2).encode(),
                            {"Content-Type": "application/json"})
        check("openai 翻译 200", code == 200
              and resp2["content"][0]["text"] == "openai-pong")
        posts = [r for r in MockUpstream.received if r[0] == "POST"]
        if posts:
            _, path, hdrs, body = posts[0]
            check("openai 上游路径 chat/completions",
                  path.startswith("/chat/completions"), path)
            check("openai 上游 Bearer 注入",
                  hdrs.get("authorization") == "Bearer KEY-BBB")
            check("anthropic->openai tools 翻译",
                  body["tools"][0]["function"]["name"] == "bash")

        # --- 坏 admin token ---
        try:
            _http("POST", f"http://127.0.0.1:{proxy_port}/__admin/switch",
                  json.dumps({"provider": "p-anthropic"}).encode(),
                  {"Content-Type": "application/json",
                   "x-csx-admin": "wrong"})
            check("坏 admin token 被拒", False)
        except urllib.error.HTTPError as e:
            check("坏 admin token 被拒", e.code == 403)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        mock.shutdown()


# ================= F. CLI =================

def test_cli():
    section("F. CLI（子进程，隔离 CSSWITCH_HOME）")
    home = Path(tempfile.mkdtemp(prefix="csx-cli-"))
    env = dict(os.environ)
    env["CSSWITCH_HOME"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"

    def run(*args):
        return subprocess.run(
            [sys.executable, str(ROOT / "csx.py"), *args],
            cwd=str(ROOT), env=env, capture_output=True,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
            timeout=60, **S._process_options())

    r = run("add", "--name", "cli-ds", "--kind", "deepseek", "--key", "sk-test-1234")
    check("add 退出码 0", r.returncode == 0, r.stderr[-200:])
    r = run("list")
    check("list 含 cli-ds", "cli-ds" in r.stdout)
    r = run("use", "cli-ds")
    check("use 成功", r.returncode == 0 and "当前配置" in r.stdout)
    r = run("status")
    check("status 退出码 0", r.returncode == 0)
    r = run("doctor")
    check("doctor 退出码 0", r.returncode == 0)
    r = run("remove", "cli-ds")
    check("remove 成功", r.returncode == 0)
    r = run("list")
    check("remove 后列表为空", "cli-ds" not in r.stdout)
    # 不存在的配置
    r = run("use", "nope")
    check("use 不存在 -> 退出码 1", r.returncode == 1)


def test_regressions_from_real_machine():
    """真实机器上踩过的三个坑的回归防护。

    这三条不是理论推演，是本机实测发现并修复的问题：
      1. 官方CSwitch 与本工具共用~/.csswitch 会导致配置互相污染 + 真实
         API Key 串味-> 必须独立目录 + 格式护栏；
      2. 代理进程继承父环境https_proxy 会让所有出站请求绕回自身 ->
         os error 10061 假502；
      3. Windows 上给 claude-science serve 传 --detached 会导致 daemon
         秒退（spawn.log: ended at +298ms）。
    """
    print("\n--- G. 真实机器回归防护 ---")

    # 1) 目录名与官方 CSwitch 区分
    check("配置目录与官方 CSwitch 隔离(.csswitch-win)",
          C.CONFIG_DIR_NAME == ".csswitch-win", C.CONFIG_DIR_NAME)

    # 1b) 格式护栏：官方配置必须被拒
    foreign = {"schema_version": 2, "profiles": [], "active_id": None}
    try:
        C._assert_not_foreign_config(foreign)
        raised = False
    except C.ForeignConfigError:
        raised = True
    check("护栏拒绝官方格式配置", raised)

    # 1c) 本工具自己的配置必须放行
    own = {"version": 1, "current": None, "providers": [],
           "settings": dict(C.DEFAULT_SETTINGS)}
    try:
        C._assert_not_foreign_config(own)
        raised = False
    except C.ForeignConfigError:
        raised = True
    check("护栏放行本工具配置", not raised)

    # 2) 代理变量清单覆盖大小写全8 项，且沙箱/代理共用同一份
    check("PROXY_ENV_VARS 覆盖 8 项（含 FTP/大小写）",
          len(K.PROXY_ENV_VARS) == 8
          and "https_proxy" in K.PROXY_ENV_VARS
          and "HTTPS_PROXY" in K.PROXY_ENV_VARS
          and "FTP_PROXY" in K.PROXY_ENV_VARS,
          str(K.PROXY_ENV_VARS))
    src_sb = Path(S.__file__).read_text(encoding="utf-8")
    check("沙箱与代理共用 PROXY_ENV_VARS 单一真相源",
          "_PROXY_ENV_VARS = K.PROXY_ENV_VARS" in src_sb
          and "K.PROXY_ENV_VARS" in Path(proxy_mod.__file__).read_text(
              encoding="utf-8"))

    # 3) serve 不得再传 --detached
    src_all = src_sb + Path(cli_mod.__file__).read_text(encoding="utf-8")
    code_lines = [ln for ln in src_all.splitlines()
                  if '"serve"' in ln or "'serve'" in ln]
    check("serve 未传 --detached（Windows 会秒退）",
          not any("--detached" in ln for ln in code_lines),
          "; ".join(code_lines[:2]))

    # 4) CONNECT 隧道全放行（v1.2.0 用户明确要求），但 Anthropic 仍须401
    from csswitch import server as srv_mod
    check("CONNECT 隧道全放行（用户要求不做限制）",
          srv_mod._tunnel_allowed("anything.example:443", "anything.example")
          and srv_mod._tunnel_allowed("conda.anaconda.org:443",
                                      "conda.anaconda.org"),
          "存在仍会拒绝的目标")
    check("Anthropic 域名仍在 BLOCKED_SUFFIXES（401 fast-fail 必需）",
          "anthropic.com" in K.BLOCKED_SUFFIXES
          and "claude.ai" in K.BLOCKED_SUFFIXES,
          str(K.BLOCKED_SUFFIXES))

    # 5) 面板一键启停 Claude Science：模块与路由必须都在
    from csswitch import lifecycle
    check("lifecycle 模块状态机齐全",
          all(hasattr(lifecycle, s) for s in
              ("snapshot", "is_busy", "start", "stop", "current_url")))
    check("lifecycle 初始状态为 idle",
          lifecycle.snapshot().get("state") == "idle",
          str(lifecycle.snapshot()))
    src_sv = Path(srv_mod.__file__).read_text(encoding="utf-8")
    check("管理路由含沙箱启停与状态",
          '"/__admin/sandbox"' in src_sv
          and '"/__admin/sandbox/start"' in src_sv
          and '"/__admin/sandbox/stop"' in src_sv)
    src_pn = (ROOT / "csswitch" / "web" / "panel.html").read_text(encoding="utf-8")
    check("面板含启动/停止按钮与轮询逻辑",
          all(s in src_pn for s in ("btnStart", "btnStop", "startSandbox",
                                    "stopSandbox", "pollSandbox")))

    # 6) 登录链接必须是新鲜的：nonce 一次性、3 分钟过期，绝不能缓存派发
    check("lifecycle 有短 TTL 的 url 缓存（避免高频 fork）",
          hasattr(lifecycle, "_url_cache"),
          "缺 _url_cache")
    check("TTL 远小于 3 分钟有效期",
          lifecycle._url_cache.get("at") == 0.0
          and getattr(lifecycle, "_IDLE", "idle") == "idle",
          "初始缓存应为空")
    src_lc = Path(lifecycle.__file__).read_text(encoding="utf-8")
    check("current_url 签名带 max_age_s（默认 45s）",
          "def current_url(max_age_s" in src_lc
          and "45.0" in src_lc,
          "缺 TTL 参数")
    check("启动完成不再把 url 写入 task（避免派发旧链）",
          "url=None, port=r.get(\"port\")" in src_lc
          or 'url=None, port=r.get("port")' in src_lc,
          "仍在缓存启动时的 URL")
    check("面板打开链接走 openScience() 现取，不再用 href",
          "openScience" in src_pn and "sbLink" not in src_pn,
          "仍有静态链接元素")
    check("面板提示 nonce 一次性 3 分钟失效",
          "3 分钟" in src_pn and "一次性" in src_pn)

    # 7) 工作目录 + 安全沙箱开关（v1.3.0）
    check("默认启用安全沙箱",
          C.DEFAULT_SETTINGS.get("sandbox_enabled") is True,
          str(C.DEFAULT_SETTINGS.get("sandbox_enabled")))
    check("面板含安全沙箱开关",
          all(s in src_pn for s in ("sSbx", "sandbox_enabled")))
    check("已移除默认工作目录（由文件夹授权替代）",
          "work_dir" not in src_pn and "sWork" not in src_pn
          and "work_dir" not in C.DEFAULT_SETTINGS
          and "work_dir" not in
          Path(SRV.__file__).read_text(encoding="utf-8"))
    src_sb2 = Path(S.__file__).read_text(encoding="utf-8")
    check("sandbox_env 按开关决定是否重定向 USERPROFILE",
          'isolate = bool(settings.get("sandbox_enabled", True))' in src_sb2)
    check("关闭安全沙箱时传--dangerously-no-sandbox",
          "--dangerously-no-sandbox" in src_sb2)
    check("虚拟登录目录与沙箱开关解耦（始终隔离）",
          "C.sandbox_auth_dir()" in src_sb2
          and "sandbox_enabled" not in src_sb2.split(
              "ensure_virtual_login(")[1][:300],
          "虚拟登录可能随开关被改到真实目录")

    # 8) 宿主文件夹授权（v1.4.0）—— 让每个对话能选宿主目录
    from csswitch import hostgrant as HG
    src_hg = Path(HG.__file__).read_text(encoding="utf-8")
    src_sv = Path(SRV.__file__).read_text(encoding="utf-8")

    check("hostgrant 走 daemon 登录表单换会话",
          "api/auth/nonce" in src_hg and "operon_auth" in src_hg,
          "缺少会话建立逻辑")
    check("hostgrant 写操作带 Origin + x-operon-csrf",
          "x-operon-csrf" in src_hg and "Origin" in src_hg)
    check("hostgrant 绕过系统代理（只打 127.0.0.1）",
          "ProxyHandler({})" in src_hg,
          "会继承 https_proxy 导致 10061")
    check("hostgrant mode 白名单 ro/rw",
          HG.MODES == ("ro", "rw"), str(HG.MODES))
    check("normalize_mode 兼容 read/write 写法",
          HG.normalize_mode("read") == "ro"
          and HG.normalize_mode("write") == "rw")
    check("mode 留空取默认 rw",
          HG.normalize_mode("") == "rw" and HG.normalize_mode(None) == "rw")
    for bad in ("xx", "rwx", "delete"):
        try:
            HG.normalize_mode(bad)
            check(f"非法 mode {bad!r} 被拒", False, "竟然通过了")
        except HG.HostGrantError:
            check(f"非法 mode {bad!r} 被拒", True)
    for bad, why in ((r"D:\绝对不存在XYZ", "不存在"),
                     ("相对/路径", "非绝对路径")):
        try:
            HG.normalize_path(bad)
            check(f"{why} 被拒", False, "竟然通过了")
        except HG.HostGrantError:
            check(f"{why} 被拒", True)
    check("Grant.as_dict 暴露 writable",
          HG.Grant(r"D:\P", "P", "rw").as_dict()["writable"] is True
          and HG.Grant(r"D:\P", "P", "ro").as_dict()["writable"] is False)
    ar = HG.AddResult(r"D:\P", "P", "rw", recorded=False)
    check("AddResult 在未落库时不谎报成功",
          ar.recorded is False and "无需" in ar.effective, ar.effective)
    ar2 = HG.AddResult(r"D:\P", "P", "ro", already_allowed=True,
                       reason="Already accessible")
    check("AddResult 识别 already_allowed",
          ar2.already_allowed and ar2.effective == ar2.reason,
          ar2.effective)
    check("端点常量与 daemon 一致",
          (HG.GRANTS_EP == "/api/preferences/host-grants"
           and HG.HOME_EP == "/api/preferences/host-home"
           and HG.BROWSE_EP == "/api/preferences/host-browse"))
    # 沙箱关闭时 daemon 不记录授权（返回 alreadyAllowed），sandbox_active
    # 必须以配置为准 —— 早前版本比较 host_home() 与 Path.home()，daemon
    # 刚起来那一瞬登录失败会被误判成"沙箱没开"
    import inspect as _inspect
    _sa = _inspect.getsource(HG.sandbox_active)
    # 去掉 docstring 与注释，只看真正执行的代码
    _code = "\n".join(ln for ln in _sa.splitlines()
                      if not ln.strip().startswith(("#", '"'))
                      and "host_home" not in ln)
    check("sandbox_active 只读配置、不做网络探测",
          'get("sandbox_enabled", True)' in _sa
          and "host_home(" not in _code,
          "仍调用 daemon 探测，会被瞬时失败带偏")
    check("409 权限降级给出可操作提示",
          "status == 409" in src_hg and "撤销" in src_hg)
    check("撤销授权不要求目录仍存在（删了就撤销不掉是 bug）",
          "must_exist: bool = True" in src_hg
          and "normalize_path(path, must_exist=False)" in src_hg)

    check("server 暴露 host-grants 读接口",
          '"/__admin/host-grants"' in src_sv
          and '"/__admin/host-browse"' in src_sv)
    check("server 暴露授权增删接口",
          '"/__admin/host-grant/add"' in src_sv
          and '"/__admin/host-grant/remove"' in src_sv)
    check("server 实现了 do_DELETE",
          "def do_DELETE" in src_sv,
          "DELETE 请求会得到 501")
    src_lc = Path(LIFECYCLE.__file__).read_text(encoding="utf-8")
    check("工作目录同步逻辑已彻底移除",
          "_sync_work_dir_grants" not in src_lc
          and "sync_settings_folders" not in
          Path(HG.__file__).read_text(encoding="utf-8"))
    check("daemon 重启后丢弃旧会话 cookie",
          src_lc.count("reset_session()") >= 2,
          f"出现 {src_lc.count('reset_session()')} 次")
    check("hostgrant 延迟导入避免循环依赖",
          "hostgrant" in src_lc and "import hostgrant as HG\nfrom . import lifecycle"
          not in src_lc)

    check("面板含授权卡片与增删按钮",
          all(s in src_pn for s in
              ("<h2>允许访问的本机文件夹", "gPath", "gMode", "grantList",
               "addGrant", "delGrant", "loadGrants")))
    check("按钮用 data-* 传值（Windows 路径不被 JS 字符串转义）",
          'data-path="${esc(g.hostPath)}"' in src_pn
          and "delGrant(this.dataset.path" in src_pn
          and 'data-name="${esc(p.name)}"' in src_pn
          and "editProvider(this.dataset.name)" in src_pn)
    check("面板解释了Science 每对话独立工作区的机制",
          "每个对话" in src_pn and "沙箱工作区" in src_pn)
    check("面板按沙箱状态区分文案",
          "sandboxActive" in src_pn
          and "不需要也无法登记授权" in src_pn
          and "列表恒为空" in src_pn)
    check("面板说明授权解决的是访问而非工作目录",
          "授权解决的是「能不能访问」" in src_pn
          and "workspaces 目录" in src_pn)
    check("面板提示对话内可直接用绝对路径",
          "list_host_grants" in src_pn
          and "request_host_access" in src_pn
          and "读一下" in src_pn)
    check("面板对未落库结果不谎报成功",
          "r.recorded === false" in src_pn and "'ℹ '" in src_pn)

    # 9) 两栏布局：工作环境 + 宿主文件夹授权在右列
    check("面板是左右两栏布局",
          "class=\"cols\"" in src_pn and src_pn.count('class="col"') >= 2
          and "grid-template-columns: minmax(0, 1.6fr) minmax(400px, 1fr)"
          in src_pn)
    check("页面铺满视口，不限制最大宽度",
          "max-width: 1500px" not in src_pn)
    check("面板支持亮/暗主题并默认跟随系统",
          "prefers-color-scheme" in src_pn
          and 'data-theme="light"' in src_pn
          and "cycleTheme" in src_pn
          and "csx_theme" in src_pn)
    check("窄屏回退单栏",
          "@media (max-width: 1100px)" in src_pn
          and ".cols { grid-template-columns: 1fr; }" in src_pn)
    # 两栏分界必须落在"已配置的上游"之后、"工作环境"之前
    _main = re.search(r"<main>(.*?)</main>", src_pn, re.S)
    _body = _main.group(1)
    # 用 class="col" 的第二个出现位置作分界（第一个是左栏）
    _first = _body.find('class="col"')
    _split = _body.find('class="col"', _first + 1)
    check("左栏在前、右栏在后",
          _first > 0 and _split > _first
          and _body.find("已配置的上游") < _split
          and _body.find("<h2>文件访问权限") > _split
          and _body.find("<h2>允许访问的本机文件夹") > _split,
          f"分界@{_split} 左栏含列表={_body.find('已配置的上游') < _split}")
    check("右栏含文件访问/文件夹/网络出口",
          _split > 0 and all(k in _body[_split:] for k in
                             ("<h2>文件访问权限", "<h2>允许访问的本机文件夹",
                              "网络出口")))
    check("端口与运行时在左栏（右栏只留文件相关）",
          _split > 0 and "端口与运行时" in _body[:_split]
          and "端口与运行时" not in _body[_split:])
    check("左栏不含右栏内容",
          _split > 0 and not any(k in _body[:_split] for k in
                                 ("<h2>允许访问的本机文件夹",
                                  "<h2>网络出口")),
          "右栏卡片混进了左栏")
    check("网格用 auto-fit 自适应换列",
          "repeat(auto-fit, minmax(" in src_pn
          and "grid-template-columns: 2fr 1fr 1fr" not in src_pn,
          "仍是固定列宽，窄栏会挤")
    check("按钮不折行",
          "white-space: nowrap" in src_pn)
    check("列多的表格横向滚动而非挤压",
          ".tablewrap" in src_pn and "overflow-x: auto" in src_pn
          and "td.ops" in src_pn)
    check("授权列表瞬时失败自动重试",
          "GRANT_RETRY" in src_pn and "正在连接 Claude Science" in src_pn)

    # 10) 编辑按钮修复：裸 fetch 不带 x-csx-admin → 403 → 表单"点了没反应"
    _js = src_pn.split("<script>", 1)[-1]
    _fetch_lines = [ln.strip() for ln in _js.splitlines()
                    if "fetch(" in ln and "async function api" not in ln]
    check("面板只有 api() 内部用裸 fetch",
          len(_fetch_lines) == 1
          and "const r = await fetch(path, opt)" in _fetch_lines[0],
          f"仍有裸 fetch: {_fetch_lines}")
    check("editProvider 走 api() 并处理失败",
          "async function editProvider" in src_pn
          and "api('/__admin/providers')" in src_pn
          and "(d.providers || [])" in src_pn,
          "编辑按钮可能又变回静默失败")
    check("编辑态有明确视觉提示",
          "正在编辑「" in src_pn and "✎ 编辑：" in src_pn)
    check("保存后回显 key_masked 让用户确认写入",
          "r.key_masked" in src_pn and "当前 Key=" in src_pn)

    # 代理无配置时的容错：新电脑第一次启动必须能打开控制台
    src_pr = Path(proxy_mod.__file__).read_text(encoding="utf-8")
    check("代理无配置也能启动（新电脑可开控制台加配置）",
          "仍启动控制台" in src_pr and "无配置" in src_pr)
    check("首次运行自动创建默认 config.json",
          "def ensure_config" in
          Path(C.__file__).read_text(encoding="utf-8")
          and "C.ensure_config()" in src_pr)
    check("无 provider 时推理请求返回 503（mode() 有默认值不能当判断）",
          "if state.current() is None:" in src_sv
          and "503" in src_sv
          and "state.mode() is None" not in src_sv)

    # 12) 环境落盘位置（v1.5.0）—— 新电脑装 Science 默认占C 盘 10 GB
    src_cli = Path(cli_mod.__file__).read_text(encoding="utf-8")
    check("提供 where 命令诊断安装盘",
          "def cmd_where(" in src_cli
          and '"where": cmd_where' in src_cli)
    check("where 说明根因是 --data-dir 默认 ~/.claude-science",
          "~/.claude-science" in src_cli and "USERPROFILE" in src_cli)
    check("where 明确劝阻改系统级 USERPROFILE",
          "不要**改系统级USERPROFILE" in src_cli
          or "不要**改系统级 USERPROFILE" in src_cli,
          "改系统级会搬走文档/桌面/应用数据")
    check("真实登录目录只读不遍历（护栏对象）",
          "真实登录目录是护栏对象" in src_cli)
    check("目录体积统计有预算上限（14 万文件会卡死）",
          "budget" in src_cli and "已抽样" in src_cli)
    launcher = ROOT / "便携启动.bat"
    launcher_raw = launcher.read_bytes() if launcher.is_file() else b""
    launcher_src = launcher_raw.decode("utf-8", errors="replace")
    check("Windows 启动脚本存在，且为 CRLF + 纯 ASCII",
          launcher.is_file()
          and launcher_raw.isascii()
          and b"\n" not in launcher_raw.replace(b"\r\n", b""),
          "LF 换行或中文注释会让 cmd 执行到错位字节（如卡在 time 提示）")
    check("启动脚本内置 CSSWITCH_HOME 指向 D 盘数据目录",
          "CSSWITCH_HOME=D:\\CSSwitch-Win-Data" in launcher_src)
    check("启动脚本用 if not defined 守卫，不覆盖用户已有设置",
          "if not defined CSSWITCH_HOME" in launcher_src)
    check("提供 check_science_location.py 诊断脚本",
          (ROOT / "tools" / "check_science_location.py").is_file())
    check("README 写明不能用剪切（conda 有绝对路径依赖）",
          "别剪切" in (ROOT / "README.md").read_text(encoding="utf-8")
          or "别直接剪切" in (ROOT / "README.md").read_text(encoding="utf-8")
          or "而不是剪切" in
          (ROOT / "README.md").read_text(encoding="utf-8"))

    # 11) 自动跳过 Sign in（v1.4.2）—— 用户报"点击打开没有跳过认证"
    #     根因：nonce 链接落地页只是需要【点击】的表单，window.open 停在
    #     "Sign in"。解法是代理侧中转：服务端预登录 → Set-Cookie → 302。
    check("面板 openScience 走中转页而非直开 nonce 链接",
          "/__admin/science-open?token=" in src_pn
          and "d.url + '#autologin'" not in src_pn,
          "仍可能停在 Sign in 页")
    check("已移除失效的 #autologin 锚点",
          "#autologin" not in src_pn,
          "跨域页面收不到面板注入的锚点逻辑")
    src_sv = Path(SRV.__file__).read_text(encoding="utf-8")
    check("存在中转路由 /__admin/science-open",
          '"/__admin/science-open"' in src_sv)
    check("中转页下发 Set-Cookie 并 302 到 daemon",
          "Set-Cookie" in src_sv and "_redirect(" in src_sv
          and "H.browser_login()" in src_sv)
    check("中转页支持 ?token= 鉴权（新标签页带不了请求头）",
          "_admin_ok_query" in src_sv
          and "hmac.compare_digest(got, token)" in src_sv)
    check("cookie 域不匹配时先跳 localhost 再登录",
          'host not in ("localhost",)' in src_sv,
          "从 127.0.0.1 访问面板时 cookie 会被浏览器拒收")
    src_hg = Path(HG.__file__).read_text(encoding="utf-8")
    check("hostgrant 提供 browser_login()",
          "def browser_login(" in src_hg)
    check("operon_csrf 不加 HttpOnly（前端要读它填 x-operon-csrf）",
          'if name != "operon_csrf"' in src_hg
          and "flags += \"; HttpOnly\"" in src_hg,
          "加了 HttpOnly 会让前端所有写操作 403")
    check("operon_auth 仍带 HttpOnly",
          "HttpOnly" in src_hg)
    check("未登录时 Science 仍 401（没有放松安全边界）",
          "Set-Cookie" not in src_hg.split("def browser_login")[0],
          "bypass 逻辑不应写在登录流程之前")
    # 弹窗拦截：await 之后再 window.open 会脱离用户手势而被拦，
    # 必须先同步开空白标签页占位
    _open_fn = src_pn.split("async function openScience")[1].split("\nasync ")[0]
    check("openScience 先同步开空白页占位保住用户手势",
          "window.open('', '_blank')" in _open_fn
          and _open_fn.index("window.open('', '_blank')")
          < _open_fn.index("await withBusy"),
          "await 之后才 window.open 会被弹窗拦截")
    check("占位页有加载提示而不是空白",
          "正在自动登录 Claude Science" in _open_fn)
    check("用 location.replace 导航到中转页",
          "_openWin.location.replace(" in _open_fn)


def test_cross_platform():
    section("macOS / Linux 跨平台适配")
    mac = [str(p) for p in S._candidate_paths("darwin")]
    linux = [str(p) for p in S._candidate_paths("linux")]
    check("macOS 发现应用包与 Unix 二进制",
          any("Claude Science.app" in p for p in mac)
          and any(p.name == "claude-science" and ".local" in p.parts
                  for p in S._candidate_paths("darwin"))
          and not any(p.endswith(".exe") for p in mac))
    check("Linux 发现常见安装路径且不选择 Windows exe",
          any(p.name == "claude-science" and ".local" in p.parts
              for p in S._candidate_paths("linux"))
          and Path("/usr/bin/claude-science") in S._candidate_paths("linux")
          and not any(p.endswith(".exe") for p in linux))
    check("Unix 后台进程使用独立会话",
          S._process_options(detached=True, os_name="posix")
          == {"start_new_session": True})
    check("Unix 普通子进程不传 creationflags",
          S._process_options(os_name="posix") == {})
    check("Windows 进程参数保持兼容",
          S._process_options(os_name="nt").get("creationflags")
          == S.CREATE_NO_WINDOW
          and S._process_options(detached=True, os_name="nt").get("creationflags")
          == S._SPAWN_FLAGS)

    old_home = os.environ.get("CSSWITCH_HOME")
    try:
        with tempfile.TemporaryDirectory(prefix="csx-unix-env-") as td:
            os.environ["CSSWITCH_HOME"] = td
            env = S.sandbox_env(18991, "test-secret", os_name="posix")
            home = C.sandbox_home()
            check("Unix 沙箱隔离 HOME / XDG / TMPDIR",
                  env["HOME"] == str(home)
                  and env["XDG_CONFIG_HOME"] == str(home / ".config")
                  and env["XDG_DATA_HOME"] == str(home / ".local" / "share")
                  and env["TMPDIR"] == str(home / "tmp"))
            check("Unix 沙箱仍注入本地推理代理",
                  env["ANTHROPIC_BASE_URL"]
                  == "http://127.0.0.1:18991/test-secret")
    finally:
        if old_home is None:
            os.environ.pop("CSSWITCH_HOME", None)
        else:
            os.environ["CSSWITCH_HOME"] = old_home

    raw = (ROOT / "启动.sh").read_bytes()
    check("Unix 启动器使用 shebang 与 LF",
          raw.startswith(b"#!/bin/sh\n") and b"\r\n" not in raw)
    check("README 包含 macOS / Linux 安装与依赖说明",
          "## macOS / Linux 安装与快速开始" in
          (ROOT / "README.md").read_text(encoding="utf-8"))


def main():
    print("CSswitch 离线自测")
    print("=" * 60)
    test_hkdf()
    test_token_crypto()
    test_virtual_login()
    test_model_and_translation()
    test_proxy_chain()
    test_cli()
    test_cross_platform()
    test_regressions_from_real_machine()
    print()
    print("=" * 60)
    print(f"结果: {PASS} 通过, {FAIL} 失败")
    if FAILED:
        print("失败项:")
        for f in FAILED:
            print(f"  - {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
