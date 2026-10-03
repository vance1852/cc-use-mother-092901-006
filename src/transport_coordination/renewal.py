"""设施更新窗口编排平台。

在基础服务的主体、场所、幂等回执与哈希审计能力之上，为老旧桥梁、隧道机电和客运站
设备的集中更新提供统一日历、限时草案、三方会签、资源租约、受影响阶段重排以及从
申报到复开的全过程重放。

关键规则：
- 草案限时：申报后须在会签期限内完成三方会签并锁定资源，逾期草案作废；
- 资源互斥：专业队伍同一时刻只能服务一个窗口，替代通道同期运力不得叠加超限，
  设施依赖的替代设施在窗口期内不得另有封闭计划；
- 调整受控：延期、部分完工、验收退回、紧急抢修只能重排受影响阶段，已发生的停运
  与支付记录只增不改；同一窗口的并发调整按版本号裁决，最多一个方案生效；
- 前置条件：释放租约要求阶段验收通过，恢复通行要求全部阶段验收、无未结束停运、
  租约全部释放；
- 可恢复：全部状态落在 SQLite，服务重启后未结束的租约与审批继续参与约束。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor
from .storage import Database


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

FACILITY_TYPES = frozenset({"bridge", "tunnel_electromech", "passenger_station"})

PARTIES = ("operations", "construction", "local_manager")
PARTY_LABELS = {"operations": "运营", "construction": "施工", "local_manager": "属地管理"}

PLAN_OPEN_STATUSES = frozenset({"draft", "approved", "locked", "in_progress"})

ADJUSTMENT_KINDS = frozenset({"delay", "partial_complete", "acceptance_reject", "emergency_repair"})
ADJUSTMENT_ROLES = {
    "delay": ("planner", "construction"),
    "partial_complete": ("construction",),
    "acceptance_reject": ("planner",),
    "emergency_repair": ("planner", "operations", "local_manager"),
}

DEFAULT_DRAFT_TTL_HOURS = 72
MAX_DRAFT_TTL_HOURS = 720

SCHEMA = """
CREATE TABLE IF NOT EXISTS renewal_facilities (
    facility_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    facility_type TEXT NOT NULL,
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS renewal_routes (
    route_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK(capacity > 0),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS renewal_crews (
    crew_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    qualifications_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS renewal_funds (
    fund_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK(amount >= 0),
    deadline TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS renewal_materials (
    material_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    arrival_date TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS renewal_plans (
    plan_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    facility_id TEXT NOT NULL REFERENCES renewal_facilities(facility_id),
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','approved','locked','in_progress','expired','reopened')),
    version INTEGER NOT NULL CHECK(version >= 1),
    draft_expires_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS renewal_phases (
    plan_id TEXT NOT NULL REFERENCES renewal_plans(plan_id),
    phase_id TEXT NOT NULL,
    seq INTEGER NOT NULL CHECK(seq >= 1),
    name TEXT NOT NULL,
    planned_start TEXT NOT NULL,
    planned_end TEXT NOT NULL,
    closure_scope TEXT NOT NULL,
    work_type TEXT NOT NULL,
    crew_id TEXT REFERENCES renewal_crews(crew_id),
    route_id TEXT REFERENCES renewal_routes(route_id),
    route_capacity INTEGER NOT NULL DEFAULT 0 CHECK(route_capacity >= 0),
    material_id TEXT REFERENCES renewal_materials(material_id),
    fund_id TEXT REFERENCES renewal_funds(fund_id),
    amount INTEGER NOT NULL DEFAULT 0 CHECK(amount >= 0),
    status TEXT NOT NULL CHECK(status IN ('scheduled','in_progress','completed','accepted','rejected')),
    actual_start TEXT,
    actual_end TEXT,
    PRIMARY KEY(plan_id, phase_id),
    UNIQUE(plan_id, seq)
);
CREATE TABLE IF NOT EXISTS renewal_approvals (
    plan_id TEXT NOT NULL REFERENCES renewal_plans(plan_id),
    party TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL,
    PRIMARY KEY(plan_id, party)
);
CREATE TABLE IF NOT EXISTS renewal_leases (
    lease_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    phase_id TEXT NOT NULL,
    resource_type TEXT NOT NULL CHECK(resource_type IN ('crew','route')),
    resource_id TEXT NOT NULL,
    capacity INTEGER NOT NULL DEFAULT 0,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','released')),
    released_at TEXT,
    release_reason TEXT,
    FOREIGN KEY(plan_id, phase_id) REFERENCES renewal_phases(plan_id, phase_id)
);
CREATE TABLE IF NOT EXISTS renewal_outages (
    outage_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES renewal_plans(plan_id),
    phase_id TEXT,
    facility_id TEXT NOT NULL,
    closure_scope TEXT NOT NULL,
    reason TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS renewal_payments (
    payment_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES renewal_plans(plan_id),
    phase_id TEXT NOT NULL,
    fund_id TEXT,
    amount INTEGER NOT NULL CHECK(amount >= 0),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS renewal_adjustments (
    adjustment_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES renewal_plans(plan_id),
    kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    phases_json TEXT NOT NULL,
    from_version INTEGER NOT NULL,
    to_version INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS renewal_conflicts (
    conflict_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    context TEXT NOT NULL,
    conflict_type TEXT NOT NULL,
    message TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    detected_at TEXT NOT NULL
);
"""


class RenewalService:
    """编排设施更新窗口的草案、会签、租约、调整与重放。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        self._mutex = threading.RLock()
        self.database.connection.executescript(SCHEMA)

    # ---- 基础工具 ----

    def _now_dt(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc).replace(microsecond=0)

    def _now(self) -> str:
        return self._format_dt(self._now_dt())

    @staticmethod
    def _format_dt(value: datetime) -> str:
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _parse_dt(self, value: Any, field: str) -> datetime:
        if not isinstance(value, str):
            raise ValidationError(f"{field} 必须是 ISO 8601 时间字符串")
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是 ISO 8601 时间") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return parsed.astimezone(timezone.utc).replace(microsecond=0)

    def _identifier(self, value: Any, field: str) -> str:
        if not isinstance(value, str):
            raise ValidationError(f"{field} 必须是字符串")
        text = value.strip()
        if not IDENTIFIER.fullmatch(text):
            raise ValidationError(f"{field} 格式无效")
        return text

    def _text(self, value: Any, field: str, limit: int = 200) -> str:
        if not isinstance(value, str):
            raise ValidationError(f"{field} 必须是字符串")
        text = value.strip()
        if not text or len(text) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return text

    def _optional_text(self, value: Any, field: str, limit: int = 200) -> str:
        if value is None:
            return ""
        return self._text(value, field, limit) if str(value).strip() else ""

    def _non_negative_int(self, value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValidationError(f"{field} 必须是非负整数")
        return value

    def _positive_int(self, value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValidationError(f"{field} 必须是正整数")
        return value

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _site(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _check_site_writer(self, connection, actor_id: str, site_id: str) -> None:
        actor = self._actor(connection, actor_id)
        self._require(actor, "admin", "operator")
        site = self._site(connection, site_id)
        if actor.organization_id != site["organization_id"] and actor.role != "admin":
            raise PermissionDenied("不能为其他组织的场所登记资源")

    # ---- 幂等回执 ----

    def _replayed(self, connection, *, request_id: str, action: str,
                  payload: dict[str, Any]) -> dict[str, Any] | None:
        request_id = self._identifier(request_id, "request_id")
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?",
                                 (request_id,)).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return json.loads(row["response_json"])

    def _store_receipt(self, connection, *, request_id: str, action: str, payload: dict[str, Any],
                       resource_type: str, resource_id: str, response: dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (self._identifier(request_id, "request_id"), action, digest(payload),
             resource_type, resource_id, canonical_json(response), self._now()))

    def _idempotent(self, connection, *, request_id: str, action: str, payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> tuple[bool, dict[str, Any]]:
        stored = self._replayed(connection, request_id=request_id, action=action, payload=payload)
        if stored is not None:
            return True, stored
        resource_type, resource_id, response = create()
        self._store_receipt(connection, request_id=request_id, action=action, payload=payload,
                            resource_type=resource_type, resource_id=resource_id, response=response)
        return False, response

    # ---- 资源登记 ----

    def register_facility(self, *, request_id: str, actor_id: str, site_id: str, facility_id: str,
                          name: str, facility_type: str, depends_on: list[str] | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "site_id": site_id, "facility_id": facility_id, "name": name,
                   "facility_type": facility_type, "depends_on": depends_on}
        with self._mutex, self.database.transaction(immediate=True) as connection:
            self._check_site_writer(connection, actor_id, site_id)
            facility_id = self._identifier(facility_id, "facility_id")
            name = self._text(name, "name")
            if facility_type not in FACILITY_TYPES:
                raise ValidationError("facility_type 不在允许范围内")
            depends: list[str] = []
            for item in depends_on or []:
                dep_id = self._identifier(item, "depends_on")
                if dep_id == facility_id:
                    raise ValidationError("设施不能依赖自身")
                if dep_id in depends:
                    continue
                if connection.execute("SELECT 1 FROM renewal_facilities WHERE facility_id=?",
                                      (dep_id,)).fetchone() is None:
                    raise NotFoundError(f"依赖设施 {dep_id} 不存在")
                depends.append(dep_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO renewal_facilities(facility_id,site_id,name,facility_type,depends_on_json,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (facility_id, site_id, name, facility_type, canonical_json(depends),
                         actor_id, self._now()))
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("设施编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="renewal_facility.registered",
                             resource_type="renewal_facility", resource_id=facility_id,
                             detail={"site_id": site_id, "name": name, "facility_type": facility_type,
                                     "depends_on": depends}, occurred_at=self._now())
                return "renewal_facility", facility_id, {"facility_id": facility_id, "site_id": site_id,
                                                         "depends_on": depends}

            replayed, response = self._idempotent(connection, request_id=request_id,
                                                  action="register_facility", payload=payload, create=create)
            return {**response, "replayed": replayed}

    def register_route(self, *, request_id: str, actor_id: str, site_id: str, route_id: str,
                       name: str, capacity: int) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "site_id": site_id, "route_id": route_id,
                   "name": name, "capacity": capacity}
        with self._mutex, self.database.transaction(immediate=True) as connection:
            self._check_site_writer(connection, actor_id, site_id)
            route_id = self._identifier(route_id, "route_id")
            name = self._text(name, "name")
            capacity = self._positive_int(capacity, "capacity")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO renewal_routes(route_id,site_id,name,capacity,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (route_id, site_id, name, capacity, actor_id, self._now()))
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("替代通道编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="renewal_route.registered",
                             resource_type="renewal_route", resource_id=route_id,
                             detail={"site_id": site_id, "name": name, "capacity": capacity},
                             occurred_at=self._now())
                return "renewal_route", route_id, {"route_id": route_id, "capacity": capacity}

            replayed, response = self._idempotent(connection, request_id=request_id,
                                                  action="register_route", payload=payload, create=create)
            return {**response, "replayed": replayed}

    def register_crew(self, *, request_id: str, actor_id: str, site_id: str, crew_id: str,
                      name: str, qualifications: list[str]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "site_id": site_id, "crew_id": crew_id,
                   "name": name, "qualifications": qualifications}
        with self._mutex, self.database.transaction(immediate=True) as connection:
            self._check_site_writer(connection, actor_id, site_id)
            crew_id = self._identifier(crew_id, "crew_id")
            name = self._text(name, "name")
            if not isinstance(qualifications, list) or not qualifications:
                raise ValidationError("qualifications 必须是非空数组")
            quals: list[str] = []
            for item in qualifications:
                qual = self._identifier(item, "qualifications")
                if qual not in quals:
                    quals.append(qual)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO renewal_crews(crew_id,site_id,name,qualifications_json,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (crew_id, site_id, name, canonical_json(quals), actor_id, self._now()))
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("专业队伍编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="renewal_crew.registered",
                             resource_type="renewal_crew", resource_id=crew_id,
                             detail={"site_id": site_id, "name": name, "qualifications": quals},
                             occurred_at=self._now())
                return "renewal_crew", crew_id, {"crew_id": crew_id, "qualifications": quals}

            replayed, response = self._idempotent(connection, request_id=request_id,
                                                  action="register_crew", payload=payload, create=create)
            return {**response, "replayed": replayed}

    def register_fund(self, *, request_id: str, actor_id: str, site_id: str, fund_id: str,
                      name: str, amount: int, deadline: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "site_id": site_id, "fund_id": fund_id,
                   "name": name, "amount": amount, "deadline": deadline}
        with self._mutex, self.database.transaction(immediate=True) as connection:
            self._check_site_writer(connection, actor_id, site_id)
            fund_id = self._identifier(fund_id, "fund_id")
            name = self._text(name, "name")
            amount = self._non_negative_int(amount, "amount")
            deadline_text = self._format_dt(self._parse_dt(deadline, "deadline"))

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO renewal_funds(fund_id,site_id,name,amount,deadline,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (fund_id, site_id, name, amount, deadline_text, actor_id, self._now()))
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("专项资金编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="renewal_fund.registered",
                             resource_type="renewal_fund", resource_id=fund_id,
                             detail={"site_id": site_id, "name": name, "amount": amount,
                                     "deadline": deadline_text}, occurred_at=self._now())
                return "renewal_fund", fund_id, {"fund_id": fund_id, "amount": amount,
                                                 "deadline": deadline_text}

            replayed, response = self._idempotent(connection, request_id=request_id,
                                                  action="register_fund", payload=payload, create=create)
            return {**response, "replayed": replayed}

    def register_material(self, *, request_id: str, actor_id: str, site_id: str, material_id: str,
                          name: str, arrival_date: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "site_id": site_id, "material_id": material_id,
                   "name": name, "arrival_date": arrival_date}
        with self._mutex, self.database.transaction(immediate=True) as connection:
            self._check_site_writer(connection, actor_id, site_id)
            material_id = self._identifier(material_id, "material_id")
            name = self._text(name, "name")
            arrival_text = self._format_dt(self._parse_dt(arrival_date, "arrival_date"))

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO renewal_materials(material_id,site_id,name,arrival_date,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (material_id, site_id, name, arrival_text, actor_id, self._now()))
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("材料编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="renewal_material.registered",
                             resource_type="renewal_material", resource_id=material_id,
                             detail={"site_id": site_id, "name": name, "arrival_date": arrival_text},
                             occurred_at=self._now())
                return "renewal_material", material_id, {"material_id": material_id,
                                                         "arrival_date": arrival_text}

            replayed, response = self._idempotent(connection, request_id=request_id,
                                                  action="register_material", payload=payload, create=create)
            return {**response, "replayed": replayed}

    # ---- 计划申报（限时草案） ----

    def create_plan(self, *, request_id: str, actor_id: str, site_id: str, plan_id: str,
                    facility_id: str, title: str, phases: list[dict[str, Any]],
                    draft_ttl_hours: int | None = None) -> dict[str, Any]:
        if not isinstance(phases, list) or not phases:
            raise ValidationError("phases 必须是非空数组")
        payload = {"actor_id": actor_id, "site_id": site_id, "plan_id": plan_id,
                   "facility_id": facility_id, "title": title, "phases": phases,
                   "draft_ttl_hours": draft_ttl_hours}
        with self._mutex, self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "planner")
            site = self._site(connection, site_id)
            if actor.organization_id != site["organization_id"]:
                raise PermissionDenied("不能为其他组织的场所申报计划")
            plan_id = self._identifier(plan_id, "plan_id")
            title = self._text(title, "title")
            facility = self._facility(connection, facility_id)
            if facility["site_id"] != site_id:
                raise ValidationError("设施不属于该场所")
            ttl = DEFAULT_DRAFT_TTL_HOURS if draft_ttl_hours is None else self._positive_int(
                draft_ttl_hours, "draft_ttl_hours")
            if ttl > MAX_DRAFT_TTL_HOURS:
                raise ValidationError(f"draft_ttl_hours 不能超过 {MAX_DRAFT_TTL_HOURS}")
            specs = self._validate_phase_specs(connection, site_id, phases)
            expires = self._now_dt() + timedelta(hours=ttl)

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                try:
                    connection.execute(
                        "INSERT INTO renewal_plans(plan_id,site_id,facility_id,title,status,version,"
                        "draft_expires_at,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (plan_id, site_id, facility_id, title, "draft", 1,
                         self._format_dt(expires), actor_id, now, now))
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("计划编号已经存在") from exc
                for seq, spec in enumerate(specs, start=1):
                    connection.execute(
                        "INSERT INTO renewal_phases(plan_id,phase_id,seq,name,planned_start,planned_end,"
                        "closure_scope,work_type,crew_id,route_id,route_capacity,material_id,fund_id,amount,"
                        "status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (plan_id, spec["phase_id"], seq, spec["name"], self._format_dt(spec["start"]),
                         self._format_dt(spec["end"]), spec["closure_scope"], spec["work_type"],
                         spec["crew_id"], spec["route_id"], spec["route_capacity"], spec["material_id"],
                         spec["fund_id"], spec["amount"], "scheduled"))
                conflicts = self._detect_and_record(connection, plan_id=plan_id,
                                                    facility_row=facility, phases=specs, context="draft")
                append_event(connection, actor_id=actor_id, action="renewal_plan.drafted",
                             resource_type="renewal_plan", resource_id=plan_id,
                             detail={"plan_id": plan_id, "site_id": site_id, "facility_id": facility_id,
                                     "title": title, "phases": len(specs),
                                     "draft_expires_at": self._format_dt(expires),
                                     "conflicts": len(conflicts)}, occurred_at=now)
                return "renewal_plan", plan_id, {
                    "plan_id": plan_id, "status": "draft", "version": 1,
                    "draft_expires_at": self._format_dt(expires),
                    "phases": [spec["phase_id"] for spec in specs], "conflicts": conflicts}

            replayed, response = self._idempotent(connection, request_id=request_id,
                                                  action="create_plan", payload=payload, create=create)
            return {**response, "replayed": replayed}

    def _validate_phase_specs(self, connection, site_id: str,
                              phases: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """校验阶段硬约束：资质、材料到场、资金期限、计划内部不重叠。"""

        specs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, item in enumerate(phases, start=1):
            if not isinstance(item, dict):
                raise ValidationError("阶段必须是对象")
            label = f"phases[{index}]"
            phase_id = self._identifier(item.get("phase_id"), f"{label}.phase_id")
            if phase_id in seen:
                raise ValidationError(f"阶段编号 {phase_id} 重复")
            seen.add(phase_id)
            name = self._text(item.get("name"), f"{label}.name")
            closure_scope = self._text(item.get("closure_scope"), f"{label}.closure_scope")
            work_type = self._identifier(item.get("work_type"), f"{label}.work_type")
            start = self._parse_dt(item.get("start"), f"{label}.start")
            end = self._parse_dt(item.get("end"), f"{label}.end")
            if end <= start:
                raise ValidationError(f"阶段 {phase_id} 的结束时间必须晚于开始时间")
            crew_id = item.get("crew_id")
            if crew_id is not None:
                crew_id = self._identifier(crew_id, f"{label}.crew_id")
                crew = connection.execute("SELECT * FROM renewal_crews WHERE crew_id=?",
                                          (crew_id,)).fetchone()
                if crew is None or crew["site_id"] != site_id:
                    raise NotFoundError(f"专业队伍 {crew_id} 不存在")
                if work_type not in json.loads(crew["qualifications_json"]):
                    raise ValidationError(f"专业队伍 {crew_id} 不具备 {work_type} 作业资质")
            route_id = item.get("route_id")
            route_capacity = self._non_negative_int(item.get("route_capacity", 0),
                                                    f"{label}.route_capacity")
            if route_id is not None:
                route_id = self._identifier(route_id, f"{label}.route_id")
                route = connection.execute("SELECT * FROM renewal_routes WHERE route_id=?",
                                           (route_id,)).fetchone()
                if route is None or route["site_id"] != site_id:
                    raise NotFoundError(f"替代通道 {route_id} 不存在")
                if route_capacity <= 0:
                    raise ValidationError(f"阶段 {phase_id} 使用替代通道时必须声明所需运力")
                if route_capacity > route["capacity"]:
                    raise ValidationError(f"阶段 {phase_id} 所需运力超过替代通道 {route_id} 的总运力")
            elif route_capacity:
                raise ValidationError(f"阶段 {phase_id} 未使用替代通道不能声明运力")
            material_id = item.get("material_id")
            if material_id is not None:
                material_id = self._identifier(material_id, f"{label}.material_id")
                material = connection.execute("SELECT * FROM renewal_materials WHERE material_id=?",
                                              (material_id,)).fetchone()
                if material is None or material["site_id"] != site_id:
                    raise NotFoundError(f"材料 {material_id} 不存在")
                if self._parse_dt(material["arrival_date"], "arrival_date") > start:
                    raise ValidationError(f"材料 {material_id} 到场时间晚于阶段 {phase_id} 的开工时间")
            fund_id = item.get("fund_id")
            amount = self._non_negative_int(item.get("amount", 0), f"{label}.amount")
            if fund_id is not None:
                fund_id = self._identifier(fund_id, f"{label}.fund_id")
                fund = connection.execute("SELECT * FROM renewal_funds WHERE fund_id=?",
                                          (fund_id,)).fetchone()
                if fund is None or fund["site_id"] != site_id:
                    raise NotFoundError(f"专项资金 {fund_id} 不存在")
                if self._parse_dt(fund["deadline"], "deadline") < end:
                    raise ValidationError(f"阶段 {phase_id} 的完工时间超出专项资金 {fund_id} 的使用期限")
            elif amount:
                raise ValidationError(f"阶段 {phase_id} 申报支付金额必须指定专项资金")
            specs.append({"phase_id": phase_id, "name": name, "closure_scope": closure_scope,
                          "work_type": work_type, "start": start, "end": end, "crew_id": crew_id,
                          "route_id": route_id, "route_capacity": route_capacity,
                          "material_id": material_id, "fund_id": fund_id, "amount": amount})
        ordered = sorted(specs, key=lambda spec: spec["start"])
        for left, right in zip(ordered, ordered[1:]):
            if right["start"] < left["end"]:
                raise ValidationError(f"阶段 {left['phase_id']} 与 {right['phase_id']} 的窗口相互重叠")
        return specs

    # ---- 冲突检测与取舍建议 ----

    def _overlaps(self, start: datetime, end: datetime, stored_start: str, stored_end: str) -> bool:
        other_start = self._parse_dt(stored_start, "starts_at")
        other_end = self._parse_dt(stored_end, "ends_at")
        return start < other_end and other_start < end

    def _available_crews(self, connection, site_id: str, work_type: str, exclude_crew_id: str,
                         start: datetime, end: datetime) -> list[str]:
        alternatives = []
        for crew in connection.execute("SELECT * FROM renewal_crews WHERE site_id=?", (site_id,)):
            if crew["crew_id"] == exclude_crew_id:
                continue
            if work_type not in json.loads(crew["qualifications_json"]):
                continue
            rows = connection.execute(
                "SELECT starts_at, ends_at FROM renewal_leases WHERE resource_type='crew' "
                "AND resource_id=? AND status='active'", (crew["crew_id"],)).fetchall()
            if any(self._overlaps(start, end, row["starts_at"], row["ends_at"]) for row in rows):
                continue
            alternatives.append(crew["crew_id"])
        return sorted(alternatives)

    def _detect_conflicts(self, connection, *, plan_id: str, facility_row,
                          phases: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """对照已锁定窗口检测跨计划冲突，并给出可执行的取舍建议。"""

        conflicts: list[dict[str, Any]] = []
        depends_on = json.loads(facility_row["depends_on_json"])
        site_id = facility_row["site_id"]
        for phase in phases:
            start, end = phase["start"], phase["end"]
            window = f"{self._format_dt(start)}~{self._format_dt(end)}"
            if phase["crew_id"]:
                rows = connection.execute(
                    "SELECT * FROM renewal_leases WHERE resource_type='crew' AND resource_id=? "
                    "AND status='active' AND plan_id!=?", (phase["crew_id"], plan_id)).fetchall()
                overlapping = [row for row in rows
                               if self._overlaps(start, end, row["starts_at"], row["ends_at"])]
                if overlapping:
                    latest_end = max(self._parse_dt(row["ends_at"], "ends_at") for row in overlapping)
                    holders = sorted({row["plan_id"] for row in overlapping})
                    alternatives = self._available_crews(connection, site_id, phase["work_type"],
                                                         phase["crew_id"], start, end)
                    suggestion = f"将窗口调整至 {self._format_dt(latest_end)} 之后"
                    if alternatives:
                        suggestion += f"，或改用具备 {phase['work_type']} 资质的队伍：{'、'.join(alternatives)}"
                    conflicts.append({
                        "conflict_type": "crew_double_booked",
                        "message": f"专业队伍 {phase['crew_id']} 在 {window} 已被计划 "
                                   f"{'、'.join(holders)} 锁定，重复占用将导致施工力量不足",
                        "suggestion": suggestion,
                        "detail": {"phase_id": phase["phase_id"], "crew_id": phase["crew_id"],
                                   "holder_plans": holders},
                    })
            if phase["route_id"]:
                route = connection.execute("SELECT * FROM renewal_routes WHERE route_id=?",
                                           (phase["route_id"],)).fetchone()
                rows = connection.execute(
                    "SELECT * FROM renewal_leases WHERE resource_type='route' AND resource_id=? "
                    "AND status='active' AND plan_id!=?", (phase["route_id"], plan_id)).fetchall()
                overlapping = [row for row in rows
                               if self._overlaps(start, end, row["starts_at"], row["ends_at"])]
                used = sum(row["capacity"] for row in overlapping)
                need = phase["route_capacity"]
                if used + need > route["capacity"]:
                    holders = sorted({row["plan_id"] for row in overlapping})
                    latest_end = max(self._parse_dt(row["ends_at"], "ends_at") for row in overlapping)
                    available = route["capacity"] - used
                    conflicts.append({
                        "conflict_type": "route_capacity_exceeded",
                        "message": f"替代通道 {phase['route_id']} 总运力 {route['capacity']}，{window} 同期"
                                   f"已被计划 {'、'.join(holders)} 锁定 {used}，本阶段还需 {need}，"
                                   f"叠加后区域通行能力将同时下降",
                        "suggestion": f"将本阶段替代运力需求降至 {available} 及以下，"
                                      f"或将窗口调整至 {self._format_dt(latest_end)} 之后",
                        "detail": {"phase_id": phase["phase_id"], "route_id": phase["route_id"],
                                   "capacity": route["capacity"], "locked": used, "requested": need,
                                   "holder_plans": holders},
                    })
            for dep in depends_on:
                rows = connection.execute(
                    "SELECT ph.plan_id, ph.phase_id, ph.planned_start, ph.planned_end "
                    "FROM renewal_phases ph JOIN renewal_plans pl ON pl.plan_id=ph.plan_id "
                    "WHERE pl.facility_id=? AND pl.status IN ('locked','in_progress') AND pl.plan_id!=?",
                    (dep, plan_id)).fetchall()
                overlapping = [row for row in rows
                               if self._overlaps(start, end, row["planned_start"], row["planned_end"])]
                if overlapping:
                    holders = sorted({row["plan_id"] for row in overlapping})
                    conflicts.append({
                        "conflict_type": "dependency_blocked",
                        "message": f"设施依赖的 {dep} 在 {window} 同期存在已锁定窗口"
                                   f"（计划 {'、'.join(holders)}），替代通行能力无法保障",
                        "suggestion": f"将窗口调整至依赖设施 {dep} 的封闭时段之外",
                        "detail": {"phase_id": phase["phase_id"], "dependency": dep,
                                   "holder_plans": holders},
                    })
        return conflicts

    def _detect_and_record(self, connection, *, plan_id: str, facility_row,
                           phases: list[dict[str, Any]], context: str) -> list[dict[str, Any]]:
        conflicts = self._detect_conflicts(connection, plan_id=plan_id,
                                           facility_row=facility_row, phases=phases)
        now = self._now()
        for conflict in conflicts:
            connection.execute(
                "INSERT INTO renewal_conflicts(conflict_id,plan_id,context,conflict_type,message,"
                "detail_json,detected_at) VALUES(?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, plan_id, context, conflict["conflict_type"], conflict["message"],
                 canonical_json({"suggestion": conflict["suggestion"], **conflict["detail"]}), now))
        return conflicts

    # ---- 会签与锁定 ----

    def approve_plan(self, *, request_id: str, actor_id: str, plan_id: str, party: str,
                     comment: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "plan_id": plan_id, "party": party, "comment": comment}
        with self._mutex, self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if party not in PARTIES:
                raise ValidationError("party 不在会签方范围内")
            if actor.role != party:
                raise PermissionDenied(f"{PARTY_LABELS[party]}方会签必须由对应角色本人签署")
            stored = self._replayed(connection, request_id=request_id,
                                    action="approve_plan", payload=payload)
            if stored is not None:
                return {**stored, "replayed": True}
            plan = self._plan_row(connection, plan_id)
            self._abort_if_expired(connection, plan)
            if plan["status"] not in ("draft", "approved"):
                raise ConflictError(f"计划当前状态为 {plan['status']}，不能继续会签")
            comment_text = self._optional_text(comment, "comment")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                try:
                    connection.execute(
                        "INSERT INTO renewal_approvals(plan_id,party,actor_id,comment,decided_at) "
                        "VALUES(?,?,?,?,?)",
                        (plan_id, party, actor_id, comment_text, now))
                except sqlite3.IntegrityError as exc:
                    raise ConflictError(f"{PARTY_LABELS[party]}方已完成会签，不能重复签署") from exc
                append_event(connection, actor_id=actor_id, action="renewal_plan.approved",
                             resource_type="renewal_plan", resource_id=plan_id,
                             detail={"plan_id": plan_id, "party": party, "comment": comment_text},
                             occurred_at=now)
                approvals = self._approvals(connection, plan_id)
                status = plan["status"]
                lock_result: dict[str, Any] = {"acquired": False, "conflicts": []}
                if len(approvals) == len(PARTIES):
                    facility = self._facility(connection, plan["facility_id"])
                    specs = self._phase_specs_from_rows(connection, plan_id)
                    conflicts = self._detect_and_record(connection, plan_id=plan_id,
                                                        facility_row=facility, phases=specs,
                                                        context="lock")
                    if conflicts:
                        connection.execute(
                            "UPDATE renewal_plans SET status='approved', updated_at=? WHERE plan_id=?",
                            (self._now(), plan_id))
                        append_event(connection, actor_id=actor_id,
                                     action="renewal_plan.lock_deferred",
                                     resource_type="renewal_plan", resource_id=plan_id,
                                     detail={"plan_id": plan_id, "conflicts": conflicts},
                                     occurred_at=self._now())
                        status = "approved"
                        lock_result["conflicts"] = conflicts
                    else:
                        leases = self._create_leases(connection, plan_id)
                        connection.execute(
                            "UPDATE renewal_plans SET status='locked', updated_at=? WHERE plan_id=?",
                            (self._now(), plan_id))
                        append_event(connection, actor_id=actor_id, action="renewal_plan.locked",
                                     resource_type="renewal_plan", resource_id=plan_id,
                                     detail={"plan_id": plan_id, "leases": leases},
                                     occurred_at=self._now())
                        status = "locked"
                        lock_result.update({"acquired": True, "leases": leases})
                return "renewal_plan", plan_id, {"plan_id": plan_id, "status": status,
                                                 "approvals": approvals, "lock": lock_result}

            replayed, response = self._idempotent(connection, request_id=request_id,
                                                  action="approve_plan", payload=payload, create=create)
            return {**response, "replayed": replayed}

    def lock_plan(self, *, request_id: str, actor_id: str, plan_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "plan_id": plan_id}
        with self._mutex:
            with self.database.transaction(immediate=True) as connection:
                actor = self._actor(connection, actor_id)
                self._require(actor, "planner")
                stored = self._replayed(connection, request_id=request_id,
                                        action="lock_plan", payload=payload)
                if stored is not None:
                    return {**stored, "replayed": True}
                plan = self._plan_row(connection, plan_id)
                self._abort_if_expired(connection, plan)
                if plan["status"] in ("locked", "in_progress"):
                    raise ConflictError("计划已锁定，不能重复锁定")
                if plan["status"] != "approved":
                    raise ConflictError("三方会签未完成，不能锁定资源")
                facility = self._facility(connection, plan["facility_id"])
                specs = self._phase_specs_from_rows(connection, plan_id)
                conflicts = self._detect_and_record(connection, plan_id=plan_id,
                                                    facility_row=facility, phases=specs,
                                                    context="lock")
                failure: list[dict[str, Any]] | None = None
                response: dict[str, Any] = {}
                if conflicts:
                    # 冲突与审计先行落库，重启后仍可解释本次取舍
                    append_event(connection, actor_id=actor_id,
                                 action="renewal_plan.lock_rejected",
                                 resource_type="renewal_plan", resource_id=plan_id,
                                 detail={"plan_id": plan_id, "conflicts": conflicts},
                                 occurred_at=self._now())
                    failure = conflicts
                else:
                    leases = self._create_leases(connection, plan_id)
                    connection.execute(
                        "UPDATE renewal_plans SET status='locked', updated_at=? WHERE plan_id=?",
                        (self._now(), plan_id))
                    append_event(connection, actor_id=actor_id, action="renewal_plan.locked",
                                 resource_type="renewal_plan", resource_id=plan_id,
                                 detail={"plan_id": plan_id, "leases": leases},
                                 occurred_at=self._now())
                    response = {"plan_id": plan_id, "status": "locked", "leases": leases}
                    self._store_receipt(connection, request_id=request_id, action="lock_plan",
                                        payload=payload, resource_type="renewal_plan",
                                        resource_id=plan_id, response=response)
            if failure is not None:
                raise ConflictError("锁定失败：与已锁定窗口存在资源冲突",
                                    extra={"plan_id": plan_id, "conflicts": failure})
            return {**response, "replayed": False}

    def _create_leases(self, connection, plan_id: str) -> list[str]:
        leases = []
        for phase in connection.execute(
                "SELECT * FROM renewal_phases WHERE plan_id=? ORDER BY seq", (plan_id,)):
            for resource_type, resource_id, capacity in (
                    ("crew", phase["crew_id"], 0),
                    ("route", phase["route_id"], phase["route_capacity"])):
                if resource_id:
                    lease_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO renewal_leases(lease_id,plan_id,phase_id,resource_type,resource_id,"
                        "capacity,starts_at,ends_at,status) VALUES(?,?,?,?,?,?,?,?,?)",
                        (lease_id, plan_id, phase["phase_id"], resource_type, resource_id, capacity,
                         phase["planned_start"], phase["planned_end"], "active"))
                    leases.append(lease_id)
        return leases

    # ---- 阶段推进 ----

    def start_phase(self, *, request_id: str, actor_id: str, plan_id: str,
                    phase_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "plan_id": plan_id, "phase_id": phase_id}
        with self._mutex, self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "construction")
            stored = self._replayed(connection, request_id=request_id,
                                    action="start_phase", payload=payload)
            if stored is not None:
                return {**stored, "replayed": True}
            plan = self._plan_row(connection, plan_id)
            if plan["status"] not in ("locked", "in_progress"):
                raise ConflictError(f"计划当前状态为 {plan['status']}，不能开工")
            phase = self._phase_row(connection, plan_id, phase_id)
            if phase["status"] != "scheduled":
                raise ConflictError(f"阶段当前状态为 {phase['status']}，不能开工")
            blockers = connection.execute(
                "SELECT phase_id FROM renewal_phases WHERE plan_id=? AND seq<? AND status!='accepted' "
                "ORDER BY seq", (plan_id, phase["seq"])).fetchall()
            if blockers:
                raise ConflictError("前置阶段尚未验收通过，不能开工",
                                    extra={"blocked_by": [row["phase_id"] for row in blockers]})

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                connection.execute(
                    "UPDATE renewal_phases SET status='in_progress', actual_start=? "
                    "WHERE plan_id=? AND phase_id=?", (now, plan_id, phase_id))
                if plan["status"] == "locked":
                    connection.execute(
                        "UPDATE renewal_plans SET status='in_progress', updated_at=? WHERE plan_id=?",
                        (now, plan_id))
                outage_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO renewal_outages(outage_id,plan_id,phase_id,facility_id,closure_scope,"
                    "reason,started_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (outage_id, plan_id, phase_id, plan["facility_id"], phase["closure_scope"],
                     f"阶段 {phase['name']} 开工封闭", now, now))
                append_event(connection, actor_id=actor_id, action="renewal_phase.started",
                             resource_type="renewal_phase", resource_id=f"{plan_id}/{phase_id}",
                             detail={"plan_id": plan_id, "phase_id": phase_id,
                                     "outage_id": outage_id}, occurred_at=now)
                append_event(connection, actor_id=actor_id, action="renewal_outage.opened",
                             resource_type="renewal_outage", resource_id=outage_id,
                             detail={"plan_id": plan_id, "phase_id": phase_id,
                                     "facility_id": plan["facility_id"],
                                     "closure_scope": phase["closure_scope"]}, occurred_at=now)
                return "renewal_phase", f"{plan_id}/{phase_id}", {
                    "plan_id": plan_id, "phase_id": phase_id, "status": "in_progress",
                    "actual_start": now, "outage_id": outage_id}

            replayed, response = self._idempotent(connection, request_id=request_id,
                                                  action="start_phase", payload=payload, create=create)
            return {**response, "replayed": replayed}

    def complete_phase(self, *, request_id: str, actor_id: str, plan_id: str, phase_id: str,
                       amount: int = 0) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "plan_id": plan_id, "phase_id": phase_id, "amount": amount}
        with self._mutex, self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "construction")
            stored = self._replayed(connection, request_id=request_id,
                                    action="complete_phase", payload=payload)
            if stored is not None:
                return {**stored, "replayed": True}
            self._plan_row(connection, plan_id)
            phase = self._phase_row(connection, plan_id, phase_id)
            if phase["status"] != "in_progress":
                raise ConflictError(f"阶段当前状态为 {phase['status']}，不能报完工")
            amount = self._non_negative_int(amount, "amount")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                payment_id = None
                if amount:
                    payment_id = self._record_payment(connection, actor_id=actor_id, plan_id=plan_id,
                                                      phase_row=phase, value=amount,
                                                      reason="completion", now=now)
                connection.execute(
                    "UPDATE renewal_phases SET status='completed', actual_end=? "
                    "WHERE plan_id=? AND phase_id=?", (now, plan_id, phase_id))
                connection.execute(
                    "UPDATE renewal_outages SET ended_at=? WHERE plan_id=? AND phase_id=? "
                    "AND ended_at IS NULL", (now, plan_id, phase_id))
                append_event(connection, actor_id=actor_id, action="renewal_phase.completed",
                             resource_type="renewal_phase", resource_id=f"{plan_id}/{phase_id}",
                             detail={"plan_id": plan_id, "phase_id": phase_id, "amount": amount,
                                     "payment_id": payment_id}, occurred_at=now)
                return "renewal_phase", f"{plan_id}/{phase_id}", {
                    "plan_id": plan_id, "phase_id": phase_id, "status": "completed",
                    "actual_end": now, "payment_id": payment_id}

            replayed, response = self._idempotent(connection, request_id=request_id,
                                                  action="complete_phase", payload=payload,
                                                  create=create)
            return {**response, "replayed": replayed}

    def accept_phase(self, *, request_id: str, actor_id: str, plan_id: str,
                     phase_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "plan_id": plan_id, "phase_id": phase_id}
        with self._mutex, self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "operations")
            stored = self._replayed(connection, request_id=request_id,
                                    action="accept_phase", payload=payload)
            if stored is not None:
                return {**stored, "replayed": True}
            phase = self._phase_row(connection, plan_id, phase_id)
            if phase["status"] != "completed":
                raise ConflictError(f"阶段当前状态为 {phase['status']}，不能验收")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE renewal_phases SET status='accepted' WHERE plan_id=? AND phase_id=?",
                    (plan_id, phase_id))
                append_event(connection, actor_id=actor_id, action="renewal_phase.accepted",
                             resource_type="renewal_phase", resource_id=f"{plan_id}/{phase_id}",
                             detail={"plan_id": plan_id, "phase_id": phase_id},
                             occurred_at=self._now())
                return "renewal_phase", f"{plan_id}/{phase_id}", {
                    "plan_id": plan_id, "phase_id": phase_id, "status": "accepted"}

            replayed, response = self._idempotent(connection, request_id=request_id,
                                                  action="accept_phase", payload=payload, create=create)
            return {**response, "replayed": replayed}

    def reject_phase(self, *, request_id: str, actor_id: str, plan_id: str, phase_id: str,
                     reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "plan_id": plan_id, "phase_id": phase_id, "reason": reason}
        with self._mutex, self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "operations")
            stored = self._replayed(connection, request_id=request_id,
                                    action="reject_phase", payload=payload)
            if stored is not None:
                return {**stored, "replayed": True}
            phase = self._phase_row(connection, plan_id, phase_id)
            if phase["status"] != "completed":
                raise ConflictError(f"阶段当前状态为 {phase['status']}，不能验收退回")
            reason = self._text(reason, "reason")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE renewal_phases SET status='rejected' WHERE plan_id=? AND phase_id=?",
                    (plan_id, phase_id))
                append_event(connection, actor_id=actor_id, action="renewal_phase.rejected",
                             resource_type="renewal_phase", resource_id=f"{plan_id}/{phase_id}",
                             detail={"plan_id": plan_id, "phase_id": phase_id, "reason": reason},
                             occurred_at=self._now())
                return "renewal_phase", f"{plan_id}/{phase_id}", {
                    "plan_id": plan_id, "phase_id": phase_id, "status": "rejected",
                    "reason": reason}

            replayed, response = self._idempotent(connection, request_id=request_id,
                                                  action="reject_phase", payload=payload, create=create)
            return {**response, "replayed": replayed}

    # ---- 调整（延期 / 部分完工 / 验收退回 / 紧急抢修） ----

    def adjust_plan(self, *, request_id: str, actor_id: str, plan_id: str, kind: str,
                    expected_version: int, reason: str, phases: list[dict[str, Any]] | None = None,
                    phase_id: str | None = None, amount: int | None = None,
                    note: str | None = None, repair_until: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "plan_id": plan_id, "kind": kind,
                   "expected_version": expected_version, "reason": reason, "phases": phases,
                   "phase_id": phase_id, "amount": amount, "note": note,
                   "repair_until": repair_until}
        with self._mutex:
            with self.database.transaction(immediate=True) as connection:
                actor = self._actor(connection, actor_id)
                if kind not in ADJUSTMENT_KINDS:
                    raise ValidationError("kind 不在允许范围内")
                self._require(actor, *ADJUSTMENT_ROLES[kind])
                stored = self._replayed(connection, request_id=request_id,
                                        action="adjust_plan", payload=payload)
                if stored is not None:
                    return {**stored, "replayed": True}
                plan = self._plan_row(connection, plan_id)
                self._abort_if_expired(connection, plan)
                if plan["status"] not in PLAN_OPEN_STATUSES:
                    raise ConflictError(f"计划当前状态为 {plan['status']}，不能调整")
                expected_version = self._positive_int(expected_version, "expected_version")
                reason = self._text(reason, "reason")
                note = self._optional_text(note, "note")
                if plan["version"] != expected_version:
                    raise ConflictError("调整基于的窗口版本已过期，同一窗口最多一个调整方案生效",
                                        extra={"current_version": plan["version"],
                                               "expected_version": expected_version})
                now = self._now()
                until = None
                if kind == "emergency_repair":
                    until = self._parse_dt(repair_until, "repair_until")
                target = None
                amount_value = 0
                if kind == "partial_complete":
                    target = self._phase_row(connection, plan_id,
                                             self._identifier(phase_id, "phase_id"))
                    if target["status"] != "in_progress":
                        raise ConflictError("部分完工只能登记在正在施工的阶段上")
                    amount_value = self._positive_int(amount, "amount")
                    self._check_fund_budget(connection, target, amount_value)
                if kind in ("delay", "acceptance_reject", "emergency_repair") and not phases:
                    raise ValidationError("必须给出需要重排的阶段")
                changes: list[dict[str, Any]] = []
                rejection: list[dict[str, Any]] | None = None
                response: dict[str, Any] = {}
                if phases:
                    changes = self._plan_reschedules(connection, plan, phases,
                                                     kind=kind, repair_until=until)
                    self._check_internal_overlap(connection, plan_id, changes)
                    for change in changes:
                        self._check_phase_constraints(connection, change["phase"],
                                                      change["start"], change["end"])
                    facility = self._facility(connection, plan["facility_id"])
                    specs = [{"phase_id": change["phase"]["phase_id"],
                              "work_type": change["phase"]["work_type"],
                              "crew_id": change["phase"]["crew_id"],
                              "route_id": change["phase"]["route_id"],
                              "route_capacity": change["phase"]["route_capacity"],
                              "start": change["start"], "end": change["end"]}
                             for change in changes]
                    conflicts = self._detect_and_record(connection, plan_id=plan_id,
                                                        facility_row=facility, phases=specs,
                                                        context="adjustment")
                    if conflicts:
                        append_event(connection, actor_id=actor_id,
                                     action="renewal_plan.adjustment_rejected",
                                     resource_type="renewal_plan", resource_id=plan_id,
                                     detail={"plan_id": plan_id, "kind": kind, "reason": reason,
                                             "conflicts": conflicts}, occurred_at=now)
                        rejection = conflicts
                if rejection is None:
                    for change in changes:
                        phase = change["phase"]
                        if phase["status"] == "in_progress":
                            connection.execute(
                                "UPDATE renewal_phases SET planned_end=? "
                                "WHERE plan_id=? AND phase_id=?",
                                (self._format_dt(change["end"]), plan_id, phase["phase_id"]))
                        elif phase["status"] == "rejected":
                            connection.execute(
                                "UPDATE renewal_phases SET planned_start=?, planned_end=?, "
                                "status='scheduled', actual_start=NULL, actual_end=NULL "
                                "WHERE plan_id=? AND phase_id=?",
                                (self._format_dt(change["start"]), self._format_dt(change["end"]),
                                 plan_id, phase["phase_id"]))
                        else:
                            connection.execute(
                                "UPDATE renewal_phases SET planned_start=?, planned_end=? "
                                "WHERE plan_id=? AND phase_id=?",
                                (self._format_dt(change["start"]), self._format_dt(change["end"]),
                                 plan_id, phase["phase_id"]))
                        connection.execute(
                            "UPDATE renewal_leases SET starts_at=?, ends_at=? "
                            "WHERE plan_id=? AND phase_id=? AND status='active'",
                            (self._format_dt(change["start"]), self._format_dt(change["end"]),
                             plan_id, phase["phase_id"]))
                    payment_id = None
                    if kind == "partial_complete":
                        payment_id = self._record_payment(connection, actor_id=actor_id,
                                                          plan_id=plan_id, phase_row=target,
                                                          value=amount_value, reason="partial",
                                                          now=now)
                    outage_id = None
                    if kind == "emergency_repair":
                        outage_id = uuid.uuid4().hex
                        connection.execute(
                            "INSERT INTO renewal_outages(outage_id,plan_id,phase_id,facility_id,"
                            "closure_scope,reason,started_at,ended_at,created_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?)",
                            (outage_id, plan_id, None, plan["facility_id"], "紧急抢修",
                             f"紧急抢修：{reason}", now, self._format_dt(until), now))
                        append_event(connection, actor_id=actor_id, action="renewal_outage.opened",
                                     resource_type="renewal_outage", resource_id=outage_id,
                                     detail={"plan_id": plan_id, "facility_id": plan["facility_id"],
                                             "closure_scope": "紧急抢修", "emergency": True,
                                             "ended_at": self._format_dt(until)}, occurred_at=now)
                    cursor = connection.execute(
                        "UPDATE renewal_plans SET version=version+1, updated_at=? "
                        "WHERE plan_id=? AND version=?", (now, plan_id, expected_version))
                    if cursor.rowcount != 1:
                        raise ConflictError("窗口已被其他调整方案修改，本方案失效")
                    adjustment_id = uuid.uuid4().hex
                    changed_summary = [{"phase_id": change["phase"]["phase_id"],
                                        "start": self._format_dt(change["start"]),
                                        "end": self._format_dt(change["end"])}
                                       for change in changes]
                    connection.execute(
                        "INSERT INTO renewal_adjustments(adjustment_id,plan_id,kind,reason,"
                        "phases_json,from_version,to_version,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (adjustment_id, plan_id, kind, reason, canonical_json(changed_summary),
                         expected_version, expected_version + 1, actor_id, now))
                    append_event(connection, actor_id=actor_id, action="renewal_plan.adjusted",
                                 resource_type="renewal_plan", resource_id=plan_id,
                                 detail={"plan_id": plan_id, "adjustment_id": adjustment_id,
                                         "kind": kind, "reason": reason, "note": note,
                                         "from_version": expected_version,
                                         "to_version": expected_version + 1,
                                         "phases": changed_summary, "payment_id": payment_id,
                                         "emergency_outage_id": outage_id}, occurred_at=now)
                    response = {"plan_id": plan_id, "adjustment_id": adjustment_id, "kind": kind,
                                "status": plan["status"], "version": expected_version + 1,
                                "changed_phases": changed_summary, "payment_id": payment_id,
                                "emergency_outage_id": outage_id}
                    self._store_receipt(connection, request_id=request_id, action="adjust_plan",
                                        payload=payload, resource_type="renewal_plan",
                                        resource_id=plan_id, response=response)
            if rejection is not None:
                raise ConflictError("调整与已锁定窗口存在资源冲突",
                                    extra={"plan_id": plan_id, "conflicts": rejection})
            return {**response, "replayed": False}

    def _plan_reschedules(self, connection, plan, items: list[dict[str, Any]], *,
                          kind: str, repair_until: datetime | None) -> list[dict[str, Any]]:
        """在内存中计算重排方案，只触碰受影响阶段，已完成阶段一律拒绝。"""

        if not isinstance(items, list) or not items:
            raise ValidationError("phases 必须是非空数组")
        planned = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValidationError("重排项必须是对象")
            phase_id = self._identifier(item.get("phase_id"), "phases[].phase_id")
            if phase_id in seen:
                raise ValidationError(f"阶段 {phase_id} 重复重排")
            seen.add(phase_id)
            phase = self._phase_row(connection, plan["plan_id"], phase_id)
            new_start = self._parse_dt(item.get("start"), f"阶段 {phase_id} 的 start")
            new_end = self._parse_dt(item.get("end"), f"阶段 {phase_id} 的 end")
            if new_end <= new_start:
                raise ValidationError(f"阶段 {phase_id} 的结束时间必须晚于开始时间")
            status = phase["status"]
            if status == "scheduled":
                if repair_until is not None and new_start < repair_until:
                    raise ValidationError(f"阶段 {phase_id} 重排后不得早于紧急抢修结束时间")
            elif status == "rejected":
                if kind != "acceptance_reject":
                    raise ConflictError(f"阶段 {phase_id} 处于验收退回状态，"
                                        f"须通过 acceptance_reject 调整重排")
            elif status == "in_progress":
                if kind == "acceptance_reject":
                    raise ConflictError(f"阶段 {phase_id} 正在施工，不能按验收退回调度")
                planned_start = self._parse_dt(phase["planned_start"], "planned_start")
                planned_end = self._parse_dt(phase["planned_end"], "planned_end")
                if new_start != planned_start:
                    raise ValidationError(f"阶段 {phase_id} 正在施工，开工时间不可更改")
                if new_end <= planned_end:
                    raise ValidationError(f"阶段 {phase_id} 正在施工，只能顺延完工时间")
                if repair_until is not None and new_end < repair_until:
                    raise ValidationError(f"阶段 {phase_id} 顺延后的完工时间不得早于紧急抢修结束时间")
            else:
                raise ConflictError(f"阶段 {phase_id} 已完成或已验收，停运与支付记录不可更改，"
                                    f"不能重排")
            planned.append({"phase": phase, "start": new_start, "end": new_end})
        return planned

    def _check_internal_overlap(self, connection, plan_id: str,
                                changes: list[dict[str, Any]]) -> None:
        windows: dict[str, tuple[datetime, datetime]] = {}
        for row in connection.execute(
                "SELECT phase_id, planned_start, planned_end FROM renewal_phases WHERE plan_id=?",
                (plan_id,)):
            windows[row["phase_id"]] = (self._parse_dt(row["planned_start"], "planned_start"),
                                        self._parse_dt(row["planned_end"], "planned_end"))
        for change in changes:
            windows[change["phase"]["phase_id"]] = (change["start"], change["end"])
        ordered = sorted(windows.items(), key=lambda item: item[1][0])
        for (left_id, (_, left_end)), (right_id, (right_start, _)) in zip(ordered, ordered[1:]):
            if right_start < left_end:
                raise ConflictError(f"重排后阶段 {left_id} 与 {right_id} 的窗口相互重叠，需一并调整")

    def _check_phase_constraints(self, connection, phase_row,
                                 new_start: datetime, new_end: datetime) -> None:
        if phase_row["fund_id"]:
            fund = connection.execute("SELECT * FROM renewal_funds WHERE fund_id=?",
                                      (phase_row["fund_id"],)).fetchone()
            if self._parse_dt(fund["deadline"], "deadline") < new_end:
                raise ValidationError(
                    f"阶段 {phase_row['phase_id']} 重排后的完工时间超出专项资金 "
                    f"{phase_row['fund_id']} 的使用期限")
        if phase_row["material_id"]:
            material = connection.execute("SELECT * FROM renewal_materials WHERE material_id=?",
                                          (phase_row["material_id"],)).fetchone()
            if self._parse_dt(material["arrival_date"], "arrival_date") > new_start:
                raise ValidationError(
                    f"材料 {phase_row['material_id']} 到场时间晚于阶段 "
                    f"{phase_row['phase_id']} 重排后的开工时间")

    def _check_fund_budget(self, connection, phase_row, value: int) -> None:
        fund_id = phase_row["fund_id"]
        if not fund_id:
            raise ValidationError("该阶段未关联专项资金，不能登记支付")
        fund = connection.execute("SELECT * FROM renewal_funds WHERE fund_id=?",
                                  (fund_id,)).fetchone()
        spent = connection.execute(
            "SELECT COALESCE(SUM(amount),0) AS total FROM renewal_payments WHERE fund_id=?",
            (fund_id,)).fetchone()["total"]
        if spent + value > fund["amount"]:
            raise ConflictError("专项资金余额不足，无法登记支付",
                                extra={"fund_id": fund_id, "budget": fund["amount"],
                                       "spent": spent, "requested": value})

    def _record_payment(self, connection, *, actor_id: str, plan_id: str, phase_row,
                        value: int, reason: str, now: str) -> str:
        self._check_fund_budget(connection, phase_row, value)
        payment_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO renewal_payments(payment_id,plan_id,phase_id,fund_id,amount,reason,"
            "created_at) VALUES(?,?,?,?,?,?,?)",
            (payment_id, plan_id, phase_row["phase_id"], phase_row["fund_id"], value, reason, now))
        append_event(connection, actor_id=actor_id, action="renewal_payment.recorded",
                     resource_type="renewal_payment", resource_id=payment_id,
                     detail={"plan_id": plan_id, "phase_id": phase_row["phase_id"],
                             "fund_id": phase_row["fund_id"], "amount": value, "reason": reason},
                     occurred_at=now)
        return payment_id

    # ---- 租约释放与复开 ----

    def release_lease(self, *, request_id: str, actor_id: str, plan_id: str,
                      lease_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "plan_id": plan_id, "lease_id": lease_id}
        with self._mutex:
            with self.database.transaction(immediate=True) as connection:
                actor = self._actor(connection, actor_id)
                self._require(actor, "planner", "operations")
                stored = self._replayed(connection, request_id=request_id,
                                        action="release_lease", payload=payload)
                if stored is not None:
                    return {**stored, "replayed": True}
                self._plan_row(connection, plan_id)
                lease = connection.execute(
                    "SELECT * FROM renewal_leases WHERE lease_id=? AND plan_id=?",
                    (lease_id, plan_id)).fetchone()
                if lease is None:
                    raise NotFoundError("租约不存在")
                if lease["status"] != "active":
                    raise ConflictError("租约已释放，不能重复操作")
                phase = self._phase_row(connection, plan_id, lease["phase_id"])
                unmet: list[dict[str, Any]] = []
                if phase["status"] != "accepted":
                    unmet.append({"name": "phase_accepted", "satisfied": False,
                                  "detail": f"阶段 {phase['phase_id']} 当前状态为 "
                                            f"{phase['status']}，验收通过前不能释放资源"})
                failure = None
                response: dict[str, Any] = {}
                if unmet:
                    append_event(connection, actor_id=actor_id,
                                 action="renewal_lease.release_rejected",
                                 resource_type="renewal_lease", resource_id=lease_id,
                                 detail={"plan_id": plan_id, "lease_id": lease_id,
                                         "unmet": unmet}, occurred_at=self._now())
                    failure = unmet
                else:
                    now = self._now()
                    connection.execute(
                        "UPDATE renewal_leases SET status='released', released_at=?, "
                        "release_reason=? WHERE lease_id=?",
                        (now, "阶段验收通过", lease_id))
                    append_event(connection, actor_id=actor_id, action="renewal_lease.released",
                                 resource_type="renewal_lease", resource_id=lease_id,
                                 detail={"plan_id": plan_id, "lease_id": lease_id,
                                         "resource_type": lease["resource_type"],
                                         "resource_id": lease["resource_id"]}, occurred_at=now)
                    response = {"lease_id": lease_id, "released": True,
                                "preconditions": [{"name": "phase_accepted", "satisfied": True,
                                                   "detail": "阶段已验收通过"}]}
                    self._store_receipt(connection, request_id=request_id, action="release_lease",
                                        payload=payload, resource_type="renewal_lease",
                                        resource_id=lease_id, response=response)
            if failure is not None:
                raise ConflictError("释放资源的前置条件未满足",
                                    extra={"unmet_preconditions": failure})
            return {**response, "replayed": False}

    def reopen_plan(self, *, request_id: str, actor_id: str, plan_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "plan_id": plan_id}
        with self._mutex:
            with self.database.transaction(immediate=True) as connection:
                actor = self._actor(connection, actor_id)
                self._require(actor, "operations")
                stored = self._replayed(connection, request_id=request_id,
                                        action="reopen_plan", payload=payload)
                if stored is not None:
                    return {**stored, "replayed": True}
                plan = self._plan_row(connection, plan_id)
                if plan["status"] == "reopened":
                    raise ConflictError("计划已复开，不能重复操作")
                if plan["status"] != "in_progress":
                    raise ConflictError(f"计划当前状态为 {plan['status']}，不能复开")
                preconditions = self._reopen_preconditions(connection, plan)
                unmet = [item for item in preconditions if not item["satisfied"]]
                failure = None
                response: dict[str, Any] = {}
                if unmet:
                    append_event(connection, actor_id=actor_id,
                                 action="renewal_plan.reopen_rejected",
                                 resource_type="renewal_plan", resource_id=plan_id,
                                 detail={"plan_id": plan_id, "unmet": unmet},
                                 occurred_at=self._now())
                    failure = unmet
                else:
                    connection.execute(
                        "UPDATE renewal_plans SET status='reopened', updated_at=? WHERE plan_id=?",
                        (self._now(), plan_id))
                    append_event(connection, actor_id=actor_id, action="renewal_plan.reopened",
                                 resource_type="renewal_plan", resource_id=plan_id,
                                 detail={"plan_id": plan_id, "preconditions": preconditions},
                                 occurred_at=self._now())
                    response = {"plan_id": plan_id, "status": "reopened",
                                "preconditions": preconditions}
                    self._store_receipt(connection, request_id=request_id, action="reopen_plan",
                                        payload=payload, resource_type="renewal_plan",
                                        resource_id=plan_id, response=response)
            if failure is not None:
                raise ConflictError("恢复通行的前置条件未满足",
                                    extra={"unmet_preconditions": failure})
            return {**response, "replayed": False}

    def _reopen_preconditions(self, connection, plan) -> list[dict[str, Any]]:
        plan_id = plan["plan_id"]
        pending = [row["phase_id"] for row in connection.execute(
            "SELECT phase_id FROM renewal_phases WHERE plan_id=? AND status!='accepted' "
            "ORDER BY seq", (plan_id,))]
        now = self._now_dt()
        open_outages = []
        for row in connection.execute(
                "SELECT outage_id, ended_at FROM renewal_outages WHERE plan_id=?", (plan_id,)):
            if row["ended_at"] is None or self._parse_dt(row["ended_at"], "ended_at") > now:
                open_outages.append(row["outage_id"])
        active_leases = [row["lease_id"] for row in connection.execute(
            "SELECT lease_id FROM renewal_leases WHERE plan_id=? AND status='active'", (plan_id,))]
        return [
            {"name": "all_phases_accepted", "satisfied": not pending,
             "detail": "全部阶段已验收通过" if not pending
                       else f"未验收通过的阶段：{'、'.join(pending)}"},
            {"name": "no_open_outages", "satisfied": not open_outages,
             "detail": "没有未结束的停运记录" if not open_outages
                       else f"未结束的停运记录：{'、'.join(open_outages)}"},
            {"name": "all_leases_released", "satisfied": not active_leases,
             "detail": "资源租约已全部释放" if not active_leases
                       else f"未释放的租约：{'、'.join(active_leases)}"},
        ]

    # ---- 查询、统一日历与重放 ----

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        connection = self.database.connection
        plan = self._plan_row(connection, plan_id)
        phases = []
        for row in connection.execute(
                "SELECT * FROM renewal_phases WHERE plan_id=? ORDER BY seq", (plan_id,)):
            phases.append({"phase_id": row["phase_id"], "seq": row["seq"], "name": row["name"],
                           "planned_start": row["planned_start"], "planned_end": row["planned_end"],
                           "closure_scope": row["closure_scope"], "work_type": row["work_type"],
                           "crew_id": row["crew_id"], "route_id": row["route_id"],
                           "route_capacity": row["route_capacity"], "material_id": row["material_id"],
                           "fund_id": row["fund_id"], "amount": row["amount"],
                           "status": row["status"], "actual_start": row["actual_start"],
                           "actual_end": row["actual_end"]})
        leases = [{"lease_id": row["lease_id"], "phase_id": row["phase_id"],
                   "resource_type": row["resource_type"], "resource_id": row["resource_id"],
                   "capacity": row["capacity"], "starts_at": row["starts_at"],
                   "ends_at": row["ends_at"], "status": row["status"],
                   "released_at": row["released_at"], "release_reason": row["release_reason"]}
                  for row in connection.execute(
                      "SELECT * FROM renewal_leases WHERE plan_id=? ORDER BY starts_at, lease_id",
                      (plan_id,))]
        conflicts = []
        for row in connection.execute(
                "SELECT * FROM renewal_conflicts WHERE plan_id=? ORDER BY detected_at, conflict_id",
                (plan_id,)):
            detail = json.loads(row["detail_json"])
            suggestion = detail.pop("suggestion", "")
            conflicts.append({"conflict_id": row["conflict_id"], "context": row["context"],
                              "conflict_type": row["conflict_type"], "message": row["message"],
                              "suggestion": suggestion, "detail": detail,
                              "detected_at": row["detected_at"]})
        outages = [{"outage_id": row["outage_id"], "phase_id": row["phase_id"],
                    "facility_id": row["facility_id"], "closure_scope": row["closure_scope"],
                    "reason": row["reason"], "started_at": row["started_at"],
                    "ended_at": row["ended_at"]}
                   for row in connection.execute(
                       "SELECT * FROM renewal_outages WHERE plan_id=? ORDER BY created_at, outage_id",
                       (plan_id,))]
        payments = [{"payment_id": row["payment_id"], "phase_id": row["phase_id"],
                     "fund_id": row["fund_id"], "amount": row["amount"], "reason": row["reason"],
                     "created_at": row["created_at"]}
                    for row in connection.execute(
                        "SELECT * FROM renewal_payments WHERE plan_id=? "
                        "ORDER BY created_at, payment_id", (plan_id,))]
        adjustments = [{"adjustment_id": row["adjustment_id"], "kind": row["kind"],
                        "reason": row["reason"], "phases": json.loads(row["phases_json"]),
                        "from_version": row["from_version"], "to_version": row["to_version"],
                        "created_by": row["created_by"], "created_at": row["created_at"]}
                       for row in connection.execute(
                           "SELECT * FROM renewal_adjustments WHERE plan_id=? "
                           "ORDER BY created_at, adjustment_id", (plan_id,))]
        return {"plan_id": plan_id, "site_id": plan["site_id"],
                "facility_id": plan["facility_id"], "title": plan["title"],
                "status": self._effective_status(plan), "version": plan["version"],
                "draft_expires_at": plan["draft_expires_at"], "created_by": plan["created_by"],
                "created_at": plan["created_at"], "updated_at": plan["updated_at"],
                "phases": phases, "approvals": self._approvals(connection, plan_id),
                "leases": leases, "conflicts": conflicts, "outages": outages,
                "payments": payments, "adjustments": adjustments,
                "reopen_preconditions": self._reopen_preconditions(connection, plan)}

    def list_plans(self, site_id: str, status: str | None = None) -> list[dict[str, Any]]:
        items = []
        for row in self.database.connection.execute(
                "SELECT * FROM renewal_plans WHERE site_id=? ORDER BY created_at, plan_id",
                (site_id,)):
            effective = self._effective_status(row)
            if status and effective != status:
                continue
            items.append({"plan_id": row["plan_id"], "facility_id": row["facility_id"],
                          "title": row["title"], "status": effective, "version": row["version"],
                          "draft_expires_at": row["draft_expires_at"],
                          "created_by": row["created_by"], "created_at": row["created_at"]})
        return items

    def calendar(self, site_id: str, start: str | None = None,
                 end: str | None = None) -> dict[str, Any]:
        """汇总统一日历：阶段窗口、资源租约、材料到场、资金期限与未结束停运。"""

        start_dt = self._parse_dt(start, "start") if start else None
        end_dt = self._parse_dt(end, "end") if end else None
        if start_dt and end_dt and end_dt <= start_dt:
            raise ValidationError("end 必须晚于 start")
        connection = self.database.connection

        def window_in_range(start_text: str, end_text: str) -> bool:
            ws = self._parse_dt(start_text, "start")
            we = self._parse_dt(end_text, "end")
            if start_dt and we <= start_dt:
                return False
            if end_dt and ws >= end_dt:
                return False
            return True

        def point_in_range(at_text: str) -> bool:
            at = self._parse_dt(at_text, "at")
            if start_dt and at < start_dt:
                return False
            if end_dt and at >= end_dt:
                return False
            return True

        entries: list[dict[str, Any]] = []
        for plan in connection.execute("SELECT * FROM renewal_plans WHERE site_id=?",
                                       (site_id,)):
            effective = self._effective_status(plan)
            if effective == "expired":
                continue
            for phase in connection.execute(
                    "SELECT * FROM renewal_phases WHERE plan_id=? ORDER BY seq",
                    (plan["plan_id"],)):
                if not window_in_range(phase["planned_start"], phase["planned_end"]):
                    continue
                entries.append({"kind": "phase_window", "plan_id": plan["plan_id"],
                                "plan_status": effective, "phase_id": phase["phase_id"],
                                "name": phase["name"], "start": phase["planned_start"],
                                "end": phase["planned_end"],
                                "closure_scope": phase["closure_scope"],
                                "work_type": phase["work_type"], "crew_id": phase["crew_id"],
                                "route_id": phase["route_id"], "phase_status": phase["status"]})
        for row in connection.execute(
                "SELECT l.* FROM renewal_leases l JOIN renewal_plans p ON p.plan_id=l.plan_id "
                "WHERE p.site_id=? AND l.status='active'", (site_id,)):
            if not window_in_range(row["starts_at"], row["ends_at"]):
                continue
            entries.append({"kind": "lease", "lease_id": row["lease_id"],
                            "plan_id": row["plan_id"], "resource_type": row["resource_type"],
                            "resource_id": row["resource_id"], "capacity": row["capacity"],
                            "start": row["starts_at"], "end": row["ends_at"]})
        for row in connection.execute("SELECT * FROM renewal_materials WHERE site_id=?",
                                      (site_id,)):
            if point_in_range(row["arrival_date"]):
                entries.append({"kind": "material_arrival", "material_id": row["material_id"],
                                "name": row["name"], "at": row["arrival_date"]})
        for row in connection.execute("SELECT * FROM renewal_funds WHERE site_id=?", (site_id,)):
            if point_in_range(row["deadline"]):
                entries.append({"kind": "fund_deadline", "fund_id": row["fund_id"],
                                "name": row["name"], "at": row["deadline"],
                                "amount": row["amount"]})
        now = self._now_dt()
        for row in connection.execute(
                "SELECT o.* FROM renewal_outages o JOIN renewal_plans p ON p.plan_id=o.plan_id "
                "WHERE p.site_id=?", (site_id,)):
            if row["ended_at"] is None or self._parse_dt(row["ended_at"], "ended_at") > now:
                entries.append({"kind": "outage", "outage_id": row["outage_id"],
                                "plan_id": row["plan_id"], "facility_id": row["facility_id"],
                                "closure_scope": row["closure_scope"],
                                "start": row["started_at"], "end": row["ended_at"]})
        facilities = [{"facility_id": row["facility_id"], "name": row["name"],
                       "facility_type": row["facility_type"],
                       "depends_on": json.loads(row["depends_on_json"])}
                      for row in connection.execute(
                          "SELECT * FROM renewal_facilities WHERE site_id=? ORDER BY facility_id",
                          (site_id,))]
        entries.sort(key=lambda entry: entry.get("start") or entry.get("at") or "")
        return {"site_id": site_id, "generated_at": self._now(), "facilities": facilities,
                "entries": entries}

    def replay_plan(self, plan_id: str) -> dict[str, Any]:
        """按审计链顺序重放一个更新计划从申报到复开的全过程。"""

        connection = self.database.connection
        plan = self._plan_row(connection, plan_id)
        events = []
        for row in connection.execute("SELECT * FROM audit_events ORDER BY sequence"):
            detail = json.loads(row["detail_json"])
            if row["resource_id"] != plan_id and detail.get("plan_id") != plan_id:
                continue
            events.append({"sequence": row["sequence"], "action": row["action"],
                           "actor_id": row["actor_id"], "occurred_at": row["occurred_at"],
                           "detail": detail})
        return {"plan_id": plan_id, "status": self._effective_status(plan), "events": events}

    def list_outages(self, site_id: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT o.* FROM renewal_outages o JOIN renewal_plans p ON p.plan_id=o.plan_id "
            "WHERE p.site_id=? ORDER BY o.created_at, o.outage_id", (site_id,)).fetchall()
        return [{"outage_id": row["outage_id"], "plan_id": row["plan_id"],
                 "phase_id": row["phase_id"], "facility_id": row["facility_id"],
                 "closure_scope": row["closure_scope"], "reason": row["reason"],
                 "started_at": row["started_at"], "ended_at": row["ended_at"]} for row in rows]

    def list_payments(self, site_id: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT pay.* FROM renewal_payments pay JOIN renewal_plans p ON p.plan_id=pay.plan_id "
            "WHERE p.site_id=? ORDER BY pay.created_at, pay.payment_id", (site_id,)).fetchall()
        return [{"payment_id": row["payment_id"], "plan_id": row["plan_id"],
                 "phase_id": row["phase_id"], "fund_id": row["fund_id"],
                 "amount": row["amount"], "reason": row["reason"],
                 "created_at": row["created_at"]} for row in rows]

    # ---- 行存取与状态 ----

    def _plan_row(self, connection, plan_id: str):
        row = connection.execute("SELECT * FROM renewal_plans WHERE plan_id=?",
                                 (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("更新计划不存在")
        return row

    def _phase_row(self, connection, plan_id: str, phase_id: str):
        row = connection.execute(
            "SELECT * FROM renewal_phases WHERE plan_id=? AND phase_id=?",
            (plan_id, phase_id)).fetchone()
        if row is None:
            raise NotFoundError("施工阶段不存在")
        return row

    def _facility(self, connection, facility_id: str):
        row = connection.execute("SELECT * FROM renewal_facilities WHERE facility_id=?",
                                 (facility_id,)).fetchone()
        if row is None:
            raise NotFoundError("设施不存在")
        return row

    def _approvals(self, connection, plan_id: str) -> list[dict[str, Any]]:
        return [{"party": row["party"], "party_label": PARTY_LABELS[row["party"]],
                 "actor_id": row["actor_id"], "comment": row["comment"],
                 "decided_at": row["decided_at"]}
                for row in connection.execute(
                    "SELECT * FROM renewal_approvals WHERE plan_id=? ORDER BY decided_at, party",
                    (plan_id,))]

    def _phase_specs_from_rows(self, connection, plan_id: str) -> list[dict[str, Any]]:
        specs = []
        for row in connection.execute(
                "SELECT * FROM renewal_phases WHERE plan_id=? ORDER BY seq", (plan_id,)):
            specs.append({"phase_id": row["phase_id"], "work_type": row["work_type"],
                          "crew_id": row["crew_id"], "route_id": row["route_id"],
                          "route_capacity": row["route_capacity"],
                          "start": self._parse_dt(row["planned_start"], "planned_start"),
                          "end": self._parse_dt(row["planned_end"], "planned_end")})
        return specs

    def _effective_status(self, plan) -> str:
        status = plan["status"]
        if status in ("draft", "approved") and self._parse_dt(
                plan["draft_expires_at"], "draft_expires_at") <= self._now_dt():
            return "expired"
        return status

    def _abort_if_expired(self, connection, plan) -> None:
        if self._effective_status(plan) == "expired" and plan["status"] != "expired":
            now = self._now()
            connection.execute(
                "UPDATE renewal_plans SET status='expired', updated_at=? WHERE plan_id=?",
                (now, plan["plan_id"]))
            append_event(connection, actor_id="system", action="renewal_plan.expired",
                         resource_type="renewal_plan", resource_id=plan["plan_id"],
                         detail={"plan_id": plan["plan_id"]}, occurred_at=now)
            raise ConflictError("草案已过会签期限，已按过期处理")
