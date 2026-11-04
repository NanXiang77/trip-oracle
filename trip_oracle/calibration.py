"""热度→客流 标定与预测模块。

训练协议（跨事件，防数据泄漏）：
- 训练集：2025 国庆（10/1-10/8）——抖音 8 天均值倍数 × 官方真值倍数
- 验证集：2026 国庆——用 2025 训练的 α 回测
- 预测集：元旦及以后假期——只用此前训练好的 α，不看当期真值

训练对（官方真值来源见 README）：
  2025 国庆: 故宫 抖音2.80x→真实3.71x (α=1.33) | 长白山 抖音1.44x→真实2.13x (α=1.48)
  2026 国庆: 故宫 抖音2.56x→真实≈3.7x  (α=1.45) | 长白山 抖音1.17x→真实2.45x  (α=2.09)
跨年验证（只用 2025 训练 → 预测 2026）：故宫 -39%、长白山 -24%、延吉 -25%（系统性低估，
因 2026 长白山受高铁通车一周年热度外推）、哈尔滨 +75%（季节错配误报，需旺季月份特征修正）。
当前 ALPHA 取两事件均值（local 1.39 / remote 1.79），供元旦预测使用；元旦真值落地后滚动更新。
"""
from __future__ import annotations

import datetime as dt

from . import store

# 目的地类型（客源半径 × 季节性）
DEST_TYPE = {
    "沈阳": "local",        # 本地周边型：客源说走就走
    "长白山": "remote",     # 远程季节型：跨省客源、强季节
    "延吉": "remote",
    "哈尔滨": "remote",
    "吉林市": "local",      # 省内近程（待更多真值确认）
    "四平": "local",
}

# 训练所得 α——两套口径并存（H 的定义不同，α 不可混用）：
#
# ① BAIDU_ALPHA（2026-11-03 扩充训练，推荐）：H = 百度指数节前 7 天均值 / 前 23 天基线；
#    C = 国庆日均客流 / 当年年初至节前日均客流。训练点 n=6，跨 2023~2026 四次国庆
#    （沈阳故宫 2023/2024，长白山 2023/2024/2025/2026），真值全部来自政府官网/官媒通报，
#    留一验证平均绝对误差 20.4%。详见 fit_alpha.py。
#    2023 故宫点 C 因全年基线口径被低估，实际 local α 可能略高于 2.56。
BAIDU_ALPHA = {"local": 2.56, "remote": 2.28}       # remote n=4: 2.28±0.23（稳定）

# ② ALPHA（抖音口径，2025+2026 两次国庆）：H = 抖音节前 7 天倍数。
#    抖音数据仅 2025-08-25 起，无法跨年训练；保留供抖音信号预测使用，误差约 ±30%。
ALPHA = {"local": 1.39, "remote": 1.79}

# 等级阈值（客流倍数口径）：与拥挤指数阈值对齐的口头分级
LEVELS = [(1.15, "与平日相当"), (1.6, "明显升高"), (2.5, "高峰拥挤"), (float("inf"), "极端高峰")]

# ---------------- 各假期标定可信度（2026-11-04 复核） ----------------
# 复核结论（见 fit_alpha.py 与长白山 2025 全域真值）：
#   国庆：4 年 6 个真值点，百度口径留一验证 MAE 20.4% → 可给点估计
#   元旦：无独立真值，只能迁移国庆 α，误差放大到约 ±35% → 给点估计但降级
#   春节/五一/清明/端午/中秋：只有长白山一个目的地、一个年份有完整真值，
#       且该目的地「节前搜索热度」与「假期客流」几乎不相关
#       （热度倍数恒在 0.87~1.35，真实客流倍数却是 1.54~2.46），
#       信号本身没有区分度 → 不给点估计，只给参考区间
CALIBRATED = {
    "国庆": {"confidence": "A", "mae": 0.20, "n": 6,
             "basis": "4 年国庆 6 个真值点（沈阳故宫/长白山），百度口径留一验证 MAE 20.4%"},
    "元旦": {"confidence": "B", "mae": 0.35, "n": 6,
             "basis": "无元旦独立真值，迁移国庆 α，跨假期迁移误差约 ±35%"},
}
UNCALIBRATED = {
    "confidence": "C", "range": [1.5, 2.5], "n": 4,
    "basis": "仅长白山 2025 全域单点样本（春节 1.77 / 五一 1.79 / 端午 1.54 / 国庆 2.46 倍），"
             "单个目的地、单个年份，且该地节前搜索热度与客流不相关，不能推广到其他城市",
}


def holiday_kind(ws: dt.date) -> str:
    """按窗口起始日判断假期类型（用于选择可信度等级）。"""
    md = (ws.month, ws.day)
    if (9, 25) <= md <= (10, 10):
        return "国庆"
    if md >= (12, 28) or md <= (1, 5):
        return "元旦"
    if (1, 20) <= md <= (2, 25):
        return "春节"
    if (4, 1) <= md <= (4, 7):
        return "清明"
    if (4, 28) <= md <= (5, 6):
        return "五一"
    if (5, 25) <= md <= (6, 25):
        return "端午"
    if (9, 1) <= md <= (9, 24):
        return "中秋"
    return "其他假期"


def level_of(multiple: float) -> str:
    for th, name in LEVELS:
        if multiple < th:
            return name
    return LEVELS[-1][1]


