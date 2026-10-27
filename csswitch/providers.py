# -*- coding: utf-8 -*-
"""内置第三方 Provider 注册表。

协议模式：
  anthropic —— 上游为 Anthropic Messages 兼容端点，代理原生透传（保真 thinking/tool_use）
  openai    —— 上游为 OpenAI Chat Completions 兼容端点，代理做双向协议翻译

模型选择器平铺机制（Science 二进制逆向结论，见参考项目 verified-facts）：
  - /v1/models 返回的 id 必须以 `claude-` 开头才会显示；
  - 只有 `claude-{opus|sonnet|haiku}-<纯数字版本>` 进主列表，其余进 "More models"。
  因此第三方模型挂在 `claude-opus-4-8` / `claude-haiku-4-5` 壳 id 上平铺，
  display_name 写真实模型名，出站由 model_map 映射回上游真实 id。

内置端点均为各官方公开的兼容端点，可在配置里用 base_url 覆盖。
未知端点一律走自定义（custom-anthropic / custom-openai），不臆造 URL。
"""

# 壳 id：Science 模型选择器主列表认可的档位。
# 权威定义在 constants.SHELL_IDS（proxy/modelmap 实际生效用的就是那一份）；
# 这里只是给各 provider 的 model_map 提供默认入口参考，避免两处漂移。
from .constants import SHELL_IDS  # noqa: F401  (供 model_map 与文档对照使用)

BUILTIN_PROVIDERS = {
    # DeepSeek 原生 Anthropic 端点：无需协议转换，thinking/tool_use 原生保真
    "deepseek": {
        "display": "DeepSeek（原生 Anthropic 端点）",
        "mode": "anthropic",
        "base_url": "https://api.deepseek.com/anthropic",
        "auth_style": "x-api-key",
        "key_hint": "sk-...（DeepSeek 开放平台）",
        "thinking": "adaptive",  # Science 发 auto 时归一为 adaptive
        "default_model": "deepseek-v4-pro",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
        # 壳 id -> 真实 id；Science 硬编码的 claude-* 也一并映射
        "model_map": {
            "claude-opus-4-8": "deepseek-v4-pro",
            "claude-sonnet-5": "deepseek-v4-flash",
            "claude-sonnet-4-6": "deepseek-v4-flash",
            "claude-haiku-4-5": "deepseek-v4-flash",
        },
        # 每模型输出上限（夹取 max_tokens，防止上游拒收）
        "model_caps": {"deepseek-v4-pro": 65536, "deepseek-v4-flash": 32768},
        "default_cap": 8192,
        # 强制 tool_choice 时 DeepSeek 不允许 thinking，需置 disabled
        "force_tool_disables_thinking": True,
    },
    # 通义千问 DashScope OpenAI 兼容端点：代理做 Anthropic<->OpenAI 翻译
    "qwen": {
        "display": "通义千问 Qwen（DashScope OpenAI 兼容）",
        "mode": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "auth_style": "bearer",
        "key_hint": "sk-...（阿里云百炼/DashScope）",
        "thinking": None,
        "default_model": "qwen-max",
        "models": ["qwen-max", "qwen-plus", "qwen-turbo", "qwen-long"],
        "model_map": {
            "claude-opus-4-8": "qwen-max",
            "claude-sonnet-5": "qwen-plus",
            "claude-sonnet-4-6": "qwen-plus",
            "claude-haiku-4-5": "qwen-turbo",
        },
        "model_caps": {"qwen-max": 8192, "qwen-plus": 8192, "qwen-turbo": 8192,
                       "qwen-long": 8192},
        "default_cap": 8192,
        "force_tool_disables_thinking": False,
    },
    # 智谱 GLM：官方 Anthropic 兼容端点
    "glm": {
        "display": "智谱 GLM（Anthropic 兼容端点）",
        "mode": "anthropic",
        "base_url": "https://open.bigmodel.cn/api/anthropic",
        "auth_style": "both",  # GLM 兼容端点同时认两种头，最大化兼容
        "key_hint": "...（智谱开放平台）",
        "thinking": "adaptive",
        "default_model": "glm-4.6",
        "models": ["glm-4.6", "glm-4.5-air"],
        "model_map": {
            "claude-opus-4-8": "glm-4.6",
            "claude-sonnet-5": "glm-4.6",
            "claude-sonnet-4-6": "glm-4.5-air",
            "claude-haiku-4-5": "glm-4.5-air",
        },
        "model_caps": {},
        "default_cap": None,
        "force_tool_disables_thinking": False,
    },
    # Kimi（Moonshot）：Anthropic 兼容端点；模型要求 thinking.type=enabled
    "kimi": {
        "display": "Kimi Moonshot（Anthropic 兼容端点）",
        "mode": "anthropic",
        "base_url": "https://api.moonshot.cn/anthropic",
        "auth_style": "both",
        "key_hint": "sk-...（Moonshot 开放平台）",
        "thinking": "enabled",
        "default_model": "kimi-k2-0905-preview",
        "models": ["kimi-k2-0905-preview", "kimi-k2-turbo-preview"],
        "model_map": {
            "claude-opus-4-8": "kimi-k2-0905-preview",
            "claude-sonnet-5": "kimi-k2-0905-preview",
            "claude-sonnet-4-6": "kimi-k2-turbo-preview",
            "claude-haiku-4-5": "kimi-k2-turbo-preview",
        },
        "model_caps": {},
        "default_cap": None,
        "force_tool_disables_thinking": False,
    },
    # MiniMax：Anthropic 兼容端点；认 adaptive thinking
    "minimax": {
        "display": "MiniMax（Anthropic 兼容端点）",
        "mode": "anthropic",
        "base_url": "https://api.minimaxi.com/anthropic",
        "auth_style": "both",
        "key_hint": "...（MiniMax 开放平台）",
        "thinking": "adaptive",
        "default_model": "MiniMax-M2",
        "models": ["MiniMax-M2", "MiniMax-M2-Stable"],
        "model_map": {
            "claude-opus-4-8": "MiniMax-M2",
            "claude-sonnet-5": "MiniMax-M2",
            "claude-sonnet-4-6": "MiniMax-M2-Stable",
            "claude-haiku-4-5": "MiniMax-M2-Stable",
        },
        "model_caps": {},
        "default_cap": None,
        "force_tool_disables_thinking": False,
    },
    # OpenRouter：Anthropic 兼容端点，模型名自填（回源拉取）
    "openrouter": {
        "display": "OpenRouter（Anthropic 兼容端点）",
        "mode": "anthropic",
        "base_url": "https://openrouter.ai/api/v1",
        "auth_style": "bearer",
        "key_hint": "sk-or-v1-...",
        "thinking": "adaptive",
        "default_model": "anthropic/claude-sonnet-4.5",
        "models": [],
        "model_map": {},
        "model_caps": {},
        "default_cap": None,
        "force_tool_disables_thinking": False,
        "fetch_models": True,  # GET /models 回源铺满选择器
    },
}

