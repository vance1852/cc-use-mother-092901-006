import unittest
from datetime import datetime, timedelta, timezone

from transport_coordination.clock import FixedClock
from transport_coordination.errors import (
    ConflictError,
    ExpiredError,
    NotFoundError,
    PermissionDenied,
    PreconditionFailed,
    ValidationError,
)
from transport_coordination.scheduling import SchedulingService
from transport_coordination.service import DomainService
from transport_coordination.storage import Database


class MovableClock(FixedClock):
    def advance(self, minutes):
        self._value += timedelta(minutes=minutes)


def make_phase(code, day, *, start="08:00", end="18:00", cost=200, volume=40,
               corridor="detour", crew="crew-a", qual="bridge", materials=("steel",),
               scope="full", kind="renewal"):
    return {
        "phase_code": code, "title": code,
        "planned_start": f"2026-10-{day:02d}T{start}:00Z",
        "planned_end": f"2026-10-{day:02d}T{end}:00Z",
        "closure_scope": scope, "diverted_volume": volume, "corridor_id": corridor,
        "crew_id": crew, "qualification": qual, "material_ids": list(materials),
        "cost": cost, "kind": kind,
    }


class SchedulingTestBase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MovableClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        self.domain = DomainService(self.database, self.clock)
        self.svc = SchedulingService(self.database, self.clock)
        d, s = self.domain, self.svc
        d.register_organization(request_id="org-1", actor_id="bootstrap",
                                organization_id="o1", name="建管中心")
        d.register_actor(request_id="adm-1", actor_id="bootstrap", new_actor_id="adm",
                         display_name="管理员", role="admin", organization_id="o1")
        for rid, aid, name, role in [
            ("op-1", "op", "运营方", "operator"),
            ("co-1", "co", "施工方", "construction"),
            ("lo-1", "lo", "属地", "locality"),
            ("rv-1", "rv", "验收员", "reviewer"),
        ]:
            d.register_actor(request_id=rid, actor_id="adm", new_actor_id=aid,
                             display_name=name, role=role, organization_id="o1")
        d.register_site(request_id="site-1", actor_id="adm", site_id="site-1",
                        organization_id="o1", name="一号节点", timezone_name="Asia/Shanghai")
        s.register_facility(request_id="fac-1", actor_id="adm", facility_id="brg",
                            site_id="site-1", name="老旧桥梁", facility_type="bridge")
        s.register_facility(request_id="fac-2", actor_id="adm", facility_id="tun",
                            site_id="site-1", name="老旧隧道", facility_type="tunnel",
                            depends_on=["brg"])
        s.register_corridor(request_id="cor-1", actor_id="adm", corridor_id="detour",
                            name="唯一替代通道", spare_capacity=100)
        s.register_crew(request_id="crew-1", actor_id="adm", crew_id="crew-a", name="甲专业队",
                        qualifications=["bridge", "tunnel"])
        s.register_crew(request_id="crew-2", actor_id="adm", crew_id="crew-b", name="乙专业队",
                        qualifications=["bridge"])
        s.register_material(request_id="mat-1", actor_id="adm", material_id="steel",
                            name="钢构件", arrived_at="2026-09-20T00:00:00Z")
        s.register_fund(request_id="fund-1", actor_id="adm", fund_id="fund-1", name="专项资金",
                        amount=1000, valid_until="2026-12-31T00:00:00Z")

    def tearDown(self):
        self.database.close()

    def declare_window(self, request_id="win-1", *, facility="brg", fund="fund-1", title="桥梁更新"):
        return self.svc.create_window(request_id=request_id, actor_id="op", facility_id=facility,
                                      fund_id=fund, title=title)["window_id"]

    def submit(self, window_id, request_id, phases, *, ttl=600, base=None, reason="initial"):
        return self.svc.submit_proposal(request_id=request_id, actor_id="op", window_id=window_id,
                                        phases=phases, ttl_minutes=ttl, base_revision=base,
                                        reason=reason)

    def sign_all(self, proposal_id, prefix):
        self.svc.sign_proposal(request_id=f"{prefix}-1", actor_id="op",
                               proposal_id=proposal_id, party="operations")
        self.svc.sign_proposal(request_id=f"{prefix}-2", actor_id="co",
                               proposal_id=proposal_id, party="construction")
        self.svc.sign_proposal(request_id=f"{prefix}-3", actor_id="lo",
                               proposal_id=proposal_id, party="locality")

    def lock(self, proposal_id, prefix, actor="op"):
        self.sign_all(proposal_id, prefix)
        return self.svc.confirm_lock(request_id=f"{prefix}-lock", actor_id=actor,
                                     proposal_id=proposal_id)


