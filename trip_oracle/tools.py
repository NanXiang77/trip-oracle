"""工具层：本地版"自定义 Skill"。

设计原则：每个工具都是**纯函数 + JSON Schema 声明**，与 AgentArts 的
自定义工具/MCP 一一对应。迁到平台上时，只需要把这几个函数包成 HTTP 接口，
Schema 原样登记即可，模型侧的提示词不用改。
"""
from __future__ import annotations

import json

from . import analyze, store
from .config import DESTINATIONS

PRICE = {"吉林市": 40, "四平": 30, "沈阳": 110, "哈尔滨": 210, "延吉": 165, "长白山": 150}
HOTEL = {d["name"]: d["stay"] for d in DESTINATIONS}
TAGS = {d["name"]: d["tags"] for d in DESTINATIONS}
DAYS = {d["name"]: d["days"] for d in DESTINATIONS}


def tool_get_crowd_matrix(days: int = 8, back_days: int = 0) -> dict:
    """返回目的地 × 日期的拥挤度矩阵（含天气与搜索热度，去/回程取更紧一侧）。

    参数
      days      往后看多少天（默认 8）
      back_days 往前回看多少天（默认 0；设 7 就能连同过去一周一起返回）

    为什么要把 today 和 date_range 直接写进返回值：
      模型（尤其轻量模型）从 JSON 的 dates 数组里推"今天是几号"经常算错，
      实测出现过把 11-05~11-12 说成 11-02~11-09 的情况。与其让它推，不如直接给结论。
    """
    from datetime import date as _date, datetime, timedelta, timezone
    TZ = timezone(timedelta(hours=8))
    today = datetime.now(TZ).date()

    rows = store.latest_train_daily()
    wx = {(w["dest"], w["date"]): w for w in store.latest_weather()}

    # 每个目的地的最新搜索热度（百度 + 抖音），按日期取最新一行
    heat: dict[str, dict] = {}
    for src, key in (("baidu_index", "heat_baidu"), ("douyin_index", "heat_douyin")):
        try:
            latest_by_dest: dict[str, dict] = {}
            for h in store.latest_heat(src):
                d = h.get("dest")
                if not d:
                    continue
                prev = latest_by_dest.get(d)
                if prev is None or (h.get("date") or "") >= (prev.get("date") or ""):
                    latest_by_dest[d] = h
            for d, h in latest_by_dest.items():
                heat.setdefault(d, {})[key] = h.get("heat")
                heat[d][key + "_date"] = h.get("date")
        except Exception:
            pass

    grouped: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        grouped.setdefault((r["dest"], r["travel_date"]), []).append(r)
    cells = []
    for (dest, d), rs in sorted(grouped.items()):
        idxs = [(analyze.crowd_index(r), r) for r in rs]
        idxs = [(i, r) for i, r in idxs if i >= 0]
        if not idxs:
            continue
        idx, worst = max(idxs, key=lambda x: x[0])
        w = wx.get((dest, d)) or {}
        h = heat.get(dest) or {}
        cells.append({
            "dest": dest,
            "date": d,
            "crowd_index": round(idx, 3),
            "level": analyze.level(idx),
            "level_text": analyze.LEVEL_TEXT.get(analyze.level(idx), "-"),
            "direction": worst["direction"],
            "trains": worst["trains_total"],
            "soldout": worst["trains_soldout"],
            "min_duration": worst["min_duration"],
            "weather": w.get("weathercode"),
            "tmax": w.get("tmax"),
            "rain_prob": w.get("precip_prob"),
            "heat_baidu": h.get("heat_baidu"),
            "heat_douyin": h.get("heat_douyin"),
        })

    all_dates = sorted({c["date"] for c in cells})
    start = today - timedelta(days=max(0, int(back_days)))
    end = today + timedelta(days=max(1, int(days)))
    picked = [d for d in all_dates if start <= _date.fromisoformat(d) <= end]

    span = f"{picked[0]} 至 {picked[-1]}" if picked else "无"
    return {
        "today": today.isoformat(),
        "date_range": span,
        "note": (f"今天是 {today.isoformat()}。本次返回的日期范围是 {span}，"
                 "共 " + str(len(picked)) + " 天。回答里所有日期必须原样使用这里列出的值，"
                 "不要自行推算、加减或改写。"),
        "dates": picked,
        "cells": [c for c in cells if c["date"] in picked],
    }


