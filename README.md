# Vorflux Gateway

`us1.vorflux.com` 的 OpenAI 兼容 API 网关 —— 逆向其前端协议（Auth0 OTP + GraphQL）后封装，
支持**多账号轮询并发**、**每账号独立代理 + 全局代理**、**熔断重试**，带管理 WebUI。

## 逆向出的上游协议

| 环节 | 端点 | 说明 |
|---|---|---|
| 登录① | `POST /api/auth/passwordless/start` `{email}` | 发送 6 位邮箱验证码 |
| 登录② | `POST /api/auth/passwordless/verify` `{email, code}` | → `access_token`/`id_token`/`refresh_token`/`identity` |
| 续期 | `POST /api/auth/passwordless/refresh` `{refresh_token}` | 轮换 access token（24h） |
| GraphQL | `POST /query` | `Authorization: Bearer` + `X-Account-ID` |
| 会话 | `CreateAgentSession` / `AddMessageToSession` / `GetSession` / `GetSessionMessages` / `CancelSession` | MANUAL+GENERAL |
| 模型 | `ListAvailableModelsForAccount` | modelKey + defaultModelKey |
| SSE | `GET /api/sessions/updates/stream`（cookie 经 `POST /api/sessions/auth`） | session_update 事件 |
| 状态机 | `QUEUED→RUNNING→AWAITING_INPUT/COMPLETED/ERROR/CANCELLED` | 轮询判终 |

## 快速开始

```bash
cd vorflux-gateway
pip install -r requirements.txt

# Windows
set ADMIN_TOKEN=your-admin-secret
set API_KEY=sk-your-master-key
python run.py

# Linux/macOS
ADMIN_TOKEN=your-admin-secret API_KEY=sk-your-master-key python run.py
```

- WebUI: `http://localhost:8787/`（用 `ADMIN_TOKEN` 登录；未设置则启动时自动生成并打印到控制台）
- OpenAI 端点: `http://localhost:8787/v1`

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `VGW_PORT` / `VGW_HOST` | `8787` / `0.0.0.0` | 监听地址 |
| `ADMIN_TOKEN` | 自动生成 | WebUI/管理 API token |
| `API_KEY` | – | `/v1` 主 key（也可在 WebUI 建子 key） |
| `VORFLUX_BASE` | `https://us1.vorflux.com` | 上游 |
| `VORFLUX_PROXY` | – | 全局代理（http/socks5，账号级代理优先） |
| `VGW_MAX_CONCURRENT` | `3` | 每账号默认并发 |
| `VGW_RETRY_BUDGET` | `2` | 跨账号重试次数 |
| `VGW_CB_FAILS` / `VGW_CB_COOLDOWN` | `3` / `60` | 熔断阈值/基础冷却秒 |
| `VGW_MAX_TURN_WAIT` | `600` | 单轮最长等待（秒） |
| `VGW_POLL_INTERVAL` | `1.6` | 消息轮询间隔（秒） |
| `VGW_POLL_FAST` / `VGW_POLL_FAST_POLLS` | `0.7` / `8` | 开局高频轮询（降首字延迟） |
| `VGW_STALL_TIMEOUT` | `90` | 无新输出判停滞→自动换号（秒） |
| `VGW_CONV_TTL` | `7200` | 会话亲和缓存有效期（秒） |
| `VGW_HEDGE_MS` | `0` | 对冲请求：N ms 后并行备份尝试（0=关，开启会双倍计费） |

## OpenAI 调用

```bash
curl http://localhost:8787/v1/chat/completions \
  -H "Authorization: Bearer sk-your-master-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "vorflux-auto",
    "messages": [{"role":"user","content":"写一个 Python 快排"}],
    "stream": false
  }'
```

- `model`：`vorflux-auto`（上游默认模型）或 `/v1/models` 返回的 modelKey，可在设置里配模型映射
- `session_id`（扩展字段）：继续已有 vorflux 会话（走 `AddMessageToSession` 而不是新建）
- **工具调用**：`tools`/`tool_choice` 全兼容 —— 经 prompt 注入由 agent 产出结构化调用，网关解析为标准 `tool_calls` + `finish_reason:"tool_calls"`；`tool` 消息回传可驱动完整循环；流式下自动缓冲防 JSON 碎片
- `POST /v1/responses`：Responses API（Codex CLI/智能体用），流式全事件序列 + function_call 支持
- 响应附带 `vorflux` 块：`session_id` / 命中的 `account` / 会话 URL / `session_status`
- 兼容任意 OpenAI SDK：`base_url=http://localhost:8787/v1`，`api_key=<网关key>`

## 账号管理（WebUI）

- **邮箱 OTP**：添加账号 → 输入邮箱 → 收 6 位码 → 验证，自动完成 `registerUser`/`selectAccount` 引导
- **Token 导入**：直接粘贴 `refresh_token` —— 支持两类自动识别：邮箱 OTP token；社交登录（Google/GitHub）Auth0 轮换 token（`v1.` 开头，DevTools → Local Storage → `@@auth0spajs@@` 条目 `body.refresh_token`），OAuth 账号自动走 `/oauth/token` 轮换
- 每账号可配：独立代理（socks5/http）、最大并发、启用/禁用、手动刷新 token、重置熔断、连通性测试

## 稳定性设计

- **轮询调度**：健康账号中按 `(inflight, LRU)` 最小负载选取
- **熔断**：连续失败 ≥3 次 → 指数冷却（60s→15min 封顶），半开恢复
- **Token 生命周期**：过期前 120s 自动刷新；401 → 强制刷新重试；refresh 被拒 → 硬熔断
- **跨账号重试**：网络/5xx/限流自动换号（`VGW_RETRY_BUDGET`），GraphQL 业务错不盲试
- **优雅判终**：状态机到终态后再宽限 `VGW_IDLE_GRACE_POLLS` 次轮询，收齐尾部消息
- **请求日志**：SQLite ring buffer（3000 条），WebUI 可查
- **信用熔断**：上游报 `Credits are exhausted` → 账号自动 `no_credits` 移出轮询（重试不耗预算），test 检测余额>0 自动复活
- **会话亲和**：客户端重发全量历史时前缀哈希命中 → `AddMessageToSession` 只发增量（降延迟/长上下文错误/上游缓存命中），pinned 到原账号，失败自动降级新建会话
- **对冲**：`VGW_HEDGE_MS>0` 时慢请求并行备份尝试，先完成者胜（流式按首 delta 定主）

## 文件结构

```
vorflux-gateway/
├── run.py                 # 入口
├── requirements.txt
├── gateway.db             # SQLite（首次运行生成）
├── app/
│   ├── config.py          # 环境变量
│   ├── db.py              # 持久化
│   ├── vorflux.py         # 逆向协议客户端 ★
│   ├── pool.py            # 账号池：调度/熔断/并发/token
│   ├── openai_api.py      # /v1/models /v1/chat/completions
│   ├── admin_api.py       # /admin/api/*
│   └── main.py            # 装配
└── webui/                 # 管理台（GSAP，无构建步骤）
```

## 已知边界

- vorflux 会话是「任务式 agent」，一轮可能要几十秒到几分钟 —— 调大客户端 timeout；
  `stream:true` 会持续推送增量消息
- 回复以消息为单位到达（非逐 token），流式模式下按消息块推送
- `AWAITING_INPUT` 视作完成；若 agent 中途需要用户决策（UserDecisionRequest），当前版本按已产出的文本返回
- 账号配额取决于各账号自身的 credit/plan
