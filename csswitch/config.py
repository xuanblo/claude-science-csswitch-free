# -*- coding: utf-8 -*-
"""配置管理：默认 %USERPROFILE%\\.csswitch-win\\config.json

⚠️ 目录名说明（重要）：Claude Science 官方自带的 CSwitch 也使用
`~/.csswitch`，且它的 config.json 格式（schema_version / profiles /
active_id / mode）与本工具完全不同，还存有用户的真实 API Key。
本工具因此**故意使用独立目录 `~/.csswitch-win`**，避免读写冲突与
密钥串味。`load_config()` 另有格式护栏：一旦在目标目录里读到官方
格式，会直接报错拒绝启动，而不是把两套字段混写进同一文件。

安全约定（对齐参考项目铁律）：
  - API Key 只落本机 config.json，不写入命令行参数、不写日志；
  - 目录 0700 / 文件 0600（Windows 上额外用 icacls 收紧 ACL 到当前用户）；
  - 界面只显示掩码（末四位）。
"""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import constants as K
from . import logging_setup as L
from . import providers as P

log = L.get_logger("cli")

# 目录名带 -win 后缀，与官方 CSwitch 的 .csswitch 区分
CONFIG_DIR_NAME = ".csswitch-win"
CONFIG_FILE = "config.json"
STATE_FILE = "state.json"

# 官方 CSwitch（Claude Science 内置）config.json 的特征字段。
# 命中任意一个即判定为"不是本工具的配置目录"，拒绝读写。
OFFICIAL_MARKER_KEYS = ("profiles", "active_id", "schema_version")

DEFAULT_SETTINGS = {
    # 端口默认值取自 constants，避免两处硬编码漂移
    "proxy_port": K.DEFAULT_PROXY_PORT,   # 本地代理端口（回环）
    "sandbox_port": K.SANDBOX_PORT,       # 沙箱 Science 端口（避开官方 8000）
    "science_bin": "",       # claude-science 可执行文件路径（空=自动发现）
    "open_browser": True,    # 启动后是否自动打开浏览器

    # ---- Science 运行环境（v1.3.0）----
    # 是否启用 Science 自带的安全沙箱。关闭后 Claude 对本机文件与网络
    # 有完全访问权、不再弹权限审批 —— 只在明确需要时关。
    "sandbox_enabled": True,
}


class ForeignConfigError(RuntimeError):
    """目标目录里是官方 CSwitch 或其他工具的配置，拒绝混写。"""


def _assert_not_foreign_config(cfg: dict):
    """护栏：拒绝把本工具字段写进非本工具的配置文件。"""
    if not isinstance(cfg, dict):
        return
    hit = [k for k in OFFICIAL_MARKER_KEYS if k in cfg]
    if hit:
        example = ("set CSSWITCH_HOME=D:\\CSSwitch-Win-Data" if os.name == "nt"
                   else 'export CSSWITCH_HOME="$HOME/.csswitch-win"')
        raise ForeignConfigError(
            f"{config_path()} 看起来是官方 Claude Science CSwitch 的配置"
            f"（命中特征字段 {hit}），不是本工具的配置目录。\n"
            f"两个工具的配置格式不同且都存有 API Key，混写会造成密钥串味与"
            f"字段互相覆盖。\n"
            f"请改用独立目录，例如先执行：\n"
            f"    {example}\n"
            f"再重新运行本工具（不要修改官方配置文件）。")


