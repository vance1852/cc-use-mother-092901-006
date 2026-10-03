"""通过 HTTP 边界重放设施更新计划从申报到复开的离线验收。

验收覆盖：统一资源登记、限时草案、三方会签、冲突解释与取舍、调整版本裁决、
停运与支付记录不可抹除、释放与复开前置条件、服务重启后租约与审批恢复。
"""

from __future__ import annotations

import itertools
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .api import route
from .clock import FixedClock
from .renewal import RenewalService
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行完整编排流程并返回可核对的结果。"""

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "renewal.sqlite3"
        clock = FixedClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        database = Database(path)
        base = DomainService(database, clock)
        renewal = RenewalService(database, clock)
        counter = itertools.count(1)

        def call(method: str, url: str, body: dict | None = None, actor: str = "") -> dict:
            status, payload = route(base, method, url, body or {},
                                    {"X-Actor-Id": actor}, renewal=renewal)
            if status >= 400:
                raise RuntimeError(f"{method} {url} 返回 {status}: {payload}")
            return payload

        def expect(status_expected: int, method: str, url: str,
                   body: dict | None = None, actor: str = "") -> dict:
            status, payload = route(base, method, url, body or {},
                                    {"X-Actor-Id": actor}, renewal=renewal)
            if status != status_expected:
                raise RuntimeError(f"{method} {url} 期望 {status_expected} 实际 {status}: {payload}")
            return payload

        def req() -> str:
            return f"req-{next(counter):03d}"

        # 1. 主体、场所与资源登记
        call("POST", "/organizations", {"request_id": req(), "organization_id": "org-001",
                                        "name": "建设管理中心"}, actor="bootstrap")
        call("POST", "/actors", {"request_id": req(), "new_actor_id": "admin-001",
                                 "display_name": "系统管理员", "role": "admin",
                                 "organization_id": "org-001"}, actor="bootstrap")
        for actor_id, name, role in [
                ("planner-001", "计划员", "planner"), ("ops-001", "运营会签", "operations"),
                ("con-001", "施工会签", "construction"), ("loc-001", "属地会签", "local_manager"),
                ("op-001", "资料员", "operator")]:
            call("POST", "/actors", {"request_id": req(), "new_actor_id": actor_id,
                                     "display_name": name, "role": role,
                                     "organization_id": "org-001"}, actor="admin-001")
        call("POST", "/sites", {"request_id": req(), "site_id": "site-001",
                                "organization_id": "org-001", "name": "城东节点",
                                "timezone_name": "Asia/Shanghai"}, actor="admin-001")
        call("POST", "/renewal/facilities",
             {"request_id": req(), "site_id": "site-001", "facility_id": "fac-detour",
              "name": "绕行桥", "facility_type": "bridge"}, actor="op-001")
        call("POST", "/renewal/facilities",
             {"request_id": req(), "site_id": "site-001", "facility_id": "fac-bridge",
              "name": "老旧主桥", "facility_type": "bridge",
              "depends_on": ["fac-detour"]}, actor="op-001")
        call("POST", "/renewal/facilities",
             {"request_id": req(), "site_id": "site-001", "facility_id": "fac-station",
              "name": "客运站", "facility_type": "passenger_station"}, actor="op-001")
        call("POST", "/renewal/routes",
             {"request_id": req(), "site_id": "site-001", "route_id": "route-1",
              "name": "替代通道一", "capacity": 4000}, actor="op-001")
        call("POST", "/renewal/crews",
             {"request_id": req(), "site_id": "site-001", "crew_id": "crew-a",
              "name": "综合一队", "qualifications": ["bridge_structure", "station_equipment"]},
             actor="op-001")
        call("POST", "/renewal/crews",
             {"request_id": req(), "site_id": "site-001", "crew_id": "crew-b",
              "name": "设备二队", "qualifications": ["station_equipment"]}, actor="op-001")
        call("POST", "/renewal/funds",
             {"request_id": req(), "site_id": "site-001", "fund_id": "fund-1",
              "name": "更新专项资金", "amount": 500000,
              "deadline": "2026-12-31T00:00:00Z"}, actor="op-001")
        call("POST", "/renewal/materials",
             {"request_id": req(), "site_id": "site-001", "material_id": "mat-1",
              "name": "桥梁支座", "arrival_date": "2026-10-01T00:00:00Z"}, actor="op-001")

        # 2. 两份计划各自申报，单项均可行
        plan_a = call("POST", "/renewal/plans", {
            "request_id": req(), "site_id": "site-001", "plan_id": "plan-a",
            "facility_id": "fac-bridge", "title": "主桥更新",
            "phases": [
                {"phase_id": "ph-a1", "name": "下部结构加固", "start": "2026-10-10T00:00:00Z",
                 "end": "2026-10-20T00:00:00Z", "closure_scope": "半幅封闭",
                 "work_type": "bridge_structure", "crew_id": "crew-a", "route_id": "route-1",
                 "route_capacity": 2000, "material_id": "mat-1", "fund_id": "fund-1",
                 "amount": 200000},
                {"phase_id": "ph-a2", "name": "桥面系更新", "start": "2026-10-21T00:00:00Z",
                 "end": "2026-10-31T00:00:00Z", "closure_scope": "全幅夜间封闭",
                 "work_type": "bridge_structure", "crew_id": "crew-a", "route_id": "route-1",
                 "route_capacity": 2000, "fund_id": "fund-1", "amount": 150000}],
        }, actor="planner-001")
        assert plan_a["status"] == "draft" and not plan_a["conflicts"]
        plan_b = call("POST", "/renewal/plans", {
            "request_id": req(), "site_id": "site-001", "plan_id": "plan-b",
            "facility_id": "fac-station", "title": "客运站设备更新",
            "phases": [
                {"phase_id": "ph-b1", "name": "站厅设备更换", "start": "2026-10-15T00:00:00Z",
                 "end": "2026-10-25T00:00:00Z", "closure_scope": "站厅封闭",
                 "work_type": "station_equipment", "crew_id": "crew-a", "route_id": "route-1",
                 "route_capacity": 2500, "fund_id": "fund-1", "amount": 100000}],
        }, actor="planner-001")
        assert plan_b["status"] == "draft" and not plan_b["conflicts"]

        # 3. plan-a 三方会签后锁定资源
        for party, actor in [("operations", "ops-001"), ("construction", "con-001"),
                             ("local_manager", "loc-001")]:
            result = call("POST", "/renewal/plans/plan-a/approvals",
                          {"request_id": req(), "party": party}, actor=actor)
        assert result["status"] == "locked" and result["lock"]["acquired"]

        # 4. plan-b 会签完成但锁定被冲突拦截，平台解释冲突与取舍
        for party, actor in [("operations", "ops-001"), ("construction", "con-001"),
                             ("local_manager", "loc-001")]:
            result = call("POST", "/renewal/plans/plan-b/approvals",
                          {"request_id": req(), "party": party}, actor=actor)
        assert result["status"] == "approved" and not result["lock"]["acquired"]
        conflict_types = {item["conflict_type"] for item in result["lock"]["conflicts"]}
        assert {"crew_double_booked", "route_capacity_exceeded"} <= conflict_types
        conflict_explained = all(item["message"] and item["suggestion"]
                                 for item in result["lock"]["conflicts"])
        rejected = expect(409, "POST", "/renewal/plans/plan-b/lock",
                          {"request_id": req()}, actor="planner-001")
        assert rejected["conflicts"]

        # 5. plan-b 接受取舍：让行至 11 月窗口后锁定成功
        call("POST", "/renewal/plans/plan-b/adjustments", {
            "request_id": req(), "kind": "delay", "expected_version": 1,
            "reason": "避开主桥锁定窗口",
            "phases": [{"phase_id": "ph-b1", "start": "2026-11-05T00:00:00Z",
                        "end": "2026-11-15T00:00:00Z"}]}, actor="planner-001")
        locked_b = call("POST", "/renewal/plans/plan-b/lock",
                        {"request_id": req()}, actor="planner-001")
        assert locked_b["status"] == "locked"

        # 6. plan-a 施工：开工、部分完工计量、阶段二顺延（含并发版本裁决）
        call("POST", "/renewal/plans/plan-a/phases/ph-a1/start",
             {"request_id": req()}, actor="con-001")
        call("POST", "/renewal/plans/plan-a/adjustments", {
            "request_id": req(), "kind": "partial_complete", "expected_version": 1,
            "reason": "首期计量", "phase_id": "ph-a1", "amount": 80000}, actor="con-001")
        history_before = call("GET", "/renewal/plans/plan-a")
        expect(409, "POST", "/renewal/plans/plan-a/adjustments", {
            "request_id": req(), "kind": "delay", "expected_version": 1,
            "reason": "过期版本的并发方案",
            "phases": [{"phase_id": "ph-a2", "start": "2026-10-25T00:00:00Z",
                        "end": "2026-11-05T00:00:00Z"}]}, actor="planner-001")
        call("POST", "/renewal/plans/plan-a/adjustments", {
            "request_id": req(), "kind": "delay", "expected_version": 2,
            "reason": "阶段二顺延一天",
            "phases": [{"phase_id": "ph-a2", "start": "2026-10-22T00:00:00Z",
                        "end": "2026-11-01T00:00:00Z"}]}, actor="planner-001")
        history_after = call("GET", "/renewal/plans/plan-a")
        history_preserved = (history_before["outages"] == history_after["outages"]
                             and history_before["payments"] == history_after["payments"])
        assert history_preserved
        call("POST", "/renewal/plans/plan-a/phases/ph-a1/complete",
             {"request_id": req(), "amount": 120000}, actor="con-001")
        call("POST", "/renewal/plans/plan-a/phases/ph-a1/accept",
             {"request_id": req()}, actor="ops-001")
        call("POST", "/renewal/plans/plan-a/phases/ph-a2/start",
             {"request_id": req()}, actor="con-001")
        call("POST", "/renewal/plans/plan-a/phases/ph-a2/complete",
             {"request_id": req(), "amount": 150000}, actor="con-001")
        call("POST", "/renewal/plans/plan-a/phases/ph-a2/accept",
             {"request_id": req()}, actor="ops-001")

        # 7. 复开前置条件：未释放租约时拒绝，释放后复开
        unmet = expect(409, "POST", "/renewal/plans/plan-a/reopen",
                       {"request_id": req()}, actor="ops-001")
        assert "all_leases_released" in {item["name"] for item in unmet["unmet_preconditions"]}
        for lease in call("GET", "/renewal/plans/plan-a")["leases"]:
            call("POST", f"/renewal/plans/plan-a/leases/{lease['lease_id']}/release",
                 {"request_id": req()}, actor="planner-001")
        reopened_a = call("POST", "/renewal/plans/plan-a/reopen",
                          {"request_id": req()}, actor="ops-001")
        assert reopened_a["status"] == "reopened"
        assert all(item["satisfied"] for item in reopened_a["preconditions"])

        # 8. 服务重启：未结束的租约与审批从 SQLite 恢复
        database.close()
        database = Database(path)
        base = DomainService(database, clock)
        renewal = RenewalService(database, clock)
        recovered_b = call("GET", "/renewal/plans/plan-b")
        restart_recovered = (recovered_b["status"] == "locked"
                             and len(recovered_b["approvals"]) == 3
                             and sum(1 for lease in recovered_b["leases"]
                                     if lease["status"] == "active") == 2
                             and call("GET", "/renewal/plans/plan-a")["status"] == "reopened")
        assert restart_recovered

        # 9. 重启后 plan-b 继续走完全生命周期
        call("POST", "/renewal/plans/plan-b/phases/ph-b1/start",
             {"request_id": req()}, actor="con-001")
        call("POST", "/renewal/plans/plan-b/phases/ph-b1/complete",
             {"request_id": req(), "amount": 100000}, actor="con-001")
        call("POST", "/renewal/plans/plan-b/phases/ph-b1/accept",
             {"request_id": req()}, actor="ops-001")
        for lease in call("GET", "/renewal/plans/plan-b")["leases"]:
            call("POST", f"/renewal/plans/plan-b/leases/{lease['lease_id']}/release",
                 {"request_id": req()}, actor="planner-001")
        reopened_b = call("POST", "/renewal/plans/plan-b/reopen",
                          {"request_id": req()}, actor="ops-001")
        assert reopened_b["status"] == "reopened"

        # 10. 重放 plan-a 从申报到复开的全过程
        replay = call("GET", "/renewal/plans/plan-a/replay")
        actions = [event["action"] for event in replay["events"]]
        assert actions[0] == "renewal_plan.drafted"
        assert actions[-1] == "renewal_plan.reopened"
        assert actions.count("renewal_plan.approved") == 3
        assert actions.index("renewal_plan.locked") < actions.index("renewal_phase.started")
        assert "renewal_plan.adjusted" in actions
        calendar = call("GET", "/renewal/calendar?site_id=site-001")
        assert calendar["entries"]
        valid, event_count = base.verify_audit()
        result = {"status": "ok", "plans": 2, "reopened": ["plan-a", "plan-b"],
                  "conflict_types": sorted(conflict_types),
                  "conflict_explained": conflict_explained,
                  "history_preserved": history_preserved,
                  "restart_recovered": restart_recovered,
                  "replayed_events": len(replay["events"]),
                  "calendar_entries": len(calendar["entries"]),
                  "audit_valid": valid, "audit_events": event_count}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
