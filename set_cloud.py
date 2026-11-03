"""写入云端推送配置 data/cloud.json（只需一次）。

用法：
    python set_cloud.py --url https://xxx.onrender.com --token abc123
    python set_cloud.py --show          # 看当前配置（口令打码）
    python set_cloud.py --clear         # 删除配置

配置优先级（push_heat.py 里）：命令行参数 > 环境变量 > data/cloud.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cloud.json")


def mask(t: str) -> str:
    return (t[:4] + "…" + t[-4:]) if len(t) > 8 else ("*" * len(t) if t else "")


def main() -> int:
    ap = argparse.ArgumentParser(description="配置云端推送地址与口令")
    ap.add_argument("--url")
    ap.add_argument("--token")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--clear", action="store_true")
    a = ap.parse_args()

    if a.show:
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
        except FileNotFoundError:
            print("还没配置（data/cloud.json 不存在）")
            return 1
        print(f"url   : {cfg.get('url', '')}")
        print(f"token : {mask(cfg.get('token', ''))}")
        print(f"文件  : {CONFIG_PATH}")
        return 0

    if a.clear:
        if os.path.exists(CONFIG_PATH):
            os.remove(CONFIG_PATH)
            print("已删除 data/cloud.json")
        else:
            print("本来就没有配置")
        return 0

    url = (a.url or "").strip().rstrip("/")
    token = (a.token or "").strip()
    if not url.startswith("http"):
        print("地址要以 http:// 或 https:// 开头，例如 https://xxx.onrender.com")
        return 2
    if not token:
        print("口令不能为空（就是云端 TOOL_API_TOKEN 的值）")
        return 2

    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"url": url, "token": token}, f, ensure_ascii=False, indent=2)
    print(f"已写入 {CONFIG_PATH}")
    print(f"  url   : {url}")
    print(f"  token : {mask(token)}")
    print("之后 push_heat.py 和 一键更新并打包.bat 都会自动用它。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
