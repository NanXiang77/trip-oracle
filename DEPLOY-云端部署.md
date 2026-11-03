# A 方案 · 云端部署（免穿透，电脑关机也能用）

> 目标：把 `tool_api`（工具服务）和 `collect`（采集）搬到云容器里，
> 平台填一个 `https://` 地址就能调，数据每天自己在云上更新。

---

## 一、这套东西在云上是怎么转的

```
                    ┌──────── 云容器（Render 免费层）────────┐
  平台智能体 ──HTTPS──▶ tool_api :$PORT                        │
                    │   ▲ 每次请求现场查 SQLite                │
                    │   │                                     │
                    │ data/oracle.db ◀── 采集线程（08:00 / 20:00）│
                    └───────────────────────────────────────────┘
```

一个进程干两件事，都在 `cloud_run.py` 里：

- 前台跑 HTTP 服务（平台调它）；
- 后台线程按东八区每天 8 点、20 点跑 `collect.py`；
- **启动时如果发现库里最新快照超过 6 小时，先补采一次**（免费实例会休眠，醒来时数据可能旧了几小时）。

---

## 二、部署步骤（Render，约 10 分钟）

### 1. 把代码推到 GitHub

仓库根目录就是 `trip-oracle/`（`.gitignore` 已配好，550MB 的 edge 浏览器缓存不会入库）。

```bash
cd trip-oracle
git init
git add .
git commit -m "trip-oracle: tools api + cloud scheduler"
git branch -M main
git remote add origin https://github.com/<你的账号>/trip-oracle.git
git push -u origin main
```

> `data/oracle.db`（5.3 MB）要一起推——它是首屏数据，没有它第一次打开是空的。
> 推之前确认 `git status` 里没有 `data/edge_cdp_profile/`。

### 2. Render 建服务

注册 https://render.com（GitHub 登录）→ **New +** → **Blueprint** → 选这个仓库。

仓库里有 `render.yaml`，它会自动建一个 Web Service：

| 配置项 | 值 |
|---|---|
| Runtime | Docker |
| Plan | Free |
| Health Check Path | `/health` |
| Region | Singapore（离国内最近） |

如果不用 Blueprint，就手动 **New → Web Service → 选仓库 → Runtime: Docker**，其余默认。

### 3. 填环境变量

| Key | Value | 说明 |
|---|---|---|
| `SKIP_HEAT` | `1` | 云端没有浏览器 cookie，百度/抖音指数采不到；跳过才不会白等 90 秒 |
| `ENABLE_SCHEDULER` | `1` | 开定时采集 |
| `COLLECT_HOURS` | `8,20` | 采集时刻（东八区） |
| `TOOL_API_PUBLIC_URL` | 部署完成后填 `https://<你的域名>.onrender.com` | OpenAPI 里对外地址，平台导入时不用手改 |
| `TOOL_API_TOKEN` | 点 **Generate** 生成并**记下来** | 平台调用要带这个口令；不填则任何人都能调 |

### 4. 等构建 & 验证

首次构建约 2~4 分钟（无 pip 依赖，很快）。部署完后：

```bash
curl https://<你的域名>.onrender.com/health
# {"ok": true, ..., "latest_snapshot": "2026-11-03 22:00:36", "auth": true}

curl https://<你的域名>.onrender.com/openapi.json | head -c 200
```

冷启动（免费实例休眠后第一次访问）大概等 30~60 秒，属正常。

### 5. 平台里登记工具

用 `交平台-接入说明.md` 第四节那张表，把 `<域名>` 换成 `https://<你的域名>.onrender.com`，
并在请求头统一加：

```
Authorization: Bearer <TOOL_API_TOKEN>
```

5 个工具：

| 工具 | 地址 |
|---|---|
| `get_crowd_matrix` | `POST /tools/get_crowd_matrix`　`{"days":8}` |
| `recommend_destinations` | `POST /tools/recommend_destinations`　`{"days":2,"budget":600,"pref":""}` |
| `estimate_budget` | `POST /tools/estimate_budget`　`{"dest":"沈阳","days":2}` |
| `predict_holiday` | `POST /tools/predict_holiday`　`{"dest":"长白山","window_start":"2026-12-31","window_end":"2027-01-02"}` |
| `explain_method` | `POST /tools/explain_method`　`{"topic":"alpha"}` |

配完在平台"测试调用"里点一次，能返回 JSON 就成了。

---

## 三、你会遇到的三个限制（提前知道就不慌）

