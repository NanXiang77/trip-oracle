# 把工具服务搬到 Hugging Face Spaces

比 SnapDeploy 强的地方：16GB 内存（现在 512MB）、48 小时才休眠（现在 15 分钟）、
部署次数不限（现在 5 次/12 小时）、同样不要信用卡。

---

## 一、你只做三件事

### 1. 注册（2 分钟）

打开 **https://huggingface.co/join** → 点 **Sign up with GitHub**（用你刚注册的 GitHub 账号）
→ 授权 → 填个用户名（比如 `NanXiang77`）→ 完事。

### 2. 建一个 Space（1 分钟）

打开 **https://huggingface.co/new-space**，只填四样：

| 字段 | 填什么 |
|---|---|
| Space name | `trip-oracle`（必须一字不差） |
| License | 随便选，`mit` 就行 |
| **Select the Space SDK** | **Docker** → 选 **Blank** |
| Space hardware | **CPU basic · 2 vCPU · 16GB · FREE**（默认就是） |
| Visibility | **Public**（免费版只能公开，我们仓库本来就公开） |

点 **Create Space**。建完会显示一个空仓库页面，**不用管它**。

### 3. 生成一个写入令牌（1 分钟）

打开 **https://huggingface.co/settings/tokens** → **Create new token**
→ Token type 选 **Write** → 名字随便（`trip-oracle-deploy`）→ Create
→ **复制那串 `hf_...` 开头的字符串**（只显示一次）。

**把这三样发我：用户名、Space 名、`hf_...` 令牌。** 剩下的我全包。

> 不想把令牌发我？也可以自己跑：
> ```
> python push_hf.py --user 你的HF用户名 --token hf_xxx
> ```
> 脚本会推代码、等你确认构建结果。

---

## 二、我做什么

1. 把本地代码（含最新数据库、`/mcp` 端点）推到 HF 的 git 仓库
2. HF 自动构建镜像（3~5 分钟），起容器
3. 验证 `https://<用户名>-trip-oracle.hf.space/health` 和 `/mcp`
4. 给你最终的 MCP 地址和口令

---

## 三、最后一步（你在华为云点）

云端起来后，回到华为云 AgentArts：

- 我的资产 → MCP → 点开「周末去哪…」→ 编辑
- 地址换成：`https://<用户名>-trip-oracle.hf.space/mcp`
- 鉴权不变：Header / `X-Api-Token` / 口令值（我验证后告诉你）
- 保存 → 点「重新部署」→ 工具数应该从 0 变 5

---

## 四、几个要知道的差别

| 项目 | SnapDeploy | HF Spaces |
|---|---|---|
| 内存 | 512 MB | 16 GB |
| 休眠 | 15 分钟无请求 | 48 小时无请求 |
| 冷启动 | 30~60 秒 | 30~90 秒 |
| 部署次数 | 5 次 / 12 小时 | 不限（git push 就构建） |
| 持久磁盘 | 有 | 免费档**没有**（重启回到镜像初始状态） |
| 网址 | `xxx.containers.snapdeploy.app` | `xxx.hf.space` |

**关于"没有持久磁盘"**：影响不大，因为

- 数据库文件 `data/oracle.db` 是**打包进镜像**的，容器一起来就有全部历史
- 云端每天 08:00 / 20:00 自己采的数据写在容器内，重启会丢——但重建时会用 GitHub 上的最新库
- 热度靠本地推送，重启后重推一次即可（`python push_heat.py --all`）

真要长期稳定，后续可以把 db 放 HF 的 Dataset 仓库里当外部存储，那是后话。

---

## 五、出问题了怎么查

HF Space 页面上有 **Logs** 标签，能看到容器输出（我们的程序会打印采集日志）。
如果状态卡在 **Building** 超过 10 分钟，或者变成 **Runtime error**，把 Logs 最后 20 行发我。
