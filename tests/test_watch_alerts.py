"""watch 模块自检：用临时数据库 + 合成快照验证售罄事件与四条预警规则。

为什么要造数据：真实的售罄场景一年只有几次（元旦、春节），
等真数据来验证规则太慢，而规则错了会在关键时刻给出错误预警或静默漏报。

运行：
    python -m tests.test_watch_alerts
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime

from trip_oracle import store, watch


def _snap(ts: str, td: str, dest: str, direction: str, sold: set[int], total: int = 10):
    """造一批车次快照：sold 里的序号设为全席别 0（售罄），其余设为有票。"""
    rows = []
    for i in range(1, total + 1):
        code = f"G{600+i}"
        seats = {"ze": 0 if i in sold else 50, "yw": 0 if i in sold else 3}
        rows.append({
            "fetched_at": ts, "travel_date": td, "dest": dest, "direction": direction,
            "train_code": code, "depart_time": "08:00", "arrive_time": "10:00",
            "duration": "02:00", "seats_json": json.dumps(seats),
        })
    return rows


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="oracle_test_")
    store.DB_PATH = os.path.join(tmp, "test.db")
    store.init_db()

    td, dest, direction = "2027-01-01", "沈阳", "out"
    # 开售后 08:00 全有票 → 10:00 卖掉 5 趟 → 12:00 卖掉 7 趟
    store.save_train_rows(_snap("2026-12-17 08:00:00", td, dest, direction, set()))
    store.save_train_rows(_snap("2026-12-17 10:00:00", td, dest, direction, {1, 2, 3, 4, 5}))
    store.save_train_rows(_snap("2026-12-17 12:00:00", td, dest, direction, {1, 2, 3, 4, 5, 6, 7}))

    n = watch.derive_events([td], [dest])
    m = watch.metrics(td, dest, direction)
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(f"  {'✓' if cond else '✗'} {name} {detail}")
        ok = ok and cond

    print("售罄事件 / 指标")
    check("事件条数 = 10 趟", n == 10, f"(实际 {n})")
    check("售罄 7 趟", m["soldout_n"] == 7, f"(实际 {m['soldout_n']})")
    check("当前售罄率 70%", abs(m["soldout_rate"] - 0.7) < 1e-6, f"(实际 {m['soldout_rate']})")
    check("24h 内最高售罄率 70%", abs(m["soldout_rate_24h"] - 0.7) < 1e-6,
          f"(实际 {m['soldout_rate_24h']})")
    check("最短售罄耗时 2.0h", m["min_hours"] == 2.0, f"(实际 {m['min_hours']})")
    check("首次观测售罄率为 0（诊断字段）", m["soldout_rate_first"] == 0.0,
          f"(实际 {m['soldout_rate_first']})")

    print("预警规则")
    got = {a["rule"] for a in store.alerts(50)}
    alerts = watch.check_alerts([td])
    got |= {a["rule"] for a in store.alerts(50)}
    check("A1 触发（24h 售罄率 70% ≥ 30%）", "A1" in got)
    check("A2 触发（2.0h ≤ 2h 极速售罄）", "A2" in got)
    check("A3 触发（50%→70%，+20pp）", "A3" in got)

    # 去重：同一 (rule, dest, date) 只应有一条
    n_a1 = len([a for a in store.alerts(50) if a["rule"] == "A1"])
    check("A1 去重后仅 1 条", n_a1 == 1, f"(实际 {n_a1})")

    print("A4 采集异常")
    for _ in range(watch.TH_FAIL_STREAK):
        rid = store.start_watch_run([td], [dest])
        store.finish_watch_run(rid, False, 0, 0, "simulated failure")
    watch.check_alerts([td])
    check("A4 触发（连续 3 轮失败）",
          any(a["rule"] == "A4" for a in store.alerts(50)))

    print("\n" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