class ResourceValidationTest(SchedulingTestBase):
    def test_crew_without_qualification_blocks_plan(self):
        wid = self.declare_window()
        bad = make_phase("p1", 5, crew="crew-b", qual="tunnel")
        with self.assertRaises(ValidationError):
            self.submit(wid, "pp-1", [bad])

    def test_material_not_arrived_blocks_plan(self):
        self.svc.register_material(request_id="mat-2", actor_id="adm", material_id="late",
                                   name="晚到材料", arrived_at="2026-10-10T00:00:00Z")
        wid = self.declare_window()
        bad = make_phase("p1", 5, materials=("late",))
        with self.assertRaises(ValidationError):
            self.submit(wid, "pp-1", [bad])

    def test_missing_reference_resources_404(self):
        wid = self.declare_window()
        with self.assertRaises(NotFoundError):
            self.submit(wid, "pp-1", [make_phase("p1", 5, crew="nope")])

    def test_diverted_volume_requires_corridor(self):
        wid = self.declare_window()
        with self.assertRaises(ValidationError):
            self.submit(wid, "pp-1", [make_phase("p1", 5, corridor=None)])

    def test_facility_self_dependency_rejected(self):
        with self.assertRaises(ValidationError):
            self.svc.register_facility(request_id="fac-3", actor_id="adm", facility_id="self",
                                       site_id="site-1", name="自依赖", facility_type="x",
                                       depends_on=["self"])


class ConflictDetectionTest(SchedulingTestBase):
    def test_two_windows_same_corridor_over_capacity(self):
        w1 = self.declare_window("win-1")
        p1 = self.submit(w1, "pp-1", [make_phase("a1", 5, volume=60)])
        self.assertEqual([], p1["conflicts"])
        self.lock(p1["proposal_id"], "s1")

        w2 = self.declare_window("win-2", facility="tun", title="隧道机电")
        p2 = self.submit(w2, "pp-2", [make_phase("b1", 5, volume=50, qual="tunnel")])
        kinds = {c["type"] for c in p2["conflicts"]}
        self.assertIn("corridor_capacity", kinds)
        capacity = next(c for c in p2["conflicts"] if c["type"] == "corridor_capacity")
        self.assertGreater(capacity["detail"]["peak_diverted"], 100)
        self.assertTrue(any(t["type"] == "shift_phase" for t in p2["tradeoffs"]))

    def test_capacity_within_limit_is_allowed(self):
        w1 = self.declare_window("win-1")
        p1 = self.submit(w1, "pp-1", [make_phase("a1", 5, volume=40)])
        self.lock(p1["proposal_id"], "s1")
        w2 = self.declare_window("win-2", facility="tun", title="隧道机电")
        p2 = self.submit(w2, "pp-2", [make_phase("b1", 5, volume=40, crew="crew-b",
                                                 qual="bridge")])
        self.assertNotIn("corridor_capacity", {c["type"] for c in p2["conflicts"]})

    def test_same_crew_double_booked(self):
        w1 = self.declare_window("win-1")
        p1 = self.submit(w1, "pp-1", [
            make_phase("a1", 5, volume=0, corridor=None),
            make_phase("a2", 5, volume=0, corridor=None, start="10:00", end="12:00"),
        ])
        self.assertIn("crew_double_booked", {c["type"] for c in p1["conflicts"]})

    def test_crew_occupied_by_other_window(self):
        w1 = self.declare_window("win-1")
        p1 = self.submit(w1, "pp-1", [make_phase("a1", 5, volume=0, corridor=None)])
        self.lock(p1["proposal_id"], "s1")
        w2 = self.declare_window("win-2")
        p2 = self.submit(w2, "pp-2", [make_phase("b1", 5, volume=0, corridor=None)])
        self.assertIn("crew_occupied", {c["type"] for c in p2["conflicts"]})

    def test_dependent_facility_occupied(self):
        # 隧道依赖桥梁；桥梁封闭更新时，隧道窗口再封闭桥梁依赖集即冲突
        w1 = self.declare_window("win-1", facility="brg", title="桥梁")
        p1 = self.submit(w1, "pp-1", [make_phase("a1", 5, volume=0, corridor=None)])
        self.lock(p1["proposal_id"], "s1")
        w2 = self.declare_window("win-2", facility="tun", title="隧道")
        p2 = self.submit(w2, "pp-2", [make_phase("b1", 5, volume=0, corridor=None,
                                                 qual="tunnel")])
        self.assertIn("facility_occupied", {c["type"] for c in p2["conflicts"]})

    def test_fund_deadline_conflict(self):
        w1 = self.declare_window()
        p1 = self.submit(w1, "pp-1", [make_phase("a1", 5, start="08:00", end="18:00")])
        bad = make_phase("late", 5)
        bad["planned_end"] = "2027-01-05T18:00:00Z"
        bad["planned_start"] = "2027-01-05T08:00:00Z"
        p2 = self.submit(w1, "pp-2", [bad])
        self.assertIn("fund_deadline", {c["type"] for c in p2["conflicts"]})

    def test_fund_budget_conflict(self):
        self.svc.register_fund(request_id="fund-2", actor_id="adm", fund_id="poor",
                               name="小额资金", amount=100, valid_until="2026-12-31T00:00:00Z")
        wid = self.svc.create_window(request_id="win-poor", actor_id="op", facility_id="brg",
                                    fund_id="poor", title="资金不足")["window_id"]
        p = self.svc.submit_proposal(request_id="pp-poor", actor_id="op", window_id=wid,
                                     phases=[make_phase("a1", 5, cost=300)], ttl_minutes=600)
        self.assertIn("fund_budget", {c["type"] for c in p["conflicts"]})


