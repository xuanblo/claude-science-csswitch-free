# -*- coding: utf-8 -*-
"""本机安装脚本：配置端口 + 注册 provider（用本工具的独立目录）。

用法：
    python tools/setup_local.py --port 18993 --sandbox-port 8090
    python tools/setup_local.py --add demo --kind deepseek --key sk-xxx
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from csswitch import config as C  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, help="本地代理端口")
    ap.add_argument("--sandbox-port", type=int, help="沙箱 Science 端口")
    ap.add_argument("--add", help="provider 名称")
    ap.add_argument("--kind", default="deepseek", help="provider 类型")
    ap.add_argument("--key", default="", help="API Key")
    ap.add_argument("--base-url", default="", help="上游base_url（custom 类型必填）")
    ap.add_argument("--model", default="", help="默认模型")
    ap.add_argument("--use", action="store_true", help="同时设为当前配置")
    a = ap.parse_args()

    cfg = C.load_config()
    if a.port:
        cfg["settings"]["proxy_port"] = a.port
    if a.sandbox_port:
        cfg["settings"]["sandbox_port"] = a.sandbox_port
    if a.add:
        if C.find_provider(cfg, a.add):
            print(f"配置已存在，跳过: {a.add}")
        else:
            rec = C.new_provider_record(a.add, a.kind, a.key, a.model, a.base_url)
            C.add_provider(cfg, rec)
            print(f"已添加: {a.add}（{a.kind}）key={C.mask_key(rec['key'])}")
    if a.use and a.add:
        C.set_current(cfg, a.add)
        print(f"已设为当前配置: {a.add}")
    C.save_config(cfg)
    print("当前 settings:", cfg["settings"])
    print("配置文件:", C.config_path())


if __name__ == "__main__":
    main()