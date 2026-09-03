"""
Manual override flow: search eligible replacement candidates for a crew slot,
preview the fairness/coverage/overtime impact of each, then commit a swap.

Candidate ranking is a statistical scoring pass over each run's existing
Assignment history — not a CP-SAT re-solve per candidate, which would be far
too slow to run interactively. Every eligible candidate is scored on how far
their workload sits from the group's mean (in standard deviations, i.e. a
z-score) across total shifts, nights, and weekends, plus a home-station
term; those scores are then run through a softmax so the ranking is backed
by an actual probability distribution ("how much better is this pick than
the alternatives") rather than a plain sort. Every candidate's hard-constraint
eligibility (leave, rest rules, max consecutive days, night restriction,
role/station match) is re-checked at both search time and commit time —
scoring only ever orders candidates who already passed those hard checks.
"""
import math
import statistics
from collections import defaultdict
from datetime import timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from .database import get_session
from .models import (
    RosterRun, Station, Ambulance, Employee, Leave,
    CrewAssignment, CrewMember, Assignment, User,
)
from .scheduler import MAX_CONSECUTIVE_DAYS, is_work_shift_code as _is_shift_code
from .auth import require_role
from .notifications import notify_employee
from .audit import log_activity
from .operational_exceptions import record_exception

router = APIRouter(prefix="/api/crew", tags=["overrides"])

any_role = require_role("admin", "call_center_tl", "drivers_manager", "phtl")


def _check_slot_scope(actor: User, slot: str):
    """Mirrors the same role boundary generation now enforces: Drivers Manager
    only ever touches the driver seat, PHTL only the medical seat, admin
    either. Call Center has no crew seats at all — their staff work the
    dispatch desk, not a CrewAssignment — so they're excluded here entirely,
    same as they're excluded from generating drivers/medical."""
    if actor.role == "admin":
        return
    if actor.role == "drivers_manager" and slot == "driver":
        return
    if actor.role == "phtl" and slot == "member":
        return
    raise HTTPException(403, f"Role '{actor.role}' can't manage the '{slot}' seat on a crew.")


def _run_shift_stats(session: Session, run_id: int):
    """employee_id -> {total, nights, weekends} across the whole run, from Assignment rows."""
    rows = session.exec(select(Assignment).where(Assignment.run_id == run_id)).all()
    stats = defaultdict(lambda: {"total": 0, "nights": 0, "weekends": 0})
    by_employee_day = defaultdict(dict)  # employee_id -> {day: code}
    for a in rows:
        by_employee_day[a.employee_id][a.day] = a.code
        if _is_shift_code(a.code):
            s = stats[a.employee_id]
            s["total"] += 1
            if a.code[0] == "N":
                s["nights"] += 1
            if a.day.weekday() >= 5:
                s["weekends"] += 1
    return stats, by_employee_day


def _eligibility(employee: Employee, day, shift: str, by_employee_day: dict, on_leave_today: set, busy_today: dict):
    if employee.id in on_leave_today:
        return False, "On leave that day."
    existing_code = busy_today.get(employee.id)
    if existing_code and _is_shift_code(existing_code):
        return False, f"Already working a {existing_code} shift that day."
    if shift == "night" and employee.restricted_night_shift:
        return False, "Flagged as no-night-shifts."

    day_history = by_employee_day.get(employee.id, {})
    yesterday = day.__class__.fromordinal(day.toordinal() - 1)
    if day_history.get(yesterday, "").startswith("N"):
        return False, "Worked a night shift the day before — owed a rest day."

    if shift == "night":
        tomorrow = day.__class__.fromordinal(day.toordinal() + 1)
        if _is_shift_code(day_history.get(tomorrow, "")):
            return False, "Already scheduled to work the next day — would break their post-night rest day."

    consec = 0
    cursor = day.__class__.fromordinal(day.toordinal() - 1)
    for _ in range(MAX_CONSECUTIVE_DAYS + 1):
        code = day_history.get(cursor)
        if code and _is_shift_code(code):
            consec += 1
            cursor = cursor.__class__.fromordinal(cursor.toordinal() - 1)
        else:
            break
    if consec >= MAX_CONSECUTIVE_DAYS:
        return False, f"Already worked {consec} days in a row — would exceed the {MAX_CONSECUTIVE_DAYS}-day limit."

    return True, None