class SigningAndLockTest(SchedulingTestBase):
    def test_draft_expires_after_ttl(self):
        wid = self.declare_window()
        p = self.submit(wid, "pp-1", [make_phase("a1", 5)], ttl=60)
        self.clock.advance(61)
        with self.assertRaises(ExpiredError):
            self.svc.sign_proposal(request_id="sg-1", actor_id="op", proposal_id=p["proposal_id"],
                                   party="operations")

    def test_lock_requires_all_three_parties(self):
        wid = self.declare_window()
        p = self.submit(wid, "pp-1", [make_phase("a1", 5)])
        self.svc.sign_proposal(request_id="sg-1", actor_id="op", proposal_id=p["proposal_id"],
                               party="operations")
        with self.assertRaises(PreconditionFailed):
            self.svc.confirm_lock(request_id="lk-1", actor_id="op", proposal_id=p["proposal_id"])

    def test_wrong_party_role_cannot_sign(self):
        wid = self.declare_window()
        p = self.submit(wid, "pp-1", [make_phase("a1", 5)])
        with self.assertRaises(PermissionDenied):
            self.svc.sign_proposal(request_id="sg-1", actor_id="co", proposal_id=p["proposal_id"],
                                   party="operations")

    def test_lock_blocked_while_conflict_exists(self):
        w1 = self.declare_window("win-1")
        p1 = self.submit(w1, "pp-1", [make_phase("a1", 5, volume=60)])
        self.lock(p1["proposal_id"], "s1")
        w2 = self.declare_window("win-2", facility="tun", title="隧道")
        p2 = self.submit(w2, "pp-2", [make_phase("b1", 5, volume=60, qual="tunnel")])
        with self.assertRaises(ConflictError):
            self.lock(p2["proposal_id"], "s2")

    def test_leases_created_on_lock(self):
        wid = self.declare_window()
        p = self.submit(wid, "pp-1", [make_phase("a1", 5, cost=300)])
        self.lock(p["proposal_id"], "s1")
        cal = self.svc.calendar()
        active = [l for l in cal["leases"] if l["window_id"] == wid]
        types = {l["resource_type"] for l in active}
        self.assertEqual({"crew", "facility", "material", "corridor", "fund"}, types)


