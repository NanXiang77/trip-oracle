"""前向观测与预警：盯住目标假期的余票，记录"什么时候卖完"。

为什么需要这一层
----------------
回放面板（app.replay_matrix）是在**拿不到历史余票**时的替代品：用搜索热度反演拥挤。
但真实预测场景里，余票是**提前 14 天预售**的（实测 2026-11-03：今天 +14 天可查，+15 天为空）。
也就是说：
    12/17  → 开售 12/31（元旦）的票
    12/17 起，元旦当天的供给压力就直接可见，不需要任何反演。

所以元旦的正确主力信号不是热度（三年元旦热度倍数 0.72~1.40，无稳定方向，已实测确认），
而是余票——尤其是**售罄速度**：同样卖完，开售 2 小时售罄和开售 5 天售罄，
代表的需求强度差一个量级。这个信息只能靠高频快照累积，事后无法补采，所以必须提前跑起来。

用法
----
    python -m trip_oracle.watch --once              # 采一次（计划任务调这个）
    python -m trip_oracle.watch --loop --interval 60  # 常驻，每小时一次
    python -m trip_oracle.watch --status            # 看当前进度与预警
    python -m trip_oracle.watch --once --dry-run    # 只查询不落库

调度建议（12/16 起）：
    schtasks /create /tn "trip-oracle-watch" /tr "C:\\...\\run_watch.bat" /sc hourly /mo 2
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import date, datetime, timedelta

from . import store
from .collectors import train
from .config import DESTINATIONS, ORIGIN_CODE

# ---------------- 目标假期 ----------------
# 只登记"尚未开售或正在预售"的假期；日期写死，避免依赖"今天+N"的滚动窗口。
TARGETS: dict[str, dict] = {
    "元旦2027": {
        "out":    [date(2026, 12, 31), date(2027, 1, 1), date(2027, 1, 2)],
        "return": [date(2027, 1, 2), date(2027, 1, 3)],
        # 已知的结构性事实（不依赖数据）：1 月初是长白山/哈尔滨冰雪季峰值
        "season_note": "冰雪季峰值：长白山、哈尔滨为年内高位；沈阳、吉林市为淡季",
    },
}

# 12306 各站起售时刻（HH:MM）。各站不同且会调整，未核实的站点留空：
# 留空时 hours_to_sold 以"首次观测到该车次"为起点，是真实耗时的**上界**（偏大）。
PRESALE_AT: dict[str, str] = {}

# ---------------- 预警阈值 ----------------
TH_FIRST_DAY_SOLDOUT = 0.30    # A1：开售首日售罄率 ≥30% → 需求强
TH_FAST_SOLD_HOURS = 2.0       # A2：任意车次 2 小时内售罄 → 极端需求
TH_JUMP_PP = 0.20              # A3：售罄率较上次观测跳增 ≥20 个百分点
TH_FAIL_STREAK = 3             # A4：连续 3 次采集失败 → 管道异常


def _log(msg: str):
    print(f"[{datetime.now():%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------- 采集 ----------------
def open_targets(today: date | None = None) -> tuple[list[date], list[tuple[date, int]]]:
    """把目标日期分成"已进预售窗口 / 还没开售"两堆。"""
    today = today or date.today()
    horizon = today + timedelta(days=train.presale_days())
    ready, waiting = [], []
    for t in TARGETS.values():
        for d in t["out"] + t["return"]:
            if d <= today:            # 已过期，不采
                continue
            if d <= horizon:
                if d not in ready:
                    ready.append(d)
            else:
                waiting.append((d, (d - today).days - train.presale_days()))
    ready.sort()
    # 去重（去程/回程可能有同一天，如 1/2 既返程又去程）
    waiting = sorted(dict.fromkeys(waiting), key=lambda x: (x[0], x[1]))
    return ready, waiting


def snapshot(dates: list[date] | None = None, dests: list[str] | None = None,
             dry_run: bool = False) -> dict:
    """抓一轮目标日期的余票，落库并重算售罄事件。"""
    store.init_db()
    today = date.today()
    horizon = today + timedelta(days=train.presale_days())

    if dates is None:
        dates, _ = open_targets(today)
    dates = [d for d in dates if d <= horizon]
    if not dates:
        nxt = min([d for t in TARGETS.values() for d in t["out"] + t["return"]
                   if d > horizon], default=None)
        _log("没有进入预售窗口的目标日期，跳过本轮（下一个目标 %s，距开售 %s 天）"
             % (nxt, (nxt - horizon).days if nxt else "-"))
        return {"ok": True, "queries": 0, "rows": 0, "skipped": "no_target_in_presale"}

    codes = train.station_codes()
    if not codes:
        _log("[!] 车站码表不可用")
        return {"ok": False, "queries": 0, "rows": 0, "error": "station_codes_unavailable"}

    wanted = set(dests or [d["name"] for d in DESTINATIONS])
    run_id = None if dry_run else store.start_watch_run(
        [d.isoformat() for d in dates], sorted(wanted))

    queries = rows_written = 0
    err = None
    try:
        for d in DESTINATIONS:
            if d["name"] not in wanted:
                continue
            code = codes.get(d["station"])
            if not code:
                _log(f"[!] {d['name']} 无电报码，跳过")
                continue
            # 去程：长春 -> 目的地
            rows, daily = train.collect_dates(d["name"], code, dates, "out")
            queries += len(daily)
            if not dry_run:
                rows_written += store.save_train_rows(rows)
                store.save_train_daily(daily)
            # 回程：目的地 -> 长春（"去得了回不来"同样不可行，必须一起看）
            rrows, rdaily = train.collect_dates(d["name"], ORIGIN_CODE, dates,
                                                "return", from_code=code)
            queries += len(rdaily)
            if not dry_run:
                rows_written += store.save_train_rows(rrows)
                store.save_train_daily(rdaily)
            _log(f"{d['name']}: 去程 {len(rows)} 行 / 回程 {len(rrows)} 行")
        if not dry_run:
            n_ev = derive_events([dd.isoformat() for dd in dates], sorted(wanted))
            _log(f"售罄事件重算 {n_ev} 条")
    except Exception as e:                      # 采集层异常不应让调度进程退出
        err = f"{type(e).__name__}: {e}"
        _log(f"[!] 采集异常 {err}")

    ok = err is None and rows_written > 0
    if not dry_run and run_id:
        store.finish_watch_run(run_id, ok, queries, rows_written, err)

    alerts = check_alerts() if not dry_run else []
    return {"ok": ok, "queries": queries, "rows": rows_written,
            "error": err, "alerts": alerts}


# ---------------- 售罄事件 ----------------
def _best(seats: dict) -> int:
    """该车次当前最好买到的席别余票（100 表示"有"，0 表示全无）。"""
    vals = [v for v in seats.values() if v >= 0]
    return max(vals) if vals else -1


def derive_events(travel_dates: list[str], dests: list[str]) -> int:
    """由快照历史推导每个车次的首次售罄时刻。

    定义：
      first_seen_at = 该车次首次被观测到的时刻（开售后第一次抓取，因而是开售时刻的**上界**）
      soldout_at    = 首次"全部席别为 0"的时刻
      hours_to_sold = 两者之差；若配置了 PRESALE_AT，则以官方起售时刻为起点（更准）

    注意：只有快照足够密，"售罄耗时"才有分辨率。间隔 2 小时采集 → 耗时精度 ±2 小时。
    """
    out_rows: list[dict] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with store.conn() as c:
        for td in travel_dates:
            for dest in dests:
                for direction in ("out", "return"):
                    snap = c.execute(
                        """SELECT fetched_at, train_code, depart_time, seats_json
                           FROM train_snapshot
                           WHERE travel_date=? AND dest=? AND direction=?
                           ORDER BY train_code, fetched_at""",
                        (td, dest, direction)).fetchall()
                    if not snap:
                        continue
                    per_train: dict[str, dict] = {}
                    for r in snap:
                        t = per_train.setdefault(r["train_code"], {
                            "first": None, "soldout": None, "last": None})
                        if t["first"] is None:
                            t["first"] = r["fetched_at"]
                        seats = json.loads(r["seats_json"] or "{}")
                        best = _best(seats)
                        if best == 0 and t["soldout"] is None:
                            t["soldout"] = r["fetched_at"]
                        t["last"] = r["seats_json"]
                    for code, t in per_train.items():
                        status = "soldout" if t["soldout"] else (
                            "open" if t["last"] else "unknown")
                        hours = None
                        if t["soldout"]:
                            # 起点优先用官方起售时刻（更准），没有则退化为首次观测时刻（耗时偏大）
                            start = t["first"]
                            if dest in PRESALE_AT:
                                cand = f"{td} {PRESALE_AT[dest]}:00"
                                start = cand if cand <= t["soldout"] else t["first"]
                            try:
                                hours = round((datetime.fromisoformat(t["soldout"])
                                               - datetime.fromisoformat(start)).total_seconds() / 3600, 2)
                            except ValueError:
                                hours = None
                        out_rows.append({
                            "dest": dest, "direction": direction, "travel_date": td,
                            "train_code": code, "first_seen_at": t["first"],
                            "soldout_at": t["soldout"], "hours_to_sold": hours,
                            "hours_to_sold_exact": None, "last_seats": t["last"],
                            "status": status, "updated_at": now,
                        })
    return store.save_soldout_events(out_rows)


# ---------------- 指标与预警 ----------------
def _parse(ts: str | None):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def metrics(travel_date: str, dest: str, direction: str = "out") -> dict:
    """某目的地某出行日的余票压力指标（直接从快照算，不依赖 soldout_event）。"""
    with store.conn() as c:
        rows = c.execute(
            """SELECT fetched_at, train_code, seats_json FROM train_snapshot
               WHERE travel_date=? AND dest=? AND direction=? ORDER BY fetched_at""",
            (travel_date, dest, direction)).fetchall()
    if not rows:
        return {"ok": False}
    by_ts: dict[str, dict[str, int]] = {}
    for r in rows:
        by_ts.setdefault(r["fetched_at"], {})[r["train_code"]] = _best(
            json.loads(r["seats_json"] or "{}"))
    stamps = sorted(by_ts)
    first, latest = stamps[0], stamps[-1]

    def rate(m: dict[str, int]) -> float:
        known = [v for v in m.values() if v >= 0]
        return (sum(1 for v in known if v == 0) / len(known)) if known else 0.0

    ev = store.soldout_events(travel_date, dest)
    ev = [e for e in ev if e["direction"] == direction and e["hours_to_sold"] is not None]
    # hours_to_sold == 0 表示"首次观测时已售罄"——耗时不可知，不能算作 0 小时，
    # 否则 A2（极速售罄）会把每次冷启动的观测都误判成抢空。单独计数。
    at_first_sight = sum(1 for e in ev if not e["hours_to_sold"] or e["hours_to_sold"] <= 0)
    hours = [e["hours_to_sold"] for e in ev if e["hours_to_sold"] and e["hours_to_sold"] > 0]

    # 开售后 24 小时内的最高售罄率：首次观测通常紧贴开售时刻（必然是 0），
    # 真正有信息量的是"开售后一天内卖掉了多少" —— A1 用它，而不是首轮观测值。
    t0 = _parse(first)
    r24, elapsed = 0.0, 0.0
    for ts, mm in by_ts.items():
        dt = _parse(ts)
        if dt is None or t0 is None:
            continue
        elapsed = max(elapsed, (dt - t0).total_seconds() / 3600)
        if (dt - t0).total_seconds() <= 86400:
            r24 = max(r24, rate(mm))

    return {
        "ok": True, "dest": dest, "travel_date": travel_date, "direction": direction,
        "trains": len(by_ts[latest]),
        "soldout_rate": round(rate(by_ts[latest]), 3),
        "soldout_rate_first": round(rate(by_ts[first]), 3),   # 开售后首轮观测（诊断用）
        "soldout_rate_24h": round(r24, 3),                    # 开售后 24h 内最高（预警用）
        "hours_since_first": round(elapsed, 1),
        "seats_total": sum(v for v in by_ts[latest].values() if v > 0),
        "observations": len(stamps),
        "first_at": first, "latest_at": latest,
        "soldout_n": len(ev),
        "at_first_sight": at_first_sight,          # 首见即售罄（耗时不可知）
        "measured_n": len(hours),                  # 真正测到耗时的趟数
        "median_hours": round(statistics.median(hours), 1) if hours else None,
        "min_hours": round(min(hours), 1) if hours else None,
    }


def check_alerts(travel_dates: list[str] | None = None) -> list[str]:
    """跑一遍预警规则，返回本轮新增的预警文本。"""
    if travel_dates is None:
        travel_dates = [d.isoformat() for t in TARGETS.values() for d in t["out"] + t["return"]]
    msgs: list[dict] = []

    for td in travel_dates:
        for d in DESTINATIONS:
            for direction in ("out", "return"):
                m = metrics(td, d["name"], direction)
                if not m.get("ok"):
                    continue
                tag = "去程" if direction == "out" else "回程"
                # A1 开售后 24h 售罄率（首轮观测几乎必为 0，不能用来判断）
                if m["soldout_rate_24h"] >= TH_FIRST_DAY_SOLDOUT:
                    msgs.append({
                        "rule": "A1", "level": "warn", "dest": d["name"], "travel_date": td,
                        "message": f"{d['name']} {td} {tag}：开售后 24h 内售罄率升至 "
                                   f"{m['soldout_rate_24h']:.0%}（{m['trains']} 趟，"
                                   f"已观测 {m['hours_since_first']:.1f}h），需求偏强",
                        "payload": json.dumps(m, ensure_ascii=False),
                    })
                # A2 极速售罄
                if m["min_hours"] is not None and 0 < m["min_hours"] <= TH_FAST_SOLD_HOURS:
                    msgs.append({
                        "rule": "A2", "level": "warn", "dest": d["name"], "travel_date": td,
                        "message": f"{d['name']} {td} {tag}：已有车次在 {m['min_hours']:.1f} 小时内售罄"
                                   f"（共 {m['soldout_n']} 趟售罄，中位 {m['median_hours']} 小时）",
                        "payload": json.dumps(m, ensure_ascii=False),
                    })
                # A3 售罄率跳增
                with store.conn() as c:
                    prev = c.execute(
                        """SELECT fetched_at, train_code, seats_json FROM train_snapshot
                           WHERE travel_date=? AND dest=? AND direction=?
                           ORDER BY fetched_at DESC LIMIT 50""",
                        (td, d["name"], direction)).fetchall()
                if len(prev) >= 2:
                    def r_of(rows):
                        m2 = {r["train_code"]: _best(json.loads(r["seats_json"] or "{}"))
                              for r in rows}
                        known = [v for v in m2.values() if v >= 0]
                        return sum(1 for v in known if v == 0) / len(known) if known else 0.0
                    now_r = m["soldout_rate"]
                    # 上一轮观测（跳过与最新同批的 fetched_at）
                    last_ts = prev[0]["fetched_at"]
                    older = [r for r in prev if r["fetched_at"] != last_ts]
                    if older:
                        prev_r = r_of([r for r in older if r["fetched_at"] == older[0]["fetched_at"]])
                        if round(now_r - prev_r, 6) >= TH_JUMP_PP:   # 0.7-0.5 在浮点下是 0.19999…
                            msgs.append({
                                "rule": "A3", "level": "info", "dest": d["name"], "travel_date": td,
                                "message": f"{d['name']} {td} {tag}：售罄率较上次观测 "
                                           f"{prev_r:.0%}→{now_r:.0%}（+{(now_r-prev_r)*100:.0f}pp）",
                            })

    # A4 采集管道异常
    runs = store.recent_watch_runs(TH_FAIL_STREAK)
    if len(runs) >= TH_FAIL_STREAK and all(r["ok"] == 0 for r in runs[:TH_FAIL_STREAK]):
        msgs.append({
            "rule": "A4", "level": "critical", "dest": None, "travel_date": None,
            "message": f"采集连续 {TH_FAIL_STREAK} 轮失败（最近：{runs[0]['started_at']}，"
                       f"{runs[0]['error'] or '无错误信息'}），余票观测已中断",
        })

    added = store.save_alerts(msgs)
    for m in added:
        _log(f"[{m['level'].upper()}] {m['message']}")
    return [m["message"] for m in added]


# ---------------- CLI ----------------
def status(dates: list[date] | None = None):
    store.init_db()
    ready, waiting = open_targets()
    if dates:                      # 手动指定要看的日期（验收/复盘用）
        ready, waiting = sorted(dates), []
    print(f"=== 预售窗口：今天 {date.today()}，可查到 {date.today() + timedelta(days=train.presale_days())} ===")
    if waiting:
        for d, days in waiting[:8]:
            print(f"  待开售  {d}  还有 {days} 天进预售")
    if not ready:
        print("  当前没有已开售的目标日期——此刻任何'元旦会有多挤'的数字都是编的。")
    else:
        print(f"  已开售  {[d.isoformat() for d in ready]}")
        print(f"\n{'日期':<12}{'目的地':<8}{'方向':<5}{'趟数':>5}{'售罄率':>8}{'售罄趟':>7}{'最短耗时h':>10}")
        for td in [d.isoformat() for d in ready]:
            for d in DESTINATIONS:
                for direction in ("out", "return"):
                    m = metrics(td, d["name"], direction)
                    if not m.get("ok"):
                        continue
                    print(f"{td:<12}{d['name']:<8}{'去' if direction=='out' else '回':<5}"
                          f"{m['trains']:>5}{m['soldout_rate']:>8.0%}{m['soldout_n']:>7}"
                          f"{(str(m['min_hours']) + 'h') if m['min_hours'] is not None else ('首见售罄×%d' % m['at_first_sight'] if m['at_first_sight'] else '-'):>10}")
    al = store.alerts(10)
    if al:
        print("\n=== 最近预警 ===")
        for a in al:
            print(f"  {a['created_at']} [{a['level']}] {a['rule']}  {a['message']}")
    runs = store.recent_watch_runs(5)
    if runs:
        print("\n=== 最近采集 ===")
        for r in runs:
            print(f"  #{r['id']} {r['started_at']} ok={r['ok']} 查询{r['queries']} 行{r['rows']}"
                  f" {r['error'] or ''}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="元旦余票前向观测与预警")
    ap.add_argument("--once", action="store_true", help="采一轮后退出（给计划任务用）")
    ap.add_argument("--loop", action="store_true", help="常驻循环")
    ap.add_argument("--interval", type=int, default=60, help="循环间隔（分钟）")
    ap.add_argument("--status", action="store_true", help="打印进度、指标与预警")
    ap.add_argument("--dates", help="覆盖目标日期，逗号分隔（YYYY-MM-DD）")
    ap.add_argument("--dests", help="只采这些目的地，逗号分隔")
    ap.add_argument("--dry-run", action="store_true", help="只查询不落库")
    a = ap.parse_args(argv)

    if a.status:
        status([date.fromisoformat(x) for x in a.dates.split(",")] if a.dates else None)
        return 0

    dates = [date.fromisoformat(x) for x in a.dates.split(",")] if a.dates else None
    dests = a.dests.split(",") if a.dests else None

    if a.loop:
        _log(f"常驻观测启动，间隔 {a.interval} 分钟；Ctrl+C 退出")
        while True:
            r = snapshot(dates, dests)
            _log(f"本轮：{'成功' if r['ok'] else '失败'} 查询 {r['queries']} 行 {r['rows']}")
            time.sleep(max(5, a.interval) * 60)
    if a.once or not a.loop:
        r = snapshot(dates, dests, dry_run=a.dry_run)
        _log(f"完成：{'成功' if r['ok'] else '失败'} 查询 {r['queries']} 行 {r['rows']}"
             f"{(' 错误: ' + r['error']) if r.get('error') else ''}")
        return 0 if r["ok"] or r.get("skipped") else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
