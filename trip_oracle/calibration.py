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
    热度输入取窗口开始前 1~24 天（节前领先信号），输出 α 放大后的预测。
    """
    ws_, we_ = dt.date.fromisoformat(win_start), dt.date.fromisoformat(win_end)
    lead_end = ws_ - dt.timedelta(days=1)
    lead_start = lead_end - dt.timedelta(days=6)
    m = _douyin_multiple(keyword, lead_start, lead_end)
    if not m["ok"]:
        return m
    t = DEST_TYPE.get(dest, "local")
    alpha = ALPHA[t]
    pred = m["multiple"] * alpha
    return {
        "ok": True,
        "dest": dest, "type": t, "alpha": alpha,
        "lead_window": f"{lead_start}~{lead_end}",
        "lead_multiple": round(m["multiple"], 2),
        "pred_multiple": round(pred, 2),
        "pred_level": level_of(pred),
        "caveat": "α 为抖音口径（2025+2026 国庆，n=2，误差约±30%）；"
                  "百度口径四年训练 α 见 BAIDU_ALPHA（n=6，留一MAE 20.4%），",
    }


if __name__ == "__main__":
    # 示例：预测即将到来的窗口（把日期换成目标假期即可）
    import sys
    dest, kw, s, e = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    r = predict_holiday(dest, kw, s, e)
    print(r)
