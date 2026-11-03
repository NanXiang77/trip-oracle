# -*- coding: utf-8 -*-
"""抖音指数（巨量算数）采集器。
链路：CDP 驱动 Edge → creator.douyin.com 页面上下文 XHR（免签名）
     → AES-CBC 固定 key/iv 解密 → heat_daily(source='douyin_index')。
首次运行需在弹出的窗口扫码登录；登录态存于 data/edge_cdp_profile。
"""
import base64
import datetime as dt
import json
import os
import time
import urllib.request as ur
import websocket

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from .. import store
from .heat import KEYWORDS
from .douyin_probe import CDP

API = "/api/v2/index/get_multi_keyword_hot_trend"
PAGE = "https://creator.douyin.com/creator-micro/creator-count/arithmetic-index"

_KEY = base64.b64decode("SjXbYTJb7zXoUToSicUL3A==")
_IV = base64.b64decode("OekMLjghRg8vlX/PemLc+Q==")

XHR_JS = """
(function(){
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '%s', true);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.onreadystatechange = () => {
      if (xhr.readyState === 4) resolve(xhr.responseText);
    };
    xhr.onerror = () => resolve('{\"status\":-1}');
    xhr.send(JSON.stringify({
      keyword_list: __KWS__,
      start_date: __START__,
      end_date: __END__,
      app_name: 'aweme'
    }));
  });
})()
""" % API


def _decrypt(data_b64: str) -> str:
    raw = base64.b64decode(data_b64)
    return unpad(AES.new(_KEY, AES.MODE_CBC, _IV).decrypt(raw), 16).decode("utf-8")


def _is_logged_in(cdp, ws) -> bool:
    try:
        names = {c["name"] for c in cdp.cookies(ws)
                 if "douyin.com" in c.get("domain", "") or "oceanengine.com" in c.get("domain", "")}
        return bool(names & {"sessionid", "sessionid_ss", "sid_tt", "sid_guard"})
    except Exception:
        return False


def _wait_login(cdp, ws, minutes: int = 10) -> bool:
    print("[douyin] 请在弹出的 Edge 窗口登录抖音（扫码/验证码）...", flush=True)
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        if _is_logged_in(cdp, ws):
            time.sleep(4)
            return True
        time.sleep(5)
    return False


def fetch_trend(cdp, ws, word: str, start_d: str, end_d: str) -> dict:
    """返回 {'ok': bool, 'msg': str, 'series': {date: index}}，date 为 YYYY-MM-DD。"""
    expr = (XHR_JS
            .replace("__KWS__", json.dumps([word], ensure_ascii=False))
            .replace("__START__", json.dumps(start_d))
            .replace("__END__", json.dumps(end_d)))
    ws.send(json.dumps({"id": 5, "method": "Runtime.evaluate",
                        "params": {"expression": expr, "awaitPromise": True,
                                   "returnByValue": True}}))
    deadline = time.time() + 60
    raw = None
    while time.time() < deadline:
        resp = json.loads(ws.recv())
        if resp.get("id") == 5:
            raw = resp.get("result", {}).get("result", {}).get("value")
            break
    if raw is None:
        return {"ok": False, "msg": "evaluate 超时", "series": {}}
    obj = json.loads(raw)
    if obj.get("status") != 0:
        return {"ok": False, "msg": f"接口 status={obj.get('status')} {obj.get('msg','')}",
                "series": {}}
    d = json.loads(_decrypt(obj["data"]))
    series = {}
    for item in d.get("hot_list", []):
        for pt in item.get("hot_list", []):
            try:
                v = int(pt["index"])
            except (KeyError, ValueError, TypeError):
                continue
            if v <= 0:
                continue  # 与百度层口径一致：0/异常视为缺失
            series[f"{pt['datetime'][:4]}-{pt['datetime'][4:6]}-{pt['datetime'][6:8]}"] = v
    return {"ok": True, "msg": f"共 {len(series)} 天", "series": series}


def collect(days: int = 90, headless: bool = False, wait_login: bool = True) -> dict:
    """采集全部关键词并落库，返回 {word: {...}} 结果。
    wait_login=False 时未登录直接跳过（供每日定时任务静默调用）。"""
    end = dt.date.today()
    start = end - dt.timedelta(days=days - 1)
    s_d, e_d = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")

    cdp = CDP()
    ws = None
    results = {}
    try:
        cdp.start(headless=headless)
        ws = cdp.open_page(PAGE, load_wait=15)
        if not _is_logged_in(cdp, ws):
            if not wait_login or not _wait_login(cdp, ws):
                return {k["word"]: {"ok": False, "msg": "未登录，跳过", "series": {}}
                        for k in KEYWORDS}

        fetched_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        for kw in KEYWORDS:
            r = fetch_trend(cdp, ws, kw["word"], s_d, e_d)
            results[kw["word"]] = r
            print(f"[douyin] {kw['dest']:<4} {kw['word']}: {r['msg']}", flush=True)
            if r["ok"]:
                rows.extend({"dest": kw["dest"], "keyword": kw["word"],
                             "source": "douyin_index", "date": d_, "heat": v,
                             "fetched_at": fetched_at}
                            for d_, v in sorted(r["series"].items()))
            time.sleep(2)
        if rows:
            store.init_db()
            store.save_heat_rows(rows)
            print(f"[douyin] 落库 {len(rows)} 行", flush=True)
    finally:
        if ws:
            try:
                ws.close()
            except OSError:
                pass
        cdp.stop()
    return results


def collect_range(start: str, end: str, headless: bool = True) -> dict:
    """按任意日期区间采集历史热度（如往年国庆用于训练标定）。
    start/end 格式 'YYYY-MM-DD'。"""
    s_d = start.replace("-", "")
    e_d = end.replace("-", "")

    cdp = CDP()
    ws = None
    results = {}
    try:
        cdp.start(headless=headless)
        ws = cdp.open_page(PAGE, load_wait=15)
        if not _is_logged_in(cdp, ws):
            return {k["word"]: {"ok": False, "msg": "未登录，跳过", "series": {}}
                    for k in KEYWORDS}

        fetched_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        for kw in KEYWORDS:
            r = fetch_trend(cdp, ws, kw["word"], s_d, e_d)
            results[kw["word"]] = r
            print(f"[douyin-hist] {kw['dest']:<4} {kw['word']}: {r['msg']}", flush=True)
            if r["ok"]:
                rows.extend({"dest": kw["dest"], "keyword": kw["word"],
                             "source": "douyin_index", "date": d_, "heat": v,
                             "fetched_at": fetched_at}
                            for d_, v in sorted(r["series"].items()))
            time.sleep(2)
        if rows:
            store.init_db()
            store.save_heat_rows(rows)
            print(f"[douyin-hist] 落库 {len(rows)} 行", flush=True)
    finally:
        if ws:
            try:
                ws.close()
            except OSError:
                pass
        cdp.stop()
    return results


if __name__ == "__main__":
    res = collect()
    for w, r in res.items():
        print(w, r["ok"], r["msg"])
