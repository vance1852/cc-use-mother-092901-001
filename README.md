# 治理社会资本准入退出协同基础服务

本项目在综合交通运输共享基础能力（运营机构、交通节点、操作者登记、角色权限、请求幂等、SQLite 事务与哈希串联审计）之上，提供一套完整的**交通项目社会资本准入退出服务**：

- **规则版本**：政策门槛（民营股比下限、必备资质、担保费率）按版本发布，只对未来生效；每个项目版本、每份协议都固化其适用规则，换版不倒改已签协议。
- **投资人关联关系**：登记母子公司、实际控制、关联、联合体等关系并做并查集分组，防止同一关联主体用不同名义在同一项目重复竞争。
- **资格证据**：资质件登记、核验、吊销、有效期管理；同一 `document_hash` 不得被多个申请重复使用，授标前再次复核有效性。
- **出资计划与分期入场**：联合体股比之和必须为 1 且达到民营比例门槛；首期成员签约入场，后续期次成员按申请时承诺股比经会签入场；新增成员只能稀释存量股比并重新校验门槛，出资计划逐版本留痕。
- **履约担保**：投标/履约担保缴纳、追索、部分兑付、释放；授标前校验足额，退出时先追索未完成义务再释放可退余额。
- **职责分离会签**：准入、成员增减、分期入场、控制权变更、违约退出均需 reviewer + admin 会签；提单人不得自签，角色槽位唯一；队列全局 FIFO 且持久化，服务重启后未完成审查保持原顺序继续办理。
- **唯一竞争结果**：一个项目最多一个申请中标，其余在审申请自动淘汰并释放其投标担保与预占资产；项目发布新版本时在审申请失效。
- **资产防重复承诺**：承诺资产在申请提交时即预占（成员变动会签提单时预占），数据库层用部分唯一索引保证同一资产任一时刻只被一个项目占用；落选、失效、驳回、退出时释放。
- **退出结算**：成员退出或违约退出时结清未完成义务，担保按义务额追索、剩余释放，资产解除占用，生成结算单；控股方须先变更控制权才能以普通成员身份退出。
- **时点还原**：可经 HTTP 还原任一时点项目采用的规则版本、项目版本、申请与证据状态、协议出资计划版本、成员变迁、担保流水和完整批准链。
- **方案比较**：对候选联合体比较民营参与比例、资质覆盖、担保覆盖、控股集中度、分期安排，输出政策达标情况与交付风险评分并给出推荐。

## 目录

- `src/transport_coordination/`：基础登记服务（`service.py`）、社会资本准入退出领域服务（`participation.py`）、SQLite 存储、审计链、HTTP 路由和离线验收；
- `tests/`：基础规则、准入退出领域规则、事务边界、接口路由和端到端验收测试。

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
PYTHONPATH=src python3 -m transport_coordination.acceptance
```

验收命令在临时 SQLite 数据库中跑通基础登记与社会资本准入全链路：规则发布、竞争申请、职责分离会签、唯一成交、控制权变更、违约退出结算（追索与可退担保）和时点还原，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，所有写接口需带幂等键 `request_id`，服务重启后 SQLite 中的业务状态、会签队列和审计历史继续保留。

### 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/rule-versions` | 发布规则版本（admin） |
| POST | `/projects`、`POST /projects/{id}/versions` | 建项目、发布项目版本 |
| POST | `/investors`、`POST /investor-relations`、`POST /investor-relations/{id}/revoke` | 投资人与关联关系 |
| POST | `/assets` | 登记可承诺资产 |
| POST | `/evidence`、`POST /evidence/{id}/verify`、`/evidence/{id}/revoke` | 资格证据登记、核验、吊销 |
| POST | `/projects/{id}/applications` | 提交竞争申请（成员、出资、证据、承诺资产） |
| POST | `/applications/{id}/guarantees` | 缴纳投标/履约担保 |
| POST | `/review-tasks`、`POST /review-tasks/{id}/sign` | 提交会签事项、会签（FIFO） |
| GET | `/review-tasks?status=pending` | 查看会签队列 |
| POST | `/agreements/{id}/obligations`、`POST /obligations/{id}/fulfill` | 义务登记与履约 |
| GET | `/applications/{id}`、`GET /agreements/{id}` | 申请与协议查询 |
| GET | `/projects/{id}/snapshot?at=ISO8601` | 任一时点规则、证据、批准链还原 |
| POST | `/projects/{id}/compare-candidates` | 候选方案民营比例与交付风险比较 |
