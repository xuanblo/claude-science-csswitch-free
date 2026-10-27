# -*- coding: utf-8 -*-
"""宿主文件夹授权（host grants）。

Science 的每个对话都会在 ``orgs/<org>/workspaces/<uuid>/`` 下自建独立工作区，
不理会进程 ``cwd``。要让对话能直接操作宿主机上的项目文件夹，入口是 daemon
的 host-grants 偏好：把宿主目录登记为 ``ro`` / ``rw`` 授权根，之后 Science
界面的目录选择器就能选到它。

**重要：只有安全沙箱启用时这套机制才有意义。**
daemon 内部先判断 filesystem sandbox 是否可用：

- 沙箱**可用**（默认）：目录必须落在 ``$HOME`` 或某个已授权根下才允许
  访问，授权会真实写进 ``host_grants`` 表。
- 沙箱**不可用**（本工具关闭了「安全沙箱」开关，传了
  ``--dangerously-no-sandbox``）：整个文件系统已经天然可访问，daemon 直接
  回 ``alreadyAllowed`` 并明说 *"no grant was needed or recorded (it will not
  appear in list_host_grants)"* —— 也就是说**调用是空操作，不会落库**。

所以本模块必须能识别这两种状态：``sandbox_active`` 为 False 时，界面应
告知用户「当前不需要授权，沙箱已关闭」，而不是谎报成功。

daemon API 需要三层凭证，本模块一次性处理：

1. ``GET /?nonce=<一次性密码>``拿登录页，响应下发 ``operon_csrf`` cookie
2. ``POST /api/auth/nonce``（表单 ``nonce`` + ``dest``）换``operon_auth`` 会话 cookie
3. 写操作额外要求 ``Origin`` 为 daemon 自身 UI 源，且带 ``x-operon-csrf`` 头

会话 cookie 在 daemon 生命周期内有效，缓存在进程内复用，避免每次操作都消耗
一次性 nonce。
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config as C
from . import lifecycle as L

GRANTS_EP = "/api/preferences/host-grants"
HOME_EP = "/api/preferences/host-home"
BROWSE_EP = "/api/preferences/host-browse"
NONCE_EP = "/api/auth/nonce"

MODES = ("ro", "rw")

_lock = threading.Lock()
_cache: "_Session | None" = None


class HostGrantError(RuntimeError):
    """daemon 拒绝或无法完成一次 host-grant 操作。"""

    def __init__(self, message: str, code: int = 0, detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass
class Grant:
    host_path: str
    mount_name: str
    mode: str

    @property
    def writable(self) -> bool:
        return self.mode == "rw"

    def as_dict(self) -> dict[str, Any]:
        return {
            "hostPath": self.host_path,
            "mountName": self.mount_name,
            "mode": self.mode,
            "writable": self.writable,
        }


@dataclass
class BrowseEntry:
    name: str
    is_dir: bool


@dataclass
class AddResult:
    """一次授权的结果。

    ``recorded=False`` 表示 daemon 认为无需记录（沙箱已关闭，整盘已可访问），
    ``already_allowed=True`` 表示目录本来就在daemon 自管目录里（工作区/
    产物目录），同样无需授权。
    """

    host_path: str
    mount_name: str
    mode: str
    guest_path: str = ""
    recorded: bool = True
    already_allowed: bool = False
    reason: str = ""

    @property
    def writable(self) -> bool:
        return self.mode == "rw"

    @property
    def effective(self) -> str:
        """给用户看的一句话结论。"""
        if self.already_allowed:
            return self.reason or "该目录本来就可访问，无需授权"
        if not self.recorded:
            return ("安全沙箱已关闭，Claude 本来就能访问整个本机文件系统，"
                    "因此无需授权（也不会记录）")
        return f"已授权 {self.host_path}（{self.mode}），对话里就能选到它"

    def as_dict(self) -> dict[str, Any]:
        return {
            "hostPath": self.host_path,
            "mountName": self.mount_name,
            "guestPath": self.guest_path or self.host_path,
            "mode": self.mode,
            "writable": self.writable,
            "recorded": self.recorded,
            "alreadyAllowed": self.already_allowed,
            "effective": self.effective,
        }


class _Session:
    """一次登录得到的会话，持有 cookie 与 csrf token。"""

    def __init__(self, port: int, opener: urllib.request.OpenerDirector,
                 cookies: dict[str, str], csrf: str, origin: str) -> None:
        self.port = port
        self.opener = opener
        self.cookies = cookies
        self.csrf = csrf
        self.origin = origin

    def request(self, method: str, path: str, payload: Any = None,
                write: bool = True, timeout: float = 60.0) -> tuple[int, Any]:
        body = None
        headers: dict[str, str] = {}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if write:
            # daemon 只接受来自自身 UI 源的浏览器写入
            headers["Origin"] = self.origin
            headers["Referer"] = self.origin + "/"
            headers["x-operon-csrf"] = self.csrf
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            resp = self.opener.open(req, timeout=timeout)
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, _loads(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            return exc.code, _loads(raw)


def _loads(raw: str) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return {"detail": raw[:400]}


def _login() -> _Session:
    """走完整登录流程换一个 daemon 会话。"""
    login_url = L.current_url(max_age_s=0.0)
    parsed = urllib.parse.urlparse(login_url)
    port = parsed.port or int(C.load_config()["settings"].get("sandbox_port", 8090))
    nonce = urllib.parse.parse_qs(parsed.query).get("nonce", [""])[0]
    if not nonce:
        raise HostGrantError("无法获取登录 nonce，请先在控制台启动 Claude Science")

    jar = http.cookiejar.CookieJar()
    # 显式清空代理：本模块只打127.0.0.1，若继承系统 https_proxy 会绕出去
    # 并报 "upstream connect failed (os error 10061)"
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(jar))
    fetch_url = login_url.replace("localhost", "127.0.0.1", 1)

    with opener.open(fetch_url, timeout=30) as resp:
        resp.read()
    form = urllib.parse.urlencode({"nonce": nonce, "dest": "/"}).encode("ascii")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{NONCE_EP}", data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with opener.open(req, timeout=30) as resp:
        resp.read()

    cookies = {c.name: c.value for c in jar}
    if "operon_auth" not in cookies:
        raise HostGrantError("daemon 未签发会话 cookie（operon_auth）")
    csrf = cookies.get("operon_csrf", "")
    if not csrf:
        raise HostGrantError("daemon 未下发 csrf cookie（operon_csrf）")
    return _Session(port, opener, cookies, csrf, f"http://localhost:{port}")


def _session() -> _Session:
    """返回可用会话；失效时自动重新登录一次。"""
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        _cache = _login()
        return _cache


def reset_session() -> None:
    """丢弃缓存会话（daemon 重启后调用）。"""
    global _cache
    with _lock:
        _cache = None


def browser_login() -> tuple[int, list[str]]:
    """预热一个可注入浏览器的登录会话，返回 ``(daemon端口, Set-Cookie 值列表)``。

    **为什么需要它**：Science 的登录 nonce 落地页只是一个需要点击的表单
    （``<form method="post" action="./api/auth/nonce" id="signin-form">``），
    光打开链接会停在 "Sign in" 页。实测服务端登录后拿到的 ``operon_auth``
    可以直接访问首页（返回真正的 Claude Science 页面），所以最干净的做法
    是：服务端先把一次性 nonce 用掉换cookie，再转交给浏览器。

    cookie **不区分端口、只认主机名**，所以面板与 daemon 同为 localhost 时，
    代理这边设的 cookie 能被 daemon 收到。

    属性严格照抄 daemon 自己下发的（实测）：
    ``operon_csrf`` **不能**加 HttpOnly —— 前端要读它填 ``x-operon-csrf``；
    ``operon_auth`` 才是 HttpOnly。
    """
    sess = _session()
    cookies = []
    for name, value in sess.cookies.items():
        flags = "Path=/; SameSite=Lax"
        if name != "operon_csrf":
            flags += "; HttpOnly"
        cookies.append(f"{name}={value}; {flags}")
    return sess.port, cookies


def _call(method: str, path: str, payload: Any = None, write: bool = True,
          retry: bool = True) -> tuple[int, Any]:
    sess = _session()
    status, data = sess.request(method, path, payload, write=write)
    if status in (401, 403) and retry and not write:
        # 会话过期，重登一次再试
        reset_session()
        status, data = _session().request(method, path, payload, write=write)
    return status, data


def host_home() -> Path:
    """daemon 视角的 HOME（用于校验路径归属）。"""
    status, data = _call("GET", HOME_EP, write=False)
    if status != 200 or not isinstance(data, dict):
        raise HostGrantError("读取 host-home 失败", status, str(data))
    return Path(str(data.get("path", "")))


def list_grants() -> list[Grant]:
    """列出已授权的宿主文件夹。"""
    status, data = _call("GET", GRANTS_EP, write=False)
    if status != 200 or not isinstance(data, dict):
        raise HostGrantError("读取授权列表失败", status, str(data))
    out: list[Grant] = []
    for item in data.get("grants") or []:
        if not isinstance(item, dict):
            continue
        host = str(item.get("hostPath") or "")
        if not host:
            continue
        out.append(Grant(
            host_path=host,
            mount_name=str(item.get("mountName") or Path(host).name),
            mode=str(item.get("mode") or "ro"),
        ))
    return out


def normalize_mode(mode: str | None) -> str:
    m = (mode or "rw").strip().lower()
    if m in ("read", "readonly", "r"):
        return "ro"
    if m in ("write", "readwrite", "w"):
        return "rw"
    if m not in MODES:
        raise HostGrantError(f"mode 必须是 {' 或 '.join(MODES)}")
    return m


def normalize_path(path: str, must_exist: bool = True) -> str:
    """规整为绝对路径并做基本合法性检查。

    ``must_exist=False`` 仅供撤销使用：目录可能已被删除或改名，
    但 daemon 里的授权记录仍需能被清掉。
    """
    raw = (path or "").strip().strip('"')
    if not raw:
        raise HostGrantError("路径不能为空")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise HostGrantError(f"必须是绝对路径：{raw}")
    try:
        resolved = p.resolve()
    except OSError as exc:
        raise HostGrantError(f"路径无法解析：{raw}（{exc.strerror or exc}）")
    if must_exist:
        if not resolved.exists():
            raise HostGrantError(f"路径不存在：{resolved}")
        if not resolved.is_dir():
            raise HostGrantError(f"只能是文件夹，不能是文件：{resolved}")
    return str(resolved)


# exFAT/FAT32 这类卷不持久化 ACL（FILE_PERSISTENT_ACLS 缺位）。沙箱的
# 删除保护边界靠 ACL 表达，在这种卷上写不出来：rw 被 daemon 直接拒；
# ro 虽然能写进列表，但之后每个新对话的 session keeper 都会起不来
# （实测日志："the volume's filesystem is exFAT, which persists NO ACLs"）。
_FILE_PERSISTENT_ACLS = 0x0008


def _volume_acl_support(path: str) -> tuple[str, bool] | None:
    """探测路径所在卷：``(文件系统名, 是否持久化 ACL)``；探测不到返回 None。"""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    anchor = Path(path).anchor or "C:\\"
    name = ctypes.create_unicode_buffer(64)
    flags = wintypes.DWORD(0)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(anchor), None, 0, None, None,
        ctypes.byref(flags), name, 64)
    if not ok:
        return None
    return name.value, bool(flags.value & _FILE_PERSISTENT_ACLS)


def ensure_volume_supports_acl(path: str) -> None:
    """在登记授权前拦掉不支持 ACL 的卷（exFAT / FAT32）。

    daemon 的报错是英文且只说"delete-withholding grant"，用户看不出根因；
    更糟的是只读授权看起来成功，却会让新对话的沙箱启动失败。所以这里
    本地先挡，直接给出中文的可操作提示。
    """
    info = _volume_acl_support(path)
    if info is None:
        return
    fs_name, has_acl = info
    if has_acl:
        return
    raise HostGrantError(
        f"{Path(path).anchor or path} 所在磁盘是 {fs_name or '不支持 ACL 的文件系统'}"
        f"（如 exFAT、FAT32），不保存文件权限，沙箱无法在上面建立「删除保护」"
        f"边界：可读写会被直接拒绝，只读虽然能登记，但会让之后每个新对话的"
        f"沙箱启动失败。请改用 NTFS 磁盘上的文件夹，"
        f"或关闭「安全沙箱」后再访问该盘。")


def add_grant(path: str, mode: str = "rw") -> AddResult:
    """授权一个宿主文件夹（已存在则更新权限模式）。

    沙箱关闭时 daemon 会返回 ``alreadyAllowed`` 而不落库 —— 这种情况通过
    ``AddResult.recorded=False`` 如实反映，不当成成功。
    """
    host = normalize_path(path)
    m = normalize_mode(mode)
    # 沙箱关闭时整盘本就可达，授权是空操作，无需做卷预检
    if sandbox_active():
        ensure_volume_supports_acl(host)
    status, data = _call("POST", GRANTS_EP, {"path": host, "mode": m})
    if status == 200 and isinstance(data, dict):
        reason = str(data.get("reason") or "")
        guest = str(data.get("guestPath") or host)
        recorded = not data.get("alreadyAllowed") and "no grant was needed" not in reason
        return AddResult(
            host_path=str(data.get("hostPath") or host),
            mount_name=str(data.get("mountName") or Path(host).name),
            mode=str(data.get("mode") or m),
            guest_path=guest,
            recorded=recorded,
            already_allowed=bool(data.get("alreadyAllowed")),
            reason=reason,
        )
    if status == 409:
        # daemon 拒绝在已有 rw 授权的路径上"降级"成 ro：共享的 live bind 会
        # 变成 ro，而 rw 那行仍留着，状态自相矛盾。它建议改用界面上的
        # Access 开关，所以这里给出可操作的提示而不是干巴巴的 409。
        raise HostGrantError(
            f"该路径已有更高权限（可读写）的授权，不能降级为只读。"
            f"要改成只读，请先撤销现有授权再重新以「只读」授权。"
            f"（{_err_text(data, '')}）", 409, str(data))
    detail = _err_text(data, "授权失败")
    hint = _explain_failure(detail)
    raise HostGrantError(f"{hint}：{detail}" if hint else detail, status, str(data))


def remove_grant(path: str) -> None:
    """撤销一个宿主文件夹的授权。

    不要求目录仍然存在：用户可能已经删掉/改名了那个文件夹，
    但 daemon 里的授权记录仍需能清掉。
    """
    host = normalize_path(path, must_exist=False)
    status, data = _call("DELETE", GRANTS_EP, {"path": host})
    if status not in (200, 204, 404):
        raise HostGrantError(_err_text(data, "撤销失败"), status, str(data))


def browse(path: str = "") -> list[BrowseEntry]:
    """列目录。空路径表示 daemon 的 HOME；只允许已授权根及其子目录。"""
    target = normalize_path(path) if path.strip() else str(host_home())
    qs = urllib.parse.urlencode({"path": target})
    status, data = _call("GET", f"{BROWSE_EP}?{qs}", write=False)
    if status != 200 or not isinstance(data, list):
        raise HostGrantError(_err_text(data, "列目录失败"), status, str(data))
    return [BrowseEntry(str(e.get("name", "")), bool(e.get("isDirectory")))
            for e in data if isinstance(e, dict)]


def _explain_failure(detail: str) -> str:
    """把 daemon 的英文报错翻成可操作的中文（原文仍附在后面）。"""
    low = detail.lower()
    if "delete-withholding" in low:
        return ("该文件夹上无法建立沙箱的「删除保护」边界（文件系统不保存 ACL，"
                "或文件夹权限是全开 DACL），因此没有授予可读写；"
                "可换 NTFS 磁盘上的子文件夹再试")
    if "could not be accessed" in low:
        return ("daemon 访问不到该目录（盘根不可作为授权根、权限不足，"
                "或卷状态异常）")
    if "not under $home" in low:
        return "该目录不在 daemon 的 $HOME 或任何已授权根之下"
    return ""


def _err_text(data: Any, fallback: str) -> str:
    if isinstance(data, dict):
        for key in ("error", "detail", "message"):
            val = data.get(key)
            if val:
                return str(val)
    return fallback


def sandbox_active() -> bool:
    """daemon 的 filesystem sandbox 是否真的在生效。

    判据用**本工具自己的配置**而不是探测：``--dangerously-no-sandbox``
    正是按 ``settings.sandbox_enabled`` 决定要不要传的，所以配置即事实。
    早前版本改成比较 ``host_home()`` 与 ``Path.home()``，实测不可靠 ——
    daemon 刚起来那一瞬登录会失败，异常被吞掉后误判成"沙箱没开"。

    daemon 侧的 HOME 只作为展示信息返回给用户看。
    """
    from . import config as C
    return bool(C.load_config()["settings"].get("sandbox_enabled", True))

