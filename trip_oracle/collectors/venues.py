"""场馆/景区预约余票采集器（Q1：目的地真实客流信号的入口）。

2026-11-02 探测结论（真实抓取测试）：
  - 沈阳故宫官网 sypm.org.cn：可达(200)，但预约购票入口在微信小程序，官网无余票接口
  - 辽宁省博物馆 lnmuseum.com.cn：可达(200)，纯展示页，预约在小程序
  - 哈尔滨极地公园 hrbpolarland.com：可达(200)，购票跳小程序
  - 长白山 cbstravel.com：301 跳转 pashmelino.com，后者无响应
  - 延边博物馆/吉林市博物馆(403)/四平战役纪念馆/长影世纪城：官网不可达或无预约系统
结论：场馆级"剩余名额"当前全部锁在微信小程序生态里，无公开 HTTP 接口可抓。

因此本采集器暂以"占位+手动兜底"运行：
  VENUES 里配置的 manual=True 场馆，可通过 record() 手动登记每日名额，
  数据进 venue_daily 表，格式与自动采集一致，后续接上真接口零迁移成本。
"""
from __future__ import annotations

import time

from .. import store

# 各目的地的主要可预约场馆（destination → venue 配置）
VENUES = [
    {"dest": "沈阳",     "venue": "沈阳故宫",   "manual": True, "capacity": 40000},
    {"dest": "沈阳",     "venue": "辽宁省博物馆", "manual": True, "capacity": 12000},
    {"dest": "哈尔滨",   "venue": "哈尔滨极地公园", "manual": False, "capacity": None,
     "note": "购票在小程序，官网无接口"},
    {"dest": "长白山",   "venue": "长白山景区", "manual": True, "capacity": 30000},
    {"dest": "延吉",     "venue": "延边博物馆", "manual": True, "capacity": 4000},
    {"dest": "吉林市",   "venue": "吉林市博物馆", "manual": True, "capacity": 6000},
    {"dest": "四平",     "venue": "四平战役纪念馆", "manual": True, "capacity": 3000},
]


def collect_all() -> int:
    """自动采集入口：当前所有场馆均为小程序预约，自动抓取返回 0。

    将来若某场馆拿到公开接口，在 VENUES 加 api 配置并在这里实现请求，
    写入格式：save_venue_rows([{dest, venue, date, capacity, remaining}])
    """
    auto = [v for v in VENUES if not v.get("manual")]
    return 0 if not auto else len(auto)


def record(dest: str, venue: str, date: str, remaining: int, capacity: int | None = None) -> int:
    """手动登记某场馆某日的剩余名额（看板/答辩前的兜底数据源）。"""
    return store.save_venue_rows([{
        "dest": dest, "venue": venue, "date": date,
        "capacity": capacity, "remaining": remaining,
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }])
