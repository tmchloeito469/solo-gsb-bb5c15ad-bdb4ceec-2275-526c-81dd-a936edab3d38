# 一次性内容投递箱 API（One-Time Drop Box）

发送者创建带过期时间的 **文本** 或 **小文件** 投递，得到一个取件令牌；
收件人凭令牌取件，**内容只能被成功读取一次**。并发取件时仅有一个请求得到内容，
其余请求以及「已撤回 / 已过期 / 已消费」都会得到明确、可区分的结果。

此外支持 **盲投递请求**：由 *收件人* 先创建一个空投递槽，得到 **一次性提交令牌**
（交给提交者）与 **私有取件令牌**（自己留存）。提交令牌只能成功写入一份内容，
取件令牌在内容提交前返回 `pending`，提交后同样只能成功读取一次。

- **FastAPI**（异步 HTTP + 自动 OpenAPI 文档）
- **PostgreSQL**：投递内容与审计的唯一可信存储，**原子消费的最终裁决者**
- **Redis**：终态快速拒绝缓存（best-effort，宕机不影响正确性）
- 令牌只存 SHA-256 哈希；明文令牌仅在创建响应中出现一次

---

## 一条命令启动

需要 Docker（含 Compose 插件）：

```bash
docker compose up --build
```

启动后：

| 服务 | 地址 |
| --- | --- |
| **API（对外端口）** | **http://localhost:8000** |
| Swagger 交互文档 | http://localhost:8000/docs |
| 健康检查 | http://localhost:8000/health |
| PostgreSQL | 仅集群内部 `db:5432`（不对宿主机暴露） |
| Redis | 仅集群内部 `redis:6379`（不对宿主机暴露） |

停止并清理数据卷：

```bash
docker compose down -v
```

> API 对外只暴露 **8000** 一个端口。数据库与 Redis 不映射到宿主机。

---

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/deliveries` | 创建文本投递（JSON） |
| `POST` | `/api/v1/deliveries/file` | 创建小文件投递（multipart/form-data） |
| `POST` | `/api/v1/pickup/{token}` | 凭令牌取件（**只能成功一次**） |
| `POST` | `/api/v1/revoke/{token}` | 发送者撤回投递 |
| `POST` | `/api/v1/drop-requests` | 收件人创建盲投递空槽（返回提交/取件两类令牌） |
| `POST` | `/api/v1/drop-requests/submit/{submit_token}` | 提交者写入文本（**仅一个提交者成功**） |
| `POST` | `/api/v1/drop-requests/submit-file/{submit_token}` | 提交者写入小文件（multipart/form-data） |
| `POST` | `/api/v1/drop-requests/pickup/{pickup_token}` | 收件人取件（未提交返回 `pending`） |
| `POST` | `/api/v1/drop-requests/revoke/{pickup_token}` | 收件人撤回盲投递请求 |
| `GET` | `/api/v1/audit/export` | 按时间范围导出审计链记录（**NDJSON 流**，只读） |
| `GET` | `/api/v1/audit/verify` | 校验库内审计链完整性（只读） |
| `POST` | `/api/v1/audit/verify` | 校验一份导出的 NDJSON（按行顺序，可发现乱序） |
| `GET` | `/health` | 健康检查 |

### 取件/撤回的明确结果

取件失败时返回统一结构 `{"result": <状态>, "detail": <中文说明>}`，状态与状态码如下：

| `result` | 含义 | HTTP 状态码 |
| --- | --- | --- |
| `success` | 取件成功（文本返回 JSON，文件返回二进制流） | `200` |
| `consumed` | 令牌已被成功取件，内容已不可再读 | `410 Gone` |
| `expired` | 投递已过期 | `410 Gone` |
| `revoked` | 投递已被发送者撤回（取件）/ 无法撤回（已被取走见下） | 取件 `409 Conflict` |
| `unknown` | 令牌不存在 | `404 Not Found` |

撤回接口：成功 `200 {"result":"revoked"}`；已被取走或已过期返回 `410`
（`result` 为 `consumed` / `expired`）；令牌不存在 `404`。

---

## 使用示例

### 1) 创建文本投递

```bash
curl -s -X POST http://localhost:8000/api/v1/deliveries \
  -H 'Content-Type: application/json' \
  -d '{"text": "这是读一次就消失的秘密", "ttl_seconds": 3600}'
