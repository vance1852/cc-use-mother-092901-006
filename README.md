# 设施更新窗口编排平台

在综合交通基础服务（机构、角色、场所登记、幂等、SQLite 事务、哈希审计链）之上，为老旧桥梁、隧道机电、客运站设备等设施的集中更新提供统一窗口编排，回答"单项计划各自可行、合在一起会不会造成区域运力同时下降"的问题。

## 核心能力

- **统一日历资源**：设施（含依赖设施）、替代通道（剩余分流能力）、专业队伍（资质）、材料（到场时间）、专项资金（额度与使用期限）登记在同一时间轴上。
- **限时草案**：每个更新窗口先形成带 TTL 的方案草案，草案逐版本留痕；超时未会签自动失效，重启后同样会被收敛。
- **三方会签锁定**：运营（operations/operator）、施工（construction）、属地（locality/locality）会签齐备且无未解决冲突后，才能确认锁定并创建设施/队伍/材料/通道/资金租约。
- **冲突检测与取舍解释**：
  - 同一替代通道分流叠加按扫描线核算峰值，超出剩余能力即报 `corridor_capacity`；
  - 同一队伍、同一种材料在同一时段被两个阶段或两个窗口占用即报冲突；
  - 封闭设施依赖集（如隧道依赖的桥梁）被占用即报 `facility_occupied`；
  - 队伍资质不符、材料未到场、预算超额、完工晚于专项资金期限均阻止锁定；
  - 每个冲突附带 `shift_phase` 等取舍建议，窗口时间线可完整重放每次冲突与取舍。
- **只重排受影响阶段**：延期自动级联顺延后续**未完工**阶段；部分完工、验收退回只重排对应阶段并按工序最小顺延；紧急抢修只插入抢修阶段。已完工阶段的停运记录与支付记录不可抹除、不可改期。
- **并发只有一个方案生效**：方案带 `base_revision` 乐观版本号，基于过期版本的调整返回冲突；同窗口新草案自动取代旧草案，配合 `BEGIN IMMEDIATE` 短事务保证串行化。
- **前置条件明确**：开工要求前置阶段完工、资源租约有效（到期须续租，续租不超过资金期限）；复开要求全部阶段完工且没有待签的重排草案，然后释放剩余租约。
- **重启恢复**：租约、会签、草案全部持久化于 SQLite；`POST /recover` 汇报所有未结束窗口的活动租约（标记已到期）、待齐会签方，并把超过 TTL 的草案置为过期。

窗口状态：`draft`（申报中）→ `locked`（资源已锁定）→ `in_progress`（已有阶段开工）→ `reopened`（恢复通行）。

## 主要 HTTP 接口

写接口均需 `X-Actor-Id`，且支持 `request_id` 幂等重放。

| 接口 | 说明 |
| --- | --- |
| `POST /facilities` `/corridors` `/crews` `/materials` `/funds` | 统一日历资源登记 |
| `POST /renewal-windows` | 申报更新窗口（设施 + 专项资金） |
| `POST /proposals` | 提交限时草案（`phases`、`ttl_minutes`、`base_revision`），响应含 `conflicts` 与 `tradeoffs` |
| `POST /proposals/sign` | 三方会签（operations/construction/locality） |
| `POST /proposals/confirm-lock` | 会签齐且冲突清零后锁定资源 |
| `POST /phases/delay` `/phases/reschedule` `/phases/emergency-repair` | 延期、按部分完工/验收退回重排、紧急抢修（均生成新限时草案） |
| `POST /phases/start` `/phases/complete` `/phases/reject` | 开工、完工/部分完工（含支付与资源释放）、验收退回 |
| `POST /leases/renew` | 未结束租约续期（上限为资金期限） |
| `POST /windows/reopen` | 前置条件满足后释放租约、恢复通行 |
| `GET /windows/{id}` `/windows/{id}/timeline` | 窗口状态；从申报到复开的全过程重放（冲突、取舍、停运凭据、支付凭据） |
| `GET /calendar` | 统一日历：活动租约、专项资金期限、窗口状态 |
| `POST /recover` | 服务重启后恢复未结束租约与审批 |

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

验收通过 HTTP 语义路由重放两个窗口争抢同一替代通道与同一支队伍的冲突、三方会签锁定、部分完工、验收退回（支付保留）、紧急抢修、复开与重启恢复，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的窗口、租约、会签、停运与支付记录以及审计历史继续保留。
