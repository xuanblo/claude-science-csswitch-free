# -*- coding: utf-8 -*-
"""上游 HTTP 客户端：连接、重试、鉴权头注入、Key 校验。

三条纪律：
  1. **Key 只驻内存**：从 config.json 读入 state.KEY，绝不进日志、
     绝不进命令行、绝不进异常消息（异常消息会落盘）；
  2. **只对传输层错误重试**：服务端明确响应（HTTPError，如 400/401）
     立即上抛，重试只会浪费配额并掩盖真实错误；
  3. **本进程不得被系统代理变量绕回来**：main() 会 pop 掉全部代理变量，
     否则上游请求会经由 sandbox_env 注入的 HTTPS_PROXY 打回自己。
"""
import json
import urllib.error
import urllib.request

from . import constants as K
from . import logging_setup as L
from . import state

log = L.get_logger("proxy")


def _scrub_detail(detail: str) -> str:
    """截断并脱敏上游错误详情，避免把 Key 之类的内容写进日志。"""
    return L._scrub((detail or "")[:400])


def upstream_urls():
    """返回 (messages_url, models_url, mode)。"""
    prov, _ = state.snapshot()
    prov = prov or {}
    base = (prov.get("base_url") or "").strip().rstrip("/")
    mode = prov.get("mode", "anthropic")
    if mode == "openai":
        return base + "/chat/completions", base + "/models", "openai"
    return base + "/v1/messages", base + "/models", "anthropic"


def _messages_url_for(rec) -> str:
    base = (rec.get("base_url") or "").rstrip("/")
    if rec.get("mode", "anthropic") == "openai":
        return base + "/chat/completions"
    return base + "/v1/messages"


def auth_headers(mode, prov=None, key=None):
    """按 provider 的 auth_style 构造上游鉴权头。"""
    if prov is None:
        prov, _, k = state.snapshot_with_key()
        key = k
    prov = prov or {}
    style = prov.get("auth_style", "x-api-key")
    h = {"User-Agent": K.UPSTREAM_UA}
    if style in ("x-api-key", "both"):
        h["x-api-key"] = key
    if style in ("bearer", "both"):
        h["Authorization"] = f"Bearer {key}"
    if mode == "anthropic":
        h["anthropic-version"] = "2023-06-01"
    return h


def _with_ua(headers):
    return {"User-Agent": K.UPSTREAM_UA, **(headers or {})}


def http_post(url, data, headers, attempts=None, timeout=None):
    """POST 上游并完整读体。连接与读体抖动重试；HTTPError 不重试。"""
    attempts = attempts or K.RETRY_ATTEMPTS
    timeout = timeout or K.UPSTREAM_TIMEOUT
    headers = _with_ua(headers)
    last = None
    for i in range(attempts):
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError:
            raise                      # 服务端明确响应，重试无意义
        except Exception as e:         # noqa: BLE001 - 传输层异常统一重试
            last = e
            if i < attempts - 1:
                log.debug("上游连接抖动，重试 %d/%d: %s", i + 1, attempts - 1, e)
                _sleep_backoff(i)
    raise last


def open_stream(url, data, headers, attempts=None, timeout=None):
    """打开上游流式连接并预读首块。

    预读是为了把「200 但立刻空体」纳入重试；首字节到手后不再重试。
    返回 (resp, first_chunk, content_type)。
    """
    attempts = attempts or K.RETRY_ATTEMPTS
    timeout = timeout or K.UPSTREAM_TIMEOUT
    headers = _with_ua(headers)
    last = None
    for i in range(attempts):
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            r = urllib.request.urlopen(req, timeout=timeout)
            first = r.read(4096)
            if not first:
                r.close()
                raise ConnectionError("上游 200 但立刻空体")
            return r, first, r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError:
            raise
        except Exception as e:         # noqa: BLE001
            last = e
            if i < attempts - 1:
                log.debug("上游流连接抖动，重试 %d/%d: %s", i + 1, attempts - 1, e)
                _sleep_backoff(i)
    raise last


