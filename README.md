# Scoped API Gateway（Python / FastAPI）

面向外部调用方的 API 网关准入层：在业务路由之前统一完成
**版本协商 → 凭据认证 → 权限授权 → 写操作幂等 → 配额控制 → 审计**，
所有放行与拒绝都有确定、可区分、可审计的结果，且在 asyncio 并发下保持一致。

## 1. 快速开始

```bash
uv sync                       # 安装依赖（Python >= 3.14）
uv run python main.py         # 启动服务：http://127.0.0.1:8000
uv run pytest                 # 运行全部测试（27 个），日志打印关键输入与判定依据
```

最小调用示例：

```bash
curl -s http://127.0.0.1:8000/healthz

curl -s http://127.0.0.1:8000/v1/echo \
  -H 'X-Api-Key: sk-active' \
  -H 'X-Api-Version: 2026-01-01'

curl -s -X POST http://127.0.0.1:8000/v1/orders \
  -H 'X-Api-Key: sk-active' \
  -H 'Idempotency-Key: order-0001' \
  -H 'Content-Type: application/json' \
  -d '{"item":"book","quantity":2}'
```

## 2. API 版本策略

通过请求头 **`X-Api-Version`** 显式选择版本；响应头回显实际协商出的版本。

| 版本 | 状态 | 行为 |
|---|---|---|
| `2023-01-01` | 已下线 retired | `410 Gone`，`error.code=version_retired`，`details.successor` 指向下一版本 |
| `2024-01-01` | 受支持 supported | **未携带版本头时的固定默认版本**（默认行为确定，不隐式升级） |
| `2025-01-01` | 已弃用 deprecated | 仍正常返回；带 `Deprecation: true`、`Sunset: <下线日期>`、`Link: ...; rel="successor-version"` |
| `2026-01-01` | 受支持 supported | 最新版本 |

- 未发送（或发送空）`X-Api-Version` → 固定解析为默认版本 `2024-01-01`。
- 未知版本 → `400 Bad Request`，`error.code=version_unknown`，
  `details.supported_versions` 列出所有可用版本。
- **版本失败与权限失败可区分**：版本问题是 `400/410 + version_*`，
  认证问题是 `401 + credential_*`，权限问题是 `403 + permission_denied`。
  版本协商在认证之前执行，错误不会相互掩盖。
- 同一业务语义（`semantic` 字段）在所有受支持版本下返回一致结果，
  版本差异仅通过响应头表达；业务不兼容变更应使用新版本日期注册新语义。

## 3. 凭据、权限与密钥轮换

通过请求头 **`X-Api-Key`** 传递密钥。

| 场景 | HTTP | error.code |
|---|---|---|
| 未提供密钥 | 401 | `credential_missing` |
| 密钥不存在 | 401 | `credential_invalid` |
| 密钥不在有效期（过期/未生效） | 401 | `credential_expired`（`details` 含 `valid_from/valid_until`） |
| 凭据被撤销 | 401 | `credential_revoked` |
| 凭据有效但缺少路由 scope | 403 | `permission_denied`（`details.required_scope`） |

- 每个凭据有若干 scope；路由在 `route_scopes` 中声明所需 scope
  （`/v1/echo` 需 `echo:read`，`/v1/orders`、`/v1/flaky` 需 `orders:write`）。
- **密钥轮换**：一个凭据可同时注册多个密钥版本，每个版本有独立的
  `[valid_from, valid_until)` 有效期。轮换重叠期内**新旧密钥并存可用**；
  旧密钥到期后自动失效，新密钥继续有效。新旧密钥认证出的是**同一个
  `credential_id`，共享同一份配额**（轮换不重置、不加倍配额）。

内置演示凭据（见 `gateway/app.py`）：

| 密钥 | 凭据 | 说明 |
|---|---|---|
| `sk-active` | cred-active | 全权能，长期有效 |
| `sk-readonly` | cred-readonly | 仅 `echo:read` |
| `sk-rotating-old` / `sk-rotating-new` | cred-rotating | 轮换重叠期，旧钥次日到期 |
| `sk-expired` | cred-expired | 密钥已过期 |
| `sk-revoked` | cred-revoked | 已撤销 |

## 4. 配额规则

- 维度：**每凭据 × 每路由**的固定时间窗口计数（默认策略可按路由覆盖）。
  演示配额：`/v1/echo` 30 次/60 秒，`/v1/orders`、`/v1/flaky` 5 次/60 秒。
- 计数时机：只有**准入通过且真正执行**的请求才在窗口内占一个名额：
  - 认证/授权/版本/幂等冲突被拒的请求**不计数**；
  - 幂等**重放不计数**（同一业务只计一次）；
  - 下游返回 **5xx 或抛异常时回滚**本次占用，调用方可安全重试；
  - 下游 4xx 视为业务终态（占用保留）。
- 并发安全：每个 `(凭据, 路由)` 一把异步锁，check-and-increment 原子完成。
  并发打满时严格恰好 `limit` 个成功，其余全部 `429`，无重复计数、无漏计。
- 超限响应：`429 Too Many Requests`，
  `error.code=quota_exceeded`，`error.retryable=true`，
  `details` 含 `limit/used/window_seconds/retry_after_seconds`，
  并回标准响应头 **`Retry-After`**（秒）。

