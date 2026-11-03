"""把工具层包成 HTTP 服务，供智能体平台（AgentArts / 扣子 / Dify / 任意 function calling）调用。

零依赖（只用标准库），因为平台只需要 HTTP + JSON。

启动：
    python tool_api.py                 # 监听 0.0.0.0:8910
    set TOOL_API_TOKEN=xxx && python tool_api.py   # 开启鉴权（请求头带 Bearer xxx）

端点：
    GET  /health                    健康检查 + 最新快照时间
    GET  /tools                     工具清单（名字 + 描述）
    GET  /openapi.json              OpenAPI 3.0 描述（可整份导入平台，或照抄）
    POST /tools/<name>  {json}      调用工具，返回 JSON
    GET  /tools/<name>?a=1&b=2      同上（部分平台的 GET 采集模式）

平台侧怎么填（以"自定义工具 / HTTP 工具"为例）：
    工具地址   https://<你的公网域名>/tools/get_crowd_matrix
    方法       POST
    请求体     {"days": 8}
    响应       直接就是结构化 JSON，不需要再嵌套
    鉴权       Header: Authorization: Bearer <TOOL_API_TOKEN>

数据更新说明：这个服务每次请求都现场查 SQLite，所以只要库里更新了，
智能体的回答就跟着变——不需要重新上传智能体。唯一改库的是采集任务（collect.py）。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import agent
from trip_oracle import store

HOST = os.environ.get("TOOL_API_HOST", "0.0.0.0")
# 云平台（Render/Railway/Fly 等）会注入 PORT，本地默认 8910
PORT = int(os.environ.get("TOOL_API_PORT") or os.environ.get("PORT") or "8910")
TOKEN = os.environ.get("TOOL_API_TOKEN", "")

NAME_DESC = {
    "get_crowd_matrix": "未来 8 天各目的地拥挤指数 / 售罄车次 / 天气",
    "recommend_destinations": "按拥挤度、花费、天气给目的地排序",
    "estimate_budget": "某目的地车票 / 住宿 / 餐饮花费明细",
    "predict_holiday": "假期客流倍数预测（α 外推，非实测）",
    "explain_method": "解释口径与方法学（拥挤指数、α、数据新鲜度）",
}

# 参数类型，用于 GET 模式把字符串转成 right type
PARAM_TYPES = {}
for spec in agent.TOOL_SPECS:
    fn = spec["function"]
    PARAM_TYPES[fn["name"]] = {k: v.get("type", "string")
                               for k, v in fn["parameters"].get("properties", {}).items()}


def openapi_doc() -> dict:
    paths = {}
    for spec in agent.TOOL_SPECS:
        fn = spec["function"]
        schemas = {}
        for k, v in fn["parameters"].get("properties", {}).items():
            sch = {"type": v.get("type", "string")}
            if v.get("description"):
                sch["description"] = v["description"]
            if v.get("enum"):
                sch["enum"] = v["enum"]
            schemas[k] = sch
        paths[f"/tools/{fn['name']}"] = {
            "post": {
                "operationId": fn["name"],
                "summary": NAME_DESC.get(fn["name"], fn["name"]),
                "description": fn["description"],
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {
                        "type": "object", "properties": schemas,
                        "required": fn["parameters"].get("required", [])}}},
                },
                "responses": {"200": {"description": "工具返回值（JSON）"}},
            }
        }
    return {
        "openapi": "3.0.3",
        "info": {"title": "trip-oracle tools", "version": "1.0.0",
                 "description": "长春出发周边游的拥挤度 / 花费 / 假期预测工具集。全部只读。"},
        "servers": [{"url": os.environ.get("TOOL_API_PUBLIC_URL")
                     or f"http://127.0.0.1:{PORT}"}],
        "paths": paths,
    }


def health() -> dict:
    rows = store.latest_train_daily()
    latest = max((r.get("fetched_at") or "" for r in rows), default="")
    return {"ok": True, "service": "trip-oracle-tools", "tools": list(agent.REGISTRY),
            "latest_snapshot": latest, "auth": bool(TOKEN)}


class Handler(BaseHTTPRequestHandler):
    server_version = "tripOracleTools/1.0"

    def log_message(self, fmt, *args):        # 安静一点，只打错误
        if str(args[1] if len(args) > 1 else "")[:1] in ("4", "5"):
            sys.stderr.write("[api] " + (fmt % args) + "\n")

    def _send(self, code: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Api-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self) -> bool:
        if not TOKEN:
            return True
        h = self.headers.get("Authorization", "")
        return h.removeprefix("Bearer ").strip() == TOKEN or \
            self.headers.get("X-Api-Token", "") == TOKEN

    def do_OPTIONS(self):                    # 浏览器预检
        self._send(204, {})

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        path = u.path.rstrip("/") or "/"

        if path == "/health":
            return self._send(200, health())
        if path == "/openapi.json":
            return self._send(200, openapi_doc())
        if path == "/tools":
            return self._send(200, [
                {"name": s["function"]["name"],
                 "description": s["function"]["description"],
                 "parameters": s["function"]["parameters"]}
                for s in agent.TOOL_SPECS])
        if path.startswith("/tools/"):
            if not self._auth_ok():
                return self._send(401, {"error": "unauthorized"})
            name = path.split("/")[2]
            types = PARAM_TYPES.get(name)
            if types is None:
                return self._send(404, {"error": f"未知工具：{name}"})
            args = {}
            for k, v in q.items():
                raw = v[0]
                t = types.get(k, "string")
                try:
                    if t == "integer":
                        args[k] = int(raw)
                    elif t == "number":
                        args[k] = float(raw)
                    elif t == "boolean":
                        args[k] = raw.lower() in ("1", "true", "yes", "y")
                    else:
                        args[k] = raw
                except ValueError:
                    return self._send(400, {"error": f"参数 {k} 类型应为 {t}"})
            return self._send(200, agent.run_tool(name, args))
        return self._send(404, {"error": "not found", "path": u.path})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if not u.path.startswith("/tools/"):
            return self._send(404, {"error": "not found", "path": u.path})
        if not self._auth_ok():
            return self._send(401, {"error": "unauthorized"})
        name = u.path.rstrip("/").split("/")[-1]
        if name not in agent.REGISTRY:
            return self._send(404, {"error": f"未知工具：{name}"})
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            args = json.loads(raw.decode("utf-8") or "{}")
        except ValueError as e:
            return self._send(400, {"error": f"请求体不是合法 JSON：{e}"})
        if not isinstance(args, dict):
            return self._send(400, {"error": "请求体应为 JSON 对象"})
        return self._send(200, agent.run_tool(name, args))


def main() -> None:
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"trip-oracle tools API → http://{HOST}:{PORT}")
    print(f"  GET /health   GET /tools   GET /openapi.json")
    print(f"  POST /tools/<name>   （鉴权：{'开' if TOKEN else '关'}）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