def http_get_json(url, headers, attempts=None, timeout=30):
    attempts = attempts or K.MODELS_RETRY_ATTEMPTS
    headers = _with_ua(headers)
    last = None
    for i in range(attempts):
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError:
            raise
        except Exception as e:         # noqa: BLE001
            last = e
            if i < attempts - 1:
                _sleep_backoff(i, base=0.6)
    raise last


def _sleep_backoff(i, base=None):
    import time
    time.sleep((base or K.RETRY_BACKOFF_BASE) * (i + 1))


def load_provider(name=None):
    """从 config.json 装载 provider 到运行态。返回 (ok, err)。"""
    from . import config as C
    cfg = C.load_config()
    rec = C.find_provider(cfg, name) if name else C.current_provider(cfg)
    if rec is None:
        with state.LOCK:
            state.clear_provider()
        return False, f"配置不存在或未设置当前项: {name}"
    if not rec.get("key"):
        return False, f"配置 {rec['name']} 缺少 API Key"
    if not rec.get("base_url"):
        return False, f"配置 {rec['name']} 缺少 base_url"
    with state.LOCK:
        state.set_provider(rec, rec["key"])
    return True, None


def _verify_body(rec):
    """Key 校验用的最小请求体。

    两种协议的 body 其实同构，但显式区分以免后续任一侧演进时静默失配。
    """
    model = rec.get("model") or (rec.get("models") or [None])[0]
    base = {"model": model, "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}]}
    if rec.get("mode", "anthropic") == "openai":
        # OpenAI 侧显式要求非流式，否则部分上游会回 SSE 导致解析失败
        base["stream"] = False
    return base


def verify_provider(name) -> dict:
    """发一条最小请求验证 Key。返回 {"ok","status","detail"}。"""
    from . import config as C
    cfg = C.load_config()
    rec = C.find_provider(cfg, name)
    if rec is None:
        return {"ok": False, "status": None, "detail": f"配置不存在: {name}"}
    if not rec.get("key"):
        return {"ok": False, "status": None, "detail": "缺少 API Key"}
    url = _messages_url_for(rec)
    h = auth_headers(rec.get("mode", "anthropic"), prov=rec, key=rec["key"])
    h["Content-Type"] = "application/json"
    body = _verify_body(rec)
    try:
        with urllib.request.urlopen(
                urllib.request.Request(
                    url, data=json.dumps(body).encode(), headers=h),
                timeout=K.VERIFY_TIMEOUT) as r:
            return {"ok": True, "status": r.status, "detail": "key 有效"}
    except urllib.error.HTTPError as e:
        detail = _scrub_detail(e.read().decode("utf-8", "replace"))
        if e.code in (401, 403):
            return {"ok": False, "status": e.code, "detail": f"Key 无效: {detail}"}
        if e.code == 429:
            return {"ok": True, "status": 429, "detail": f"Key 有效但被限流: {detail}"}
        # 其它状态码说明 Key 已送达上游（如 400 参数问题），换过去再说
        return {"ok": True, "status": e.code,
                "detail": f"上游返回 {e.code}（key 已送达）: {detail}"}
    except Exception as e:             # noqa: BLE001
        return {"ok": False, "status": None, "detail": f"网络不可达: {e}"}


def fetch_relay_models():
    """回源拉上游 /models，刷新缓存。返回归一化 list。"""
    _, models_url, mode = upstream_urls()
    if not models_url:
        return []
    raw = http_get_json(models_url, auth_headers(mode))
    data = raw.get("data") if isinstance(raw, dict) else raw
    out, ids = [], []
    for m in data or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or m.get("name")
        if not mid:
            continue
        ids.append(mid)
        out.append({"type": "model", "id": mid,
                    "display_name": m.get("display_name") or mid,
                    "supports_tools": None,
                    "created_at": K.FAKE_CREATED_AT})
    if ids:
        with state.LOCK:
            state.set_upstream_models(ids)
    return out
