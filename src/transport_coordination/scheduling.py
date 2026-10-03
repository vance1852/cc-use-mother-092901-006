"""设施更新窗口编排：统一日历、限时会签、资源租约与不可变记录。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import (
    ConflictError,
    ExpiredError,
    NotFoundError,
    PermissionDenied,
    PreconditionFailed,
    ValidationError,
)
from .storage import Database

SIGN_PARTIES = ("operations", "construction", "locality")
# 会签方对应的操作者角色：运营方 operator、施工方 construction、属地管理方 locality
PARTY_ROLES = {"operations": "operator", "construction": "construction", "locality": "locality"}
PHASE_KINDS = ("renewal", "emergency")


class SchedulingService:
    """在统一日历上编排设施更新窗口的申报、会签、执行与复开。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础工具

    def _now_dt(self):
        return self.clock.now()

    def _now(self) -> str:
        return self._now_dt().isoformat().replace("+00:00", "Z")

    def _iso(self, parsed) -> str:
        return parsed.isoformat().replace("+00:00", "Z")

    def _dt(self, value: Any, field: str):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field} 必须是 ISO-8601 时间") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须带时区")
        return parsed.astimezone(timezone.utc)

    def _text(self, value: Any, field: str, limit: int = 200) -> str:
        value = str(value if value is not None else "").strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _number(self, value: Any, field: str, *, positive: bool = False) -> float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValidationError(f"{field} 必须是数值")
        if positive and value <= 0:
            raise ValidationError(f"{field} 必须大于 0")
        if value < 0:
            raise ValidationError(f"{field} 不能为负")
        return float(value)

    def _actor(self, connection, actor_id: str):
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require(self, actor, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str, payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
        request_id = self._text(request_id, "request_id", 64)
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {"request_id": request_id, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"], "replayed": True,
                    **json.loads(row["response_json"])}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {"request_id": request_id, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False, **response}

    def _journal(self, connection, *, window_id: str, event_type: str, actor_id: str,
                 detail: dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO window_journal(window_id,event_type,actor_id,event_at,detail_json) VALUES(?,?,?,?,?)",
            (window_id, event_type, actor_id, self._now(), canonical_json(detail)),
        )

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    # ------------------------------------------------------------------ 日历资源登记

    def register_facility(self, *, request_id: str, actor_id: str, facility_id: str, site_id: str,
                          name: str, facility_type: str, depends_on: list[str] | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "facility_id": facility_id, "site_id": site_id,
                   "name": name, "facility_type": facility_type, "depends_on": depends_on or []}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            facility_id = self._text(facility_id, "facility_id", 64)
            name = self._text(name, "name")
            facility_type = self._text(facility_type, "facility_type", 64)
            depends = [self._text(x, "depends_on", 64) for x in (depends_on or [])]
            if facility_id in depends:
                raise ValidationError("设施不能依赖自身")
            if connection.execute("SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone() is None:
                raise NotFoundError("场所不存在")

            def create():
                if connection.execute("SELECT 1 FROM facilities WHERE facility_id=?", (facility_id,)).fetchone():
                    raise ConflictError("设施编号已经存在")
                for dep in depends:
                    if connection.execute("SELECT 1 FROM facilities WHERE facility_id=?", (dep,)).fetchone() is None:
                        raise NotFoundError(f"依赖设施 {dep} 不存在")
                connection.execute(
                    "INSERT INTO facilities(facility_id,site_id,name,facility_type,depends_on_json,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (facility_id, site_id, name, facility_type, canonical_json(depends), actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="facility.registered",
                            resource_type="facility", resource_id=facility_id,
                            detail={"name": name, "facility_type": facility_type, "depends_on": depends})
                return "facility", facility_id, {"facility_id": facility_id}

            return self._idempotent(connection, request_id=request_id, action="register_facility",
                                    payload=payload, create=create)

    def register_corridor(self, *, request_id: str, actor_id: str, corridor_id: str,
                          name: str, spare_capacity: float) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "corridor_id": corridor_id, "name": name,
                   "spare_capacity": spare_capacity}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            corridor_id = self._text(corridor_id, "corridor_id", 64)
            name = self._text(name, "name")
            capacity = self._number(spare_capacity, "spare_capacity")

            def create():
                if connection.execute("SELECT 1 FROM corridors WHERE corridor_id=?", (corridor_id,)).fetchone():
                    raise ConflictError("替代通道编号已经存在")
                connection.execute(
                    "INSERT INTO corridors(corridor_id,name,spare_capacity,created_by,created_at) VALUES(?,?,?,?,?)",
                    (corridor_id, name, capacity, actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="corridor.registered",
                            resource_type="corridor", resource_id=corridor_id,
                            detail={"name": name, "spare_capacity": capacity})
                return "corridor", corridor_id, {"corridor_id": corridor_id}

            return self._idempotent(connection, request_id=request_id, action="register_corridor",
                                    payload=payload, create=create)

    def register_crew(self, *, request_id: str, actor_id: str, crew_id: str, name: str,
                      qualifications: list[str]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "crew_id": crew_id, "name": name,
                   "qualifications": qualifications}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            crew_id = self._text(crew_id, "crew_id", 64)
            name = self._text(name, "name")
            quals = sorted({self._text(q, "qualification", 64) for q in qualifications})
            if not quals:
                raise ValidationError("队伍至少需要一项资质")

            def create():
                if connection.execute("SELECT 1 FROM crews WHERE crew_id=?", (crew_id,)).fetchone():
                    raise ConflictError("队伍编号已经存在")
                connection.execute(
                    "INSERT INTO crews(crew_id,name,qualifications_json,created_by,created_at) VALUES(?,?,?,?,?)",
                    (crew_id, name, canonical_json(quals), actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="crew.registered",
                            resource_type="crew", resource_id=crew_id,
                            detail={"name": name, "qualifications": quals})
                return "crew", crew_id, {"crew_id": crew_id}

            return self._idempotent(connection, request_id=request_id, action="register_crew",
                                    payload=payload, create=create)

    def register_material(self, *, request_id: str, actor_id: str, material_id: str, name: str,
                          arrived_at: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "material_id": material_id, "name": name, "arrived_at": arrived_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            material_id = self._text(material_id, "material_id", 64)
            name = self._text(name, "name")
            arrived = self._iso(self._dt(arrived_at, "arrived_at"))

            def create():
                if connection.execute("SELECT 1 FROM materials WHERE material_id=?", (material_id,)).fetchone():
                    raise ConflictError("材料编号已经存在")
                connection.execute(
                    "INSERT INTO materials(material_id,name,arrived_at,created_by,created_at) VALUES(?,?,?,?,?)",
                    (material_id, name, arrived, actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="material.registered",
                            resource_type="material", resource_id=material_id,
                            detail={"name": name, "arrived_at": arrived})
                return "material", material_id, {"material_id": material_id}

            return self._idempotent(connection, request_id=request_id, action="register_material",
                                    payload=payload, create=create)

    def register_fund(self, *, request_id: str, actor_id: str, fund_id: str, name: str,
                      amount: float, valid_until: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "fund_id": fund_id, "name": name, "amount": amount,
                   "valid_until": valid_until}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            fund_id = self._text(fund_id, "fund_id", 64)
            name = self._text(name, "name")
            amount_value = self._number(amount, "amount", positive=True)
            until = self._iso(self._dt(valid_until, "valid_until"))

            def create():
                if connection.execute("SELECT 1 FROM funds WHERE fund_id=?", (fund_id,)).fetchone():
                    raise ConflictError("专项资金编号已经存在")
                connection.execute(
                    "INSERT INTO funds(fund_id,name,amount,valid_until,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (fund_id, name, amount_value, until, actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="fund.registered",
                            resource_type="fund", resource_id=fund_id,
                            detail={"name": name, "amount": amount_value, "valid_until": until})
                return "fund", fund_id, {"fund_id": fund_id}

            return self._idempotent(connection, request_id=request_id, action="register_fund",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 窗口申报

    def create_window(self, *, request_id: str, actor_id: str, facility_id: str, fund_id: str,
                      title: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "facility_id": facility_id, "fund_id": fund_id, "title": title}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            facility_id = self._text(facility_id, "facility_id", 64)
            fund_id = self._text(fund_id, "fund_id", 64)
            title = self._text(title, "title")
            if connection.execute("SELECT 1 FROM facilities WHERE facility_id=?", (facility_id,)).fetchone() is None:
                raise NotFoundError("设施不存在")
            if connection.execute("SELECT 1 FROM funds WHERE fund_id=?", (fund_id,)).fetchone() is None:
                raise NotFoundError("专项资金不存在")

            def create():
                window_id = "win-" + uuid.uuid4().hex[:12]
                connection.execute(
                    "INSERT INTO renewal_windows(window_id,facility_id,fund_id,title,status,active_revision,"
                    "created_by,created_at,updated_at) VALUES(?,?,?,?,?,0,?,?,?)",
                    (window_id, facility_id, fund_id, title, "draft", actor_id, self._now(), self._now()),
                )
                self._journal(connection, window_id=window_id, event_type="window.declared",
                              actor_id=actor_id, detail={"facility_id": facility_id, "fund_id": fund_id,
                                                         "title": title})
                self._audit(connection, actor_id=actor_id, action="window.declared",
                            resource_type="renewal_window", resource_id=window_id,
                            detail={"facility_id": facility_id, "fund_id": fund_id})
                return "renewal_window", window_id, {"window_id": window_id, "status": "draft"}

            return self._idempotent(connection, request_id=request_id, action="create_window",
                                    payload=payload, create=create)

    def _load_window(self, connection, window_id: str):
        row = connection.execute("SELECT * FROM renewal_windows WHERE window_id=?", (window_id,)).fetchone()
        if row is None:
            raise NotFoundError("更新窗口不存在")
        return row

    # ------------------------------------------------------------------ 阶段规范化与冲突评估

    def _normalize_phase(self, connection, raw: dict[str, Any], ordinal: int,
                         allowed_code: str | None = None) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValidationError("每个阶段必须是对象")
        code = self._text(raw.get("phase_code"), "phase_code", 64)
        if allowed_code and code != allowed_code:
            raise ValidationError(f"该接口只能调整阶段 {allowed_code}")
        title = self._text(raw.get("title"), f"{code}.title")
        start = self._dt(raw.get("planned_start"), f"{code}.planned_start")
        end = self._dt(raw.get("planned_end"), f"{code}.planned_end")
        if end <= start:
            raise ValidationError(f"{code} 的结束时间必须晚于开始时间")
        closure_scope = self._text(raw.get("closure_scope"), f"{code}.closure_scope")
        diverted = self._number(raw.get("diverted_volume", 0), f"{code}.diverted_volume")
        corridor_id = raw.get("corridor_id")
        if corridor_id:
            corridor_id = self._text(corridor_id, f"{code}.corridor_id", 64)
            corridor = connection.execute("SELECT * FROM corridors WHERE corridor_id=?", (corridor_id,)).fetchone()
            if corridor is None:
                raise NotFoundError(f"{code} 引用的替代通道不存在")
            if diverted <= 0:
                raise ValidationError(f"{code} 占用替代通道时 diverted_volume 必须大于 0")
        elif diverted > 0:
            raise ValidationError(f"{code} 有分流流量但未指定替代通道")
        crew_id = self._text(raw.get("crew_id"), f"{code}.crew_id", 64)
        crew = connection.execute("SELECT * FROM crews WHERE crew_id=?", (crew_id,)).fetchone()
        if crew is None:
            raise NotFoundError(f"{code} 引用的队伍不存在")
        qualification = self._text(raw.get("qualification"), f"{code}.qualification", 64)
        if qualification not in json.loads(crew["qualifications_json"]):
            raise ValidationError(f"队伍 {crew_id} 缺少资质 {qualification}")
        material_ids = raw.get("material_ids") or []
        if not isinstance(material_ids, list) or not material_ids:
            raise ValidationError(f"{code} 至少需要一种材料")
        material_ids = [self._text(m, f"{code}.material_id", 64) for m in material_ids]
        for material_id in material_ids:
            material = connection.execute("SELECT * FROM materials WHERE material_id=?", (material_id,)).fetchone()
            if material is None:
                raise NotFoundError(f"{code} 引用的材料 {material_id} 不存在")
            if start < self._dt(material["arrived_at"], "material.arrived_at"):
                raise ValidationError(f"{code} 开始早于材料 {material_id} 到场时间")
        cost = self._number(raw.get("cost"), f"{code}.cost", positive=True)
        kind = raw.get("kind", "renewal")
        if kind not in PHASE_KINDS:
            raise ValidationError(f"{code}.kind 非法")
        return {"phase_code": code, "title": title, "ordinal": ordinal,
                "planned_start": self._iso(start), "planned_end": self._iso(end),
                "start_dt": start, "end_dt": end, "closure_scope": closure_scope,
                "diverted_volume": diverted, "corridor_id": corridor_id, "crew_id": crew_id,
                "qualification": qualification, "material_ids": material_ids,
                "cost": cost, "kind": kind}

    def _normalize_phases(self, connection, raw_phases: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(raw_phases, list) or not raw_phases:
            raise ValidationError("phases 必须是非空数组")
        phases: list[dict[str, Any]] = []
        seen: set[str] = set()
        for ordinal, raw in enumerate(raw_phases, start=1):
            phase = self._normalize_phase(connection, raw, ordinal)
            if phase["phase_code"] in seen:
                raise ValidationError(f"阶段编号重复: {phase['phase_code']}")
            seen.add(phase["phase_code"])
            phases.append(phase)
        return phases

    def _facility_closure_set(self, connection, facility_id: str) -> list[str]:
        facility = connection.execute("SELECT * FROM facilities WHERE facility_id=?", (facility_id,)).fetchone()
        return [facility_id] + json.loads(facility["depends_on_json"])

    def _proposal_demands(self, connection, window, phases: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把方案折算成设施/队伍/材料/通道在时间轴上的占用区间。"""
        demands: list[dict[str, Any]] = []
        closure_set = self._facility_closure_set(connection, window["facility_id"])
        for phase in phases:
            if phase["closure_scope"] != "none":
                for facility_id in closure_set:
                    demands.append({"resource_type": "facility", "resource_id": facility_id,
                                    "phase_code": phase["phase_code"], "demand": 1.0,
                                    "start_dt": phase["start_dt"], "end_dt": phase["end_dt"]})
            demands.append({"resource_type": "crew", "resource_id": phase["crew_id"],
                            "phase_code": phase["phase_code"], "demand": 1.0,
                            "start_dt": phase["start_dt"], "end_dt": phase["end_dt"]})
            for material_id in phase["material_ids"]:
                demands.append({"resource_type": "material", "resource_id": material_id,
                                "phase_code": phase["phase_code"], "demand": 1.0,
                                "start_dt": phase["start_dt"], "end_dt": phase["end_dt"]})
            if phase["corridor_id"]:
                demands.append({"resource_type": "corridor", "resource_id": phase["corridor_id"],
                                "phase_code": phase["phase_code"], "demand": phase["diverted_volume"],
                                "start_dt": phase["start_dt"], "end_dt": phase["end_dt"]})
        return demands

    @staticmethod
    def _overlaps(a_start, a_end, b_start, b_end) -> bool:
        return a_start < b_end and b_start < a_end

    def _evaluate_conflicts(self, connection, window, phases: list[dict[str, Any]]
                            ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        conflicts: list[dict[str, Any]] = []
        tradeoffs: list[dict[str, Any]] = []
        # 已完工阶段是不可变历史：不参与冲突评估、预算与期限检查
        completed_codes = {r["phase_code"] for r in connection.execute(
            "SELECT phase_code FROM phase_executions WHERE window_id=? AND status='completed'",
            (window["window_id"],)).fetchall()}
        phases = [p for p in phases if p["phase_code"] not in completed_codes]
        demands = self._proposal_demands(connection, window, phases)
        remaining_cost = sum(p["cost"] for p in phases)

        # ---- 专项资金：已付（含本窗口不可退支付）+ 其他窗口预留 + 未完工阶段造价
        fund = connection.execute("SELECT * FROM funds WHERE fund_id=?", (window["fund_id"],)).fetchone()
        paid_all = connection.execute(
            "SELECT COALESCE(SUM(amount),0) AS total FROM payments WHERE fund_id=?",
            (window["fund_id"],)).fetchone()["total"]
        reserved_other = connection.execute(
            "SELECT COALESCE(SUM(demand),0) AS total FROM leases WHERE resource_type='fund' "
            "AND status='active' AND resource_id=? AND window_id<>?",
            (window["fund_id"], window["window_id"])).fetchone()["total"]
        if paid_all + reserved_other + remaining_cost > fund["amount"] + 1e-9:
            conflicts.append({"type": "fund_budget", "fund_id": fund["fund_id"],
                              "message": "预算超过专项资金可用额度（已支付款项不可退回）",
                              "detail": {"remaining_cost": remaining_cost, "paid_total": paid_all,
                                         "reserved_other_windows": reserved_other,
                                         "fund_amount": fund["amount"]}})
        deadline = self._dt(fund["valid_until"], "fund.valid_until")
        for phase in phases:
            if phase["end_dt"] > deadline:
                conflicts.append({"type": "fund_deadline", "phase_code": phase["phase_code"],
                                  "message": "阶段完工晚于专项资金期限",
                                  "detail": {"planned_end": phase["planned_end"],
                                             "valid_until": fund["valid_until"]}})
                tradeoffs.append({"type": "shift_phase", "phase_code": phase["phase_code"],
                                  "resource_type": "fund", "resource_id": fund["fund_id"],
                                  "latest_end": fund["valid_until"],
                                  "message": f"将阶段 {phase['phase_code']} 提前到专项资金期限 "
                                             f"{fund['valid_until']} 之前完工"})

        other_leases = connection.execute(
            "SELECT * FROM leases WHERE status='active' AND window_id<>?", (window["window_id"],)
        ).fetchall()

        def add_shift(phase_code: str, resource_type: str, resource_id: str, earliest) -> None:
            tradeoffs.append({"type": "shift_phase", "phase_code": phase_code,
                              "resource_type": resource_type, "resource_id": resource_id,
                              "earliest_start": self._iso(earliest),
                              "message": f"将阶段 {phase_code} 顺延至 {self._iso(earliest)} 之后，"
                                         f"等待 {resource_type}:{resource_id} 释放"})

        # ---- 队伍/材料：方案内部互斥
        for i, demand in enumerate(demands):
            if demand["resource_type"] not in ("crew", "material"):
                continue
            for other in demands[i + 1:]:
                if other["resource_type"] != demand["resource_type"] \
                        or other["resource_id"] != demand["resource_id"] \
                        or other["phase_code"] == demand["phase_code"]:
                    continue
                if self._overlaps(demand["start_dt"], demand["end_dt"], other["start_dt"], other["end_dt"]):
                    conflicts.append({
                        "type": f"{demand['resource_type']}_double_booked",
                        "resource_type": demand["resource_type"], "resource_id": demand["resource_id"],
                        "phase_code": demand["phase_code"], "other_phase_code": other["phase_code"],
                        "message": f"阶段 {demand['phase_code']} 与 {other['phase_code']} 在同一时段"
                                   f"占用同一{demand['resource_type']} {demand['resource_id']}",
                        "detail": {"self": [demand["planned_start"] if "planned_start" in demand
                                            else self._iso(demand["start_dt"]),
                                            self._iso(demand["end_dt"])],
                                   "other": [self._iso(other["start_dt"]), self._iso(other["end_dt"])]},
                    })

        # ---- 设施/队伍/材料：与其他窗口活动租约互斥
        for demand in demands:
            if demand["resource_type"] == "corridor":
                continue
            for lease in other_leases:
                if lease["resource_type"] != demand["resource_type"] \
                        or lease["resource_id"] != demand["resource_id"]:
                    continue
                ls, le = self._dt(lease["leased_from"], "lease.from"), self._dt(lease["valid_until"], "lease.until")
                if not self._overlaps(demand["start_dt"], demand["end_dt"], ls, le):
                    continue
                label = {"facility": "设施（含依赖通道）", "crew": "专业队伍",
                         "material": "到场材料"}[demand["resource_type"]]
                conflicts.append({
                    "type": f"{demand['resource_type']}_occupied",
                    "resource_type": demand["resource_type"], "resource_id": demand["resource_id"],
                    "phase_code": demand["phase_code"], "other_window_id": lease["window_id"],
                    "message": f"{label} {demand['resource_id']} 在该时段已被窗口 {lease['window_id']} 占用",
                    "detail": {"lease_until": self._iso(le)},
                })
                add_shift(demand["phase_code"], demand["resource_type"], demand["resource_id"], le)

        # ---- 替代通道：扫描线叠加分流能力（方案内部 + 跨窗口）
        corridor_ids = sorted({d["resource_id"] for d in demands if d["resource_type"] == "corridor"})
        for corridor_id in corridor_ids:
            corridor = connection.execute("SELECT * FROM corridors WHERE corridor_id=?", (corridor_id,)).fetchone()
            events = []
            for demand in demands:
                if demand["resource_type"] == "corridor" and demand["resource_id"] == corridor_id:
                    events.append((demand["start_dt"], 1, demand["demand"],
                                   f"{window['window_id']}:{demand['phase_code']}"))
                    events.append((demand["end_dt"], -1, demand["demand"],
                                   f"{window['window_id']}:{demand['phase_code']}"))
            for lease in other_leases:
                if lease["resource_type"] == "corridor" and lease["resource_id"] == corridor_id:
                    ls = self._dt(lease["leased_from"], "lease.from")
                    le = self._dt(lease["valid_until"], "lease.until")
                    tag = f"{lease['window_id']}:{lease['phase_code'] or '*'}"
                    events.append((ls, 1, lease["demand"], tag))
                    events.append((le, -1, lease["demand"], tag))
            events.sort(key=lambda e: (e[0], e[1]))
            load = 0.0
            peak = 0.0
            peak_at = None
            peak_contributors: list[str] = []
            active_tags: set[str] = set()
            for moment, direction, volume, tag in events:
                if direction == 1:
                    load += volume
                    active_tags.add(tag)
                    if load > peak + 1e-9:
                        peak, peak_at, peak_contributors = load, moment, sorted(active_tags)
                else:
                    load -= volume
                    active_tags.discard(tag)
            if peak > corridor["spare_capacity"] + 1e-9:
                conflicts.append({
                    "type": "corridor_capacity", "corridor_id": corridor_id,
                    "message": f"替代通道 {corridor_id} 分流能力不足，区域运力会同时下降",
                    "detail": {"peak_diverted": peak, "peak_at": self._iso(peak_at),
                               "spare_capacity": corridor["spare_capacity"],
                               "contributors": peak_contributors},
                })
                lease_end = None
                for lease in other_leases:
                    if lease["resource_type"] == "corridor" and lease["resource_id"] == corridor_id:
                        le = self._dt(lease["valid_until"], "lease.until")
                        lease_end = le if lease_end is None else max(lease_end, le)
                for demand in demands:
                    if demand["resource_type"] == "corridor" and demand["resource_id"] == corridor_id:
                        add_shift(demand["phase_code"], "corridor", corridor_id,
                                  lease_end or demand["end_dt"])
        return conflicts, tradeoffs

    # ------------------------------------------------------------------ 方案持久化

    def _persist_proposal(self, connection, *, window, phases: list[dict[str, Any]], base_revision: int,
                          reason: str, ttl_minutes: int, created_by: str,
                          conflicts: list[dict[str, Any]], tradeoffs: list[dict[str, Any]]) -> dict[str, Any]:
        if base_revision != window["active_revision"]:
            raise ConflictError("方案基于过期版本：同一窗口已有更新的生效方案，请基于当前版本重新调整")
        revision = window["active_revision"] + 1
        proposal_id = "pp-" + uuid.uuid4().hex[:12]
        expires = self._iso(self._now_dt().replace(microsecond=0) + timedelta(minutes=ttl_minutes))
        budget = sum(p["cost"] for p in phases)
        connection.execute(
            "INSERT INTO proposals(proposal_id,window_id,revision,base_revision,status,reason,"
            "ttl_expires_at,budget,conflicts_json,tradeoffs_json,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (proposal_id, window["window_id"], revision, base_revision, "draft", reason,
             expires, budget, canonical_json(conflicts), canonical_json(tradeoffs), created_by, self._now()),
        )
        for phase in phases:
            connection.execute(
                "INSERT INTO phase_plans(phase_plan_id,proposal_id,window_id,phase_code,title,ordinal,"
                "planned_start,planned_end,closure_scope,diverted_volume,corridor_id,crew_id,qualification,"
                "material_ids_json,cost,kind) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, proposal_id, window["window_id"], phase["phase_code"], phase["title"],
                 phase["ordinal"], phase["planned_start"], phase["planned_end"], phase["closure_scope"],
                 phase["diverted_volume"], phase["corridor_id"], phase["crew_id"], phase["qualification"],
                 canonical_json(phase["material_ids"]), phase["cost"], phase["kind"]),
            )
        # 并发调整同一窗口时，新草案取代旧草案，最多一个方案保持待生效
        connection.execute(
            "UPDATE proposals SET status='superseded' WHERE window_id=? AND status='draft' "
            "AND proposal_id<>?", (window["window_id"], proposal_id))
        connection.execute(
            "UPDATE renewal_windows SET active_revision=?,effective_proposal_id=?,updated_at=? "
            "WHERE window_id=?",
            (revision, proposal_id, self._now(), window["window_id"]))
        self._journal(connection, window_id=window["window_id"], event_type="proposal.created",
                      actor_id=created_by,
                      detail={"proposal_id": proposal_id, "revision": revision, "base_revision": base_revision,
                              "reason": reason, "ttl_expires_at": expires,
                              "conflicts": conflicts, "tradeoffs": tradeoffs,
                              "phases": [{"phase_code": p["phase_code"], "ordinal": p["ordinal"],
                                          "planned_start": p["planned_start"], "planned_end": p["planned_end"],
                                          "crew_id": p["crew_id"], "corridor_id": p["corridor_id"],
                                          "kind": p["kind"], "cost": p["cost"]} for p in phases]})
        self._audit(connection, actor_id=created_by, action="proposal.created",
                    resource_type="proposal", resource_id=proposal_id,
                    detail={"window_id": window["window_id"], "revision": revision,
                            "conflict_count": len(conflicts), "reason": reason})
        return {"proposal_id": proposal_id, "revision": revision, "ttl_expires_at": expires,
                "conflicts": conflicts, "tradeoffs": tradeoffs, "budget": budget}

    def submit_proposal(self, *, request_id: str, actor_id: str, window_id: str,
                        phases: list[dict[str, Any]], ttl_minutes: int = 1440,
                        base_revision: int | None = None, reason: str = "initial") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "window_id": window_id, "phases": phases,
                   "ttl_minutes": ttl_minutes, "base_revision": base_revision, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            window = self._load_window(connection, window_id)
            if window["status"] != "draft":
                raise ConflictError("窗口已经会签锁定，初始申报不能再次提交")
            ttl_minutes = int(ttl_minutes)
            if ttl_minutes <= 0:
                raise ValidationError("ttl_minutes 必须为正")
            normalized = self._normalize_phases(connection, phases)
            conflicts, tradeoffs = self._evaluate_conflicts(connection, window, normalized)

            def create():
                fresh_window = self._load_window(connection, window_id)
                result = self._persist_proposal(
                    connection, window=fresh_window, phases=normalized,
                    base_revision=fresh_window["active_revision"] if base_revision is None else int(base_revision),
                    reason=self._text(reason, "reason", 200), ttl_minutes=ttl_minutes,
                    created_by=actor_id, conflicts=conflicts, tradeoffs=tradeoffs)
                return "proposal", result["proposal_id"], result

            return self._idempotent(connection, request_id=request_id, action="submit_proposal",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 会签

    def sign_proposal(self, *, request_id: str, actor_id: str, proposal_id: str, party: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "proposal_id": proposal_id, "party": party}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if party not in SIGN_PARTIES:
                raise ValidationError("会签方必须是 operations/construction/locality")
            self._require(actor, "admin", PARTY_ROLES[party])
            proposal = connection.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            if proposal is None:
                raise NotFoundError("方案不存在")
            window = self._load_window(connection, proposal["window_id"])

            def create():
                fresh = connection.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
                if fresh["status"] == "locked":
                    if connection.execute("SELECT 1 FROM signatures WHERE proposal_id=? AND party=?",
                                          (proposal_id, party)).fetchone():
                        return "signature", f"{proposal_id}:{party}", {
                            "proposal_id": proposal_id, "party": party, "locked": True,
                            "signed_parties": list(SIGN_PARTIES)}
                    raise ConflictError("方案已锁定，不能补签")
                if fresh["status"] == "superseded":
                    raise ConflictError("方案已被新版本取代，不能会签；请会签当前生效草案")
                if self._now_dt() > self._dt(fresh["ttl_expires_at"], "ttl_expires_at"):
                    connection.execute("UPDATE proposals SET status='expired' WHERE proposal_id=?", (proposal_id,))
                    self._journal(connection, window_id=window["window_id"], event_type="proposal.expired",
                                  actor_id=actor_id, detail={"proposal_id": proposal_id})
                    self._audit(connection, actor_id=actor_id, action="proposal.expired",
                                resource_type="proposal", resource_id=proposal_id,
                                detail={"window_id": window["window_id"]})
                    raise ExpiredError("限时草案超过会签期限，须重新申报方案")
                if window["effective_proposal_id"] != proposal_id:
                    raise ConflictError("该方案不是当前生效版本，不能会签")
                connection.execute(
                    "INSERT INTO signatures(proposal_id,window_id,party,actor_id,signed_at) VALUES(?,?,?,?,?)",
                    (proposal_id, window["window_id"], party, actor_id, self._now()),
                )
                self._journal(connection, window_id=window["window_id"], event_type="proposal.signed",
                              actor_id=actor_id, detail={"proposal_id": proposal_id, "party": party})
                self._audit(connection, actor_id=actor_id, action="proposal.signed",
                            resource_type="proposal", resource_id=proposal_id,
                            detail={"window_id": window["window_id"], "party": party})
                parties = [r["party"] for r in connection.execute(
                    "SELECT party FROM signatures WHERE proposal_id=? ORDER BY party", (proposal_id,))]
                locked = set(parties) == set(SIGN_PARTIES)
                result = {"proposal_id": proposal_id, "party": party, "locked": locked,
                          "signed_parties": parties}
                if locked:
                    phases = self._fetch_plan_phases(connection, proposal_id)
                    conflicts, _ = self._evaluate_conflicts(connection, window, phases)
                    if conflicts:
                        result["locked"] = False
                        result["blocking_conflicts"] = conflicts
                        self._journal(connection, window_id=window["window_id"], event_type="lock.blocked",
                                      actor_id=actor_id, detail={"proposal_id": proposal_id,
                                                                "conflicts": conflicts})
                return "signature", f"{proposal_id}:{party}", result

            return self._idempotent(connection, request_id=request_id, action="sign_proposal",
                                    payload=payload, create=create)

    def confirm_lock(self, *, request_id: str, actor_id: str, proposal_id: str) -> dict[str, Any]:
        """三方会签齐后，在冲突解除时显式确认锁定资源。"""
        payload = {"actor_id": actor_id, "proposal_id": proposal_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "construction", "locality")
            proposal = connection.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            if proposal is None:
                raise NotFoundError("方案不存在")
            window = self._load_window(connection, proposal["window_id"])

            def create():
                fresh = connection.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
                if fresh["status"] == "locked":
                    return "proposal", proposal_id, {"proposal_id": proposal_id, "locked": True,
                                                     "revision": fresh["revision"]}
                if fresh["status"] != "draft":
                    raise ConflictError(f"方案状态为 {fresh['status']}，不能锁定")
                parties = {r["party"] for r in connection.execute(
                    "SELECT party FROM signatures WHERE proposal_id=?", (proposal_id,))}
                missing = [p for p in SIGN_PARTIES if p not in parties]
                if missing:
                    raise PreconditionFailed(f"会签未齐备，缺少: {missing}")
                phases = self._fetch_plan_phases(connection, proposal_id)
                completed_codes = {r["phase_code"] for r in connection.execute(
                    "SELECT phase_code FROM phase_executions WHERE window_id=? AND status='completed'",
                    (window["window_id"],)).fetchall()}
                open_phases = [p for p in phases if p["phase_code"] not in completed_codes]
                conflicts, tradeoffs = self._evaluate_conflicts(connection, window, open_phases)
                if conflicts:
                    self._journal(connection, window_id=window["window_id"], event_type="lock.blocked",
                                  actor_id=actor_id, detail={"proposal_id": proposal_id,
                                                            "conflicts": conflicts})
                    raise ConflictError("方案仍存在未解决冲突，不能锁定: "
                                        + "; ".join(c["message"] for c in conflicts))
                self._lock_proposal(connection, window=window, proposal=fresh, phases=phases, actor_id=actor_id)
                return "proposal", proposal_id, {"proposal_id": proposal_id, "locked": True,
                                                 "revision": fresh["revision"]}

            return self._idempotent(connection, request_id=request_id, action="confirm_lock",
                                    payload=payload, create=create)

    def _fetch_plan_phases(self, connection, proposal_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM phase_plans WHERE proposal_id=? ORDER BY ordinal", (proposal_id,)).fetchall()
        return [self._row_to_phase(row) for row in rows]

    def _row_to_phase(self, row) -> dict[str, Any]:
        keys = row.keys()
        return {"phase_code": row["phase_code"],
                "title": row["title"] if "title" in keys else row["phase_code"],
                "ordinal": row["ordinal"],
                "planned_start": row["planned_start"], "planned_end": row["planned_end"],
                "start_dt": self._dt(row["planned_start"], "planned_start"),
                "end_dt": self._dt(row["planned_end"], "planned_end"),
                "closure_scope": row["closure_scope"], "diverted_volume": row["diverted_volume"],
                "corridor_id": row["corridor_id"], "crew_id": row["crew_id"],
                "qualification": row["qualification"],
                "material_ids": json.loads(row["material_ids_json"]),
                "cost": row["cost"], "kind": row["kind"]}

    def _lock_proposal(self, connection, *, window, proposal, phases: list[dict[str, Any]],
                       actor_id: str) -> None:
        now = self._now()
        reschedule = window["status"] in ("locked", "in_progress")
        if reschedule:
            # 重排锁定：已完成阶段不可改变，只重建受影响阶段的租约
            completed_rows = connection.execute(
                "SELECT phase_code,planned_start,planned_end,closure_scope,diverted_volume,corridor_id,"
                "crew_id,qualification,material_ids_json,cost,kind,ordinal FROM phase_executions "
                "WHERE window_id=? AND status='completed'", (window["window_id"],)).fetchall()
            completed = {r["phase_code"]: self._row_to_phase(r) for r in completed_rows}
            for code, old in completed.items():
                new = next((p for p in phases if p["phase_code"] == code), None)
                if new is None:
                    raise ConflictError(f"已完工阶段 {code} 不能从方案中删除，历史停运与支付记录保持有效")
                for field in ("planned_start", "planned_end", "closure_scope", "diverted_volume",
                              "corridor_id", "crew_id", "qualification", "material_ids", "cost", "kind",
                              "ordinal"):
                    if old[field] != new[field]:
                        raise ConflictError(f"已完工阶段 {code} 的 {field} 不能重排")
            # 释放本窗口全部尚未结束的租约，随后只按未完工阶段重建；
            # 已完工阶段的资源在完工当时已经释放，不能因重排被重新租占
            connection.execute(
                "UPDATE leases SET status='released', released_at=? WHERE window_id=? AND status='active'",
                (now, window["window_id"]))
        completed_codes = {r["phase_code"] for r in connection.execute(
            "SELECT phase_code FROM phase_executions WHERE window_id=? AND status='completed'",
            (window["window_id"],)).fetchall()} if reschedule else set()
        open_phases = [p for p in phases if p["phase_code"] not in completed_codes]
        demands = self._proposal_demands(connection, window, open_phases)
        lease_rows = []
        for demand in demands:
            lease_rows.append((uuid.uuid4().hex, window["window_id"], proposal["proposal_id"],
                               demand["phase_code"], demand["resource_type"], demand["resource_id"],
                               demand["demand"], self._iso(demand["start_dt"]), self._iso(demand["end_dt"]),
                               "active", None, now))
        paid_total = connection.execute(
            "SELECT COALESCE(SUM(amount),0) AS total FROM payments WHERE window_id=?",
            (window["window_id"],)).fetchone()["total"]
        budget = sum(p["cost"] for p in phases)
        reserve = max(0.0, budget - paid_total)
        fund = connection.execute("SELECT * FROM funds WHERE fund_id=?", (window["fund_id"],)).fetchone()
        lease_rows.append((uuid.uuid4().hex, window["window_id"], proposal["proposal_id"], None,
                           "fund", window["fund_id"], reserve, now, fund["valid_until"],
                           "active", None, now))
        connection.executemany(
            "INSERT INTO leases(lease_id,window_id,proposal_id,phase_code,resource_type,resource_id,"
            "demand,leased_from,valid_until,status,released_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            lease_rows)
        connection.execute("UPDATE proposals SET status='locked' WHERE proposal_id=?",
                           (proposal["proposal_id"],))

        if not reschedule:
            for phase in phases:
                connection.execute(
                    "INSERT INTO phase_executions(window_id,phase_code,proposal_id,revision,status,ordinal,"
                    "planned_start,planned_end,closure_scope,diverted_volume,corridor_id,crew_id,qualification,"
                    "material_ids_json,cost,kind) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (window["window_id"], phase["phase_code"], proposal["proposal_id"], proposal["revision"],
                     "scheduled", phase["ordinal"], phase["planned_start"], phase["planned_end"],
                     phase["closure_scope"], phase["diverted_volume"], phase["corridor_id"], phase["crew_id"],
                     phase["qualification"], canonical_json(phase["material_ids"]), phase["cost"], phase["kind"]))
            connection.execute(
                "UPDATE renewal_windows SET status='locked',active_revision=?,updated_at=? WHERE window_id=?",
                (proposal["revision"], now, window["window_id"]))
        else:
            self._apply_reschedule(connection, window=window, proposal=proposal, phases=phases, actor_id=actor_id)
        self._journal(connection, window_id=window["window_id"],
                      event_type="window.reschedule_locked" if reschedule else "window.locked",
                      actor_id=actor_id, detail={"proposal_id": proposal["proposal_id"],
                                                 "revision": proposal["revision"],
                                                 "lease_count": len(lease_rows)})
        self._audit(connection, actor_id=actor_id,
                    action="window.reschedule_locked" if reschedule else "window.locked",
                    resource_type="renewal_window", resource_id=window["window_id"],
                    detail={"proposal_id": proposal["proposal_id"], "revision": proposal["revision"],
                            "leases": len(lease_rows)})

    def _apply_reschedule(self, connection, *, window, proposal, phases: list[dict[str, Any]],
                          actor_id: str) -> None:
        existing = {r["phase_code"]: r for r in connection.execute(
            "SELECT * FROM phase_executions WHERE window_id=?", (window["window_id"],))}
        affected = []
        for phase in phases:
            row = existing.get(phase["phase_code"])
            if row is None:
                connection.execute(
                    "INSERT INTO phase_executions(window_id,phase_code,proposal_id,revision,status,ordinal,"
                    "planned_start,planned_end,closure_scope,diverted_volume,corridor_id,crew_id,qualification,"
                    "material_ids_json,cost,kind) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (window["window_id"], phase["phase_code"], proposal["proposal_id"], proposal["revision"],
                     "scheduled", phase["ordinal"], phase["planned_start"], phase["planned_end"],
                     phase["closure_scope"], phase["diverted_volume"], phase["corridor_id"], phase["crew_id"],
                     phase["qualification"], canonical_json(phase["material_ids"]), phase["cost"], phase["kind"]))
                affected.append({"phase_code": phase["phase_code"], "change": "inserted", "kind": phase["kind"]})
                continue
            if row["status"] == "completed":
                continue
            changed = (row["planned_start"] != phase["planned_start"]
                       or row["planned_end"] != phase["planned_end"]
                       or row["closure_scope"] != phase["closure_scope"]
                       or row["diverted_volume"] != phase["diverted_volume"]
                       or row["corridor_id"] != phase["corridor_id"]
                       or row["crew_id"] != phase["crew_id"]
                       or row["qualification"] != phase["qualification"]
                       or row["material_ids_json"] != canonical_json(phase["material_ids"])
                       or row["cost"] != phase["cost"] or row["ordinal"] != phase["ordinal"]
                       or row["kind"] != phase["kind"])
            new_status = "scheduled" if row["status"] in ("partially_completed", "acceptance_rejected") \
                else row["status"]
            connection.execute(
                "UPDATE phase_executions SET proposal_id=?,revision=?,ordinal=?,planned_start=?,planned_end=?,"
                "closure_scope=?,diverted_volume=?,corridor_id=?,crew_id=?,qualification=?,"
                "material_ids_json=?,cost=?,kind=?,status=? WHERE window_id=? AND phase_code=?",
                (proposal["proposal_id"], proposal["revision"], phase["ordinal"], phase["planned_start"],
                 phase["planned_end"], phase["closure_scope"], phase["diverted_volume"], phase["corridor_id"],
                 phase["crew_id"], phase["qualification"], canonical_json(phase["material_ids"]),
                 phase["cost"], phase["kind"], new_status, window["window_id"], phase["phase_code"]))
            if changed:
                affected.append({"phase_code": phase["phase_code"], "change": "rescheduled",
                                 "status": new_status,
                                 "planned_start": phase["planned_start"], "planned_end": phase["planned_end"]})
        connection.execute(
            "UPDATE renewal_windows SET active_revision=?,updated_at=? WHERE window_id=?",
            (proposal["revision"], self._now(), window["window_id"]))
        self._journal(connection, window_id=window["window_id"], event_type="window.rescheduled",
                      actor_id=actor_id, detail={"proposal_id": proposal["proposal_id"],
                                                 "revision": proposal["revision"], "reason": proposal["reason"],
                                                 "affected_phases": affected})
        self._audit(connection, actor_id=actor_id, action="window.rescheduled",
                    resource_type="renewal_window", resource_id=window["window_id"],
                    detail={"proposal_id": proposal["proposal_id"], "affected": affected})

    # ------------------------------------------------------------------ 重排：延期 / 部分完工 / 退回 / 紧急抢修

    def _current_phases(self, connection, window) -> list[dict[str, Any]]:
        proposal_id = window["effective_proposal_id"]
        if not proposal_id:
            raise ConflictError("窗口尚无生效方案")
        return self._fetch_plan_phases(connection, proposal_id)

    def _create_reschedule(self, *, request_id: str, actor_id: str, action: str,
                           payload: dict[str, Any], connection, window, phases: list[dict[str, Any]],
                           reason: str, ttl_minutes: int, base_revision: int | None,
                           tradeoff_note: dict[str, Any]) -> dict[str, Any]:
        phases.sort(key=lambda p: p["ordinal"])
        conflicts, tradeoffs = self._evaluate_conflicts(connection, window, phases)
        tradeoffs.insert(0, tradeoff_note)

        def create():
            fresh_window = self._load_window(connection, window["window_id"])
            result = self._persist_proposal(
                connection, window=fresh_window, phases=phases,
                base_revision=fresh_window["active_revision"] if base_revision is None else int(base_revision),
                reason=reason, ttl_minutes=int(ttl_minutes), created_by=actor_id,
                conflicts=conflicts, tradeoffs=tradeoffs)
            return "proposal", result["proposal_id"], result

        return self._idempotent(connection, request_id=request_id, action=action,
                                payload=payload, create=create)

    def delay_phase(self, *, request_id: str, actor_id: str, window_id: str, phase_code: str,
                    new_start: str, ttl_minutes: int = 1440, base_revision: int | None = None) -> dict[str, Any]:
        """阶段延期：受影响阶段及其后续未完工阶段整体顺延，已完工阶段不动。"""
        payload = {"actor_id": actor_id, "window_id": window_id, "phase_code": phase_code,
                   "new_start": new_start, "base_revision": base_revision}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "construction")
            window = self._load_window(connection, window_id)
            if window["status"] not in ("locked", "in_progress"):
                raise ConflictError("只有会签锁定后的窗口可以申请延期")
            phase_code = self._text(phase_code, "phase_code", 64)
            target_start = self._dt(new_start, "new_start")
            phases = self._current_phases(connection, window)
            target = next((p for p in phases if p["phase_code"] == phase_code), None)
            if target is None:
                raise NotFoundError("阶段不存在")
            target_exec = connection.execute(
                "SELECT status FROM phase_executions WHERE window_id=? AND phase_code=?",
                (window_id, phase_code)).fetchone()
            if target_exec["status"] == "completed":
                raise ConflictError("阶段已完工，不能延期；历史停运与支付记录不可抹除")
            delta = target_start - target["start_dt"]
            if delta.total_seconds() <= 0:
                raise ValidationError("延期后的开始时间必须晚于原计划开始时间")
            shifted: list[str] = []
            for phase in phases:
                ex = connection.execute("SELECT status FROM phase_executions WHERE window_id=? AND phase_code=?",
                                        (window_id, phase["phase_code"])).fetchone()
                if ex["status"] == "completed" or phase["ordinal"] < target["ordinal"]:
                    continue
                phase["start_dt"] += delta
                phase["end_dt"] += delta
                phase["planned_start"] = self._iso(phase["start_dt"])
                phase["planned_end"] = self._iso(phase["end_dt"])
                shifted.append(phase["phase_code"])
            return self._create_reschedule(
                request_id=request_id, actor_id=actor_id, action="delay_phase", payload=payload,
                connection=connection, window=window, phases=phases,
                reason=f"delay:{phase_code}", ttl_minutes=ttl_minutes, base_revision=base_revision,
                tradeoff_note={"type": "cascade_delay", "phases": shifted,
                               "delta_minutes": delta.total_seconds() / 60,
                               "message": f"阶段 {phase_code} 延期后，受影响的后续未完工阶段 "
                                          f"{shifted} 自动顺延；已完工阶段与历史记录不变"})

    def reschedule_phase(self, *, request_id: str, actor_id: str, window_id: str, phase_code: str,
                         changes: dict[str, Any], ttl_minutes: int = 1440,
                         base_revision: int | None = None) -> dict[str, Any]:
        """部分完工或验收退回后，只重排受影响阶段，后续阶段按工序最小顺延。"""
        payload = {"actor_id": actor_id, "window_id": window_id, "phase_code": phase_code,
                   "changes": changes, "base_revision": base_revision}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "construction")
            window = self._load_window(connection, window_id)
            if window["status"] not in ("locked", "in_progress"):
                raise ConflictError("窗口未锁定，不能按完工/验收结果重排")
            phase_code = self._text(phase_code, "phase_code", 64)
            reason = self._text((changes or {}).get("reason", f"rework:{phase_code}"), "reason")
            phases = self._current_phases(connection, window)
            target = next((p for p in phases if p["phase_code"] == phase_code), None)
            if target is None:
                raise NotFoundError("阶段不存在")
            ex = connection.execute("SELECT * FROM phase_executions WHERE window_id=? AND phase_code=?",
                                    (window_id, phase_code)).fetchone()
            if ex["status"] not in ("scheduled", "partially_completed", "acceptance_rejected"):
                raise ConflictError(f"阶段状态 {ex['status']} 不允许重排（进行中需先报验，已完工不可改）")
            new_start = self._dt(changes["planned_start"], "changes.planned_start") \
                if changes.get("planned_start") else target["start_dt"]
            new_end = self._dt(changes["planned_end"], "changes.planned_end") \
                if changes.get("planned_end") else target["end_dt"]
            if new_end <= new_start:
                raise ValidationError("结束时间必须晚于开始时间")
            target["start_dt"], target["end_dt"] = new_start, new_end
            target["planned_start"], target["planned_end"] = self._iso(new_start), self._iso(new_end)
            if changes.get("closure_scope"):
                target["closure_scope"] = self._text(changes["closure_scope"], "changes.closure_scope")
            if changes.get("diverted_volume") is not None:
                target["diverted_volume"] = self._number(changes["diverted_volume"], "changes.diverted_volume")
            if changes.get("crew_id"):
                target["crew_id"] = self._text(changes["crew_id"], "changes.crew_id", 64)
                crew = connection.execute("SELECT * FROM crews WHERE crew_id=?", (target["crew_id"],)).fetchone()
                if crew is None:
                    raise NotFoundError("队伍不存在")
                if changes.get("qualification"):
                    target["qualification"] = self._text(changes["qualification"], "changes.qualification", 64)
                if target["qualification"] not in json.loads(crew["qualifications_json"]):
                    raise ValidationError(f"队伍 {target['crew_id']} 缺少资质 {target['qualification']}")

            # 工序约束：按 ordinal 保证不与已完工阶段重叠，仅推动受影响的后续阶段
            affected = [phase_code]
            ordered = sorted(phases, key=lambda p: p["ordinal"])
            previous_end = None
            for phase in ordered:
                row = connection.execute("SELECT status FROM phase_executions WHERE window_id=? AND phase_code=?",
                                         (window_id, phase["phase_code"])).fetchone()
                if row["status"] == "completed":
                    previous_end = phase["end_dt"]
                    continue
                if previous_end and phase["start_dt"] < previous_end:
                    shift = previous_end - phase["start_dt"]
                    phase["start_dt"] += shift
                    phase["end_dt"] += shift
                    phase["planned_start"] = self._iso(phase["start_dt"])
                    phase["planned_end"] = self._iso(phase["end_dt"])
                    if phase["phase_code"] != phase_code:
                        affected.append(phase["phase_code"])
                previous_end = phase["end_dt"]
            return self._create_reschedule(
                request_id=request_id, actor_id=actor_id, action="reschedule_phase", payload=payload,
                connection=connection, window=window, phases=phases,
                reason=reason, ttl_minutes=ttl_minutes, base_revision=base_revision,
                tradeoff_note={"type": "affected_only", "phases": affected,
                               "message": "仅重排受影响阶段并按工序最小顺延；已发生的停运记录与支付记录不可抹除"})

    def emergency_repair(self, *, request_id: str, actor_id: str, window_id: str, phase: dict[str, Any],
                         ttl_minutes: int = 360, base_revision: int | None = None) -> dict[str, Any]:
        """紧急抢修：只插入抢修阶段，其余阶段保持原计划，冲突时走限时会签重排。"""
        payload = {"actor_id": actor_id, "window_id": window_id, "phase": phase,
                   "base_revision": base_revision}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            window = self._load_window(connection, window_id)
            if window["status"] not in ("locked", "in_progress"):
                raise ConflictError("窗口未锁定，不能发起紧急抢修")
            raw = dict(phase or {})
            raw.setdefault("kind", "emergency")
            emergency = self._normalize_phase(connection, raw, 0)

            def create():
                fresh_window = self._load_window(connection, window_id)
                phases = self._current_phases(connection, fresh_window)
                existing_index = next((i for i, p in enumerate(phases)
                                       if p["phase_code"] == emergency["phase_code"]), None)
                if existing_index is not None:
                    executed = connection.execute(
                        "SELECT 1 FROM phase_executions WHERE window_id=? AND phase_code=?",
                        (window_id, emergency["phase_code"])).fetchone()
                    if executed:
                        raise ConflictError("抢修阶段编号已存在且已进入执行，不能覆盖")
                    # 限时草案会签前允许修正同一抢修阶段
                    phases[existing_index] = emergency
                else:
                    phases.append(emergency)
                phases.sort(key=lambda p: (p["start_dt"], p["phase_code"]))
                for ordinal, item in enumerate(phases, start=1):
                    item["ordinal"] = ordinal
                conflicts, tradeoffs = self._evaluate_conflicts(connection, fresh_window, phases)
                tradeoffs.insert(0, {"type": "emergency_only", "phase_code": emergency["phase_code"],
                                     "message": "紧急抢修只插入受影响阶段；与新抢修时段冲突的后续阶段按工序顺延，"
                                                "其余阶段保持原计划"})
                result = self._persist_proposal(
                    connection, window=fresh_window, phases=phases,
                    base_revision=fresh_window["active_revision"] if base_revision is None else int(base_revision),
                    reason=f"emergency:{emergency['phase_code']}", ttl_minutes=int(ttl_minutes),
                    created_by=actor_id, conflicts=conflicts, tradeoffs=tradeoffs)
                self._journal(connection, window_id=window_id, event_type="emergency.declared",
                              actor_id=actor_id, detail={"proposal_id": result["proposal_id"],
                                                         "phase_code": emergency["phase_code"]})
                self._audit(connection, actor_id=actor_id, action="emergency.declared",
                            resource_type="proposal", resource_id=result["proposal_id"],
                            detail={"window_id": window_id, "phase_code": emergency["phase_code"]})
                return "proposal", result["proposal_id"], result

            return self._idempotent(connection, request_id=request_id, action="emergency_repair",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 阶段执行

    def _require_phase_leases(self, connection, *, window_id: str, phase: dict[str, Any]) -> None:
        now = self._now_dt()
        resource_types = ["crew", "facility", "material"]
        if phase["corridor_id"]:
            resource_types.append("corridor")
        placeholders = ",".join("?" for _ in resource_types)
        rows = connection.execute(
            f"SELECT * FROM leases WHERE window_id=? AND phase_code=? AND status='active' "
            f"AND resource_type IN ({placeholders})",
            [window_id, phase["phase_code"], *resource_types]).fetchall()
        seen = {(r["resource_type"], r["resource_id"]) for r in rows}
        expected = {("crew", phase["crew_id"])}
        for material_id in json.loads(phase["material_ids_json"]):
            expected.add(("material", material_id))
        if phase["corridor_id"]:
            expected.add(("corridor", phase["corridor_id"]))
        if not rows or expected - seen:
            raise PreconditionFailed("阶段资源租约不完整，资源未锁定或已释放")
        for row in rows:
            if now >= self._dt(row["valid_until"], "lease.valid_until"):
                raise ExpiredError(f"资源 {row['resource_type']}:{row['resource_id']} 的租约已到期，"
                                   "请先续租再推进阶段")

    def start_phase(self, *, request_id: str, actor_id: str, window_id: str, phase_code: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "window_id": window_id, "phase_code": phase_code}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "construction")
            window = self._load_window(connection, window_id)
            if window["status"] not in ("locked", "in_progress"):
                raise ConflictError("窗口资源尚未锁定，不能开工")
            phase_code = self._text(phase_code, "phase_code", 64)

            def create():
                phase = connection.execute("SELECT * FROM phase_executions WHERE window_id=? AND phase_code=?",
                                           (window_id, phase_code)).fetchone()
                if phase is None:
                    raise NotFoundError("阶段不存在")
                if phase["status"] != "scheduled":
                    raise ConflictError(f"阶段当前状态 {phase['status']}，不能开工")
                prior = connection.execute(
                    "SELECT phase_code,status FROM phase_executions WHERE window_id=? AND ordinal<? "
                    "ORDER BY ordinal", (window_id, phase["ordinal"])).fetchall()
                unfinished = [r["phase_code"] for r in prior if r["status"] != "completed"]
                if unfinished:
                    raise PreconditionFailed(f"前置阶段尚未完工: {unfinished}")
                self._require_phase_leases(connection, window_id=window_id, phase=phase)
                fund = connection.execute("SELECT * FROM funds WHERE fund_id=?", (window["fund_id"],)).fetchone()
                if self._now_dt() > self._dt(fund["valid_until"], "fund.valid_until"):
                    raise PreconditionFailed("专项资金已超过使用期限，不能开工")
                connection.execute(
                    "UPDATE phase_executions SET status='in_progress',actual_start=COALESCE(actual_start,?) "
                    "WHERE window_id=? AND phase_code=?", (self._now(), window_id, phase_code))
                outage_id = "out-" + uuid.uuid4().hex[:12]
                connection.execute(
                    "INSERT INTO outage_records(outage_id,window_id,phase_code,facility_id,kind,scope,"
                    "diverted_volume,occurred_at,actor_id,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (outage_id, window_id, phase_code, window["facility_id"], phase["kind"],
                     phase["closure_scope"], phase["diverted_volume"], self._now(), actor_id,
                     f"替代通道: {phase['corridor_id'] or '未占用'}"))
                if window["status"] == "locked":
                    connection.execute(
                        "UPDATE renewal_windows SET status='in_progress',updated_at=? WHERE window_id=?",
                        (self._now(), window_id))
                self._journal(connection, window_id=window_id, event_type="phase.started", actor_id=actor_id,
                              detail={"phase_code": phase_code, "outage_id": outage_id,
                                      "closure_scope": phase["closure_scope"],
                                      "diverted_volume": phase["diverted_volume"]})
                self._audit(connection, actor_id=actor_id, action="phase.started",
                            resource_type="phase", resource_id=f"{window_id}:{phase_code}",
                            detail={"outage_id": outage_id})
                return "phase_execution", f"{window_id}:{phase_code}", {
                    "window_id": window_id, "phase_code": phase_code, "status": "in_progress",
                    "outage_id": outage_id}

            return self._idempotent(connection, request_id=request_id, action="start_phase",
                                    payload=payload, create=create)

    def complete_phase(self, *, request_id: str, actor_id: str, window_id: str, phase_code: str,
                       partial: bool = False, amount: float | None = None) -> dict[str, Any]:
        """报验完工或部分完工；支付记录只增不改，并释放该阶段占用的资源租约。"""
        payload = {"actor_id": actor_id, "window_id": window_id, "phase_code": phase_code,
                   "partial": partial, "amount": amount}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "construction", "operator")
            window = self._load_window(connection, window_id)
            phase_code = self._text(phase_code, "phase_code", 64)

            def create():
                phase = connection.execute("SELECT * FROM phase_executions WHERE window_id=? AND phase_code=?",
                                           (window_id, phase_code)).fetchone()
                if phase is None:
                    raise NotFoundError("阶段不存在")
                if phase["status"] != "in_progress":
                    raise ConflictError(f"阶段当前状态 {phase['status']}，不能报验")
                paid = connection.execute(
                    "SELECT COALESCE(SUM(amount),0) AS total FROM payments WHERE window_id=? AND phase_code=?",
                    (window_id, phase_code)).fetchone()["total"]
                if partial:
                    if amount is None:
                        raise ValidationError("部分完工必须申报本次支付金额")
                    pay_amount = self._number(amount, "amount", positive=True)
                    if paid + pay_amount > phase["cost"] + 1e-9:
                        raise ConflictError("部分完工累计支付不能超过阶段造价")
                    new_status = "partially_completed"
                else:
                    pay_amount = phase["cost"] - paid
                    if pay_amount < -1e-9:
                        raise ConflictError("阶段累计支付已超过造价")
                    new_status = "completed"
                connection.execute(
                    "UPDATE phase_executions SET status=?,actual_end=? WHERE window_id=? AND phase_code=?",
                    (new_status, self._now(), window_id, phase_code))
                payment_id = None
                if pay_amount > 1e-9:
                    payment_id = "pay-" + uuid.uuid4().hex[:12]
                    milestone = ("phase.partial:" if partial else "phase.completed:") + phase_code
                    connection.execute(
                        "INSERT INTO payments(payment_id,window_id,fund_id,phase_code,amount,milestone,"
                        "paid_by,paid_at) VALUES(?,?,?,?,?,?,?,?)",
                        (payment_id, window_id, window["fund_id"], phase_code, pay_amount, milestone,
                         actor_id, self._now()))
                released = connection.execute(
                    "UPDATE leases SET status='released',released_at=? WHERE window_id=? AND status='active' "
                    "AND phase_code=?", (self._now(), window_id, phase_code)).rowcount
                self._journal(connection, window_id=window_id,
                              event_type="phase.partially_completed" if partial else "phase.completed",
                              actor_id=actor_id,
                              detail={"phase_code": phase_code, "payment_id": payment_id,
                                      "paid_amount": pay_amount, "paid_total": paid + pay_amount,
                                      "released_leases": released})
                self._audit(connection, actor_id=actor_id,
                            action="phase.partially_completed" if partial else "phase.completed",
                            resource_type="phase", resource_id=f"{window_id}:{phase_code}",
                            detail={"payment_id": payment_id, "amount": pay_amount})
                return "phase_execution", f"{window_id}:{phase_code}", {
                    "window_id": window_id, "phase_code": phase_code, "status": new_status,
                    "payment_id": payment_id, "paid_amount": pay_amount, "released_leases": released}

            return self._idempotent(connection, request_id=request_id,
                                    action="complete_phase_partial" if partial else "complete_phase",
                                    payload=payload, create=create)

    def reject_phase(self, *, request_id: str, actor_id: str, window_id: str, phase_code: str,
                     note: str = "") -> dict[str, Any]:
        """验收退回：已支付款项保留，阶段回到待重排，须重新形成限时草案并会签。"""
        payload = {"actor_id": actor_id, "window_id": window_id, "phase_code": phase_code, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            window = self._load_window(connection, window_id)
            phase_code = self._text(phase_code, "phase_code", 64)

            def create():
                phase = connection.execute("SELECT * FROM phase_executions WHERE window_id=? AND phase_code=?",
                                           (window_id, phase_code)).fetchone()
                if phase is None:
                    raise NotFoundError("阶段不存在")
                if phase["status"] != "completed":
                    raise ConflictError(f"阶段当前状态 {phase['status']}，只有已报验完工的阶段可被验收退回")
                connection.execute(
                    "UPDATE phase_executions SET status='acceptance_rejected' WHERE window_id=? AND phase_code=?",
                    (window_id, phase_code))
                self._journal(connection, window_id=window_id, event_type="phase.acceptance_rejected",
                              actor_id=actor_id, detail={"phase_code": phase_code, "note": note,
                                                         "payments_retained": True,
                                                         "outages_retained": True})
                self._audit(connection, actor_id=actor_id, action="phase.acceptance_rejected",
                            resource_type="phase", resource_id=f"{window_id}:{phase_code}",
                            detail={"note": note})
                return "phase_execution", f"{window_id}:{phase_code}", {
                    "window_id": window_id, "phase_code": phase_code,
                    "status": "acceptance_rejected"}

            return self._idempotent(connection, request_id=request_id, action="reject_phase",
                                    payload=payload, create=create)

    def renew_leases(self, *, request_id: str, actor_id: str, window_id: str, until: str) -> dict[str, Any]:
        """为窗口尚未结束的租约续期，续期上限受专项资金期限约束。"""
        payload = {"actor_id": actor_id, "window_id": window_id, "until": until}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "construction")
            window = self._load_window(connection, window_id)
            if window["status"] not in ("locked", "in_progress"):
                raise ConflictError("窗口没有活动租约，无需续期")
            requested = self._dt(until, "until")
            if requested <= self._now_dt():
                raise ValidationError("续期截止时间必须晚于当前时间")

            def create():
                fund = connection.execute("SELECT * FROM funds WHERE fund_id=?", (window["fund_id"],)).fetchone()
                cap = self._dt(fund["valid_until"], "fund.valid_until")
                until_iso = self._iso(min(requested, cap))
                result = connection.execute(
                    "UPDATE leases SET valid_until=? WHERE window_id=? AND status='active' AND valid_until<?",
                    (until_iso, window_id, until_iso))
                self._journal(connection, window_id=window_id, event_type="leases.renewed", actor_id=actor_id,
                              detail={"requested_until": self._iso(requested), "until": until_iso,
                                      "renewed": result.rowcount,
                                      "capped_by_fund_deadline": requested > cap})
                self._audit(connection, actor_id=actor_id, action="leases.renewed",
                            resource_type="renewal_window", resource_id=window_id,
                            detail={"until": until_iso, "count": result.rowcount})
                return "renewal_window", window_id, {"window_id": window_id, "renewed": result.rowcount,
                                                     "valid_until": until_iso}

            return self._idempotent(connection, request_id=request_id, action="renew_leases",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 复开

    def reopen(self, *, request_id: str, actor_id: str, window_id: str) -> dict[str, Any]:
        """全部阶段完工且无待签草案后，释放剩余租约并恢复通行。"""
        payload = {"actor_id": actor_id, "window_id": window_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            window = self._load_window(connection, window_id)

            def create():
                if window["status"] not in ("locked", "in_progress"):
                    raise ConflictError(f"窗口状态为 {window['status']}，不能复开")
                phases = connection.execute(
                    "SELECT phase_code,status FROM phase_executions WHERE window_id=?", (window_id,)).fetchall()
                unmet = [f"阶段 {r['phase_code']} 状态为 {r['status']}" for r in phases
                         if r["status"] != "completed"]
                if connection.execute("SELECT 1 FROM proposals WHERE window_id=? AND status='draft'",
                                      (window_id,)).fetchone():
                    unmet.append("存在尚未会签锁定的限时重排草案")
                if unmet:
                    raise PreconditionFailed("恢复通行前置条件未满足: " + "; ".join(unmet))
                released = connection.execute(
                    "UPDATE leases SET status='released',released_at=? WHERE window_id=? AND status='active'",
                    (self._now(), window_id)).rowcount
                connection.execute(
                    "UPDATE renewal_windows SET status='reopened',updated_at=? WHERE window_id=?",
                    (self._now(), window_id))
                self._journal(connection, window_id=window_id, event_type="window.reopened", actor_id=actor_id,
                              detail={"released_leases": released, "outage_records_immutable": True,
                                      "payment_records_immutable": True})
                self._audit(connection, actor_id=actor_id, action="window.reopened",
                            resource_type="renewal_window", resource_id=window_id,
                            detail={"released_leases": released})
                return "renewal_window", window_id, {"window_id": window_id, "status": "reopened",
                                                     "released_leases": released}

            return self._idempotent(connection, request_id=request_id, action="reopen_window",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 查询、重放与重启恢复

    def get_window(self, window_id: str) -> dict[str, Any]:
        connection = self.database.connection
        window = connection.execute("SELECT * FROM renewal_windows WHERE window_id=?", (window_id,)).fetchone()
        if window is None:
            raise NotFoundError("更新窗口不存在")
        proposal = None
        if window["effective_proposal_id"]:
            row = connection.execute("SELECT * FROM proposals WHERE proposal_id=?",
                                     (window["effective_proposal_id"],)).fetchone()
            signatures = [{"party": r["party"], "actor_id": r["actor_id"], "signed_at": r["signed_at"]}
                          for r in connection.execute(
                              "SELECT * FROM signatures WHERE proposal_id=? ORDER BY party",
                              (row["proposal_id"],))]
            proposal = {"proposal_id": row["proposal_id"], "revision": row["revision"],
                        "base_revision": row["base_revision"], "status": row["status"],
                        "reason": row["reason"], "ttl_expires_at": row["ttl_expires_at"],
                        "budget": row["budget"], "conflicts": json.loads(row["conflicts_json"]),
                        "tradeoffs": json.loads(row["tradeoffs_json"]), "signatures": signatures}
        phases = [{"phase_code": r["phase_code"], "status": r["status"], "ordinal": r["ordinal"],
                   "planned_start": r["planned_start"], "planned_end": r["planned_end"],
                   "actual_start": r["actual_start"], "actual_end": r["actual_end"],
                   "crew_id": r["crew_id"], "corridor_id": r["corridor_id"], "kind": r["kind"],
                   "cost": r["cost"]}
                  for r in connection.execute(
                      "SELECT * FROM phase_executions WHERE window_id=? ORDER BY ordinal", (window_id,))]
        return {"window_id": window["window_id"], "facility_id": window["facility_id"],
                "fund_id": window["fund_id"], "title": window["title"], "status": window["status"],
                "active_revision": window["active_revision"], "effective_proposal": proposal,
                "phases": phases}

    def timeline(self, window_id: str) -> dict[str, Any]:
        """重放窗口从申报到复开的全过程，含每次冲突、取舍、停运与支付凭据。"""
        connection = self.database.connection
        if connection.execute("SELECT 1 FROM renewal_windows WHERE window_id=?", (window_id,)).fetchone() is None:
            raise NotFoundError("更新窗口不存在")
        events = [{"sequence": r["sequence"], "event_type": r["event_type"], "actor_id": r["actor_id"],
                   "event_at": r["event_at"], "detail": json.loads(r["detail_json"])}
                  for r in connection.execute(
                      "SELECT * FROM window_journal WHERE window_id=? ORDER BY sequence", (window_id,))]
        outages = [dict(r) for r in map(dict, connection.execute(
            "SELECT outage_id,phase_code,facility_id,kind,scope,diverted_volume,occurred_at,actor_id,note "
            "FROM outage_records WHERE window_id=? ORDER BY occurred_at", (window_id,)).fetchall())]
        payments = [dict(r) for r in map(dict, connection.execute(
            "SELECT payment_id,phase_code,amount,milestone,paid_by,paid_at FROM payments "
            "WHERE window_id=? ORDER BY paid_at", (window_id,)).fetchall())]
        return {"window_id": window_id, "events": events, "outage_records": outages,
                "payment_records": payments}

    def calendar(self, start: str | None = None, end: str | None = None) -> dict[str, Any]:
        """统一日历：活动租约占用、专项资金期限与窗口状态。"""
        connection = self.database.connection
        query = "SELECT * FROM leases WHERE status='active'"
        parameters: list[Any] = []
        if start:
            query += " AND valid_until>?"
            parameters.append(start)
        if end:
            query += " AND leased_from<?"
            parameters.append(end)
        leases = [dict(r) for r in map(dict, connection.execute(
            query + " ORDER BY leased_from", parameters).fetchall())]
        funds = [dict(r) for r in map(dict, connection.execute(
            "SELECT fund_id,name,amount,valid_until FROM funds ORDER BY valid_until").fetchall())]
        windows = [dict(r) for r in map(dict, connection.execute(
            "SELECT window_id,facility_id,fund_id,status,active_revision FROM renewal_windows "
            "ORDER BY window_id").fetchall())]
        return {"as_of": self._now(), "leases": leases, "funds": funds, "windows": windows}

    def recover(self) -> dict[str, Any]:
        """服务重启后恢复尚未结束的租约与审批：期限全部以持久化时间戳为准。"""
        connection = self.database.connection
        now = self._now_dt()
        report_windows = []
        expired_drafts = 0
        for window in connection.execute(
                "SELECT * FROM renewal_windows WHERE status IN ('draft','locked','in_progress') "
                "ORDER BY window_id"):
            drafts = []
            for proposal in connection.execute(
                    "SELECT * FROM proposals WHERE window_id=? AND status IN ('draft','locked') "
                    "ORDER BY revision", (window["window_id"],)):
                overdue = proposal["status"] == "draft" and now > self._dt(proposal["ttl_expires_at"], "ttl")
                if overdue:
                    connection.execute("UPDATE proposals SET status='expired' WHERE proposal_id=?",
                                       (proposal["proposal_id"],))
                    expired_drafts += 1
                    self._journal(connection, window_id=window["window_id"], event_type="proposal.expired",
                                  actor_id="system", detail={"proposal_id": proposal["proposal_id"],
                                                             "reason": "restart_recovery"})
                    status = "expired"
                else:
                    status = proposal["status"]
                signatures = [r["party"] for r in connection.execute(
                    "SELECT party FROM signatures WHERE proposal_id=? ORDER BY party",
                    (proposal["proposal_id"],))]
                drafts.append({"proposal_id": proposal["proposal_id"], "revision": proposal["revision"],
                               "status": status, "ttl_expires_at": proposal["ttl_expires_at"],
                               "signatures": signatures,
                               "missing_parties": [p for p in SIGN_PARTIES if p not in signatures]
                               if status == "draft" else []})
            leases = []
            for lease in connection.execute(
                    "SELECT * FROM leases WHERE window_id=? AND status='active' ORDER BY leased_from",
                    (window["window_id"],)):
                leases.append({"lease_id": lease["lease_id"], "phase_code": lease["phase_code"],
                               "resource_type": lease["resource_type"], "resource_id": lease["resource_id"],
                               "demand": lease["demand"], "leased_from": lease["leased_from"],
                               "valid_until": lease["valid_until"],
                               "expired": now >= self._dt(lease["valid_until"], "lease.valid_until")})
            report_windows.append({"window_id": window["window_id"], "status": window["status"],
                                   "active_revision": window["active_revision"],
                                   "proposals": drafts,
                                   "leases": leases,
                                   "leases_expired": [l["lease_id"] for l in leases if l["expired"]]})
        return {"as_of": self._now(), "expired_drafts": expired_drafts,
                "active_windows": report_windows}