class ConcurrencyTest(SchedulingTestBase):
    def test_stale_base_revision_rejected(self):
        wid = self.declare_window()
        first = self.submit(wid, "pp-1", [make_phase("a1", 5)])
        self.assertEqual(1, first["revision"])
        second = self.submit(wid, "pp-2", [make_phase("a1", 6)])
        self.assertEqual(2, second["revision"])
        with self.assertRaises(ConflictError):
            self.submit(wid, "pp-3", [make_phase("a1", 7)], base=1)

    def test_new_proposal_supersedes_old_and_blocks_signing(self):
        wid = self.declare_window()
        old = self.submit(wid, "pp-1", [make_phase("a1", 5)])
        new = self.submit(wid, "pp-2", [make_phase("a1", 6)])
        with self.assertRaises(ConflictError):
            self.svc.sign_proposal(request_id="sg-old", actor_id="op", proposal_id=old["proposal_id"],
                                   party="operations")
        self.lock(new["proposal_id"], "s1")
        self.assertEqual("locked", self.svc.get_window(wid)["status"])


class ExecutionAndRescheduleTest(SchedulingTestBase):
    def _locked_window(self, phases, request_prefix="p", window_request="win-1"):
        wid = self.declare_window(window_request)
        p = self.submit(wid, f"{request_prefix}-1", phases)
        self.lock(p["proposal_id"], f"{request_prefix}-s")
        return wid, p["proposal_id"]

    def test_phase_order_precondition(self):
        wid, _ = self._locked_window([make_phase("a1", 5), make_phase("a2", 6)])
        with self.assertRaises(PreconditionFailed):
            self.svc.start_phase(request_id="st-2", actor_id="co", window_id=wid, phase_code="a2")

    def test_delay_cascades_to_later_open_phases_only(self):
        phases = [make_phase("a1", 5), make_phase("a2", 6), make_phase("a3", 7)]
        wid, _ = self._locked_window(phases)
        self.svc.start_phase(request_id="st-1", actor_id="co", window_id=wid, phase_code="a1")
        self.svc.complete_phase(request_id="cp-1", actor_id="co", window_id=wid, phase_code="a1")
        r = self.svc.delay_phase(request_id="dl-1", actor_id="co", window_id=wid,
                                 phase_code="a2", new_start="2026-10-10T08:00:00Z")
        note = r["tradeoffs"][0]
        self.assertEqual("cascade_delay", note["type"])
        self.assertNotIn("a1", note["phases"])
        self.assertIn("a3", note["phases"])
        plan = {ph["phase_code"]: ph for ph in self._plan_of(r["proposal_id"])}
        self.assertEqual("2026-10-11T18:00:00Z", plan["a3"]["planned_end"])

    def _plan_of(self, proposal_id):
        rows = self.database.connection.execute(
            "SELECT phase_code,planned_start,planned_end FROM phase_plans WHERE proposal_id=? ORDER BY ordinal",
            (proposal_id,)).fetchall()
        return [{"phase_code": r["phase_code"], "planned_start": r["planned_start"],
                 "planned_end": r["planned_end"]} for r in rows]

    def test_completed_phase_cannot_be_rescheduled(self):
        wid, _ = self._locked_window([make_phase("a1", 5), make_phase("a2", 6)])
        self.svc.start_phase(request_id="st-1", actor_id="co", window_id=wid, phase_code="a1")
        self.svc.complete_phase(request_id="cp-1", actor_id="co", window_id=wid, phase_code="a1")
        with self.assertRaises(ConflictError):
            self.svc.delay_phase(request_id="dl-1", actor_id="co", window_id=wid,
                                 phase_code="a1", new_start="2026-10-20T08:00:00Z")

    def test_partial_completion_records_payment_and_releases_leases(self):
        wid, _ = self._locked_window([make_phase("a1", 5, cost=300)])
        self.svc.start_phase(request_id="st-1", actor_id="co", window_id=wid, phase_code="a1")
        r = self.svc.complete_phase(request_id="cp-1", actor_id="co", window_id=wid,
                                    phase_code="a1", partial=True, amount=100)
        self.assertEqual("partially_completed", r["status"])
        self.assertGreater(r["released_leases"], 0)
        with self.assertRaises(ConflictError):
            self.svc.complete_phase(request_id="cp-2", actor_id="co", window_id=wid,
                                    phase_code="a1", partial=True, amount=999)

    def test_rejection_keeps_payment_and_outage_history(self):
        wid, _ = self._locked_window([make_phase("a1", 5, cost=300)])
        self.svc.start_phase(request_id="st-1", actor_id="co", window_id=wid, phase_code="a1")
        self.svc.complete_phase(request_id="cp-1", actor_id="co", window_id=wid, phase_code="a1")
        self.svc.reject_phase(request_id="rj-1", actor_id="rv", window_id=wid, phase_code="a1",
                              note="验收不合格")
        timeline = self.svc.timeline(wid)
        self.assertEqual(1, len(timeline["outage_records"]))
        self.assertEqual(300, timeline["payment_records"][0]["amount"])
        # 退回后重排、复工、再完工：原支付保留，只补差额（此处为 0），停运多一条
        r = self.svc.reschedule_phase(request_id="rs-1", actor_id="co", window_id=wid,
                                      phase_code="a1",
                                      changes={"planned_start": "2026-10-09T08:00:00Z",
                                               "planned_end": "2026-10-09T18:00:00Z"})
        self.lock(r["proposal_id"], "rs-s")
        self.svc.start_phase(request_id="st-2", actor_id="co", window_id=wid, phase_code="a1")
        self.svc.complete_phase(request_id="cp-2", actor_id="co", window_id=wid, phase_code="a1")
        timeline = self.svc.timeline(wid)
        self.assertEqual(2, len(timeline["outage_records"]))
        self.assertEqual(1, len(timeline["payment_records"]))

    def test_emergency_repair_inserts_phase_and_reschedules_conflicts(self):
        wid, _ = self._locked_window([make_phase("a1", 10)])
        em = self.svc.emergency_repair(
            request_id="em-1", actor_id="op", window_id=wid,
            phase=make_phase("emg", 9, cost=50), ttl_minutes=360)
        self.assertEqual([], em["conflicts"])
        self.assertEqual("emergency_only", em["tradeoffs"][0]["type"])
        self.lock(em["proposal_id"], "em-s")
        codes = [p["phase_code"] for p in self.svc.get_window(wid)["phases"]]
        self.assertEqual(["emg", "a1"], codes)

    def test_emergency_draft_can_be_corrected_before_signing(self):
        wid, _ = self._locked_window([make_phase("a1", 10)])
        bad = self.svc.emergency_repair(
            request_id="em-1", actor_id="op", window_id=wid,
            phase=make_phase("emg", 10, cost=50), ttl_minutes=360)
        self.assertTrue(bad["conflicts"])
        good = self.svc.emergency_repair(
            request_id="em-2", actor_id="op", window_id=wid,
            phase=make_phase("emg", 9, cost=50), ttl_minutes=360)
        self.assertEqual([], good["conflicts"])


