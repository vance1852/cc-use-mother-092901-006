# 编排老旧交通设施更新协同基础服务

本项目提供综合交通运输业务共享的服务端基础能力，负责运营机构、交通节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域服务可以在这些稳定边界上扩展自己的状态、规则和接口。

在此之上，项目内置了**设施更新窗口编排平台**（`renewal.py`）：面向老旧桥梁、隧道机电和客运站设备的集中更新期，把设施依赖、施工阶段、封闭范围、替代能力、队伍资质、材料到场和专项资金期限放进统一日历，按"限时草案 → 三方会签 → 锁定资源 → 施工推进 → 验收 → 释放资源 → 恢复通行"编排，并支持从申报到复开的全过程重放。

## 目录

- `src/transport_coordination/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由、离线验收，以及设施更新窗口编排平台（`renewal.py`、`renewal_acceptance.py`）；
- `tests/`：基础规则、事务边界、接口路由、编排平台规则和端到端验收测试。

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
PYTHONPATH=src python3 -m transport_coordination.renewal_acceptance
```

基础验收在临时库中登记组织、操作者、场所和参考资料，核对幂等回执与审计链；编排验收则通过 HTTP 路由重放两份更新计划从申报、会签、冲突取舍、调整、施工、验收到复开的全过程，并在中途重启服务验证未结束租约与审批的恢复。成功时各输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态、未结束租约、审批记录和审计历史继续保留。

## 设施更新窗口编排平台

### 角色

在基础角色之外新增：`planner`（计划员，申报与调整）、`operations`（运营会签/验收/复开）、`construction`（施工方会签/开工/完工/计量）、`local_manager`（属地管理会签）。会签必须三方本人角色分别签署，任何人不能代签。

### 资源与日历

- `POST /renewal/facilities`：登记设施（`bridge` / `tunnel_electromech` / `passenger_station`），`depends_on` 声明替代依赖设施；
- `POST /renewal/routes`：替代通道及总运力；`POST /renewal/crews`：专业队伍及作业资质；
- `POST /renewal/funds`：专项资金及使用期限；`POST /renewal/materials`：材料及到场时间；
- `GET /renewal/calendar?site_id=&start=&end=`：统一日历，汇总阶段窗口、生效租约、材料到场、资金期限和未结束停运。

### 计划生命周期

1. `POST /renewal/plans`：申报限时草案（默认 72 小时会签期限，`draft_ttl_hours` 可调）。申报即校验队伍资质、材料到场、资金期限和计划内部重叠；并对照已锁定窗口检测跨计划冲突，随草案返回解释与取舍建议；
2. `POST /renewal/plans/{id}/approvals`：运营、施工、属地管理三方会签。第三方签署后在同一事务中尝试锁定资源：无冲突则建立队伍与替代通道租约并置为 `locked`；有冲突则计划停在 `approved`，响应携带每条冲突的原因与取舍建议（可改用的空闲队伍、可压缩的运力、可让行的窗口）；
3. `POST /renewal/plans/{id}/lock`：冲突解除后重试锁定；仍存在冲突时返回 `409` 及结构化冲突说明；
4. `POST /renewal/plans/{id}/phases/{ph}/start|complete|accept|reject`：施工推进。开工建立停运记录，完工登记支付并结束停运，前置阶段未验收不得开工；
5. `POST /renewal/plans/{id}/leases/{lease}/release`：释放资源，前置条件是所属阶段已验收通过；
6. `POST /renewal/plans/{id}/reopen`：恢复通行，前置条件是全部阶段验收通过、无未结束停运、租约全部释放；不满足时 `409` 逐条列出未满足项；
7. `GET /renewal/plans/{id}/replay`：按时间顺序重放该计划从申报到复开的全部事件。

### 调整规则

`POST /renewal/plans/{id}/adjustments` 支持四类调整，均要求 `expected_version` 与当前窗口版本一致——同一窗口的并发调整最多一个方案生效，其余收到 `409`：

- `delay`：延期，只重排受影响阶段（未开工阶段整体移动，在施工阶段只能顺延完工时间）；
- `partial_complete`：部分完工计量，登记支付记录，可同时顺延剩余阶段；
- `acceptance_reject`：验收退回后的返工重排，被退回阶段回到待排期；
- `emergency_repair`：紧急抢修，立即记录抢修停运，受影响阶段必须排到抢修结束之后。

重排会同步移动租约窗口并重新校验资金期限、材料到场和跨计划冲突；冲突时整个调整回滚（版本不变），响应解释冲突。已发生的停运与支付记录只增不改，任何调整都不会抹除历史。

### 并发与恢复

所有写操作在 `BEGIN IMMEDIATE` 事务中串行执行；窗口版本号以 `UPDATE ... WHERE version=?` 受控递增，保证并发调整同一窗口时最多一个方案生效。平台状态全部落在 SQLite，服务重启后未结束的租约继续参与冲突拦截，未完成的会签可以继续，审计链可离线校验。
