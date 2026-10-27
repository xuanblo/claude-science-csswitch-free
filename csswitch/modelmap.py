# -*- coding: utf-8 -*-
"""模型名解析、thinking/max_tokens 归一化、/v1/models 装配。

Science 的模型选择器只认 `claude-{opus|sonnet|haiku}-<版本>` 这类壳 id，
第三方模型无法直接显示。做法是把真实模型「平铺」到壳 id 上：选择器里显示
真实模型名（display_name），出站时再映射回上游真实 id（resolve_model）。

壳 id 顺序即语义：SHELL_IDS[0] 是最强档，[2] 是轻量档。
"""
import re

from . import constants as K
from . import logging_setup as L
from . import state
from . import upstream

log = L.get_logger("proxy")

_DATE_SUFFIX = re.compile(r"-\d{8}$")


def _shell_index(name):
    try:
        return K.SHELL_IDS.index(name)
    except ValueError:
        return None


def resolve_model(name):
    """Science 传来的模型名 -> 当前 provider 的真实上游模型 id。

    优先级：force_model > 壳 id 位置映射 > 显式 model_map >
            已是真实 id > 去日期后缀 > 前缀匹配 > 回源贴合 > 默认。
    位置映射优先于 model_map，因为它是用户在选择器里的主动选择。
    """
    prov, _ = state.snapshot()
    if prov is None:
        return name or K.SHELL_IDS[0]
    if prov.get("force_model"):
        return prov["force_model"]

    models = prov.get("models") or []
    if not name:
        return prov.get("model") or (models or [prov.get("default_model")])[0]

    # 1) 选择器壳 id：按位置取用户真实选中的模型
    idx = _shell_index(name)
    if idx is not None and 0 <= idx < len(models):
        return models[idx]

    # 2) 显式映射（覆盖 Science 硬编码的 claude-*）
    mm = prov.get("model_map") or {}
    if name in mm:
        return mm[name]

    # 3) 已是真实 id
    if name in models:
        return name

    # 4) 去日期后缀再试（Science 会给壳 id 附加快照日期）
    stripped = _DATE_SUFFIX.sub("", name)
    if stripped in mm:
        return mm[stripped]
    if stripped in models:
        return stripped

    # 5) 前缀匹配
    for k, v in mm.items():
        if name.startswith(k) or stripped.startswith(k):
            return v

    # 6) 回源贴合（fetch_models 类上游）
    if prov.get("passthrough"):
        upstream_models = state.upstream_models()
        if upstream_models:
            if name in upstream_models:
                return name
            for mid in upstream_models:
                if mid.startswith(name + "-"):
                    return mid

    # 7) 默认
    return prov.get("model") or (models or [prov.get("default_model")])[0]


def _enabled_budget(max_tokens):
    default = 1024
    if isinstance(max_tokens, int) and max_tokens > 0:
        return max(1, min(default, max_tokens - 1))
    return default


def normalize_thinking(body):
    """按上游策略归一化 thinking 字段（原地修改并返回 body）。"""
    prov, _ = state.snapshot()
    if prov is None:
        return body
    tc = body.get("tool_choice")
    forcing = isinstance(tc, dict) and tc.get("type") in ("any", "tool")
    # (A) 强制工具时，部分上游不允许 thinking -> disabled
    if forcing and prov.get("force_tool_disables_thinking"):
        body["thinking"] = {"type": "disabled"}
        return body
    # (B) 模板策略：enabled（Kimi 类强制）/ adaptive（默认）
    policy = prov.get("thinking")
    if policy == "enabled":
        th = body.get("thinking")
        if not (isinstance(th, dict) and th.get("type") == "enabled"):
            body["thinking"] = {"type": "enabled",
                                "budget_tokens": _enabled_budget(body.get("max_tokens"))}
        return body
    # (C) auto -> adaptive
    th = body.get("thinking")
    if isinstance(th, dict) and th.get("type") == "auto":
        th = dict(th)
        th["type"] = "adaptive"
        body["thinking"] = th
    return body


def clamp_max_tokens(v, model):
    """把 max_tokens 夹到该模型的输出上限，防止上游拒收。"""
    if not v:
        return v
    prov, _ = state.snapshot()
    prov = prov or {}
    caps = prov.get("model_caps") or {}
    cap = caps.get(model, prov.get("default_cap"))
    if cap:
        return min(int(v), int(cap))
    return v


def _model_entry(model_id, display):
    return {"type": "model", "id": model_id, "display_name": display,
            "supports_tools": None, "created_at": K.FAKE_CREATED_AT}


def _list_response(data):
    return {"data": data, "has_more": False,
            "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None}


def build_models_response():
    """装配 /v1/models 响应。返回 (http_code, body)。"""
    prov, pname = state.snapshot()
    if prov is None:
        return 503, {"error_kind": "no_provider", "message": "代理未加载 provider"}
    if prov.get("force_model"):
        return 200, _list_response(
            [_model_entry(K.SHELL_IDS[0], prov["force_model"])])
    if prov.get("fetch_models") and prov.get("base_url"):
        import urllib.error
        try:
            data = upstream.fetch_relay_models()
            log.info("GET /v1/models -> %s（回源）: %d 个模型", pname, len(data))
            return 200, _list_response(data)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:            # noqa: BLE001 - 读取错误体失败无害
                detail = ""
            return e.code, {"error_kind": "upstream", "upstream_status": e.code,
                            "message": f"upstream {e.code}: {detail}"}
        except Exception as e:          # noqa: BLE001
            return 502, {"error_kind": "network", "upstream_status": None,
                         "message": str(e)}
    models = prov.get("models") or [prov.get("model") or prov.get("default_model")]
    return 200, _list_response(
        [_model_entry(K.SHELL_IDS[i], m) for i, m in enumerate(models[:3])])
