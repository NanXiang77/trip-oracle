"""把本机采集到的搜索热度推到云端服务（替代"把 cookie 交给云端"）。

为什么这么做：
  · 百度指数要登录后的 cookie，抖音指数要扫码登录态的浏览器 profile —— 这两样
    都属于个人账号凭证，放服务器上既有泄露风险，也会因为机房 IP 触发风控。
  · 所以热度继续在本地采（collect.py 已经采好入库），采完只把"数字"POST 出去。
  · 连接方向是本机 → 云端（普通 HTTPS 出站），不需要内网穿透、不用开端口。

用法：
    python push_heat.py                          # 推最近 2 天（覆盖百度 T+1 / 抖音 T+2）
    python push_heat.py --days 7                 # 推最近 7 天
    python push_heat.py --all                    # 全量推一遍（首次建立云端库时用）
    python push_heat.py --url https://x.onrender.com --token abc123
    python push_heat.py --dry-run                # 只统计不发

环境变量（省得每次敲参数）：
    TOOL_API_URL    例 https://trip-oracle.onrender.com
    TOOL_API_TOKEN  与云端 TOOL_API_TOKEN 一致
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

from trip_oracle import store

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "oracle.db")
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cloud.json")


def load_config() -> dict:
    """读 data/cloud.json（由 推送配置.bat 写入），免去每次敲参数。"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def load_rows(days: int | None) -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    sql = "SELECT dest, keyword, source, date, heat, fetched_at FROM heat_daily"
    args: tuple = ()
    if days is not None:
        since = (date.today() - timedelta(days=days)).isoformat()
        sql += " WHERE date >= ?"
        args = (since,)
    rows = [dict(r) for r in con.execute(sql + " ORDER BY date, source, dest", args)]
    con.close()
    return rows


def push(url: str, token: str, rows: list[dict], timeout: int = 60) -> dict:
    body = json.dumps({"rows": rows,
                       "pushed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url.rstrip("/") + "/ingest/heat", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}",
                 # Cloudflare 会拦截 Python-urllib 默认签名（403 error 1010），伪装成正常客户端
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) trip-oracle-push/1.0",
                 "Accept": "application/json"})
    # 显式绕过系统代理：本机/局域网地址走代理会直接 502
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="把本地热度数据推送到云端工具服务")
    ap.add_argument("--url", default=os.environ.get("TOOL_API_URL", ""))
    ap.add_argument("--token", default=os.environ.get("TOOL_API_TOKEN", ""))
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--all", action="store_true", help="全量推送")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    cfg = load_config()
    url = a.url or cfg.get("url") or ""
    token = a.token or cfg.get("token") or ""
    src = ("命令行/环境变量" if a.url else "data/cloud.json") if url else ""

    rows = load_rows(None if a.all else a.days)
    if not rows:
        print("[push_heat] 本地没有可推的热度数据（先跑 collect.py）")
        return 1

    per: dict[str, tuple[str, int]] = {}
    for r in rows:
        last, n = per.get(r["source"], ("", 0))
        per[r["source"]] = (max(last, r["date"]), n + 1)
    print(f"[push_heat] 待推 {len(rows)} 行：" + "；".join(
        f"{s} 到 {v[0]}（{v[1]} 行）" for s, v in sorted(per.items())))

    if a.dry_run:
        print("[push_heat] --dry-run，不发送")
        return 0
    if not url:
        print("[push_heat] 还没配置云端地址：双击 推送配置.bat 填一次，"
              "或加 --url https://xxx.onrender.com")
        return 2
    if not token:
        print("[push_heat] 缺少口令：加 --token xxx，或在 推送配置.bat 里填")
        return 2
    print(f"[push_heat] 目标 {url}（配置来自{src}）")

    try:
        out = push(url, token, rows)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        print(f"[push_heat] 失败 HTTP {e.code}：{detail}")
        return 3
    except Exception as e:
        print(f"[push_heat] 失败 {type(e).__name__}: {e}")
        return 3

    print(f"[push_heat] 云端入库 {out.get('saved', 0)} 行"
          + (f"，拒收 {out['rejected']} 行" if out.get("rejected") else ""))
    for s in out.get("status", []):
        print(f"            {s['source']}: 到 {s['last_date']}，共 {s['rows']} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