1. **免费实例 15 分钟无请求会休眠**：第一次调用慢 30~60 秒，之后正常。
   答辩前先用浏览器打开一次 `/health` 预热。
2. **磁盘是临时的**：重新部署后，库会回到仓库里那个版本，然后定时采集重新覆盖。
   也就是说"重启丢几小时数据"，不丢历史（历史在 git 里）。
3. **百度/抖音指数在云端不采**（`SKIP_HEAT=1`）：
   矩阵、车票、天气、α 预测全都不受影响，只有热度曲线会停在最后一次写入的时间。
   **要云端也有热度，走第四节的"本地推送"，不要把 cookie 传到服务器上。**

---

## 四、热度怎么上云：本地推送，不传凭证

百度指数要登录后的 cookie（约 2~4 周过期），抖音指数要一串扫码登录态的浏览器 profile，
这两样都是**个人账号凭证**：放到服务器上有泄露风险，而且机房 IP 异地登录极易触发风控，
cookie 会提前失效——结果是"传一次管三天"，还得你重新登录。

所以方向反过来：**热度在本地采，采完只把数字 POST 上去。**
连接方向是本机 → 云端（普通 HTTPS 出站），**不需要内网穿透、不用开端口**。

### 云端侧（已内置）

| 端点 | 作用 |
|---|---|
| `POST /ingest/heat` | 收 `{"rows":[{dest,keyword,source,date,heat}]}`，幂等 upsert 进 `heat_daily` |
| `GET /ingest/status` | 看各源覆盖情况（到哪天、多少行），确认推送是否成功 |

两条都要求 `Authorization: Bearer <TOOL_API_TOKEN>`；**没设 TOKEN 时写入直接被拒（503）**，
避免写接口裸奔。想彻底关掉就设 `INGEST_ENABLED=0`。

### 本机侧（配置一次，之后全自动）

双击 **`推送配置.bat`**，按提示填两项：域名（只填 `xxx.onrender.com` 那段）和 `TOOL_API_TOKEN`。
它会写成 `data/cloud.json`（已在 `.gitignore` 里，不会被推到 GitHub）。

之后只跑 `push_heat.py` 就行，地址和口令自动读配置：

```bash
python push_heat.py --all      # 首次：历史热度全量推一遍
python push_heat.py            # 日常：推最近 2 天（幂等，重复推不会写重复行）
python push_heat.py --dry-run  # 只看要推多少行，不发送
```

管理配置：`python set_cloud.py --show` 查看（口令打码）、`--clear` 删除。
命令行参数与环境变量（`TOOL_API_URL` / `TOOL_API_TOKEN`）仍然可用，优先级更高。

配好之后，**`一键更新并打包.bat` 会在采集完自动推送**（没配置就跳过，不报错）。

### 推送的边界（说清楚，别指望它解决关机）

- 推送只让**云端有热度数据**，采集本身仍在你的电脑上；**电脑关机，热度就停更**（余票/天气不受影响，云端自己采）。
- 云端拿到的是"截至本机最后一次推送"的数据，`/ingest/status` 里的 `last_date` 就是它的真实新鲜度。

---

## 五、其他平台（备选）

| 平台 | 用什么 | 备注 |
|---|---|---|
| **Railway** | 仓库已有 `Procfile`（`web: python cloud_run.py`） | 自动识别，也支持定时（free 额度有限） |
| **Fly.io** | `fly launch --dockerfile Dockerfile` | 需 `fly.toml`，可挂持久卷（数据不丢） |
| **任意云主机** | `docker build -t trip . && docker run -d -p 80:8080 trip` | 前面加 Nginx + HTTPS |

想"数据永久不丢"，就用 Fly.io 挂一个 volume 到 `/app/data`，或云主机直接跑——免费层的临时磁盘是省事换来的代价。

---

## 六、上线自查清单

- [ ] `curl https://<域名>/health` → 200，`latest_snapshot` 是今天
- [ ] `curl -H "Authorization: Bearer <token>" https://<域名>/tools/explain_method` → 返回口径说明
- [ ] 平台 5 个工具"测试调用"全通，超时设 ≥ 15s（冷启动）
- [ ] 日志里能看到 `[collect] done rc=0`（说明定时采集真的跑了）
- [ ] 答辩前一天确认 `latest_snapshot` 是当天；不是就手动访问一次 `/health` 唤醒

---

## 七、本地想模拟云端跑

```bash
set PORT=8925
set SKIP_HEAT=1
set ENABLE_SCHEDULER=1
python cloud_run.py
# 另开窗口
curl http://127.0.0.1:8925/health
```
