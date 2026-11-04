"""本地看板：把采集结果渲染成"拥挤度矩阵 + 目的地卡 + 建议"。

运行：python app.py  → 浏览器打开 http://127.0.0.1:8901
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from trip_oracle import analyze, store
from trip_oracle.collectors import weather as weather_mod
from trip_oracle.config import DESTINATIONS, FORECAST_DAYS

PORT = 8901

# 票价基准（二等座/硬座，元）——后续可换成实时查询，这里先用人工基准保证演示可用
PRICE = {"吉林市": 40, "四平": 30, "沈阳": 110, "哈尔滨": 210, "延吉": 165, "长白山": 150}
HOTEL = {d["name"]: d["stay"] for d in DESTINATIONS}

# 法定假日（住宿上浮判断用；按假期逐段补充）
HOLIDAY_SET = {f"2026-10-0{d}" for d in range(1, 8)} | \
              {f"2027-01-0{d}" for d in (1, 2, 3)} | {"2026-12-31"}


def stay_uplift(dates: list[str], nights: int) -> float:
    """住宿上浮系数：按每晚入住日期判断。周五/周六入住 ×1.4，法定假日 ×2.0，取最大。

    背景：HOTEL 表存的是平日基准价。用户实测反馈——国庆沈阳很挤、房价翻倍，
    平日价直接求和会严重低估假日花费。
    """
    f = 1.0
    for i in range(min(nights, len(dates))):
        d = dates[i]
        if d in HOLIDAY_SET:
            f = max(f, 2.0)
        elif date.fromisoformat(d).weekday() in (4, 5):  # 周五/周六晚
            f = max(f, 1.4)
    return f
DAYS_HINT = {d["name"]: d["days"] for d in DESTINATIONS}
TAGS = {d["name"]: d["tags"] for d in DESTINATIONS}


def picked_daily() -> list[dict]:
    """挑出"该用哪次采集"的日汇总行，矩阵/车票两个板块共用同一套口径。

    · 当天这一列 = "出发前"信息 → 取当天中午 12 点前最早的一次采集（早上 8 点那次，
      车都还在）；当天没采过 / 只在下午晚上采过，就用"昨天最后一次"（即提前一天能
      知道的信息），绝不拿 21 点只剩 4 趟车的残数据充数 —— 那会显示成"今天很空"。
    · 其它日期 = 最新一次采集。
    """
    today_s = date.today().isoformat()
    keys = ("travel_date", "dest", "direction")
    morning: dict[tuple, dict] = {}     # 当天 12 点前的采集
    preday: dict[tuple, dict] = {}      # 当天之前最后一次采集
    latest: dict[tuple, dict] = {}
    for r in store.train_daily_all():
        k = tuple(r[x] for x in keys)
        fa = r.get("fetched_at") or ""
        prev = latest.get(k)
        if prev is None or fa > (prev.get("fetched_at") or ""):
            latest[k] = r
        if r["travel_date"] == today_s:
            if fa[:10] == today_s and fa[11:16] <= "12:00":
                if k not in morning or fa < (morning[k].get("fetched_at") or ""):
                    morning[k] = r
            elif fa[:10] < today_s:
                p = preday.get(k)
                if p is None or fa > (p.get("fetched_at") or ""):
                    preday[k] = r
    return [morning.get(k) or preday.get(k) or latest.get(k) or v
            for k, v in sorted(latest.items())]


def build_matrix() -> dict:
    """目的地 × 日期 的拥挤度矩阵 + 天气（去/回程取更紧的一侧）。"""
    today = date.today()
    today_s = today.isoformat()
    daily = picked_daily()

    cell = {}
    dates, dests = set(), set()
    snap: dict[str, str] = {}          # 每个旅行日：实际用到的那次快照时间
    for r in daily:
        d, fa = r["travel_date"], (r.get("fetched_at") or "")
        if d == today_s:                # 当天多取"当天最早那次"，取 min 才不会串到隔夜
            snap[d] = min(snap.get(d) or fa, fa)
        else:
            snap[d] = max(snap.get(d) or "", fa)
    for r in daily:
        idx = analyze.crowd_index(r)
        if idx < 0:
            continue
        # 假期/周末放大：余票压力本身在假期会低估（淡季基线为 0 时乘完仍为 0，
        # 天然不会"让所有都凸显"）；倍数随假期类型不同，写入 cell 供前端标注
        mult = analyze.holiday_multiplier(date.fromisoformat(r["travel_date"]))
        idx = min(1.0, idx * mult)
        key = (r["dest"], r["travel_date"])
        old = cell.get(key)
        # 同一 (目的地, 日期) 有去/回两行：取更紧的作为该格的值，保留明细供 tooltip
        if old is None or idx > old["index"]:
            merged = dict(old or {})
            merged.update({
                "index": idx, "level": analyze.level(idx),
                "level_text": analyze.LEVEL_TEXT.get(analyze.level(idx), "-"),
                "trains": r["trains_total"], "soldout": r["trains_soldout"],
                "known": r["trains_known"],
                "seats": r["seats_total"], "min_duration": r["min_duration"],
            })
            merged.setdefault("dir_detail", {})[r["direction"]] = {
                "index": round(idx, 3), "soldout": r["trains_soldout"], "seats": r["seats_total"],
            }
            cell[key] = merged
        else:
            old.setdefault("dir_detail", {})[r["direction"]] = {
                "index": round(idx, 3), "soldout": r["trains_soldout"], "seats": r["seats_total"],
            }
        dates.add(r["travel_date"])
        dests.add(r["dest"])

    # 保留"今天起 N 天"这个连续窗口：
    #   当天这一列照留 —— "今天出发去不去"是最常见的用法，不能砍；
    #   它的值取当天最早一次采集（早上车都还在），晚上再看也不会被"车已发走"拉低。
    #   watch.py 会另外采远期目标日期（元旦），那些日期不连续，必须挡在窗口外。
    today_s = today.isoformat()
    horizon = (today + timedelta(days=FORECAST_DAYS)).isoformat()
    dates = {d for d in dates if today_s <= d <= horizon}

    wx = {}
    for w in store.latest_weather():
        wx[(w["dest"], w["date"])] = {
            "tmax": w["tmax"], "tmin": w["tmin"],
            "rain": w["precip_prob"], "code": w["weathercode"],
            "text": weather_mod.describe(w["weathercode"], w.get("text")),
        }
    # 当天这一列是不是"当天采的"：是 → 出发前实况；否 → 提前一天采的预判
    today_snap = snap.get(today_s, "")
    return {
        "mode": "live",
        "dates": sorted(dates),
        "dests": [d["name"] for d in DESTINATIONS if d["name"] in dests],
        "mults": {d: analyze.holiday_multiplier(date.fromisoformat(d)) for d in dates},
        "cell": {f"{k[0]}|{k[1]}": v for k, v in cell.items()},
        "weather": {f"{k[0]}|{k[1]}": v for k, v in wx.items()},
        "snap": {d: snap.get(d, "") for d in dates},
        "data_age": max((snap.get(d, "") for d in dates), default=""),
        "today": today_s,
        "today_same_day": bool(today_snap) and today_snap[:10] == today_s,
        "start": sorted(dates)[0] if dates else today_s,
        "updated": datetime.now().strftime("%m-%d %H:%M"),
    }


SEAT_NAMES = {"swz": "商务座", "tz": "特等座", "zy": "一等座", "ze": "二等座",
              "rz": "软座", "rw": "软卧", "yw": "硬卧", "yz": "硬座", "wz": "无座"}


def _seat_text(v: int) -> str | None:
    """12306 余票语义：-1 未知/无此席别，0 无票，1~99 具体张数，100 = 有（≥20 张）。"""
    if v < 0:
        return None
    if v == 0:
        return "无"
    return "有" if v >= 100 else f"{v} 张"


def ticket_view(dest: str | None = None, travel_date: str | None = None,
                direction: str = "out") -> dict:
    """车票剩余板块：某城市某天 每一趟车还剩什么票。

    口径与矩阵一致（picked_daily）：当天用"出发前"那次采集，其它日期用最新一次。
    返回 {"dest","date","dir","dates","dests","days":[…7 天汇总…],"trains":[…明细…]}。
    """
    rows = picked_daily()
    today_s = date.today().isoformat()
    horizon = (date.today() + timedelta(days=FORECAST_DAYS)).isoformat()
    dates = sorted({r["travel_date"] for r in rows if today_s <= r["travel_date"] <= horizon})
    if not dates:
        return {"dest": None, "date": None, "dir": direction, "dates": [],
                "dests": [], "days": [], "trains": []}
    dests = [d["name"] for d in DESTINATIONS if any(r["dest"] == d["name"] for r in rows)]
    dest = dest if dest in dests else dests[0]
    d = travel_date if travel_date in dates else dates[0]
    direction = direction if direction in ("out", "return") else "out"

    by_date = {r["travel_date"]: r for r in rows
               if r["dest"] == dest and r["direction"] == direction}
    days = []
    for dd in dates:
        r = by_date.get(dd)
        days.append({
            "date": dd,
            "weekday": "周" + "一二三四五六日"[date.fromisoformat(dd).weekday()],
            "total": (r or {}).get("trains_total") or 0,
            "soldout": (r or {}).get("trains_soldout") or 0,
            "available": max(0, ((r or {}).get("trains_known") or 0)
                             - ((r or {}).get("trains_soldout") or 0)),
            "known": (r or {}).get("trains_known") or 0,
            "seats": (r or {}).get("seats_total") or 0,
            "fetched_at": (r or {}).get("fetched_at") or "",
        })

    picked = by_date.get(d) or {}
    trains = []
    for t in store.train_rows_at(d, dest, direction, picked.get("fetched_at") or ""):
        try:
            seats = json.loads(t["seats_json"])
        except (TypeError, ValueError):
            seats = {}
        items = [{"name": SEAT_NAMES.get(k, k), "value": v, "text": _seat_text(v)}
                 for k, v in seats.items()]
        items = [i for i in items if i["text"]]
        # 一趟车"有票"= 至少一个席别有票；全部为 0 才算售罄
        status = ("unknown" if not items else
                  ("ok" if any(i["value"] > 0 for i in items) else "soldout"))
        trains.append({
            "code": t["train_code"], "depart": t["depart_time"], "arrive": t["arrive_time"],
            "duration": t["duration"], "status": status,
            "seats": items,
            "soldout_seats": [i["name"] for i in items if i["value"] == 0],
        })
    return {"dest": dest, "date": d, "dir": direction, "dates": dates, "dests": dests,
            "days": days, "trains": trains, "fetched_at": picked.get("fetched_at") or ""}


def replay_matrix(replay_date: str) -> dict:
    """历史回溯面板。

    硬约束：12306 不提供历史余票 → 拥挤指数无法用真实售票数据回放。
    处理：拥挤度 = 当日百度热度 ÷ 该地 60 天中位数 的比值线性映射（明确标注"估计"）；
          天气 = Open-Meteo 历史档案的实测值。
    用途：答辩回测叙事——验证"节前热度抬升 → 拥挤"的传导是否成立。
    """
    import statistics
    per: dict[str, dict[str, list[float]]] = {}
    # 基线只取回看日前 60 天：库里已有 3 年历史，若用全历史中位数，
    # 往年国庆高峰会抬高基线、反而压低当年假期的反演值
    win_start = (date.fromisoformat(replay_date) - timedelta(days=60)).isoformat()
    for r in store.heat_daily_all():
        if r["heat"] and win_start <= r["date"] <= replay_date:
            per.setdefault(r["dest"], {}).setdefault(r["date"], []).append(r["heat"])
    import math
    cell, wx = {}, {}
    mult = analyze.holiday_multiplier(date.fromisoformat(replay_date))
    pending: dict[str, dict] = {}       # 先算"有信号"的，再给"数据薄"的借先验

    for d in DESTINATIONS:
        name = d["name"]
        m = per.get(name) or {}
        if not m.get(replay_date):
            # 当天完全没有有效热度（冷门词假 0 已剔除）→ 不是"不挤"，是没测到
            pending[name] = {"m": m, "base": None, "n": len(m), "missing": True}
            continue
        base = statistics.median(sum(v) / len(v) for v in m.values()) or 1.0
        n_valid = len(m)                    # 窗口内有效天数（假 0 已剔除）
        # 数据太薄：冷门词（四平）60 天只有 ~25 天有效，中位数不可靠，
        # 单日 ±50% 的波动是噪声而非需求信号 → 自身不给点估计
        thin = n_valid < 40 or base < 15
        if thin:
            pending[name] = {"m": m, "base": base, "n": n_valid}
        else:
            ratio = (sum(m[replay_date]) / len(m[replay_date])) / base
            # 对数阻尼：比值 1 → 0.18，2 → 0.42，4.5 → 0.73；低基数爆表被抑制
            idx = max(0.02, min(1.0, 0.18 + 0.35 * math.log(max(ratio, 0.5))))
            # 假期/周末放大：热度反演在假期系统性低估（对照 2026 国庆实测），倍数随假期不同
            idx = min(1.0, idx * mult)
            cell[f"{name}|{replay_date}"] = {
                "index": round(idx, 3), "level": analyze.level(idx),
                "level_text": analyze.LEVEL_TEXT.get(analyze.level(idx), "-"),
                "est": True, "thin": False, "signal": "own",
                "note": f"百度热度 {round(sum(m[replay_date]) / len(m[replay_date]))} / "
                        f"前60天中位 {round(base)}，×{mult:g}",
            }

    # 数据薄的城市：不能给 0.18（那是"假定舒适"，同样是无根据的断言）。
    # 假期是区域性共同冲击，用当天"有信号"城市的水平做先验，给区间而非点值，
    # 明确标 signal="prior"（参考），让用户看到"可能挤，但我们无法确认"。
    solid = [c["index"] for c in cell.values() if not c["thin"]]
    prior = statistics.median(solid) if solid else 0.35
    for name, info in pending.items():
        lo, hi = max(0.02, prior * 0.7), min(1.0, prior * 1.3)
        why = ("当日无有效热度数据（关键词流量过低，已剔除虚假 0）"
               if info.get("missing") else
               f"自身有效数据仅 {info['n']}/60 天（中位 {round(info['base'])}），不采信单日波动")
        cell[f"{name}|{replay_date}"] = {
            "index": round(prior, 3), "level": analyze.level(prior),
            "level_text": analyze.LEVEL_TEXT.get(analyze.level(prior), "-"),
            "est": True, "thin": True, "signal": "prior",
            "range": [round(lo, 2), round(hi, 2)],
            "note": f"{why}；按同日有信号城市中位 {round(prior,2)} 借估，"
                    f"实际可能落在 {lo:.2f}~{hi:.2f}",
        }

    for d in DESTINATIONS:
        name = d["name"]
        if f"{name}|{replay_date}" not in cell:
            continue
        arch = weather_mod.fetch_archive(d["lat"], d["lon"], replay_date, replay_date)
        if arch:
            w = arch[0]
            wx[f"{name}|{replay_date}"] = {
                "tmax": w["tmax"], "tmin": w["tmin"], "rain": None,
                "text": weather_mod.describe(w["weathercode"]),
            }
    return {
        "mode": "replay",
        "notice": f"历史回溯 {replay_date}：12306 不提供历史余票，拥挤指数为"
                  f"百度热度反演估计 × 假期/周末放大系数（{analyze.holiday_multiplier(date.fromisoformat(replay_date)):g}×）；"
                  f"天气为历史档案实测。",
        "dates": [replay_date],
        "mults": {replay_date: analyze.holiday_multiplier(date.fromisoformat(replay_date))},
        "dests": [x["name"] for x in DESTINATIONS if f"{x['name']}|{replay_date}" in cell],
        "cell": cell, "weather": wx,
        "updated": datetime.now().strftime("%m-%d %H:%M"),
    }


def recommend(days: int = 2, budget: int = 600, pref: str = "") -> list[dict]:
    """推荐逻辑：拥挤度 × 花费 × 天气 × 偏好 × 热度动量，加权打分。

    天气按类别评分（analyze.weather_flag）：降雨减分、强降雨重减、
    降雪对"自然"型目的地（冬季）加分、对城市型轻减——游客可能是奔着雪去的。
    花费 = 往返车票 ×2 + 住宿 ×(天数-1) + 餐饮 90/天，明细一并返回。
    """
    m = build_matrix()
    heat = heat_stats()
    dates = m["dates"][:days] if days else m["dates"][:2]
    month = date.today().month
    out = []
    for name in m["dests"]:
        if pref and pref not in TAGS.get(name, []):
            continue
        cells = [m["cell"][f"{name}|{d}"] for d in dates if f"{name}|{d}" in m["cell"]]
        if not cells:
            continue
        idx = sum(c["index"] for c in cells) / len(cells)
        flags = []
        wx_penalty = 0.0
        for d in dates:
            w = m["weather"].get(f"{name}|{d}")
            if not w:
                continue
            f = analyze.weather_flag(w.get("code"), TAGS.get(name, []), month)
            wx_penalty = max(wx_penalty, f["penalty"])
            if f["flag"]:
                flags.append(f"{d[5:]} {f['flag']}")
        trip_days = max(1, days)
        transport = PRICE.get(name, 100) * 2
        uplift = stay_uplift(dates, max(0, trip_days - 1))
        stay = round(HOTEL.get(name, 120) * max(0, trip_days - 1) * uplift)
        food = 90 * trip_days
        cost = transport + stay + food
        h = heat.get(name) or {"recent": 0, "momentum": 0}
        # 热度动量：0 → 中性 0.5，±50% → 0/1，封顶防极端值
        heat_factor = max(0.0, min(1.0, 0.5 + h["momentum"]))
        wx_factor = max(0.0, min(1.0, 1 - wx_penalty))
        score = (1 - min(1, idx / 0.8)) * 40 + max(0, 1 - cost / max(budget, 1)) * 30 \
            + wx_factor * 15 + heat_factor * 15
        arrow = "↑" if h["momentum"] > 0.05 else ("↓" if h["momentum"] < -0.05 else "→")
        out.append({
            "name": name, "crowd": round(idx, 3), "level": analyze.level(idx),
            "level_text": analyze.LEVEL_TEXT.get(analyze.level(idx), "-"),
            "cost": cost, "cost_detail": {"transport": transport, "stay": stay, "food": food,
                                          "uplift": uplift},
            "rain": max([w["rain"] for d in dates
                         if (w := m["weather"].get(f"{name}|{d}"))
                         and w["rain"] is not None] or [0]),
            "weather": "；".join(flags) if flags else (
                (m["weather"].get(f"{name}|{dates[0]}") or {}).get("text", "-")),
            "heat": h["recent"], "heat_momentum": h["momentum"],
            "duration": min((c["min_duration"] for c in cells if c["min_duration"]), default=""),
            "score": round(score, 1),
            "within_budget": cost <= budget,
        })
    out.sort(key=lambda x: -x["score"])
    return out


def chat_reply(text: str) -> dict:
    """意向对话（规则版）：解析自然语言意向 → 调本模块函数 → 组装回答。

    未来接 LLM 时只换这一层（换成 function calling），工具与数据完全复用。
    解析：天数（"2天"）、预算（"600块/元"）、偏好（看雪/滑雪/温泉→自然，
    逛街/博物馆→城市，拍照/打卡→网红）、指定目的地（名字命中）。
    """
    import re
    t = text.strip()
    if not t:
        return {"reply": "说说你的意向：比如「两天，600 块，想看雪」或「元旦去长白山挤吗」"}

    m = re.search(r"(\d)\s*天", t)
    days = min(max(int(m.group(1)), 1), 3) if m else 2
    m_b = re.search(r"(\d{2,5})\s*(?:块|元|预算)", t)
    budget = min(max(int(m_b.group(1)), 100), 5000) if m_b else 600

    snow = any(k in t for k in ("雪", "滑雪", "冰雪"))
    if snow or any(k in t for k in ("温泉", "自然", "风景", "山")):
        pref = "自然"
    elif any(k in t for k in ("逛街", "城市", "博物馆", "演出", "citywalk")):
        pref = "城市"
    elif any(k in t for k in ("网红", "拍照", "打卡", "美食")):
        pref = "网红"
    else:
        pref = ""

    # 1) 点名目的地 → 单地详解
    dest = next((d["name"] for d in DESTINATIONS if d["name"] in t), None)
    if dest:
        today_s = date.today().isoformat()
        trip = max(1, days)
        # 假期问询先分流：过去/未开售的假期绝不能拿"今天这列"的平日指数来答——
        # 那是答非所问（用户问国庆，回答 11-03 舒适 0.01 纯属胡说）
        HOL = {"国庆": ("2026-10-01", "2026-10-07"),
               "元旦": ("2026-12-31", "2027-01-02")}
        hol = next((k for k in HOL if k in t), None)
        transport = PRICE.get(dest, 100) * 2
        food = 90 * trip
        horizon_s = (date.today() + timedelta(days=FORECAST_DAYS)).isoformat()
        # 过去的假期 / 还没开售的未来假期：都走"说明 + 预算估算"，不出平日指数
        if hol and (HOL[hol][1] < today_s or HOL[hol][0] > horizon_s):
            past = HOL[hol][1] < today_s
            why = (f"今年{hol}（{HOL[hol][0][5:]}~{HOL[hol][1][5:]}）已经过了" if past
                   else f"{hol}（{HOL[hol][0][5:]}~{HOL[hol][1][5:]}）还没到，且车票 12/17 才开售")
            nk = {"国庆": "元旦", "元旦": "春节"}.get(hol)
            nxt = {"元旦": "（12-31~01-02）", "春节": "（日期未定）"}.get(nk, "")
            est_stay = round(HOTEL.get(dest, 120) * max(0, trip - 1) * 2)
            lines = [why + "，我不拿平日的指数来冒充假期数据。"]
            if nk:
                lines.append(f"下一个假期是**{nk}{nxt}**："
                             + ("车票 12/17 开售，现在 12306 还查不到余票，"
                                "所以暂时给不出拥挤指数——不是不答，是数据还没产生。"))
            lines.append(f"{dest} {trip} 天按假日价的预算估算：车票 {transport} + 住宿 "
                         f"{est_stay}（{max(0, trip-1)} 晚，假日 ×2）"
                         f" + 餐饮 {food}（90/天）= **{transport + est_stay + food} 元**"
                         "（估算，未含热度修正）")
            lines.append("12/17 开售后矩阵里会出现元旦列，我也计划从开售起高频盯售罄速度，"
                         "到时候再用节前热度信号修正拥挤预测。")
            return {"reply": "\n".join(lines)}

        m_ = build_matrix()
        # "周末"问询 → 选最近的一个周末（周六+周日），而不是矩阵开头那两天；
        # 今天正好是周末则从今天算起。
        if any(k in t for k in ("周末", "周六", "周日", "礼拜")):
            wk = [d for d in m_["dates"] if date.fromisoformat(d).weekday() >= 5]
            dates_sel = wk[:trip]
        else:
            dates_sel = m_["dates"]
        cells = [(d, m_["cell"][f"{dest}|{d}"], m_["weather"].get(f"{dest}|{d}"))
                 for d in dates_sel if f"{dest}|{d}" in m_["cell"]]
        idx = sum(c["index"] for _, c, _ in cells) / max(1, len(cells))
        # 假期问询（"国庆/元旦/春节"）→ 住宿按假日价 ×2；否则按窗口日期判断周末上浮
        if any(k in t for k in ("国庆", "元旦", "春节", "假期")):
            uplift = 2.0
        else:
            uplift = stay_uplift([d for d, _, _ in cells], max(0, trip - 1))
        stay = round(HOTEL.get(dest, 120) * max(0, trip - 1) * uplift)
        lines = [f"【{dest} · {days} 天】拥挤指数 {idx:.2f}（{analyze.LEVEL_TEXT.get(analyze.level(idx))}）",
                 f"花费明细：车票 {transport} + 住宿 {stay}（{max(0, trip-1)} 晚"
                 + (f"，含假日/周末上浮 ×{uplift:g}" if uplift > 1 else "，平日价")
                 + f"） + 餐饮 {food}（90/天）= **{transport + stay + food} 元**"]
        for d, _, w in cells:
            if w:
                lines.append(f"  {d[5:]}：{w.get('text') or '—'} {w.get('tmin')}~{w.get('tmax')}°"
                             + ("（降雪已按加分处理）" if analyze.weather_flag(
                                 w.get("code"), TAGS.get(dest, []), date.today().month)["penalty"] < 0 else ""))
        lines.append(f"住宿基准为经济型快捷酒店平日价（{dest} {HOTEL.get(dest, 120)} 元/晚档）；"
                     + (f"本次已含 ×{uplift:g} 上浮。" if uplift > 1
                        else "本次不含周末/假日，未上浮。"))
        if any(k in t for k in ("国庆", "元旦", "春节", "假期")):
            lines.append("（你问的是假期——那要看节前热度预测，这块的 α 标定误差约 ±20%，"
                         "放假日期定了之后我再用节前信号修正。）")
        return {"reply": "\n".join(lines)}

    # 2) 未点名 → 推荐排序
    items = recommend(days, budget, pref)
    if not items:
        return {"reply": "这个偏好下暂时没有匹配的目的地数据，换个偏好或放宽预算试试。"}
    top = items[0]
    lines = [f"按你的意向（{days} 天 / 预算 {budget} 元"
             + (f" / 偏好{'看雪' if snow else pref or '不限'}）" if (snow or pref) else "）"),
             f"首推 **{top['name']}**：拥挤 {top['crowd']:.2f}（{top['level_text']}），"
             f"预计 {top['cost']} 元（车票 {top['cost_detail']['transport']} + 住宿 "
             f"{top['cost_detail']['stay']} + 餐饮 {top['cost_detail']['food']}），天气：{top['weather']}"]
    alts = [x["name"] for x in items[1:3] if x["within_budget"] or not top["within_budget"]]
    if alts:
        lines.append(f"备选：{'、'.join(alts)}")
    over = [x for x in items if not x["within_budget"]]
    if over:
        lines.append("超预算：\n  " + "、".join(f"{x['name']}（{x['cost']} 元）" for x in over[:3]))
    if snow:
        snow_d = [f"{x['name']}" for x in items
                  if "自然" in TAGS.get(x["name"], [])]
        if snow_d:
            lines.append("看雪提示：降雪对" + "、".join(snow_d) +
                         "这类自然型目的地按加分处理——有人就是奔着雪去的；"
                         "但雪天路滑，往返车次可能受影响。")
    return {"reply": "\n".join(lines), "items": items[:4]}


def heat_stats() -> dict:
    """每目的地热度动量：近 7 天均值 vs 前 7 天均值（百度指数，意图侧领先指标）。"""
    per: dict[str, dict[str, list[float]]] = {}
    for r in store.heat_daily_all():
        if r["heat"]: per.setdefault(r["dest"], {}).setdefault(r["date"], []).append(r["heat"])
    today = date.today()

    def _avg(m: dict, lo: int, hi: int):
        vals = []
        for i in range(lo, hi):
            v = m.get((today - timedelta(days=i)).isoformat())
            if v:
                vals.append(sum(v) / len(v))
        return sum(vals) / len(vals) if vals else None

    out = {}
    for dest, m in per.items():
        recent, prior = _avg(m, 1, 8), _avg(m, 8, 15)
        mom = (recent / prior - 1) if recent and prior and prior > 0 else 0.0
        out[dest] = {"recent": round(recent or 0), "prior": round(prior or 0),
                     "momentum": round(mom, 3)}
    return out


def heat_view(date_str: str | None = None) -> dict:
    """热度历史视图（双源）：某一天各目的地的热度快照 + 30 天走势。

    百度与抖音量纲不同 → 前端画双 Y 轴，两条线同图对比。
    返回 {"dates", "cur", "window", "dests",
          "series":  {source: {dest: [30 个值或 None]}},
          "current": {source: {dest: 当日值}}}。
    """
    def _one(source: str):
        rows = store.heat_daily_all(source)
        per: dict[str, dict[str, list[float]]] = {}
        all_dates: set[str] = set()
        for r in rows:
            if r["heat"]:
                per.setdefault(r["dest"], {}).setdefault(r["date"], []).append(r["heat"])
                all_dates.add(r["date"])
        return all_dates, per

    baidu_dates, baidu_per = _one("baidu_index")
    dy_dates, dy_per = _one("douyin_index")
    all_dates = sorted(baidu_dates | dy_dates)
    if not all_dates:
        return {"dates": [], "cur": None, "window": [], "dests": [],
                "series": {}, "current": {}}

    # 查看日固定落在坐标轴最右端：即使当天还没数据，也保留这一列/这一点（空白），
    # 而不是把整条曲线默默截断到"最后一个有数据的日子"——那样用户会以为选错了日期。
    if date_str:
        cur = date_str
    else:
        # 默认跟"今天"对齐（和矩阵同一起点）；今天的指数还没出就留空点并说明，
        # 不要悄悄退回到昨天——否则用户以为看板漏了一天
        cur = max(all_dates[-1], date.today().isoformat())
    d0 = date.fromisoformat(cur)
    # 窗口 = 查看日前 60 天 ~ 当天（查看日固定在最右；平日均值线仍用节前+节后数据计算）
    window = [(d0 - timedelta(days=i)).isoformat() for i in range(60, -1, -1)]

    missing: dict[str, str] = {}      # 源 → 该源最近有数据的一天（用于"今日暂无"说明）
    series, current, fit = {}, {}, {}
    dests: set[str] = set()
    for src, per in (("baidu_index", baidu_per), ("douyin_index", dy_per)):
        cur_vals = {dest: round(sum(v) / len(v)) for dest, m in per.items()
                    if (v := m.get(cur))}
        if not cur_vals:
            # 所选日期该源无数据 → 回退到该源最近有数据的一天（卡片数值用），
            # 但曲线仍画到查看日，末点留空并给出说明
            src_dates = sorted({d for m in per.values() for d in m})
            if src_dates:
                # 取"查看日及之前最近的一天"——原来写成 min() 会挑到三年前的第一天
                alt = max((s for s in src_dates if s <= cur), default=src_dates[-1])
                cur_vals = {dest: round(sum(v) / len(v)) for dest, m in per.items()
                            if (v := m.get(alt))}
                missing[src] = alt
        current[src] = cur_vals
        dests |= set(cur_vals)
        series[src] = {dest: [sum(m[ds]) / len(m[ds]) if m.get(ds) else None
                              for ds in window]
                       for dest, m in per.items() if dest in cur_vals}

        # 平日基线（两段式）：只用查看日前 60 天的平日（剔除假期与周末）。
        # 热度有漂移（如 8 月暑假高、9 月回落），一条水平线拟合不了两头——
        # 枚举分割点做 2 段水平拟合（段内平方和最小）；两段均值差 <10% 时合并为一条
        fit[src] = {}
        for dest, vals in series[src].items():
            pts = []
            for d, v in per.get(dest, {}).items():
                dd = date.fromisoformat(d)
                off = (dd - d0).days
                if not (-60 <= off <= 0):
                    continue
                if analyze.holiday_multiplier(dd) > 1.0:   # 假期 + 周五/六/日全部剔除
                    continue
                pts.append((off + 60, sum(v) / len(v)))    # 窗口索引 0~60
            pts.sort()
            n = len(pts)
            if n >= 16:
                vs = [p[1] for p in pts]
                best = None
                for k in range(8, n - 7):                  # 每段至少 8 个平日点
                    a, b = vs[:k], vs[k:]
                    ma, mb = sum(a) / len(a), sum(b) / len(b)
                    sse = (sum((x - ma) ** 2 for x in a)
                           + sum((x - mb) ** 2 for x in b))
                    if best is None or sse < best[0]:
                        best = (sse, k, ma, mb)
                _, k, ma, mb = best
                if abs(ma - mb) / max(ma, mb) < 0.25:      # 差异不大 → 合并一条（<25% 视觉上分不出）
                    fit[src][dest] = [{"from": pts[0][0], "to": pts[-1][0],
                                       "mean": round(sum(vs) / n, 1)}]
                else:
                    fit[src][dest] = [
                        {"from": pts[0][0], "to": pts[k - 1][0], "mean": round(ma, 1)},
                        {"from": pts[k][0], "to": pts[-1][0], "mean": round(mb, 1)},
                    ]

        series[src] = {dest: [round(v, 1) if v is not None else None
                              for v in vals]
                       for dest, vals in series[src].items()}

    dests = sorted(dests, key=lambda d: -(current["baidu_index"].get(d)
                                          or current["douyin_index"].get(d) or 0))
    # 为什么"今天"看不到今天：百度指数 T+1（次日约 08:00 后才有），抖音通常滞后 1~2 天。
    # 这不是采集漏了，是指数产品本身的发布节奏 → 在图上明确写出来，并保留右端空点。
    note = ""
    if missing:
        name = {"baidu_index": "百度", "douyin_index": "抖音"}
        srcs = "、".join(name[s] for s in missing)
        ends = "、".join(f"{name[s]} {v[5:]}" for s, v in missing.items())
        note = (f"{cur[5:]} 的{srcs}指数要到次日 08:00 后才发布（抖音再晚 1~2 天），右端这一点先空着；"
                f"已出到 {ends}，预测就是拿这段走势当基线 → 提前判断后面几天不受影响。")
    return {"dates": all_dates, "cur": cur, "window": window,
            "dests": dests, "series": series, "current": current, "fit": fit,
            "data_end": {"baidu_index": baidu_dates and max(baidu_dates),
                         "douyin_index": dy_dates and max(dy_dates)},
            "missing": missing, "note": note}


PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>周末去哪 · 拥挤度预测</title>
<style>
  :root{
    --bg:#0f1419; --panel:#161c23; --line:#242c36; --text:#e6edf3; --sub:#8b949e;
    --c1:#1f6f4a; --c2:#2f8f5b; --c3:#8a6d1f; --c4:#a8482a; --c5:#7f1d1d;
    --accent:#3b82f6;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  .wrap{max-width:1120px;margin:0 auto;padding:28px 20px 60px}
  h1{font-size:22px;margin:0 0 6px}
  .sub{color:var(--sub);font-size:13px;margin-bottom:22px}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:18px}
  table{width:100%;border-collapse:separate;border-spacing:4px;font-size:13px}
  th{color:var(--sub);font-weight:500;font-size:12px;padding:4px 6px;text-align:center}
  th.l,td.l{text-align:left;padding-left:8px}
  td{text-align:center;border-radius:6px;padding:9px 4px}
  .cell{font-weight:600}
  .w{display:block;font-size:11px;font-weight:400;opacity:.85;margin-top:2px;
     word-break:break-all;line-height:1.35}
  .c1{background:var(--c1)} .c2{background:var(--c2)} .c3{background:var(--c3)}
  .c4{background:var(--c4)} .c5{background:var(--c5)}
  .c0{background:#2b333d;color:var(--sub)}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
  .card{background:#121820;border:1px solid var(--line);border-radius:10px;padding:14px}
  .card h3{margin:0 0 8px;font-size:15px}
  .tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:10px;
       background:#1e2836;color:#9fb4cc;margin-right:6px}
  .row{display:flex;justify-content:space-between;font-size:13px;color:var(--sub);margin-top:5px}
  .row b{color:var(--text);font-weight:600}
  .bad{color:#f87171}
  form{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
  select,input{background:#0d1218;border:1px solid var(--line);color:var(--text);
               border-radius:8px;padding:8px 10px;font-size:13px}
  button{background:var(--accent);border:0;color:#fff;border-radius:8px;
         padding:9px 16px;font-size:13px;cursor:pointer}
  button:hover{filter:brightness(1.1)}
  .legend{display:flex;gap:14px;font-size:12px;color:var(--sub);margin-top:10px;flex-wrap:wrap}
  .dot{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px}
  .note{color:var(--sub);font-size:12px;margin-top:10px}
  .chip{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--line);
        border-radius:16px;padding:4px 12px;margin:0 6px 6px 0;cursor:pointer;
        font-size:12px;color:#b9c4d2;user-select:none}
  .chip.on{background:#1e2836;color:var(--text);border-color:var(--accent)}
  .chip input{display:none}
  .chipbar{margin-bottom:12px}
  /* 车票板块的日期按钮：显式配色，避免被全局 button 蓝底吞掉文字 */
  .daybtn{background:#0d1218;border:1px solid var(--line);color:#c6d1df;
          border-radius:8px;padding:6px 10px;margin:0 6px 6px 0;font-size:12px;
          cursor:pointer;text-align:left;line-height:1.45}
  .daybtn b{font-weight:600;color:var(--text)}
  .daybtn .ov{display:block;font-size:11px;color:#8b949e}
  .daybtn.on{background:#12233a;border-color:var(--accent)}
  .daybtn.on .ov{color:#9fc3e8}
</style></head>
<body><div class="wrap">
  <h1>周末去哪 · 周边拥挤度预测</h1>
  <div class="sub">长春出发 · 大学生短途出行决策 · 数据更新于 <span id="upd">-</span></div>

  <div class="panel">
    <form id="mf" style="margin-bottom:10px">
      <input type="date" id="mdate">
      <button type="submit">回看该日</button>
      <button type="button" id="mlive">回到实时</button>
      <span class="note">过去的日期为回溯模式：拥挤度=热度反演估计（标注"估计"），天气=历史实测</span>
    </form>
    <div id="notice"></div>
    <div id="snapNote" class="note" style="margin:0 0 8px"></div>
    <div id="tkChartWrap" style="margin:0 0 4px">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:10px">
        <div class="note" style="margin:0" id="tkChartTitle">余票走势 · 每个城市每天「售罄车次占比」（越高越难买）</div>
        <div style="display:flex;gap:8px;align-items:center">
          <button type="button" class="daybtn" id="refreshBtn" onclick="doRefresh()" style="padding:3px 9px;margin:0">↻ 刷新12306</button>
          <button type="button" class="daybtn" id="tkMetric" style="padding:3px 9px;margin:0">指标：售罄率</button>
        </div>
      </div>
      <div style="position:relative;height:220px;margin-top:6px">
        <canvas id="tkChart" role="img" aria-label="各目的地每日售罄车次占比走势">余票走势图</canvas>
      </div>
      <div class="note" id="tkChartNote" style="margin:2px 0 10px"></div>
    </div>
    <div id="matrix"></div>
    <div class="legend">
      <span><i class="dot" style="background:var(--c1)"></i>1 舒适</span>
      <span><i class="dot" style="background:var(--c2)"></i>2 较舒适</span>
      <span><i class="dot" style="background:var(--c3)"></i>3 一般</span>
      <span><i class="dot" style="background:var(--c4)"></i>4 拥挤</span>
      <span><i class="dot" style="background:var(--c5)"></i>5 很挤</span>
      <span class="note">格子里的数字是拥挤指数（0 最宽松，1 最挤），下方是最高气温/天气</span>
    </div>
  </div>

  <div class="panel">
    <h3 style="margin:0 0 4px;font-size:15px">搜索热度 · 想去的意愿
      <span style="margin-left:10px;font-weight:400;font-size:12px;color:var(--sub)">
        选一个城市查看走势</span>
      <button type="button" id="srcToggle" style="margin-left:12px;font-size:12px">双源</button>
      <span style="font-size:11px;color:var(--sub)">（点击切换：百度 → 抖音 → 双源）</span>
    </h3>
    <form id="hf" style="margin-bottom:10px">
      <input type="date" id="hdate">
      <button type="submit">查看那天</button>
      <span class="note">水平虚线 = 平日基线（剔除假期与周末；水平漂移明显时自动分成前/后两段）；左右轴为各自原始数值</span>
    </form>
    <div class="chipbar" id="chips"></div>
    <div style="position:relative;height:360px">
      <canvas id="heatChart" role="img" aria-label="所选城市百度与抖音指数近30天走势">热度走势图</canvas>
    </div>
    <div class="legend" id="heatLegend"></div>
  </div>

  <div class="panel">
    <h3 style="margin:0 0 4px;font-size:15px">车票剩余 · 每趟车还剩多少
      <span style="margin-left:10px;font-weight:400;font-size:12px;color:var(--sub)">
        选城市 → 选日期 → 看具体车次</span>
      <button type="button" id="dirToggle" style="margin-left:12px;font-size:12px">去程</button>
    </h3>
    <div class="chipbar" id="tkChips" style="margin-bottom:8px"></div>
    <div id="tkDays" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px"></div>
    <div id="tkSummary" class="note" style="margin:0 0 8px"></div>
    <div id="tkTable" style="max-height:430px;overflow:auto"></div>
  </div>

  <div class="panel">
    <h3 style="margin:0 0 8px;font-size:15px">说出你的意向 · 我来算账</h3>
    <form id="cf" style="margin-bottom:6px">
      <input id="ctext" placeholder="例如：两天，600 块，想看雪 ｜ 或：元旦去长白山挤吗" style="flex:1;min-width:240px">
      <button>发送</button>
    </form>
    <div id="chatReply" style="white-space:pre-wrap;font-size:13px;line-height:1.7"></div>
    <div class="cards" id="chatCards" style="margin-top:12px"></div>
  </div>

  <div class="panel">
    <form id="f">
      <select id="days"><option value="2">2 天</option><option value="1">当天</option><option value="3">3 天</option></select>
      <select id="pref"><option value="">不限偏好</option><option value="自然">自然</option>
        <option value="城市">城市</option><option value="网红">网红/美食</option></select>
      <input id="budget" type="number" value="600" min="100" step="50" style="width:110px">
      <button>给我推荐</button>
      <span class="note">预算含往返车票、住宿、日均 90 元餐饮；周末/假日住宿自动上浮</span>
    </form>
    <div class="cards" id="cards"></div>
  </div>

  <div class="note" id="foot"></div>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
const LV={1:'舒适',2:'较舒适',3:'一般',4:'拥挤',5:'很挤'};
let mDate=new URLSearchParams(location.search).get('date')||'';
async function load(){
  const m=await (await fetch('/api/matrix'+(mDate?'?date='+mDate:''))).json();
  document.getElementById('upd').textContent=m.updated;
  document.getElementById('notice').innerHTML=m.notice
    ?`<div style="background:#3a2f16;border:1px solid #8a6d1f;border-radius:8px;padding:8px 12px;margin-bottom:10px;color:#e8c86a;font-size:12px">⏳ ${m.notice}</div>`:'';
  document.querySelector('h1').textContent='周末去哪 · 周边拥挤度预测'+(m.notice?'（历史回溯）':'');
  document.getElementById('mdate').value=mDate;
  // 采集时间核对：余票是"快照"型数据，没跑采集就一直停在上一批
  {
    const age=(m.data_age||'').slice(0,16);            // 2026-11-03 22:00
    const today=m.today||'';
    const ts=(m.snap&&m.snap[today]||'');              // 今天这一列实际用的快照
    const lag=age.slice(0,10)<today;                   // 整表是否还是昨天的
    let s='';
    if(ts){
      s = m.today_same_day
        ? `今天（${today.slice(5)}）这一列用今早 ${ts.slice(11,16)} 的采集：当天出发前的余票状态`
        : `今天（${today.slice(5)}）还没采过（或只在傍晚后采、车已发走），这一列用 ${ts.slice(5,16)} 的快照＝“出发前能知道”的信息`;
    }
    document.getElementById('snapNote').innerHTML = m.notice ? '' :
      (lag?`<span style="color:#e8c86a">全表最新快照 ${age.slice(5)} 采，今天还没跑采集；跑一次即可刷新。</span>　`:'')
      + (s||'');
  }
  let h='<table style="table-layout:fixed"><tr><th class="l" style="width:12%">目的地</th>';
  const colw=(88/Math.max(1,m.dates.length)).toFixed(1);
  m.dates.forEach(d=>{const mu=(m.mults&&m.mults[d])||1;
    const sn=(m.snap&&m.snap[d]||'').slice(5,16);
    const isToday=d===m.today;
    const tag=isToday?`<br><span style="font-weight:400;font-size:10px;color:#8fb3d9">${m.today_same_day?'今采':'预判'}</span>`:'';
    h+=`<th style="width:${colw}%"${sn?` title="余票快照：${sn} 采集"`:''}>${d.slice(5)}${tag}${mu>1?`<br><span style="font-weight:400;font-size:10px;opacity:.8">×${mu}</span>`:''}</th>`;});
  h+='</tr>';
  m.dests.forEach(dest=>{
    h+=`<tr><td class="l">${dest}</td>`;
    m.dates.forEach(d=>{
      const c=m.cell[dest+'|'+d], w=m.weather[dest+'|'+d];
      if(!c){h+='<td class="c0">-</td>';return;}
      const wt=w?`${w.text} ${w.tmin==null?'':Math.round(w.tmin)+'~'}${Math.round(w.tmax)}°`:'';
      const est=c.thin
        ?`<span title="${(c.note||'').replace(/"/g,'&quot;')}" style="opacity:.8;font-size:10px;border:1px dashed currentColor;border-radius:3px;padding:0 2px">参考</span> `
        :(c.est?'<span style="opacity:.7;font-size:10px">估计</span> ':'');
      const num=c.thin&&c.range?`≈${c.index.toFixed(2)}（${c.range[0]}~${c.range[1]}）`:c.index.toFixed(2);
      h+=`<td class="c${c.level}">${est}<span class="cell">${c.level}</span><span class="w">${num} · ${wt}</span></td>`;
    });
    h+='</tr>';
  });
  h+='</table>';
  document.getElementById('matrix').innerHTML=h;
  renderTkChart(m);
  const hasThin=Object.values(m.cell).some(c=>c&&c.thin);
  document.getElementById('foot').textContent=
    '拥挤指数由 12306 余票压力折算：售罄车次占比 × 0.7 + 座位宽松度 × 0.3；等级按 0.15/0.30/0.50/0.70 分档。'
    +(hasThin?'　标注「参考」的格子：本地关键词有效数据不足 40/60 天，不采信单日波动，'
      +'按同日其它城市水平借估并给出区间——不是判定为"不挤"，而是"无法确认，可能同样挤"。':'');
  rec();
}
async function rec(){
  const days=document.getElementById('days').value;
  const pref=document.getElementById('pref').value;
  const budget=document.getElementById('budget').value;
  const r=await (await fetch(`/api/recommend?days=${days}&pref=${encodeURIComponent(pref)}&budget=${budget}`)).json();
  document.getElementById('cards').innerHTML=r.map((x,i)=>`
    <div class="card">
      <h3>${i+1}. ${x.name} <span class="tag">${x.level_text}</span></h3>
      <div class="row"><span>拥挤指数</span><b>${x.crowd.toFixed(2)}</b></div>
      <div class="row"><span>最快车程</span><b>${x.duration}</b></div>
      <div class="row"><span>预计花费</span><b class="${x.within_budget?'':'bad'}">¥${x.cost}${x.within_budget?'':' 超预算'}</b></div>
      <div class="row" style="font-size:11px"><span>车票×2 / 住宿 / 餐饮${x.cost_detail.uplift>1?'（含上浮 ×'+x.cost_detail.uplift+'）':''}</span>
        <b>¥${x.cost_detail.transport} / ¥${x.cost_detail.stay} / ¥${x.cost_detail.food}</b></div>
      <div class="row"><span>天气</span><b>${x.weather} · 降雨 ${x.rain==null?'-':x.rain+'%'}</b></div>
      <div class="row"><span>搜索热度</span><b>${x.heat}${x.heat_momentum>0.05?' ↑':(x.heat_momentum<-0.05?' ↓':' →')}近7天，较前7天${x.heat_momentum>=0?'+':''}${Math.round(x.heat_momentum*100)}%</b></div>
      <div class="row"><span>综合得分</span><b>${x.score}</b></div>
    </div>`).join('') || '<div class="note">没有符合条件的目的地</div>';
}
document.getElementById('f').addEventListener('submit',e=>{e.preventDefault();rec();});
document.getElementById('mf').addEventListener('submit',e=>{e.preventDefault();
  mDate=document.getElementById('mdate').value;load();});
document.getElementById('mlive').addEventListener('click',()=>{
  mDate='';document.getElementById('mdate').value='';load();});
const HC_COLORS=['#E24B4A','#3b82f6','#0F6E56','#ef9f27','#a855f7','#ec4899','#94a3b8'];
/* ---- 余票走势图（矩阵上方） ---- */
let mvChart=null,mvMetric='rate',mvData=null;
function tkVal(c,metric){
  if(!c)return null;
  const known=c.known||c.trains||0;
  if(!known)return null;
  if(metric==='rate')return +((100*(c.soldout||0))/known).toFixed(1);
  return Math.round((c.seats||0)/known);
}
function renderTkChart(m){
  const wrap=document.getElementById('tkChartWrap');
  if(!m||m.notice||!m.dates||!m.dates.length||!m.cell){wrap.style.display='none';return;}
  wrap.style.display='';
  mvData=m;
  const dates=m.dates, rate=mvMetric==='rate';
  const ds=m.dests.map((dest,i)=>({
    label:dest,
    data:dates.map(d=>tkVal(m.cell[dest+'|'+d],mvMetric)),
    borderColor:HC_COLORS[i%HC_COLORS.length],
    backgroundColor:HC_COLORS[i%HC_COLORS.length]+'22',
    borderWidth:1.8,pointRadius:2.5,pointHoverRadius:4,tension:.25,spanGaps:true
  }));
  document.getElementById('tkMetric').textContent='指标：'+(rate?'售罄率':'剩余座位/趟');
  document.getElementById('tkChartTitle').textContent=rate
    ?'余票走势 · 每个城市每天「售罄车次占比」（越高越难买）'
    :'余票走势 · 每个城市每天「每趟平均剩余座位」（越低越紧）';
  const allZero=ds.every(s=>s.data.every(v=>!v));
  document.getElementById('tkChartNote').innerHTML = allZero
    ?'当前窗口内 6 个城市售罄率都是 0——淡季余票充足，不是缺数据。等 12/17 元旦车票开售后，'
     +'这张图才会看出哪天的票先被抢空；线上颜色可选（点图例可隐藏城市）。'
    :'横轴与下方矩阵同一批日期；悬停看当天「售罄 x / 共 y 趟」。点图例可隐藏/显示城市。';
  if(mvChart){mvChart.destroy();mvChart=null;}
  mvChart=new Chart(document.getElementById('tkChart'),{
    type:'line',
    data:{labels:dates.map(d=>d.slice(5)),datasets:ds},
    options:{
      responsive:true,maintainAspectRatio:false,animation:false,
      interaction:{mode:'index',intersect:false},
      plugins:{
        legend:{position:'bottom',labels:{boxWidth:8,boxHeight:8,font:{size:11},color:'#b9c4d2',padding:12}},
        tooltip:{
          callbacks:{
            label:it=>{
              const c=mvData.cell[it.dataset.label+'|'+dates[it.dataIndex]]||{};
              return `${it.dataset.label}：${it.parsed.y==null?'无数据':it.parsed.y+(rate?'%':' 座/趟')}`
                +`（售罄 ${c.soldout||0}/${c.known||c.trains||0} 趟）`;
            }
          }
        }
      },
      scales:{
        x:{grid:{color:'#1b2430'},ticks:{color:ctx=>dates[ctx.index]===m.today?'#8fb3d9':'#8b949e',font:{size:10}}},
        y:{beginAtZero:true,grid:{color:'#1b2430'},
           ticks:{color:'#8b949e',font:{size:10},
                  callback:v=>rate?v+'%':v}}
      }
    }
  });
}
document.getElementById('tkMetric').addEventListener('click',()=>{
  mvMetric=mvMetric==='rate'?'seats':'rate';
  if(mvData)renderTkChart(mvData);
});
let heatData=null,selDest=null,hc=null;
function renderChips(){
  const el=document.getElementById('chips');
  el.innerHTML=heatData.dests.map(dest=>
    `<label class="chip${selDest===dest?' on':''}"><input type="radio" name="hdest" ${selDest===dest?'checked':''} data-dest="${dest}">${dest}</label>`).join('');
  el.querySelectorAll('input').forEach(cb=>cb.addEventListener('change',()=>{
    selDest=cb.dataset.dest;renderChips();renderChart();
  }));
}
function renderChart(){
  const v=heatData;if(!v||!selDest)return;
  const labels=v.window.map(s=>s.slice(5));
  const color=v.dests.indexOf(selDest)%HC_COLORS.length;
  const cB=HC_COLORS[color], cD=HC_COLORS[(color+1)%HC_COLORS.length];
  const datasets=[];
  const mk=(label,data,color,dash,extra)=>({label,data,borderColor:color,backgroundColor:color,
    borderWidth:dash?1.2:2,tension:.3,pointRadius:0,spanGaps:true,...(extra||{})});
  const addFit=(srcKey,color,ax)=>{
    const segs=(v.fit&&v.fit[srcKey]&&v.fit[srcKey][selDest])||[];
    segs.forEach((sg,i)=>{
      const name=segs.length>1?(i===0?'前段':'后段'):'平日';
      const data=labels.map((_,j)=>(j>=sg.from&&j<=sg.to)?sg.mean:null);
      datasets.push(mk(`${srcKey==='baidu_index'?'百度':'抖音'} ${name}均值 ${sg.mean}`,data,color+'99',true,{yAxisID:ax}));
    });
  };
  if(heatMode!=='douyin'&&v.series.baidu_index&&v.series.baidu_index[selDest]){
    const both=heatMode==='both';
    datasets.push(mk('百度（左轴）',v.series.baidu_index[selDest],cB,false,
      {yAxisID:'y',borderWidth:both?2:2.5}));
    addFit('baidu_index',cB,'y');
  }
  if(heatMode!=='baidu'&&v.series.douyin_index&&v.series.douyin_index[selDest]){
    const both=heatMode==='both';
    datasets.push(mk('抖音'+(both?'（右轴）':''),v.series.douyin_index[selDest],cD,false,
      {yAxisID:both?'y1':'y',borderDash:both?[3,3]:[],borderWidth:both?2:2.5}));
    addFit('douyin_index',cD,both?'y1':'y');
  }
  // 轴压缩：查看日（最后 4 天）的假期尖峰会让主体被压扁——
  // 以"除尖峰外最大值"×1.35 作为轴上限，尖峰超出顶部（tooltip 仍可见并在图例标注真实峰值）
  const axisMax=arr=>{
    const vals=(arr||[]).filter(x=>x!=null);
    if(!vals.length)return null;
    const all=Math.max(...vals);
    const body=Math.max(...vals.slice(0,Math.max(1,vals.length-4)));
    return all>body*1.5?Math.ceil(body*1.35):null;   // 只有尖峰>主体50%时才压缩
  };
  const peakNote=(srcKey,ymax)=>{
    const arr=(v.series[srcKey]||{})[selDest]||[];
    const all=Math.max(...arr.filter(x=>x!=null),0);
    return (ymax&&all>ymax)?`（峰值 ${Math.round(all)} 超出顶部）`:'';};
  const yMax=axisMax(heatMode!=='douyin'?(v.series.baidu_index||{})[selDest]:null);
  const y1Max=axisMax(heatMode!=='baidu'?(v.series.douyin_index||{})[selDest]:null);
  const legendNote=(yMax||y1Max)?'（假期尖峰超出图顶，真实数值悬停可见）':'';
  document.getElementById('heatLegend').innerHTML=datasets.map(ds=>
    `<span style="display:flex;align-items:center"><i class="dot" style="background:${ds.borderColor}"></i>${ds.label}</span>`).join('')
    + (legendNote?`<span class="note" style="margin-left:8px">${legendNote}</span>`:'');
  if(hc)hc.destroy();
  hc=new Chart(document.getElementById('heatChart'),{
    type:'line',data:{labels,datasets},
    options:{responsive:true,maintainAspectRatio:false,
      interaction:{mode:'index',intersect:false},
      plugins:{legend:{display:false},
        tooltip:{callbacks:{label:c=>`${c.dataset.label}：${c.parsed.y==null?'-':Math.round(c.parsed.y)}`}}},
      scales:{y:{position:'left',grid:{color:'rgba(255,255,255,.06)'},ticks:{color:cB},
                  ...(yMax?{max:yMax}:{})},
              y1:{display:heatMode==='both',position:'right',grid:{display:false},ticks:{color:cD},
                  ...(y1Max&&heatMode==='both'?{max:y1Max}:{})},
              x:{grid:{display:false},ticks:{color:'#8b949e',maxTicksLimit:12}}}}});
}
let heatMode='both';
document.getElementById('srcToggle').addEventListener('click',()=>{
  heatMode=heatMode==='both'?'baidu':(heatMode==='baidu'?'douyin':'both');
  const btn=document.getElementById('srcToggle');
  btn.textContent=heatMode==='both'?'双源':(heatMode==='baidu'?'仅百度':'仅抖音');
  renderChart();
});
async function loadHeat(d){
  const v=await (await fetch('/api/heat'+(d?'?date='+d:''))).json();
  if(!v.cur)return;
  heatData=v;
  document.getElementById('hdate').value=v.cur;
  if(!selDest||!v.dests.includes(selDest))selDest=v.dests[0];
  renderChips();renderChart();
}
document.getElementById('hf').addEventListener('submit',e=>{e.preventDefault();
  loadHeat(document.getElementById('hdate').value);});
/* ---- 车票剩余 ---- */
let tkData=null,tkDest=null,tkDate=null,tkDir='out';
async function loadTickets(dest,date,dir){
  const q=new URLSearchParams();
  if(dest)q.set('dest',dest);
  if(date)q.set('date',date);
  q.set('dir',dir||tkDir);
  const v=await (await fetch('/api/tickets?'+q)).json();
  if(!v.dests||!v.dests.length){document.getElementById('tkTable').innerHTML='<div class="note">暂无数据</div>';return;}
  tkData=v;tkDest=v.dest;tkDate=v.date;tkDir=v.dir;
  renderTickets();
}
function renderTickets(){
  const v=tkData;if(!v)return;
  // 城市
  document.getElementById('tkChips').innerHTML=v.dests.map(d=>
    `<label class="chip${tkDest===d?' on':''}"><input type="radio" name="tkdest" ${tkDest===d?'checked':''} data-dest="${d}">${d}</label>`).join('');
  document.getElementById('tkChips').querySelectorAll('input').forEach(cb=>
    cb.addEventListener('change',()=>loadTickets(cb.dataset.dest,v.date,tkDir)));
  // 日期（带余票/售罄概览）
  document.getElementById('tkDays').innerHTML=v.days.map(d=>{
    const on=d.date===v.date, sold=d.soldout;
    return `<button type="button" class="daybtn${on?' on':''}" data-d="${d.date}">
      <b>${d.date.slice(5)} ${d.weekday}</b>
      <span class="ov">${d.available}/${d.known} 有票${sold?` · 售罄${sold}`:''}</span></button>`;
  }).join('');
  document.getElementById('tkDays').querySelectorAll('button').forEach(b=>
    b.addEventListener('click',()=>loadTickets(v.dest,b.dataset.d,tkDir)));
  // 汇总
  const day=v.days.find(d=>d.date===v.date)||{};
  document.getElementById('tkSummary').innerHTML=
    `<b style="color:var(--text)">${v.dest} · ${v.date.slice(5)} ${day.weekday||''} · ${tkDir==='out'?'长春 → '+v.dest:v.dest+' → 长春'}</b>`
    +`　共 ${day.total||0} 趟，${day.available||0} 趟有票，售罄 ${day.soldout||0} 趟，剩余座位约 ${(day.seats||0).toLocaleString()} 个`
    +(v.fetched_at?`　<span style="opacity:.8">快照 ${v.fetched_at.slice(5,16)}</span>`:'');
  // 明细
  const row=t=>{
    const pills=t.seats.map(s=>{
      const ok=s.value>0;
      return `<span style="display:inline-block;font-size:11px;padding:1px 6px;margin:2px 4px 2px 0;border-radius:4px;
        border:1px solid ${ok?'#2f6f4a':'#4a2a2a'};background:${ok?'#14251c':'#241618'};color:${ok?'#8fd6ae':'#d69a9a'}">${s.name} ${s.text}</span>`;
    }).join('');
    const tag=t.status==='soldout'
      ?'<span style="font-size:11px;color:#f87171">售罄</span>'
      :(t.status==='unknown'?'<span style="font-size:11px;color:#8b949e">无数据</span>':'');
    return `<tr><td class="l" style="font-weight:600">${t.code}</td>
      <td class="l" style="color:var(--sub)">${t.depart}→${t.arrive}</td>
      <td style="color:var(--sub)">${t.duration}</td>
      <td class="l">${pills||'<span style="color:#8b949e;font-size:11px">-</span>'}</td>
      <td>${tag}</td></tr>`;
  };
  const soldFirst=[...v.trains].sort((a,b)=>(a.status==='soldout'?0:1)-(b.status==='soldout'?0:1));
  document.getElementById('tkTable').innerHTML=v.trains.length
    ? `<table style="table-layout:fixed"><thead><tr><th class="l" style="width:12%">车次</th>
        <th class="l" style="width:16%">发车→到达</th><th style="width:10%">历时</th>
        <th class="l" style="width:52%">席别余票</th><th style="width:10%">状态</th></tr></thead>
       <tbody>${soldFirst.map(row).join('')}</tbody></table>`
    : '<div class="note">这一天没有查到车次（可能已过发车时间）</div>';
}
document.getElementById('dirToggle').addEventListener('click',()=>{
  tkDir=tkDir==='out'?'return':'out';
  document.getElementById('dirToggle').textContent=tkDir==='out'?'去程':'回程';
  loadTickets(tkDest,tkDate,tkDir);
});
/* ---- 意向对话 ---- */
document.getElementById('cf').addEventListener('submit',async e=>{
  e.preventDefault();
  const t=document.getElementById('ctext').value.trim();
  if(!t)return;
  const el=document.getElementById('chatReply');
  el.innerHTML='<span style="color:var(--sub)">查询中…</span>';
  const v=await (await fetch('/api/chat?text='+encodeURIComponent(t))).json();
  el.innerHTML=(v.reply||'').replace(/\\*\\*(.+?)\\*\\*/g,'<b>$1</b>');
  document.getElementById('chatCards').innerHTML=(v.items||[]).map((x,i)=>`
    <div class="card">
      <h3>${i+1}. ${x.name} <span class="tag">${x.level_text}</span></h3>
      <div class="row"><span>拥挤指数</span><b>${x.crowd.toFixed(2)}</b></div>
      <div class="row"><span>预计花费</span><b class="${x.within_budget?'':'bad'}">¥${x.cost}</b></div>
      <div class="row" style="font-size:11px"><span>车票/住宿/餐饮</span>
        <b>¥${x.cost_detail.transport}/¥${x.cost_detail.stay}/¥${x.cost_detail.food}</b></div>
      <div class="row"><span>天气</span><b>${x.weather}</b></div>
    </div>`).join('');
});
load();
loadHeat(new URLSearchParams(location.search).get('date'));
loadTickets();
let refTimer=null;
async function doRefresh(){
  const b=document.getElementById('refreshBtn');
  if(b.disabled)return;
  await (await fetch('/api/refresh')).json();
  b.disabled=true;
  const t0=Date.now();
  b.textContent='采集中…';
  refTimer=setInterval(async()=>{
    const s=await (await fetch('/api/refresh/status')).json();
    b.textContent='采集中 '+Math.round((Date.now()-t0)/1000)+'s';
    if(!s.running){
      clearInterval(refTimer);refTimer=null;
      b.disabled=false;b.textContent='↻ 刷新12306';
      if(s.ok===false){alert('刷新失败：'+(s.detail||'').slice(-200));}
      await load();
    }
  },4000);
}
</script></body></html>
"""


