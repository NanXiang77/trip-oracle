"""拥挤指数：把余票压力换算成 1~5 档，供展示与预测共用。

为什么不直接看"余票张数"：
  不同目的地车次数量差异巨大（吉林 60+ 趟、白河 10 趟以内），
  绝对张数不可比，必须归一化成"售罄比例 + 座位宽松度"。
"""
from __future__ import annotations

import time

SEAT_REF = 120          # 每趟车"宽松"的参考座位数
LEVEL_CUTS = (0.15, 0.30, 0.50, 0.70)
LEVEL_TEXT = {1: "舒适", 2: "较舒适", 3: "一般", 4: "拥挤", 5: "很挤"}


def crowd_index(d: dict) -> float:
    known = d.get("trains_known") or 0
    if not known:
        return -1.0
    soldout_ratio = (d.get("trains_soldout") or 0) / known
    loose = min(1.0, (d.get("seats_total") or 0) / (known * SEAT_REF))
    return round(0.7 * soldout_ratio + 0.3 * (1 - loose), 4)


def level(idx: float) -> int:
    if idx < 0:
        return 0
    for i, cut in enumerate(LEVEL_CUTS, start=1):
        if idx < cut:
            return i
    return 5


def build_crowd_rows(daily_rows: list[dict]) -> list[dict]:
    """把日汇总行合成拥挤等级。

    daily_rows 现在同时含去程（direction='out'）和回程（'return'），
    同一 (目的地, 日期) 取两侧中更紧的指数——"去得了回不来"也算不可行。
    """
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    grouped: dict[tuple[str, str], list[float]] = {}
    for d in daily_rows:
        idx = crowd_index(d)
        if idx < 0:
            continue
        grouped.setdefault((d["dest"], d["travel_date"]), []).append(idx)
    out = []
    for (dest, dt), idxs in sorted(grouped.items()):
        idx = max(idxs)
        out.append({
            "travel_date": dt,
            "dest": dest,
            "crowd_index": idx,
            "crowd_level": level(idx),
            "computed_at": now,
        })
    return out


# ---------------- 天气分类评分（雪/雨区别对待） ----------------
SNOW_CODES = {71, 73, 75, 77, 85, 86}          # 降雪类
RAIN_CODES = {51, 53, 55, 56, 57, 61, 63, 80, 81}   # 小到中雨/冻雨
HEAVY_CODES = {65, 66, 67, 82, 95, 96, 99}     # 大雨/雷暴/极端


def weather_flag(code, dest_tags: list[str] | None = None, month: int | None = None) -> dict:
    """把天气码分类成 (标签, 惩罚 0~1)。惩罚为负表示加分（如自然型目的地降雪）。

    - 降雪对"自然"型目的地（冬季）是卖点，不减分反而加；
    - 对城市型是出行不便，轻度减分；
    - 降雨一律减分，强降雨/雷暴重减。
    """
    tags = dest_tags or []
    winter = (month in (11, 12, 1, 2, 3)) if month else True
    code = int(code) if code is not None else None
    if code is None:
        return {"flag": "", "penalty": 0.0}
    if code in HEAVY_CODES:
        return {"flag": "强降雨/雷暴", "penalty": 0.6}
    if code in SNOW_CODES:
        if "自然" in tags and winter:
            return {"flag": "降雪·观景加分", "penalty": -0.15}
        return {"flag": "降雪·路面湿滑", "penalty": 0.15}
    if code in RAIN_CODES:
        return {"flag": "降雨", "penalty": 0.35}
    return {"flag": "", "penalty": 0.0}


# ---------------- 假期/周末拥挤放大系数 ----------------
# 背景：拥挤指数来自余票压力与热度反演，两者在假期都会系统性低估
#（淡季余票 0 → 指数 0，看不出假期差异）。对不同假期乘不同倍数，
# 周末小倍数；乘法保序——平日无压力的目的地乘完仍不凸显。
# 倍数为经验估计（对照 2026 国庆实测客流），待真值积累后校准。
HOLIDAY_WINDOWS = {
    # (起始月, 日, 结束月, 日, 倍数)
    (10, 1, 10, 8, 2.2),    # 国庆（8天，最挤）
    (12, 31, 1, 2, 1.6),    # 元旦（跨年小长假）
    (4, 4, 4, 6, 1.5),      # 清明
    (5, 1, 5, 5, 1.9),      # 五一
    (6, 12, 6, 14, 1.4),    # 端午（2027 参考）
    (9, 15, 9, 17, 1.5),    # 中秋（2027 参考）
    (2, 5, 2, 11, 1.8),     # 春节（2027，出行集中但旅游客流次于国庆）
}
WEEKEND_MULT = {4: 1.25, 5: 1.25, 6: 1.05}   # 周五/周六 1.25，周日 1.05


def holiday_multiplier(d) -> float:
    """给定 date，返回拥挤度放大系数。假期 > 周末，取 max。"""
    m = (d.month, d.day)
    for m1, d1, m2, d2, k in HOLIDAY_WINDOWS:
        if (m1, d1) <= (m2, d2):          # 同年窗口
            if (m1, d1) <= m <= (m2, d2):
                return k
        else:                              # 跨年窗口（如元旦 12/31~1/2）
            if m >= (m1, d1) or m <= (m2, d2):
                return k
    return WEEKEND_MULT.get(d.weekday(), 1.0)
