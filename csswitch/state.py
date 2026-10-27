# -*- coding: utf-8 -*-
"""代理运行态：跨模块共享的可变全局状态。

原先这些变量散落在 proxy.py 的模块级命名空间，被拆出的 modelmap /
upstream / server 三个模块都要读写。集中到本模块后：

  - 状态只有一份，且 `from . import state` 后读到的是同一个对象；
  - 所有读写都在 `LOCK` 保护下进行，热切换不会读到撕裂的中间态；
  - 需要在测试中替换状态时，setattr(state, "PROV", rec) 即可。

约定：外部模块读状态走 `state.snapshot()` / `state.current()`，
不要直接 `from .state import PROV`（那是值拷贝，会与运行态脱钩）。
"""
import threading
import time

LOCK = threading.Lock()

PROV = None           # 当前 provider 记录（dict）；None = 未装载
PROV_NAME = None      # 当前 provider 名（用于日志与 /health）
KEY = None            # 当前上游 API Key（只驻内存，绝不入日志）
AUTH_SECRET = None    # path secret（ANTHROPIC_BASE_URL 的路径段）
ADMIN_TOKEN = None    # 管理 API token
UPSTREAM_MODELS = []  # fetch_models 类上游回源得到的模型 id 缓存
LOG_PATH = None       # 文件日志路径；None = 只打 stdout
START_TS = time.time()


def set_provider(rec, key):
    """原子装载 provider + Key，并清空回源模型缓存。调用方须已持有 LOCK。"""
    global PROV, PROV_NAME, KEY, UPSTREAM_MODELS
    PROV, PROV_NAME, KEY = dict(rec), rec["name"], key
    UPSTREAM_MODELS = []


def clear_provider():
    """清空 provider 与 Key（装载失败时用）。调用方须已持有 LOCK。"""
    global PROV, PROV_NAME, KEY
    PROV, PROV_NAME, KEY = None, None, None


def set_upstream_models(ids):
    """写回源模型缓存。调用方须已持有 LOCK。"""
    global UPSTREAM_MODELS
    UPSTREAM_MODELS = list(ids or [])


def snapshot():
    """取一致性快照：(provider_dict_or_None, provider_name_or_None)。

    provider dict 返回副本，调用方随便改都不会污染运行态。
    """
    with LOCK:
        return (dict(PROV) if PROV else None), PROV_NAME


def snapshot_with_key():
    """取 (provider_dict_or_None, name_or_None, key_or_None)。"""
    with LOCK:
        return (dict(PROV) if PROV else None), PROV_NAME, KEY


def current():
    """取当前 provider 名。"""
    with LOCK:
        return PROV_NAME


def mode(default="anthropic"):
    """取当前 provider 的协议模式。"""
    with LOCK:
        return (PROV or {}).get("mode", default)


def auth_secret():
    with LOCK:
        return AUTH_SECRET


def admin_token():
    with LOCK:
        return ADMIN_TOKEN


def upstream_models():
    with LOCK:
        return list(UPSTREAM_MODELS)


def uptime_s() -> int:
    return int(time.time() - START_TS)
