"""把本地看板导出成「单文件静态快照」，便于通过微信/邮件分享给组员。

思路：
  - 后端是纯 stdlib 的 http.server，前端只发 3 个 GET（matrix / heat / recommend）；
  - 这里把前端可能发出的所有请求提前算好，按 URL 建索引，去重后内嵌进 HTML；
  - 再把 fetch 覆盖成"查表"，并把 Chart.js 内联，得到一个可以双击打开、
    不依赖 Python/联网的 .html。

用法：
    python build_snapshot.py                # 默认今天
    python build_snapshot.py 2026-10-01     # 指定快照视角日期
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
from datetime import date, timedelta

import app as dash
from trip_oracle.config import DESTINATIONS

ROOT = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(ROOT, "vendor", "chart.umd.js")
OUT_DIR = os.path.join(ROOT, "dist")

# 数据覆盖区间：heat 最早 2025-08-25（抖音），百度 2026-08-04 起；渲染取 8-01 起够用
DATE_FROM = date(2026, 8, 1)
DATE_TO = date(2026, 11, 20)
SOURCES = ["baidu_index", "douyin_index"]
PREFS = ["", "自然", "城市", "网红"]
DAYS = [1, 2, 3]
BUDGETS = list(range(100, 3001, 50))


def _daterange(a: date, b: date) -> list[str]:
    out, cur = [], a
    while cur <= b:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _prewarm_archive() -> None:
    """回溯面板按天取历史天气，逐日联网会慢到不可用（单次 ~30s）。

    这里整段拉一次，再把 fetch_archive 换成内存切片，回溯降到毫秒级。
    """
    from trip_oracle.collectors import weather as W
    from trip_oracle.config import DESTINATIONS

    by_coord: dict[tuple, dict[str, dict]] = {}
    # Open-Meteo 档案有约 5 天延迟，超出区间会整体返回空
    arc_end = min(DATE_TO, date.today() - timedelta(days=5)).isoformat()
    for d in DESTINATIONS:
        rows = W.fetch_archive(d["lat"], d["lon"], DATE_FROM.isoformat(), arc_end)
        by_coord[(round(d["lat"], 2), round(d["lon"], 2))] = {r["date"]: r for r in rows}
        print(f"[archive] {d['name']}: {len(rows)} 天")

    def patched(lat: float, lon: float, start: str, end: str):
        m = by_coord.get((round(lat, 2), round(lon, 2)))
        if not m:
            return []
        return [v for k, v in sorted(m.items()) if start <= k <= end]

    W.fetch_archive = patched


def build_payload() -> tuple[list[str], dict[str, int]]:
    """返回 (去重后的 JSON 片段列表, {url: 片段下标})。"""
    _prewarm_archive()
    pool: list[str] = []
    seen: dict[str, int] = {}

    def put(obj) -> int:
        raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        h = hashlib.md5(raw.encode("utf-8")).hexdigest()
        if h not in seen:
            seen[h] = len(pool)
            pool.append(raw)
        return seen[h]

    snap: dict[str, int] = {}
    today = date.today()
    dates = _daterange(DATE_FROM, DATE_TO)

    live_matrix = put(dash.build_matrix())
    replay: dict[str, int] = {}
    snap["/api/matrix"] = live_matrix
    for d in dates:
        if date.fromisoformat(d) < today:
            if d not in replay:
                replay[d] = put(dash.replay_matrix(d))
            snap[f"/api/matrix?date={d}"] = replay[d]
        else:
            snap[f"/api/matrix?date={d}"] = live_matrix

    snap["/api/heat"] = put(dash.heat_view(None))
    for d in dates:
        snap[f"/api/heat?date={d}"] = put(dash.heat_view(d))

    # 车票剩余：目的地 × 日期 × 方向（前端首屏无参调用也要能命中）
    tk_dates = dash.build_matrix()["dates"]
    tk_dests = [d["name"] for d in DESTINATIONS]
    snap["/api/tickets?dir=out"] = put(dash.ticket_view(None, None, "out"))
    snap["/api/tickets?dir=return"] = put(dash.ticket_view(None, None, "return"))
    for dd in tk_dates:
        for dirn in ("out", "return"):
            snap[f"/api/tickets?date={urllib.parse.quote(dd)}&dir={dirn}"] = \
                put(dash.ticket_view(None, dd, dirn))
            for de in tk_dests:
                snap[f"/api/tickets?dest={urllib.parse.quote(de)}"
                     f"&date={urllib.parse.quote(dd)}&dir={dirn}"] = \
                    put(dash.ticket_view(de, dd, dirn))

    for days in DAYS:
        for pref in PREFS:
            q = urllib.parse.quote(pref)
            for budget in BUDGETS:
                url = f"/api/recommend?days={days}&pref={q}&budget={budget}"
                snap[url] = put(dash.recommend(days, budget, pref))

    return pool, snap


FETCH_SHIM = """
window.addEventListener('unhandledrejection', e=>{
  const r=e.reason; document.title='REJ: '+(r&&r.message||r)+' @ '+(r&&r.stack||'').slice(0,200);
});
window.__D__ = %(pool)s;
window.__SNAP__ = %(snap)s;
(function(){
  const cache = {};
  function nearestRecommend(path){
    // 预算未命中时，取最近的已缓存预算（推荐结果是分段函数，差别很小）
    const q = path.split('?')[1] || '';
    const p = new URLSearchParams(q);
    const days = p.get('days'), pref = p.get('pref'), budget = Number(p.get('budget'));
    let best = null, gap = Infinity;
    for (const k in window.__SNAP__) {
      if (!k.startsWith('/api/recommend?')) continue;
      const kp = new URLSearchParams(k.split('?')[1]);
      if (kp.get('days') !== days || (kp.get('pref') || '') !== (pref || '')) continue;
      const g = Math.abs(Number(kp.get('budget')) - budget);
      if (g < gap) { gap = g; best = k; }
    }
    return best;
  }
  const _fetch = window.fetch;
  window.fetch = function(input, init){
    const url = (typeof input === 'string') ? input : (input && input.url) || '';
    let path = url.replace(/^https?:\\/\\/[^/]+/, '');
    if (!(path in window.__SNAP__)) {
      if (path.startsWith('/api/recommend?')) path = nearestRecommend(path) || path;
    }
    if (path in window.__SNAP__) {
      const body = window.__D__[window.__SNAP__[path]];
      const val = (typeof body === 'string') ? JSON.parse(body) : body;
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve(val),
        text: () => Promise.resolve(typeof body === 'string' ? body : JSON.stringify(body))
      });
    }
    return _fetch ? _fetch(input, init)
                  : Promise.resolve({ok:false,status:404,json:()=>Promise.resolve({})});
  };
})();
"""

BADGE = """
<div style="position:fixed;right:14px;bottom:14px;z-index:99;background:rgba(18,22,28,.88);
  color:#9aa4b2;border:1px solid rgba(255,255,255,.14);padding:6px 12px;border-radius:999px;
  font:12px/1.5 -apple-system,'Segoe UI',sans-serif;backdrop-filter:blur(6px)">
  离线快照 · 生成于 %(stamp)s · 数据截至 2026-11-02
