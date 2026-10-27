# -*- coding: utf-8 -*-
"""全局常量：唯一真相源。

原先散落在各模块的硬编码值（端口、超时、重试次数、伪造时间戳、壳 id、
CONNECT 隧道策略、请求体上限）统一收敛到本模块，改动只需改一处。

与 config.DEFAULT_SETTINGS 的分工：
  - 本模块放「协议/运行时特性」常量（改这些等于改协议行为）；
  - config.DEFAULT_SETTINGS 放「用户可覆盖」的默认端口（运行时会读它）。
"""
from . import __version__

# ---------------- 上游标识 ----------------

UPSTREAM_UA = f"CSswitch-Win/{__version__} (+https://github.com/csswitch-win/csswitch-win)"
"""发往上游的 User-Agent。

注意：不得引用任何第三方仓库地址 —— 本项目发出的每个请求都带此 UA，
冒用他人项目会造成溯源混淆与合规问题。
"""

# ---------------- 网络与重试 ----------------

DEFAULT_PROXY_PORT = 18991   # 本地代理默认端口（回环）
SANDBOX_PORT = 8990          # 沙箱 Science 默认端口（避开官方 8000）
REAL_DEFAULT_PORT = 8000     # 官方实例默认端口，禁止作为沙箱端口

UPSTREAM_TIMEOUT = 300       # 上游请求超时（秒）；长上下文推理需要较大余量
RETRY_ATTEMPTS = 4           # 连接抖动重试次数（仅对传输层错误生效）
RETRY_BACKOFF_BASE = 0.8     # 重试退避基数（秒），第 i 次等待 BASE * (i+1)
MODELS_RETRY_ATTEMPTS = 3    # /models 回源重试次数
VERIFY_TIMEOUT = 30          # Key 校验最小请求超时（秒）
PORT_PROBE_TIMEOUT = 20      # 端口绑定重试上限（次）
READY_WAIT_SEC = 90          # 沙箱就绪等待上限（秒）

# ---------------- 安全 ----------------

MAX_BODY_BYTES = 64 * 1024 * 1024   # 入站请求体上限 64MB，超出返回 413
#  Anthropic 长上下文常见在数十 MB 量级，64MB 留足余量；
#  不设上限时一个超大 Content-Length 即可让本机内存耗尽。

# ---------------- 模型选择器 ----------------

# Science 模型选择器认可的壳 id（逆向结论，见 providers 模块文档）。
# 顺序即语义：resolve_model 按下标把壳 id 映射到用户真实模型的第 N 个。
# 改动此列表会改变模型选择行为，需同步 README 与 providers.model_map。
SHELL_IDS = ["claude-opus-4-8", "claude-haiku-4-5", "claude-sonnet-5"]

FAKE_CREATED_AT = "2026-01-01T00:00:00Z"
"""/v1/models 响应的 created_at 占位值。

Science 模型选择器要求该字段存在但不校验语义，用固定值保证响应稳定可测。
"""

# ---------------- CONNECT 隧道 ----------------

BLOCKED_SUFFIXES = ("anthropic.com", "claude.ai", "claude.com")
"""对 Anthropic 系域名的 CONNECT 返回 401（未登录）而非 403。

403 会被客户端当成组织/权限问题反复重试，卡在 "Switching organization"；
401 则被判为 logged-out 秒过。
"""

TUNNEL_ALLOWLIST_ENV = "CSSWITCH_ALLOW_TUNNEL"
"""CONNECT 隧道策略开关（v1.2.0 起默认**全部放行**）。

留空或 '*' = 任意目标都放行隧道（当前默认）。
设为 "host1,host2" = 回到白名单模式，仅放行这些目标。

背景：白名单最初用于避免本机附带通用正向代理，但实测代价过大 ——
Claude Science 的分析环境需访问大量包索引/CDN/文献 API，漏放任一个都会
表现为 Environments 面板装包失败，并连锁引发 Agent 报
"Your Claude session is no longer valid"。逐个补白名单不现实，故改为全放行。
代理仅监听 127.0.0.1，风险面等同于"本机直接装了个系统代理"。
"""

TUNNEL_ALLOW_ALL = "*"

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

TUNNEL_TRUSTED_DOMAINS = (
    # 下面是 Science 常用出口，仅作为**文档参考与将来收紧白名单时的候选集**，
    # v1.2.0 起不再参与判定（全放行）。保留它是为了让面板能展示"科学计算
    # 工具链会访问哪些站点"，以及日后想收紧时有现成清单可用。
    "conda.anaconda.org", "repo.anaconda.com", "pypi.org",
    "files.pythonhosted.org", "cran.r-project.org", "github.com",
    "raw.githubusercontent.com", "gitlab.com", "eutils.ncbi.nlm.nih.gov",
    "rest.uniprot.org", "www.ebi.ac.uk", "huggingface.co",
    "s3.amazonaws.com", "storage.googleapis.com",
    "ocsp.digicert.com", "time.windows.com",
)

# 必须从"我们自己拉起的子进程"环境里剥离的代理变量。
#
# 背景：很多企业环境 / 开发容器会在父进程里预设 HTTP_PROXY 等变量。若代理
# 进程继承了它们，urllib 就会把"访问上游 API"的请求也转发给那个代理 ——
# 本机实测踩过：https_proxy=127.0.0.1:61511 使所有出站请求返回
# os error 10061（目标计算机积极拒绝），表现为 502，且日志误指向上游地址。
#
# 代理进程（proxy_start 拉起）与沙箱子进程（sandbox_env）都必须剥离，
# 否则请求会绕回我们自己的本地代理形成回环。
PROXY_ENV_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY",
                  "http_proxy", "https_proxy", "all_proxy", "ftp_proxy")
