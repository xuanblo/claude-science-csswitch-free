# -*- coding: utf-8 -*-
"""Anthropic ⇄ OpenAI 协议翻译（纯函数，无 IO、无全局状态）。

只在 mode="openai" 的上游上启用。Anthropic 兼容端点走原生透传，不经本模块。

拆成独立模块的理由：这是全项目业务价值最高、也最容易写单元测试的一段。
原先它藏在 963 行的 proxy.py 里，只能靠整链自测间接覆盖。
"""
import json

from . import state
from .modelmap import clamp_max_tokens, resolve_model


def map_tool_choice(tc, tools):
    """Anthropic tool_choice -> OpenAI tool_choice。

    Anthropic: {"type": "auto"|"any"|"tool"|"none", "name": ...}
    OpenAI   : "auto" | "none" | "required" | {"type":"function","function":{"name":...}}
    """
    if not isinstance(tc, dict):
        return None
    t = tc.get("type")
    if t == "auto":
        return "auto"
    if t == "none":
        return "none"
    if t == "tool" and tc.get("name"):
        return {"type": "function", "function": {"name": tc["name"]}}
    if t == "any":
        names = [x["name"] for x in (tools or []) if x.get("name")]
        if len(names) == 1:
            # 单工具时指名，比 required 兼容性更好（部分上游不接受 required）
            return {"type": "function", "function": {"name": names[0]}}
        return "required"
    return None


def _content_blocks_to_openai(content):
    """单条消息的 content 块 -> (text, tool_calls, tool_results)。"""
    text_parts, tool_calls, tool_results = [], [], []
    for blk in content or []:
        if not isinstance(blk, dict):
            continue
        t = blk.get("type")
        if t == "text":
            text_parts.append(blk.get("text", ""))
        elif t == "tool_use":
            tool_calls.append({
                "id": blk.get("id"), "type": "function",
                "function": {"name": blk.get("name"),
                             "arguments": json.dumps(blk.get("input", {}),
                                                     ensure_ascii=False)},
            })
        elif t == "tool_result":
            c = blk.get("content")
            if isinstance(c, list):
                c = "".join(x.get("text", "") for x in c if isinstance(x, dict))
            tool_results.append({
                "role": "tool", "tool_call_id": blk.get("tool_use_id"),
                "content": c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)})
    return text_parts, tool_calls, tool_results


def anthropic_to_openai(req):
    """Anthropic Messages 请求 -> OpenAI Chat Completions 请求。"""
    msgs = []

    sys_prompt = req.get("system")
    if isinstance(sys_prompt, list):
        sys_prompt = "\n".join(b.get("text", "") for b in sys_prompt
                               if isinstance(b, dict))
    if sys_prompt:
        msgs.append({"role": "system", "content": sys_prompt})

    for m in req.get("messages", []):
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue
        text_parts, tool_calls, tool_results = _content_blocks_to_openai(content)
        if role == "assistant" and tool_calls:
            msgs.append({"role": "assistant",
                         "content": "".join(text_parts) or None,
                         "tool_calls": tool_calls})
        elif tool_results:
            msgs.extend(tool_results)
            if text_parts:
                msgs.append({"role": role, "content": "".join(text_parts)})
        else:
            msgs.append({"role": role, "content": "".join(text_parts)})

    out = {"model": resolve_model(req.get("model")),
           "messages": msgs,
           "stream": False}   # 流式由 SSE 回放实现（见 server._replay_as_sse）
    if req.get("max_tokens"):
        out["max_tokens"] = clamp_max_tokens(req["max_tokens"], out["model"])
    if req.get("temperature") is not None:
        out["temperature"] = req["temperature"]
    if req.get("tools"):
        out["tools"] = [{"type": "function",
                         "function": {"name": t["name"],
                                      "description": t.get("description", ""),
                                      "parameters": t.get("input_schema", {})}}
                        for t in req["tools"] if t.get("name")]
    tcm = map_tool_choice(req.get("tool_choice"), req.get("tools"))
    if tcm is not None:
        out["tool_choice"] = tcm
    if req.get("stop_sequences"):
        out["stop"] = req["stop_sequences"]
    if req.get("top_p") is not None:
        out["top_p"] = req["top_p"]
    return out


_FINISH_REASON = {"stop": "end_turn", "length": "max_tokens",
                  "tool_calls": "tool_use"}


def openai_to_anthropic(resp, model_id):
    """OpenAI Chat Completions 响应 -> Anthropic Messages 响应。"""
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message", {})
    blocks = []
    if msg.get("content"):
        blocks.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (ValueError, TypeError):
            args = {}
        blocks.append({"type": "tool_use", "id": tc.get("id"),
                       "name": fn.get("name"), "input": args})
    fr = choice.get("finish_reason")
    stop = _FINISH_REASON.get(fr, "end_turn")
    usage = resp.get("usage") or {}
    return {
        "id": resp.get("id", "msg_proxy"), "type": "message", "role": "assistant",
        "model": model_id,
        "content": blocks or [{"type": "text", "text": ""}],
        "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens") or 0,
                  "output_tokens": usage.get("completion_tokens") or 0},
    }


def load_provider(name=None):
    """（兼容旧调用路径）装载 provider —— 实际实现见 upstream 模块。"""
    from . import upstream
    return upstream.load_provider(name)


__all__ = ["anthropic_to_openai", "openai_to_anthropic", "map_tool_choice",
           "load_provider", "state"]