def tool_recommend(days: int = 2, budget: int = 600, pref: str = "") -> dict:
    """按"人少、便宜、天气好"给目的地排序。"""
    m = tool_get_crowd_matrix(days)
    out = []
    for d in DESTINATIONS:
        name = d["name"]
        if pref and pref not in TAGS.get(name, []):
            continue
        cs = [c for c in m["cells"] if c["dest"] == name]
        if not cs:
            continue
        idx = sum(c["crowd_index"] for c in cs) / len(cs)
        rain = max((c["rain_prob"] or 0) for c in cs)
        cost = PRICE.get(name, 100) * 2 + HOTEL.get(name, 120) * max(0, days - 1) + 90 * days
        score = (1 - min(1, idx / 0.8)) * 45 + max(0, 1 - cost / max(budget, 1)) * 35 \
            + max(0, 1 - rain / 100) * 20
        out.append({
            "dest": name, "crowd_index": round(idx, 3),
            "level_text": analyze.LEVEL_TEXT.get(analyze.level(idx), "-"),
            "cost": cost, "within_budget": cost <= budget,
            "rain_prob": rain, "fastest": min((c["min_duration"] for c in cs if c["min_duration"]), default=""),
            "recommend_days": DAYS.get(name, 2), "score": round(score, 1),
        })
    out.sort(key=lambda x: -x["score"])
    return {"query": {"days": days, "budget": budget, "pref": pref}, "items": out}


def tool_budget(dest: str, days: int = 2, holiday: bool = False) -> dict:
    """估算一次往返旅行的总花费（元）。holiday=True 时住宿按假日价 ×2（国庆等假期房价普遍翻倍）。"""
    if dest not in PRICE:
        return {"error": f"未收录目的地：{dest}"}
    transport = PRICE[dest] * 2
    uplift = 2.0 if holiday else 1.0
    stay = round(HOTEL[dest] * max(0, days - 1) * uplift)
    food = 90 * days
    return {"dest": dest, "days": days, "transport": transport, "stay": stay,
            "food": food, "total": transport + stay + food,
            "stay_uplift": uplift,
            "note": "车票按二等座/硬座基准；住宿为经济型快捷酒店平日价，节假日 ×2、周五/周六晚 ×1.4；餐饮 90 元/天。两天行程按 1 晚住宿计。"}


# ── AgentArts 侧的 Schema 声明（可直接复制到平台的自定义工具里） ─────────────
TOOLS_SPEC = [
    {
        "name": "get_crowd_matrix",
        "description": "查询长春出发各目的地未来若干天的拥挤度矩阵与天气，用于回答哪天人多、哪天人少",
        "parameters": {
            "type": "object",
            "properties": {"days": {"type": "integer", "description": "查询天数，默认 8"}},
        },
    },
    {
        "name": "recommend_destinations",
        "description": "按天数、预算、偏好推荐目的地，返回排序后的候选清单与依据",
        "parameters": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "出行天数"},
                "budget": {"type": "integer", "description": "总预算（元）"},
                "pref": {"type": "string", "description": "偏好：自然/城市/网红，可为空"},
            },
            "required": ["days", "budget"],
        },
    },
    {
        "name": "estimate_budget",
        "description": "估算指定目的地、指定天数的往返总花费",
        "parameters": {
            "type": "object",
            "properties": {
                "dest": {"type": "string", "description": "目的地名称"},
                "days": {"type": "integer", "description": "出行天数"},
                "holiday": {"type": "boolean", "description": "是否节假日出行（ true 时住宿按假日价 ×2）"},
            },
            "required": ["dest"],
        },
    },
]

REGISTRY = {
    "get_crowd_matrix": tool_get_crowd_matrix,
    "recommend_destinations": tool_recommend,
    "estimate_budget": tool_budget,
}


def call(name: str, args: dict | None = None):
    fn = REGISTRY.get(name)
    if not fn:
        return {"error": f"未知工具：{name}"}
    try:
        return fn(**(args or {}))
    except TypeError as e:
        return {"error": f"参数不匹配：{e}"}


if __name__ == "__main__":
    print(json.dumps(TOOLS_SPEC, ensure_ascii=False, indent=2))
