# -*- coding: utf-8 -*-
"""pytest 适配层：把自研自测套件包成 pytest 用例。

为什么不直接用 pytest 写测试：
  `tests/selftest.py` 是面向用户的双击友好套件 —— 零依赖、中文输出、
  分段清晰、无需安装测试框架。保留它作为主入口。

本文件只做一件事：让 `pytest tests/` 与 CI 能断言「全部通过」，
并把失败输出完整打进 pytest 的报告里（否则关键上下文会被吞掉）。

运行：
    python tests/selftest.py     # 直接跑自研套件（推荐给用户）
    pytest tests/                # CI 用
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELFTEST = ROOT / "tests" / "selftest.py"

# 与 selftest.py 的结尾输出一致；改动那边记得同步这里
EXPECTED = "结果: 187 通过, 0 失败"


def _run_selftest():
    """跑一遍自测，返回 (returncode, stdout)。用当前解释器保证环境一致。"""
    p = subprocess.run(
        [sys.executable, str(SELFTEST)],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    )
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def test_selftest_all_pass():
    """全部 187 项断言必须通过。失败时把完整输出贴进 pytest 报告。"""
    code, out = _run_selftest()
    assert EXPECTED in out, (
        f"自测未全部通过（期望输出包含 {EXPECTED!r}）。\n"
        f"退出码: {code}\n"
        f"{'-' * 60}\n{out}\n{'-' * 60}"
    )


def test_no_hardcoded_credentials():
    """源码里不得出现真实凭证形态的字符串。

    运行时生成的虚拟令牌前缀（sk-ant-virtual-）与文档示例
    （sk-你的KEY / sk-xxx）属于白名单。
    """
    import re
    suspicious = re.compile(
        r"sk-[A-Za-z0-9]{16,}"      # 真实 Key 形态：足够长的随机串
        r"|sk-ant-[A-Za-z0-9_\-]{20,}"
        r"|AKIA[0-9A-Z]{16}"        # AWS Access Key
        r"|ghp_[A-Za-z0-9]{36}"     # GitHub token
    )
    allow = ("sk-ant-virtual-", "sk-你的", "sk-xxx", "sk-demo", "sk-abc")
    bad = []
    for f in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in str(f) or ".venv" in str(f):
            continue
        for i, line in enumerate(
                f.read_text(encoding="utf-8").splitlines(), 1):
            if any(a in line for a in allow):
                continue
            if suspicious.search(line):
                bad.append(f"{f.relative_to(ROOT)}:{i}: {line.strip()[:100]}")
    assert not bad, "源码中出现疑似硬编码凭证:\n" + "\n".join(bad)


def test_runtime_data_not_tracked():
    """含明文 Key 的运行时数据不得存在于仓库内。"""
    leaked = [p for p in (".csswitch", "config.json", "state.json", "sandbox")
              if (ROOT / p).exists()]
    assert not leaked, (
        f"仓库内出现运行时数据（可能含明文 API Key）: {leaked}\n"
        f"确认 .gitignore 生效，必要时执行: git rm -r --cached <路径>")
