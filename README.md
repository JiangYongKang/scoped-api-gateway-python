# Scoped API Gateway — 准入层（FastAPI）

面向外部调用方的 API 网关准入层，解决四件事：

1. **API 版本协商**——显式选版本、确定的默认版本、未知/下线/弃用可区分；
2. **凭据与配额**——有效/过期/撤销/权限不足区分明确，密钥轮换期新旧并存，
   每凭据每路由的配额在并发下精确，重复/并发/失败请求不多扣也不漏扣；
   支持**批量写入按条计费**（整批全有或全无），并有**每路由共享容量上限**兜底；
3. **写操作幂等**——同键重放返回首次结果且无额外副作用，同键不同载荷被拒绝；
4. **可审计**——放行与拒绝全部留痕、可关联调用方，审计内容不含明文凭据；
   调用方可**自助查询用量**（只读、不扣额度）。

## 1. 版本策略

- 调用方用请求头 **`X-Api-Version`** 选择版本；
- 未携带该头时默认使用 **`v2`**（最新稳定版），行为确定；
- `GET /versions` 可查询完整版本策略（无需认证）。

| 版本 | 状态 | 行为 |
|------|------|------|
| `v3` | supported（最新） | 正常服务 |
| `v2` | supported（默认） | 正常服务；未指定版本即走此版本 |
| `v1` | **deprecated** | 仍可用；响应带 `Deprecation: true`、`Sunset`、`X-Retirement-Date: 2027-06-01`、`X-Successor-Version: v2` |
| `v0` | **retired**（2025-01-01 下线） | 返回 `410`，错误码 `version_retired`，体中给出下线日期与替代版本 |
| 其它 | unknown | 返回 `400`，错误码 `version_unknown`，体中给出支持的版本与默认版本 |

版本失败与权限失败通过状态码与错误码明确区分：
`400 version_unknown` / `410 version_retired` ≠ `401 *` / `403 forbidden`。

**跨版本一致性**：`v1/v2/v3` 共用同一套业务处理与同一套额度核算（批量写、
共享容量、用量查询在所有可用版本上行为一致），返回 `data` 部分完全一致，
只有外层 `api_version` 与弃用响应头不同。

## 2. 凭据规则

- 调用方用 **`X-Api-Key`** 头出示密钥；系统只保存密钥的 SHA-256 哈希，
  日志/审计里永远不出现明文（审计模块对敏感字段做脱敏，并提供校验断言）。
- 认证结果互斥且可区分：

| 情况 | 状态码 | error.code |
|------|--------:|------------|
| 未带密钥 | 401 | `missing_credential` |
| 密钥不存在 / 尚未生效 | 401 | `credential_invalid` |
| 已过期（`now >= valid_until`） | 401 | `credential_expired` |
| 已撤销 | 401 | `credential_revoked` |
| 凭据有效但缺少该路由权限域 | 403 | `forbidden` |

- **密钥轮换**：新旧凭据按各自的 `valid_from / valid_until` 有效期并存。
  种子数据里 `rot-old-secret` 与 `rot-new-secret` 有 1 小时重叠窗口；
  窗口内两者均可用，旧密钥过期后新密钥继续有效。

### 种子密钥（本地验证用，基准时间 2026-09-25 12:00 UTC）

| 密钥 | key_id | 说明 |
|------|--------|------|
| `test-key-active` | acct-active | 读写权限，配额：list/detail 各 100，create 5/小时 |
| `rot-old-secret` | acct-rot-old | 轮换旧密钥，基准时间后 2 小时过期 |
| `rot-new-secret` | acct-rot-new | 轮换新密钥，基准时间前 1 小时已生效 |
| `test-key-expired` | acct-expired | 已过期 |
| `test-key-revoked` | acct-revoked | 已撤销 |
| `test-key-readonly` | acct-readonly | 只有读权限域 |

## 3. 配额规则

- 粒度：**每凭据 × 每路由**，固定窗口（默认 3600 秒），互不影响；
- 扣减是锁内原子操作：并发请求不会超发，被拒请求不计数；
- **按量计费**：单条写 `POST /widgets` 每次扣 1；批量写 `POST /widgets/batch`
  按本批**实际写入的条数**扣（`items` 的长度），两者落在同一个
  `widgets:create` 桶里统一核算；
- **整批全有或全无**：剩余额度（或共享容量）不够覆盖整批时，整批拒绝——
  不会只写一部分，也不会扣一半再退一半；被拒的请求不占用任何额度；
