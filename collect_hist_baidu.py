"""补抓历史百度指数：2023-08-01 ~ 昨天，全部关键词，按 90 天分段落库。

用法：python collect_hist_baidu.py
"""
from __future__ import annotations

import time
from datetime import date, timedelta

from trip_oracle import store
from trip_oracle.collectors import heat
from qdata.baidu_index import get_search_index

START = date(2023, 8, 1)
SEG = timedelta(days=90)
COOL = 2.5


def main() -> int:
    cookie = heat._load_cookie()
    if not cookie:
        print("缺少 cookie")
        return 1
    end = date.today() - timedelta(days=1)
    fetched = time.strftime("%Y-%m-%d %H:%M:%S")
    n_saved = 0
    for k in heat.KEYWORDS:
        seg_start = START
        while seg_start <= end:
            seg_end = min(seg_start + SEG - timedelta(days=1), end)
            try:
                rows = list(get_search_index(
                    keywords_list=[[k["word"]]],
                    start_date=seg_start.isoformat(),
                    end_date=seg_end.isoformat(), cookies=cookie))
                recs = []
                for r in rows:
                    if r.get("type") == "all" and r.get("index"):
                        try:
                            v = int(r["index"])
                        except (ValueError, TypeError):
                            continue
                        if v <= 0:          # 假 0：缺失而非真 0
                            continue
                        recs.append({"dest": k["dest"], "keyword": k["word"],
                                     "source": "baidu_index", "date": r["date"],
                                     "heat": v, "fetched_at": fetched})
                if recs:
                    store.save_heat_rows(recs)
                    n_saved += len(recs)
                    print(f"{k['word']} {seg_start}~{seg_end}: {len(recs)} 天")
                else:
                    print(f"{k['word']} {seg_start}~{seg_end}: 0 天")
            except Exception as e:
                print(f"{k['word']} {seg_start}~{seg_end}: ERR {type(e).__name__} {str(e)[:80]}")
            seg_start = seg_end + timedelta(days=1)
            time.sleep(COOL)
    print(f"[done] 共落库 {n_saved} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
