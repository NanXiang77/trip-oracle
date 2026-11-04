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
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import agent
from trip_oracle import store

HOST = os.environ.get("TOOL_API_HOST", "0.0.0.0")
# 云平台（Render/Railway/Fly 等）会注入 PORT，本地默认 8910
PORT = int(os.environ.get("TOOL_API_PORT") or os.environ.get("PORT") or "8910")
TOKEN = os.environ.get("TOOL_API_TOKEN", "")
# 本机推送通道：热度在本地采（要登录态），采完 POST 到这里入库。
# 云端只收不采，因此不需要、也不应该持有任何百度/抖音凭证。
INGEST_ENABLED = os.environ.get("INGEST_ENABLED", "1") not in ("0", "false", "False")

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
            "latest_snapshot": latest, "auth": bool(TOKEN), "mcp": "/mcp"}


# ---- MCP（Streamable HTTP）：华为云 AgentArts 等平台通过 MCP 协议接入 ----
# 协议：JSON-RPC 2.0，POST /mcp。支持 initialize / notifications/* / ping /
# tools/list / tools/call。无会话状态（每次请求独立），支持 2025-03-26 与
# 2025-06-18 两个协议版本。客户端 Accept 带 text/event-stream 时按 SSE 帧
# 回复（event: message\ndata: {...}\n\n），否则直接回 application/json——
# 两种都是 Streamable HTTP 允许的响应形态。
MCP_PROTOCOLS = ("2025-03-26", "2025-06-18")


def mcp_tools_list() -> dict:
    tools = []
    for spec in agent.TOOL_SPECS:
        fn = spec["function"]
        tools.append({"name": fn["name"], "description": fn["description"],
                      "inputSchema": fn["parameters"]})
    return {"tools": tools}


def mcp_tools_call(params: dict) -> dict:
    name = params.get("name")
    args = params.get("arguments") or {}
    if name not in agent.REGISTRY:
        return {"content": [{"type": "text", "text": f"未知工具：{name}"}], "isError": True}
    try:
        out = agent.run_tool(name, args if isinstance(args, dict) else {})
        return {"content": [{"type": "text",
                             "text": json.dumps(out, ensure_ascii=False)}],
                "isError": False}
    except Exception as e:                   # 工具内部错误也走标准 content 返回
        return {"content": [{"type": "text", "text": f"工具执行失败：{e}"}],
                "isError": True}