class LeaseAndReopenTest(SchedulingTestBase):
    def test_expired_lease_blocks_start_and_renew_restores(self):
        wid = self.declare_window()
        p = self.submit(wid, "pp-1", [make_phase("a1", 5)])
        self.lock(p["proposal_id"], "s1")
        self.clock.advance(60 * 24 * 5)
        with self.assertRaises(ExpiredError):
            self.svc.start_phase(request_id="st-1", actor_id="co", window_id=wid, phase_code="a1")
        self.svc.renew_leases(request_id="rn-1", actor_id="co", window_id=wid,
                              until="2026-12-30T00:00:00Z")
        self.svc.start_phase(request_id="st-2", actor_id="co", window_id=wid, phase_code="a1")

    def test_renew_capped_by_fund_deadline(self):
        wid = self.declare_window()
        p = self.submit(wid, "pp-1", [make_phase("a1", 5)])
        self.lock(p["proposal_id"], "s1")
        r = self.svc.renew_leases(request_id="rn-1", actor_id="co", window_id=wid,
                                  until="2028-01-01T00:00:00Z")
        self.assertEqual("2026-12-31T00:00:00Z", r["valid_until"])

    def test_reopen_requires_all_completed_and_no_pending_draft(self):
        wid = self.declare_window()
        p = self.submit(wid, "pp-1", [make_phase("a1", 5)])
        self.lock(p["proposal_id"], "s1")
        with self.assertRaises(PreconditionFailed):
            self.svc.reopen(request_id="rp-1", actor_id="op", window_id=wid)

    def test_full_reopen_releases_remaining_leases(self):
        wid = self.declare_window()
        p = self.submit(wid, "pp-1", [make_phase("a1", 5)])
        self.lock(p["proposal_id"], "s1")
        self.svc.start_phase(request_id="st-1", actor_id="co", window_id=wid, phase_code="a1")
        self.svc.complete_phase(request_id="cp-1", actor_id="co", window_id=wid, phase_code="a1")
        r = self.svc.reopen(request_id="rp-1", actor_id="op", window_id=wid)
        self.assertEqual("reopened", r["status"])
        # 资金租约在复开时释放
        self.assertGreaterEqual(r["released_leases"], 1)
        active = [l for l in self.svc.calendar()["leases"] if l["window_id"] == wid]
        self.assertEqual([], active)