- **不多扣**：
  - 版本/认证/权限阶段被拒的请求根本不进入配额阶段；
  - 写路由先判幂等：**重放与同键冲突在获取配额之前就返回，完全不碰配额**；
  - 已通过配额但业务处理失败（如 422）会**把本批占用的额度全部退回**并释放幂等占位；
- **不漏扣**：只有真正首次执行副作用的请求扣费；
- 超限时返回 `429 quota_exceeded`，并给出可重试信息：
  `Retry-After` 头 + 体中 `retry_after_seconds / reset_at / used / limit`；
- 允许的请求带 `X-RateLimit-Limit` / `X-RateLimit-Remaining`。

### 3.1 共享容量上限（每路由，所有调用方共用）

- 每条路由另有一个**所有调用方共享的总容量上限**（固定窗口，与配额窗口同长），
  用来兜底保护后端；种子值：list/detail 各 1000，create 50；
- 获取同样是锁内原子、按量全有或全无：**多个不同调用方并发挤同一条路由，
  合计也严格不超过上限**；
- 准入顺序上共享容量在自有配额之后：容量不足被拒时，刚拿到的自有配额会立即回退，
  请求不留下任何占用；
- 两种 429 可明确区分，各自带可重试时间：

| 情况 | error.code | 含义 | 重试依据 |
|------|------------|------|----------|
| 自有额度不够 | `quota_exceeded` | 只有当前调用方超限 | `Retry-After` 头 + `retry_after_seconds/reset_at` |
| 路由整体满了 | `capacity_exceeded` | 所有调用方合计超限 | `Retry-After` 头 + `retry_after_seconds/reset_at`，体中带 `shared_used/shared_limit` |

准入顺序（写路由）：
`版本 → 凭据 → 权限 → 幂等判定(重放/冲突短路) → 自有配额(仅首写者) → 共享容量 → 副作用`。

### 3.2 批量写入 `POST /widgets/batch`

- 请求体：`{"items": [{"name": "a"}, {"name": "b"}, ...]}`，必须带
  `Idempotency-Key` 头；单批上限 100 条；
- 计费：按 `items` 条数从 `widgets:create` 桶扣减（自有配额与共享容量同口径）；
- 任一条记录非法（如缺 `name`）→ 整批 `422`，**一条都不写**，已占额度全部退回；
- 幂等语义与单条写一致：同键同载荷重放返回首次结果、不重复扣费、不新增记录；
  同键不同载荷 `409 idempotency_conflict`。

### 3.3 用量查询 `GET /usage`

- 调用方自助查看：每路由**自己**当前窗口的 `used / limit / remaining / reset_at`，
  以及该路由的**共享容量水位** `shared.{used,limit,remaining,reset_at}`；
- **只读**：不消耗自有配额、不占用共享容量、不改动任何窗口状态；
- 只需有效凭据（不要求特定路由权限域，只读凭据也可查询）；
- 版本/凭据判定与其它路由完全一致：未指定版本走默认 `v2`，
  `v0 → 410`、`v9 → 400`、无密钥 → `401`。

## 4. 幂等语义

- 写请求 `POST /widgets` 必须带 **`Idempotency-Key`** 头，否则 `400 bad_request`；
- 请求载荷经规范化 JSON 的 SHA-256 作为指纹，与键、调用方、路由一起绑定；
- **首次提交**：执行唯一一次副作用，返回 `201`；
- **同键同载荷重放**（含并发重复）：直接返回首次的状态码与响应体，
  响应带 `Idempotent-Replay: true` 且体中 `idempotent_replay: true`，
  **不新增 widget、不扣配额**；
- **同键不同载荷 / 不同调用方 / 不同路由**：返回 `409 idempotency_conflict`，
  首次结果不被覆盖；
- 首写者并发重复时，重复请求等待首写完成后拿到同一份结果（首写者语义）；
- 首写者在完成前失败会释放占位并回滚配额，调用方可安全用同一键重试。

## 5. 审计

每次判定（allow / reject / replay）都产生一条结构化记录：
`event_id, ts, key_id, route, requested_version, resolved_version,
decision, reason, quota_before, quota_after, detail`。
- 通过 `key_id` 关联调用方，但**绝不记录明文密钥**（`detail` 中敏感键名自动脱敏，
  测试用 `audit.assert_no_secret(...)` 断言明文不泄漏）；
- 拒绝与放行原因一一对应到上述错误码，便于追溯。

## 6. 本地验证

```bash
# 安装依赖（需要 Python 3.14）
uv sync

# 运行全部单测（含并发配额/共享容量竞争、批量整批拒绝与回退、用量查询、
# 凭据轮换/过期、版本边界、幂等重放/冲突）
uv run pytest                      # 终端会打印每个用例的关键输入与判定依据

# 启动服务
uv run uvicorn main:app --reload --port 8000
```