```

响应（`token` 仅此一次返回，请立即保存）：

```json
{
  "delivery_id": "8f1c…-…-…",
  "token": "U9rX…取件令牌…",
  "expires_at": "2026-09-21T10:00:00Z",
  "url": "http://localhost:8000/api/v1/pickup/U9rX…"
}
```

### 2) 取件（第一次成功）

```bash
curl -s -X POST "http://localhost:8000/api/v1/pickup/U9rX…"
```

```json
{
  "result": "success",
  "delivery_id": "8f1c…",
  "kind": "text",
  "content": "这是读一次就消失的秘密",
  "content_sha256": "…",
  "consumed_at": "2026-09-21T09:05:11Z"
}
```

再取一次：

```bash
curl -s -X POST "http://localhost:8000/api/v1/pickup/U9rX…" -i
# HTTP/1.1 410 Gone
# {"result":"consumed","detail":"该令牌已被成功取件，内容仅可读取一次。"}
```

### 3) 创建小文件并取件

```bash
# 创建（默认上限 1 MiB）
curl -s -X POST http://localhost:8000/api/v1/deliveries/file \
  -F 'file=@/path/to/report.pdf' \
  -F 'ttl_seconds=600'

# 取件（成功返回二进制流，响应头带校验值）
curl -X POST "http://localhost:8000/api/v1/pickup/<token>" \
  -D - -o downloaded.pdf
# X-Drop-Result: success
# X-Content-SHA256: <sha256>
# Content-Disposition: attachment; filename="report.pdf"; …
```

### 4) 撤回

```bash
curl -s -X POST "http://localhost:8000/api/v1/revoke/<token>"
# 成功：   200 {"result":"revoked", ...}
# 已取走： 410 {"result":"consumed", ...}
# 已过期： 410 {"result":"expired", ...}
```

撤回后再取件得到 `409 {"result":"revoked"}`。

---

## 盲投递请求（收件人发起的空槽）

普通投递是「发送者创建、收件人取件」；盲投递请求则相反：**收件人先创建一个
空投递槽**，把一次性提交令牌交给提交者（可公开张贴，谁先提交谁得），自己
留存私有取件令牌等待内容到达。

### 流程与状态机

```
收件人 POST /api/v1/drop-requests
   → 得到 submit_token（交给提交者）与 pickup_token（收件人自留）

awaiting ──提交者写入──▶ submitted ──收件人取件──▶ consumed
   │                       │
   └──收件人撤回──▶ revoked ◀──收件人撤回──┘
   └──到期──▶ expired ◀──到期──┘
