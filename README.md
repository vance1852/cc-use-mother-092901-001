# 交通项目社会资本准入退出协同服务

本项目在综合交通运输业务共享的服务端基础能力（运营机构、操作者、角色权限、请求幂等、
SQLite 事务与哈希串联审计）之上，提供一套完整的**交通项目社会资本准入退出服务**：

- 项目版本、规则（政策门槛）版本、投资人关联关系、资格证据、出资计划、履约担保统一登记；
- 资格准入、分期入场、联合体成员增减、控制权变更、违约/协商退出五类审查均走
  **评审员复核 + 管理员批准**的职责分离会签，提议人不得签署，两签人角色固定且不得越级；
- 待审任务按事件序号形成持久化 FIFO 队列，**服务重启后顺序不变**，且必须按队列顺序会签；
- 关联投资主体（同一控股、母子公司等）在同一项目同一阶段只能有一个在途/生效申请，
  防止不同主体重复提交资质与出资承诺；
- 同一资产被登记为预留/承诺后，不能再被其他项目或其他在途申请占用，授标转承诺、
  落选/驳回/退出即释放；
- 竞争申请只有一个生效结果：授标时其余在途竞争申请自动闭环为 `lost` 并释放预留；
- 会签通过即冻结协议：协议内嵌当时的规则版本与门槛、项目版本、证据哈希、出资计划摘要、
  批准链与协议哈希。**规则换版不倒改已签协议**，结构性变更沿用协议冻结的规则；
- 退出时结清未缴出资与未完成义务，计算追索金额，按违约/协商口径没收或退还履约担保，
  同步释放占用资产；
- `GET /project-snapshot?project_id=...&as_of=...` 可还原任一时点采用的规则、证据、
  批准链与项目版本；`GET /scenarios` 比较候选方案的民营参与比例与项目交付风险。

## 目录

- `src/transport_coordination/`
  - 基础域：`models.py`、`domain.py`、`service.py`、`audit.py`、`clock.py`、`storage.py`；
  - 准入退出域：`admission_models.py`、`admission_store.py`、`projection.py`、
    `admission_service.py`；
  - HTTP 边界：`api.py`；离线验收：`acceptance.py`、`admission_acceptance.py`。
- `tests/`：基础规则、事务边界、接口路由、准入退出域规则与端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
# 基础登记域
PYTHONPATH=src python3 -m transport_coordination.acceptance
# 社会资本准入退出域
PYTHONPATH=src python3 -m transport_coordination.admission_acceptance
```

验收成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。准入退出验收覆盖关联重复
申报拦截、竞争唯一授标、FIFO 会签、担保没收/退还、资产释放、规则冻结与重启还原。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，所有写入都需要
`request_id` 保证幂等。服务重启后 SQLite 中的业务状态、事件流与审计历史继续保留。

### 准入退出域接口

| 方法与路径 | 说明 |
| --- | --- |
| `POST /policies` | 发布规则版本（民营比例、担保覆盖、必备证据等门槛） |
| `POST /projects` | 发布项目版本（投资额、民营目标比例、资产清单） |
| `POST /investors` / `POST /investor-relations` | 投资人登记与关联关系登记 |
| `POST /evidence` / `POST /evidence-verifications` | 资格证据登记（评审员核验） |
| `POST /guarantees` | 履约担保登记 |
| `POST /capital-plans` | 出资计划（总额、民营额、分期） |
| `POST /obligations` / `POST /obligation-updates` | 建设/出资义务登记与状态更新 |
| `POST /reviews` | 发起审查（`qualification`/`entry`/`member_change`/`control_change`/`exit`） |
| `POST /review-approvals` | 会签意见（评审员先、管理员后；驳回即终结） |
| `GET /review-queue` | 待审 FIFO 队列（重启后顺序不变） |
| `GET /reviews?review_id=&as_of=` | 审查详情、冻结协议与退出结清结果 |
| `GET /project-snapshot?project_id=&as_of=` | 任一时点的规则、证据、批准链与时间线 |
| `GET /scenarios?project_id=&stage_label=` | 候选方案民营比例与交付风险比较 |
| `GET /policies` / `GET /projects` / `GET /guarantees` / `GET /investor-groups` | 只读查询 |

### 审查状态

`pending`（会签中）→ `approved`（通过，按类型结果为 `awarded`/`entry`/`member_change`/
`control_change`）、`rejected`（驳回）、`closed`（竞争落选）、`exited`（退出结清完成，
结果为 `exit_default`/`exit_breach`）。
