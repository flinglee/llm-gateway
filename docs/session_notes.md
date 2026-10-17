# llm-gateway 会话/决策笔记

> 跨会话真相源。新会话先读本文件。

## 来历（2026-10-16）
- 用户：「刚刚问过的**阿里云百炼网关**，写一个**优惠使用的应用**」。确认过两件事：
  1. 形态 = **独立服务**（不是 DCP 控制台的一个 Tab，也不是离线分析工具）：对外暴露 OpenAI 兼容 `/v1`，
     内部做「性价比路由 + 缓存去重 + 多 Key 轮换 + 预算/配额」。
  2. Key = 用户说已填，但**实测 `d:/project/data-convergence/backend/llm_providers.json` 里 bailian 的
     api_key 仍为空、enabled=false**（用脚本打印确认过）。所以代码按「有 Key 即用」实现，无 Key 时
     自动降级到规则/拒单并给出明确诊断。验证了**不要盲信用户口述配置**。
- 目录：`d:/project/llm-gateway`（与 DCP 平级的独立应用），端口 **8021**（DCP 用 8011）。

## 架构
```
app/config.py    读 config/providers.json，${ENV} 占位展开（如 ${BAILIAN_API_KEY}）
app/pricing.py   价目展平 / tier 过滤 / cheapest / cost_of / estimate_tokens / recommend_table
app/store.py     SQLite 账本：usage(明细) + cache(响应缓存) + keystate(Key 日花费/次数/冷却)
app/router.py    候选路由：模型按单价升序 × 可用 Key，受日预算与单 Key 日额度约束
app/upstream.py  urllib 调 OpenAI 兼容接口（chat / chat_stream / embeddings / probe）
app/main.py      FastAPI：/v1/*（OpenAI 兼容）+ /api/*（管理）+ 静态控制台（最后挂载）
dashboard/       index.html 控制台（单页，暗色，风格与 DCP 控制台一致）
```
- 依赖仅 fastapi + uvicorn（本机已装 fastapi 0.143 / uvicorn 0.54 / pydantic 2.14，Python 3.14）。

## 省钱四招（已实现）
1. **性价比路由**：`cheap|balanced|quality` 档 → tier>=档位中单价最低者（对话场景排除 embedding 模型）。
   `model=auto` / `auto-cheap` / `auto-balanced` / `auto-quality`，或 `x-task` 头。
2. **缓存去重**：非流式且 temperature<=0.3 → sha256(payload+model) 命中即返回，cost=0。
3. **多 Key 轮换**：每个上游可配多个 Key，按日额度/失败冷却跳过，自动换下一个。
4. **预算/配额**：全局日预算（超预算 `downgrade` 降级到最便宜档 / `reject` 拒绝）+ 单 Key 日额度。
   - 兼容老客户端：`gpt-4o-mini→cheap`、`gpt-4o/gpt-4→quality` 等别名自动映射；**未知模型不报错**，按默认档路由。

## 实测（假上游端到端，2026-10-16，临时文件已清理）
| 用例 | 结果 |
|---|---|
| cheap 首次 | 选中最便宜的 `fake-turbo`，cost=0.0006，key#0 |
| 相同请求再发 | **缓存命中，cost=0，cached=True** |
| Key0 达日额度后的新请求 | **自动轮换到 Key#1** |
| balanced | 落到 tier>=2 的 `fake-max` |
| `gpt-4o` 别名 | 映射 quality → `fake-max` |
| 显式模型 / 未知模型 | 直连 / 回落默认档 |
| 两 Key 均超额 | 503 + 明确诊断文案 |
真实 Key 尚未接入（用户待填），在线路径待验证。

## 单 Key + 免费额度优先（2026-10-16 用户提出，同日实现）
- 用户原话要点：**只有一个 Key**，不想为用完免费额度去平台网站来回切账号；百炼各模型各有免费/限免 token，
  要求「**先把免费的 token 都用完，再看哪个最便宜用哪个**」。
