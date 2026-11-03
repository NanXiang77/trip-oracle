"""把 trip-oracle 包装成智能体：LLM 大脑 + 只读工具层。

用法：
    # 1) 只看工具声明（不调模型，无需密钥）
    python agent.py --tools

    # 2) 直接跑单个工具（排查数据）
    python agent.py --call get_crowd_matrix '{"days":3}'

    # 3) 交互对话（需要 OpenAI 兼容端点）
    set AGENT_API_KEY=sk-xxx
    set AGENT_BASE_URL=https://api.deepseek.com/v1   （可选，默认 DeepSeek）
    set AGENT_MODEL=deepseek-chat                    （可选）
    python agent.py

没有配置密钥时自动降级为"规则版"（复用 app.chat_reply），功能少但不崩。

设计原则（答辩讲点）：
  · 数字只能来自工具返回值，模型不许编；
  · 工具全部只读：不能触发采集、不能写库、不能联网；
  · 工具秒回（纯 SQLite 查询），慢活（采集）留给定时任务；
  · 工具声明与 AgentArts/OpenAI 的 function calling schema 一一对应，
    迁到平台时只需把 tools.call 包成 HTTP 路由，提示词不用改。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.request

from trip_oracle import calibration, store, tools
from trip_oracle.config import DESTINATIONS

SYSTEM_PROMPT = """你是"周末去哪"，一位只依据真实数据说话的周边游顾问，服务从长春出发的大学生。

铁律：
1. 所有数字必须来自工具返回值。工具没给的数字一律不说，可以说"我查一下"。
2. 回答拥挤度时必须带不确定性：例如"预计是平日的 2.3 倍（4 年国庆数据标定，典型误差 ±20%）"。
3. 用户问未来 8 天内 → 调 get_crowd_matrix（12306 余票口径，实测快照）；
   问国庆/元旦等假期 → 调 predict_holiday（α 外推口径），并说明这是预测不是实测。
