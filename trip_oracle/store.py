"""SQLite 存储层：所有采集结果落一张库，方便后面做特征与回测。"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "oracle.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS train_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at   TEXT NOT NULL,           -- 采集时间（本地时间）
    travel_date  TEXT NOT NULL,           -- 出行日期
    dest         TEXT NOT NULL,           -- 目的地展示名
    direction    TEXT NOT NULL DEFAULT 'out',  -- out=长春->目的地 / return=目的地->长春
    train_code   TEXT NOT NULL,           -- 车次
    depart_time  TEXT,
    arrive_time  TEXT,
    duration     TEXT,
    -- 各席别余票：-1 未知 / 0 无 / 100 表示"有"（上限）
    seats_json   TEXT,
    UNIQUE (fetched_at, travel_date, dest, direction, train_code)
);

CREATE TABLE IF NOT EXISTS train_daily (
    travel_date TEXT NOT NULL,
    dest        TEXT NOT NULL,
    direction   TEXT NOT NULL DEFAULT 'out',
    fetched_at  TEXT NOT NULL,
    trains_total   INTEGER,
    trains_soldout INTEGER,
    trains_known   INTEGER,
    seats_total    INTEGER,
    min_duration   TEXT,
    PRIMARY KEY (travel_date, dest, direction, fetched_at)
);

CREATE TABLE IF NOT EXISTS weather_daily (
    dest  TEXT NOT NULL,
    date  TEXT NOT NULL,
    tmax  REAL, tmin REAL,
    precip_prob REAL,
    weathercode INTEGER,
    text TEXT,                          -- 官方天气现象文本（中国天气网）
    fetched_at TEXT,
    PRIMARY KEY (dest, date)
);

CREATE TABLE IF NOT EXISTS crowd_daily (
    travel_date TEXT NOT NULL,
    dest        TEXT NOT NULL,
    crowd_index REAL,      -- 0~1 拥挤指数（去/回取更紧的一侧）
    crowd_level INTEGER,   -- 1~5 等级
    computed_at TEXT,
    PRIMARY KEY (travel_date, dest)
);

CREATE TABLE IF NOT EXISTS venue_daily (
    dest        TEXT NOT NULL,
    venue       TEXT NOT NULL,          -- 场馆名
    date        TEXT NOT NULL,          -- 可预约日期
    capacity    INTEGER,                -- 总名额（能拿到时）
    remaining   INTEGER,                -- 剩余名额（核心信号）
    fetched_at  TEXT,
    PRIMARY KEY (dest, venue, date)
);

CREATE TABLE IF NOT EXISTS heat_daily (
    dest        TEXT NOT NULL,
    keyword     TEXT NOT NULL,          -- 搜索词（如"沈阳中街"）
    source      TEXT NOT NULL,          -- baidu_index / manual / ...
    date        TEXT NOT NULL,          -- 数据日期
    heat        INTEGER,                -- 指数值
    fetched_at  TEXT,
    PRIMARY KEY (dest, keyword, source, date)
);

-- ---------- 前向观测：售罄时点与预警（2026-11-03 新增） ----------
-- 国庆的历史余票拿不到，只能热度反演；但真实预测场景里余票是提前 14 天预售的，
-- 所以"何时卖完"是比"当下剩余多少"更强的需求信号，必须逐次快照记录下来。
CREATE TABLE IF NOT EXISTS soldout_event (
    dest          TEXT NOT NULL,
    direction     TEXT NOT NULL DEFAULT 'out',
    travel_date   TEXT NOT NULL,
    train_code    TEXT NOT NULL,
    first_seen_at TEXT,                 -- 首次观测到该车次（≈开售后首次抓取，下界）
    soldout_at    TEXT,                 -- 首次"全部席别为 0"的时刻
    hours_to_sold       REAL,           -- 观测时长内售罄耗时（小时，与真实开售时刻的差为负偏）
    hours_to_sold_exact REAL,           -- 用官方起售时刻求得的耗时（拿到起售时间后填）
    last_seats    TEXT,                 -- 最近一次席位快照 JSON
    status        TEXT,                 -- soldout / open / unknown
    updated_at    TEXT,
    PRIMARY KEY (dest, direction, travel_date, train_code)
);

CREATE TABLE IF NOT EXISTS watch_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    target_dates TEXT,                  -- 本次覆盖的出行日期（逗号分隔）
    dests       TEXT,
    queries     INTEGER DEFAULT 0,      -- 成功查询次数
    rows        INTEGER DEFAULT 0,      -- 写入快照行数
    ok          INTEGER DEFAULT 0,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS alert_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    level       TEXT NOT NULL,          -- info / warn / critical
    rule        TEXT NOT NULL,          -- A1 首日售罄 / A2 极速售罄 / A3 环比跳增 / A4 采集异常
    dest        TEXT,
    travel_date TEXT,
    message     TEXT NOT NULL,
    payload     TEXT,
    acked       INTEGER DEFAULT 0
);
"""


