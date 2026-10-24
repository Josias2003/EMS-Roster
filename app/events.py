"""
Events: Administrator creates them; Drivers Manager and PHTL are the only
roles that participate in event planning (Call Center's roster is never
touched — it isn't part of the employee pools this module draws from at all).

Replan fills an event's required ambulances/drivers/medical crew using a
strict priority order, same philosophy as the manual-override candidate
search in overrides.py (reused here):
  Tier 1 — staff who are off that day for ordinary rotation reasons (least disruptive)
  Tier 2 — staff already working a regular shift that day, reassigned to the event
           (this leaves a gap at their original post — reported, not silently dropped)
  Tier 3 — staff whose day off is a mandated post-night-shift rest day (last resort)
Approved leave (AL/SL/ML/PL/TR/AD/P) is never touched, at any tier.

Within a tier, anyone on standby/regulation duty that day sorts first —
they're the natural pick for the event's crew leader, since they're
already held ready and not pulled off an active post. Each ambulance's
first-assigned medical crew member becomes that crew's leader; other
staff can still be added alongside them from any tier.
"""
from collections import defaultdict
from datetime import date as date_cls
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from .database import get_session
from .models import (
    Event, EventAssignment, EventCrewMember, EVENT_TYPES,
    Ambulance, Employee, Leave, Assignment, CrewAssignment, CrewMember, RosterRun, StandbyAssignment, User, Station,
    EXCEPTION_REASONS, OperationalException,
)
from .overrides import _run_shift_stats, _is_shift_code
from .auth import require_role, get_current_user
from .audit import log_activity
from .notifications import notify_employee
from .operational_exceptions import record_exception

router = APIRouter(prefix="/api/events", tags=["events"])

participant_role = require_role("admin", "drivers_manager", "phtl")


class EventCreate(BaseModel):
    name: str
    type: str
    date: date_cls
    time: Optional[str] = None
    location: str
    priority: int = 1
    ambulances_required: int
    medical_crew_required: int
    drivers_required: int


@router.post("")
def create_event(payload: EventCreate, session: Session = Depends(get_session), user=Depends(require_role("admin"))):
    if payload.type not in EVENT_TYPES:
        raise HTTPException(400, f"type must be one of {EVENT_TYPES}")
    event = Event(**payload.dict(), created_by=user.id)
    session.add(event)
    session.commit()
    session.refresh(event)
    log_activity(session, user, "event.create", "event", event.id,
                 f"Created event '{event.name}' on {event.date.isoformat()} (priority {event.priority}).")
    session.commit()
    session.refresh(event)  # the second commit expires it; without this the response body is empty
    return event


class EventUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    date: Optional[date_cls] = None
    time: Optional[str] = None
    location: Optional[str] = None
    priority: Optional[int] = None
    ambulances_required: Optional[int] = None
    medical_crew_required: Optional[int] = None
    drivers_required: Optional[int] = None


