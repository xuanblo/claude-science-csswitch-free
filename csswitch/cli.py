# -*- coding: utf-8 -*-
"""CSswitch-Win 命令行界面。

命令一览：
  csx doctor                       环境诊断（Python/依赖/Science 安装/端口/路径）
  csx list                         列出全部第三方配置
  csx add [--name --kind --key ...]   新建配置（缺参数进交互）
  csx remove <name>                删除配置
  csx use <name>                   切换当前配置（代理在跑则热切换，Science 不重启）
  csx verify <name>                只校验 Key，不切换
  csx start [--sandbox-port N]     一键开始：起代理 + 沙箱 Science + 开浏览器
  csx stop                         停止沙箱与代理
  csx official [--port N]          官方 Claude 模式（真实登录，不走代理）
  csx status                       总览状态
  csx proxy-start / proxy-stop     单独控制代理（调试用）
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from . import __version__
from . import config as C
from . import constants as K
from . import logging_setup as L
from . import providers as P
from . import sandbox as S

log = L.get_logger("cli")

CREATE_NO_WINDOW = 0x08000000
ROOT = Path(__file__).resolve().parent.parent


def _ok(msg):
    print(f"  ✓ {msg}")


def _err(msg):
    print(f"  ✗ {msg}")


def _info(msg):
    print(f"  · {msg}")


def _warn(msg):
    print(f"  ! {msg}")


def _hdr(title):
    print()
    print(f"── {title} " + "─" * max(0, 56 - len(title)))


def _prompt(msg: str, default: str = "", required: bool = False) -> str:
    """交互提示；非 TTY（脚本/管道）时用默认值，必填项缺失则报错退出。"""
    if sys.stdin.isatty():
        try:
            v = input(msg).strip()
            return v or default
        except EOFError:
            return default
    if required and not default:
        print(f"  ✗ 非交互环境缺少必填参数：{msg.strip()}", file=sys.stderr)
        sys.exit(2)
    return default


# ---------------- 代理进程管理 ----------------

def proxy_health() -> dict:
    """探测本机代理是否在跑（带 path secret 的 /health）。"""
    st = C.load_state()
    port = st.get("proxy_port") or C.load_config()["settings"]["proxy_port"]
    secret = st.get("auth_secret")
    if not secret:
        return {"running": False, "port": port}
    url = f"http://127.0.0.1:{port}/{secret}/health"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            if r.status == 200:
                body = json.loads(r.read())
                return {"running": True, "port": port, "provider": body.get("provider"),
                        "admin_token": st.get("admin_token")}
    except Exception:
        pass
    return {"running": False, "port": port}


def proxy_start(provider: str = None) -> dict:
    """后台启动代理进程，等待 /health 就绪。"""
    cfg = C.load_config()
    port = cfg["settings"]["proxy_port"]
    h = proxy_health()
    if h["running"]:
        return {"ok": True, "already": True, "port": h["port"],
                "provider": h.get("provider")}
    if S._port_open(port):
        return {"ok": False, "detail": f"端口 {port} 被其它进程占用，请更换 "
                                       f"settings.proxy_port 或结束该进程。"}
    C.logs_dir().mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    # 关键：剥离父进程的代理变量。若继承 https_proxy 等，代理进程访问上游
    # 也会被转发到那个代理，形成回环并报 os error 10061（本机实测踩过）。
    for k in K.PROXY_ENV_VARS:
        env.pop(k, None)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    args = [sys.executable, "-m", "csswitch.proxy", "--port", str(port)]
    if provider:
        args += ["--provider", provider]
    logf = open(C.logs_dir() / "proxy.log", "a", encoding="utf-8")
    try:
        p = subprocess.Popen(args, cwd=str(ROOT), env=env, stdout=logf,
                             stderr=logf, close_fds=True,
                             **S._process_options(detached=True))
    except Exception as e:  # noqa: BLE001
        logf.close()
        log.warning("代理进程创建失败: %s", e)
        return {"ok": False, "detail": f"代理启动失败: {e}"}
    finally:
        # Popen 已持有 fd，父进程这份可以关掉，避免句柄泄漏
        logf.close()
    deadline = time.time() + 20
    while time.time() < deadline:
        h = proxy_health()
        if h["running"]:
            return {"ok": True, "already": False, "port": h["port"],
                    "provider": h.get("provider"), "pid": p.pid}
        if p.poll() is not None:
            tail = S._tail_log(C.logs_dir() / "proxy.log", 15)
            log.warning("代理进程退出 code=%s: %s", p.returncode, tail)
            return {"ok": False, "detail": f"代理进程退出（code={p.returncode}）:\n{tail}"}
        time.sleep(0.5)
    return {"ok": False, "detail": "代理 20s 内未就绪，详见 ~/.csswitch/logs/proxy.log"}


def proxy_stop() -> dict:
    st = C.load_state()
    pid = st.get("proxy_pid")
    killed = False
    if pid:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/PID", str(pid),
                                "/T"], capture_output=True,
                               creationflags=CREATE_NO_WINDOW)
            else:
                os.kill(pid, 15)
            killed = True
        except Exception as e:  # noqa: BLE001 - 进程可能已自行退出
            log.debug("代理进程结束失败（可能已退出）: %s", e)
    # 清掉 state.json 里的 pid，避免下次 stop 误杀被系统复用的 pid
    st = C.load_state()
    st.pop("proxy_pid", None)
    C.save_state(st)
    return {"ok": True, "killed": killed, "pid": pid}


def _admin_call(path: str, body: dict) -> dict:
    h = proxy_health()
    if not h["running"]:
        return {"ok": False, "detail": "代理未运行"}
    token = h.get("admin_token")
    req = urllib.request.Request(
        f"http://127.0.0.1:{h['port']}/__admin/{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-csx-admin": token or ""},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception as ex:  # noqa: BLE001
            log.debug("admin 错误响应解析失败: %s", ex)
            return {"ok": False, "detail": f"admin API HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        log.debug("admin 调用失败: %s", e)
        return {"ok": False, "detail": str(e)}


# ---------------- 命令实现 ----------------

def cmd_doctor(args):
    _hdr(f"CSswitch-Win 环境诊断 (v{__version__})")
    py = sys.version.split()[0]
    _info(f"Python: {py}  ({sys.executable})")
    try:
        import cryptography
        _ok(f"cryptography: {cryptography.__version__}")
    except ImportError:
        _err("cryptography 未安装 → 运行: pip install -r requirements.txt")
    cfg = C.load_config()
    bin_path = S.find_science_binary(cfg)
    if bin_path:
        _ok(f"claude-science: {bin_path}")
        try:
            out = subprocess.run(S._cmd_prefix(bin_path) + ["--version"],
                                 capture_output=True, text=True, timeout=20,
                                 **S._process_options())
            ver = (out.stdout or out.stderr).strip().splitlines()
            _info(f"版本: {ver[0] if ver else '(未知)'}")
        except Exception as e:  # noqa: BLE001
            log.debug("版本探测失败: %s", e)
            _info(f"版本探测失败: {e}")
    else:
        _err("未找到 claude-science。请安装当前系统的原生版 Claude Science 后重试；"
             "或把路径写进配置文件的 settings.science_bin")
    _info(f"操作系统: {sys.platform}")
    if sys.platform.startswith("linux"):
        missing = [cmd for cmd in ("bwrap", "socat") if not shutil.which(cmd)]
        if missing:
            _warn(f"Linux 安全沙箱缺少依赖: {', '.join(missing)}；"
                  "Ubuntu/Debian 可运行 sudo apt install bubblewrap socat")
        else:
            _ok("Linux 安全沙箱依赖: bubblewrap / socat")
    real = C.real_auth_dir()
    _info(f"真实登录目录: {real}  ({'存在' if real.exists() else '不存在'}，"
          f"本工具绝不读写)")
    _info(f"沙箱目录: {C.sandbox_root()}")
    _info(f"沙箱 auth_dir: {C.sandbox_auth_dir()}")
    _info(f"沙箱 data-dir: {C.sandbox_data_dir()}")
    h = proxy_health()
    if h["running"]:
        _ok(f"代理: 运行中 127.0.0.1:{h['port']}  provider={h.get('provider')}")
    else:
        _info(f"代理: 未运行（端口 {h['port']}）")
    ss = S.sandbox_status()
    if ss["running"] and ss.get("detail"):
        _warn(f"沙箱 Science: 端口 {ss['port']} 被外部进程占用，"
              f"并非本工具的沙箱；请更换 settings.sandbox_port")
    elif ss["running"]:
        _ok(f"沙箱 Science: 运行中 端口 {ss['port']}")
    else:
        _info(f"沙箱 Science: 未运行（端口 {ss['port']}）")
    if S._port_open(S.REAL_DEFAULT_PORT):
        _info(f"官方实例: 端口 {S.REAL_DEFAULT_PORT} 已占用（官方 Science 可能在运行）")
    n = len(cfg["providers"])
    _info(f"配置: {n} 条第三方配置，当前 = {cfg.get('current') or '(未设置)'}")
    _info(f"日志目录: {C.logs_dir()}（CSSWITCH_DEBUG=1 可看调试日志）")
    print()
    return 0


def cmd_list(args):
    cfg = C.load_config()
    _hdr("第三方配置列表")
    if not cfg["providers"]:
        _info("（空）用 `csx add` 新建一条配置")
        print()
        return 0
    print(f"  {'':2} {'名称':<18} {'类型':<18} {'上游模型':<24} {'Key':<14} 端点")
    print("  " + "-" * 100)
    for p in cfg["providers"]:
        cur = "▶" if p["name"] == cfg.get("current") else " "
        models = p.get("models") or []
        model = p.get("model") or (models[0] if models else "-")
        print(f"  {cur} {p['name']:<18} {p['kind']:<18} {model:<24} "
              f"{C.mask_key(p.get('key','')):<14} {p.get('base_url','')}")
    print()
    return 0


def cmd_add(args):
    cfg = C.load_config()
    name = args.name or _prompt("配置名称（如 ds-pro）: ", required=True)
    if C.find_provider(cfg, name):
        _err(f"配置名已存在: {name}")
        return 1
    kind = args.kind
    if not kind:
        print("\n可选类型:")
        for i, k in enumerate(P.all_kinds(), 1):
            print(f"  {i:2}. {k:<18} {P.kind_display(k)}")
        sel = _prompt(f"选择类型 [1-{len(P.all_kinds())}]（默认 1=deepseek）: ")
        try:
            kind = P.all_kinds()[int(sel) - 1]
        except (ValueError, IndexError):
            kind = "deepseek"
    tpl = P.template_for(kind)
    key = args.key or _prompt(f"API Key（{tpl.get('key_hint','')}）: ", required=True)
    model = args.model or _prompt(
        f"默认模型（回车 = {tpl['default_model']}）: ", default=tpl["default_model"])
    base_url = args.base_url
    if not base_url and tpl.get("passthrough"):
        base_url = _prompt("上游 base_url（如 https://api.example.com/v1）: ",
                           required=True)
    try:
        rec = C.new_provider_record(name, kind, key, model=model, base_url=base_url)
        C.add_provider(cfg, rec)
    except ValueError as e:
        _err(str(e))
        return 1
    _ok(f"已保存配置 {name}（{kind}），Key 仅存本机 ~/.csswitch/config.json")
    if not cfg.get("current"):
        _info("已自动设为当前配置")
    _info(f"提示：`csx use {name}` 切换；`csx start` 一键开始")
    print()
    return 0


def cmd_remove(args):
    cfg = C.load_config()
    if not C.find_provider(cfg, args.name):
        _err(f"配置不存在: {args.name}")
        return 1
    C.remove_provider(cfg, args.name)
    _ok(f"已删除 {args.name}")
    if cfg.get("current"):
        _info(f"当前配置 = {cfg['current']}")
    print()
    return 0


def cmd_verify(args):
    cfg = C.load_config()
    rec = C.find_provider(cfg, args.name)
    if not rec:
        _err(f"配置不存在: {args.name}")
        return 1
    _info(f"校验 {args.name} 的 Key（最小请求，花费 ≈ 0）...")
    from .proxy import verify_provider
    v = verify_provider(args.name)
    if v["ok"]:
        _ok(f"通过：{v['detail']}")
    else:
        _err(f"未通过：{v['detail']}")
    print()
    return 0 if v["ok"] else 1


def cmd_use(args):
    cfg = C.load_config()
    rec = C.find_provider(cfg, args.name)
    if not rec:
        _err(f"配置不存在: {args.name}")
        return 1
    h = proxy_health()
    if h["running"]:
        r = _admin_call("switch", {"provider": args.name})
        if r.get("switched"):
            _ok(f"已热切换 -> {args.name}（ Science 无需重启，下一条请求即走新模型 ）")
            v = r.get("verify") or {}
            if v.get("status") == 429:
                _info("注意：Key 有效但当前被限流")
            print()
            return 0
        _err(f"切换失败：{r.get('detail') or r}")
        print()
        return 1
    C.set_current(cfg, args.name)
    _ok(f"已设为当前配置（代理未运行，将在下次 `csx start` 时生效）")
    _info(f"上游: {rec.get('base_url')}  模型: {rec.get('model')}")
    print()
    return 0


def cmd_where(args):
    """诊断 Claude Science 的环境到底装在哪个盘，并给出迁移指引。

    新电脑上装 Science 时环境默认落C 盘，是因为 ``claude-science`` 的
    ``--data-dir`` 默认值是 ``~/.claude-science``，而Windows 上 ``~``
    就是 ``%USERPROFILE%``。所以要改的是 USERPROFILE，而不是去翻
    Science 的安装参数。
    """
    _hdr("Claude Science 环境位置")

    def _size(p, budget=20000):
        """目录体积（MB）。

        conda 环境动辄十几万文件，全量遍历要几十秒。这里设预算：
        超过就按已扫到的部分估算并注明，避免诊断命令像卡死。
        """
        tot = n = 0
        for root, _d, files in os.walk(p, onerror=lambda e: None):
            for f in files:
                try:
                    tot += (Path(root) / f).stat().st_size
                    n += 1
                except OSError:
                    pass
            if n > budget:
                break
        return n, tot, n > budget

    if os.name == "nt":
        up = os.environ.get("USERPROFILE", "")
        _info(f"USERPROFILE = {up or '(未设置)'}")
        _info(f"os.homedir() = {Path.home()}   <- Science 按它推导安装位置")
        if (up[:1] or "C").upper() == "C":
            _warn("USERPROFILE 仍在 C 盘：新装 Science 会把约 10 GB 的conda "
                  "环境落到 C 盘")
        else:
            _ok(f"USERPROFILE 指向 {up[:1].upper()} 盘，新装的环境会落在那里")
    else:
        _info(f"HOME = {os.environ.get('HOME', '') or '(未设置)'}")
        _info(f"Path.home() = {Path.home()}   <- Science 按它推导安装位置")
        _info("macOS/Linux 可用 CSSWITCH_HOME 改变本工具的数据目录，"
              "无需修改系统 HOME")

    real = C.real_auth_dir()
    if real.is_dir():
        # 真实登录目录是护栏对象（本工具绝不读写），这里只判断它**在不在**
        # 以及有没有实质内容，纯信息展示，不做任何修改
        cd = real / "conda"
        n, tot, partial = _size(cd) if cd.is_dir() else (0, 0, False)
        if tot > 0:
            note = "（已抽样）" if partial else ""
            _warn(f"官方目录存在: {real}")
            _info(f"    其中 conda {n:,} 个文件 / "
                  f"{tot / 1024 ** 3:.2f} GB{note}")
            _info("    提示：若你只用本工具跑沙箱，这份通常是初次安装的残留，"
                  "确认无会话数据后可删（先备份 config 类小文件）")
        else:
            # Science 被启动过就会重建空壳目录，0 内容说明它没实际使用过
            _info(f"官方目录: {real}（空目录，未实际使用，可忽略）")
    else:
        _info(f"官方目录: {real}（不存在，很好）")

    sb = C.sandbox_auth_dir()
    if sb.is_dir():
        n, tot, partial = _size(sb)
        note = "（已抽样）" if partial else ""
        _ok(f"本工具沙箱 auth_dir: {sb}")
        _info(f"    {n:,} 个文件 / {tot / 1024 ** 3:.2f} GB{note}")
    else:
        _info(f"本工具沙箱 auth_dir: {sb}（尚未创建，先跑 csx start）")

    print()
    if os.name == "nt":
        _hdr("新电脑上让它装到 D 盘")
        print("""