def base_dir() -> Path:
    """CSwitch-Win 根目录：默认 %USERPROFILE%\\.csswitch-win。

    可用环境变量 CSSWITCH_HOME 覆盖（测试隔离 / 便携模式）。
    """
    env = os.environ.get("CSSWITCH_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / CONFIG_DIR_NAME


def config_path() -> Path:
    return base_dir() / CONFIG_FILE


def state_path() -> Path:
    return base_dir() / STATE_FILE


def logs_dir() -> Path:
    return base_dir() / "logs"


def sandbox_root() -> Path:
    return base_dir() / "sandbox"


def sandbox_home() -> Path:
    """沙箱 USERPROFILE（Science 按 homedir 推导 ~/.claude-science 登录态）。"""
    return sandbox_root() / "home"


def sandbox_data_dir() -> Path:
    """沙箱 data-dir（--data-dir 指向这里，会话数据与真实目录分离）。"""
    return sandbox_root() / "data"


def sandbox_auth_dir() -> Path:
    """沙箱 auth_dir：虚拟登录凭证写入处（= 沙箱 HOME 下的 .claude-science）。"""
    return sandbox_home() / ".claude-science"


def real_auth_dir() -> Path:
    """真实登录目录（护栏对象，绝不触碰）：%USERPROFILE%\\.claude-science。"""
    return Path.home() / ".claude-science"


# ---------------- 底层读写 ----------------

def _harden_path(p: Path):
    """尽力收紧权限：Unix chmod；Windows icacls 移除继承并只授权当前用户。

    注意：文件用 `user:F`，目录才用 `user:(OI)(CI)F` —— 容器继承标志打在
    文件上会产生不可读的 ACE（实测踩过），目录与文件必须区别对待。
    best-effort：失败只记 warning，不阻断功能。
    """
    try:
        if os.name == "nt":
            user = os.environ.get("USERNAME", "")
            if not user:
                log.debug("USERNAME 为空，跳过 icacls: %s", p)
                return
            ace = f"{user}:(OI)(CI)F" if p.is_dir() else f"{user}:F"
            r = subprocess.run(["icacls", str(p), "/inheritance:r", "/grant:r", ace],
                               capture_output=True, timeout=10)
            if r.returncode != 0:
                log.warning("icacls 收紧权限失败(%s): %s",
                            r.returncode, r.stderr.decode("utf-8", "replace")[:200])
        else:
            os.chmod(p, 0o700 if p.is_dir() else 0o600)
    except Exception as e:  # noqa: BLE001 - 权限收紧失败不应阻断功能
        log.warning("权限收紧失败 %s: %s", p, e)


def _grant_current_user(p: Path):
    """给已存在的文件补上当前用户的完全控制权（不改继承设置）。

    用于：父目录被 /inheritance:r 收紧后，新建文件默认不继承任何 ACE，
    导致后续 os.replace 报 WinError 5 拒绝访问（本机实测踩过）。
    """
    if os.name != "nt":
        return
    user = os.environ.get("USERNAME", "")
    if not user:
        return
    try:
        subprocess.run(["icacls", str(p), "/grant:r", f"{user}:F"],
                       capture_output=True, timeout=10)
    except Exception as e:  # noqa: BLE001
        log.debug("授权失败 %s: %s", p, e)


def _atomic_write(path: Path, text: str):
    """原子写：随机临时名 + O_EXCL + fsync + os.replace。

    临时文件名带随机后缀，避免并发写（多进程热切换 + CLI 写 config）时
    互相覆盖。临时文件以 0600 创建，尽量缩短「继承父目录权限」的窗口。

    Windows 关键顺序：新建临时文件后必须显式授权当前用户（父目录可能已被
    收紧继承），否则 os.replace 会报 WinError 5。本机实测踩过此坑。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _grant_current_user(tmp)
        os.replace(str(tmp), path)
        _harden_path(path)
    except PermissionError as e:
        # 旧权限残留导致 replace 失败：给目标文件与临时文件补授权后重试
        log.warning("原子写被拒（补授权重试）: %s", e)
        _grant_current_user(path)
        _grant_current_user(tmp)
        try:
            os.replace(str(tmp), path)
            _harden_path(path)
        except OSError as e2:
            log.error("原子写重试仍失败: %s", e2)
            raise
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError as e:
                log.debug("临时文件清理失败 %s: %s", tmp, e)


def load_config() -> dict:
    cfg_path = config_path()
    if not cfg_path.is_file():
        return {"version": 1, "current": None, "providers": [],
                "settings": dict(DEFAULT_SETTINGS)}
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("config.json 损坏（%s），已回退默认空配置：%s", e, cfg_path)
        log.warning("如需保留旧配置请先备份再手工修复该文件")
        cfg = {}
    if not isinstance(cfg, dict):
        log.warning("config.json 根节点不是对象，已回退默认空配置")
        cfg = {}
    # 护栏：拒绝在官方 CSwitch 的配置上继续操作（抛错而非静默混写）
    _assert_not_foreign_config(cfg)
    cfg.setdefault("version", 1)
    cfg.setdefault("current", None)
    cfg.setdefault("providers", [])
    if not isinstance(cfg["providers"], list):
        log.warning("config.json 的 providers 不是数组，已重置")
        cfg["providers"] = []
    settings = dict(DEFAULT_SETTINGS)
    if isinstance(cfg.get("settings"), dict):
        settings.update(cfg["settings"])
    cfg["settings"] = settings
    return cfg


def save_config(cfg: dict):
    # 写入前再校验一次：即使调用方绕过了 load_config，也不会污染他人配置
    _assert_not_foreign_config(cfg)
    _atomic_write(config_path(), json.dumps(cfg, ensure_ascii=False, indent=2))


def ensure_config() -> dict:
    """确保 config.json 存在：首次运行时生成默认配置并落盘。

    新电脑 / 空数据目录第一次启动时，控制台和代理都需要一个可写的配置
    文件。这里主动建一份空配置（只有默认 settings、没有 provider），
    用户随后在面板里添加第一条即可。
    """
    cfg = load_config()
    if not config_path().is_file():
        save_config(cfg)
        log.info("已创建默认配置: %s", config_path())
    return cfg


def load_state() -> dict:
    p = state_path()
    if not p.is_file():
        return {}
    try:
        v = json.loads(p.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except Exception as e:  # noqa: BLE001
        log.debug("state.json 读取失败（运行态丢失，不影响使用）: %s", e)
        return {}


def save_state(st: dict):
    _atomic_write(state_path(), json.dumps(st, ensure_ascii=False, indent=2))


# ---------------- Provider 记录 CRUD ----------------

def _norm_kind(kind: str) -> str:
    """未知类型一律落到 custom-anthropic，绝不臆造。"""
    return kind if kind in P.all_kinds() else "custom-anthropic"


def new_provider_record(name: str, kind: str, key: str, model: str = "",
                        base_url: str = "", models=None) -> dict:
    """按类型模板新建一条配置（只存本机，不联网）。"""
    kind = _norm_kind(kind)
    tpl = P.template_for(kind)
    model = (model or "").strip() or tpl["default_model"]
    base_url = (base_url or "").strip() or tpl.get("base_url", "")
    model_map = dict(tpl.get("model_map") or {})
    # 自定义（passthrough）不建映射；壳 id 无意义时也不建
    if tpl.get("passthrough"):
        model_map = {}
    return {
        "name": name,
        "kind": kind,
        "mode": tpl["mode"],
        "base_url": base_url,
        "key": key,
        "model": model,
        "models": list(models or tpl.get("models") or []),
        "model_map": model_map,
        "model_caps": dict(tpl.get("model_caps") or {}),
        "default_cap": tpl.get("default_cap"),
        "auth_style": tpl.get("auth_style", "x-api-key"),
        "thinking": tpl.get("thinking"),
        "force_tool_disables_thinking": tpl.get("force_tool_disables_thinking", False),
        "fetch_models": tpl.get("fetch_models", False),
        "passthrough": tpl.get("passthrough", False),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def find_provider(cfg: dict, name: str):
    for p in cfg["providers"]:
        if p["name"] == name:
            return p
    return None


def add_provider(cfg: dict, rec: dict) -> str:
    if find_provider(cfg, rec["name"]):
        raise ValueError(f"配置名已存在: {rec['name']}")
    if not rec.get("key"):
        raise ValueError("缺少 API Key")
    cfg["providers"].append(rec)
    if not cfg.get("current"):
        cfg["current"] = rec["name"]
    save_config(cfg)
    return rec["name"]


def update_provider(cfg: dict, rec: dict) -> str:
    """按name 就地更新一条配置（保留未提供的字段）。"""
    for i, p in enumerate(cfg["providers"]):
        if p["name"] == rec["name"]:
            merged = dict(p)
            merged.update({k: v for k, v in rec.items() if v is not None})
            cfg["providers"][i] = merged
            save_config(cfg)
            return rec["name"]
    raise ValueError(f"配置不存在: {rec['name']}")


def remove_provider(cfg: dict, name: str) -> bool:
    before = len(cfg["providers"])
    cfg["providers"] = [p for p in cfg["providers"] if p["name"] != name]
    if len(cfg["providers"]) == before:
        return False
    if cfg.get("current") == name:
        cfg["current"] = cfg["providers"][0]["name"] if cfg["providers"] else None
    save_config(cfg)
    return True


def set_current(cfg: dict, name: str):
    if not find_provider(cfg, name):
        raise ValueError(f"配置不存在: {name}")
    cfg["current"] = name
    save_config(cfg)


def current_provider(cfg: dict):
    name = cfg.get("current")
    return find_provider(cfg, name) if name else None


def mask_key(key: str) -> str:
    """掩码：只露末四位。"""
    if not key:
        return "(未设置)"
    if len(key) <= 4:
        return "****"
    return key[:3] + "..." + key[-4:]
