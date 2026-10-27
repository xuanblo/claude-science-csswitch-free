# -*- coding: utf-8 -*-
"""统一日志：替代原先 proxy.py / sandbox.py 各自实现的 log()。

改造动机：项目里有 46 处 `except Exception`（其中 13 处是完全静默的
`except Exception: pass`）。一旦出问题，用户看到的日志是空的，无法自助
排障。本模块提供统一入口与两条纪律：

  1. **预期内的失败**（可选文件不存在、端口未监听、外部探测失败）
     用 `debug()`，只在 CSSWITCH_DEBUG=1 或 --debug 时输出，不打扰用户；
  2. **非预期的失败** 用 `warning()`，一定落盘并带上下文；
  3. **安全护栏失败必须 raise**（如 oauth_forge 的路径护栏）—— 那不是
     「可以忽略的异常」，绝不允许降级为 warning。

文件日志：默认写 `<CSSWITCH_HOME>/logs/<name>.log`，自动轮转。
日志中严禁出现 API Key —— 见 `_scrub()` 的兜底脱敏。
"""
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = set()
_MAX_BYTES = 2 * 1024 * 1024   # 单文件 2MB
_BACKUP_COUNT = 3              # 保留 3 份历史

# 日志里绝不出现的敏感串长度下限（避免把短 token 也打出来）
_SCRUB_MIN_LEN = 12


class _Formatter(logging.Formatter):
    """CLI 友好格式：[HH:MM:SS] [级别] 内容"""

    def format(self, record):
        ts = time.strftime("%H:%M:%S")
        level = "" if record.levelno == logging.INFO else f"[{record.levelname}] "
        return f"[{ts}] {level}{record.getMessage()}"


def _scrub(text: str) -> str:
    """兜底脱敏：把疑似 Key 的长串打码。

    调用纪律是「压根不把 Key 传进 log()」；这一层是防呆 —— 万一有人
    把上游返回的报文整段 log 出去，这里能挡住常见形态。
    """
    import re
    out = re.sub(r"(sk-[A-Za-z0-9_\-]{8,})", lambda m: m.group(1)[:3] + "***", text)
    out = re.sub(r"(?i)(bearer\s+)([A-Za-z0-9._\-]{%d,})" % _SCRUB_MIN_LEN,
                 lambda m: m.group(1) + "***", out)
    return out


def _log_dir():
    """取日志目录。延迟到真正写日志时才导入 config。

    必须惰性：config 模块在 import 期就会建 logger，若此处直接
    `from . import config`，会形成 logging_setup -> config ->
    logging_setup 的循环，config 尚未初始化完（无 logs_dir），
    导致文件日志静默失效。这里用函数内 import + 失败降级兜住。
    """
    try:
        from . import config as C
        d = C.logs_dir()
    except Exception:  # noqa: BLE001 - config 还在初始化 / 不可用
        return None
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception as e:  # noqa: BLE001 - 目录不可建（如只读介质）
        print(f"[csswitch] 日志目录不可用（仅输出到控制台）: {e}", file=sys.stderr)
        return None


class _LazyFileHandler(logging.Handler):
    """第一次写入时才解析日志目录并挂 RotatingFileHandler。

    为什么需要它：logger 在各模块 import 期就被创建，那时 config 可能
    尚未初始化完（循环依赖）。惰性解析让「创建 logger」与「解析路径」
    解耦，既避免循环导入，也不丢任何一条早期日志。
    """

    def __init__(self, name):
        super().__init__()
        self._logger_name = name
        self._inner = None
        self._warned = False

    def _resolve(self):
        if self._inner is not None:
            return self._inner
        d = _log_dir()
        if d is None:
            return None
        try:
            fh = RotatingFileHandler(
                str(d / f"{self._logger_name}.log"), maxBytes=_MAX_BYTES,
                backupCount=_BACKUP_COUNT, encoding="utf-8")
            fh.setFormatter(self.formatter or _Formatter())
            fh.setLevel(logging.DEBUG)
            self._inner = fh
            return fh
        except Exception as e:  # noqa: BLE001 - 文件日志不可用不应阻断功能
            if not self._warned:
                self._warned = True
                print(f"[csswitch] 文件日志不可用（仅输出到控制台）: {e}",
                      file=sys.stderr)
            return None

    def emit(self, record):
        target = self._resolve()
        if target is None:
            return
        target.emit(record)

    def close(self):
        if self._inner is not None:
            self._inner.close()
        super().close()


def get_logger(name: str = "csswitch") -> logging.Logger:
    """取（并首次配置）一个 logger。重复调用只生效一次。"""
    logger = logging.getLogger(name)
    if name in _CONFIGURED:
        return logger

    debug = os.environ.get("CSSWITCH_DEBUG", "").strip() not in ("", "0", "false")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.propagate = False
    fmt = _Formatter()

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    stream.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.addHandler(stream)

    # 文件日志：惰性解析路径（见 _LazyFileHandler 的说明），
    # 写失败也不能让主流程挂掉（磁盘满/只读介质）
    file_handler = _LazyFileHandler(name)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    _CONFIGURED.add(name)
    return logger


def debug(logger, msg, *args):
    """预期内失败：默认静默，CSSWITCH_DEBUG=1 时可见。"""
    logger.debug(_scrub(msg % args if args else msg))


def info(logger, msg, *args):
    logger.info(_scrub(msg % args if args else msg))


def warning(logger, msg, *args):
    """非预期失败：一定落盘，带上下文。"""
    logger.warning(_scrub(msg % args if args else msg))


def exception(logger, msg, *args):
    """带 traceback 的异常记录（替代裸 print + 静默 pass）。"""
    logger.exception(_scrub(msg % args if args else msg))


# 预置两个常用 logger，避免调用方到处 get_logger()
proxy_log = None    # 代理侧：proxy / server / upstream / modelmap
cli_log = None      # CLI 侧：cli / sandbox / oauth_forge


def init(names=("proxy", "cli")):
    """预初始化一组 logger，返回 {name: logger}。"""
    return {n: get_logger(n) for n in names}