手工验证示例：

```bash
# 版本策略
curl -s localhost:8000/versions | python3 -m json.tool

# 默认版本 (v2)
curl -s -H 'X-Api-Key: test-key-active' localhost:8000/widgets

# 弃用版本（观察 Deprecation/Sunset/X-Successor-Version 头）
curl -si -H 'X-Api-Key: test-key-active' -H 'X-Api-Version: v1' \
  localhost:8000/widgets | grep -iE 'HTTP/|deprecation|sunset|successor'

# 已下线 / 未知版本
curl -s -H 'X-Api-Key: test-key-active' -H 'X-Api-Version: v0' localhost:8000/widgets
curl -s -H 'X-Api-Key: test-key-active' -H 'X-Api-Version: v9' localhost:8000/widgets

# 幂等写：第一次 201，第二次重放（同样 201 且 idempotent_replay=true，只建一个 widget）
curl -s -XPOST -H 'X-Api-Key: test-key-active' -H 'Idempotency-Key: demo-1' \
  -H 'Content-Type: application/json' -d '{"name":"demo"}' localhost:8000/widgets
curl -s -XPOST -H 'X-Api-Key: test-key-active' -H 'Idempotency-Key: demo-1' \
  -H 'Content-Type: application/json' -d '{"name":"demo"}' localhost:8000/widgets

# 同键不同载荷 -> 409 idempotency_conflict
curl -s -XPOST -H 'X-Api-Key: test-key-active' -H 'Idempotency-Key: demo-1' \
  -H 'Content-Type: application/json' -d '{"name":"other"}' localhost:8000/widgets

# 并发压测配额（create 限额 5）：恰好 5 个 201，其余 429
seq 60 | xargs -P60 -I{} curl -s -o /dev/null -w '%{http_code}\n' -XPOST \
  -H 'X-Api-Key: test-key-active' -H 'Idempotency-Key: c-{}' \
  -H 'Content-Type: application/json' -d '{"name":"n{}"}' localhost:8000/widgets \
  | sort | uniq -c

# 批量写：一次 3 条扣 3 份；重放同键不重复扣费
curl -s -XPOST -H 'X-Api-Key: test-key-active' -H 'Idempotency-Key: batch-1' \
  -H 'Content-Type: application/json' \
  -d '{"items":[{"name":"a"},{"name":"b"},{"name":"c"}]}' \
  localhost:8000/widgets/batch
curl -s -XPOST -H 'X-Api-Key: test-key-active' -H 'Idempotency-Key: batch-1' \
  -H 'Content-Type: application/json' \
  -d '{"items":[{"name":"a"},{"name":"b"},{"name":"c"}]}' \
  localhost:8000/widgets/batch   # idempotent_replay=true

# 剩余 2 份时提交 3 条 -> 整批 429 quota_exceeded，一条不写、一份不扣
curl -s -XPOST -H 'X-Api-Key: test-key-active' -H 'Idempotency-Key: batch-2' \
  -H 'Content-Type: application/json' \
  -d '{"items":[{"name":"d"},{"name":"e"},{"name":"f"}]}' \
  localhost:8000/widgets/batch

# 用量查询（只读，查多少次都不扣额度）：自有用量 + 共享水位
curl -s -H 'X-Api-Key: test-key-active' localhost:8000/usage | python3 -m json.tool
```

## 7. 代码结构

```
gateway/
  models.py       领域类型（版本状态、凭据、配额/幂等/审计记录、错误码枚举）
  clock.py        可注入时钟（测试用 FixedClock 控制时间）
  errors.py       网关异常与统一错误响应体
  versioning.py   版本注册表与协商
  credentials.py  凭据哈希、有效期/状态校验、权限域
  quota.py        原子固定窗口配额（按量 try_acquire / rollback / 只读 peek）
  capacity.py     每路由共享容量上限（全调用方合计，原子、可回退、可只读查询）
  idempotency.py  幂等存储（首写者语义、重放、冲突、占位释放）
  audit.py        追加式审计与密钥脱敏
  admission.py    准入流水线（读：全阶段；写：pre-quota + 配额两阶段；用量：轻量）
  routes.py       /widgets 读写、/widgets/batch 批量写、/usage 用量查询与错误处理
  store.py        种子凭据、默认配额与共享容量、业务数据
  deps.py         服务装配
  app.py          FastAPI 工厂 + /versions
tests/            版本/凭据/配额(并发)/容量(并发)/批量/用量/幂等(并发) 测试
```

所有状态保存在内存中，重启即回到种子数据，适合本地与测试验证。
