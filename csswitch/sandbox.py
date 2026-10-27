# -*- coding: utf-8 -*-
"""沙箱管理：在隔离环境中启动本机原生 Claude Science，并注入本地代理。

Windows 使用 USERPROFILE/APPDATA 隔离；macOS 与 Linux 使用 HOME、XDG
目录和 TMPDIR 隔离。进程创建参数也按平台切换，避免把 Windows 的
creationflags 传给 Unix subprocess。

隔离铁律：
  - 沙箱 HOME/data-dir 全部落在 CSSWITCH_HOME/sandbox 下；
  - 真实 ~/.claude-science 零读取、零修改、零删除；
  - 虚拟登录只写沙箱 auth_dir（oauth_forge 护栏兜底）。
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from . import config as C
from . import constants as K
from . import logging_setup as L
from . import oauth_forge as OF

log = L.get_logger("cli")

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
_SPAWN_FLAGS = CREATE_NO_WINDOW | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def _process_options(detached=False, os_name=None):
    """返回当前平台的 subprocess 参数；Unix 后台进程另起会话。"""
    os_name = os_name or os.name
    if os_name == "nt":
        return {"creationflags": _SPAWN_FLAGS if detached else CREATE_NO_WINDOW}
    return {"start_new_session": True} if detached else {}


# 官方保留默认端口（claude-science serve 默认 8000）
REAL_DEFAULT_PORT = K.REAL_DEFAULT_PORT

# 代理相关环境变量：沙箱子进程必须剥离，避免上游请求绕回本地代理
# 代理变量清单统一收敛到 constants（沙箱与代理进程共用同一份）
_PROXY_ENV_VARS = K.PROXY_ENV_VARS


# ---------------- 二进制发现 ----------------

def _candidate_paths(platform=None):
    """各系统常见安装位置；platform 参数便于离线测试非本机分支。"""
    platform = platform or sys.platform
    home = Path.home()
    if platform == "win32":
        la = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
        return [
            la / "Programs" / "claude-science" / "claude-science.exe",
            la / "Programs" / "claude-science" / "bin" / "claude-science.exe",
            la / "Programs" / "Claude Science" / "claude-science.exe",
            la / "Programs" / "Claude Science" / "Claude Science.exe",
            la / "Microsoft" / "WindowsApps" / "claude-science.exe",
            home / ".local" / "bin" / "claude-science.exe",
        ]
    if platform == "darwin":
        apps = [Path("/Applications/Claude Science.app"),
                home / "Applications" / "Claude Science.app"]
        paths = [app / "Contents" / sub / "claude-science"
                 for app in apps for sub in ("Resources/bin", "MacOS")]
        return paths + [home / ".local" / "bin" / "claude-science",
                        Path("/opt/homebrew/bin/claude-science"),
                        Path("/usr/local/bin/claude-science")]
    return [home / ".local" / "bin" / "claude-science",
            Path("/usr/local/bin/claude-science"),
            Path("/usr/bin/claude-science"),
            Path("/opt/claude-science/bin/claude-science")]


def _from_start_menu():
    """解析开始菜单快捷方式（.lnk）找目标 exe。失败返回 None。"""
    if os.name != "nt":
        return None
    menu = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    if not menu.is_dir():
        return None
    ps = (
        "$sh = New-Object -ComObject WScript.Shell;"
        "Get-ChildItem -Path '%s' -Recurse -Filter *.lnk | "
        "Where-Object { $_.Name -like '*Science*' } | "
        "ForEach-Object { try { $sh.CreateShortcut($_.FullName).TargetPath } catch {} }"
    ) % str(menu)
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=20,
                             creationflags=CREATE_NO_WINDOW)
        for line in out.stdout.splitlines():
            line = line.strip()
            if line.lower().endswith(".exe") and Path(line).is_file():
                return line
    except Exception as e:  # noqa: BLE001 - 开始菜单不可查时按「未发现」处理
        log.debug("开始菜单快捷方式解析失败: %s", e)
    return None


def _bundled_science() -> str:
    """便携模式只选择当前系统的二进制，避免把 Windows exe 用到 Unix。"""
    root = Path(__file__).resolve().parent.parent  # csswitch/ -> 项目根
    if os.name == "nt":
        rels = ("runtime/claude-science/claude-science.exe",
                "runtime/claude-science/Claude Science.exe")
    else:
        rels = ("runtime/claude-science/claude-science",)
    for rel in rels:
        p = root / rel
        if p.is_file():
            return str(p)
    return ""


def find_science_binary(cfg=None) -> str:
    """发现 claude-science 可执行文件。找不到返回 ""。"""
    cfg = cfg or C.load_config()
    # 0) 便携版：项目内自带（最高优先级）
    bundled = _bundled_science()
    if bundled:
        return bundled
    # 1) 用户显式指定
    manual = (cfg.get("settings") or {}).get("science_bin", "")
    if manual and Path(manual).is_file():
        return manual
    # 2) PATH
    w = shutil.which("claude-science")
    if w:
        p = Path(w)
        if p.suffix.lower() == ".exe" or p.suffix.lower() == "":
            return str(p)
        # .cmd/.bat shim：记录路径，启动时经 cmd /c 执行
        return str(p)
    # 3) 常见安装路径
    for p in _candidate_paths():
        if p.is_file():
            return str(p)
    # 4) 开始菜单快捷方式
    lnk = _from_start_menu()
    if lnk:
        return lnk
    return ""


def _cmd_prefix(bin_path: str):
    """Windows cmd shim 经 cmd /c；原生 exe 与 Unix 可执行文件直接运行。"""
    if os.name == "nt" and bin_path.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", bin_path]
    return [bin_path]


# ---------------- 环境装配 ----------------

def sandbox_env(proxy_port: int, auth_secret: str, os_name=None) -> dict:
    """沙箱子进程环境：注入代理指向 + 按设置决定用户目录是否隔离。

    os_name 仅供跨平台离线测试，正常调用始终取当前系统。

    先剥离父进程的全部代理变量再设自己需要的 HTTPS_PROXY：
    用户本机的 HTTP_PROXY（如企业环境）若被继承下来，会让沙箱里的普通
    HTTP 请求走公司代理，行为不可控且难排查。与 proxy._clear_proxy_env 同策略。

    settings["sandbox_enabled"]（v1.3.0）：
    - True（默认）：USERPROFILE/HOME/APPDATA 全部重定向到沙箱 home，
      Claude 看不到你的真实用户目录，虚拟登录也写在沙箱里。安全边界最紧。
    - False：**保留真实的 USERPROFILE/HOME/APPDATA**，Claude 可以直接读写
      你的实际文件（含 D:\\Paper 等项目目录），无需再逐个授权。
      代价：它对整个用户目录有完全访问权 —— 这正是用户明确要求的能力，
      所以做成默认关闭的开关而非默认行为。

    无论开关如何设置，虚拟登录（~/.claude-science/.oauth-tokens）始终写在
    **沙箱** home 里，不污染你真实的 ~/.claude-science。
    """
    settings = C.load_config()["settings"]
    isolate = bool(settings.get("sandbox_enabled", True))
    # env_home：Claude 看到的用户主目录。隔离时指向沙箱，否则是真实家目录。
    env_home = C.sandbox_home() if isolate else Path.home()
    # AppData 始终独立：即使不隔离也不往真实 AppData 里写，避免与官方
    # Science 实例抢同一份配置/缓存。
    appdata = (C.sandbox_home() / "AppData") if isolate \
        else (env_home / "AppData")

    os_name = os_name or os.name
    env = dict(os.environ)
    for k in _PROXY_ENV_VARS:
        env.pop(k, None)
    env["HOME"] = str(env_home)
    if os_name == "nt":
        env["USERPROFILE"] = str(env_home)
        env["APPDATA"] = str(appdata / "Roaming")
        env["LOCALAPPDATA"] = str(appdata / "Local")
        env["TEMP"] = str(appdata / "Local" / "Temp")
        env["TMP"] = env["TEMP"]
    elif isolate:
        # Unix 工具常绕过 HOME 直接读 XDG/TMPDIR；这些也必须一并隔离。
        env["XDG_CONFIG_HOME"] = str(env_home / ".config")
        env["XDG_DATA_HOME"] = str(env_home / ".local" / "share")
        env["XDG_CACHE_HOME"] = str(env_home / ".cache")
        env["XDG_STATE_HOME"] = str(env_home / ".local" / "state")
        env["TMPDIR"] = str(env_home / "tmp")

    # 推理劫持：走本地代理（path secret 一次性鉴权）
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{proxy_port}/{auth_secret}"
    # 外联防卡：Anthropic 域名 CONNECT 经代理 401 秒判未登录；
    # 刻意不设 http_proxy（普通 HTTP 直连，避免干扰装包/MCP）

    env["HTTPS_PROXY"] = f"http://127.0.0.1:{proxy_port}"
    env["https_proxy"] = env["HTTPS_PROXY"]
    env["NO_PROXY"] = "127.0.0.1,localhost,::1"
    env["no_proxy"] = env["NO_PROXY"]
    return env


def ensure_sandbox_dirs():
    home = C.sandbox_home()
    for d in (home, C.sandbox_data_dir(), C.logs_dir(),
              home / "AppData" / "Roaming",
              home / "AppData" / "Local" / "Temp",
              home / ".config", home / ".local" / "share",
              home / ".local" / "state", home / ".cache", home / "tmp"):
        d.mkdir(parents=True, exist_ok=True)


def _port_open(port: int, host="127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _run(bin_path, args, env, timeout=60, capture=True):
    cmd = _cmd_prefix(bin_path) + args
    return subprocess.run(cmd, env=env, capture_output=capture, text=True,
                          timeout=timeout, **_process_options())


# ---------------- 启动 / 停止 / 状态 ----------------

def _our_sandbox_running(bin_path: str, port: int) -> bool:
    """端口通的前提下，判断是不是【本沙箱】在跑（权威判据：status --data-dir）。"""
    data_dir = C.sandbox_data_dir()
    if not data_dir.exists():
        return False
    env = sandbox_env(C.load_config()["settings"]["proxy_port"], "x")
    try:
        out = _run(bin_path, ["status", "--data-dir", str(data_dir)], env,
                   timeout=15)
        if out.returncode == 0 and out.stdout.strip():
            return bool(json.loads(out.stdout.strip()).get("running"))
    except Exception as e:  # noqa: BLE001 - 判不出来按「不是本沙箱」处理
        log.debug("沙箱状态探测失败（按未运行处理）: %s", e)
    return False


def resolve_sandbox_port(port: int, bin_path: str) -> int:
    """解析沙箱端口：空闲 -> 原端口；本沙箱在跑 -> 停掉后用原端口；
    外来进程占用 -> 自动顺延到后续空闲端口（最多 +20）。"""
    if not _port_open(port):
        return port
    if _our_sandbox_running(bin_path, port):
        stop_sandbox(port=port)
        for _ in range(20):
            if not _port_open(port):
                return port
            time.sleep(0.25)
    for p in range(port + 1, port + 21):
        if not _port_open(p):
            log.info("端口 %d 被外来进程占用，沙箱自动顺延到 %d", port, p)
            return p
    raise RuntimeError(
        f"端口 {port}~{port + 20} 均被占用。请在 ~/.csswitch/config.json 的 "
        f"settings.sandbox_port 手动更换端口。")


def start_sandbox(proxy_port: int, auth_secret: str, port: int = None,
                  open_browser: bool = True,
                  wait_sec: int = K.READY_WAIT_SEC) -> dict:
    """一键开始：确保虚拟登录 -> 启动沙箱 Science -> 就绪 -> 返回 URL。"""
    cfg = C.load_config()
    bin_path = find_science_binary(cfg)
    if not bin_path:
        return {"ok": False, "stage": "science_not_found",
                "detail": "未找到 claude-science 可执行文件。请先安装当前系统的原生版 "
                          "Claude Science，或在配置中 settings.science_bin 手动指定路径。"
                          "可用 `csx doctor` 诊断。"}
    if sys.platform.startswith("linux") and cfg["settings"].get("sandbox_enabled", True):
        missing = [cmd for cmd in ("bwrap", "socat") if not shutil.which(cmd)]
        if missing:
            return {"ok": False, "stage": "dependencies",
                    "detail": f"Linux 安全沙箱缺少依赖：{', '.join(missing)}。"
                              "Ubuntu/Debian 可执行 `sudo apt install bubblewrap socat`，"
                              "安装后重试。"}

    ensure_sandbox_dirs()

    # 1) 虚拟登录（幂等；护栏在 oauth_forge 内）
    try:
        result, action = OF.ensure_virtual_login(
            C.sandbox_auth_dir(),
            email=OF.VIRTUAL_EMAIL,
            sandbox_root=C.sandbox_root(),
            real_dir=C.real_auth_dir())
    except Exception as e:  # noqa: BLE001
        log.warning("虚拟登录失败: %s", e)
        return {"ok": False, "stage": "virtual_login", "detail": str(e)}
    log.info("虚拟登录就绪（%s）：%s", action, result["enc_file"])

    # 2) 端口解析（外来占用自动顺延，绝不误杀外来进程）
    port = port or cfg["settings"]["sandbox_port"]
    if port == REAL_DEFAULT_PORT:
        return {"ok": False, "stage": "port",
                "detail": f"拒绝：端口 {REAL_DEFAULT_PORT} 是官方实例默认端口，"
                          f"沙箱必须使用其它端口。"}
    try:
        port = resolve_sandbox_port(port, bin_path)
    except RuntimeError as e:
        return {"ok": False, "stage": "port", "detail": str(e)}

    # 3) 启动
    #
    # 注意：不要传 --detached。
    # 本机实测 claude-science 0.1.61 在 Windows 上带 --detached 时 daemon
    # 会在 ~300ms 内自行退出（spawn.log: "start feedback: ended at +298ms"），
    # 端口短暂可连后马上关闭，表现为"启动成功但秒退"。
    # 去掉该参数后 daemon 稳定常驻。前台进程本身已用 DETACHED_PROCESS
    # creationflags 脱离控制台，等效且可靠。
    data_dir = C.sandbox_data_dir()
    env = sandbox_env(proxy_port, auth_secret)
    logf = C.logs_dir() / "sandbox.log"
    args = ["serve", "--data-dir", str(data_dir), "--port", str(port),
            "--host", "127.0.0.1", "--no-browser", "--no-auto-update"]

    # 安全沙箱开关：关闭后 Claude 对本机文件/网络有完全访问权、不弹审批。
    # 仅在用户明确要求"直接在自己的项目目录里工作"时才关。
    if not cfg["settings"].get("sandbox_enabled", True):
        args.append("--dangerously-no-sandbox")

    try:
        with open(logf, "a", encoding="utf-8") as lf:
            lf.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} serve "
                     f"port={port} data-dir={data_dir} "
                     f"sandbox={'on' if cfg['settings'].get('sandbox_enabled', True) else 'OFF'} "
                     f"====\n")
            lf.flush()
            p = subprocess.Popen(_cmd_prefix(bin_path) + args, env=env,
                                 stdout=lf, stderr=lf, close_fds=True,
                                 **_process_options(detached=True))
    except Exception as e:  # noqa: BLE001
        log.warning("沙箱启动失败: %s", e)
        return {"ok": False, "stage": "spawn", "detail": f"启动失败: {e}"}

    # 4) 就绪探活：TCP 端口 + status 命令双重确认
    url = None
    deadline = time.time() + wait_sec
    last_err = ""
    while time.time() < deadline:
        if _port_open(port):
            try:
                out = _run(bin_path, ["status", "--data-dir", str(data_dir)],
                           env, timeout=20)
                if out.returncode == 0 and out.stdout.strip():
                    try:
                        st = json.loads(out.stdout.strip())
                        if st.get("running"):
                            break
                    except json.JSONDecodeError:
                        break  # 端口已通即视为就绪
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
        if p.poll() is not None:
            tail = _tail_log(logf, 20)
            log.warning("Science 进程退出 code=%s，日志尾部: %s", p.returncode, tail)
            return {"ok": False, "stage": "exited",
                    "detail": f"Science 进程退出（code={p.returncode}）。"
                              f"日志尾部:\n{tail}"}
        time.sleep(1.0)
    else:
        tail = _tail_log(logf, 20)
        log.warning("沙箱就绪超时 %ds，最后错误: %s", wait_sec, last_err)
        return {"ok": False, "stage": "timeout",
                "detail": f"等待就绪超时（{wait_sec}s）{last_err}。日志尾部:\n{tail}"}

    # 5) 取登录链接（nonce 一次性签名浏览器标签页）
    try:
        out = _run(bin_path, ["url", "--data-dir", str(data_dir)], env, timeout=20)
        url = _first_url(out.stdout)
    except Exception as e:  # noqa: BLE001 - 取不到就退回端口根路径
        log.debug("取登录链接失败（退回端口根路径）: %s", e)
    if not url:
        url = f"http://127.0.0.1:{port}/"

    st = C.load_state()
    st.update({"sandbox_pid": p.pid, "sandbox_port": port,
               "sandbox_started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "mode": "third-party"})
    C.save_state(st)

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception as e:  # noqa: BLE001 - 打不开浏览器不影响服务
            log.warning("自动打开浏览器失败（可手动访问 %s）: %s", url, e)
    return {"ok": True, "stage": "ready", "url": url, "port": port,
            "pid": p.pid, "login_action": action}


def _tail_log(path: Path, n: int) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception as e:  # noqa: BLE001 - 无日志本身不是错误
        log.debug("读取日志尾部失败 %s: %s", path, e)
        return "(无日志)"


def _kill_by_cmdline(match: str):
    """兜底：仅结束命令行同时含 claude-science 与本沙箱 data-dir 的进程。"""
    if not match:
        return
    try:
        if os.name == "nt":
            ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "
                  f"'*{match}*' }} | ForEach-Object {{ taskkill /F /PID $_.ProcessId }}")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=30, **_process_options())
            return
        out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True,
                             text=True, timeout=15)
        for line in out.stdout.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) != 2:
                continue
            pid_text, cmdline = fields
            if match not in cmdline or "claude-science" not in cmdline.lower():
                continue
            pid = int(pid_text)
            if pid != os.getpid():
                os.kill(pid, signal.SIGTERM)
    except Exception as e:  # noqa: BLE001 - 兜底手段失败则 stop 命令已尽力
        log.debug("按命令行匹配杀进程失败: %s", e)


def stop_sandbox(port: int = None) -> dict:
    """停止沙箱 Science（先 stop 命令，兜底按 data-dir 命令行匹配）。"""
    cfg = C.load_config()
    port = port or cfg["settings"]["sandbox_port"]
    data_dir = C.sandbox_data_dir()
    bin_path = find_science_binary(cfg)
    stopped = False
    if bin_path:
        env = sandbox_env(cfg["settings"]["proxy_port"],
                          C.load_state().get("auth_secret") or "x")
        try:
            _run(bin_path, ["stop", "--data-dir", str(data_dir)], env, timeout=30)
            stopped = True
        except Exception as e:  # noqa: BLE001 - 后面还有命令行兜底
            log.debug("stop 命令失败（转用命令行兜底）: %s", e)
    # 兜底：命令行含沙箱 data-dir 的进程（路径含 .csswitch\\sandbox，与真实目录天然区分）
    _kill_by_cmdline(str(data_dir))
    for _ in range(20):
        if not _port_open(port):
            break
        time.sleep(0.25)
    st = C.load_state()
    st.pop("sandbox_pid", None)
    st.pop("sandbox_port", None)
    if st.get("mode") == "third-party":
        st["mode"] = None
    C.save_state(st)
    return {"ok": True, "stopped": stopped, "port": port}


def sandbox_status() -> dict:
    """沙箱状态。

    注意：端口通 != 沙箱在跑。本机可能有外部服务占用该端口（如 WSL 转发、
    其它开发服务），仅凭 TCP connect 会误报"运行中"。权威判据是
    `claude-science status --data-dir <沙箱 data-dir>` 返回 running=true；
    该命令不成立时才退回"端口通"作为弱信号，并在 detail 里标明不可信。
    """
    st = C.load_state()
    port = st.get("sandbox_port") or C.load_config()["settings"]["sandbox_port"]
    result = {"running": False, "port": port,
              "pid": st.get("sandbox_pid"),
              "started_at": st.get("sandbox_started_at"),
              "detail": ""}
    if not _port_open(port):
        return result
    data_dir = C.sandbox_data_dir()
    bin_path = find_science_binary()
    if bin_path and data_dir.exists():
        env = sandbox_env(C.load_config()["settings"]["proxy_port"],
                          C.load_state().get("auth_secret") or "x")
        try:
            out = _run(bin_path, ["status", "--data-dir", str(data_dir)],
                       env, timeout=15)
            if out.returncode == 0 and out.stdout.strip():
                running = bool(json.loads(out.stdout.strip()).get("running"))
                result["running"] = running
                result["detail"] = "" if running else "端口通但沙箱未运行"
                return result
        except Exception as e:  # noqa: BLE001 - 探测失败降级为弱信号
            log.debug("沙箱状态探测失败（降级为端口判定）: %s", e)
    # 降级：端口通，但无法确认归属
    result["running"] = True
    result["detail"] = (f"端口 {port} 被外部进程占用（未能确认为本沙箱），"
                        f"status 判定不可信；建议改 settings.sandbox_port")
    return result


# ---------------- 官方 Claude 模式 ----------------

def start_official(port: int = REAL_DEFAULT_PORT, open_browser: bool = True) -> dict:
    """官方模式：不注入任何代理环境，用真实登录启动/打开官方 Science。"""
    bin_path = find_science_binary()
    if not bin_path:
        return {"ok": False, "stage": "science_not_found",
                "detail": "未找到 claude-science 可执行文件。`csx doctor` 可诊断。"}
    env = dict(os.environ)  # 不覆盖 USERPROFILE / 不设 ANTHROPIC_BASE_URL
    # 已在运行 -> 直接 mint 新链接
    try:
        out = _run(bin_path, ["status"], env, timeout=20)
        if out.returncode == 0:
            try:
                if json.loads(out.stdout.strip()).get("running"):
                    out2 = _run(bin_path, ["open"], env, timeout=20)
                    return {"ok": True, "stage": "already-running",
                            "detail": "官方实例已在运行，已打开新标签页。",
                            "url": _first_url(out2.stdout)}
            except json.JSONDecodeError:
                pass
    except Exception as e:  # noqa: BLE001 - 探测失败就当没在跑
        log.debug("官方实例状态探测失败（当作未运行）: %s", e)
    logf = C.logs_dir() / "official.log"
    try:
        with open(logf, "a", encoding="utf-8") as lf:
            # 同start_sandbox：不传 --detached（Windows 上会导致秒退）
            p = subprocess.Popen(
                _cmd_prefix(bin_path) + ["serve", "--port", str(port),
                                         "--no-browser"],
                env=env, stdout=lf, stderr=lf, close_fds=True,
                **_process_options(detached=True))
    except Exception as e:  # noqa: BLE001
        log.warning("官方实例启动失败: %s", e)
        return {"ok": False, "stage": "spawn", "detail": f"启动失败: {e}"}
    deadline = time.time() + K.READY_WAIT_SEC
    while time.time() < deadline:
        if _port_open(port):
            break
        if p.poll() is not None:
            tail = _tail_log(logf, 20)
            log.warning("官方实例退出 code=%s: %s", p.returncode, tail)
            return {"ok": False, "stage": "exited",
                    "detail": f"官方实例退出（code={p.returncode}）。"
                              f"日志尾部:\n{tail}"}
        time.sleep(1.0)
    else:
        tail = _tail_log(logf, 20)
        log.warning("官方实例 %ds 内未就绪: %s", K.READY_WAIT_SEC, tail)
        return {"ok": False, "stage": "timeout",
                "detail": f"官方实例 {K.READY_WAIT_SEC}s 内未就绪。日志尾部:\n{tail}"}
    url = None
    try:
        out = _run(bin_path, ["url"], env, timeout=20)
        url = _first_url(out.stdout)
    except Exception as e:  # noqa: BLE001 - 取不到就退回端口根路径
        log.debug("取官方登录链接失败（退回端口根路径）: %s", e)
    url = url or f"http://127.0.0.1:{port}/"
    st = C.load_state()
    st.update({"official_port": port, "mode": "official",
               "official_started_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    C.save_state(st)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception as e:  # noqa: BLE001
            log.warning("自动打开浏览器失败（可手动访问 %s）: %s", url, e)
    return {"ok": True, "stage": "ready", "url": url, "port": port}


def _first_url(text: str):
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("http://") or line.startswith("https://"):
            return line
    return None


# 说明：v1.0.0 曾在本模块自定义 `def log(msg)`，现已由 csswitch.logging_setup
# 的统一 logger（模块顶部 `log`）取代，不再需要本地实现。