## 5. 写操作幂等语义

所有写方法（`POST/PUT/PATCH/DELETE`，受保护路由）必须携带
**`Idempotency-Key`** 请求头，否则 `400 idempotency_key_missing`。

- **首次提交**：执行业务、计一次配额、固化「状态码 + 响应体 + 载荷指纹」。
- **重复提交（同键、同凭据、同路由、同载荷）**：不执行业务、不产生额外副作用、
  不重复扣配额，直接返回**与首次完全一致的状态码和响应体**，
  并带响应头 `Idempotent-Replayed: true`。
- **同键不同载荷** → `409 Conflict`，`error.code=idempotency_payload_conflict`；
  请求不执行、不计数。
- **幂等键被其它凭据/路由复用** → `409`，`error.code=idempotency_key_reused`。
- **同键并发**：第二个及以后的请求等待首个完成，然后重放首个结果，
  业务在并发下严格只执行一次。
- **首请求业务失败（5xx/异常）**：清除进行中记录并唤醒等待者，
  允许用**同一个键**重试；成功后才固化终态。
- 载荷指纹：JSON 按规范化形式计算（键顺序无关），非 JSON 退化为原始字节哈希；
  因此 JSON 仅键序不同视为同一载荷。

## 6. 审计与可追溯

- 每个请求的每一次关键判定（`version_negotiation / authentication /
  authorization / idempotency / quota / request / idempotency_replay /
  business_failure / business_exception`）都写一条结构化审计记录，
  放行 `decision=allow` 与拒绝 `decision=deny` 均记录。
- 记录包含：`request_id`、`credential_id`、路由、协商版本、幂等键、
  判定原因（错误码）与错误详情，可关联到具体调用方与请求
  （响应头回显 `X-Request-Id`，也支持调用方自带）。
- **审计绝不包含明文凭据**：只记录密钥指纹 `sha256:<前12位>`；
  所有演示密钥在启动时被注册为「审计禁出值」，任何审计记录一旦包含明文
  （包括误放进 `extra` 的字段）会在写出时立即抛出 `AssertionError`，从运行时兜底。
- 本地可通过 `GET /__test/state` 查看内存中的审计事件、配额用量与副作用计数。

统一错误响应体：

```json
{
  "error": {
    "code": "quota_exceeded",
    "message": "...",
    "request_id": "…",
    "version": "2024-01-01",
    "retryable": true,
    "details": { "limit": 5, "used": 5, "retry_after_seconds": 42 }
  }
}
```

## 7. 本地验证

```bash
uv run pytest                      # 全量 27 个用例
uv run pytest tests/test_quota_concurrency.py   # 仅并发配额
uv run pytest tests/test_idempotency.py         # 仅幂等重放/冲突
uv run pytest -s                    # 同时查看 gateway.audit 审计日志输出
```

测试覆盖：

| 文件 | 覆盖点 |
|---|---|
| `tests/test_versions.py` | 默认版本确定性、显式选择、弃用头、未知(400)/下线(410)、版本与认证可区分、跨版本语义一致 |
| `tests/test_credentials.py` | 缺失/无效/过期/撤销/权限不足可区分、轮换新旧并存、过期边界 |
| `tests/test_quota_concurrency.py` | 100 并发严格 10 成功、按凭据×路由独立、失败回滚、真实 ASGI 20 并发订单恰好 5 成功 |
| `tests/test_idempotency.py` | 重放一致且无额外副作用、重放不扣配额、载荷/跨凭据冲突、10 并发同键只执行一次、审计无明文密钥 |
| `tests/test_failure_retry.py` | 5xx 回滚配额并释放幂等键、超限被拒不计数、同键失败后重试成功并固化 |
| `tests/test_error_matrix.py` | 10 类拒绝原因的 `(HTTP 状态码, error.code)` 两两可区分矩阵 |

手动联调辅助端点（公开，不经准入）：

- `POST /__test/reset`：清空配额窗口、幂等记录、审计与副作用计数；
- `GET /__test/state`：查看副作用计数、配额用量与审计事件。

## 8. 代码结构

```
gateway/
  errors.py        # GatewayError 分层：version/auth/permission/quota/idempotency
  versions.py      # 版本注册表（supported/deprecated/retired）与协商
  credentials.py   # 凭据、多密钥版本轮换、有效期、scope 授权
  quotas.py        # 每凭据×路由固定窗口配额（asyncio 锁，acquire/release）
  idempotency.py   # 幂等记录、并发等待、载荷指纹、完成/失败终结
  audit.py         # 结构化审计 + 密钥指纹（无明文）
  admission.py     # ASGI 中间件：按序串联全部判定、响应头、5xx 补偿
  app.py           # FastAPI 工厂：演示凭据/配额/业务路由/测试辅助端点
main.py            # 入口：create_app() + uvicorn
tests/             # pytest 套件（见上表）
```

> 说明：当前版本状态、凭据、配额与幂等记录均为**进程内存存储**，适合本地验证
> 与单进程部署；多进程/持久化场景可分别替换 `CredentialStore / QuotaManager /
> IdempotencyStore` 的实现，准入中间件接口不变。
