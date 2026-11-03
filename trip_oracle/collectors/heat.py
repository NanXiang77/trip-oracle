"""搜索热度采集器（Q1 的间接信号层）。

2026-11-02 突破：百度指数打通 ✅
  之前裸请求持续 10018（风控），根因不是 Cookie，而是缺少 `Cipher-Text`
  签名头 + 返回数据是替换密码加密。改用社区维护的 qdata 库（内部完成
  Cipher-Text 生成、请求、ptbk 密钥获取、解密）后彻底解决。
  Cookie 仍需用户提供（登录 index.baidu.com 后复制，存 data/baidu_cookie.txt），
  约 2~4 周过期；失败时明确报"cookie 失效请更新"。

其他平台探测结论（全部实测）：
  巨量算数/抖音指数  argus 反爬签名，裸请求返回 HTML 挑战页
  搜狗指数 / 360趋势 已停服（连接失败）
  Google Trends     直连不通；本机代理当前未监听
  场馆预约数据       全在微信小程序内，无法程序化获取 → venues.py 手动兜底

设计：provider 插拔式——
  1. baidu_index（当前主力）：qdata 自动采集 + 解密 + 落库。
  2. manual：record() 手动登记（兜底，和 venues 一致）。

关键词配置：目的地 + 城内热点（如"沈阳中街"），搜索热度反映"想去的人"，
是目的地客流的领先指标（决定去 → 搜索 → 出行，通常提前 1~7 天）。
"""
from __future__ import annotations

import time

from .. import store

import os

DATA_DIR = os.path.dirname(store.DB_PATH)
COOKIE_FILE = os.path.join(DATA_DIR, "baidu_cookie.txt")


def _load_cookie() -> str:
    if not os.path.exists(COOKIE_FILE):
        return ""
    return open(COOKIE_FILE, encoding="utf-8").read().strip()


# 关键词配置：dest 用于和火车/场馆数据对齐
KEYWORDS = [
    {"dest": "沈阳",   "word": "沈阳中街"},
    {"dest": "沈阳",   "word": "沈阳故宫"},
    {"dest": "延吉",   "word": "延吉旅游"},
    {"dest": "哈尔滨", "word": "哈尔滨旅游"},
    {"dest": "长白山", "word": "长白山"},
    {"dest": "吉林市", "word": "吉林市旅游"},
    {"dest": "四平",   "word": "四平旅游"},
]


def fetch_baidu_index(word: str, days: int = 90) -> dict:
    """通过 qdata 拉取某词近 N 天百度指数（all 端）。

    返回 {'ok': bool, 'msg': str, 'series': {date: value}}。
    """
    cookie = _load_cookie()
    if not cookie:
        return {"ok": False, "msg": "缺少 cookie：请登录 index.baidu.com 后把 Cookie 存到 data/baidu_cookie.txt",
                "series": {}}
    end = time.strftime("%Y-%m-%d")
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    try:
        from qdata.baidu_index import get_search_index
        rows = list(get_search_index(
            keywords_list=[[word]], start_date=start, end_date=end, cookies=cookie))
    except Exception as e:
        return {"ok": False, "msg": f"采集失败（可能是 cookie 过期，请重新登录复制）：{type(e).__name__}: {e}",
                "series": {}}
    series = {}
    for r in rows:
        if r.get("type") == "all" and r.get("index"):
            try:
                v = int(r["index"])
            except (ValueError, TypeError):
                continue
            # 冷门词低流量日百度直接返回 0/空，是"无数据"不是真 0 —— 跳过
            if v <= 0:
                continue
            series[r["date"]] = v
    return {"ok": True, "msg": f"共 {len(series)} 天", "series": series}


def collect_all(days: int = 90) -> dict:
    """批量采集所有关键词并落库 heat_daily。返回各关键词的结果摘要。"""
    out = {}
    for k in KEYWORDS:
        r = fetch_baidu_index(k["word"], days)
        if r["ok"] and r["series"]:
            fetched = time.strftime("%Y-%m-%d %H:%M:%S")
            store.save_heat_rows([
                {"dest": k["dest"], "keyword": k["word"], "source": "baidu_index",
                 "date": d, "heat": v, "fetched_at": fetched}
                for d, v in sorted(r["series"].items())
            ])
        out[f"{k['dest']}:{k['word']}"] = r
        time.sleep(2)   # 低频友好，避免触发风控
    return out


def record(dest: str, keyword: str, date: str, heat: int, source: str = "manual") -> int:
    """手动登记某词某日的热度值（兜底数据源）。"""
    return store.save_heat_rows([{
        "dest": dest, "keyword": keyword, "source": source, "date": date,
        "heat": heat, "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }])