@router.patch("/{event_id}")
def update_event(event_id: int, payload: EventUpdate, session: Session = Depends(get_session),
                  actor: User = Depends(require_role("admin"))):
    """Partial edit — only fields present in the request body are touched, so
    this is also how a start time that was unknown at creation gets filled in
    later: create the event with time=null, then PATCH just {"time": "18:30"}
    once it's confirmed. Note this doesn't retroactively fix a plan that was
    already committed with replan_event before the time was known — Tier-1
    picks assigned under a missing time default to a day shift (see
    _event_is_night_fallback); re-run Plan coverage after the time is set if
    that assumption needs to change."""
    event = session.get(Event, event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    updates = payload.dict(exclude_unset=True)
    if "type" in updates and updates["type"] not in EVENT_TYPES:
        raise HTTPException(400, f"type must be one of {EVENT_TYPES}")
    changed = [f for f, v in updates.items() if getattr(event, f) != v]
    for field, value in updates.items():
        setattr(event, field, value)
    if changed:
        session.add(event)
        session.commit()
        session.refresh(event)
        log_activity(session, actor, "event.update", "event", event.id,
                     f"Updated event '{event.name}': {', '.join(changed)}.")
        session.commit()
    return event


@router.delete("/{event_id}")
def delete_event(event_id: int, session: Session = Depends(get_session), actor: User = Depends(require_role("admin"))):
    event = session.get(Event, event_id)
    if not event:
        raise HTTPException(404, "Event not found")

    # Delete children before the parent — no ORM relationship() here, just
    # plain FK columns, so SQLAlchemy won't infer the dependency order itself.
    assignment_ids = session.exec(select(EventAssignment.id).where(EventAssignment.event_id == event_id)).all()
    if assignment_ids:
        for m in session.exec(select(EventCrewMember).where(EventCrewMember.event_assignment_id.in_(assignment_ids))).all():
            session.delete(m)
        session.flush()
        for a in session.exec(select(EventAssignment).where(EventAssignment.id.in_(assignment_ids))).all():
            session.delete(a)

    # OperationalException.event_id is a real FK — keep the exception itself
    # (it's the audit trail of a real command decision, e.g. someone pulled
    # off their regular crew) and just detach it from the event being removed.
    for exc in session.exec(select(OperationalException).where(OperationalException.event_id == event_id)).all():
        exc.event_id = None
        session.add(exc)

    log_activity(session, actor, "event.delete", "event", event.id,
                 f"Deleted event '{event.name}' ({event.date.isoformat()}).")
    session.delete(event)
    session.commit()
    return {"ok": True}


@router.get("")
def list_events(session: Session = Depends(get_session), _=Depends(participant_role)):
    events = session.exec(select(Event)).all()
    # created_by is a user id, but /users is admin-only and Drivers Manager /
    # PHTL both need to see who's in charge of an event they're planning
    # coverage for — resolve the username here instead of making the
    # frontend join against data it may not have access to.
    usernames = {u.id: u.username for u in session.exec(select(User)).all()}
    return [{**e.model_dump(), "created_by_username": usernames.get(e.created_by)} for e in events]


@router.get("/{event_id}")
def get_event(event_id: int, session: Session = Depends(get_session), _=Depends(participant_role)):
    event = session.get(Event, event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    creator = session.get(User, event.created_by)
    assignments = session.exec(select(EventAssignment).where(EventAssignment.event_id == event_id)).all()
    members_by_assignment = defaultdict(list)
    if assignments:
        rows = session.exec(select(EventCrewMember).where(
            EventCrewMember.event_assignment_id.in_([a.id for a in assignments])
        )).all()
        for m in rows:
            members_by_assignment[m.event_assignment_id].append((m.employee_id, m.is_leader))

    employees = {e.id: e for e in session.exec(select(Employee)).all()}
    ambulances = {a.id: a for a in session.exec(select(Ambulance)).all()}

    def emp_out(eid, is_leader=False):
        e = employees.get(eid)
        return {"id": e.id, "name": e.name, "role": e.role, "is_leader": is_leader} if e else None

    out_assignments = []
    for a in assignments:
        amb = ambulances.get(a.ambulance_id)
        out_assignments.append({
            "id": a.id,
            "ambulance": {"id": amb.id, "plate_no": amb.plate_no} if amb else None,
            "driver": emp_out(a.driver_employee_id) if a.driver_employee_id else None,
            "members": [emp_out(eid, is_leader) for eid, is_leader in members_by_assignment.get(a.id, [])],
        })
    event_out = {**event.model_dump(), "created_by_username": creator.username if creator else None}
    return {"event": event_out, "assignments": out_assignments}


def _candidate_pool(session: Session, run_id: int, day, is_driver: bool):
    """Returns (tier1, tier2, tier3) lists of eligible employee_ids for an event on `day`,
    ranked within each tier by standby/regulation-team membership first, then
    current shift load (least-loaded first)."""
    stats, by_employee_day = _run_shift_stats(session, run_id)
    leave_rows = session.exec(select(Leave).where(Leave.day == day)).all()
    on_leave_today = {l.employee_id for l in leave_rows}

    standby_ids = {
        s.employee_id for s in session.exec(
            select(StandbyAssignment).where(StandbyAssignment.run_id == run_id, StandbyAssignment.day == day)
        ).all()
    }

    # event_eligible gates this separately from ordinary rotation eligibility —
    # not everyone cleared to work a normal shift is cleared for event duty.
    pool = session.exec(select(Employee).where(
        Employee.is_driver == is_driver, Employee.active == True, Employee.facility_id == None,
        Employee.event_eligible == True,
    )).all()

    yesterday = day.__class__.fromordinal(day.toordinal() - 1)
    tier1, tier2, tier3 = [], [], []
    for e in pool:
        if e.id in on_leave_today:
            continue  # approved leave — never touched by event replanning
        today_code = by_employee_day.get(e.id, {}).get(day)
        was_night_yesterday = by_employee_day.get(e.id, {}).get(yesterday, "").startswith("N")
        if today_code and _is_shift_code(today_code):
            tier2.append(e)  # already working — reassigning them leaves a gap at their original post
        elif was_night_yesterday:
            tier3.append(e)  # mandated post-night rest day — last resort
        else:
            tier1.append(e)  # ordinary day off, or not on this run's roster at all — least disruptive

    def rank(group):
        # standby/regulation-team members first (natural pick for crew leader), then least-loaded
        return sorted(group, key=lambda e: (e.id not in standby_ids, stats.get(e.id, {"total": 0})["total"]))

    return rank(tier1), rank(tier2), rank(tier3)


def _set_event_day_code(session: Session, run_id: int, employee_id: int, day, is_night: bool):
    """Once someone is actually committed onto an event's crew for `day`,
    their Assignment row for that day needs to say so — "DE"/"NE" (day/night
    event, already defined in LEAVE_CODES), not a stale station D#/N# code
    left over from their original post, and not nothing at all for a tier-1
    pull who had no Assignment row that day in the first place (upsert)."""
    code = "NE" if is_night else "DE"
    row = session.exec(select(Assignment).where(
        Assignment.run_id == run_id, Assignment.employee_id == employee_id, Assignment.day == day,
    )).first()
    if row:
        row.code = code
        session.add(row)
    else:
        session.add(Assignment(run_id=run_id, employee_id=employee_id, day=day, code=code))


def _event_is_night_fallback(event: "Event") -> bool:
    """No shift concept exists on Event itself — for someone pulled onto it
    who had no origin shift to infer day/night from (a tier-1 pick, already
    off that day), fall back to event.time. Most EVENT_TYPES are daytime by
    default, so no time on file means day."""
    if not event.time:
        return False
    try:
        hour = int(event.time.split(":")[0])
    except (ValueError, IndexError):
        return False
    return hour >= 18 or hour < 6


def _origin_crew_assignment(session: Session, run_id: int, day, employee_id: int, is_driver: bool):
    """Where is this person actually posted today, if anywhere — the
    CrewAssignment they'd be pulled off of. Shared by pull_for_event and the
    replan commit's disruption bookkeeping so both compute "origin" the
    same way."""
    if is_driver:
        return session.exec(select(CrewAssignment).where(
            CrewAssignment.run_id == run_id, CrewAssignment.day == day,
            CrewAssignment.driver_employee_id == employee_id,
        )).first()
    crew_ids_today = session.exec(select(CrewAssignment.id).where(
        CrewAssignment.run_id == run_id, CrewAssignment.day == day,
    )).all()
    if not crew_ids_today:
        return None
    member_row = session.exec(select(CrewMember).where(
        CrewMember.employee_id == employee_id, CrewMember.crew_assignment_id.in_(crew_ids_today),
    )).first()
    return session.get(CrewAssignment, member_row.crew_assignment_id) if member_row else None


def _severity_for_removal(session: Session, origin_crew: CrewAssignment, employee_id: int, is_driver: bool) -> str:
    """Would pulling this person off `origin_crew` leave it without a
    driver/without any Pro-rated medical staff? That's the line between a
    numeric shortfall (operational) and a real safety gap."""
    if is_driver:
        origin_amb = session.get(Ambulance, origin_crew.ambulance_id)
        origin_station = session.get(Station, origin_amb.station_id) if origin_amb and origin_amb.station_id else None
        return "safety" if not origin_station or origin_station.min_drivers_per_crew >= 1 else "operational"
    other_members = session.exec(select(CrewMember).where(
        CrewMember.crew_assignment_id == origin_crew.id, CrewMember.employee_id != employee_id,
    )).all()
    other_pro_remaining = False
    for om in other_members:
        om_emp = session.get(Employee, om.employee_id)
        if om_emp and om_emp.is_pro:
            other_pro_remaining = True
            break
    return "safety" if not other_pro_remaining else "operational"


class ReplanRequest(BaseModel):
    run_id: int
    reason: Optional[str] = None
    justification: Optional[str] = None


def _compute_replan(session: Session, event: Event, run_id: int):
    """Pure computation, no DB writes: which ambulances/drivers/medical
    staff a replan would pick, and what that would disrupt. Shared by the
    read-only preview endpoint and the actual commit below, so "what you
    previewed" and "what you get" can never drift apart."""
    all_ambulances = session.exec(select(Ambulance).where(Ambulance.status == "active")).all()
    on_duty_today = {
        c.ambulance_id for c in session.exec(
            select(CrewAssignment).where(CrewAssignment.run_id == run_id, CrewAssignment.day == event.date)
        ).all()
    }
    spare = [a for a in all_ambulances if a.id not in on_duty_today]
    busy = [a for a in all_ambulances if a.id in on_duty_today]
    chosen_ambulances = (spare + busy)[:event.ambulances_required]
    ambulance_gap = max(event.ambulances_required - len(chosen_ambulances), 0)
    pulled_from_duty = [a for a in chosen_ambulances if a.id in on_duty_today]

    driver_t1, driver_t2, driver_t3 = _candidate_pool(session, run_id, event.date, is_driver=True)
    drivers_chosen = (driver_t1 + driver_t2 + driver_t3)[:event.drivers_required]
    driver_gap = max(event.drivers_required - len(drivers_chosen), 0)

    med_t1, med_t2, med_t3 = _candidate_pool(session, run_id, event.date, is_driver=False)
    medical_chosen = (med_t1 + med_t2 + med_t3)[:event.medical_crew_required]
    medical_gap = max(event.medical_crew_required - len(medical_chosen), 0)

    chosen_ids = {e.id for e in drivers_chosen} | {e.id for e in medical_chosen}

    plan = []
    for i, amb in enumerate(chosen_ambulances):
        driver = drivers_chosen[i] if i < len(drivers_chosen) else None
        members_for_this = medical_chosen[i::max(len(chosen_ambulances), 1)]
        # round-robin distribution means members_for_this[0] is always the
        # highest-ranked (standby-preferred) pick available for this crew —
        # that's who becomes the event's crew TL for this ambulance.
        plan.append({
            "ambulance_id": amb.id, "plate_no": amb.plate_no,
            "pulled_off_regular_duty": amb.id in on_duty_today,
            "driver": driver, "members": members_for_this,
        })

    disrupted_drivers = [e for e in (driver_t2 + driver_t3) if e.id in chosen_ids]
    disrupted_medical = [e for e in (med_t2 + med_t3) if e.id in chosen_ids]
    driver_t3_ids = {e.id for e in driver_t3}
    med_t3_ids = {e.id for e in med_t3}

    return {
        "plan": plan,
        "gaps": {"ambulances_short": ambulance_gap, "drivers_short": driver_gap, "medical_short": medical_gap},
        "would_be_status": "assigned" if not (ambulance_gap or driver_gap or medical_gap) else "planned",
        "disruption": {
            "ambulances_pulled_off_regular_duty": [a.plate_no for a in pulled_from_duty],
            "staff_reassigned_from_existing_shift": [e.name for e in (driver_t2 + med_t2) if e.id in chosen_ids],
            "staff_pulled_from_mandated_rest": [e.name for e in (driver_t3 + med_t3) if e.id in chosen_ids],
        },
        # kept out of the serialized preview response — used internally by
        # the commit path to notify people and write OperationalExceptions
        "disrupted_drivers": disrupted_drivers, "disrupted_medical": disrupted_medical,
        "driver_t3_ids": driver_t3_ids, "med_t3_ids": med_t3_ids,
    }


def _serialize_plan(computed: dict) -> dict:
    return {
        "assignments": [{
            "ambulance_id": p["ambulance_id"], "plate_no": p["plate_no"],
            "pulled_off_regular_duty": p["pulled_off_regular_duty"],
            "driver": {"id": p["driver"].id, "name": p["driver"].name} if p["driver"] else None,
            "members": [{"id": m.id, "name": m.name, "is_leader": (j == 0)} for j, m in enumerate(p["members"])],
        } for p in computed["plan"]],
        "gaps": computed["gaps"],
        "disruption": computed["disruption"],
        "has_disruption": any(computed["disruption"].values()),
    }


@router.get("/{event_id}/replan/preview")
def preview_replan(event_id: int, run_id: int,
                    session: Session = Depends(get_session), actor=Depends(participant_role)):
    """Read-only: "no compliant plan exists, here's what's possible" —
    computes the exact same plan replan_event would commit, without writing
    anything. Safe to call repeatedly while a leader compares options."""
    event = session.get(Event, event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    if not session.get(RosterRun, run_id):
        raise HTTPException(404, "Roster run not found")
    computed = _compute_replan(session, event, run_id)
    return {"event_id": event_id, "would_be_status": computed["would_be_status"], **_serialize_plan(computed)}


@router.post("/{event_id}/replan")
def replan_event(event_id: int, payload: ReplanRequest,
                  session: Session = Depends(get_session), actor=Depends(participant_role)):
    event = session.get(Event, event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    run = session.get(RosterRun, payload.run_id)
    if not run:
        raise HTTPException(404, "Roster run not found")

    computed = _compute_replan(session, event, payload.run_id)
    serialized = _serialize_plan(computed)
    # A clean, tier-1-only plan commits with no extra friction. The moment
    # it disrupts someone's existing post or mandated rest, this stops being
    # "the algorithm picked" and becomes a command decision — same reason +
    # justification requirement as every other override in this app.
    if serialized["has_disruption"] and not (payload.reason and payload.justification):
        raise HTTPException(400, "This plan disrupts existing coverage — a reason and justification are required to commit it.")

    # clear any previous replan attempt for this event so re-running is idempotent.
    # Delete children before parents explicitly — there's no ORM relationship() here
    # (just plain FK columns), so SQLAlchemy won't infer the dependency order itself.
    old_assignment_ids = session.exec(
        select(EventAssignment.id).where(EventAssignment.event_id == event_id)
    ).all()
    if old_assignment_ids:
        for m in session.exec(
            select(EventCrewMember).where(EventCrewMember.event_assignment_id.in_(old_assignment_ids))
        ).all():
            session.delete(m)
        session.flush()
        for a in session.exec(select(EventAssignment).where(EventAssignment.id.in_(old_assignment_ids))).all():
            session.delete(a)
        session.flush()

    for p in computed["plan"]:
        ea = EventAssignment(event_id=event_id, ambulance_id=p["ambulance_id"],
                              driver_employee_id=p["driver"].id if p["driver"] else None)
        session.add(ea)
        session.flush()
        if p["driver"]:
            origin = _origin_crew_assignment(session, payload.run_id, event.date, p["driver"].id, True)
            is_night = origin.shift == "night" if origin else _event_is_night_fallback(event)
            _set_event_day_code(session, payload.run_id, p["driver"].id, event.date, is_night)
        for j, m in enumerate(p["members"]):
            session.add(EventCrewMember(event_assignment_id=ea.id, employee_id=m.id, is_leader=(j == 0)))
            origin = _origin_crew_assignment(session, payload.run_id, event.date, m.id, False)
            is_night = origin.shift == "night" if origin else _event_is_night_fallback(event)
            _set_event_day_code(session, payload.run_id, m.id, event.date, is_night)

    event.status = computed["would_be_status"]
    session.add(event)
    log_activity(session, actor, "event.replan", "event", event.id,
                 f"Replanned '{event.name}' against roster #{payload.run_id} — "
                 f"{len(computed['plan'])} ambulance(s), gaps: {computed['gaps']['ambulances_short']} amb / "
                 f"{computed['gaps']['drivers_short']} drv / {computed['gaps']['medical_short']} med."
                 + (f" Reason: {payload.reason}." if serialized["has_disruption"] else ""))

    # Anyone actually pulled off their regular post (tier 2/3) needs to hear
    # about it directly — the API response alone only reaches whoever clicked
    # "replan" in the UI at that moment. Their manager hears about it too,
    # since it changes coverage at that person's original post, not just the
    # event itself. Each also gets its own OperationalException — a batch of
    # five pulled people is five separate command decisions, not one blob.
    disrupted_drivers, disrupted_medical = computed["disrupted_drivers"], computed["disrupted_medical"]
    event_when = event.date.isoformat() + (f" {event.time}" if event.time else "")
    for e, is_driver in [(e, True) for e in disrupted_drivers] + [(e, False) for e in disrupted_medical]:
        notify_employee(
            session, e.id,
            title=f"Reassigned to event: {event.name}",
            body=f"Your regular shift on {event.date.isoformat()} has been reassigned — "
                 f"you're now covering the event '{event.name}' at {event.location} on {event_when} instead.",
            related_type="event_replan", related_id=event.id,
        )
        origin = _origin_crew_assignment(session, payload.run_id, event.date, e.id, is_driver)
        if origin:
            t3_ids = computed["driver_t3_ids"] if is_driver else computed["med_t3_ids"]
            severity = "safety" if e.id in t3_ids else _severity_for_removal(session, origin, e.id, is_driver)
            origin_amb = session.get(Ambulance, origin.ambulance_id)
            record_exception(
                session, actor, exception_type="event_replan",
                rule_broken="rest_after_night" if e.id in t3_ids else "no_replacement",
                severity=severity, reason=payload.reason, justification=payload.justification,
                impact_summary=f"Reassigned {e.name} from {origin.shift} shift, ambulance "
                                f"{origin_amb.plate_no if origin_amb else origin.ambulance_id} to cover "
                                f"'{event.name}' on {event.date.isoformat()}.",
                run_id=payload.run_id, event_id=event.id, day=event.date, shift=origin.shift,
                station_id=origin_amb.station_id if origin_amb else None, ambulance_id=origin.ambulance_id,
                employee_id=e.id, crew_assignment_id=origin.id,
            )
    if disrupted_drivers:
        for mgr in session.exec(select(User).where(User.role.in_(("drivers_manager", "admin")),
                                                     User.employee_id != None)).all():
            notify_employee(
                session, mgr.employee_id,
                title=f"Drivers reassigned to event: {event.name}",
                body=f"{len(disrupted_drivers)} driver(s) were pulled off their regular {event.date.isoformat()} "
                     f"post to cover '{event.name}': {', '.join(e.name for e in disrupted_drivers)}.",
                related_type="event_replan", related_id=event.id,
            )
    if disrupted_medical:
        for mgr in session.exec(select(User).where(User.role.in_(("phtl", "admin")),
                                                     User.employee_id != None)).all():
            notify_employee(
                session, mgr.employee_id,
                title=f"Medical crew reassigned to event: {event.name}",
                body=f"{len(disrupted_medical)} medical staff member(s) were pulled off their regular "
                     f"{event.date.isoformat()} post to cover '{event.name}': "
                     f"{', '.join(e.name for e in disrupted_medical)}.",
                related_type="event_replan", related_id=event.id,
            )

    session.commit()

    return {"event_id": event_id, "status": event.status, **_serialize_plan(computed)}


class PullForEventRequest(BaseModel):
    run_id: int
    employee_id: int
    ambulance_id: int   # which of the EVENT's ambulances to place them on
    slot: str            # "driver" | "member"
    reason: str           # one of EXCEPTION_REASONS
    justification: str    # free-text — why this specific person, right now


@router.post("/{event_id}/pull")
def pull_for_event(event_id: int, payload: PullForEventRequest,
                    session: Session = Depends(get_session), actor: User = Depends(participant_role)):
    """The deliberate, human-judgement escape hatch replan_event's tiers
    don't cover: standby's already spent, and PHTL (or Drivers Manager)
    decides a SPECIFIC person is coming off their regular crew for this
    event regardless — no algorithm picks who, no automatic floor stops it.
    The resulting gap at their original post isn't hidden: it's logged,
    notified, and still shows up in validate_roster() afterward."""
    event = session.get(Event, event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    if payload.slot not in ("driver", "member"):
        raise HTTPException(400, "slot must be 'driver' or 'member'")
    employee = session.get(Employee, payload.employee_id)
    if not employee or not employee.active:
        raise HTTPException(400, "Employee not found or inactive.")
    if payload.slot == "driver" and not employee.is_driver:
        raise HTTPException(400, "That employee isn't a qualified driver.")
    if payload.slot == "member" and employee.is_driver:
        raise HTTPException(400, "That employee is a driver, not medical crew.")
    # Same lane boundary as everywhere else: Drivers Manager only pulls
    # drivers, PHTL only pulls medical staff, admin either.
    if actor.role == "drivers_manager" and payload.slot != "driver":
        raise HTTPException(403, "Drivers Manager can only pull drivers.")
    if actor.role == "phtl" and payload.slot != "member":
        raise HTTPException(403, "PHTL can only pull medical crew.")
    ambulance = session.get(Ambulance, payload.ambulance_id)
    if not ambulance:
        raise HTTPException(404, "Ambulance not found")
    if payload.reason not in EXCEPTION_REASONS:
        raise HTTPException(400, f"reason must be one of {EXCEPTION_REASONS}")
    if not payload.justification or not payload.justification.strip():
        raise HTTPException(400, "A written justification is required.")

    # Remove them from wherever they're currently posted that day, if anywhere.
    # rule_broken/severity stay None when there's nothing to disrupt (no prior
    # assignment that day) — no OperationalException is recorded for a plain
    # assignment that didn't break anything.
    removed_from = None
    rule_broken = None
    severity = None
    origin_crew_id = None
    is_driver_slot = payload.slot == "driver"
    origin = _origin_crew_assignment(session, payload.run_id, event.date, payload.employee_id, is_driver_slot)
    if origin:
        removed_from = (f"driver seat, ambulance #{origin.ambulance_id} ({origin.shift})" if is_driver_slot
                         else f"medical crew, ambulance #{origin.ambulance_id} ({origin.shift})")
        rule_broken = "no_replacement"
        severity = _severity_for_removal(session, origin, payload.employee_id, is_driver_slot)
        origin_crew_id = origin.id
        if is_driver_slot:
            origin.driver_employee_id = None
            session.add(origin)
        else:
            member_row = session.exec(select(CrewMember).where(
                CrewMember.crew_assignment_id == origin.id, CrewMember.employee_id == payload.employee_id,
            )).first()
            if member_row:
                session.delete(member_row)

    # Place them on the event.
    ea = session.exec(select(EventAssignment).where(
        EventAssignment.event_id == event_id, EventAssignment.ambulance_id == payload.ambulance_id,
    )).first()
    if not ea:
        ea = EventAssignment(event_id=event_id, ambulance_id=payload.ambulance_id)
        session.add(ea)
        session.flush()
    if payload.slot == "driver":
        ea.driver_employee_id = employee.id
        session.add(ea)
    else:
        session.add(EventCrewMember(event_assignment_id=ea.id, employee_id=employee.id, is_leader=False))

    is_night = origin.shift == "night" if origin else _event_is_night_fallback(event)
    _set_event_day_code(session, payload.run_id, employee.id, event.date, is_night)

    origin_desc = removed_from or "no prior assignment that day"
    log_activity(session, actor, "event.pull", "event", event.id,
                 f"Pulled {employee.name} from {origin_desc} to cover '{event.name}' ({payload.slot}) "
                 f"— deliberate short-staffing decision, standby exhausted. Reason: {payload.reason}.")

    if removed_from and rule_broken:
        record_exception(
            session, actor,
            exception_type="pull_for_event", rule_broken=rule_broken, severity=severity,
            reason=payload.reason, justification=payload.justification,
            impact_summary=f"Pulled {employee.name} from {removed_from} to cover '{event.name}' "
                            f"on {event.date.isoformat()} — that post is now short-staffed.",
            run_id=payload.run_id, event_id=event.id, day=event.date,
            employee_id=employee.id, ambulance_id=payload.ambulance_id, crew_assignment_id=origin_crew_id,
        )

    notify_employee(
        session, employee.id,
        title=f"Reassigned to event: {event.name}",
        body=f"You've been pulled from {origin_desc} to cover '{event.name}' at {event.location} on {event.date.isoformat()}.",
        related_type="event_pull", related_id=event.id,
    )
    if removed_from:
        manager_roles = ("drivers_manager", "admin") if payload.slot == "driver" else ("phtl", "admin")
        for mgr in session.exec(select(User).where(User.role.in_(manager_roles), User.employee_id != None)).all():
            notify_employee(
                session, mgr.employee_id,
                title=f"Crew pulled short for event: {event.name}",
                body=f"{employee.name} was pulled from {origin_desc} to cover '{event.name}' — "
                     f"that post is now short-staffed as a deliberate decision.",
                related_type="event_pull", related_id=event.id,
            )

    session.commit()
    return {"ok": True, "removed_from": removed_from, "severity": severity if removed_from else None}
