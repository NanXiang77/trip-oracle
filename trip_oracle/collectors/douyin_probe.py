# -*- coding: utf-8 -*-
"""抖音指数（巨量算数）采集：通过 CDP 驱动 Edge 打开站点页，
在页面上下文调用接口，签名由站点自带 JS 生成，无需逆向、无需 webdriver。"""
import base64
import json
import subprocess
import time
import urllib.request

import websocket

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PAGE = "https://trendinsight.oceanengine.com/trend-index/analysis"
API = "https://trendinsight.oceanengine.com/api/open/index/get_multi_keyword_hot_trend"
DEBUG_PORT = 9333
PROFILE = r"C:\Users\Nan\WorkBuddy\2026-10-29-13-31-07\trip-oracle\data\edge_cdp_profile"

FETCH_JS = """
(async () => {
  const r = await fetch(arguments ? null : null); return null;
})()
"""
FETCH_JS = """
(async () => {
  const body = JSON.stringify({
    keyword_list: __KWS__,
    start_date: __START__,
    end_date: __END__,
    app_name: 'aweme'
  });
  const r = await fetch('%s', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: body,
    credentials: 'include'
  });
  return await r.text();
})()
""" % API


class CDP:
    def __init__(self, port=DEBUG_PORT):
        self.port = port
        self.proc = None

    def start(self, headless=False):
        args = [
            EDGE,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={PROFILE}",
            "--no-first-run", "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1280,900",
            "about:blank",
        ]
        if headless:
            args.insert(1, "--headless=new")
        self.proc = subprocess.Popen(args)
        # 等调试端口就绪
        for _ in range(30):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=2)
                return
            except Exception:
                time.sleep(0.5)
        raise RuntimeError("CDP 端口未就绪")

    def _ctl(self):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json", timeout=5).read()

    def open_page(self, url, load_wait=8):
        info = json.loads(self._ctl())
        tab = next(t for t in info if t["type"] == "page" and t["url"].startswith("about:"))
        ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=60)
        ws.send(json.dumps({"id": 99, "method": "Page.navigate", "params": {"url": url}}))
        time.sleep(load_wait)
        return ws

    def cookies(self, ws):
        """通过 CDP 拿全部 Cookie（含 HttpOnly）。"""
        ws.send(json.dumps({"id": 77, "method": "Network.getAllCookies"}))
        deadline = time.time() + 10
        while time.time() < deadline:
            resp = json.loads(ws.recv())
            if resp.get("id") == 77:
                return resp.get("result", {}).get("cookies", [])
        return []

    def eval(self, ws, expr, await_promise=True):
        msg_id = 1
        ws.send(json.dumps({
            "id": msg_id, "method": "Runtime.evaluate",
            "params": {"expression": expr, "awaitPromise": await_promise,
                       "returnByValue": True},
        }))
        deadline = time.time() + 55
        while time.time() < deadline:
            resp = json.loads(ws.recv())
            if resp.get("id") == msg_id:
                r = resp.get("result", {})
                if "exceptionDetails" in r:
                    raise RuntimeError(json.dumps(r["exceptionDetails"], ensure_ascii=False)[:400])
                return r.get("result", {}).get("value")
        raise TimeoutError("CDP evaluate 超时")

    def stop(self):
        if self.proc:
            try:
                self.proc.kill()
            except OSError:
                pass
            self.proc = None


def fetch_douyin_index(keywords, start_date, end_date, headless=False):
    expr = (FETCH_JS
            .replace("__KWS__", json.dumps(keywords, ensure_ascii=False))
            .replace("__START__", json.dumps(start_date))
            .replace("__END__", json.dumps(end_date)))
    cdp = CDP()
    ws = None
    try:
        cdp.start(headless=headless)
        ws = cdp.open_page(PAGE)
        time.sleep(3)  # 等 acrawler 挂上钩子
        raw = cdp.eval(ws, expr)
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {"raw": str(raw)[:500]}
    finally:
        if ws:
            try:
                ws.close()
            except OSError:
                pass
        cdp.stop()


if __name__ == "__main__":
    import sys
    kws = sys.argv[1].split(",") if len(sys.argv) > 1 else ["长白山"]
    res = fetch_douyin_index(kws, "20261001", "20261031")
    print(json.dumps(res, ensure_ascii=False)[:1500])