def _classify_override(reason: Optional[str], actor_role: str, leave_code: Optional[str] = None):
    """Maps a hard-eligibility rejection reason to (rule_broken, severity) if
    it's a rule class a leader can override with a reason + justification.
    Rest-day and Pro-coverage are overridable for anyone in-lane. Leave is
    normally the one exception nothing can break — admin, PHTL, and Drivers
    Manager each get a narrow, always-safety-severity path for a genuine
    flagged shortage in their own lane (the caller's _check_slot_scope has
    already confirmed they're in-lane for this slot by the time this runs).
    ML and SL are the hard exception to THAT exception: maternity and sick
    leave stay completely untouchable, for every role, no matter what —
    checked against the actual Leave.code for that day, not just the fact
    that someone's on leave at all. Anything else (already double-booked,
    past the max-consecutive-days limit) always returns None — those are
    data-integrity issues, not judgment calls."""
    if not reason:
        return None
    if "rest day" in reason:
        return ("rest_after_night", "safety")
    if "Pro-rated member" in reason:
        return ("pro_coverage", "safety")
    if reason == "On leave that day.":
        if leave_code in ("ML", "SL"):
            return None
        if actor_role in ("admin", "phtl", "drivers_manager"):
            return ("leave_override", "safety")
    return None


def _zscore(value: float, pool: list) -> float:
    """How many standard deviations `value` sits below the pool's mean.
    Positive means under-worked relative to peers (a good thing to pick for);
    negative means already carrying more than their share. Degenerates to 0
    when the pool can't support a spread (empty or single-candidate)."""
    if len(pool) < 2:
        return 0.0
    mean = statistics.mean(pool)
    spread = statistics.pstdev(pool)
    if spread == 0:
        return 0.0
    return (mean - value) / spread


def _score_candidates(scored: list):
    """
    Composite fairness score per eligible candidate, then a softmax over
    those scores so ranking is backed by a real probability distribution
    (each candidate's share of "this is the right pick") rather than a bare
    sort. `scored` is a list of dicts with total/nights/weekends/is_home.
    Returns the same list with `score` and `suitability_pct` filled in.
    """
    if not scored:
        return scored
    totals = [c["current_total"] for c in scored]
    nights = [c["current_nights"] for c in scored]
    weekends = [c["current_weekends"] for c in scored]

    for c in scored:
        total_z = _zscore(c["current_total"], totals)
        nights_z = _zscore(c["current_nights"], nights)
        weekend_z = _zscore(c["current_weekends"], weekends)
        home_term = 1.0 if c["is_home_station"] else 0.0
        # Fairness (overall load) carries the most weight; recent-nights and
        # weekend equity are secondary tie-breakers; home-station is a mild
        # nudge, not a hard preference (the scheduler already lets medical
        # staff float system-wide).
        c["score"] = 0.5 * total_z + 0.25 * nights_z + 0.15 * weekend_z + 0.35 * home_term

    # Softmax over the eligible pool turns raw scores into a probability
    # distribution — "suitability" is literally this candidate's share of
    # total softmax mass, so the numbers across the list always sum to ~100.
    max_score = max(c["score"] for c in scored)
    min_score = min(c["score"] for c in scored)
    exps = [math.exp(c["score"] - max_score) for c in scored]  # shift for numerical stability
    total_exp = sum(exps)
    score_range = max_score - min_score
    for c, e in zip(scored, exps):
        c["suitability_pct"] = round(100 * e / total_exp, 1) if total_exp else round(100 / len(scored), 1)
        # A large eligible pool spreads softmax thin (even the best candidate
        # might only hold ~10% of the total probability mass), which reads as
        # a weak match even when they're clearly the top choice. relative_fit
        # is the same ranking min-max scaled against just this pool's best
        # and worst score, so the UI can show an honest probability number
        # next to a bar that actually looks full for the #1 pick.
        c["relative_fit_pct"] = round(100 * (c["score"] - min_score) / score_range, 1) if score_range else 100.0

    scored.sort(key=lambda c: -c["score"])
    return scored