def _migrate(c):
    """老库缺列时增量补齐。"""
    wcols = {r[1] for r in c.execute("PRAGMA table_info(weather_daily)")}
    if wcols and "text" not in wcols:
        c.execute("ALTER TABLE weather_daily ADD COLUMN text TEXT")
    cols = {r[1] for r in c.execute("PRAGMA table_info(train_daily)")}
    if not cols or "direction" in cols:
        return
    print("[migrate] 火车表缺 direction 列，重建并迁移旧数据…")
    c.executescript("""
        ALTER TABLE train_snapshot RENAME TO _ts_old;
        ALTER TABLE train_daily    RENAME TO _td_old;
    """)
    # 重建（复用 SCHEMA）
    for stmt in SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt.startswith("CREATE TABLE IF NOT EXISTS train_"):
            c.execute(stmt)
    c.executescript("""
        INSERT INTO train_snapshot (fetched_at, travel_date, dest, direction, train_code,
                                    depart_time, arrive_time, duration, seats_json)
            SELECT fetched_at, travel_date, dest, 'out', train_code,
                   depart_time, arrive_time, duration, seats_json FROM _ts_old;
        INSERT INTO train_daily (travel_date, dest, direction, fetched_at, trains_total,
                                 trains_soldout, trains_known, seats_total, min_duration)
            SELECT travel_date, dest, 'out', fetched_at, trains_total,
                   trains_soldout, trains_known, seats_total, min_duration FROM _td_old;
        DROP TABLE _ts_old; DROP TABLE _td_old;
    """)
    print("[migrate] 完成")


@contextmanager
def conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with conn() as c:
        c.executescript(SCHEMA)
        _migrate(c)


def save_train_rows(rows: list[dict]):
    if not rows:
        return 0
    with conn() as c:
        c.executemany(
            """INSERT OR REPLACE INTO train_snapshot
               (fetched_at, travel_date, dest, direction, train_code, depart_time, arrive_time, duration, seats_json)
               VALUES (:fetched_at, :travel_date, :dest, :direction, :train_code, :depart_time, :arrive_time, :duration, :seats_json)""",
            rows,
        )
    return len(rows)


def save_train_daily(rows: list[dict]):
    if not rows:
        return 0
    with conn() as c:
        c.executemany(
            """INSERT OR REPLACE INTO train_daily
               (travel_date, dest, direction, fetched_at, trains_total, trains_soldout, trains_known, seats_total, min_duration)
               VALUES (:travel_date, :dest, :direction, :fetched_at, :trains_total, :trains_soldout, :trains_known, :seats_total, :min_duration)""",
            rows,
        )
    return len(rows)


