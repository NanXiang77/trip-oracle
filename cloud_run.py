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
    MIN_GAP_H            距上次采集不足这么多小时就跳过本次，默认 3
    STALE_HOURS          启动补采阈值（仅 CATCHUP_ON_BOOT=1 时生效），默认 6
    CATCHUP_ON_BOOT      1=启动时若数据过期先补采（免费实例上会拖慢唤醒，默认关）
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
# 定时采集的最小间隔：库里数据比这更新就跳过（防止同一时段被重复唤醒后重复采）
MIN_GAP_H = float(os.environ.get("MIN_GAP_H", "3"))


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


def _fresh(hours: float) -> bool:
    """库里最新快照距今是否小于 hours 小时。用于防止同一时段被重复唤醒后重复采集。"""
    latest = _latest_snapshot()
    if not latest:
        return False
    try:
        when = dt.datetime.strptime(latest, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
    except ValueError:
        return False
    return (dt.datetime.now(TZ) - when).total_seconds() < hours * 3600


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
            # 免费实例会被外部 ping 反复唤醒，同一小时内可能启动多次。
            # 若库存已经是最近 MIN_GAP_H 小时内的，说明这轮采过了，直接跳过，
            # 既省采集耗时，也省免费档的运行时长。
            if _fresh(MIN_GAP_H):
                print(f"[collect] skip {now:%H}:00 — data fresh ({_latest_snapshot()}), "
                      f"gap<{MIN_GAP_H}h", flush=True)
            else:
                _run_collect(f"scheduled {now:%H}:00")
        time.sleep(60)


def start_dashboard() -> None:
    """在内部端口跑看板，供 tool_api 把 / 与 /api/* 反向代理过去。

    云端只暴露一个端口，所以看板不能自己监听公网，只能挂在 127.0.0.1 上，
    由 tool_api 按路径转发。
    """
    try:
        os.environ.setdefault("DASHBOARD_PORT", "8900")
        os.environ.setdefault("DASHBOARD_HOST", "127.0.0.1")
        os.environ.setdefault("DASHBOARD_OPEN_BROWSER", "0")
        import app as dashboard          # 延迟 import：PORT 在模块加载时读环境变量
        threading.Thread(target=dashboard.main, daemon=True).start()
        print(f"[dash] dashboard on 127.0.0.1:{dashboard.PORT}", flush=True)
    except Exception as e:
        print(f"[dash] failed to start: {e!r}", flush=True)


def main() -> None:
    print(f"[boot] tz=UTC+8 hours={COLLECT_HOURS} skips_heat={os.environ.get('SKIP_HEAT') == '1'} "
          f"latest={_latest_snapshot() or 'empty'}", flush=True)
    if os.environ.get("ENABLE_DASHBOARD", "1") != "0":
        start_dashboard()
    if os.environ.get("ENABLE_SCHEDULER", "1") != "0":
        threading.Thread(target=scheduler, daemon=True).start()
    tool_api.main()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
