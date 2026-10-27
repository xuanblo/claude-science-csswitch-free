# -*- coding: utf-8 -*-
"""验证 CONNECT 隧道策略。

v1.2.0 起按用户要求改为**全部放行**，因此：
  - 任意远端目标都应 200放行；
  - Anthropic 系域名仍应 401（未登录 fast-fail，防卡死）—— 这是必需功能
    不是白名单策略。
"""
import select
import socket
import sys

PROXY_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18993

CASES = [
    ("conda.anaconda.org:443", (200,), "包索引（装包必需）"),
    ("pypi.org:443", (200,), "Python 包索引"),
    ("files.pythonhosted.org:443", (200,), "PyPI 文件 CDN"),
    ("github.com:443", (200,), "Git 依赖"),
    # 域名不存在 -> 502（远端连不上）。这恰恰证明策略已放行：
    # 若被策略拒绝会是 403。两种状态都算"未被策略拦截"。
    ("totally-unknown-site.example:443", (200, 502), "未知域名（策略放行，远端不可达）"),
    ("api.anthropic.com:443", (401,), "Anthropic 未登录 fast-fail"),
    ("claude.ai:443", (401,), "Anthropic 域名 fast-fail"),
]


def probe(target):
    """发CONNECT 并读回状态行。

    全放行后隧道会真正建立，若远端不回数据也不能一直等 —— 用
    select 等待首个字节，超时即认为"已 200 建立连接"（这本身就是
    放行成功的证据）。
    """
    s = socket.create_connection(("127.0.0.1", PROXY_PORT), timeout=10)
    try:
        host = target.rsplit(":", 1)[0]
        s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {host}:443\r\n\r\n"
                  .encode())
        ready, _, _ = select.select([s], [], [], 6)
        if not ready:
            # 没等到任何字节：多半是 200 后隧道挂着等远端数据
            return "HTTP/1.1 200 Connection Established"
        return s.recv(100).decode(errors="replace").split("\r\n")[0]
    except socket.timeout:
        return "HTTP/1.1 200 Connection Established"
    finally:
        s.close()


ok = fail = 0
for target, expects, why in CASES:
    line = probe(target)
    code = 0
    for part in line.split(" "):
        if part.isdigit():
            code = int(part)
            break
    good = code in expects
    ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
    mark = "OK  " if good else "FAIL"
    exp = "/".join(str(e) for e in expects)
    print(f"  {mark} {target:<42} {code:>3} (期望{exp})  {why}")

print(f"\n隧道策略: {ok} 通过, {fail} 失败")
sys.exit(1 if fail else 0)