```

- 两类令牌**分别哈希存储**在 `submit_token_hash` / `pickup_token_hash` 两个
  独立列：提交接口只按提交令牌查找，取件/撤回接口只按取件令牌查找，
  **任何接口不得互换权限**（拿提交令牌去取件只会得到 `404 unknown`）；
- 提交是数据库条件更新裁决的**一次性写入**：并发提交、收件人撤回与过期
  三者竞争时只有一个提交者成功，失败方得到明确的 `submitted` / `revoked` /
  `expired`；
- 取件在内容提交前返回 `202 pending`（非终态，可稍后重试），提交后沿用
  与普通投递完全相同的原子单次取件。

### 提交接口的明确结果

| `result` | 含义 | HTTP 状态码 |
| --- | --- | --- |
| `success` | 本次提交被接受（唯一赢家） | `201` |
| `submitted` | 槽内已有一份成功提交，提交令牌已失效 | `409 Conflict` |
| `revoked` | 收件人已撤回该请求 | `409 Conflict` |
| `expired` | 请求已过期 | `410 Gone` |
| `unknown` | 提交令牌不存在 | `404 Not Found` |

### 取件接口的明确结果

| `result` | 含义 | HTTP 状态码 |
| --- | --- | --- |
| `pending` | 内容尚未提交，可稍后重试 | `202 Accepted` |
| `success` | 取件成功（文本 JSON / 文件二进制流），仅一次 | `200` |
| `consumed` | 内容已被取走 | `410 Gone` |
| `revoked` | 请求已被撤回 | `409 Conflict` |
| `expired` | 请求已过期 | `410 Gone` |
| `unknown` | 取件令牌不存在 | `404 Not Found` |

撤回接口：成功 `200 {"result":"revoked"}`（重复撤回幂等返回
`already_revoked`）；已被取走 `410 consumed`；已过期 `410 expired`；
令牌不存在 `404 unknown`。

### 调用示例

```bash
# 1) 收件人创建空槽（两个明文令牌仅此一次返回）
curl -s -X POST http://localhost:8000/api/v1/drop-requests \
  -H 'Content-Type: application/json' \
  -d '{"ttl_seconds": 3600}'
```

```json
{
  "request_id": "3fa8…-…-…",
  "submit_token": "kQ2w…提交令牌，交给提交者…",
  "pickup_token": "Zz9x…取件令牌，收件人自留…",
  "expires_at": "2026-09-26T11:00:00Z",
  "submit_url": "http://localhost:8000/api/v1/drop-requests/submit/kQ2w…",
  "pickup_url": "http://localhost:8000/api/v1/drop-requests/pickup/Zz9x…"
}
```

```bash
# 2) 收件人轮询：内容未提交前返回 pending
curl -s -X POST "http://localhost:8000/api/v1/drop-requests/pickup/Zz9x…" -i
# HTTP/1.1 202 Accepted
# {"result":"pending","detail":"内容尚未提交，请稍后重试。"}

# 3) 提交者写入文本（或改用 submit-file 上传小文件）
curl -s -X POST "http://localhost:8000/api/v1/drop-requests/submit/kQ2w…" \
  -H 'Content-Type: application/json' \
  -d '{"text": "这是提交者写入的内容"}'
# 201 {"result":"success","request_id":"3fa8…","submitted_at":"…"}

curl -s -X POST "http://localhost:8000/api/v1/drop-requests/submit-file/kQ2w…" \
  -F 'file=@/path/to/evidence.zip'

# 4) 收件人取件（成功仅一次）
curl -s -X POST "http://localhost:8000/api/v1/drop-requests/pickup/Zz9x…"
# {"result":"success","request_id":"3fa8…","kind":"text",
#  "content":"这是提交者写入的内容","content_sha256":"…","consumed_at":"…"}

# 5) 收件人撤回（提交前或提交后未取件均可）
curl -s -X POST "http://localhost:8000/api/v1/drop-requests/revoke/Zz9x…"
# 200 {"result":"revoked", ...}
```

---

## 并发原子消费是如何保证的

这是本服务的核心。正确性**只依赖 PostgreSQL 的单条条件更新**，不依赖应用层锁：

```sql
UPDATE deliveries
SET status = 'consumed', consumed_at = now
WHERE token_hash = :token_hash
  AND status = 'active'
  AND expires_at > now