4. 给推荐必须给理由：引用拥挤指数、花费明细、天气三项中支撑结论的那一项。
5. 某地数据不足（如四平热度基数低）→ 明说"这个目的地信号弱，参考性有限"，不硬给结论。
6. 语气：简洁直接，像靠谱学长。结论先行，最多三条理由，不写小作文，不堆 emoji。
7. 如果用户问"数据准不准/怎么算的"，调 explain_method，用真实口径回答，不要临场编。
"""

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "get_crowd_matrix",
        "description": "查询长春出发 6 个目的地未来若干天的拥挤指数、售罄车次、剩余座位与天气。",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "查询天数，默认 8，最多 8"}}},
    }},
    {"type": "function", "function": {
        "name": "recommend_destinations",
        "description": "按拥挤度、花费、天气、热度给目的地排序并给推荐理由。用户说'推荐/去哪好/想出去走走'时调用。",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "行程天数 1~3"},
            "budget": {"type": "integer", "description": "总预算（元，含往返车票+住宿+餐饮）"},
            "pref": {"type": "string", "enum": ["", "自然", "城市", "网红"],
                     "description": "偏好，不限传空串"}}},
    }},
    {"type": "function", "function": {
        "name": "estimate_budget",
        "description": "估算某目的地的花费明细（车票/住宿/餐饮）。",
        "parameters": {"type": "object", "properties": {
            "dest": {"type": "string", "description": "目的地名，如 沈阳"},
            "days": {"type": "integer", "description": "天数 1~3"},
            "holiday": {"type": "boolean", "description": "是否按假日价（住宿翻倍）"}},
            "required": ["dest"]},
    }},
    {"type": "function", "function": {
        "name": "predict_holiday",
        "description": "预测某个假期窗口的客流倍数与拥挤等级（α 外推，非实测）。只用于未来假期。",
        "parameters": {"type": "object", "properties": {
            "dest": {"type": "string", "description": "目的地名"},
            "window_start": {"type": "string", "description": "假期开始日 YYYY-MM-DD"},
            "window_end": {"type": "string", "description": "假期结束日 YYYY-MM-DD"}},
            "required": ["dest", "window_start", "window_end"]},
    }},
    {"type": "function", "function": {
        "name": "explain_method",
        "description": "解释数据口径与方法学：拥挤指数怎么算、α 怎么标定、误差多大、数据多新。",
        "parameters": {"type": "object", "properties": {
            "topic": {"type": "string", "enum": ["crowd", "alpha", "freshness", "all"],
                      "description": "想解释的主题"}}},
    }},
]

HOLIDAY_DATES = {"国庆": ("2026-10-01", "2026-10-07"),
                 "元旦": ("2026-12-31", "2027-01-02"),
                 "春节": ("2027-02-06", "2027-02-12")}
KEYWORDS = {d["name"]: d["keywords"][0] if d.get("keywords") else d["name"]
            for d in DESTINATIONS}


# ---------------- 工具实现（全部只读、秒回） ----------------

def t_get_crowd_matrix(days: int = 8) -> dict:
    return tools.tool_get_crowd_matrix(days=int(days))


def t_recommend(days: int = 2, budget: int = 600, pref: str = "") -> dict:
    return tools.tool_recommend(days=int(days), budget=int(budget), pref=pref or "")


def t_budget(dest: str, days: int = 2, holiday: bool = False) -> dict:
    return tools.tool_budget(dest=dest, days=int(days), holiday=bool(holiday))


def t_predict_holiday(dest: str, window_start: str, window_end: str) -> dict:
    kw = KEYWORDS.get(dest, dest)
    r = calibration.predict_holiday(dest, kw, window_start, window_end)
    r.update({"dest": dest, "keyword": kw, "window": [window_start, window_end],
              "note": "α 外推，非实测；余票放票后会用售罄速度修正"})
    return r


def t_explain_method(topic: str = "all") -> dict:
    rows = store.latest_train_daily()
    latest = max((r.get("fetched_at") or "" for r in rows), default="")
    text = {
        "crowd": "拥挤指数 = 售罄车次占比 × 0.7 + 座位宽松度 × 0.3，再做假期/周末放大"
                 "（国庆 ×2.2、五一 ×1.9、元旦 ×1.6、周末 ×1.25/1.05），截断到 [0,1]。"
                 "分档：<0.15 舒适 / <0.30 较舒适 / <0.50 一般 / <0.70 拥挤 / ≥0.70 很挤。",
        "alpha": "α 是「假期客流 ÷ 平日基线」的放大系数，用 4 年国庆的百度/抖音热度与"
                 "真实客流标定：本地口径 2.56、远程口径 2.28（n=4），留一验证 MAE 约 20%。"
                 "抖音口径与百度口径不能混用。",
        "freshness": f"余票与天气按采集任务刷新，当前最新快照 {latest or '无'}。"
                     "百度指数 T+1 发布（次日 08:00 后），抖音再晚 1~2 天，"
                     "所以热度曲线右端可能空一天，这是平台节奏，不影响余票口径的判断。",
    }
    if topic == "all":
        return {"method": text, "freshness": latest}
    return {"topic": topic, "method": text.get(topic, "未知主题")}


REGISTRY = {
    "get_crowd_matrix": t_get_crowd_matrix,
    "recommend_destinations": t_recommend,
    "estimate_budget": t_budget,
    "predict_holiday": t_predict_holiday,
    "explain_method": t_explain_method,
}


def run_tool(name: str, args: dict) -> dict:
    fn = REGISTRY.get(name)
    if not fn:
        return {"error": f"未知工具：{name}"}
    try:
        return fn(**(args or {}))
    except TypeError as e:
        return {"error": f"参数不匹配：{e}"}


# ---------------- LLM 调度 ----------------

def _chat(messages: list[dict]) -> dict:
    """OpenAI 兼容 /chat/completions（DeepSeek / 智谱 / 通义 均可）。"""
    base = os.environ.get("AGENT_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("AGENT_MODEL", "deepseek-chat")
    key = os.environ.get("AGENT_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or ""
    body = json.dumps({"model": model, "messages": messages,
                       "tools": TOOL_SPECS, "temperature": 0.3}).encode()
    req = urllib.request.Request(base + "/chat/completions", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode())


def has_key() -> bool:
    return bool(os.environ.get("AGENT_API_KEY") or os.environ.get("DEEPSEEK_API_KEY"))


def answer(question: str, verbose: bool = True) -> str:
    if not has_key():
        from app import chat_reply        # 降级：规则版，不依赖模型
        return "[降级为规则版：未配置 AGENT_API_KEY]\n" + chat_reply(question)["reply"]

    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": question}]
    for _ in range(5):                    # 最多 5 轮工具调用，防死循环
        rsp = _chat(messages)
        msg = rsp["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            return msg.get("content") or "（模型没有返回内容）"
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": calls})
        for tc in calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            out = run_tool(name, args)
            if verbose:
                print(f"  [tool] {name}({json.dumps(args, ensure_ascii=False)}) "
                      f"→ {len(json.dumps(out, ensure_ascii=False))} 字符", file=sys.stderr)
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": json.dumps(out, ensure_ascii=False)})
    return "（工具调用层数过多，已中止；请把问题拆小一点）"


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "--tools":
        print(json.dumps(TOOL_SPECS, ensure_ascii=False, indent=2))
        return
    if argv and argv[0] == "--call":
        name = argv[1]
        args = json.loads(argv[2]) if len(argv) > 2 else {}
        print(json.dumps(run_tool(name, args), ensure_ascii=False, indent=2))
        return
    if argv and argv[0] == "--ask":
        print(answer(" ".join(argv[1:])))
        return

    print("周末去哪 · 智能体（输入 q 退出）"
          + ("" if has_key() else "　[未配置 AGENT_API_KEY，走规则版]"))
    while True:
        try:
            q = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if q in ("q", "quit", "exit"):
            break
        if not q:
            continue
        print(answer(q))


if __name__ == "__main__":
    main()