class CandidateOut(BaseModel):
    employee_id: int
    name: str
    role: str
    is_pro: bool
    eligible: bool
    reason: Optional[str] = None
    overridable: bool = False
    override_rule: Optional[str] = None
    current_total: int
    current_nights: int
    current_weekends: int
    projected_total: int
    is_home_station: bool
    why_recommended: Optional[str] = None
    score: Optional[float] = None
    suitability_pct: Optional[float] = None
    relative_fit_pct: Optional[float] = None


@router.get("/{crew_assignment_id}/candidates")
def get_candidates(crew_assignment_id: int, slot: str, exclude_employee_id: Optional[int] = None,
                    session: Session = Depends(get_session), actor: User = Depends(any_role)):
    """
    slot: "driver" or "member" — which kind of seat we're filling.
    exclude_employee_id: the person currently in that seat, if replacing one (omit when filling an empty seat).
    """
    if slot not in ("driver", "member"):
        raise HTTPException(400, "slot must be 'driver' or 'member'")
    _check_slot_scope(actor, slot)

    crew = session.get(CrewAssignment, crew_assignment_id)
    if not crew:
        raise HTTPException(404, "Crew assignment not found")
    ambulance = session.get(Ambulance, crew.ambulance_id)
    station = session.get(Station, ambulance.station_id) if ambulance else None
    if not station:
        raise HTTPException(400, "This crew's ambulance has no station assigned.")

    if slot == "driver":
        pool = session.exec(select(Employee).where(
            Employee.is_driver == True, Employee.active == True, Employee.facility_id == None
        )).all()
    else:
        # Any active medical staffer is a candidate, not just this station's home-anchored
        # ones — the scheduler itself now lets medical staff float system-wide with a
        # home-station preference, so the override search should offer the same reach.
        # Facility-posted staff (Logistics, Head Office, ...) are a fixed assignment,
        # never part of the ambulance rotation, so they're excluded here too.
        pool = session.exec(select(Employee).where(
            Employee.is_driver == False, Employee.active == True, Employee.facility_id == None
        )).all()

    members = session.exec(select(CrewMember).where(CrewMember.crew_assignment_id == crew_assignment_id)).all()
    current_ids = {m.employee_id for m in members}
    if crew.driver_employee_id:
        current_ids.add(crew.driver_employee_id)

    other_pro_present = any(
        m.employee_id != exclude_employee_id and session.get(Employee, m.employee_id) and session.get(Employee, m.employee_id).is_pro
        for m in members
    )

    leave_rows = session.exec(select(Leave).where(Leave.day == crew.day)).all()
    on_leave_today = {l.employee_id for l in leave_rows}
    leave_code_by_employee = {l.employee_id: l.code for l in leave_rows}

    same_day = session.exec(select(Assignment).where(
        Assignment.run_id == crew.run_id, Assignment.day == crew.day
    )).all()
    busy_today = {a.employee_id: a.code for a in same_day}

    stats, by_employee_day = _run_shift_stats(session, crew.run_id)

    eligible_pool, ineligible = [], []
    for e in pool:
        if e.id in current_ids and e.id != exclude_employee_id:
            continue  # already on this crew in another seat
        eligible, reason = _eligibility(e, crew.day, crew.shift, by_employee_day, on_leave_today, busy_today)
        s = stats.get(e.id, {"total": 0, "nights": 0, "weekends": 0})
        is_home = (slot == "driver") or e.station_id == station.id
        if eligible and slot == "member" and not other_pro_present and not e.is_pro:
            eligible = False
            reason = "Would leave this crew with no Pro-rated member — pick a Pro instead."
        classification = None if eligible else _classify_override(reason, actor.role, leave_code_by_employee.get(e.id))
        row = {
            "employee_id": e.id, "name": e.name, "role": e.role, "is_pro": e.is_pro,
            "eligible": eligible, "reason": reason,
            "overridable": classification is not None, "override_rule": classification[0] if classification else None,
            "current_total": s["total"], "current_nights": s["nights"], "current_weekends": s["weekends"],
            "projected_total": s["total"] + 1, "is_home_station": is_home,
        }
        (eligible_pool if eligible else ineligible).append(row)

    _score_candidates(eligible_pool)  # fills in score + suitability_pct, sorts best-first
    for c in eligible_pool:
        why = f"{c['suitability_pct']}% suitability — {c['current_total']} shift(s) so far this run"
        if len(eligible_pool) > 1:
            others = [o["current_total"] for o in eligible_pool]
            why += f" (pool average {round(statistics.mean(others), 1)})"
        if not c["is_home_station"]:
            why += ". Not based at this station — only offered because no home-station candidate scored higher."
        c["why_recommended"] = why

    out = [CandidateOut(**c) for c in eligible_pool]
    out += [CandidateOut(**c, why_recommended=None, score=None, suitability_pct=None, relative_fit_pct=None) for c in ineligible]
    return out