RETURNING id, content, …;
```

- 多个并发请求执行同一条语句时，PostgreSQL 用**行锁**把对该行的更新串行化；
- 只有**第一个**事务满足 `status='active' AND 未过期`，它把状态改成 `consumed`
  并通过 `RETURNING` 拿到内容（唯一赢家）；
- 其余事务的 `WHERE` 不再匹配，返回 0 行，随后在新事务里读取当前终态，
  得到明确的 `consumed` / `revoked` / `expired`；
- 未命中的事务**不会在旧快照里回写任何状态**，因此不可能覆盖赢家刚提交的终态；
- 内容仅在赢家事务中随 `RETURNING` 取出一次，响应后不落缓存。

**Redis 的角色（非正确性依赖）**：只缓存终态标记（`consumed` / `revoked` /
`expired`），用于对已知失效令牌快速拒绝、减轻数据库压力；**绝不缓存明文内容**，
采用 Lua「仅当不存在时设置」保证终态不被互相覆盖，并设置 TTL。Redis 不可用时
请求自动回落到数据库判定，原子消费依旧成立。

**过期**：取件/撤回时惰性提升 + 后台协程每 30 秒批量兜底标记过期。

### 撤回与取件竞争

撤回使用同样的单条条件更新（目标状态 `revoked`）。它与取件互斥：
要么内容被取走（撤回得到 `consumed`），要么撤回先成功（取件得到 `revoked`），
不会出现“既取走又撤回”。

盲投递请求的提交/取件/撤回也走同一模式，只是 `WHERE` 条件分别落在
`submit_token_hash` / `pickup_token_hash` 列上：

```sql
-- 提交：只有空槽（awaiting）且未过期才能被写入，并发下只有一个提交者命中
UPDATE drop_requests
SET status = 'submitted', content = :content, …, submitted_at = now
WHERE submit_token_hash = :submit_token_hash
  AND status = 'awaiting'
  AND expires_at > now
RETURNING id, submitted_at;

-- 取件：只有已提交（submitted）且未过期才能被消费
UPDATE drop_requests
SET status = 'consumed', consumed_at = now
WHERE pickup_token_hash = :pickup_token_hash
  AND status = 'submitted'
  AND expires_at > now
RETURNING id, content, …;
```

撤回与过期针对 `awaiting` / `submitted` 两个活状态，因此「提交 vs 撤回」
并发时：要么提交先成功、撤回随后拒收已提交内容（两者都成功，取件得到
`revoked`），要么撤回先成功、提交得到 `409 revoked`——绝不出现
“既提交成功又被静默撤回后还能取到内容”的中间态。

---

## 审计与可验证完整性（哈希链）

所有关键动作写入 `audit_logs` 表，并且**每一条新日志（包括未知令牌等失败
请求）都在同一个 PostgreSQL 事务中加入一条只增哈希链**：

- 动作：`create` / `pickup` / `revoke`（普通投递），
  `drop_create` / `drop_submit` / `drop_pickup` / `drop_revoke`（盲投递请求）
- 结果：`success` / `consumed` / `revoked` / `expired` / `unknown`，
  以及盲投递特有的 `submitted`（重复提交被拒）/ `pending`（空槽取件）
- 关联 `delivery_id`（可空）、`token_hash`、客户端 IP、User-Agent、时间与少量详情；
  盲投递请求的 `request_id` 记录在 `detail` 中（`delivery_id` 仅关联普通投递）
- **未知令牌的尝试也留痕并入链**（`delivery_id` 为空、仅记录哈希），便于暴力探测审计

明文内容不写入审计，也不会出现在任何导出/校验数据中。

### 每条记录的链字段

| 字段 | 含义 |
| --- | --- |
| `seq` | 全链单调递增序号（从 1 开始，唯一约束兜底） |
| `event_digest` | 规范化事件摘要的 SHA-256 |
| `prev_hash` | 前一条记录的链哈希；序号 1 的 `prev_hash` 为固定创世哈希 |
| `entry_hash` | 本条链哈希：`SHA256(prev_hash ‖ "|" ‖ event_digest)` |

**规范化事件摘要**对业务字段（格式版本、序号、UTC 微秒时间、动作、结果、
`delivery_id`、`token_hash`、IP、UA、详情）做确定性序列化：UTF-8、键排序、
紧凑 JSON，再取 SHA-256。因此任一字段被改写，摘要都会失配。

> 链只能阻止“非重算式”篡改。拥有数据库写权限的攻击者若从某点起重算全部
> 后续哈希，表面校验仍可通过——因此**导出接口返回的 `X-Chain-End-Hash`
> 应由外部独立留存**（如每次导出后另行存档），作为日后校验的信任锚。

### 并发写入：链序由数据库裁决

链游标是 `audit_chain_state` 表中的单行 `(last_seq, last_hash)`。追加一条
审计时，在请求所在事务内：

1. 读取当前游标，计算新序号/摘要/链哈希；
2. **先**执行条件更新抢占游标：
   `UPDATE audit_chain_state SET last_seq=new, last_hash=… WHERE id=1 AND last_seq=:read`；
3. 仅当更新命中（`rowcount=1`）才插入审计行并提交；未命中说明他人已推进
   游标，本事务没有产生任何写入，重读游标后重试。

这与原子消费使用完全相同的裁决模式：PostgreSQL（READ COMMITTED）用行锁
把对游标单行的更新串行化，竞争事务的 `WHERE` 在锁释放后按最新行版本重新
判定，只有一个事务命中。链的全序因此由数据库裁决，不依赖应用层锁。

### 导出 NDJSON（只读）

```bash
# 全量导出（左闭右开时间范围，两个参数均可省略）
curl -s -D headers.txt -o audit.ndjson \
  "http://localhost:8000/api/v1/audit/export?start=2026-09-21T00:00:00Z&end=2026-09-22T00:00:00Z"

