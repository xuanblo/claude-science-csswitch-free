# -*- coding: utf-8 -*-
"""CSswitch 本地代理：把 Claude Science 的推理请求转发到当前第三方 Provider。

监听 127.0.0.1（仅回环）。Science 经 ANTHROPIC_BASE_URL=http://127.0.0.1:<port>/<secret>
打入请求，代理：
  1. 剥离入站 Authorization / x-api-key（Science 的虚拟 OAuth，绝不外泄）；
  2. 按当前 Provider 注入上游鉴权头（Key 只驻内存，不落日志）；
  3. Anthropic 兼容端点 → 原生透传（流式 SSE 字节级保真）；
     OpenAI 兼容端点 → Anthropic<->OpenAI 双向协议翻译（流式以 SSE 回放保真 tool_use）；
  4. GET /v1/models 供 Science 模型选择器铺列表（壳 id 平铺 + 真实名显示）；
  5. CONNECT 隧道：Anthropic 域名 401 fast-fail（防 "Switching organization" 卡死），
     其余目标需在白名单内（见 constants.TUNNEL_ALLOWLIST_ENV）；
  6. 管理 API（/__admin/*，admin token 鉴权）：热切换当前 Provider，Science 无需重启。

模块划分（v1.1.0 起，从原 963 行单文件拆出）：
  state.py     运行态（LOCK / PROV / KEY / secret / token）
  constants.py 全部硬编码常量（端口、超时、壳 id、请求体上限、隧道策略）
  modelmap.py  模型名解析、thinking/max_tokens 归一化、/v1/models 装配
  translate.py Anthropic <-> OpenAI 协议翻译（纯函数）
  upstream.py  上游 HTTP 客户端、重试、鉴权头、Key 校验
  server.py    class H（HTTP 编解码层）
  proxy.py     本文件：启动编排 + 兼容 re-export

独立运行（调试用）：
  python -m csswitch.proxy --port 18993 [--config <csswitch_home 目录>]
  （--config 指向包含 config.json 的目录，默认 ~/.csswitch-win）
"""
import argparse
import os
import secrets
import sys
import time
from pathlib import Path

from . import __version__
from . import constants as K
from . import logging_setup as L
from . import state

# --- 兼容 re-export ---
# v1.0.0 把这些实现放在本模块内，外部脚本与 tests/selftest.py 依赖
# `from csswitch.proxy import ...`。保留导出以免破坏既有调用路径。
from .modelmap import (  # noqa: E402,F401
    build_models_response,
    clamp_max_tokens,
    normalize_thinking,
    resolve_model,
)
from .state import (  # noqa: E402,F401
    ADMIN_TOKEN,
    AUTH_SECRET,
    KEY,
    LOCK,
    LOG_PATH,
    PROV,
    PROV_NAME,
    START_TS,
    UPSTREAM_MODELS,
)
from .translate import (  # noqa: E402,F401
    anthropic_to_openai,
    map_tool_choice,
    openai_to_anthropic,
)
from .upstream import (  # noqa: E402,F401
    fetch_relay_models,
    http_get_json,
    http_post,
    load_provider,
    open_stream,
    verify_provider,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from csswitch import config as C  # noqa: E402

log = L.get_logger("proxy")

__all__ = [
    "main", "load_provider", "verify_provider", "resolve_model",
    "normalize_thinking", "clamp_max_tokens", "build_models_response",
    "anthropic_to_openai", "openai_to_anthropic", "map_tool_choice",
    "http_post", "open_stream", "http_get_json", "fetch_relay_models",
    "PROV", "PROV_NAME", "KEY", "LOCK", "AUTH_SECRET", "ADMIN_TOKEN",
    "LOG_PATH", "UPSTREAM_MODELS", "START_TS",
]

# 注意：上面 re-export 的 PROV / KEY / ... 是「导入时刻的值拷贝」。
# 运行态的正确入口是 csswitch.state；这些名字仅为兼容旧调用路径而存在，
# 新代码请直接 `from . import state` 并用 state.snapshot() 读取。


def _write_state(port):
    """把本次运行的 secret/token/pid 落 state.json，供 CLI 读取。"""
    st = C.load_state()
    st.update({
        "proxy_pid": os.getpid(),
        "proxy_port": port,
        "auth_secret": state.AUTH_SECRET,
        "admin_token": state.ADMIN_TOKEN,
        "proxy_started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    C.save_state(st)


def _clear_proxy_env():
    """剥掉系统代理变量。

    本进程就是代理：上游直连，绝不能让 HTTPS_PROXY 把上游请求绕回自己。
    与 sandbox.sandbox_env 采取同样策略（那边同样必须清理），
    清单统一收敛到 constants.PROXY_ENV_VARS，避免两处漂移。
    """
    for k in K.PROXY_ENV_VARS:
        os.environ.pop(k, None)


def main(argv=None):
    ap = argparse.ArgumentParser(description="CSswitch-Win 本地代理")
    ap.add_argument("--config", default=None,
                    help="config.json 所在目录（默认 ~/.csswitch-win）")
    ap.add_argument("--port", type=int, default=None,
                    help="监听端口（默认取配置 settings.proxy_port = "
                         f"{K.DEFAULT_PROXY_PORT}）")
    ap.add_argument("--provider", default=None,
                    help="启动时装载的 provider 名（默认 current）")
    ap.add_argument("--log", default=None)
    args = ap.parse_args(argv)

    _clear_proxy_env()
    if args.config:
        os.environ["CSSWITCH_HOME"] = str(Path(args.config).expanduser().resolve())

    # 端口单一真相源：配置优先，其次内置默认（原先硬编码 18991，会与配置漂移）
    if args.port is None:
        args.port = (C.load_config()["settings"].get("proxy_port")
                     or K.DEFAULT_PROXY_PORT)

    C.logs_dir().mkdir(parents=True, exist_ok=True)
    # 新电脑 / 空数据目录：先把默认 config.json 建出来，后续面板才有东西可写
    C.ensure_config()
    state.LOG_PATH = args.log or str(C.logs_dir() / "proxy.log")
    state.AUTH_SECRET = secrets.token_urlsafe(16)
    state.ADMIN_TOKEN = secrets.token_urlsafe(24)

    ok, err = load_provider(args.provider)
    if not ok:
        # 无配置也启动：新电脑 / 空数据目录时，控制台需要代理才能打开并
        # 添加第一条配置。推理请求由 server 返回 503（未加载 provider），
        # 不会静默落到错误上游。
        log.warning("代理以「无配置」模式启动：%s", err)
        print(f"[csswitch] 提示：{err}；仍启动控制台，请在面板里添加配置。",
              file=sys.stderr)

    from . import server
    try:
        srv = server.serve(args.port)
    except RuntimeError as e:
        print(f"[csswitch] {e}", file=sys.stderr, flush=True)
        return 2

    _write_state(args.port)
    prov, pname = state.snapshot()
    prov = prov or {}
    log.info("CSswitch-Win %s 代理启动  127.0.0.1:%d  provider=%s  mode=%s  "
             "key=已加载(未显示)  上游=%s",
             __version__, args.port, pname, prov.get("mode"),
             prov.get("base_url"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("代理收到中断，退出")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