- 落地：
  1. `models[]` 新增 `free_quota_tokens`（免费额度 tokens，0=无）与 `free_period`（day|month|forever，默认 month）。
  2. 新增 `modelstate` 表按 (upstream, model, period) 累计 tokens —— **额度按模型计量，不是按 Key**，所以单 Key 也能逐个模型薅。
  3. 路由顺序（`pricing.candidates` 排序键 `(免费是否未尽, unit, tier)`）：
     有免费额度的（内部按单价）→ 全部用尽后**全局最便宜** → 超预算再降级。
  4. `policy.prefer_free=true`（纳入免费低档模型）、`policy.free_first_then_cheapest=true`
     （cheap/balanced/free 档免费用尽后取全局最便宜，不再被档位下限卡住）；
     **quality 档例外**：坚持 tier>=3，不拿免费低档模型凑数（`router.py` 里 `FREE_FIRST_TASKS` 白名单控制）。
  5. 记账区分免费/付费：`usage.free + price_in/price_out`，`stats.free_saving` 直接算出"薅到了多少钱"；
     响应 `gateway.free_used / free_left / reason`（reason 明说"免费额度优先：还剩 N tokens"）。
  6. 控制台：模型表加「免费额度(周期)/已用/剩余」列，概览加"薅到的免费额度"卡片，试用面板加 `free` 档。
- 假上游端到端实测（单 Key，免费额度 3000/月，每次调用 1500 tokens，免费模型单价比另一付费模型贵）：
  1、2 次 → 免费模型（left 3000→1500，**部分剩余仍坚持免费**）；3、4 次 → 切到更便宜的付费模型；
  quality → 最贵高质模型；`free_saving`=0.002 / 3000 tokens / 2 次。全部符合预期。

## 踩坑（都真实发生过）
1. `spend_today()` 查 `keystate` 用错列名 `cost` → 实际是 `spend`（500）。
2. `record()` 插 `keystate` 占位符 7 个只传 5 个 → `(?,?,?,?,1,0,0)` + 4 参。
3. **SQLite 死锁**：`cache_clear()` 在 `with _LOCK:` 内又调 `db()`（内部再取同一把锁）→ 之后所有请求 TimeoutError。
   修：`threading.RLock()`。（教训：**持锁区内不要再调用会自行取锁的函数**。）
4. **embedding 被当成最便宜对话模型**：tier1 的 text-embedding-v3 单价比 qwen-turbo 低，直接按单价排序会选中它去聊天。
   修：`pricing.candidates()` 在无 cap 时默认排除 `caps` 含 `embedding` 的模型。
5. 静态 `mount("/")` 必须在**所有 API 路由之后**（DCP 老坑，此处提前规避）。
6. 冒烟连跑第二遍会因**上一轮账本残留**（Key 日额度已耗尽）全部 503 —— 重跑前删 `data/gateway.db`。
7. PowerShell 起后台服务看不到日志：用 `Start-Process ... -RedirectStandardError .\data\server.err`，
   报错就 `Get-Content .\data\server.err -Tail 30 -Encoding UTF8`（-Encoding 必带）。
8. **冒烟被工具空闲超时杀掉时 `finally` 不执行** → 配置没还原、备份还在。对策：冒烟脚本自己
   `Popen` 起服务并负责 terminate；切配置前先 `copyfile(REAL, BAK)`；**开跑前删 `data/gateway.db`**
   （否则上一轮失败的 Key 冷却 180s 会让本轮全部 503，看起来像功能坏了）。
9. `write_to_file` 写文件时命令被中断会留下**空文件**（`_fake_upstream.py` 就这样空过一次，
   报 `Attribute "app" not found in module`）。写完文件后要 `py -m py_compile` 或读回确认。
10. 测试数据陷阱：若"免费模型"同时也是最便宜的付费模型，免费用尽后看起来"没切换"——
    验证切换必须让免费模型的付费单价比另一个模型**贵**。
11. 推荐表与路由曾口径不一致（`recommend_table` 用 tier 下限、`router` 用全局最便宜）→ 统一为
    `pricing.route_pool(task)` 单一真相源，两边共用。