cat headers.txt | grep -i x-chain
# X-Chain-Genesis-Hash: <64 位十六进制>
# X-Chain-Start-Hash:   <范围内第一条记录的链哈希，空范围为空>
# X-Chain-End-Hash:     <范围内最后一条记录的链哈希，空范围为空>
# X-Chain-Record-Count: <记录数>
```

- 每行一条记录（`application/x-ndjson`），字段仅为审计元数据 + 链哈希，
  **不含投递明文内容**；
- 计数、起止哈希与流内容在同一个 **READ ONLY + REPEATABLE READ** 事务中
  取得，共享同一快照（驱动层拒绝任何写操作）；
- 建议每次导出后把响应头中的 `X-Chain-End-Hash` 与记录数独立存档。

导出的 NDJSON 长这样（一行）：

```json
{"action":"create","client_ip":null,"created_at":"2026-09-21T09:05:11.123456Z","delivery_id":"8f1c…","detail":{"kind":"text","size":30,"ttl_seconds":3600},"entry_hash":"…","event_digest":"…","prev_hash":"…","result":"success","seq":1,"token_hash":"…","user_agent":null}
```

### 校验：五类问题，指出第一处

```bash
# 校验库内全链（GET 版按库内 seq 顺序；只读，不产生新审计记录）
curl -s http://localhost:8000/api/v1/audit/verify
# {"ok":true,"count":12,"first_seq":1,"last_seq":12,"start_hash":"…","end_hash":"…",
#  "error":null,"seq":null,"position":null,"detail":null}

# 校验一份导出文件（POST 版严格按上传行顺序，可发现乱序）
curl -s -X POST --data-binary @audit.ndjson \
  -H 'Content-Type: application/x-ndjson' \
  http://localhost:8000/api/v1/audit/verify
