# -*- coding: utf-8 -*-
"""CSswitch —— Windows / macOS / Linux 版 Claude Science 模型切换工具。

直接代理并切换本机已安装的原生 Claude Science 客户端。
核心机制（参考 bfzha/CSswitch-wsl_linux 与 SuperJJ007/CSSwitch 的已验证实现）：
  1. 推理劫持：ANTHROPIC_BASE_URL 指向本地代理，剥离 OAuth、注入第三方 Key；
  2. 虚拟登录：沙箱 auth_dir 内伪造 v2 AES-256-GCM 令牌，让 Science 认为已登录；
  3. 环境隔离：沙箱 HOME / XDG 或 APPDATA / data-dir / 端口，真实登录零触碰。

本包只声明元信息，不在导入期拉起子模块（避免循环依赖）。
子模块按需显式导入：
    from csswitch import config, providers, proxy, sandbox, oauth_forge
"""

# 注意：PEP 396 约定的双下划线名是 __version__。
# v1.0.0 曾误写为 __version，导致 importlib.metadata / pkg.__version__ 取不到。
__version__ = "1.5.0"

__all__ = ["__version__"]
