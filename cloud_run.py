"""云端一体进程：HTTP 工具服务 + 每日采集调度（Render / Railway / Fly / 云主机通用）。

一个容器里干两件事：
  1. 前台：起 tool_api（平台调它取数据）
  2. 后台线程：每天 08:00 / 20:00（Asia/Shanghai）跑一次 collect.py；
     启动时若发现库里最新快照超过 6 小时，先补采一次（免费实例会休眠，
     醒来时数据可能是几小时前的）。

环境变量：
    PORT                 平台注入，Web 服务端口
    ENABLE_SCHEDULER     1=开启定时采集（默认 1），0=只提供 API
    COLLECT_HOURS        采集小时，默认 "8,20"
    SKIP_HEAT            1=跳过百度/抖音指数（云端默认建议开，因为没有浏览器 cookie）
    TOOL_API_TOKEN       平台调用时带的 Bearer 口令（可选，建议开）
    TOOL_API_PUBLIC_URL  OpenAPI 里对外暴露的地址，如 https://xxx.onrender.com
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import threading
import time

import collect
import tool_api
from trip_oracle import store

TZ = dt.timezone(dt.timedelta(hours=8))          # 固定东八区，不依赖容器 tzdata
COLLECT_HOURS = [int(h) for h in os.environ.get("COLLECT_HOURS", "8,20").split(",")]
STALE_HOURS = float(os.environ.get("STALE_HOURS", "6"))


def _latest_snapshot() -> str:
    rows = store.latest_train_daily()
    return max((r.get("fetched_at") or "" for r in rows), default="")


def _run_collect(reason: str) -> None:
    t0 = time.time()
    print(f"[collect] start ({reason}) at {dt.datetime.now(TZ):%Y-%m-%d %H:%M}", flush=True)
    try:
        rc = collect.main(["collect.py"])
        print(f"[collect] done rc={rc} in {time.time() - t0:.0f}s", flush=True)
    except Exception as e:                       # 采集失败不能拖垮 API
        print(f"[collect] FAILED: {e!r}", flush=True)


def _stale() -> bool:
    latest = _latest_snapshot()
    if not latest:
        return True
    try:
        when = dt.datetime.strptime(latest, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
    except ValueError:
        return True
    return (dt.datetime.now(TZ) - when).total_seconds() > STALE_HOURS * 3600


def scheduler() -> None:
    # 免费实例冷启动有几十秒窗口，若在此窗口内启动补采，健康检查会一直过不了、
    # 容器永远停留在 "waking up"。默认关闭启动补采（余票数据随镜像自带、热度走本机推送），
    # 只有显式设 CATCHUP_ON_BOOT=1 才补采。
    if os.environ.get("CATCHUP_ON_BOOT") == "1" and _stale():
        _run_collect("startup, data is stale")
    done: set[str] = set()
    while True:
        now = dt.datetime.now(TZ)
        key = now.strftime("%Y-%m-%d %H")
        if now.hour in COLLECT_HOURS and key not in done:
            done.add(key)
            _run_collect(f"scheduled {now:%H}:00")
        time.sleep(60)


def main() -> None:
    print(f"[boot] tz=UTC+8 hours={COLLECT_HOURS} skips_heat={os.environ.get('SKIP_HEAT') == '1'} "
          f"latest={_latest_snapshot() or 'empty'}", flush=True)
    if os.environ.get("ENABLE_SCHEDULER", "1") != "0":
        threading.Thread(target=scheduler, daemon=True).start()
    tool_api.main()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