```

`ok=false` 时按检查顺序返回**第一处**错误，`error` 取值：

| `error` | 含义 | `seq` / `position` |
| --- | --- | --- |
| `duplicate` | 同一序号重复出现 | 重复序号 / 所在行 |
| `out_of_order` | 记录乱序（序号回退或提前） | 越界序号 / 所在行 |
| `missing` | 缺行（序号跳号且期望序号未在后续出现） | 缺失序号 / 发现位置 |
| `field_tampered` | 规范化摘要不匹配，字段被改写 | 被改记录序号 / 所在行 |
| `chain_break` | 链哈希或前指针不匹配 | 断裂处序号 / 所在行 |

- `GET` 全链校验从创世哈希锚定；带时间范围时自动以库中该片段首条的前一条
  记录作为锚点（片段之前缺行不在该范围校验内，请用全链校验获得权威结论）。
- `POST` 校验以序号 1 开头时自动锚定创世哈希；校验任意片段时可通过
  `?anchor_hash=<前一条链哈希>` 显式提供信任锚（通常取自导出响应头）。
- 上传内容仅用于重算校验，**不会入库**；请求体上限 10 MiB。

### 数据模型要点

- 令牌为 32 字节 `secrets.token_urlsafe(32)`，库中仅存其 **SHA-256**；
- 内容存 PostgreSQL `bytea`，同时记录大小与 SHA-256；
- 状态机：`active → consumed | revoked | expired`，单向不可逆；
- 盲投递请求（`drop_requests` 表）：`awaiting → submitted → consumed`，
  活状态（`awaiting`/`submitted`）可被撤回或过期裁决为 `revoked`/`expired`；
  提交/取件两类令牌哈希分别存于 `submit_token_hash` / `pickup_token_hash`
  两个唯一列，提交前内容字段全部为空（空槽）；
- `(status, expires_at)` 与令牌哈希建有索引，支撑并发判定与清扫；
- `audit_logs` 额外保存 `seq / event_digest / prev_hash / entry_hash`
  （链字段），链游标存于单行表 `audit_chain_state`，表结构在启动时
  自动创建并种子化创世游标。

---

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://dropbox:dropbox@db:5432/dropbox` | 异步数据库 DSN |
| `REDIS_URL` | `redis://redis:6379/0` | Redis 地址 |
| `MAX_CONTENT_BYTES` | `1048576`（1 MiB） | 单条文本/文件大小上限 |
| `TTL_MIN_SECONDS` | `60` | 允许的最短有效期 |
| `TTL_MAX_SECONDS` | `604800`（7 天） | 允许的最长有效期 |
| `REDIS_FLAG_TTL_SECONDS` | `2592000`（30 天） | Redis 终态标记保留时长 |
| `SWEEP_INTERVAL_SECONDS` | `30` | 后台过期清扫间隔 |

本地裸跑（不用 Docker）可复制 `.env.example` 为 `.env` 后自行准备 PG/Redis，
再 `uvicorn app.main:app --reload`。

---

## 本地开发与测试

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

测试套件使用文件型 SQLite（WAL，多连接真实争抢写锁）+ `fakeredis`，
包含 **50 路并发取件仅有一个赢家**、**取件/撤回并发互斥**、撤回、过期、
文件一次性下载、未知令牌与审计留痕，以及**哈希链入链/导出 NDJSON/五类
篡改检测（缺行、乱序、重复、字段改写、链断裂并指出第一处）/40 路并发
写入序号严格连续/链游标竞态失配重试**等用例。盲投递请求覆盖：**50 路并发
提交仅有一个赢家**、提交/撤回并发终态一致、提交前取件返回 `pending`、
两类令牌权限互不通用、撤回幂等、过期槽提交/取件/撤回均为 `expired`、
文件提交与一次性下载、四类动作全部入链且链校验通过。另在真实
PostgreSQL 上验证过 100 路并发取件恰好 1 个赢家、30 组取件/撤回互斥。

---

## 安全说明与边界

- 取件令牌是唯一的“能力凭证”：知道令牌即可取件，因此应通过私密渠道发送；
  库中不存明文令牌，泄露数据库无法反推取件地址。
- 默认内容上限 1 MiB，面向“文本或小文件”；大文件投递不在设计范围内。
- 本服务不含发送者身份认证/速率限制，生产部署建议放在鉴权网关之后并自行限流。
- 表结构在启动时自动 `CREATE TABLE`（简单场景不引入迁移工具）。
