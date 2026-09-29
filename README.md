# 样本交接链核验服务（Chain of Custody）

为环境检测机构提供的样本保管链登记、核验与核定服务。技术栈：**Python 3.11 + FastAPI + SQLite**，容器化一键启动。

## 一、它解决什么问题

样本从采集到实验室入库经过多人交接，需要回答三个问题：

1. **证据链**：按时间排列的移交、签收、封签检查、入库记录；
2. **当前保管人**：此刻样本依法应由谁持有；
3. **结论**：链条是 `valid`（有效）/ `pending`（待补证）/ `invalid`（无效），以及原因。

## 二、启动

### 方式 A：Docker Compose（推荐，一键）

```bash
docker compose up -d --build
```

首次启动自动完成：安装依赖 → 建库建表 → 写入 5 个演示样本（覆盖有效/待补证/无效场景）。
数据保存在命名卷 `custody-data` 中，容器重建不丢数据。

查看状态：

```bash
docker compose ps
docker compose logs -f
```

停止 / 清空数据重来：

```bash
docker compose down          # 停止
docker compose down -v       # 停止并删除数据卷（恢复初始演示数据）
```

### 方式 B：本地 Python 运行（不使用 Docker）

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### 地址与端口

| 项目 | 地址 |
| --- | --- |
| API 根地址 | http://localhost:8080 |
| 健康检查 | http://localhost:8080/health |
| 交互式文档（Swagger） | http://localhost:8080/docs |
| OpenAPI 描述 | http://localhost:8080/openapi.json |

## 三、配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `COC_HOST` | `0.0.0.0` | 监听地址 |
| `COC_PORT` | `8080` | 监听端口（compose 中端口映射需同步修改） |
| `COC_DB_PATH` | `./data/custody.db`（容器内 `/data/custody.db`） | SQLite 数据库文件路径 |
| `COC_SEED_DEMO` | `1` | 空库启动时是否写入演示数据，设为 `0` 得空库 |

## 四、HTTP API

所有接口前缀 `/api`，请求/响应均为 `application/json; charset=utf-8`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/samples` | 样本登记（采集时间/采集人/封签号） |
| GET | `/api/samples` | 样本清单与结论摘要 |
| GET | `/api/samples/{sample_code}` | **证据链 + 当前保管人 + 结论与原因** |
| GET | `/api/samples/{sample_code}/events` | 原始事件（按发生时间排列） |
| POST | `/api/events` | 接收事件：移交/签收/封签检查/入库（统一入口） |
| GET | `/api/events/{event_no}` | 按唯一事件号查询 |
| POST | `/api/samples/{sample_code}/approve` | 核定入库（仅有效链；核定后历史锁定） |

### 事件类型与必填字段

`POST /api/events` 按 `event_type` 区分四类记录，公共必填 `seal_number`：

| event_type | 含义 | 类型专有必填字段 |
| --- | --- | --- |
| `transfer` | 移交 | `from_custodian`、`to_custodian` |
| `receipt` | 签收 | `transfer_no`（对应移交事件号）、`receiver` |
| `seal_check` | 封签检查 | `checker`、`seal_result`（`intact`/`damaged`） |
| `storage` | 实验室入库 | `lab` |

所有事件均带全局唯一 `event_no` 与 `occurred_at`（**发生时间**，不是录入时间）。

### 状态码约定

| 状态码 | 含义 |
| --- | --- |
| 201 | 创建成功 |
| 200 | 同内容重试命中幂等 |
| 404 | 样本/事件不存在 |
| 409 | 异内容冲突、核定后改写历史 |
| 422 | 参数缺失，或待补证/无效链申请核定 |

错误体统一为：`{"error": {"code": "...", "message": "...", "details": ...}}`。

## 五、核验规则（业务语义）

**移交（transfer）**

- 必须由**当前保管人**发起，否则判 `CHAIN_BROKEN_TRANSFER_CUSTODIAN`（保管人断链）；
- 移交后、签收前**不得再次移交**，否则判 `CHAIN_BROKEN_DOUBLE_TRANSFER`。

**签收（receipt）**

- 必须按 `transfer_no` 匹配到一笔合法且未闭合的移交；
- `receiver` 必须等于移交单的接收人，封签号必须与登记封签一致；
- `occurred_at` 必须**严格晚于**移交发生时间；
- 同一移交单被第二次有效签收判 `DUPLICATE_RECEIPT`（重复签收）。

**封签检查（seal_check）与入库（storage）**

- 任一事件的封签号与登记不符 → `SEAL_MISMATCH`；
- 入库不转移保管权，只标记“已入库”事实。

**乱序补录**

- 核验一律按 `occurred_at` 重放（而非录入先后）。先录入入库、后补录移交/签收，
  查询时仍按发生时间得到正确链条；每接收一条事件都即时重算。

**幂等与冲突**

- 同一 `event_no` + 同内容（除事件号外全部业务字段的 SHA-256 一致）→ 200 幂等返回；
- 同一 `event_no` + 异内容 → 409 `EVENT_CONFLICT`；样本登记同理。

**结论（无效优先）**

- `invalid`：存在封签不符 / 保管人断链 / 重复签收等任何无效事实——**即使同时缺签收或缺入库，也优先判无效**；
- `pending`：无无效事实，但缺有效签收（`NO_RECEIPT`）、有移交未签收（`TRANSFER_OPEN`）或缺入库（`NO_STORAGE`）；
- `valid`：无无效事实，且至少一笔有效签收 + 一笔入库，当前无在途移交。

**核定入库**

- 仅 `valid` 链可调用 `approve`，核定后保存结论快照；
- 核定后：新增/异内容事件一律 409 `HISTORY_LOCKED`，同内容重试仍幂等；核定接口本身幂等。

## 六、调用示例

完整可运行示例见 [`examples/`](examples/) 目录：

- [`examples/curl-examples.sh`](examples/curl-examples.sh) —— curl 全流程
- [`examples/python-client.py`](examples/python-client.py) —— Python（仅标准库）

### curl 快速上手

```bash
# 1) 登记样本
curl -s -X POST http://localhost:8080/api/samples \
  -H 'Content-Type: application/json' \
  -d '{"sample_code":"S-001","collected_at":"2026-09-29T08:00:00Z",
       "collector":"张采集","seal_number":"SEAL-001"}'

