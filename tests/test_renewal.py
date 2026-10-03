import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from transport_coordination.errors import (ConflictError, NotFoundError,
                                           PermissionDenied, ValidationError)
from transport_coordination.renewal import RenewalService
from transport_coordination.service import DomainService
from transport_coordination.storage import Database


BASE_TIME = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


class StepClock:
    """测试用可推进时钟。"""

    def __init__(self, value):
        self._value = value

    def now(self):
        return self._value

    def advance(self, **kwargs):
        self._value = self._value + timedelta(**kwargs)


def bootstrap(base, renewal):
    """登记组织、角色、场所与编排资源。"""
    base.register_organization(request_id="org", actor_id="bootstrap",
                               organization_id="o1", name="建设管理中心")
    base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                        display_name="管理员", role="admin", organization_id="o1")
    for request_id, actor_id, role, name in [
            ("actor-planner", "p1", "planner", "计划员"),
            ("actor-ops", "ops1", "operations", "运营会签"),
            ("actor-con", "con1", "construction", "施工会签"),
            ("actor-loc", "loc1", "local_manager", "属地会签"),
            ("actor-op", "op1", "operator", "资料员")]:
        base.register_actor(request_id=request_id, actor_id="a1", new_actor_id=actor_id,
                            display_name=name, role=role, organization_id="o1")
    base.register_site(request_id="site", actor_id="a1", site_id="s1",
                       organization_id="o1", name="一区节点", timezone_name="Asia/Shanghai")
    renewal.register_facility(request_id="fac-2", actor_id="op1", site_id="s1",
                              facility_id="f2", name="绕行桥", facility_type="bridge")
    renewal.register_facility(request_id="fac-1", actor_id="op1", site_id="s1",
                              facility_id="f1", name="老桥", facility_type="bridge",
                              depends_on=["f2"])
    renewal.register_facility(request_id="fac-3", actor_id="op1", site_id="s1",
                              facility_id="f3", name="客运站",
                              facility_type="passenger_station")
    renewal.register_route(request_id="route-1", actor_id="op1", site_id="s1",
                           route_id="r1", name="替代通道一", capacity=4000)
    renewal.register_crew(request_id="crew-1", actor_id="op1", site_id="s1",
                          crew_id="c1", name="桥梁一队", qualifications=["wt1"])
    renewal.register_crew(request_id="crew-2", actor_id="op1", site_id="s1",
                          crew_id="c2", name="桥梁二队", qualifications=["wt1"])
    renewal.register_fund(request_id="fund-1", actor_id="op1", site_id="s1",
                          fund_id="fund1", name="专项资金", amount=500000,
                          deadline="2026-12-31T00:00:00Z")
    renewal.register_material(request_id="mat-1", actor_id="op1", site_id="s1",
                              material_id="m1", name="支座",
                              arrival_date="2026-10-01T00:00:00Z")


def make_phases(start="2026-10-10T00:00:00Z", end="2026-10-20T00:00:00Z",
                crew="c1", route_cap=2000, with_second=False):
    phases = [{"phase_id": "ph1", "name": "阶段一", "start": start, "end": end,
               "closure_scope": "半幅封闭", "work_type": "wt1", "crew_id": crew,
               "route_id": "r1", "route_capacity": route_cap, "material_id": "m1",
               "fund_id": "fund1", "amount": 200000}]
    if with_second:
        phases.append({"phase_id": "ph2", "name": "阶段二",
                       "start": "2026-10-21T00:00:00Z", "end": "2026-10-31T00:00:00Z",
                       "closure_scope": "全幅封闭", "work_type": "wt1", "crew_id": crew,
                       "route_id": "r1", "route_capacity": 2000, "fund_id": "fund1",
                       "amount": 150000})
    return phases


class RenewalServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = StepClock(BASE_TIME)
        self.base = DomainService(self.database, self.clock)
        self.service = RenewalService(self.database, self.clock)
        bootstrap(self.base, self.service)

    def tearDown(self):
        self.database.close()

    def _create_plan(self, plan_id="plan-1", facility="f3", phases=None, ttl=None):
        kwargs = dict(request_id=f"req-{plan_id}", actor_id="p1", site_id="s1",
                      plan_id=plan_id, facility_id=facility, title=f"计划{plan_id}",
                      phases=phases or make_phases())
        if ttl is not None:
            kwargs["draft_ttl_hours"] = ttl
        return self.service.create_plan(**kwargs)

    def _approve_all(self, plan_id):
        result = None
        for party, actor in [("operations", "ops1"), ("construction", "con1"),
                             ("local_manager", "loc1")]:
            result = self.service.approve_plan(
                request_id=f"ap-{plan_id}-{party}", actor_id=actor,
                plan_id=plan_id, party=party)
        return result

    def _lock(self, plan_id="plan-1", **kwargs):
        self._create_plan(plan_id=plan_id, **kwargs)
        return self._approve_all(plan_id)

    def test_full_lifecycle_to_reopen(self):
        result = self._lock(phases=make_phases(with_second=True))
        self.assertEqual("locked", result["status"])
        self.assertTrue(result["lock"]["acquired"])
        self.service.start_phase(request_id="s1", actor_id="con1",
                                 plan_id="plan-1", phase_id="ph1")
        self.service.complete_phase(request_id="c1", actor_id="con1",
                                    plan_id="plan-1", phase_id="ph1", amount=200000)
        self.service.accept_phase(request_id="a1", actor_id="ops1",
                                  plan_id="plan-1", phase_id="ph1")
        self.service.start_phase(request_id="s2", actor_id="con1",
                                 plan_id="plan-1", phase_id="ph2")
        self.service.complete_phase(request_id="c2", actor_id="con1",
                                    plan_id="plan-1", phase_id="ph2", amount=150000)
        self.service.accept_phase(request_id="a2", actor_id="ops1",
                                  plan_id="plan-1", phase_id="ph2")
        plan = self.service.get_plan("plan-1")
        for lease in plan["leases"]:
            self.service.release_lease(request_id=f"rel-{lease['lease_id']}",
                                       actor_id="p1", plan_id="plan-1",
                                       lease_id=lease["lease_id"])
        reopened = self.service.reopen_plan(request_id="reopen-1", actor_id="ops1",
                                            plan_id="plan-1")
        self.assertEqual("reopened", reopened["status"])
        self.assertTrue(all(item["satisfied"] for item in reopened["preconditions"]))
        plan = self.service.get_plan("plan-1")
        self.assertEqual("reopened", plan["status"])
        self.assertEqual(2, len(plan["outages"]))
        self.assertTrue(all(outage["ended_at"] for outage in plan["outages"]))
        self.assertEqual(350000, sum(payment["amount"] for payment in plan["payments"]))

    def test_competing_plan_lock_conflict_explained(self):
        self._lock(phases=make_phases(with_second=True))
        draft = self.service.create_plan(
            request_id="req-plan-2", actor_id="p1", site_id="s1", plan_id="plan-2",
            facility_id="f3", title="客运站设备更新",
            phases=[{"phase_id": "ph1", "name": "设备更换",
                     "start": "2026-10-15T00:00:00Z", "end": "2026-10-25T00:00:00Z",
                     "closure_scope": "站厅封闭", "work_type": "wt1", "crew_id": "c1",
                     "route_id": "r1", "route_capacity": 3000, "fund_id": "fund1",
                     "amount": 100000}])
        types = {conflict["conflict_type"] for conflict in draft["conflicts"]}
        self.assertIn("crew_double_booked", types)
        self.assertIn("route_capacity_exceeded", types)
        result = self._approve_all("plan-2")
        self.assertEqual("approved", result["status"])
        self.assertFalse(result["lock"]["acquired"])
        with self.assertRaises(ConflictError) as ctx:
            self.service.lock_plan(request_id="lock-plan-2", actor_id="p1", plan_id="plan-2")
        conflicts = ctx.exception.extra["conflicts"]
        self.assertTrue(conflicts)
        self.assertTrue(all(conflict["suggestion"] for conflict in conflicts))
        crew_conflict = next(c for c in conflicts if c["conflict_type"] == "crew_double_booked")
        self.assertIn("c2", crew_conflict["suggestion"])

    def test_adjustment_resolves_conflict_and_locks(self):
        self._lock(phases=make_phases(with_second=True))
        self._create_plan(plan_id="plan-2", phases=make_phases(
            start="2026-10-15T00:00:00Z", end="2026-10-25T00:00:00Z"))
        result = self._approve_all("plan-2")
        self.assertFalse(result["lock"]["acquired"])
        adjusted = self.service.adjust_plan(
            request_id="adj-1", actor_id="p1", plan_id="plan-2", kind="delay",
            expected_version=1, reason="避开已锁定窗口",
            phases=[{"phase_id": "ph1", "start": "2026-11-05T00:00:00Z",
                     "end": "2026-11-15T00:00:00Z"}])
        self.assertEqual(2, adjusted["version"])
        locked = self.service.lock_plan(request_id="lock-plan-2", actor_id="p1",
                                        plan_id="plan-2")
        self.assertEqual("locked", locked["status"])

    def test_only_one_adjustment_survives_per_version(self):
        self._lock()
        first = self.service.adjust_plan(
            request_id="adj-1", actor_id="p1", plan_id="plan-1", kind="delay",
            expected_version=1, reason="顺延",
            phases=[{"phase_id": "ph1", "start": "2026-10-12T00:00:00Z",
                     "end": "2026-10-22T00:00:00Z"}])
        self.assertEqual(2, first["version"])
        with self.assertRaises(ConflictError):
            self.service.adjust_plan(
                request_id="adj-2", actor_id="p1", plan_id="plan-1", kind="delay",
                expected_version=1, reason="并发方案",
                phases=[{"phase_id": "ph1", "start": "2026-10-13T00:00:00Z",
                         "end": "2026-10-23T00:00:00Z"}])
        self.assertEqual(2, self.service.get_plan("plan-1")["version"])

    def test_concurrent_adjustments_have_single_winner(self):
        self._lock()
        barrier = threading.Barrier(2)
        outcomes = []

        def attempt(tag, start, end):
            try:
                barrier.wait(timeout=5)
                self.service.adjust_plan(
                    request_id=f"adj-{tag}", actor_id="p1", plan_id="plan-1",
                    kind="delay", expected_version=1, reason="并发调整",
                    phases=[{"phase_id": "ph1", "start": start, "end": end}])
                outcomes.append("ok")
            except ConflictError:
                outcomes.append("conflict")

        threads = [threading.Thread(target=attempt, args=(
            "a", "2026-10-12T00:00:00Z", "2026-10-22T00:00:00Z")),
            threading.Thread(target=attempt, args=(
                "b", "2026-10-13T00:00:00Z", "2026-10-23T00:00:00Z"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(["conflict", "ok"], sorted(outcomes))
        self.assertEqual(2, self.service.get_plan("plan-1")["version"])

    def test_draft_expires_after_ttl(self):
        self._create_plan(ttl=1)
        self.clock.advance(hours=2)
        with self.assertRaises(ConflictError):
            self.service.approve_plan(request_id="ap-x", actor_id="ops1",
                                      plan_id="plan-1", party="operations")
        self.assertEqual("expired", self.service.get_plan("plan-1")["status"])

    def test_approval_requires_matching_party(self):
        self._create_plan()
        with self.assertRaises(PermissionDenied):
            self.service.approve_plan(request_id="ap-bad", actor_id="ops1",
                                      plan_id="plan-1", party="construction")
        self.service.approve_plan(request_id="ap-ok", actor_id="ops1",
                                  plan_id="plan-1", party="operations")
        with self.assertRaises(ConflictError):
            self.service.approve_plan(request_id="ap-dup", actor_id="ops1",
                                      plan_id="plan-1", party="operations")

    def test_adjustment_preserves_outage_and_payment_records(self):
        self._lock(phases=make_phases(with_second=True))
        self.service.start_phase(request_id="s1", actor_id="con1",
                                 plan_id="plan-1", phase_id="ph1")
        self.service.adjust_plan(request_id="adj-partial", actor_id="con1",
                                 plan_id="plan-1", kind="partial_complete",
                                 expected_version=1, reason="完成一半",
                                 phase_id="ph1", amount=80000)
        self.service.complete_phase(request_id="c1", actor_id="con1",
                                    plan_id="plan-1", phase_id="ph1", amount=120000)
        before = self.service.get_plan("plan-1")
        self.service.adjust_plan(request_id="adj-delay", actor_id="p1",
                                 plan_id="plan-1", kind="delay", expected_version=2,
                                 reason="阶段二顺延",
                                 phases=[{"phase_id": "ph2",
                                          "start": "2026-10-23T00:00:00Z",
                                          "end": "2026-11-02T00:00:00Z"}])
        after = self.service.get_plan("plan-1")
        self.assertEqual(before["outages"], after["outages"])
        self.assertEqual(before["payments"], after["payments"])
        self.assertEqual(2, len(after["payments"]))

    def test_completed_phase_cannot_be_rescheduled(self):
        self._lock(phases=make_phases(with_second=True))
        self.service.start_phase(request_id="s1", actor_id="con1",
                                 plan_id="plan-1", phase_id="ph1")
        self.service.complete_phase(request_id="c1", actor_id="con1",
                                    plan_id="plan-1", phase_id="ph1", amount=200000)
        with self.assertRaises(ConflictError):
            self.service.adjust_plan(
                request_id="adj-x", actor_id="p1", plan_id="plan-1", kind="delay",
                expected_version=1, reason="试图改写已完成阶段",
                phases=[{"phase_id": "ph1", "start": "2026-10-15T00:00:00Z",
                         "end": "2026-10-25T00:00:00Z"}])

    def test_acceptance_rejection_triggers_rework_reschedule(self):
        self._lock()
        self.service.start_phase(request_id="s1", actor_id="con1",
                                 plan_id="plan-1", phase_id="ph1")
        self.service.complete_phase(request_id="c1", actor_id="con1",
                                    plan_id="plan-1", phase_id="ph1", amount=200000)
        self.service.reject_phase(request_id="rj1", actor_id="ops1",
                                  plan_id="plan-1", phase_id="ph1", reason="压实度不足")
        self.assertEqual("rejected",
                         self.service.get_plan("plan-1")["phases"][0]["status"])
        self.service.adjust_plan(
            request_id="adj-rw", actor_id="p1", plan_id="plan-1",
            kind="acceptance_reject", expected_version=1, reason="返工窗口",
            phases=[{"phase_id": "ph1", "start": "2026-10-22T00:00:00Z",
                     "end": "2026-10-28T00:00:00Z"}])
        phase = self.service.get_plan("plan-1")["phases"][0]
        self.assertEqual("scheduled", phase["status"])
        self.assertIsNone(phase["actual_start"])
        plan = self.service.get_plan("plan-1")
        self.assertEqual(1, len(plan["outages"]))
        self.assertEqual(200000, plan["payments"][0]["amount"])
        self.service.start_phase(request_id="s2", actor_id="con1",
                                 plan_id="plan-1", phase_id="ph1")

    def test_emergency_repair_records_outage_and_pushes_phases(self):
        self._lock(phases=make_phases(with_second=True))
        result = self.service.adjust_plan(
            request_id="adj-em", actor_id="ops1", plan_id="plan-1",
            kind="emergency_repair", expected_version=1, reason="突发支座滑移",
            repair_until="2026-10-25T00:00:00Z",
            phases=[{"phase_id": "ph1", "start": "2026-10-26T00:00:00Z",
                     "end": "2026-11-05T00:00:00Z"},
                    {"phase_id": "ph2", "start": "2026-11-06T00:00:00Z",
                     "end": "2026-11-16T00:00:00Z"}])
        self.assertIsNotNone(result["emergency_outage_id"])
        plan = self.service.get_plan("plan-1")
        emergency = [outage for outage in plan["outages"] if outage["phase_id"] is None]
        self.assertEqual(1, len(emergency))
        self.assertEqual("2026-10-25T00:00:00Z", emergency[0]["ended_at"])
        with self.assertRaises(ValidationError):
            self.service.adjust_plan(
                request_id="adj-em2", actor_id="ops1", plan_id="plan-1",
                kind="emergency_repair", expected_version=2, reason="再次抢修",
                repair_until="2026-11-20T00:00:00Z",
                phases=[{"phase_id": "ph1", "start": "2026-11-18T00:00:00Z",
                         "end": "2026-11-25T00:00:00Z"}])

    def test_partial_complete_checks_fund_budget(self):
        self._lock()
        self.service.start_phase(request_id="s1", actor_id="con1",
                                 plan_id="plan-1", phase_id="ph1")
        with self.assertRaises(ConflictError):
            self.service.adjust_plan(request_id="adj-big", actor_id="con1",
                                     plan_id="plan-1", kind="partial_complete",
                                     expected_version=1, reason="超预算支付",
                                     phase_id="ph1", amount=600000)
        result = self.service.adjust_plan(request_id="adj-ok", actor_id="con1",
                                          plan_id="plan-1", kind="partial_complete",
                                          expected_version=1, reason="首期计量",
                                          phase_id="ph1", amount=80000)
        self.assertIsNotNone(result["payment_id"])
        self.assertEqual(2, self.service.get_plan("plan-1")["version"])

    def test_release_requires_acceptance(self):
        self._lock()
        self.service.start_phase(request_id="s1", actor_id="con1",
                                 plan_id="plan-1", phase_id="ph1")
        self.service.complete_phase(request_id="c1", actor_id="con1",
                                    plan_id="plan-1", phase_id="ph1", amount=200000)
        lease = self.service.get_plan("plan-1")["leases"][0]
        with self.assertRaises(ConflictError) as ctx:
            self.service.release_lease(request_id="rel-1", actor_id="p1",
                                       plan_id="plan-1", lease_id=lease["lease_id"])
        self.assertIn("unmet_preconditions", ctx.exception.extra)
        self.service.accept_phase(request_id="a1", actor_id="ops1",
                                  plan_id="plan-1", phase_id="ph1")
        released = self.service.release_lease(request_id="rel-2", actor_id="p1",
                                              plan_id="plan-1", lease_id=lease["lease_id"])
        self.assertTrue(released["released"])
        with self.assertRaises(ConflictError):
            self.service.release_lease(request_id="rel-3", actor_id="p1",
                                       plan_id="plan-1", lease_id=lease["lease_id"])

    def test_reopen_requires_all_preconditions(self):
        self._lock()
        self.service.start_phase(request_id="s1", actor_id="con1",
                                 plan_id="plan-1", phase_id="ph1")
        with self.assertRaises(ConflictError) as ctx:
            self.service.reopen_plan(request_id="ro-1", actor_id="ops1", plan_id="plan-1")
        unmet = {item["name"] for item in ctx.exception.extra["unmet_preconditions"]}
        self.assertEqual({"all_phases_accepted", "no_open_outages",
                          "all_leases_released"}, unmet)

    def test_dependency_facility_window_blocks_lock(self):
        self._create_plan(plan_id="plan-d", facility="f2",
                          phases=make_phases(crew="c2"))
        self._approve_all("plan-d")
        self._create_plan(plan_id="plan-1", facility="f1", phases=make_phases(crew="c1"))
        result = self._approve_all("plan-1")
        self.assertFalse(result["lock"]["acquired"])
        types = {conflict["conflict_type"] for conflict in result["lock"]["conflicts"]}
        self.assertIn("dependency_blocked", types)

    def test_unqualified_crew_rejected(self):
        phases = make_phases()
        phases[0]["work_type"] = "wt2"
        with self.assertRaises(ValidationError):
            self._create_plan(phases=phases)

    def test_late_material_rejected(self):
        phases = make_phases(start="2026-09-28T00:00:00Z", end="2026-10-08T00:00:00Z")
        with self.assertRaises(ValidationError):
            self._create_plan(phases=phases)

    def test_fund_deadline_rejected(self):
        phases = make_phases(start="2026-12-20T00:00:00Z", end="2027-01-10T00:00:00Z")
        with self.assertRaises(ValidationError):
            self._create_plan(phases=phases)

    def test_overlapping_phases_rejected(self):
        phases = make_phases()
        phases.append({"phase_id": "ph2", "name": "重叠阶段",
                       "start": "2026-10-15T00:00:00Z", "end": "2026-10-25T00:00:00Z",
                       "closure_scope": "封闭", "work_type": "wt1", "crew_id": "c2"})
        with self.assertRaises(ValidationError):
            self._create_plan(phases=phases)

    def test_amount_requires_fund(self):
        phases = make_phases()
        del phases[0]["fund_id"]
        with self.assertRaises(ValidationError):
            self._create_plan(phases=phases)

    def test_create_plan_idempotent_replay(self):
        first = self._create_plan()
        second = self._create_plan()
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        with self.assertRaises(ConflictError):
            self.service.create_plan(request_id="req-plan-1", actor_id="p1",
                                     site_id="s1", plan_id="plan-1", facility_id="f3",
                                     title="不同内容", phases=make_phases())

    def test_calendar_aggregates_windows_leases_and_constraints(self):
        self._lock(phases=make_phases(with_second=True))
        calendar = self.service.calendar("s1")
        kinds = {entry["kind"] for entry in calendar["entries"]}
        self.assertIn("phase_window", kinds)
        self.assertIn("lease", kinds)
        self.assertIn("material_arrival", kinds)
        self.assertIn("fund_deadline", kinds)
        self.assertEqual(3, len(calendar["facilities"]))

    def test_replay_lists_full_lifecycle(self):
        self._lock()
        self.service.start_phase(request_id="s1", actor_id="con1",
                                 plan_id="plan-1", phase_id="ph1")
        self.service.complete_phase(request_id="c1", actor_id="con1",
                                    plan_id="plan-1", phase_id="ph1", amount=200000)
        self.service.accept_phase(request_id="a1", actor_id="ops1",
                                  plan_id="plan-1", phase_id="ph1")
        for lease in self.service.get_plan("plan-1")["leases"]:
            self.service.release_lease(request_id=f"rel-{lease['lease_id']}",
                                       actor_id="p1", plan_id="plan-1",
                                       lease_id=lease["lease_id"])
        self.service.reopen_plan(request_id="ro-1", actor_id="ops1", plan_id="plan-1")
        replay = self.service.replay_plan("plan-1")
        actions = [event["action"] for event in replay["events"]]
        self.assertEqual("renewal_plan.drafted", actions[0])
        self.assertEqual("renewal_plan.reopened", actions[-1])
        self.assertEqual(3, actions.count("renewal_plan.approved"))
        self.assertIn("renewal_plan.locked", actions)
        with self.assertRaises(NotFoundError):
            self.service.replay_plan("missing")

    def test_restart_recovers_open_leases_and_approvals(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "renewal.sqlite3"
            clock = StepClock(BASE_TIME)
            database = Database(path)
            base = DomainService(database, clock)
            service = RenewalService(database, clock)
            bootstrap(base, service)
            service.create_plan(request_id="req-plan-1", actor_id="p1", site_id="s1",
                                plan_id="plan-1", facility_id="f3", title="计划一",
                                phases=make_phases(crew="c2", route_cap=1000,
                                                   start="2026-10-10T00:00:00Z",
                                                   end="2026-10-15T00:00:00Z"))
            service.approve_plan(request_id="ap-1-ops", actor_id="ops1",
                                 plan_id="plan-1", party="operations")
            service.approve_plan(request_id="ap-1-con", actor_id="con1",
                                 plan_id="plan-1", party="construction")
            service.create_plan(request_id="req-plan-2", actor_id="p1", site_id="s1",
                                plan_id="plan-2", facility_id="f3", title="计划二",
                                phases=make_phases(crew="c1", route_cap=2000))
            for party, actor in [("operations", "ops1"), ("construction", "con1"),
                                 ("local_manager", "loc1")]:
                service.approve_plan(request_id=f"ap-2-{party}", actor_id=actor,
                                     plan_id="plan-2", party=party)
            self.assertEqual("locked", service.get_plan("plan-2")["status"])
            database.close()

            database2 = Database(path)
            base2 = DomainService(database2, clock)
            service2 = RenewalService(database2, clock)
            recovered = service2.get_plan("plan-1")
            self.assertEqual(2, len(recovered["approvals"]))
            result = service2.approve_plan(request_id="ap-1-loc", actor_id="loc1",
                                           plan_id="plan-1", party="local_manager")
            self.assertTrue(result["lock"]["acquired"])
            service2.create_plan(request_id="req-plan-3", actor_id="p1", site_id="s1",
                                 plan_id="plan-3", facility_id="f3", title="计划三",
                                 phases=make_phases(crew="c1", route_cap=1000,
                                                    start="2026-10-12T00:00:00Z",
                                                    end="2026-10-18T00:00:00Z"))
            outcome = None
            for party, actor in [("operations", "ops1"), ("construction", "con1"),
                                 ("local_manager", "loc1")]:
                outcome = service2.approve_plan(request_id=f"ap-3-{party}", actor_id=actor,
                                                plan_id="plan-3", party=party)
            self.assertFalse(outcome["lock"]["acquired"])
            valid, _ = base2.verify_audit()
            self.assertTrue(valid)
            database2.close()


if __name__ == "__main__":
    unittest.main()
