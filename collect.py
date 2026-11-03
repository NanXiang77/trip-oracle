"""采集入口：每运行一次，把未来 N 天的余票 + 天气落库。

用法：
    python collect.py            # 采集全部目的地
    python collect.py 吉林市 沈阳  # 只采集指定目的地
"""
from __future__ import annotations

import os
import sys
import time

from trip_oracle import analyze, store
from trip_oracle.collectors import heat, train, weather
from trip_oracle.config import DESTINATIONS, FORECAST_DAYS, ORIGIN_CODE


def main(argv: list[str]) -> int:
    store.init_db()
    codes = train.station_codes()
    if not codes:
        print("[!] 车站码表下载失败，12306 可能暂时不可用")
        return 1

    wanted = set(argv[1:])
    dests = [d for d in DESTINATIONS if not wanted or d["name"] in wanted]

    all_daily = []
    for d in dests:
        code = codes.get(d["station"])
        if not code:
            print(f"[!] 找不到车站 {d['station']} 的电报码，跳过 {d['name']}")
            continue
        t0 = time.time()
        # 去程：长春 -> 目的地
        rows, daily = train.collect(d["name"], code, FORECAST_DAYS, direction="out")
        store.save_train_rows(rows)
        store.save_train_daily(daily)
        all_daily.extend(daily)
        n_out = len(rows)
        # 回程：目的地 -> 长春（同样覆盖未来 N 天，供不同行程长度选用）
        rrows, rdaily = train.collect(d["name"], ORIGIN_CODE, FORECAST_DAYS,
                                      direction="return", from_code=code)
        store.save_train_rows(rrows)
        store.save_train_daily(rdaily)
        all_daily.extend(rdaily)

        w = weather.collect(d["name"], d["lat"], d["lon"], FORECAST_DAYS)
        store.save_weather(w)

        print(f"[ok] {d['name']:<5} 去程 {n_out:>3} 条 / 回程 {len(rrows):>3} 条 "
              f"/ 日期 {len(daily) + len(rdaily)} 行 / 天气 {len(w)} 天 / {time.time() - t0:.1f}s")

    store.save_crowd(analyze.build_crowd_rows(all_daily))
    print("\n库内统计：", store.stats())

    # 搜索热度（Q1 领先指标）：百度指数近 90 天，qdata 签名链路
    # 云端部署没有浏览器 cookie / qdata，设 SKIP_HEAT=1 直接跳过（余票与天气才是主口径）
    if os.environ.get("SKIP_HEAT") == "1":
        print("[heat] SKIP_HEAT=1，跳过百度/抖音指数（云端无 cookie，属预期行为）")
    else:
        try:
            hres = heat.collect_all(days=90)
            ok = sum(1 for v in hres.values() if v["ok"])
            print(f"[ok] 搜索热度 {ok}/{len(hres)} 词成功"
                  + ("" if ok == len(hres) else "（失败多为 cookie 过期，更新 data/baidu_cookie.txt）"))
        except Exception as e:
            print(f"[!] 搜索热度采集失败（不影响主流程）：{e}")

        # 抖音指数（creator.douyin.com，需已登录；未登录静默跳过）
        try:
            from trip_oracle.collectors import douyin
            dres = douyin.collect(days=90, wait_login=False)
            dok = sum(1 for v in dres.values() if v["ok"])
            if any("未登录" in v["msg"] for v in dres.values()):
                print("[douyin] 未登录，跳过（手动运行 python -m trip_oracle.collectors.douyin 可扫码登录）")
            else:
                print(f"[ok] 抖音指数 {dok}/{len(dres)} 词成功")
        except Exception as e:
            print(f"[!] 抖音指数采集失败（不影响主流程）：{e}")

    # 控制台预览：按目的地 × 日期打印拥挤等级（去/回取更紧一侧）
    print("\n拥挤等级（1 舒适 … 5 很挤，数字为该日拥挤指数，已含回程压力）")
    grouped: dict[str, dict[str, list[float]]] = {}
    for row in store.latest_train_daily():
        idx = analyze.crowd_index(row)
        if idx < 0:
            continue
        grouped.setdefault(row["dest"], {}).setdefault(row["travel_date"], []).append(idx)
    dates = sorted({d for m in grouped.values() for d in m})
    header = "目的地".ljust(8) + "".join(d[5:].rjust(9) for d in dates)
    print(header)
    for name, m in grouped.items():
        cells = []
        for dt in dates:
            idxs = m.get(dt)
            if not idxs:
                cells.append("     -".rjust(9))
                continue
            idx = max(idxs)
            cells.append(f"{analyze.LEVEL_TEXT.get(analyze.level(idx), '?')}{idx:.2f}".rjust(9))
        print(name.ljust(8) + "".join(cells))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