class ReplaceRequest(BaseModel):
    slot: str            # "driver" | "member"
    old_employee_id: Optional[int] = None
    new_employee_id: int
    override: bool = False               # commit anyway through an overridable hard-eligibility rejection
    reason: Optional[str] = None         # required with override=True — one of EXCEPTION_REASONS
    justification: Optional[str] = None  # required with override=True — free-text


@router.post("/{crew_assignment_id}/replace")
def replace_crew_member(crew_assignment_id: int, payload: ReplaceRequest,
                         session: Session = Depends(get_session), actor: User = Depends(any_role)):
    if payload.slot not in ("driver", "member"):
        raise HTTPException(400, "slot must be 'driver' or 'member'")
    _check_slot_scope(actor, payload.slot)

    crew = session.get(CrewAssignment, crew_assignment_id)
    if not crew:
        raise HTTPException(404, "Crew assignment not found")
    ambulance = session.get(Ambulance, crew.ambulance_id)
    station = session.get(Station, ambulance.station_id) if ambulance else None

    new_employee = session.get(Employee, payload.new_employee_id)
    if not new_employee or not new_employee.active:
        raise HTTPException(400, "New employee not found or inactive.")
    if payload.slot == "driver" and not new_employee.is_driver:
        raise HTTPException(400, "New employee is not a qualified driver.")
    if payload.slot == "member" and new_employee.is_driver:
        raise HTTPException(400, "New employee must be medical staff, not a driver.")
    if payload.override and not (payload.reason and payload.justification):
        raise HTTPException(400, "An override requires both a reason and a written justification.")

    leave_rows = session.exec(select(Leave).where(Leave.day == crew.day)).all()
    on_leave_today = {l.employee_id for l in leave_rows}
    leave_code_by_employee = {l.employee_id: l.code for l in leave_rows}
    same_day = session.exec(select(Assignment).where(
        Assignment.run_id == crew.run_id, Assignment.day == crew.day
    )).all()
    busy_today = {a.employee_id: a.code for a in same_day if a.employee_id != payload.old_employee_id}
    _, by_employee_day = _run_shift_stats(session, crew.run_id)
    eligible, reason = _eligibility(new_employee, crew.day, crew.shift, by_employee_day, on_leave_today, busy_today)

    # override_used tracks whichever rule this commit ends up riding an
    # override through, so a single OperationalException row can be written
    # once the assignment actually succeeds (see below, near log_activity).
    override_used = None
    if not eligible:
        classification = _classify_override(reason, actor.role, leave_code_by_employee.get(new_employee.id))
        if classification and payload.override:
            override_used = classification
        else:
            raise HTTPException(409, f"Cannot assign {new_employee.name}: {reason}")

    code = f"{'D' if crew.shift == 'day' else 'N'}{station.code}"

    if payload.slot == "driver":
        crew.driver_employee_id = new_employee.id
        session.add(crew)
    else:
        members = session.exec(select(CrewMember).where(CrewMember.crew_assignment_id == crew_assignment_id)).all()
        target = next((m for m in members if m.employee_id == payload.old_employee_id), None)
        if not target:
            raise HTTPException(400, "old_employee_id is not currently on this crew.")
        if not new_employee.is_pro:
            still_has_pro = any(m.employee_id != target.employee_id and session.get(Employee, m.employee_id)
                                 and session.get(Employee, m.employee_id).is_pro for m in members)
            if not still_has_pro:
                if payload.override:
                    # Don't let this secondary check clobber a classification
                    # already made above (e.g. leave_override, rest_after_night)
                    # — the real reason this needed an override was whatever
                    # _eligibility() rejected first; a resulting Pro gap is a
                    # consequence of THAT choice, not a second, separate one.
                    override_used = override_used or ("pro_coverage", "safety")
                else:
                    raise HTTPException(409, f"Cannot assign {new_employee.name}: would leave this crew with no Pro-rated member.")
        target.employee_id = new_employee.id
        target.is_leader = False
        session.add(target)

    # keep the flat day-grid Assignment rows in sync
    if payload.old_employee_id:
        old_row = session.exec(select(Assignment).where(
            Assignment.run_id == crew.run_id, Assignment.employee_id == payload.old_employee_id, Assignment.day == crew.day
        )).first()
        if old_row:
            old_row.code = "O"
            old_row.station_id = None
            old_row.manually_edited = True
            session.add(old_row)

    new_row = session.exec(select(Assignment).where(
        Assignment.run_id == crew.run_id, Assignment.employee_id == new_employee.id, Assignment.day == crew.day
    )).first()
    if new_row:
        new_row.code = code
        new_row.station_id = station.id
        new_row.manually_edited = True
        session.add(new_row)
    else:
        session.add(Assignment(run_id=crew.run_id, employee_id=new_employee.id, day=crew.day,
                                code=code, station_id=station.id, manually_edited=True))

    # only affected employees get notified, and only when this changes an already-published roster
    run = session.get(RosterRun, crew.run_id)
    if run and run.status == "published":
        when = crew.day.isoformat()
        if payload.old_employee_id:
            notify_employee(
                session, payload.old_employee_id,
                title="Roster change", body=f"You were removed from the {when} {crew.shift} shift.",
                related_type="crew_replace", related_id=crew.run_id,
            )
        notify_employee(
            session, new_employee.id,
            title="Roster change", body=f"You were assigned to the {when} {crew.shift} shift.",
            related_type="crew_replace", related_id=crew.run_id,
        )

    old_employee = session.get(Employee, payload.old_employee_id) if payload.old_employee_id else None
    override_note = ""
    if override_used:
        rule_broken, severity = override_used
        exception_type = {"rest_after_night": "rest_override", "pro_coverage": "pro_coverage_override",
                           "leave_override": "leave_override"}[rule_broken]
        override_note = f" [OVERRIDE — {rule_broken.replace('_',' ')}, {payload.reason}]"
        record_exception(
            session, actor,
            exception_type=exception_type,
            rule_broken=rule_broken, severity=severity, reason=payload.reason, justification=payload.justification,
            impact_summary=f"{crew.day.isoformat()} {crew.shift} shift, ambulance "
                            f"{ambulance.plate_no if ambulance else crew.ambulance_id}: assigned {new_employee.name} "
                            f"despite {rule_broken.replace('_',' ')} — {(old_employee.name if old_employee else 'empty slot')} → {new_employee.name}.",
            run_id=crew.run_id, day=crew.day, shift=crew.shift,
            station_id=station.id if station else None, ambulance_id=crew.ambulance_id,
            employee_id=new_employee.id, crew_assignment_id=crew.id,
        )
    log_activity(
        session, actor, "crew.replace", "crew_assignment", crew.id,
        f"{crew.day.isoformat()} {crew.shift} shift, {payload.slot}: "
        f"{(old_employee.name if old_employee else 'empty slot')} → {new_employee.name}.{override_note}"
    )
    session.commit()
    return {"ok": True, "override_used": {"rule_broken": override_used[0], "severity": override_used[1]} if override_used else None}