</div>
"""


def main(view: date) -> str:
    pool, snap = build_payload()

    page = dash.PAGE
    chart_js = open(VENDOR, encoding="utf-8").read()
    page = page.replace(
        '<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>',
        "<script>" + chart_js + "</script>",
    )

    shim = FETCH_SHIM % {"pool": "[" + ",".join(pool) + "]", "snap": json.dumps(snap)}
    page = page.replace("<script>\nconst LV=", "<script>" + shim + "</script>\n<script>\nconst LV=", 1)

    stamp = date.today().isoformat()
    if "<body>" in page:
        page = page.replace("<body>", "<body>" + BADGE % {"stamp": stamp}, 1)

    os.makedirs(OUT_DIR, exist_ok=True)
    name = f"trip-oracle-看板快照-{view.isoformat()}.html"
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)

    size = os.path.getsize(path) / 1024 / 1024
    print(f"[ok] {path}")
    print(f"     片段 {len(pool)} 条 / URL {len(snap)} 个 / {size:.2f} MB")

    # 顺手打 zip（含使用说明），省得每次手动打包
    import zipfile
    zp = path[:-5] + ".zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(path, os.path.basename(path))
        readme = os.path.join(OUT_DIR, "使用说明-给组员.txt")
        if os.path.exists(readme):
            z.write(readme, os.path.basename(readme))
    print(f"[ok] {zp}")
    return path


if __name__ == "__main__":
    import sys

    v = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    main(v)