## 业务分类调度 + 多应用接入（2026-10-17 用户提出，同日实现）
- 用户原话：项目里已分「建库、分析、爬取」，后续还会有「视觉、语音识别」等；问
  ①怎么在当前项目里用这个模型调度 ②怎么给其它应用提供服务。
- 落地：
  1. `config/categories.json`：`categories[]`（name/label/task/caps/models/daily_budget_cny）+ `apps[]` + `gateway_keys[]`。
     `caps` 需全部满足；`models` 是白名单；分类日预算独立于全局预算。
  2. 客户端三种指定方式（优先级）：body `category` > `x-task` 头 > **分类名当 model**（如 `model="vision"`）> default 分类。
     `GET /v1/models` 同时列出分类与真实模型，OpenAI SDK 侧可见。
  3. `pricing.pool_for_category()`：有 caps 的分类直接从"满足能力的模型"里选（**不能走对话候选池**——
     那条池会排除 embedding 模型，会把 embedding 分类打成空池）；`free_first` 类任务不设档位下限，
     quality 类设 tier 下限；排序仍是 `(免费未尽, unit, tier)`。
  4. 应用身份：body `app` / `x-app-id` 头 / `?app=` / `Authorization: Bearer <gateway_key>` 映射；外部应用不接触上游 Key。
     `usage` 增加 `category/app` 列，`stats` 增加 `by_category/by_app`；应用级日预算在路由里生效。
  5. 控制台加「业务分类调度」「接入的应用」两张表；试用面板加分类下拉（动态取自 /api/categories）。
  6. API：`GET /api/categories`、`POST /api/categories/reload`、`GET /api/apps`。
- 假上游实测：`model=schema_build`→便宜档 json 模型；`model=vision`→视觉模型；`model=analysis`(quality)→最贵高质；
  `body.category=vision` 同等生效；`Bearer` 网关 Key→app=dcp 且**应用日预算超限自动降级**；未知分类→400 并列可用分类；
  `/v1/embeddings` 自动走 embedding 分类选向量模型；按分类/按应用统计正确。

### 本轮踩坑（第 12-14 条）
12. **查分类查错了配置对象**：`router.choose` 里 `categories.get(name, cfg)` 第二个参数传成了上游配置 providers.json
    （里面没有 categories 键）→ 静默取不到分类 → 分类约束全失效（vision 选成了最便宜的文本模型）。
    修：`choose(..., ccfg=...)` 显式传分类配置，所有分类查询统一用 ccfg。
13. **有能力要求的分类被对话池规则误伤**：`candidates()` 在无 cap 时主动排除 embedding 模型，
    `pool_for_category` 复用它导致 embedding 分类空池。修：caps 场景直接从 all_models 过滤+排序。
14. **超预算降级丢掉分类约束**：降级分支用了 `candidates(1, cap=None)`，vision 会被降级成纯文本模型。
    修：降级也走 `pool_for_category`。
15. `write_to_file` 往**工作区外**（`d:/project/llm-gateway/...`）写文件，有概率被写成 0 字节且回报成功
    （`_fake_upstream.py` 连踩两次，报 `Attribute "app" not found in module`）。对策：冒烟脚本用内嵌源码
    自己 `write_fake()` 生成；凡写工作区外文件后必须 `py -m py_compile` 或读回校验。
16. 冒烟脚本的清理逻辑在失败路径也会删临时配置文件，导致重跑时 `FileNotFoundError`。
    修：临时配置改为**内嵌 dict**，跑完从备份还原（`restore()` 只还原、不删临时文件）。

## DCP 接入改造（2026-10-17，用户要求"修改 dcp 让它能调网关"）
改的都是 DCP（`d:/project/data-convergence`），网关本身零改动：
1. `backend/llm_providers.json`：新增 `gateway` provider（`base_url=http://127.0.0.1:8021/v1`、`model=auto`、
   `api_key=gw-dcp`、`enabled=true`），`default` 改为 `gateway`；原 bailian 等 8 个 provider 原样保留、enabled=false。