class ApproveShortageRequest(BaseModel):
    rule_broken: str      # "pro_coverage" | "understaffed" | "no_driver"
    reason: str
    justification: str


@router.post("/{crew_assignment_id}/approve-shortage")
def approve_shortage(crew_assignment_id: int, payload: ApproveShortageRequest,
                      session: Session = Depends(get_session), actor: User = Depends(any_role)):
    """The other half of "pull someone" — sometimes there's nobody to pull at
    all, and the honest move is accepting the gap rather than moving anyone.
    No CrewMember/Assignment row changes here; this only records that a
    leader looked at the shortfall and signed off on it. Lane-gated the same
    way everything else here is: PHTL/admin for the two medical rules,
    Drivers Manager/admin for a missing driver."""
    if payload.rule_broken not in ("pro_coverage", "understaffed", "no_driver"):
        raise HTTPException(400, "rule_broken must be 'pro_coverage', 'understaffed', or 'no_driver'")
    if payload.rule_broken == "no_driver" and actor.role not in ("drivers_manager", "admin"):
        raise HTTPException(403, "Only Drivers Manager or admin can approve a crew running with no driver.")
    if payload.rule_broken in ("pro_coverage", "understaffed") and actor.role not in ("phtl", "admin"):
        raise HTTPException(403, "Only PHTL or admin can approve a medical crew running short.")

    crew = session.get(CrewAssignment, crew_assignment_id)
    if not crew:
        raise HTTPException(404, "Crew assignment not found")
    ambulance = session.get(Ambulance, crew.ambulance_id)
    station = session.get(Station, ambulance.station_id) if ambulance else None

    if payload.rule_broken == "no_driver":
        if crew.driver_employee_id is not None:
            raise HTTPException(400, "This crew already has a driver assigned — nothing to approve.")
        severity = "safety"
        impact_summary = (
            f"{crew.day.isoformat()} {crew.shift} shift, ambulance {ambulance.plate_no if ambulance else crew.ambulance_id}: "
            f"approved to run with no driver assigned."
        )
    else:
        members = session.exec(select(CrewMember).where(CrewMember.crew_assignment_id == crew_assignment_id)).all()
        member_employees = [session.get(Employee, m.employee_id) for m in members]
        member_employees = [e for e in member_employees if e]
        has_pro = any(e.is_pro for e in member_employees)
        expected_size = (station.night_crew_size if crew.shift == "night" else station.day_crew_size) if station else None

        if payload.rule_broken == "pro_coverage" and has_pro:
            raise HTTPException(400, "This crew currently has a Pro-rated member — nothing to approve.")
        if payload.rule_broken == "understaffed":
            if expected_size is None:
                raise HTTPException(400, "This ambulance has no station on file to compare crew size against.")
            if len(member_employees) >= expected_size:
                raise HTTPException(400, f"This crew already meets the expected size ({expected_size}) — nothing to approve.")

        severity = "safety" if (payload.rule_broken == "pro_coverage" or not member_employees) else "operational"
        size_note = f"{len(member_employees)}/{expected_size}" if expected_size is not None else str(len(member_employees))
        impact_summary = (
            f"{crew.day.isoformat()} {crew.shift} shift, ambulance {ambulance.plate_no if ambulance else crew.ambulance_id}: "
            + ("approved to run with no Pro-rated crew member." if payload.rule_broken == "pro_coverage"
               else f"approved to run understaffed ({size_note} medical crew).")
        )

    record_exception(
        session, actor, exception_type="approved_shortage", rule_broken=payload.rule_broken, severity=severity,
        reason=payload.reason, justification=payload.justification, impact_summary=impact_summary,
        run_id=crew.run_id, day=crew.day, shift=crew.shift,
        station_id=station.id if station else None, ambulance_id=crew.ambulance_id,
        crew_assignment_id=crew.id,
    )
    log_activity(session, actor, "crew.approve_shortage", "crew_assignment", crew.id,
                 f"{crew.day.isoformat()} {crew.shift} shift, ambulance "
                 f"{ambulance.plate_no if ambulance else crew.ambulance_id}: approved to run short "
                 f"({payload.rule_broken.replace('_',' ')}). Reason: {payload.reason}.")
    session.commit()
    return {"ok": True, "severity": severity}