def save_weather(rows: list[dict]):
    if not rows:
        return 0
    rows = [{**r, "text": r.get("text")} for r in rows]
    with conn() as c:
        c.executemany(
            """INSERT OR REPLACE INTO weather_daily
               (dest, date, tmax, tmin, precip_prob, weathercode, text, fetched_at)
               VALUES (:dest, :date, :tmax, :tmin, :precip_prob, :weathercode, :text, :fetched_at)""",
            rows,
        )
    return len(rows)


def save_crowd(rows: list[dict]):
    if not rows:
        return 0
    with conn() as c:
        c.executemany(
            """INSERT OR REPLACE INTO crowd_daily
               (travel_date, dest, crowd_index, crowd_level, computed_at)
               VALUES (:travel_date, :dest, :crowd_index, :crowd_level, :computed_at)""",
            rows,
        )
    return len(rows)


def train_daily_all():
    """全部日汇总行（含 fetched_at），供调用方自己挑快照。

    为什么不用 latest_train_daily：出发当天的车会陆续发走，越晚采越"空"——
    21 点再看当天只剩几趟车，算出来的指数天然偏低。当天这一列要取"当天最早
    一次采集"（早上车都还在）才接近出发前实况。
    """
    with conn() as c:
        cur = c.execute(
            "SELECT * FROM train_daily ORDER BY travel_date, dest, direction, fetched_at")
        return [dict(r) for r in cur.fetchall()]


def train_rows_at(travel_date: str, dest: str, direction: str, fetched_at: str) -> list[dict]:
    """取某次采集的逐车次明细（与 train_daily 的汇总行同一时刻）。"""
    with conn() as c:
        cur = c.execute(
            """SELECT * FROM train_snapshot
               WHERE travel_date=? AND dest=? AND direction=? AND fetched_at=?
               ORDER BY depart_time""",
            (travel_date, dest, direction, fetched_at))
        return [dict(r) for r in cur.fetchall()]


def latest_train_daily():
    with conn() as c:
        cur = c.execute("""
            SELECT d.* FROM train_daily d
            JOIN (SELECT travel_date, dest, MAX(fetched_at) AS ts
                  FROM train_daily GROUP BY travel_date, dest) m
              ON d.travel_date = m.travel_date AND d.dest = m.dest AND d.fetched_at = m.ts
            ORDER BY d.travel_date, d.dest
        """)
        return [dict(r) for r in cur.fetchall()]


def latest_weather():
    with conn() as c:
        cur = c.execute("SELECT * FROM weather_daily ORDER BY date, dest")
        return [dict(r) for r in cur.fetchall()]


def heat_daily_all(source: str = "baidu_index") -> list[dict]:
    with conn() as c:
        cur = c.execute(
            "SELECT dest, keyword, date, heat FROM heat_daily WHERE source=? ORDER BY date",
            (source,))
        return [dict(r) for r in cur.fetchall()]


def save_venue_rows(rows: list[dict]):
    if not rows:
        return 0
    with conn() as c:
        c.executemany(
            """INSERT OR REPLACE INTO venue_daily
               (dest, venue, date, capacity, remaining, fetched_at)
               VALUES (:dest, :venue, :date, :capacity, :remaining, :fetched_at)""",
            rows,
        )
    return len(rows)


def latest_venue_daily():
    with conn() as c:
        cur = c.execute("SELECT * FROM venue_daily ORDER BY date, dest, venue")
        return [dict(r) for r in cur.fetchall()]


def save_heat_rows(rows: list[dict]):
    if not rows:
        return 0
    with conn() as c:
        c.executemany(
            """INSERT OR REPLACE INTO heat_daily
               (dest, keyword, source, date, heat, fetched_at)
               VALUES (:dest, :keyword, :source, :date, :heat, :fetched_at)""",
            rows,
        )
    return len(rows)


def latest_heat(source: str | None = None):
    with conn() as c:
        sql = "SELECT * FROM heat_daily"
        args = ()
        if source:
            sql += " WHERE source = ?"
            args = (source,)
        cur = c.execute(sql + " ORDER BY date, dest, keyword", args)
        return [dict(r) for r in cur.fetchall()]