def _rpc(mid, result) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _rpc_err(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def ingest_heat(payload) -> tuple[int, dict]:
    """接收本机推来的热度行（只写 heat_daily，幂等 upsert）。

    必须配置 TOOL_API_TOKEN —— 写接口不能用"无鉴权"模式，否则谁都能往库里灌数。
    """
    if not INGEST_ENABLED:
        return 503, {"ok": False, "error": "接收通道已关闭（INGEST_ENABLED=0）"}
    if not TOKEN:
        return 503, {"ok": False,
                     "error": "未设置 TOOL_API_TOKEN，拒绝写入（写接口必须鉴权）"}
    if not isinstance(payload, dict):
        return 400, {"ok": False, "error": "请求体应为 JSON 对象"}
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        return 400, {"ok": False, "error": "缺少 rows 数组"}

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    clean, bad = [], 0
    for r in rows:
        if not isinstance(r, dict):
            bad += 1
            continue
        dest, kw, d = r.get("dest"), r.get("keyword"), r.get("date")
        if not (dest and kw and d):
            bad += 1
            continue
        try:
            heat = int(r.get("heat"))
        except (TypeError, ValueError):
            bad += 1
            continue
        clean.append({"dest": str(dest), "keyword": str(kw),
                      "source": str(r.get("source") or "baidu_index"),
                      "date": str(d)[:10], "heat": heat,
                      "fetched_at": r.get("fetched_at") or now})
    if not clean:
        return 400, {"ok": False, "error": "没有一行合法数据（需 dest/keyword/date/heat）",
                     "rejected": bad}
    saved = store.save_heat_rows(clean)
    return 200, {"ok": True, "saved": saved, "rejected": bad,
                 "pushed_at": payload.get("pushed_at") or "",
                 "status": store.heat_status()}


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
        if path == "/mcp":
            # MCP Streamable HTTP 的可选 SSE 通道。部分平台（如华为云 AgentArts）
            # 靠 GET 探测 transport type，收到 405 会报 "Failed to determine MCP
            # transport type"，所以这里返回一个合法的事件流而不是拒绝。
            return self._mcp_sse_probe()
        if path == "/openapi.json":
            return self._send(200, openapi_doc())
        if path == "/tools":
            return self._send(200, [
                {"name": s["function"]["name"],
                 "description": s["function"]["description"],
                 "parameters": s["function"]["parameters"]}
                for s in agent.TOOL_SPECS])
        if path == "/ingest/status":         # 热度覆盖情况（本机推送是否成功，一眼可见）
            if not self._auth_ok():
                return self._send(401, {"error": "unauthorized"})
            return self._send(200, {"enabled": INGEST_ENABLED,
                                    "require_token": bool(TOKEN),
                                    "sources": store.heat_status()})
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
        # 看板页面与它的接口：/、/dashboard、/api/* 交给内部 app.py
        if path in ("/", "/dashboard") or path.startswith("/api/"):
            return self._proxy_dashboard()
        return self._send(404, {"error": "not found", "path": u.path})

    def _accept202(self) -> None:
        self.send_response(202)
        self.send_header("Content-Length", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _proxy_dashboard(self) -> None:
        """把看板请求转发给内部 app.py。

        云端（SnapDeploy 等）只暴露一个端口，所以看板不能自己对外，
        由这里按路径转发：/、/dashboard、/api/*  →  127.0.0.1:8900
        """
        base = os.environ.get("DASHBOARD_INTERNAL", "http://127.0.0.1:8900")
        path = self.path
        if path == "/dashboard" or path.startswith("/dashboard?"):
            path = "/"                        # 别名：/dashboard 等价于看板首页
        url = base.rstrip("/") + path
        data = None
        headers = {"User-Agent": "trip-oracle-internal"}
        if self.command == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(n) if n else b""
            headers["Content-Type"] = self.headers.get("Content-Type") or "application/json"
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=self.command)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=30) as r:
                data = r.read()
                self.send_response(r.status)
                ct = r.headers.get("Content-Type")
                if ct:
                    self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            self._send(e.code, {"error": "dashboard upstream error", "status": e.code})
        except Exception as e:
            self._send(502, {"error": f"dashboard unavailable: {type(e).__name__}"})

    def _mcp_sse_probe(self) -> None:
        """响应 GET /mcp：开一条短命的 SSE 流，用于平台识别 transport type。"""
        print(f"[mcp] GET probe accept={self.headers.get('Accept','')!r} "
              f"ua={(self.headers.get('User-Agent') or '')[:70]!r}", flush=True)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b": mcp stream ready\n\n")
            self.wfile.flush()
            time.sleep(1.0)
            self.wfile.write(b"event: ping\ndata: {\"jsonrpc\":\"2.0\"}\n\n")
            self.wfile.flush()
        except Exception:
            pass

    def _mcp_respond(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if "text/event-stream" in (self.headers.get("Accept") or ""):
            # 注意：Content-Length 必须是完整帧的字节数（含 "event: message\ndata: " 与结尾 "\n\n"）。
            # 之前写死 +22 而实际是 +23，差 1 字节会让严格的客户端（如 httpx/MCP SDK）
            # 永远读不满一个 SSE 帧，表现为 "Failed to determine MCP transport type"。
            frame = b"event: message\ndata: " + body + b"\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(frame)))
            self.end_headers()
            self.wfile.write(frame)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

    def _handle_mcp(self) -> None:
        if not self._auth_ok():
            print(f"[mcp] POST 401 accept={self.headers.get('Accept','')!r} "
                  f"ua={(self.headers.get('User-Agent') or '')[:70]!r}", flush=True)
            return self._send(401, {"error": "unauthorized"})
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        print(f"[mcp] POST accept={self.headers.get('Accept','')!r} "
              f"ct={self.headers.get('Content-Type','')!r} "
              f"ua={(self.headers.get('User-Agent') or '')[:70]!r} "
              f"body={raw[:160]!r}", flush=True)
        try:
            msg = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return self._mcp_respond(_rpc_err(None, -32700, "Parse error"))
        if isinstance(msg, list):            # 平台网关不支持批量，取首条即可
            msg = msg[0] if msg else {}
        if not isinstance(msg, dict):
            return self._mcp_respond(_rpc_err(None, -32600, "Invalid Request"))
        method = msg.get("method") or ""
        mid = msg.get("id")
        if method == "initialize":
            ver = (msg.get("params") or {}).get("protocolVersion")
            return self._mcp_respond(_rpc(mid, {
                "protocolVersion": ver if ver in MCP_PROTOCOLS else MCP_PROTOCOLS[-1],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "trip-oracle", "version": "1.0.0"},
                "instructions": "长春出发周边游数据工具集：12306 余票拥挤指数、"
                                "花费估算、假期客流预测。全部只读，每次现场查库。",
            }))
        if method.startswith("notifications/"):
            return self._accept202()
        if method == "ping":
            return self._mcp_respond(_rpc(mid, {}))
        if method == "tools/list":
            return self._mcp_respond(_rpc(mid, mcp_tools_list()))
        if method == "tools/call":
            return self._mcp_respond(_rpc(mid, mcp_tools_call(msg.get("params") or {})))
        return self._mcp_respond(_rpc_err(mid, -32601, f"Method not found: {method}"))

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path.startswith("/api/"):      # 看板的 POST 接口（如 /api/refresh）
            return self._proxy_dashboard()
        if u.path.rstrip("/") == "/mcp":
            return self._handle_mcp()
        if u.path.rstrip("/") == "/ingest/heat":
            if not self._auth_ok():
                return self._send(401, {"error": "unauthorized"})
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except ValueError as e:
                return self._send(400, {"ok": False, "error": f"请求体不是合法 JSON：{e}"})
            code, out = ingest_heat(payload)
            return self._send(code, out)
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
    print(f"  GET /health   GET /tools   GET /openapi.json   POST /mcp (MCP Streamable HTTP)")
    print(f"  POST /tools/<name>   （鉴权：{'开' if TOKEN else '关'}）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