# 2) 移交（当前保管人=张采集 发起）
curl -s -X POST http://localhost:8080/api/events \
  -H 'Content-Type: application/json' \
  -d '{"event_no":"EV-T1","sample_code":"S-001","event_type":"transfer",
       "occurred_at":"2026-09-29T09:00:00Z",
       "from_custodian":"张采集","to_custodian":"钱运输","seal_number":"SEAL-001"}'

# 3) 签收（匹配移交单号/接收人/封签号，且时间晚于移交）
curl -s -X POST http://localhost:8080/api/events \
  -H 'Content-Type: application/json' \
  -d '{"event_no":"EV-R1","sample_code":"S-001","event_type":"receipt",
       "occurred_at":"2026-09-29T12:00:00Z","transfer_no":"EV-T1",
       "receiver":"钱运输","seal_number":"SEAL-001"}'

# 4) 封签检查
curl -s -X POST http://localhost:8080/api/events \
  -H 'Content-Type: application/json' \
  -d '{"event_no":"EV-C1","sample_code":"S-001","event_type":"seal_check",
       "occurred_at":"2026-09-29T12:30:00Z","checker":"陈验收",
       "seal_result":"intact","seal_number":"SEAL-001"}'

# 5) 实验室入库
curl -s -X POST http://localhost:8080/api/events \
  -H 'Content-Type: application/json' \
  -d '{"event_no":"EV-W1","sample_code":"S-001","event_type":"storage",
       "occurred_at":"2026-09-29T15:00:00Z","lab":"中心实验室一号库",
       "seal_number":"SEAL-001"}'

# 6) 查询证据链 / 当前保管人 / 结论
curl -s http://localhost:8080/api/samples/S-001

# 7) 核定入库（仅 valid 可成功）
curl -s -X POST http://localhost:8080/api/samples/S-001/approve \
  -H 'Content-Type: application/json' \
  -d '{"approver":"质量总监","note":"链条完整，准予入库"}'
```

## 七、演示数据

默认初始化的 5 个样本（容器启动即有，可直接在 `/docs` 中点选体验）：

| 样本 | 场景 | 结论 |
| --- | --- | --- |
| `S-VALID-001` | 移交→签收→封签检查→入库，且事件**乱序录入** | `valid` |
| `S-PEND-002` | 移交后未签收、未入库 | `pending`（TRANSFER_OPEN / NO_STORAGE） |
| `S-PEND-005` | 已签收未入库 | `pending`（NO_STORAGE） |
| `S-INVALID-003` | 同一移交单重复签收（尽管已入库，无效优先） | `invalid`（DUPLICATE_RECEIPT） |
| `S-INVALID-004` | 入库时封签号不符 | `invalid`（SEAL_MISMATCH） |

## 八、运行测试

```bash
pip install pytest httpx
python -m pytest -q
```

## 九、项目结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由与错误处理
│   ├── verification.py  # 交接链重放核验（核心规则）
│   ├── database.py      # SQLite 连接、建表、读写
│   ├── schemas.py       # Pydantic 请求模型
│   ├── seed.py          # 演示数据
│   └── config.py        # 环境变量配置
├── tests/               # pytest（规则 + API，共 18 例）
├── examples/            # curl / Python 调用示例
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