class RecoveryTest(SchedulingTestBase):
    def test_recover_reports_pending_signatures_and_expired_leases(self):
        wid = self.declare_window()
        p = self.submit(wid, "pp-1", [make_phase("a1", 20)], ttl=600)
        self.svc.sign_proposal(request_id="sg-1", actor_id="op", proposal_id=p["proposal_id"],
                               party="operations")
        revived = SchedulingService(self.database, self.clock)
        report = revived.recover()
        entry = next(w for w in report["active_windows"] if w["window_id"] == wid)
        draft = entry["proposals"][0]
        self.assertEqual(["operations"], draft["signatures"])
        self.assertEqual(["construction", "locality"], draft["missing_parties"])

        # 锁定后推进时钟，新实例恢复时标记到期租约
        self.svc.sign_proposal(request_id="sg-2", actor_id="co", proposal_id=p["proposal_id"],
                               party="construction")
        self.svc.sign_proposal(request_id="sg-3", actor_id="lo", proposal_id=p["proposal_id"],
                               party="locality")
        self.svc.confirm_lock(request_id="lk-1", actor_id="op", proposal_id=p["proposal_id"])
        self.clock.advance(60 * 24 * 30)
        report = revived.recover()
        entry = next(w for w in report["active_windows"] if w["window_id"] == wid)
        self.assertTrue(entry["leases_expired"])

    def test_recover_expires_overdue_drafts_after_restart(self):
        wid = self.declare_window()
        self.submit(wid, "pp-1", [make_phase("a1", 5)], ttl=60)
        self.clock.advance(61)
        report = SchedulingService(self.database, self.clock).recover()
        entry = next(w for w in report["active_windows"] if w["window_id"] == wid)
        self.assertEqual("expired", entry["proposals"][0]["status"])
        self.assertEqual(1, report["expired_drafts"])


class QueryAndIdempotencyTest(SchedulingTestBase):
    def test_timeline_explains_conflicts_and_tradeoffs(self):
        w1 = self.declare_window("win-1")
        p1 = self.submit(w1, "pp-1", [make_phase("a1", 5, volume=60)])
        self.lock(p1["proposal_id"], "s1")
        w2 = self.declare_window("win-2", facility="tun", title="隧道")
        self.submit(w2, "pp-2", [make_phase("b1", 5, volume=60, qual="tunnel")])
        events = self.svc.timeline(w2)["events"]
        created = next(e for e in events if e["event_type"] == "proposal.created")
        self.assertTrue(created["detail"]["conflicts"])
        self.assertTrue(created["detail"]["tradeoffs"])

    def test_idempotent_replay_returns_same_proposal(self):
        wid = self.declare_window()
        first = self.submit(wid, "pp-1", [make_phase("a1", 5)])
        second = self.submit(wid, "pp-1", [make_phase("a1", 5)])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["proposal_id"], second["proposal_id"])


if __name__ == "__main__":
    unittest.main()