def save_soldout_events(rows: list[dict]):
    if not rows:
        return 0
    with conn() as c:
        c.executemany(
            """INSERT INTO soldout_event
               (dest, direction, travel_date, train_code, first_seen_at, soldout_at,
                hours_to_sold, hours_to_sold_exact, last_seats, status, updated_at)
               VALUES (:dest, :direction, :travel_date, :train_code, :first_seen_at, :soldout_at,
                       :hours_to_sold, :hours_to_sold_exact, :last_seats, :status, :updated_at)
               ON CONFLICT(dest, direction, travel_date, train_code) DO UPDATE SET
                 first_seen_at = COALESCE(soldout_event.first_seen_at, excluded.first_seen_at),
                 -- 售罄时刻一旦观测到就不再改动（首次售罄才是有意义的信号）
                 soldout_at    = COALESCE(soldout_event.soldout_at, excluded.soldout_at),
                 hours_to_sold = COALESCE(soldout_event.hours_to_sold, excluded.hours_to_sold),
                 hours_to_sold_exact = COALESCE(excluded.hours_to_sold_exact,
                                                soldout_event.hours_to_sold_exact),
                 last_seats    = excluded.last_seats,
                 status        = excluded.status,
                 updated_at    = excluded.updated_at""",
            rows,
        )
    return len(rows)


def soldout_events(travel_date: str | None = None, dest: str | None = None):
    with conn() as c:
        sql = "SELECT * FROM soldout_event WHERE 1=1"
        args: list = []
        if travel_date:
            sql += " AND travel_date = ?"
            args.append(travel_date)
        if dest:
            sql += " AND dest = ?"
            args.append(dest)
        return [dict(r) for r in c.execute(sql + " ORDER BY travel_date, dest, train_code", args)]


def start_watch_run(target_dates: list[str], dests: list[str]) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO watch_run (started_at, target_dates, dests) VALUES (?,?,?)",
            (_now(), ",".join(target_dates), ",".join(dests)),
        )
        return cur.lastrowid


def finish_watch_run(run_id: int, ok: bool, queries: int, rows: int, error: str | None = None):
    with conn() as c:
        c.execute(
            "UPDATE watch_run SET ended_at=?, ok=?, queries=?, rows=?, error=? WHERE id=?",
            (_now(), int(ok), queries, rows, error, run_id),
        )


def recent_watch_runs(n: int = 10):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM watch_run ORDER BY id DESC LIMIT ?", (n,))]


def save_alerts(rows: list[dict]) -> list[dict]:
    """写入预警；同 (rule, dest, travel_date) 只留一条，避免每小时重复轰炸。

    返回**实际新增**的记录（调用方据此打印，重复的不会反复刷屏）。
    """
    if not rows:
        return []
    added: list[dict] = []
    with conn() as c:
        for r in rows:
            dup = c.execute(
                "SELECT id FROM alert_log WHERE rule=? AND IFNULL(dest,'')=IFNULL(?,'')"
                " AND IFNULL(travel_date,'')=IFNULL(?,'')",
                (r["rule"], r.get("dest"), r.get("travel_date")),
            ).fetchone()
            if dup:
                continue
            c.execute(
                """INSERT INTO alert_log (created_at, level, rule, dest, travel_date, message, payload)
                   VALUES (?,?,?,?,?,?,?)""",
                (_now(), r["level"], r["rule"], r.get("dest"), r.get("travel_date"),
                 r["message"], r.get("payload")),
            )
            added.append(r)
    return added


def alerts(limit: int = 50):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM alert_log ORDER BY id DESC LIMIT ?", (limit,))]


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def stats():
    with conn() as c:
        out = {}
        for t in ("train_snapshot", "train_daily", "weather_daily", "crowd_daily",
                  "venue_daily", "heat_daily", "soldout_event", "alert_log", "watch_run"):
            out[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return out