2. `backend/app/services/llm_client.py`：`chat()`/`_chat_one()` 增加可选 `headers` 参数并透传（用于传 `x-task` 分类）。
3. 按场景传分类：`schema_builder.py` → `x-task: schema_build`；`governor.py` → `x-task: analysis`；
   `diagnoser.py` → `x-task: crawl`。source_advisor 未改（走 default chat 分类）。
4. 运行方式（两服务都已按此启动并验证）：
   - 网关：`cd d:/project/llm-gateway; py -3 -m uvicorn app.main:app --host 0.0.0.0 --port 8021`
   - DCP：`cd d:/project/data-convergence/backend; py -3 -m uvicorn app.main:app --host 0.0.0.0 --port 8011`
   - 页面：网关控制台 http://localhost:8021/ ｜ DCP 控制台 http://localhost:8011/
5. 链路实测：DCP `POST /api/llm/test {"provider":"gateway"}` → 网关 → 返回 503「无可用模型（已启用但缺少 Key）」→
   DCP 按设计降级返回 `{"ok":false}`，不崩溃。**唯一阻塞是百炼 Key 未填**。
6. 关于"Key 已填"的第三次澄清：用户在第二轮说"已经填到 llm_providers.json 了"，但我当时**实测打印**
   DCP 的 `backend/llm_providers.json`，bailian 的 `api_key` 仍为空、`enabled=false`（网关侧 `${BAILIAN_API_KEY}` 同样未设）。
   即：没有 Key 被覆盖丢失，但也从没真正填上。正确做法是填到**网关**的 `d:/project/llm-gateway/config/providers.json`
   的 `bailian.api_keys`（配置每次请求热加载，填完即生效，无需重启）。

## 控制台可视化配置 Key/价格（2026-10-17，用户要求"界面配 Key，不要改文件"）
- 新增 `app/config_admin.py`（`/api/config/*`），控制台新增「Key 与价格配置」卡片：
  - 上游 Key：掩码展示、添加、按序号删除、enabled 开关；
  - 网关访问 Key：添加（key+app，app 不在 apps 里会自动登记）、删除；
  - 模型价格/免费额度：price_in/price_out/free_quota_tokens/free_period 在线改。
- 写操作直接落盘 `config/*.json`（先写 .tmp 再 replace，防半截文件）；配置每次请求热加载，保存即生效。
- 安全：所有 `/api/config` 读写都要求请求头 `x-admin-key`（默认 `gw-admin`，
  可用环境变量 `GATEWAY_ADMIN_KEY` 改）。控制台把密钥存 localStorage，之后无感。
- 实测 11 项全过：无密钥 403 / 错误密钥 403 / 读配置 / 加 Key / 停启用 / 改价格 / 发网关 Key /
  删网关 Key / 删上游 Key / health 生效。

### 本轮踩坑（第 19-20 条）
19. **`${ENV_VAR}` 占位符被外部机制展开成 `${}`**：`providers.json` 里 `bailian.api_keys` 从
    `${BAILIAN_API_KEY}` 变成 `${}`（PowerShell/工具链把 `${...}` 当变量占位替换为空），
    导致该 Key 静默失效、界面显示 `${}（环境变量占位）`。修复用**工作区内的 python 脚本**
    做精确字符串替换恢复（`"${}"` → `"${BAILIAN_API_KEY}"`）。
    教训：① 含 `${}` 的配置不要用 `py -c`/PowerShell 命令改，也不要指望 write_to_file 到
    工作区外（已两次被截断/替换）；② 改完必须读回校验值，不能只看"文件存在"。
20. `GET /api/config` 最初忘了加 `_check()`，会把默认管理密钥通过 `admin_key_hint` 返回给
    未认证方 → 已改为读也要密钥。

## 下一步建议
- 填真实百炼 Key（`config/providers.json` 的 `bailian.api_keys`，或环境变量 `BAILIAN_API_KEY`）→
  `POST /api/probe` 验证连通 → 控制台看实时花费。
- 让 DCP 的 `llm_base_url` 指向 `http://localhost:8021/v1`，即可让 schema_builder/governor 自动走省钱路由。
- 尚未做：官方实时价拉取、多实例（SQLite 需换）、按用户维度的配额。