# ---- 手动刷新：页面按钮触发一次快速采集（今天+明天余票 + 天气，约 1~2 分钟）----
APP_DIR = os.path.dirname(os.path.abspath(__file__))
_REFRESH: dict = {"running": False, "started": "", "finished": "", "ok": None,
                  "seconds": 0.0, "detail": ""}
_RLOCK = threading.Lock()


def _refresh_worker():
    t0 = time.time()
    env = dict(os.environ, COLLECT_DAYS="2", SKIP_HEAT="1", SKIP_DOUYIN="1")
    try:
        proc = subprocess.run(
            [sys.executable, os.path.join(APP_DIR, "collect.py")],
            cwd=APP_DIR, env=env, capture_output=True, text=True, timeout=420)
        ok = proc.returncode == 0
        detail = (proc.stdout or "")[-300:] if ok else \
            ((proc.stderr or proc.stdout or "")[-500:])
    except Exception as e:  # 超时或其他异常
        ok, detail = False, repr(e)
    with _RLOCK:
        _REFRESH.update(running=False, ok=ok, detail=detail,
                        seconds=round(time.time() - t0, 1),
                        finished=datetime.now().strftime("%m-%d %H:%M:%S"))


def refresh_start() -> dict:
    with _RLOCK:
        if _REFRESH["running"]:
            return dict(_REFRESH)
        _REFRESH.update(running=True, ok=None, detail="", finished="",
                        seconds=0.0,
                        started=datetime.now().strftime("%m-%d %H:%M:%S"))
        threading.Thread(target=_refresh_worker, daemon=True).start()
        return dict(_REFRESH)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if u.path == "/api/matrix":
            q = urllib.parse.parse_qs(u.query)
            d = (q.get("date") or [None])[0]
            if d:
                try:
                    if date.fromisoformat(d) < date.today():
                        return self._send(200, json.dumps(replay_matrix(d), ensure_ascii=False))
                except ValueError:
                    pass
            return self._send(200, json.dumps(build_matrix(), ensure_ascii=False))
        if u.path == "/api/recommend":
            q = urllib.parse.parse_qs(u.query)
            days = int((q.get("days") or ["2"])[0])
            budget = int((q.get("budget") or ["600"])[0])
            pref = (q.get("pref") or [""])[0]
            return self._send(200, json.dumps(recommend(days, budget, pref), ensure_ascii=False))
        if u.path == "/api/heat":
            q = urllib.parse.parse_qs(u.query)
            return self._send(200, json.dumps(
                heat_view((q.get("date") or [None])[0]), ensure_ascii=False))
        if u.path == "/api/tickets":
            q = urllib.parse.parse_qs(u.query)
            return self._send(200, json.dumps(
                ticket_view((q.get("dest") or [None])[0],
                            (q.get("date") or [None])[0],
                            (q.get("dir") or ["out"])[0]), ensure_ascii=False))
        if u.path == "/api/chat":
            q = urllib.parse.parse_qs(u.query)
            return self._send(200, json.dumps(
                chat_reply((q.get("text") or [""])[0]), ensure_ascii=False))
        if u.path == "/api/refresh":
            return self._send(200, json.dumps(refresh_start(), ensure_ascii=False))
        if u.path == "/api/refresh/status":
            with _RLOCK:
                return self._send(200, json.dumps(dict(_REFRESH), ensure_ascii=False))
        self._send(404, json.dumps({"error": "not found"}))


def main():
    store.init_db()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"看板已启动： http://127.0.0.1:{PORT}   (Ctrl+C 退出)")
    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
