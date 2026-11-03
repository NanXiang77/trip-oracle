"""12306 余票采集器（公开接口，低频访问，不涉及账号登录）。

思路：余票 = 供给与需求的差。同一趟车随着出行日临近，余票单调减少；
      把"售罄车次占比 + 剩余座位总量"合起来，就是目的地的拥挤度信号。
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta

from ..config import ORIGIN_CODE, SEAT_FIELDS

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
BASE = "https://kyfw.12306.cn"
INIT_URL = f"{BASE}/otn/leftTicket/init"
STATION_JS = f"{BASE}/otn/resources/js/framework/station_name.js"
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")

# 12306 依赖 init 页面下发的 cookie，必须带 CookieJar，否则查询会被打回登录页
_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(_JAR),
    urllib.request.ProxyHandler({}),
)
_QUERY_PATH = "/otn/leftTicket/queryZ"   # 首次请求后会被服务端纠正
_cookie_ready = False


def _request(url, headers=None, timeout=20):
    hdr = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
    if headers:
        hdr.update(headers)
    req = urllib.request.Request(url, headers=hdr)
    with _OPENER.open(req, timeout=timeout) as r:
        return r.status, r.read(), dict(r.headers)


def _ensure_cookie():
    """12306 需要先访问一次 init 页面拿到 cookie，否则查询会被重定向。"""
    global _cookie_ready
    if _cookie_ready:
        return
    try:
        _request(INIT_URL)
    except Exception:
        pass
    _cookie_ready = True


def station_codes() -> dict:
    """解析车站中文名 -> 电报码，本地缓存一份，避免每次下载。"""
    cache = os.path.join(DATA_DIR, "station_name.js")
    os.makedirs(DATA_DIR, exist_ok=True)
    text = ""
    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 7 * 86400:
        text = open(cache, encoding="utf-8", errors="ignore").read()
    if not text:
        try:
            _ensure_cookie()
            _, raw, _ = _request(STATION_JS)
            text = raw.decode("utf-8", "ignore")
            open(cache, "w", encoding="utf-8").write(text)
        except Exception:
            return {}
    out = {}
    for item in re.findall(r"@([a-z]+)\|([^|]+)\|([A-Z]+)\|", text or ""):
        name, code = item[1], item[2]
        out.setdefault(name, code)
    return out


def _seat_value(raw: str) -> int:
    """-1 未知 / 0 无票 / 1..99 具体张数 / 100 表示"有"（当作充足）"""
    if raw is None:
        return -1
    raw = raw.strip()
    if raw == "":
        return -1
    if raw == "有":
        return 100
    if raw == "无":
        return 0
    if raw.isdigit():
        return min(int(raw), 100)
    return -1


def query(depart_date: date, to_code: str, from_code: str = ORIGIN_CODE) -> list[dict]:
    """查询某天 长春->目的地 的全部车次余票。"""
    global _QUERY_PATH
    _ensure_cookie()
    dto = {
        "leftTicketDTO.train_date": depart_date.strftime("%Y-%m-%d"),
        "leftTicketDTO.from_station": from_code,
        "leftTicketDTO.to_station": to_code,
        "purpose_codes": "ADULT",
    }
    url = f"{BASE}{_QUERY_PATH}?" + urllib.parse.urlencode(dto)
    try:
        _, raw, _ = _request(url, headers={"Referer": INIT_URL})
        text = raw.decode("utf-8-sig", "ignore")
        obj = json.loads(text)
    except Exception:
        return []

    # 服务端返回了 HTML（最常见是反爬校验页/登录页），本次视为失败
    if not isinstance(obj, dict):
        return []

    # 服务端会返回应当使用的查询端点，例如 {"c_url": "leftTicket/queryG", "status": false}
    if isinstance(obj, dict) and obj.get("status") is False and obj.get("c_url"):
        _QUERY_PATH = "/otn/" + obj["c_url"]
        return query(depart_date, to_code, from_code)

    rows = (obj.get("data") or {}).get("result") or []
    out = []
    for row in rows:
        f = row.split("|")
        if len(f) < 35:
            continue
        seats = {}
        for seat_name, idx in SEAT_FIELDS.items():
            if idx < len(f):
                seats[seat_name] = _seat_value(f[idx])
        out.append({
            "train_code": f[3],
            "depart_time": f[8],
            "arrive_time": f[9],
            "duration": f[10],
            "seats": seats,
            "can_buy": f[11] == "Y",
        })
    return out


def summarize(trains: list[dict]) -> dict:
    """把一天的余票压成几个指标。"""
    total = len(trains)
    soldout = 0
    known = 0
    seats_sum = 0
    min_dur = None
    for t in trains:
        vals = [v for v in t["seats"].values() if v >= 0]
        # 长途/短途席别不同，取"能买到的最便宜的席别"作为判断依据：
        # 只要有一个席别有票，就算这趟车没售罄
        if not vals:
            continue
        known += 1
        best = max(vals)
        if best == 0:
            soldout += 1
        seats_sum += sum(v for v in vals if v > 0)
        if min_dur is None or t["duration"] < min_dur:
            min_dur = t["duration"]
    return {
        "trains_total": total,
        "trains_soldout": soldout,
        "trains_known": known,
        "seats_total": seats_sum,
        "min_duration": min_dur or "",
    }


def collect_dates(dest_name: str, to_code: str, dates: list[date],
                  direction: str = "out", from_code: str = ORIGIN_CODE) -> tuple[list[dict], list[dict]]:
    """采集**指定出行日期**的余票（前向观测用）。

    与 collect() 的区别：collect 只覆盖"今天起 N 天"这个滚动窗口，追不上固定假期；
    这里直接给目标日期（如元旦 12/31~1/2）。注意 12306 只放 14 天内的票，
    超出窗口的日期查询会返回空列表，调用方需自行判断"是否已开售"。
    """
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    rows, daily = [], []
    for d in dates:
        trains = query(d, to_code, from_code)
        if not trains:
            time.sleep(1.2)
            continue
        s = summarize(trains)
        for t in trains:
            rows.append({
                "fetched_at": now,
                "travel_date": d.isoformat(),
                "dest": dest_name,
                "direction": direction,
                "train_code": t["train_code"],
                "depart_time": t["depart_time"],
                "arrive_time": t["arrive_time"],
                "duration": t["duration"],
                "seats_json": json.dumps(t["seats"], ensure_ascii=False),
            })
        daily.append({
            "travel_date": d.isoformat(), "dest": dest_name, "direction": direction,
            "fetched_at": now, **s,
        })
        time.sleep(0.6)   # 低频，友好访问
    return rows, daily


def presale_days() -> int:
    """12306 预售天数（实测 2026-11-03：今天 +14 天可查，+15 天返回空）。"""
    return 14


def collect(dest_name: str, to_code: str, days: int,
            direction: str = "out", from_code: str = ORIGIN_CODE) -> tuple[list[dict], list[dict]]:
    """采集一个方向未来 N 天的余票，返回 (明细行, 日汇总行)。

    direction="out"    长春 -> 目的地（from_code 默认长春）
    direction="return" 目的地 -> 长春（调用方传 from_code=目的地电报码）
    """
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    rows, daily = [], []
    for i in range(days):
        d = date.today() + timedelta(days=i)
        trains = query(d, to_code, from_code)
        if not trains:
            time.sleep(1.2)
            continue
        s = summarize(trains)
        for t in trains:
            rows.append({
                "fetched_at": now,
                "travel_date": d.isoformat(),
                "dest": dest_name,
                "direction": direction,
                "train_code": t["train_code"],
                "depart_time": t["depart_time"],
                "arrive_time": t["arrive_time"],
                "duration": t["duration"],
                "seats_json": json.dumps(t["seats"], ensure_ascii=False),
            })
        daily.append({
            "travel_date": d.isoformat(),
            "dest": dest_name,
            "direction": direction,
            "fetched_at": now,
            **s,
        })
        time.sleep(0.6)   # 低频，友好访问
    return rows, daily
