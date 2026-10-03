"""运行设施更新窗口编排的离线端到端验收：通过 HTTP 语义接口重放全过程。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from transport_coordination.api import route
from transport_coordination.clock import FixedClock
from transport_coordination.service import DomainService
from transport_coordination.storage import Database


class MovableClock(FixedClock):
    def advance(self, minutes: int) -> None:
        self._value += timedelta(minutes=minutes)


def run() -> dict[str, object]:
    """通过 HTTP 路由重放一个更新窗口从申报到复开的全过程并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "acceptance.sqlite3"
        database = Database(db_path)
        clock = MovableClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        service = DomainService(database, clock)

        def call(method: str, path: str, body: dict | None = None, actor: str = "adm"):
            status, payload = route(service, method, path, body or {}, {"X-Actor-Id": actor})
            if status >= 400:
                raise AssertionError(f"{method} {path} -> {status}: {json.dumps(payload, ensure_ascii=False)}")
            return status, payload

        def phase(code, day, *, start="08:00", end="18:00", cost=200, volume=60,
                  qual="bridge", kind="renewal"):
            return {"phase_code": code, "title": code,
                    "planned_start": f"2026-10-{day:02d}T{start}:00Z",
                    "planned_end": f"2026-10-{day:02d}T{end}:00Z",
                    "closure_scope": "full", "diverted_volume": volume, "corridor_id": "detour",
                    "crew_id": "crew-a", "qualification": qual, "material_ids": ["steel"],
                    "cost": cost, "kind": kind}

        # ------------------------------------------------ 基础登记
        call("POST", "/organizations", {"request_id": "org-1", "organization_id": "o1",
                                        "name": "建设管理中心"}, actor="bootstrap")
        call("POST", "/actors", {"request_id": "adm-1", "new_actor_id": "adm", "display_name": "管理员",
                                 "role": "admin", "organization_id": "o1"}, actor="bootstrap")
        for rid, aid, name, role in [
            ("op-1", "op", "运营负责人", "operator"),
            ("co-1", "co", "施工负责人", "construction"),
            ("lo-1", "lo", "属地负责人", "locality"),
            ("rv-1", "rv", "验收员", "reviewer"),
        ]:
            call("POST", "/actors", {"request_id": rid, "new_actor_id": aid, "display_name": name,
                                     "role": role, "organization_id": "o1"})
        call("POST", "/sites", {"request_id": "site-1", "site_id": "site-1", "organization_id": "o1",
                                "name": "一号节点", "timezone_name": "Asia/Shanghai"})
        call("POST", "/facilities", {"request_id": "fac-1", "facility_id": "brg", "site_id": "site-1",
                                     "name": "老旧桥梁", "facility_type": "bridge"})
        call("POST", "/facilities", {"request_id": "fac-2", "facility_id": "tun", "site_id": "site-1",
                                     "name": "老旧隧道", "facility_type": "tunnel", "depends_on": ["brg"]})
        call("POST", "/corridors", {"request_id": "cor-1", "corridor_id": "detour", "name": "唯一替代通道",
                                    "spare_capacity": 100})
        call("POST", "/crews", {"request_id": "crew-1", "crew_id": "crew-a", "name": "甲专业队",
                                "qualifications": ["bridge", "tunnel"]})
        call("POST", "/materials", {"request_id": "mat-1", "material_id": "steel", "name": "钢构件",
                                    "arrived_at": "2026-09-20T00:00:00Z"})
        call("POST", "/funds", {"request_id": "fund-1", "fund_id": "fund-1", "name": "专项资金",
                                "amount": 1000, "valid_until": "2026-12-31T00:00:00Z"})

        # ------------------------------------------------ 窗口甲申报并锁定
        _, win_a = call("POST", "/renewal-windows", {"request_id": "win-a", "facility_id": "brg",
                                                     "fund_id": "fund-1", "title": "桥梁更新"}, actor="op")
        wid_a = win_a["window_id"]
        _, prop_a = call("POST", "/proposals", {"request_id": "pp-a1", "window_id": wid_a,
                                                "ttl_minutes": 600,
                                                "phases": [phase("ph1", 5, cost=300),
                                                           phase("ph2", 6, cost=200)]}, actor="op")
        pid_a = prop_a["proposal_id"]

        # 窗口乙在甲锁定前申报，单项可行
        _, win_b = call("POST", "/renewal-windows", {"request_id": "win-b", "facility_id": "tun",
                                                     "fund_id": "fund-1", "title": "隧道机电"}, actor="op")
        wid_b = win_b["window_id"]

        # 三方会签甲
        call("POST", "/proposals/sign", {"request_id": "sg-a1", "proposal_id": pid_a,
                                         "party": "operations"}, actor="op")
        call("POST", "/proposals/sign", {"request_id": "sg-a2", "proposal_id": pid_a,
                                         "party": "construction"}, actor="co")
        call("POST", "/proposals/sign", {"request_id": "sg-a3", "proposal_id": pid_a,
                                         "party": "locality"}, actor="lo")
        call("POST", "/proposals/confirm-lock", {"request_id": "lk-a", "proposal_id": pid_a}, actor="op")

        # ------------------------------------------------ 重启恢复：租约与会签均持久化
        restarted = Database(db_path)
        try:
            status, recovery = route(DomainService(restarted, clock), "POST", "/recover", {},
                                     {"X-Actor-Id": "adm"})
            assert status == 200
            entry_a = next(w for w in recovery["active_windows"] if w["window_id"] == wid_a)
            assert entry_a["status"] == "locked" and entry_a["leases"], entry_a
        finally:
            restarted.close()

        # ------------------------------------------------ 合在一起的冲突：乙与甲抢同一通道、同一队伍
        # 带冲突的方案仍形成限时草案（HTTP 201），冲突与取舍建议写进方案，供会签前解释
        _, conflict_prop = call("POST", "/proposals",
                                {"request_id": "pp-b1", "window_id": wid_b, "ttl_minutes": 600,
                                 "phases": [phase("tb1", 5, start="10:00", end="12:00", cost=100,
                                                  volume=50, qual="tunnel")]}, actor="op")
        assert {c["type"] for c in conflict_prop["conflicts"]} >= {"corridor_capacity",
                                                                   "crew_occupied"}
        assert any(t["type"] == "shift_phase" for t in conflict_prop["tradeoffs"])
        # 乙按取舍建议改到甲完工之后，冲突消除（新限时草案取代旧草案）
        call("POST", "/proposals", {"request_id": "pp-b2", "window_id": wid_b, "ttl_minutes": 60,
                                    "phases": [phase("tb1", 8, start="10:00", end="12:00", cost=100,
                                                    volume=50, qual="tunnel")]}, actor="op")

        # ------------------------------------------------ 执行甲：ph1 部分完工
        call("POST", "/phases/start", {"request_id": "st-1", "window_id": wid_a, "phase_code": "ph1"},
             actor="co")
        call("POST", "/phases/complete", {"request_id": "cp-1", "window_id": wid_a, "phase_code": "ph1",
                                          "partial": True, "amount": 100}, actor="co")
        # 部分完工后必须重排受影响阶段，不能直接复工
        status, _ = route(service, "POST", "/phases/start",
                          {"request_id": "st-1b", "window_id": wid_a, "phase_code": "ph1"},
                          {"X-Actor-Id": "co"})
        assert status == 409
        _, rs1 = call("POST", "/phases/reschedule", {"request_id": "rs-1", "window_id": wid_a,
                                                     "phase_code": "ph1",
                                                     "changes": {"planned_start": "2026-10-09T08:00:00Z",
                                                                 "planned_end": "2026-10-09T18:00:00Z",
                                                                 "reason": "partial-continue"}},
                      actor="co")
        for rid2, actor2, party in [("rs1-1", "op", "operations"), ("rs1-2", "co", "construction"),
                                    ("rs1-3", "lo", "locality")]:
            call("POST", "/proposals/sign", {"request_id": rid2, "proposal_id": rs1["proposal_id"],
                                             "party": party}, actor=actor2)
        call("POST", "/proposals/confirm-lock", {"request_id": "rs1-lk",
                                                 "proposal_id": rs1["proposal_id"]}, actor="op")
        call("POST", "/phases/start", {"request_id": "st-1c", "window_id": wid_a, "phase_code": "ph1"},
             actor="co")
        call("POST", "/phases/complete", {"request_id": "cp-1b", "window_id": wid_a,
                                          "phase_code": "ph1"}, actor="co")

        # ------------------------------------------------ ph2 完工被验收退回，支付与停运保留
        call("POST", "/phases/start", {"request_id": "st-2", "window_id": wid_a, "phase_code": "ph2"},
             actor="co")
        call("POST", "/phases/complete", {"request_id": "cp-2", "window_id": wid_a,
                                          "phase_code": "ph2"}, actor="co")
        call("POST", "/phases/reject", {"request_id": "rj-1", "window_id": wid_a, "phase_code": "ph2",
                                        "note": "防水层不合格"}, actor="rv")
        _, rs2 = call("POST", "/phases/reschedule", {"request_id": "rs-2", "window_id": wid_a,
                                                     "phase_code": "ph2",
                                                     "changes": {"planned_start": "2026-10-12T08:00:00Z",
                                                                 "planned_end": "2026-10-12T18:00:00Z",
                                                                 "reason": "waterproof-rework"}},
                      actor="co")
        for rid2, actor2, party in [("rs2-1", "op", "operations"), ("rs2-2", "co", "construction"),
                                    ("rs2-3", "lo", "locality")]:
            call("POST", "/proposals/sign", {"request_id": rid2, "proposal_id": rs2["proposal_id"],
                                             "party": party}, actor=actor2)
        call("POST", "/proposals/confirm-lock", {"request_id": "rs2-lk",
                                                 "proposal_id": rs2["proposal_id"]}, actor="op")

        # ------------------------------------------------ 紧急抢修只插入受影响阶段
        _, em = call("POST", "/phases/emergency-repair",
                     {"request_id": "em-1", "window_id": wid_a, "ttl_minutes": 360,
                      "phase": phase("em1", 11, start="08:00", end="12:00", cost=50, volume=30,
                                     kind="emergency")}, actor="op")
        assert em["conflicts"] == [], em["conflicts"]
        for rid2, actor2, party in [("em-s1", "op", "operations"), ("em-s2", "co", "construction"),
                                    ("em-s3", "lo", "locality")]:
            call("POST", "/proposals/sign", {"request_id": rid2, "proposal_id": em["proposal_id"],
                                             "party": party}, actor=actor2)
        call("POST", "/proposals/confirm-lock", {"request_id": "em-lk",
                                                 "proposal_id": em["proposal_id"]}, actor="op")
        call("POST", "/phases/start", {"request_id": "em-st", "window_id": wid_a, "phase_code": "em1"},
             actor="co")
        call("POST", "/phases/complete", {"request_id": "em-cp", "window_id": wid_a,
                                          "phase_code": "em1"}, actor="co")

        # ------------------------------------------------ ph2 返工复工（不再产生重复支付），复开
        call("POST", "/phases/start", {"request_id": "st-2b", "window_id": wid_a, "phase_code": "ph2"},
             actor="co")
        call("POST", "/phases/complete", {"request_id": "cp-2b", "window_id": wid_a,
                                          "phase_code": "ph2"}, actor="co")
        # 前置条件不满足时不能复开：乙窗口还有限时草案（不影响甲，故甲可复开）
        _, reopened = call("POST", "/windows/reopen", {"request_id": "rp-1", "window_id": wid_a},
                           actor="op")
        assert reopened["status"] == "reopened"

        # ------------------------------------------------ 重放与不可变记录核对
        _, timeline = call("GET", f"/windows/{wid_a}/timeline")
        event_types = [e["event_type"] for e in timeline["events"]]
        assert "window.declared" in event_types and "window.reopened" in event_types
        assert "window.rescheduled" in event_types
        outage_count = len(timeline["outage_records"])  # ph1 两次 + ph2 两次 + em1
        payment_total = sum(p["amount"] for p in timeline["payment_records"])
        payments = [(p["phase_code"], p["amount"]) for p in timeline["payment_records"]]

        # 乙的限时草案在时钟越过 TTL 后，由重启恢复自动收敛为 expired
        clock.advance(60 * 24 * 2)
        restarted = Database(db_path)
        try:
            _, recovery2 = route(DomainService(restarted, clock), "POST", "/recover", {},
                                 {"X-Actor-Id": "adm"})
        finally:
            restarted.close()
        entry_b = next(w for w in recovery2["active_windows"] if w["window_id"] == wid_b)
        assert all(p["status"] == "expired" for p in entry_b["proposals"]), entry_b

        valid, audit_count = service.verify_audit()
        database.close()
        return {"status": "ok", "audit_valid": valid, "audit_events": audit_count,
                "outage_records": outage_count, "payment_records": len(payments),
                "payment_total": payment_total, "payments": payments,
                "released_leases_on_reopen": reopened["released_leases"],
                "conflict_explained": True, "revisions_locked": 4}


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