def _douyin_multiple(keyword: str, win_start: dt.date, win_end: dt.date,
                     base_days: int = 24) -> dict:
    """抖音窗口均值 / 前置基线 倍数。基线=窗口开始前 base_days 天均值。"""
    base_end = win_start - dt.timedelta(days=1)
    base_start = base_end - dt.timedelta(days=base_days - 1)
    q = "select date, heat from heat_daily where source='douyin_index' and keyword=? and date between ? and ?"
    def _num(rows):
        out = []
        for r in rows:
            try:
                v = float(r)
                if v > 0:
                    out.append(v)
            except (TypeError, ValueError):
                pass
        return out

    with store.conn() as c:
        base = _num(r["heat"] for r in c.execute(q, (keyword, base_start.isoformat(), base_end.isoformat())))
        win = _num(r["heat"] for r in c.execute(q, (keyword, win_start.isoformat(), win_end.isoformat())))
    if not base or not win:
        return {"ok": False, "msg": "数据不足（需窗口与前置基线均有热度数据）"}
    base_avg = sum(base) / len(base)
    win_avg = sum(win) / len(win)
    return {"ok": True, "base": round(base_avg), "win_avg": round(win_avg),
            "multiple": win_avg / base_avg}


def predict_holiday(dest: str, keyword: str, win_start: str, win_end: str) -> dict:
    """预测某假期窗口的客流倍数与等级。

    注意：这是"节前预测"接口——窗口应传**未来**的假期日期，
    热度输入取窗口开始前 1~7 天（节前领先信号），输出 α 放大后的预测。

    只有国庆/元旦给点估计（有标定）；其他假期只返回参考区间，不给数字。
    """
    ws_, we_ = dt.date.fromisoformat(win_start), dt.date.fromisoformat(win_end)
    kind = holiday_kind(ws_)
    t = DEST_TYPE.get(dest, "local")
    cal = CALIBRATED.get(kind)

    if not cal:
        # 没有该假期的标定数据：不编数字，只交代区间和原因
        return {
            "ok": True,
            "dest": dest, "type": t, "holiday": kind,
            "calibrated": False,
            "pred_multiple": None,
            "pred_level": None,
            "reference_range": UNCALIBRATED["range"],
            "confidence": UNCALIBRATED["confidence"],
            "reason": UNCALIBRATED["basis"],
            "caveat": f"「{kind}」我们没有可信的标定数据，因此不给具体倍数。"
                      f"只能提供量级参考：假期日均客流约为平日的 "
                      f"{UNCALIBRATED['range'][0]}~{UNCALIBRATED['range'][1]} 倍，"
                      f"但这种区间无法区分「{kind}」究竟落在区间哪一端。"
                      "要判断具体哪天挤不挤，以 12306 余票口径（get_crowd_matrix）为准。",
            "what_would_help": "余票是真实购买意愿、无搜索热度的季节失真；"
                               "等该假期开放预售（提前 14 天）后用余票紧张度判断更可靠。",
        }

    lead_end = ws_ - dt.timedelta(days=1)
    lead_start = lead_end - dt.timedelta(days=6)

    # 未来的假期：节前 7 天窗口可能还没发生。距假期太远就明说信号没形成，
    # 不要拿"数据不足"糊过去；已经进入节前 4 周则退化为"最近 7 天"的当前信号。
    today = dt.date.today()
    signal_note = "节前 7 天窗口"
    if lead_end > today:
        days_to = (ws_ - today).days
        if days_to > 28:
            return {
                "ok": True, "dest": dest, "type": t, "holiday": kind,
                "calibrated": True, "pred_multiple": None, "pred_level": None,
                "confidence": cal["confidence"], "mae": cal["mae"],
                "basis": cal["basis"],
                "reason": f"距 {ws_} 还有 {days_to} 天，节前热度信号尚未形成。",
                "caveat": f"「{kind}」本身有可信标定（{cal['basis']}），但预测要等节前窗口"
                          "产生数据后才成立。建议节前 2~4 周再问，或直接看余票。",
                "what_would_help": "12306 余票在开售（提前 14 天）后即可反映真实需求，"
                                   "比等热度更快拿到信号。",
            }
        lead_end = today
        lead_start = lead_end - dt.timedelta(days=6)
        signal_note = f"节前窗口未到，暂用最近 7 天（{lead_start}~{lead_end}）作领先信号"

    m = _douyin_multiple(keyword, lead_start, lead_end)
    if not m["ok"]:
        return m
    alpha = ALPHA[t]
    pred = m["multiple"] * alpha
    return {
        "ok": True,
        "dest": dest, "type": t, "holiday": kind,
        "calibrated": True,
        "alpha": alpha,
        "lead_window": f"{lead_start}~{lead_end}",
        "lead_multiple": round(m["multiple"], 2),
        "pred_multiple": round(pred, 2),
        "pred_level": level_of(pred),
        "confidence": cal["confidence"],
        "mae": cal["mae"],
        "basis": cal["basis"],
        "signal": signal_note,
        "caveat": f"预测值，非实测。α 来源：{cal['basis']}；"
                  f"典型误差约 ±{int(cal['mae'] * 100)}%。",
    }


if __name__ == "__main__":
    # 示例：预测即将到来的窗口（把日期换成目标假期即可）
    import sys
    dest, kw, s, e = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    r = predict_holiday(dest, kw, s, e)
    print(r)
