# -*- coding: utf-8 -*-
"""抖音指数采集（巨量算数）：首次运行需扫码登录，登录态存于专用 profile。
用法：
  python -m trip_oracle.collectors.douyin_login_collect 长白山,沈阳中街 20261001 20261031
  python -m trip_oracle.collectors.douyin_login_collect  # 默认 7 关键词 + 近 90 天
"""
import datetime as dt
import json
import os
import sys
import time

from .douyin_probe import CDP, PAGE, PROFILE

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data")

KEYWORDS = ["长白山", "哈尔滨旅游", "沈阳中街", "沈阳故宫", "延吉旅游", "吉林市旅游", "四平旅游"]

FETCH_JS = """
(function(){
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', 'https://trendinsight.oceanengine.com/api/open/index/get_multi_keyword_hot_trend', true);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.onreadystatechange = () => {
      if (xhr.readyState === 4) resolve(xhr.status + '|' + xhr.responseText);
    };
    xhr.onerror = () => resolve('XHR_ERR|');
    xhr.send(JSON.stringify({
      keyword_list: __KWS__,
      start_date: __START__,
      end_date: __END__,
      app_name: 'aweme'
    }));
  });
})()
"""

def is_logged_in(cdp, ws):
    """CDP 层查 Cookie（含 HttpOnly 的 sessionid）。"""
    try:
        names = {c["name"] for c in cdp.cookies(ws) if "oceanengine.com" in c.get("domain", "")}
        return bool(names & {"sessionid", "sessionid_ss", "sid_tt", "sid_guard"})
    except Exception:
        return False


def wait_login(cdp, ws, minutes=10):
    print("请在弹出的 Edge 窗口中登录抖音（扫码或验证码）...", flush=True)
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        if is_logged_in(cdp, ws):
            time.sleep(5)  # 等登录后的跳转与 SDK 初始化
            return True
        time.sleep(5)
    return False


def main():
    kws = sys.argv[1].split(",") if len(sys.argv) > 1 else KEYWORDS
    if len(sys.argv) > 3:
        start_d, end_d = sys.argv[2], sys.argv[3]
    else:
        end_d = dt.date.today().strftime("%Y%m%d")
        start_d = (dt.date.today() - dt.timedelta(days=89)).strftime("%Y%m%d")

    cdp = CDP()
    ws = None
    try:
        cdp.start(headless=False)  # 登录必须可见窗口
        ws = cdp.open_page(PAGE, load_wait=25)
        if not is_logged_in(cdp, ws):
            if not wait_login(cdp, ws):
                print("TIMEOUT: 6 分钟内未完成登录", flush=True)
                return
            print("登录成功，开始采集...", flush=True)
            time.sleep(3)
        else:
            print("已有登录态，直接采集。", flush=True)

        # 抖音接口单次只支持一个关键词（多词会报参数错），逐词拉取
        results = {}
        for kw in kws:
            expr = (FETCH_JS
                    .replace("__KWS__", json.dumps([kw], ensure_ascii=False))
                    .replace("__START__", json.dumps(start_d))
                    .replace("__END__", json.dumps(end_d)))
            try:
                raw = cdp.eval(ws, expr)
                status, _, body = str(raw).partition("|")
                results[kw] = {"status": status, "body": json.loads(body) if body.startswith("{") else body[:300]}
                print(f"[{kw}] status={status}", flush=True)
            except Exception as e:
                print(f"[{kw}] FAIL: {str(e)[:200]}", flush=True)
                results[kw] = {"error": str(e)[:200]}
            time.sleep(2)

        os.makedirs(BASE, exist_ok=True)
        out = os.path.join(BASE, "douyin_index_raw.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=1)
        print("已保存:", out, flush=True)
    finally:
        if ws:
            try:
                ws.close()
            except OSError:
                pass
        cdp.stop()


if __name__ == "__main__":
    main()
