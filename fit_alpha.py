"""用 2023~2026 四次国庆的「节前热度倍数 × 实际客流倍数」重新拟合 α。

客流真值全部来自公开通报（政府官网/官媒），口径见 TRUTHS 注释。
热度 H 从 heat_daily（baidu_index，2023-08 起的历史补抓）读取：
    H = 节前 7 天均值 / 再往前 23 天基线均值
拟合：α_i = C_i / H_i；按 DEST_TYPE 分组给均值 ± 标准差，并做留一验证。

用法：python fit_alpha.py
"""
from __future__ import annotations

import statistics as st
from datetime import date

from trip_oracle import store

# ---------------- 真值（来源均为官网/官媒公开通报） ----------------
# holiday: (win_start, win_end)
HOLIDAYS = {
    2023: (date(2023, 9, 29), date(2023, 10, 6)),   # 中秋国庆连休 8 天
    2024: (date(2024, 10, 1), date(2024, 10, 7)),   # 7 天
    2025: (date(2025, 10, 1), date(2025, 10, 8)),   # 中秋国庆连休 8 天
    2026: (date(2026, 10, 1), date(2026, 10, 7)),   # 7 天
}

# (dest, keyword, year, C, note)
TRUTHS = [
    # 沈阳故宫：2023 国庆 8 天 32.7786 万（沈阳文旅局）；2023 全年 440.85 万（故宫官网年报）
    ("沈阳", "沈阳故宫", 2023, 32.7786 / 8 / (440.85 / 365), "2023国庆32.78万/8天;全年440.85万→日均基线，C 略被低估"),
    # 2024 国庆 7 天 25.48 万（辽报）；2024-01-01~10-07 累计 431.51 万 → 年初至 9/30 日均
    ("沈阳", "沈阳故宫", 2024, 25.48 / 7 / ((431.51 - 25.48) / 274), "2024国庆25.48万/7天;年初至9/30基线406.03万/274天"),
    # 长白山主景区（管委会经发局）：2023 国庆 16.65 万/8天，截至 10-06 累计 237.06 万
    ("长白山", "长白山", 2023, 16.65 / 8 / ((237.06 - 16.65) / 271), "2023国庆16.65万/8天;年初至9/28基线220.41万/271天"),
    # 2024 国庆 18.18 万/7天，截至 10-07 累计 295.6 万
    ("长白山", "长白山", 2024, 18.18 / 7 / ((295.6 - 18.18) / 274), "2024国庆18.18万/7天;年初至9/30基线277.42万/274天"),
    # 2025 国庆 22.17 万/8天（+6.71%）；无 2025 年内累计 → 用 2024 年初至今基线近似
    ("长白山", "长白山", 2025, 22.17 / 8 / ((295.6 - 18.18) / 274), "2025国庆22.17万/8天;基线沿用2024年初至今日均（近似）"),
    # 2026 国庆满额 4.1 万/日（预约上限）；9 月日均 1.67 万（50.12万/30天）→ 基线偏旺，C 偏低
    ("长白山", "长白山", 2026, 4.1 / 1.67, "2026国庆满额4.1万/日;9月日均1.67万为基线（旺季口径，C 偏低）"),
]

# 有真值但缺平日基线，暂不进拟合（报告里说明）
UNFITTED = [
    "延吉 2024 国庆 91.3万/7天（+7.2%）——无延吉平日日均",
    "哈尔滨 2024 国庆全市 835.7万/7天、中央大街 97.86万/7天——无平日基线",
    "沈阳方城 2024 国庆 812.27万/7天、中街 763.16万/7天——街区无闸机口径",
    "延边州 2025/2026 国庆 543.44万/612.31万——州域口径无平日基线",
]

LEAD = 7      # 节前窗口天数
BASE = 23     # 基线天数


def heat_multiple(keyword: str, year: int) -> dict:
    ws, _ = HOLIDAYS[year]
    lead_end = ws.fromordinal(ws.toordinal() - 1)
    lead_start = lead_end.fromordinal(lead_end.toordinal() - LEAD + 1)
    base_end = lead_start.fromordinal(lead_start.toordinal() - 1)
    base_start = base_end.fromordinal(base_end.toordinal() - BASE + 1)
    q = ("select date, heat from heat_daily where source='baidu_index' "
         "and keyword=? and date between ? and ?")
    with store.conn() as c:
        def avg(a, b):
            vals = [r["heat"] for r in c.execute(q, (keyword, a.isoformat(), b.isoformat()))]
            vals = [float(v) for v in vals if v and float(v) > 0]
            return sum(vals) / len(vals) if vals else None, len(vals)
        b, nb = avg(base_start, base_end)
        w, nw = avg(lead_start, lead_end)
    if not b or not w:
        return {"ok": False, "msg": f"热度不足 base={nb} win={nw}"}
    return {"ok": True, "H": w / b, "base": round(b), "win": round(w),
            "win_days": nw, "base_days": nb}


def main() -> int:
    points = []
    for dest, kw, year, C, note in TRUTHS:
        m = heat_multiple(kw, year)
        if not m["ok"]:
            print(f"[skip] {dest} {year}: {m['msg']}")
            continue
        points.append({"dest": dest, "kw": kw, "year": year, "C": C,
                       "H": m["H"], "note": note, **m})
    print(f"\n=== 训练点（n={len(points)}）===")
    print(f"{'地点':<6}{'年':<6}{'H热度倍数':<10}{'C客流倍数':<10}{'α=C/H':<8}")
    for p in points:
        p["alpha"] = p["C"] / p["H"]
        print(f"{p['dest']:<6}{p['year']:<6}{p['H']:<10.2f}{p['C']:<10.2f}{p['alpha']:<8.2f}"
              f"  win={p['win']}/base={p['base']}  # {p['note'][:38]}")

    from trip_oracle.calibration import DEST_TYPE
    print("\n=== 分组拟合 ===")
    for t in ("local", "remote"):
        alphas = [p["alpha"] for p in points if DEST_TYPE.get(p["dest"]) == t]
        if not alphas:
            continue
        mean = st.mean(alphas)
        sd = st.stdev(alphas) if len(alphas) > 1 else 0.0
        print(f"{t}: n={len(alphas)}  α = {mean:.2f} ± {sd:.2f}  "
              f"(范围 {min(alphas):.2f}~{max(alphas):.2f})")

    print("\n=== 留一验证（用其余点拟合并预测该点） ===")
    errs = []
    for i in range(len(points)):
        train = [p for j, p in enumerate(points) if j != i]
        t = DEST_TYPE.get(points[i]["dest"])
        pool = [p["alpha"] for p in train if DEST_TYPE.get(p["dest"]) == t] or \
               [p["alpha"] for p in train]
        a = st.mean(pool)
        pred = points[i]["H"] * a
        err = (pred - points[i]["C"]) / points[i]["C"]
        errs.append(abs(err))
        print(f"{points[i]['dest']}{points[i]['year']}: 预测 {pred:.2f} vs 实际 "
              f"{points[i]['C']:.2f}  误差 {err:+.1%}")
    if errs:
        print(f"平均绝对误差: {st.mean(errs):.1%}")
    print("\n暂未纳入拟合（缺平日基线）：")
    for u in UNFITTED:
        print("  -", u)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
