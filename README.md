# llm-gateway — 免费额度优先的 OpenAI 兼容网关

一个"先薅免费、后付费"的大模型网关：对外暴露 OpenAI 兼容接口，对内把请求路由到
**当前还有免费额度的最强模型**，全平台免费耗尽后才落付费兜底。

## 特性

- **OpenAI 兼容**：`POST /v1/chat/completions`、`/v1/embeddings`，`model` 填 `auto` / `auto-cheap` / `auto-balanced` / `auto-quality`
- **免费额度优先级联**：多平台免费额度（按模型各自剩余量/到期日）→ 0 元永久免费模型 → 廉价付费兜底
- **实时免费额度看板**：百炼控制台"免费额度"页数据自动抓取（每模型剩余量/到期时间），每日定时 + 按需刷新
- **质量门槛**：`balanced` 档只用 tier≥2 模型、`quality` 档只用 tier≥3，推理请求不会被 7B 小模型接住
- **内置聊天页与会话记忆**：`/ui` 网页聊天，服务端按会话存上下文（客户端只发新消息），自动截断防止历史滚雪球，每条回复显示实际模型/是否免费/token 消耗
- **长期记忆层（自进化地基）**：每轮对话后自动用免费模型抽取值得记住的信息（偏好/事实），免费向量模型嵌入去重入库；新对话按语义检索 top-k 注入 system prompt——**跨会话记得你是谁**；`/ui` 记忆面板可查看/增删
- **省钱配套**：响应缓存去重、多 Key 轮换、失败冷却、每日预算与降级

## 快速开始

```bash
pip install -r requirements.txt
playwright install chromium          # 可选：启用百炼免费额度实时抓取
copy .env.example .env               # 然后填入你自己的 API Key
python -m uvicorn app.main:app --host 0.0.0.0 --port 8021
```

## 配置

1. **密钥**放 `.env`（已 gitignore，不会被提交/分发），格式见 `.env.example`
2. **上游/模型/价格/免费额度**在 `config/providers.json`：
   - 每个上游：`base_url`、`api_keys`（写 `${VAR}` 占位）、`enabled`
   - 每个模型：`tier`(1便宜/2均衡/3高质)、`price_in/out`(¥/1M tokens)、`caps`、
     `free_quota_tokens`(免费额度基线)、`free_expire`(到期日，可选兜底)
3. **路由策略**在 `providers.json` 的 `policy` 节（默认任务档、缓存、预算、免费低水位等）

## 路由规则（免费优先 + 质量门槛）

| 任务档 | 免费模型门槛 | 免费组排序 |
|---|---|---|
| `auto-cheap` | 不限 | 最便宜优先 |
| `auto-balanced` | tier≥2 | 能力优先（tier 高者胜，同级价高≈更强） |
| `auto-quality` | tier≥3 | 同上 |

一个模型免费耗尽（实时剩余量为 0 / 到期 / 上游 403）→ 自动切到下一个免费模型；
同档免费全部耗尽 → 按档位下限取最便宜付费。

## 百炼免费额度实时抓取（可选）

对阿里云主账号：网关会用 Playwright 驱动浏览器打开百炼控制台"免费额度"页，
抓取**每个模型**的剩余额度与到期时间（该数据无公开 API）。

```bash
# 首次/登录态过期时：弹出浏览器登录一次（SSO 通常免验证码）
python -c "import app.freetier as f; f.login_once()"
# 手动刷新一次
python -m app.freetier
```

- 登录态存在 `.pw-profile/`（已 gitignore），刷新默认每 10 分钟按需、每日定时
- 不配置浏览器时网关照常工作，免费额度退化为本地记账 + 上游 403 反馈

## 主要接口

| 接口 | 说明 |
|---|---|
| `POST /v1/chat/completions` | 对话（OpenAI 兼容，无状态） |
| `POST /v1/embeddings` | 向量（自动选免费向量模型） |
| `GET /ui` | 内置聊天页（会话记忆 + 模型/token 可视化） |
| `POST /api/chat` | 会话聊天：只发新消息，服务端拼上下文（自动截断最近 12 条） |
| `GET /api/chat/history?sid=` / `POST /api/chat/clear` | 会话历史 / 清空 |
| `GET /api/memory` / `POST /api/memory` / `POST /api/memory/delete` | 长期记忆：列表 / 手动添加 / 删除 |
| `GET /api/quota/debug` | 免费额度看板：每模型剩余量/到期/低水位 |
| `POST /api/config/reload` | 热加载配置 |

### 会话记忆（/api/chat）

- 上下文存服务端 sqlite（`messages` 表，按 `sid` 隔离），客户端每轮只发新消息
- 拼上下文自动截断：默认最近 12 条（`policy.session_max_messages` 可调），多轮对话不会把免费额度吃光
- 每条回复带 `model/upstream/free/tokens/cost` 元数据，前端可视化

## 安全提示

- `.env`、`.pw-profile/`、`.freetier_cache.json` 含密钥/登录态，**不要**提交或分发
- 分发项目时只给：代码 + `config/providers.json`（占位符版）+ `.env.example`
