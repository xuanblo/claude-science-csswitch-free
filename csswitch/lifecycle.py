# -*- coding: utf-8 -*-
"""沙箱生命周期管理（供 Web 面板一键启动/停止 Claude Science）。

为什么单独一个模块：
  - 启动要等就绪探活（实测 30~90 秒），绝不能占着 HTTP 线程同步等；
  - 需要把"进行中/成功/失败"的状态暴露给前端轮询；
  - 需要防重复点击（并发启动会把端口搞乱）。

用模块级单例状态 + 后台线程实现，不引入额外依赖。
"""
import threading
import time

from . import logging_setup as L
from . import sandbox as S

log = L.get_logger("server")


def _hostgrant():
    """延迟导入 hostgrant（它反过来依赖本模块取登录链接）。"""
    from . import hostgrant as HG
    return HG


# 任务状态机：idle -> running -> ready / failed
_IDLE = "idle"
_RUNNING = "running"
_READY = "ready"
_FAILED = "failed"
_STOPPING = "stopping"

_lock = threading.Lock()
_task = {
    "state": _IDLE,
    "detail": "",
    "url": None,
    "port": None,
    "pid": None,
    "started_at": None,
    "finished_at": None,
}
_worker = None

# 登录 URL 的短 TTL 缓存（见 current_url 的说明）。刻意做得很短，
# 因为 nonce 3 分钟就过期，这里的目的只是省掉高频轮询时的进程 fork。
_url_cache = {"url": "", "at": 0.0}


def snapshot() -> dict:
    """当前任务状态（供前端轮询）。"""
    with _lock:
        return dict(_task)


def is_busy() -> bool:
    with _lock:
        return _task["state"] in (_RUNNING, _STOPPING)


def _set(**kw):
    with _lock:
        _task.update(kw)


def _run_start(port: int, open_browser: bool):
    """后台执行启动。异常必须捕获，否则线程静默死掉面板永远转圈。"""
    from . import config as C
    try:
        st = C.load_state()
        secret = st.get("auth_secret") or ""
        cfg = C.load_config()
        proxy_port = cfg["settings"]["proxy_port"]
        _set(state=_RUNNING, detail="正在准备虚拟登录并拉起 Science ...",
             started_at=time.strftime("%H:%M:%S"), finished_at=None,
             url=None, port=port, pid=None)
        r = S.start_sandbox(proxy_port=proxy_port, auth_secret=secret,
                            port=port, open_browser=open_browser)
        if r.get("ok"):
            _set(state=_READY,
                 detail=f"已就绪（端口 {r.get('port')}）",
                 url=None, port=r.get("port"),
                 pid=r.get("pid"),
                 finished_at=time.strftime("%H:%M:%S"))
            # 不缓存启动那一刻的 URL：nonce 3 分钟即过期，派发旧链必然
            # 让用户看到 "this link has expired"。前端点"打开"时现取。
            _url_cache.update(url="", at=0.0)
            # daemon 换了进程，旧的 host-grant 会话 cookie 必然失效
            _hostgrant().reset_session()
            log.info("[panel] 沙箱启动成功 port=%s pid=%s",
                     r.get("port"), r.get("pid"))
        else:
            _set(state=_FAILED,
                 detail=f"启动失败（{r.get('stage')}）：{r.get('detail')}",
                 finished_at=time.strftime("%H:%M:%S"))
            log.warning("[panel] 沙箱启动失败 stage=%s", r.get("stage"))
    except Exception as e:  # noqa: BLE001 - 后台线程必须兜住，否则面板永远转圈
        _set(state=_FAILED, detail=f"启动异常：{e}",
             finished_at=time.strftime("%H:%M:%S"))
        log.exception("[panel] 沙箱启动异常")


def _run_stop(port: int):
    try:
        _set(state=_STOPPING, detail="正在停止沙箱 ...")
        r = S.stop_sandbox(port=port)
        _set(state=_IDLE,
             detail="已停止" if r.get("stopped") else "未在运行",
             url=None, pid=None, finished_at=time.strftime("%H:%M:%S"))
        _hostgrant().reset_session()
        log.info("[panel] 沙箱已停止 port=%s", port)
    except Exception as e:  # noqa: BLE001
        _set(state=_FAILED, detail=f"停止异常：{e}",
             finished_at=time.strftime("%H:%M:%S"))
        log.exception("[panel] 沙箱停止异常")


def start(port: int = None, open_browser: bool = True) -> dict:
    """请求启动沙箱。立即返回，实际工作在后台线程。"""
    global _worker
    with _lock:
        if _task["state"] == _RUNNING:
            return {"ok": False, "detail": "正在启动中，请稍候",
                    "state": _RUNNING}
        # 已就绪且进程仍在 -> 不重复拉起
        ss = S.sandbox_status()
        if ss.get("running"):
            return {"ok": False, "state": _READY,
                    "detail": f"沙箱已在运行（端口 {ss['port']}）",
                    # 同上：不派发可能已过期的 URL
                    "url": "", "port": ss["port"]}
        _worker = threading.Thread(target=_run_start, args=(port, open_browser),
                                   daemon=True, name="csx-sandbox-start")
        _worker.start()
    return {"ok": True, "state": _RUNNING, "detail": "启动任务已开始"}


def stop(port: int = None) -> dict:
    """请求停止沙箱。"""
    global _worker
    with _lock:
        if _task["state"] == _RUNNING:
            return {"ok": False, "detail": "正在启动中，请先等待完成"}
        _worker = threading.Thread(target=_run_stop, args=(port,),
                                   daemon=True, name="csx-sandbox-stop")
        _worker.start()
    return {"ok": True, "state": _STOPPING, "detail": "停止任务已开始"}


def current_url(max_age_s: float = 45.0) -> str:
    """取沙箱的**新鲜**登录 URL。

    重要：nonce 是**一次性密码，3 分钟就过期**（Science 自己也这么提示：
    "This link is a one-time password, not a bookmark: it logs one browser tab
    in, then expires (3 min)"）。曾经实现里"有缓存就直接返回"，导致面板
    几小时后仍在派发启动时那条旧链接，用户点开必然看到
    "this link has expired"。实测每次调用 `claude-science url` 都会返回
    全新 nonce。

    这里仍保留一个**很短**的 TTL（默认 45 秒，远小于 3 分钟有效期），
    纯粹是为了避免面板 1.5 秒轮询时反复 fork `claude-science url` 进程；
    超过 45 秒必定重新索取，绝不会派发一条用掉的链接。
    """
    now = time.time()
    with _lock:
        if (_url_cache["url"] and now - _url_cache["at"] < max_age_s):
            return _url_cache["url"]

    from . import config as C
    cfg = C.load_config()
    bin_path = S.find_science_binary(cfg)
    data_dir = C.sandbox_data_dir()
    port = cfg["settings"]["sandbox_port"]
    if not (bin_path and data_dir.exists() and S._port_open(port)):
        with _lock:
            _url_cache.update(url="", at=now)
        return ""
    env = S.sandbox_env(cfg["settings"]["proxy_port"],
                       C.load_state().get("auth_secret") or "x")
    try:
        out = S._run(bin_path, ["url", "--data-dir", str(data_dir)],
                     env, timeout=25)
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("http://") or line.startswith("https://"):
                with _lock:
                    _url_cache.update(url=line, at=time.time())
                return line
    except Exception as e:  # noqa: BLE001 - 取不到就返回空让前端提示
        log.debug("取登录链接失败: %s", e)
    with _lock:
        _url_cache.update(url="", at=now)
    return ""