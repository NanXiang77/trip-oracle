"""天气采集（2026-11-02 重构：双源）。

问题复盘：
  1. 老版本展示只取 tmax → 片面。库里其实一直存了 tmin，是展示层的问题。
  2. WMO 码表缺 85/86（阵雪）、56/57（冻雨）等 → "未知"。
  3. Open-Meteo 是欧洲模型，对中国不够权威 → 换中国天气网（weather.com.cn，
     中国气象局主办）为主源，Open-Meteo 降级为后备与 7 天外的补充。

数据流：
  主源  中国天气网 7 天预报页（官方、含天气现象文本/高低温/风力）
  后备  Open-Meteo（覆盖最多 16 天， precipitation_probability 降水概率
        是它独有的字段，官方页没有）
  合并  官方优先，缺的日期用 Open-Meteo 补
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.parse
import urllib.request

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://www.weather.com.cn/",
}

# 目的地 → 中国天气网城市码（toy1.weather.com.cn/search 解析所得）
CITY_CODES = {
    "长春":   "101060101",
    "吉林市": "101060201",
    "四平":   "101060401",
    "延吉":   "101060301",
    "长白山": "101060901",      # 抚松县（长白山所在县；保护区 A 码无 7 天页）
    "沈阳":   "101070101",
    "哈尔滨": "101050101",
}

OM_API = "https://api.open-meteo.com/v1/forecast"
OM_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"


# 历史档案进程内缓存：回溯面板会对同一坐标反复取单日，缓存后只打一次网络
_ARCHIVE_CACHE: dict[tuple, list[dict]] = {}


def fetch_archive(lat: float, lon: float, start: str, end: str) -> list[dict]:
    """历史天气档案（实测值，非预报）。用于回溯某天（如 10-01）的真实天气。"""
    key = (round(lat, 2), round(lon, 2), start, end)
    if key in _ARCHIVE_CACHE:
        return _ARCHIVE_CACHE[key]
    q = {
        "latitude": lat, "longitude": lon,
        "start_date": start, "end_date": end,
        "daily": "temperature_2m_max,temperature_2m_min,weathercode",
        "timezone": "Asia/Shanghai",
    }
    url = OM_ARCHIVE + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with _OPENER.open(req, timeout=20) as r:
            obj = json.loads(r.read().decode("utf-8", "ignore"))
    except Exception:
        return []
    d = obj.get("daily") or {}
    times, out = d.get("time") or [], []
    for i, ds in enumerate(times):
        try:
            out.append({
                "date": ds,
                "tmax": d["temperature_2m_max"][i],
                "tmin": d["temperature_2m_min"][i],
                "precip_prob": None,
                "weathercode": d["weathercode"][i],
                "text": None, "wind": None,
            })
        except (KeyError, IndexError, TypeError):
            continue
    _ARCHIVE_CACHE[key] = out
    return out


# ---------------- 中国天气网（主源） ----------------

def fetch_weathercn(city_code: str) -> list[dict]:
    """解析 /weather/{code}.shtml 7 天预报。返回 [{date,tmax,tmin,text,wind}]。"""
    url = f"https://www.weather.com.cn/weather/{city_code}.shtml"
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with _OPENER.open(req, timeout=20) as r:
            html = r.read().decode("utf-8", "ignore")
    except Exception:
        return []
    blocks = re.findall(r'<li class="sky[^"]*".*?</li>', html, re.S)
    out, day0 = [], dt.date.today()
    for i, b in enumerate(blocks):
        try:
            head = re.search(r"<h1>([^<]+)</h1>", b).group(1)
            wea = re.search(r'class="wea"[^>]*>([^<]+)<', b).group(1).strip()
            tem = re.search(r'<span>(-?\d+)</span>\s*/\s*<i>(-?\d+)℃</i>', b)
            win = re.search(r"<i>([^<]*级)</i>", b)
            if "今天" in head:
                d = day0
            else:
                d = day0 + dt.timedelta(days=i)
            out.append({
                "date": d.isoformat(),
                "tmax": float(tem.group(1)),
                "tmin": float(tem.group(2)),
                "text": wea,
                "wind": win.group(1).strip() if win else None,
                "precip_prob": None,      # 官方页无降水概率
                "weathercode": None,      # 官方直接给文本，无需 WMO 码
            })
        except (AttributeError, ValueError):
            continue
    return out


# ---------------- Open-Meteo（后备 + 7 天外补充） ----------------

def fetch_openmeteo(lat: float, lon: float, days: int = 16) -> list[dict]:
    q = {
        "latitude": lat, "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weathercode",
        "timezone": "Asia/Shanghai", "forecast_days": days,
    }
    url = OM_API + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with _OPENER.open(req, timeout=20) as r:
            obj = json.loads(r.read().decode("utf-8", "ignore"))
    except Exception:
        return []
    d = obj.get("daily") or {}
    times, out = d.get("time") or [], []
    for i, ds in enumerate(times):
        try:
            out.append({
                "date": ds,
                "tmax": d["temperature_2m_max"][i],
                "tmin": d["temperature_2m_min"][i],
                "precip_prob": d["precipitation_probability_max"][i],
                "weathercode": d["weathercode"][i],
                "text": None, "wind": None,
            })
        except (KeyError, IndexError, TypeError):
            continue
    return out


def collect(dest_name: str, lat: float, lon: float, days: int = 15) -> list[dict]:
    """官方 7 天 + Open-Meteo 补足到 days 天，同日期官方优先。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    official = fetch_weathercn(CITY_CODES.get(dest_name, ""))
    om = fetch_openmeteo(lat, lon, max(days, 16))
    by_date = {}
    for row in om:                      # 后备打底
        by_date[row["date"]] = row
    for row in official:                # 官方覆盖
        by_date[row["date"]] = row
    return [{**row, "dest": dest_name, "fetched_at": now}
            for _, row in sorted(by_date.items())]


WMO = {
    0: "晴", 1: "大部晴", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "小雨", 55: "中雨",
    56: "冻雨", 57: "强冻雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "强阵雨", 82: "暴雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷雨伴冰雹",
}


def describe(code, text=None) -> str:
    """优先用官方天气文本；无文本再查 WMO 码。"""
    if text:
        return str(text)
    try:
        return WMO.get(int(code), "未知")
    except (TypeError, ValueError):
        return "未知"