根因：claude-science 的 --data-dir 默认是 ~/.claude-science，
     Windows 上 ~ = %USERPROFILE%，所以要改USERPROFILE。

做法（**不要**改系统级 USERPROFILE —— 那会把文档/桌面/应用数据
      一起搬走，几乎所有软件都会受影响）：
  在启动 Science 的快捷方式或脚本里加两行，再首次启动它：

      set "USERPROFILE=D:\\ClaudeScience-Data"
      set "HOME=D:\\ClaudeScience-Data"

  本项目的 便携启动.bat 已内置沙箱隔离（数据都在 D:\\CSSwitch-Win-Data），
  所以用本工具启动不需要额外配置。

已装好的要挪走：用 robocopy **复制**（别剪切，conda 环境内有绝对
路径依赖），验证新位置能跑之后再删旧目录。
""".rstrip())
    else:
        _hdr("macOS / Linux 数据位置")
        print("""
本工具的数据目录由 CSSWITCH_HOME 决定，默认是 ~/.csswitch-win。
沙箱 HOME 位于 $CSSWITCH_HOME/sandbox/home，Science 的会话与分析环境
均在该沙箱内。需要换盘时先执行 csx stop，再复制整个数据目录并更新
CSSWITCH_HOME；不要直接剪切，conda 环境内有绝对路径依赖。
""".rstrip())
    return 0


def cmd_start(args):
    cfg = C.load_config()
    cur = C.current_provider(cfg)
    if not cur:
        _err("尚未创建任何第三方配置。先 `csx add` 建一条，再 `csx start`。")
        return 1
    _hdr("一键开始（第三方模型模式）")
    _info(f"当前配置: {cur['name']}  ({cur['kind']})  "
          f"Key={C.mask_key(cur.get('key',''))}")
    _info(f"上游: {cur.get('base_url')}")
    r = proxy_start()
    if not r["ok"]:
        _err(f"代理启动失败: {r['detail']}")
        return 1
    if r.get("already"):
        _ok(f"代理已在运行: 127.0.0.1:{r['port']}  provider={r.get('provider')}")
        if r.get("provider") != cur["name"]:
            sw = _admin_call("switch", {"provider": cur["name"]})
            if sw.get("switched"):
                _ok(f"代理当前 provider 与配置不一致，已热切换 -> {cur['name']}")
            else:
                _info(f"代理 provider 切换未生效: {sw.get('detail')}")
    else:
        _ok(f"代理已启动: 127.0.0.1:{r['port']}  provider={r.get('provider')}")
    st = C.load_state()
    r2 = S.start_sandbox(proxy_port=r["port"],
                         auth_secret=st.get("auth_secret") or "x",
                         port=args.sandbox_port)
    if not r2["ok"]:
        _err(f"沙箱启动失败[{r2['stage']}]: {r2['detail']}")
        _info("代理保持运行；排障见 ~/.csswitch/logs/sandbox.log")
        return 1
    _ok(f"沙箱 Science 就绪: 端口 {r2['port']}（虚拟登录 {r2['login_action']}）")
    _ok(f"已在浏览器打开: {r2['url']}")
    print()
    print("  ┌────────────────────────────────────────────┐")
    print("  │  使用说明                                   │")
    print("  │  1. 浏览器标签页即 Science（本地虚拟登录）    │")
    print("  │  2. 顶部模型选择器选你配置的真实模型          │")
    print("  │  3. 随时 `csx use <其它配置>` 热切换，       │")
    print("  │     进行中的会话下一条请求即走新模型          │")
    print("  │  4. `csx official` 切回官方 Claude           │")
    print("  └────────────────────────────────────────────┘")
    print()
    return 0


def cmd_stop(args):
    _hdr("停止")
    r = S.stop_sandbox()
    _ok(f"沙箱 Science 已停止（端口 {r['port']}）")
    r2 = proxy_stop()
    if r2.get("killed"):
        _ok(f"代理已停止（pid={r2['pid']}）")
    else:
        _info("代理未在运行（或已由其它方式停止）")
    print()
    return 0


def cmd_official(args):
    _hdr("官方 Claude 模式")
    _info("先停止第三方链路（代理 + 沙箱）...")
    S.stop_sandbox()
    proxy_stop()
    r = S.start_official(port=args.port)
    if not r["ok"]:
        _err(f"官方模式启动失败[{r['stage']}]: {r['detail']}")
        return 1
    _ok(f"官方 Science 就绪: {r.get('url')}")
    _info("官方模式使用你的真实 Claude 登录，推理不走本地代理。")
    print()
    return 0


def cmd_status(args):
    cfg = C.load_config()
    _hdr("运行状态")
    cur = C.current_provider(cfg)
    h = proxy_health()
    print(f"  代理      : {'运行中 127.0.0.1:%s  provider=%s' % (h['port'], h.get('provider')) if h['running'] else '未运行'}")
    ss = S.sandbox_status()
    if ss["running"] and ss.get("detail"):
        print(f"  沙箱Science: 端口 {ss['port']} 被外部进程占用（非本工具沙箱）")
    else:
        print(f"  沙箱Science: {'运行中 端口 %s (pid=%s)' % (ss['port'], ss.get('pid')) if ss['running'] else '未运行'}")
    print(f"  当前配置  : {cur['name'] if cur else '(未设置)'}"
          f"{'  →  ' + cur.get('base_url','') if cur else ''}")
    print(f"  Science   : {S.find_science_binary(cfg) or '未找到'}")
    st = C.load_state()
    print(f"  模式      : {st.get('mode') or '(未启动)'}")
    print()
    return 0


def cmd_proxy_start(args):
    r = proxy_start(args.provider)
    if r["ok"]:
        _ok(f"代理运行中: 127.0.0.1:{r['port']}  provider={r.get('provider')}"
            f"{'（本就已在运行）' if r.get('already') else ''}")
        print()
        return 0
    _err(r["detail"])
    print()
    return 1


def cmd_proxy_stop(args):
    r = proxy_stop()
    if r.get("killed"):
        _ok(f"代理已停止（pid={r['pid']}）")
    else:
        _info("代理未在运行")
    print()
    return 0


def cmd_panel(args):
    """打开 Web 控制台（图形化配置第三方模型）。"""
    h = proxy_health()
    if not h["running"]:
        r = proxy_start()
        if not r.get("ok"):
            _err(f"代理未运行且启动失败：{r.get('detail')}")
            return 1
        h = proxy_health()
    token = h.get("admin_token") or C.load_state().get("admin_token") or ""
    url = f"http://127.0.0.1:{h['port']}/?token={token}"
    _hdr("Web 控制台")
    _ok(f"代理已就绪: 127.0.0.1:{h['port']}  provider={h.get('provider')}")
    print(f"\n  在浏览器打开以下地址即可图形化配置:\n")
    print(f"    {url}\n")
    _info("该地址含一次性管理令牌，只对本机有效；勿分享")
    if not args.no_open:
        try:
            webbrowser.open(url)
            _ok("已打开浏览器")
        except Exception as e:  # noqa: BLE001
            _warn(f"自动打开失败，请手工复制上面的地址：{e}")
    print()
    return 0


# ---------------- 入口 ----------------

def build_parser():
    ap = argparse.ArgumentParser(
        prog="csx", description="CSswitch：Windows / macOS / Linux 版 "
                                "Claude Science 第三方模型切换工具")
    ap.add_argument("--version", action="version",
                    version=f"CSswitch-Win {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("doctor", help="环境诊断")

    sub.add_parser("where", help="诊断 Claude Science 环境装在哪个盘")

    sub.add_parser("list", help="列出全部配置")

    p = sub.add_parser("add", help="新建第三方配置")
    p.add_argument("--name", default=None)
    p.add_argument("--kind", default=None, choices=P.all_kinds())
    p.add_argument("--key", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--base-url", dest="base_url", default=None)

    p = sub.add_parser("remove", help="删除配置")
    p.add_argument("name")

    p = sub.add_parser("verify", help="只校验 Key，不切换")
    p.add_argument("name")

    p = sub.add_parser("use", help="切换当前配置（热切换）")
    p.add_argument("name")

    p = sub.add_parser("start", help="一键开始：代理 + 沙箱 Science + 浏览器")
    p.add_argument("--sandbox-port", dest="sandbox_port", type=int, default=None)

    sub.add_parser("stop", help="停止沙箱与代理")

    p = sub.add_parser("official", help="官方 Claude 模式（真实登录）")
    p.add_argument("--port", type=int, default=S.REAL_DEFAULT_PORT)
    sub.add_parser("status", help="总览状态")

    p = sub.add_parser("proxy-start", help="单独启动代理")
    p.add_argument("--provider", default=None)

    sub.add_parser("proxy-stop", help="单独停止代理")

    p = sub.add_parser("panel", help="打开 Web 控制台（图形化配置模型）")
    p.add_argument("--no-open", action="store_true", help="只打印地址不打开浏览器")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    table = {
        "doctor": cmd_doctor, "list": cmd_list, "add": cmd_add,
        "remove": cmd_remove, "verify": cmd_verify, "use": cmd_use,
        "start": cmd_start, "stop": cmd_stop, "official": cmd_official,
        "status": cmd_status, "proxy-start": cmd_proxy_start,
        "proxy-stop": cmd_proxy_stop,
        "panel": cmd_panel, "where": cmd_where,
    }
    fn = table.get(args.cmd)
    if fn is None:
        build_parser().print_help()
        return 0
    try:
        return fn(args) or 0
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130
    except C.ForeignConfigError as e:
        # 配置目录冲突：给可操作指引，不打traceback（这不是程序 bug）
        print(f"\n  ✗ 配置目录冲突，已拒绝操作以保护官方配置\n")
        for line in str(e).splitlines():
            print(f"    {line}")
        return 2
    except Exception as e:  # noqa: BLE001 - CLI 顶层兜底：给可读报错而非 traceback
        log.exception("命令 %s 执行失败", args.cmd)
        print(f"\n  ✗ 执行出错: {e}\n    详见 {C.logs_dir() / 'cli.log'}"
              f"（或设 CSSWITCH_DEBUG=1 重跑）", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