# 自定义类型：用户自填 base_url + key + 模型
CUSTOM_KINDS = {
    "custom-anthropic": {
        "display": "自定义 Anthropic 兼容端点",
        "mode": "anthropic",
        "auth_style": "both",
        "thinking": "adaptive",
        "passthrough": True,   # 不重映射模型名，上游原生认什么就发什么
    },
    "custom-openai": {
        "display": "自定义 OpenAI 兼容端点",
        "mode": "openai",
        "auth_style": "bearer",
        "thinking": None,
        "passthrough": True,
    },
}


def template_for(kind: str) -> dict:
    """按类型取模板（内置 or 自定义），用于新建配置时预填。"""
    if kind in BUILTIN_PROVIDERS:
        t = dict(BUILTIN_PROVIDERS[kind])
    elif kind in CUSTOM_KINDS:
        t = dict(CUSTOM_KINDS[kind])
    else:
        raise KeyError(f"未知 provider 类型: {kind}")
    t.setdefault("models", [])
    t.setdefault("model_map", {})
    t.setdefault("model_caps", {})
    t.setdefault("default_cap", None)
    t.setdefault("force_tool_disables_thinking", False)
    t.setdefault("fetch_models", False)
    t.setdefault("passthrough", False)
    t.setdefault("mode", "anthropic")
    t.setdefault("auth_style", "x-api-key")
    return t


def all_kinds() -> list:
    """全部可选类型（供 CLI 菜单）。"""
    return list(BUILTIN_PROVIDERS.keys()) + list(CUSTOM_KINDS.keys())


def kind_display(kind: str) -> str:
    """类型的展示名；未知类型回退为原样输出。"""
    try:
        return template_for(kind)["display"]
    except KeyError:
        return kind
