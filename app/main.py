import calendar
from datetime import date, datetime, timedelta
from collections import defaultdict
from typing import Optional, List
from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select
from pydantic import BaseModel
import io, os, json, tempfile

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment

from fastapi.middleware.gzip import GZipMiddleware

from . import jobs
from .database import init_db, get_session, engine
from .models import (
    Station, Employee, Leave, RosterRun, Assignment, LEAVE_CODES,
    Ambulance, CrewAssignment, CrewMember, StandbyAssignment, StandbySettings, User, Facility,
    Certification, Notification, EventAssignment, EventCrewMember, OperationalException,
    MIDDLE_SITE_CODES,
)
from .scheduler import (generate_roster, date_range, _shift_hours, MAX_CONSECUTIVE_DAYS,
                         is_work_shift_code, schedule_facility_staff, assign_standby, assign_regulation_nurses,
                         rebalance_medical_hours)
from .auth import router as auth_router, get_current_user, require_role
from .ambulances import router as ambulances_router
from .crew import router as crew_router
from .overrides import router as overrides_router
from .notifications import router as notifications_router, notify_employee
from .events import router as events_router
from .audit import router as audit_router, log_activity
from .facilities import router as facilities_router
from .operational_exceptions import router as exceptions_router

# Call & Dispatch's OWN independent Regulation-desk staffing target per
# shift, confirmed directly — separate from the desk's COMBINED total
# (Facility.required_headcount), which is what PHTL's medical
# critical/anesthesiologist pool fills the rest of.
DISPATCH_OWN_REGULATION_TARGET = 2

app = FastAPI(title="SAMU Fair Rostering System")
app.add_middleware(GZipMiddleware, minimum_size=1024)  # rosters/employee lists are large JSON; the 3k-line page too
app.include_router(auth_router)
app.include_router(ambulances_router)
app.include_router(crew_router)
app.include_router(overrides_router)
app.include_router(notifications_router)
app.include_router(events_router)
app.include_router(audit_router)
app.include_router(facilities_router)
app.include_router(exceptions_router)

# Any authenticated user, regardless of role.
any_role = require_role("admin", "call_center_tl", "drivers_manager", "phtl")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# The OS temp dir (not a folder next to the app code) so this works on a
# read-only deployment filesystem too (Vercel's function bundle is
# read-only except /tmp) — same call resolves to a normal writable temp
# folder locally and in Docker, no environment-specific branching needed.
OUTPUT_DIR = os.path.join(tempfile.gettempdir(), "ems_roster_exports")
os.makedirs(OUTPUT_DIR, exist_ok=True)


def _period_label(run: RosterRun) -> str:
    if run.start_date == run.end_date:
        return run.start_date.strftime("%b %d, %Y")
    same_month = (run.start_date.year, run.start_date.month) == (run.end_date.year, run.end_date.month)
    start_fmt = run.start_date.strftime("%b %d") if same_month else run.start_date.strftime("%b %d, %Y")
    return f"{start_fmt} – {run.end_date.strftime('%b %d, %Y')}"


@app.on_event("startup")
def on_startup():
    init_db()


# ---------- Stations ----------
class StationCreate(BaseModel):
    name: str
    code: str
    day_crew_size: int = 3
    night_crew_size: int = 3
    min_drivers_per_crew: int = 1
    active: bool = True
    night_active: bool = True


class StationUpdate(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None
    day_crew_size: Optional[int] = None
    night_crew_size: Optional[int] = None
    min_drivers_per_crew: Optional[int] = None
    active: Optional[bool] = None
    night_active: Optional[bool] = None


@app.get("/api/stations")
def list_stations(session: Session = Depends(get_session), _=Depends(any_role)):
    return session.exec(select(Station)).all()


@app.post("/api/stations")
def create_station(payload: StationCreate, session: Session = Depends(get_session), _=Depends(require_role("admin"))):
    if payload.min_drivers_per_crew > payload.day_crew_size or payload.min_drivers_per_crew > payload.night_crew_size:
        raise HTTPException(400, "min_drivers_per_crew can't exceed the day or night crew size.")
    station = Station(**payload.dict())
    session.add(station)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, f"Station code '{payload.code}' is already in use.")
    session.refresh(station)
    return station


@app.put("/api/stations/{station_id}")
def update_station(station_id: int, payload: StationUpdate, session: Session = Depends(get_session), _=Depends(require_role("admin"))):
    station = session.get(Station, station_id)
    if not station:
        raise HTTPException(404, "Station not found")
    updates = payload.dict(exclude_unset=True)
    for k, v in updates.items():
        setattr(station, k, v)
    # min_drivers_per_crew above the crew size would otherwise silently floor
    # medical crew need to 0 (scheduler.py clamps it there) rather than erroring
    # — surfacing it here means a typo doesn't quietly zero out a station's
    # medical coverage.
    if station.min_drivers_per_crew > station.day_crew_size or station.min_drivers_per_crew > station.night_crew_size:
        raise HTTPException(400, "min_drivers_per_crew can't exceed the day or night crew size.")
    session.add(station)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, f"Station code '{payload.code}' is already in use.")
    session.refresh(station)
    return station


def _station_delete_preview(session: Session, station_id: int) -> dict:
    """What a forced delete of this station would actually do, computed
    without touching anything — shared by the preview endpoint (so the UI
    can show a precise warning before the user confirms) and the delete
    endpoint itself (so the same check gates the real mutation)."""
    active_employees = session.exec(
        select(Employee).where(Employee.station_id == station_id, Employee.active == True)
    ).all()
    ambulances = session.exec(select(Ambulance).where(Ambulance.station_id == station_id)).all()
    ambulance_ids = [a.id for a in ambulances]
    blocking_ambulances = []
    crew_assignment_count = 0
    event_assignment_count = 0
    for amb in ambulances:
        crew_rows = session.exec(select(CrewAssignment).where(CrewAssignment.ambulance_id == amb.id)).all()
        event_rows = session.exec(select(EventAssignment).where(EventAssignment.ambulance_id == amb.id)).all()
        crew_assignment_count += len(crew_rows)
        event_assignment_count += len(event_rows)
        if crew_rows or event_rows:
            blocking_ambulances.append(amb.plate_no)
    # A station with any generated-roster history (even one that's since been
    # fully vacated of active staff/ambulances) leaves that history's
    # station_id dangling if deleted — normally never force-able, only
    # admin's stronger force_history purge (see delete_station) goes further.
    assignment_count = len(session.exec(select(Assignment).where(Assignment.station_id == station_id)).all())
    return {
        "blocked_by_history": assignment_count > 0,
        "active_employees": [e.name for e in active_employees],
        "ambulances_to_delete": [a.plate_no for a in ambulances if a.plate_no not in blocking_ambulances],
        "blocking_ambulances": blocking_ambulances,
        "can_force_delete": assignment_count == 0 and not blocking_ambulances,
        "crew_assignment_count": crew_assignment_count,
        "event_assignment_count": event_assignment_count,
        "assignment_count": assignment_count,
    }


@app.get("/api/stations/{station_id}/delete-preview")
def station_delete_preview(station_id: int, session: Session = Depends(get_session), _=Depends(require_role("admin"))):
    if not session.get(Station, station_id):
        raise HTTPException(404, "Station not found")
    return _station_delete_preview(session, station_id)


@app.delete("/api/stations/{station_id}")
def delete_station(station_id: int, force: bool = False, force_history: bool = False,
                    session: Session = Depends(get_session), actor: User = Depends(require_role("admin"))):
    st = session.get(Station, station_id)
    if not st:
        raise HTTPException(404, "Station not found")

    preview = _station_delete_preview(session, station_id)

    if force_history:
        # The one truly irreversible option: admin has explicitly chosen to
        # purge this station's real roster history, not just detach it. The
        # current schedule's crew/assignment rows for it are gone, not
        # orphaned — only the Activity Log entry below survives as a record
        # this ever existed. Employees themselves are still only detached,
        # never deleted.
        ambulance_ids = [a.id for a in session.exec(select(Ambulance).where(Ambulance.station_id == station_id)).all()]
        crew_ids = session.exec(select(CrewAssignment.id).where(CrewAssignment.ambulance_id.in_(ambulance_ids))).all() if ambulance_ids else []
        event_assignment_ids = session.exec(select(EventAssignment.id).where(EventAssignment.ambulance_id.in_(ambulance_ids))).all() if ambulance_ids else []

        if crew_ids:
            for m in session.exec(select(CrewMember).where(CrewMember.crew_assignment_id.in_(crew_ids))).all():
                session.delete(m)
        if event_assignment_ids:
            for m in session.exec(select(EventCrewMember).where(EventCrewMember.event_assignment_id.in_(event_assignment_ids))).all():
                session.delete(m)
        session.flush()

        # OperationalException rows referencing what's about to go are kept
        # as audit trail (same detach-not-delete pattern as roster/event
        # delete) — just unlinked from the crew/station/ambulance being purged.
        exceptions_to_detach = session.exec(select(OperationalException).where(
            OperationalException.station_id == station_id
        )).all()
        if crew_ids:
            exceptions_to_detach += session.exec(select(OperationalException).where(
                OperationalException.crew_assignment_id.in_(crew_ids)
            )).all()
        if ambulance_ids:
            exceptions_to_detach += session.exec(select(OperationalException).where(
                OperationalException.ambulance_id.in_(ambulance_ids)
            )).all()
        for exc in {e.id: e for e in exceptions_to_detach}.values():
            if crew_ids and exc.crew_assignment_id in crew_ids:
                exc.crew_assignment_id = None
            if ambulance_ids and exc.ambulance_id in ambulance_ids:
                exc.ambulance_id = None
            if exc.station_id == station_id:
                exc.station_id = None
            session.add(exc)
        session.flush()

        if crew_ids:
            for c in session.exec(select(CrewAssignment).where(CrewAssignment.id.in_(crew_ids))).all():
                session.delete(c)
        if event_assignment_ids:
            for e in session.exec(select(EventAssignment).where(EventAssignment.id.in_(event_assignment_ids))).all():
                session.delete(e)
        for a in session.exec(select(Assignment).where(Assignment.station_id == station_id)).all():
            session.delete(a)
        session.flush()

        for emp in session.exec(select(Employee).where(Employee.station_id == station_id, Employee.active == True)).all():
            emp.station_id = None
            session.add(emp)
        for amb in session.exec(select(Ambulance).where(Ambulance.station_id == station_id)).all():
            session.delete(amb)

        log_activity(session, actor, "station.delete", "station", st.id,
                     f"PERMANENTLY deleted station '{st.name}' including its roster history — "
                     f"purged {preview['crew_assignment_count']} crew assignment(s), "
                     f"{preview['event_assignment_count']} event assignment(s), "
                     f"{preview['assignment_count']} schedule row(s); detached {len(preview['active_employees'])} "
                     f"employee(s); removed {len(preview['ambulances_to_delete']) + len(preview['blocking_ambulances'])} ambulance(s).")
        session.delete(st)
        session.commit()
        return {"ok": True, "purged_crew_assignments": preview["crew_assignment_count"],
                "purged_assignments": preview["assignment_count"]}

    if preview["blocked_by_history"]:
        raise HTTPException(409, "This station has roster history and can't be deleted — "
                                  "set it inactive instead to keep it out of future generation.")
    if preview["blocking_ambulances"]:
        raise HTTPException(409, f"Ambulance(s) {', '.join(preview['blocking_ambulances'])} have roster or event "
                                  f"history and can't be deleted — set their status to 'out_of_service' instead, "
                                  f"or set this station inactive rather than deleting it.")

    if not force:
        if preview["active_employees"]:
            raise HTTPException(409, "This station still has active employees assigned to it. "
                                      "Reassign or deactivate them first.")
        if preview["ambulances_to_delete"]:
            raise HTTPException(409, "This station still has ambulance(s) assigned to it. Reassign or remove them first.")
    else:
        # Confirmed clean by the preview above — no history, no ambulance
        # with history of its own. Detach staff (they stay active employees,
        # just without a home station) and remove the now-orphaned ambulances.
        for emp in session.exec(select(Employee).where(Employee.station_id == station_id, Employee.active == True)).all():
            emp.station_id = None
            session.add(emp)
        for amb in session.exec(select(Ambulance).where(Ambulance.station_id == station_id)).all():
            session.delete(amb)

    log_activity(session, actor, "station.delete", "station", st.id,
                 f"Deleted station '{st.name}'"
                 + (f" — detached {len(preview['active_employees'])} employee(s), "
                    f"removed {len(preview['ambulances_to_delete'])} ambulance(s): "
                    f"{', '.join(preview['ambulances_to_delete'])}." if force and
                    (preview["active_employees"] or preview["ambulances_to_delete"]) else "."))

    session.delete(st)
    session.commit()
    return {"ok": True, "detached_employees": len(preview["active_employees"]),
             "deleted_ambulances": len(preview["ambulances_to_delete"])}


# ---------- Standby settings ----------
class StandbySettingsUpdate(BaseModel):
    driver_day: int
    driver_night: int
    medical_day: int
    medical_night: int
    call_center_day: int
    call_center_night: int


@app.get("/api/standby-settings")
def get_standby_settings(session: Session = Depends(get_session), _: User = Depends(any_role)):
    settings = _get_standby_settings(session)
    session.commit()
    session.refresh(settings)
    return settings


@app.put("/api/standby-settings")
def update_standby_settings(payload: StandbySettingsUpdate, session: Session = Depends(get_session),
                             actor: User = Depends(require_role("admin"))):
    for field, value in payload.dict().items():
        if value < 0:
            raise HTTPException(400, f"{field} can't be negative.")
    settings = _get_standby_settings(session)
    for field, value in payload.dict().items():
        setattr(settings, field, value)
    session.add(settings)
    log_activity(session, actor, "standby_settings.update", "standby_settings", settings.id,
                 f"Updated standby pools — drivers {settings.driver_day}/{settings.driver_night}, "
                 f"medical {settings.medical_day}/{settings.medical_night}, "
                 f"call center {settings.call_center_day}/{settings.call_center_night} (day/night).")
    session.commit()
    session.refresh(settings)
    return settings


# ---------- Employees ----------
class EmployeeCreate(BaseModel):
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    role: str
    is_driver: bool = False
    is_team_leader: bool = False
    station_id: Optional[int] = None
    facility_id: Optional[int] = None
    restricted_night_shift: bool = False
    night_only: bool = False
    active: bool = True
    skill_score: int = 1
    is_pro: bool = False
    floats_freely: bool = False
    event_eligible: bool = True


class EmployeeUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None
    is_driver: Optional[bool] = None
    is_team_leader: Optional[bool] = None
    station_id: Optional[int] = None
    facility_id: Optional[int] = None
    restricted_night_shift: Optional[bool] = None
    night_only: Optional[bool] = None
    active: Optional[bool] = None
    skill_score: Optional[int] = None
    is_pro: Optional[bool] = None
    floats_freely: Optional[bool] = None
    event_eligible: Optional[bool] = None


@app.get("/api/employees")
def list_employees(session: Session = Depends(get_session), actor: User = Depends(any_role)):
    # Names/roles/flags are needed broadly (Crew Rollup, standby, candidate
    # search all cross-reference people outside a manager's own lane), so
    # the list itself isn't filtered — but contact info has no legitimate
    # cross-lane use, so it's stripped for anyone outside the viewer's lane.
    employees = session.exec(select(Employee)).all()
    out = []
    for e in employees:
        row = e.dict()
        if not _employee_in_actor_lane(e, actor.role):
            row["phone"] = None
            row["email"] = None
        out.append(row)
    return out


@app.get("/api/employees/hours-worked")
def employees_hours_worked(
    start_date: Optional[date] = None, end_date: Optional[date] = None,
    session: Session = Depends(get_session), _: User = Depends(any_role),
):
    """employee_id -> hours actually worked (from Assignment rows across
    every roster run, not tied to whichever one happens to be open) over a
    date range — defaults to the current calendar month so Workforce can show
    "hours this month" without the caller having to know today's date."""
    if start_date is None or end_date is None:
        today = date.today()
        start_date = date(today.year, today.month, 1)
        next_month = date(today.year + (today.month == 12), (today.month % 12) + 1, 1)
        end_date = next_month - timedelta(days=1)
    if end_date < start_date:
        raise HTTPException(400, "end_date can't be before start_date.")

    rows = session.exec(select(Assignment).where(
        Assignment.day >= start_date, Assignment.day <= end_date
    )).all()
    hours = defaultdict(int)
    for a in rows:
        hours[a.employee_id] += _shift_hours(a.code)
    return {"start_date": start_date, "end_date": end_date, "hours": hours}


class EmployeeBulkUpdate(BaseModel):
    # Filter: which employees this touches. filter_role=None means "every
    # active employee" — every filter is AND-ed together.
    filter_role: Optional[str] = None
    filter_active_only: bool = True
    # Set: only fields explicitly provided are changed; everything else on
    # each matched employee is left exactly as-is.
    set_role: Optional[str] = None
    set_is_driver: Optional[bool] = None
    set_is_pro: Optional[bool] = None
    set_event_eligible: Optional[bool] = None
    # True: report exactly who/what would change and stop there — no writes,
    # no commit, no audit-log entry. Lets the caller preview a bulk edit
    # before actually committing to it on a filter that could match far more
    # (or fewer) records than expected.
    dry_run: bool = False


@app.post("/api/employees/bulk-update")
def bulk_update_employees(payload: EmployeeBulkUpdate, session: Session = Depends(get_session),
                           actor: User = Depends(require_role("admin"))):
    """Admin-only bulk edit — for exactly the kind of cleanup real imported
    data tends to need (e.g. every employee still sitting at role
    "unassigned" needs reclassifying in one pass instead of 148 individual
    edits). Only the set_* fields actually provided get changed; the filter
    narrows which employees are touched. Every match is still logged
    individually to the Activity Log, not just as one bulk line, so it's
    fully auditable afterward."""
    changes = {}
    if payload.set_role is not None:
        changes["role"] = payload.set_role
    if payload.set_is_driver is not None:
        changes["is_driver"] = payload.set_is_driver
    if payload.set_is_pro is not None:
        changes["is_pro"] = payload.set_is_pro
    if payload.set_event_eligible is not None:
        changes["event_eligible"] = payload.set_event_eligible
    if not changes:
        raise HTTPException(400, "Nothing to set — provide at least one of set_role, set_is_driver, "
                                  "set_is_pro, set_event_eligible.")

    query = select(Employee)
    if payload.filter_active_only:
        query = query.where(Employee.active == True)
    if payload.filter_role is not None:
        query = query.where(Employee.role == payload.filter_role)
    matched = session.exec(query).all()

    if payload.dry_run:
        return {
            "ok": True, "dry_run": True, "matched": len(matched),
            "sample": [{"id": e.id, "name": e.name, "role": e.role} for e in matched[:20]],
            "changes": changes,
        }

    for emp in matched:
        for k, v in changes.items():
            setattr(emp, k, v)
        session.add(emp)

    log_activity(session, actor, "employee.bulk_update", "employee", None,
                 f"Bulk-updated {len(matched)} employee(s) "
                 f"(filter: role={payload.filter_role or 'any'}, active_only={payload.filter_active_only}) "
                 f"— set {', '.join(f'{k}={v}' for k, v in changes.items())}.")
    session.commit()
    return {"ok": True, "updated": len(matched)}


@app.post("/api/employees")
def create_employee(payload: EmployeeCreate, session: Session = Depends(get_session), actor: User = Depends(require_role("admin"))):
    employee = Employee(**payload.dict())
    session.add(employee)
    session.commit()
    session.refresh(employee)
    log_activity(session, actor, "employee.create", "employee", employee.id, f"Added employee '{employee.name}' ({employee.role}).")
    session.commit()
    return employee


@app.put("/api/employees/{employee_id}")
def update_employee(employee_id: int, payload: EmployeeUpdate, session: Session = Depends(get_session), actor: User = Depends(require_role("admin"))):
    existing = session.get(Employee, employee_id)
    if not existing:
        raise HTTPException(404, "Employee not found")
    changes = payload.dict(exclude_unset=True)
    for k, v in changes.items():
        setattr(existing, k, v)
    session.add(existing)
    if changes:
        log_activity(session, actor, "employee.update", "employee", existing.id,
                     f"Updated employee '{existing.name}': {', '.join(changes.keys())}.")
    session.commit()
    session.refresh(existing)
    return existing


@app.delete("/api/employees/{employee_id}")
def delete_employee(employee_id: int, session: Session = Depends(get_session), actor: User = Depends(require_role("admin"))):
    emp = session.get(Employee, employee_id)
    if not emp:
        raise HTTPException(404, "Employee not found")

    referenced = any([
        session.exec(select(Leave.id).where(Leave.employee_id == employee_id)).first(),
        session.exec(select(Assignment.id).where(Assignment.employee_id == employee_id)).first(),
        session.exec(select(CrewAssignment.id).where(CrewAssignment.driver_employee_id == employee_id)).first(),
        session.exec(select(CrewMember.id).where(CrewMember.employee_id == employee_id)).first(),
        session.exec(select(StandbyAssignment.id).where(StandbyAssignment.employee_id == employee_id)).first(),
        session.exec(select(Notification.id).where(Notification.employee_id == employee_id)).first(),
        session.exec(select(Certification.id).where(Certification.employee_id == employee_id)).first(),
        session.exec(select(EventAssignment.id).where(EventAssignment.driver_employee_id == employee_id)).first(),
        session.exec(select(EventCrewMember.id).where(EventCrewMember.employee_id == employee_id)).first(),
        session.exec(select(User.id).where(User.employee_id == employee_id)).first(),
    ])
    if referenced:
        emp.active = False
        session.add(emp)
        log_activity(session, actor, "employee.deactivate", "employee", emp.id,
                     f"Deactivated employee '{emp.name}' because they are referenced by roster history.")
        session.commit()
        session.refresh(emp)
        return {"ok": True, "deactivated": True, "employee": emp}

    log_activity(session, actor, "employee.delete", "employee", emp.id,
                 f"Removed unused employee '{emp.name}'.")
    session.delete(emp)
    session.commit()
    return {"ok": True, "deactivated": False}


# ---------- Leave ----------
@app.get("/api/leave")
def list_leave(start_date: date, end_date: date, session: Session = Depends(get_session), _=Depends(any_role)):
    rows = session.exec(select(Leave).where(Leave.day >= start_date, Leave.day <= end_date)).all()
    return rows


class LeaveIn(BaseModel):
    employee_id: int
    day: date                        # start date (or the only date, if end_day is omitted)
    end_day: Optional[date] = None   # inclusive end of the range — a single day if omitted
    code: str


# Fixed entitlements — everything else (AL, SL, Permission, Forced off,
# Mission, ...) stays manually entered with no cap, varying by employer/
# contract. These two are checked and flagged, never blocked (matches
# every other soft-constraint in this app: warn clearly, don't silently
# stop someone from recording real HR exceptions).
LEAVE_ENTITLEMENT_DAYS = {"ML": 96, "PL": 7}


def _contiguous_leave_block_days(session: Session, employee_id: int, code: str, day: date) -> int:
    """
    How many consecutive days (including `day` itself) of this same leave
    code form one unbroken block for this employee — walking backward and
    forward through their existing records. Used as a stand-in for "one
    maternity/paternity leave instance" since leave isn't tracked as a
    single date-range request, just individual day records.
    """
    existing_days = set(session.exec(
        select(Leave.day).where(Leave.employee_id == employee_id, Leave.code == code)
    ).all())
    existing_days.add(day)
    count = 1
    cursor = day - timedelta(days=1)
    while cursor in existing_days:
        count += 1
        cursor -= timedelta(days=1)
    cursor = day + timedelta(days=1)
    while cursor in existing_days:
        count += 1
        cursor += timedelta(days=1)
    return count


@app.post("/api/leave")
def create_leave(payload: LeaveIn, session: Session = Depends(get_session), actor: User = Depends(require_role("admin"))):
    if payload.code not in LEAVE_CODES:
        raise HTTPException(400, f"Unknown leave code. Use one of {list(LEAVE_CODES)}")
    end_day = payload.end_day or payload.day
    if end_day < payload.day:
        raise HTTPException(400, "End date can't be before the start date.")
    if (end_day - payload.day).days > 400:
        raise HTTPException(400, "That range is unusually long — check the dates.")

    emp = session.get(Employee, payload.employee_id)
    created_days = []
    cursor = payload.day
    while cursor <= end_day:
        session.add(Leave(employee_id=payload.employee_id, day=cursor, code=payload.code))
        created_days.append(cursor)
        cursor += timedelta(days=1)
    session.commit()

    when = payload.day.isoformat() if end_day == payload.day else f"{payload.day.isoformat()} to {end_day.isoformat()}"
    log_activity(session, actor, "leave.create", "leave", None,
                 f"Added {payload.code} ({LEAVE_CODES.get(payload.code, payload.code)}) for "
                 f"{emp.name if emp else 'employee #'+str(payload.employee_id)}: {when} ({len(created_days)} day(s)).")
    session.commit()

    warning = None
    entitlement = LEAVE_ENTITLEMENT_DAYS.get(payload.code)
    if entitlement is not None:
        block_days = _contiguous_leave_block_days(session, payload.employee_id, payload.code, end_day)
        if block_days > entitlement:
            warning = (f"{emp.name if emp else 'This employee'}'s continuous {LEAVE_CODES[payload.code]} block "
                       f"is now {block_days} days — {block_days - entitlement} over the standard {entitlement}-day entitlement.")

    return {"days_created": len(created_days), "start": payload.day.isoformat(), "end": end_day.isoformat(), "warning": warning}


@app.delete("/api/leave/{leave_id}")
def delete_leave(leave_id: int, session: Session = Depends(get_session), actor: User = Depends(require_role("admin"))):
    lv = session.get(Leave, leave_id)
    if not lv:
        raise HTTPException(404, "Leave record not found")
    emp = session.get(Employee, lv.employee_id)
    log_activity(session, actor, "leave.delete", "leave", lv.id,
                 f"Removed {lv.code} for {emp.name if emp else 'employee #'+str(lv.employee_id)} on {lv.day.isoformat()}.")
    session.delete(lv)
    session.commit()
    return {"ok": True}


# ---------- Roster generation ----------
def _month_bounds(d: date):
    last_day = calendar.monthrange(d.year, d.month)[1]
    return date(d.year, d.month, 1), date(d.year, d.month, last_day)


def _starting_hours_for_month(session: Session, run_start: date, exclude_run_id: int) -> dict:
    """
    {employee_id: {"total","night","weekend"}} of hours already worked
    elsewhere in run_start's calendar month, from every OTHER roster run
    that overlaps that month (draft or not — a draft still represents
    the plan so far). Feeds generate_roster()'s starting_hours so a
    roster generated week-by-week still balances by month's end instead
    of each week only being fair in isolation.
    """
    month_start, month_end = _month_bounds(run_start)
    prior_runs = session.exec(select(RosterRun).where(
        RosterRun.id != exclude_run_id,
        RosterRun.start_date <= month_end,
        RosterRun.end_date >= month_start,
    )).all()
    result = defaultdict(lambda: {"total": 0, "night": 0, "weekend": 0})
    if not prior_runs:
        return result
    run_ids = [r.id for r in prior_runs]
    rows = session.exec(select(Assignment).where(
        Assignment.run_id.in_(run_ids),
        Assignment.day >= month_start, Assignment.day <= month_end,
    )).all()
    for a in rows:
        hrs = _shift_hours(a.code)
        if hrs == 0:
            continue
        result[a.employee_id]["total"] += hrs
        if a.code[0] == "N":
            result[a.employee_id]["night"] += hrs
        if a.day.weekday() >= 5:
            result[a.employee_id]["weekend"] += hrs
    return result


def _boundary_state_for_run(session: Session, run_start: date) -> dict:
    """
    {employee_id: {"was_night_before": bool, "consecutive_before": int}} —
    what everyone was doing on the days immediately before run_start, from
    ANY roster run (not month-scoped like _starting_hours_for_month, since
    a night shift on Jan 31 still means Feb 1 must be a rest day). Feeds
    generate_roster()'s boundary_state so the rest-after-night and
    max-consecutive-days rules extend across run boundaries instead of
    silently resetting to "nobody worked yesterday" at the start of every
    single generation.
    """
    lookback_start = run_start - timedelta(days=MAX_CONSECUTIVE_DAYS)
    rows = session.exec(select(Assignment).where(
        Assignment.day >= lookback_start, Assignment.day < run_start,
    ).order_by(Assignment.run_id)).all()  # ascending run_id so the most-recently-generated row wins on any overlap

    by_employee_day = defaultdict(dict)
    for a in rows:
        by_employee_day[a.employee_id][a.day] = a.code

    prior_day = run_start - timedelta(days=1)
    result = {}
    for eid, day_codes in by_employee_day.items():
        was_night_before = day_codes.get(prior_day, "").startswith("N")
        consec = 0
        cursor = prior_day
        for _ in range(MAX_CONSECUTIVE_DAYS):
            code = day_codes.get(cursor)
            if is_work_shift_code(code):
                consec += 1
                cursor -= timedelta(days=1)
            else:
                break
        result[eid] = {"was_night_before": was_night_before, "consecutive_before": consec}
    return result


def _get_standby_settings(session: Session) -> StandbySettings:
    """Singleton (id=1) admin-set standby headcounts. Created lazily with
    all-zero defaults on first access — matches the old per-station
    behavior of "no standby team until someone opts in", just global now."""
    settings = session.get(StandbySettings, 1)
    if not settings:
        settings = StandbySettings(id=1)
        session.add(settings)
        session.flush()
    return settings


def _recompute_standby_group(session: Session, run: RosterRun, group: str, pool: list,
                              need_day: int, need_night: int, leaves_all: dict, days: list):
    """Recomputes one group's ("driver" | "medical" | "call_center") shared
    standby pool from the run's current Assignment rows for that pool —
    same reasoning as the driver/medical recompute already had: staying
    correct regardless of which scope generated last, or in what order,
    since it reads DB state rather than trusting only this pass's in-memory
    assignments. Only replaces this group's own StandbyAssignment rows,
    leaving the other two groups' rows untouched."""
    pool_ids = {e["id"] for e in pool}
    current_rows = session.exec(select(Assignment).where(
        Assignment.run_id == run.id, Assignment.employee_id.in_(pool_ids)
    )).all() if pool_ids else []
    worked_assignments = [{"employee_id": a.employee_id, "day": a.day, "code": a.code} for a in current_rows]
    standby_assignments = assign_standby(pool, need_day, need_night, leaves_all, days, worked_assignments, group)
    for sb in session.exec(select(StandbyAssignment).where(
        StandbyAssignment.run_id == run.id, StandbyAssignment.group == group
    )).all():
        session.delete(sb)
    session.flush()
    for sb in standby_assignments:
        session.add(StandbyAssignment(run_id=run.id, group=group, day=sb["day"],
                                       shift=sb["shift"], employee_id=sb["employee_id"]))


def _regulation_facility_ids(session: Session) -> set:
    """Which facilities are an actual Regulation desk, not just any facility
    that happens to have one call_and_dispatch employee on file — real bug
    found via direct data check: "Administration" has 4 nurses/anesthesiologists
    /critical/drivers and exactly ONE call_and_dispatch person, clearly a
    general office posting, not a call-center desk, but was being treated as
    a second Regulation desk needing its own medical coverage. A facility
    only counts if call_and_dispatch is its DOMINANT role (majority of its
    active staff) — "Call center" (6 of 7 staff) qualifies, "Administration"
    (1 of 7) does not."""
    all_staff = session.exec(select(Employee).where(Employee.active == True, Employee.facility_id.is_not(None))).all()
    by_facility = defaultdict(list)
    for e in all_staff:
        by_facility[e.facility_id].append(e)
    return {
        fid for fid, staff in by_facility.items()
        if sum(1 for e in staff if e.role == "call_and_dispatch") > len(staff) / 2
    }


def _regulation_facilities_and_committed(session: Session, run: RosterRun) -> tuple:
    """Active Regulation-desk facilities (see _regulation_facility_ids) plus
    how many of Call & Dispatch's OWN people are already committed to each
    facility/day/shift in this run's CURRENT persisted schedule — read fresh
    from the DB, same "current state, not memory" philosophy as
    _recompute_standby_group, so it's correct regardless of whether Call &
    Dispatch's own schedule was generated before or after this call. Feeds
    generate_roster()'s regulation-folding so PHTL's medical CP-SAT solve can
    plan Regulation-desk hours jointly with the field schedule instead of
    layering them on afterward — see generate_roster's docstring for why
    that joint solve is the real fix for the combined-hours fairness gap.
    Returns (facilities: list[dict], committed: dict[(facility_id, date, "day"|"night"), int]).
    """
    facility_ids = _regulation_facility_ids(session)
    if not facility_ids:
        return [], {}
    facilities = [f.dict() for f in session.exec(
        select(Facility).where(Facility.id.in_(facility_ids), Facility.active == True)
    ).all()]
    dispatch_staff = session.exec(select(Employee).where(
        Employee.role == "call_and_dispatch", Employee.facility_id.in_(facility_ids)
    )).all()
    dispatch_ids = {e.id for e in dispatch_staff}
    committed = defaultdict(int)
    dispatch_rows = session.exec(select(Assignment).where(
        Assignment.run_id == run.id, Assignment.employee_id.in_(dispatch_ids)
    )).all() if dispatch_ids else []
    for a in dispatch_rows:
        if is_work_shift_code(a.code) and a.facility_id is not None:
            committed[(a.facility_id, a.day, "day" if a.code[0] == "D" else "night")] += 1
    return facilities, committed


def _recompute_regulation_nurses(session: Session, run: RosterRun, nurse_pool: list,
                                  leaves_all: dict, days: list) -> int:
    """PHTL's nurse contribution to the Regulation desk — a pure TOP-UP pass,
    additive only. Regulation is now primarily filled by generate_roster()'s
    own CP-SAT model (folded in jointly with the field schedule — see its
    docstring), which already writes DR/NR rows directly into this run's
    Assignment table for the medical pool (which facility tracked via the
    facility_id column, not the code — DR/NR is the same generic code
    everywhere, matching the real paper roster's legend). This function
    exists only to catch the gap that opens when Call & Dispatch's OWN
    schedule is generated/regenerated AFTER PHTL's medical CP-SAT solve
    already ran (so the CP-SAT solve saw stale or zero dispatch commitment) —
    it must NEVER delete or reconsider anyone the CP-SAT pass already placed
    on Regulation, only fill whatever's still short. Real incident this
    replaces: the old delete-then-reinsert version wiped every CP-SAT-picked
    Regulation assignment and rebuilt it from scratch with a plain greedy
    pass, silently discarding the joint field+Regulation hour optimization
    every time Call & Dispatch's schedule changed.
    Returns how many NEW Assignment rows it wrote (0 in the common case,
    where CP-SAT already met the target)."""
    nurse_ids = {e["id"] for e in nurse_pool}
    regulation_facility_ids = _regulation_facility_ids(session)
    dispatch_staff = session.exec(select(Employee).where(
        Employee.role == "call_and_dispatch", Employee.facility_id.in_(regulation_facility_ids)
    )).all() if regulation_facility_ids else []
    dispatch_ids_by_facility = defaultdict(set)
    for e in dispatch_staff:
        dispatch_ids_by_facility[e.facility_id].add(e.id)

    if not nurse_pool or not dispatch_ids_by_facility:
        return 0

    nurse_rows = session.exec(select(Assignment).where(
        Assignment.run_id == run.id, Assignment.employee_id.in_(nurse_ids)
    )).all()
    # Existing row per (employee_id, day) — every nurse already has an "O" (or
    # leave/work, including any CP-SAT-picked Regulation shift) row here from
    # the normal medical generation pass, since every employee gets an
    # Assignment row every day. A NEW pick has to UPDATE that row, never
    # INSERT a second one, or it collides with the
    # uq_assignment_run_employee_day constraint. worked_assignments doubles as
    # the eligibility map assign_regulation_nurses reads (which already skips
    # anyone with an existing work code, so CP-SAT's own picks are correctly
    # left untouched and never double-booked) AND gets extended with each
    # facility's own new picks before the next facility runs.
    existing_row_by_key = {(a.employee_id, a.day): a for a in nurse_rows}
    worked_assignments = [{"employee_id": a.employee_id, "day": a.day, "code": a.code} for a in nurse_rows]

    total = 0
    for facility_id, dispatch_ids in dispatch_ids_by_facility.items():
        facility = session.get(Facility, facility_id)
        if not facility or not facility.active:
            continue
        dispatch_rows = session.exec(select(Assignment).where(
            Assignment.run_id == run.id, Assignment.employee_id.in_(dispatch_ids)
        )).all()
        committed = defaultdict(int)
        for a in dispatch_rows:
            if is_work_shift_code(a.code):
                committed[(a.day, "day" if a.code[0] == "D" else "night")] += 1
        # Whatever the nurse pool ALREADY covers for this facility (almost
        # always from CP-SAT) counts toward the target too, so this pass only
        # asks for the remaining gap — never re-fills what's already there.
        # Matched via Assignment.facility_id, not the code string — DR/NR is
        # the same generic code at every facility (matching the real paper
        # roster's legend), so which facility a pick belongs to only ever
        # lives in the facility_id column.
        for a in nurse_rows:
            if is_work_shift_code(a.code) and a.facility_id == facility_id:
                committed[(a.day, "day" if a.code[0] == "D" else "night")] += 1
        reg_assignments = assign_regulation_nurses(nurse_pool, facility.dict(), committed,
                                                     leaves_all, days, worked_assignments)
        for a in reg_assignments:
            existing = existing_row_by_key.get((a["employee_id"], a["day"]))
            if existing:
                existing.code = a["code"]
                existing.facility_id = a["facility_id"]
                existing.station_id = None
                session.add(existing)
            else:
                row = Assignment(run_id=run.id, employee_id=a["employee_id"], day=a["day"],
                                  code=a["code"], facility_id=a["facility_id"])
                session.add(row)
                existing_row_by_key[(a["employee_id"], a["day"])] = row
            worked_assignments.append(a)
        total += len(reg_assignments)
    return total


def _run_solver_and_persist(session: Session, run: RosterRun, scope: str = "all") -> dict:
    """Runs the scheduler for run's date range and writes Assignment/CrewAssignment/
    CrewMember/StandbyAssignment rows against the given (already-persisted) run.
    Shared by first-time generation, regeneration, and now partial-scope
    generation (Drivers Manager generates "drivers", PHTL generates "medical",
    admin generates "all" — see the role-gated endpoints below). A partial
    scope MERGES into whatever the other scope already produced for this run
    instead of overwriting it: CrewAssignment rows are looked up by
    (ambulance, day, shift) first, and only the fields belonging to the
    current scope are touched. Each of the three global standby pools
    (driver/medical/call_center — see StandbySettings) is recomputed from
    the run's current Assignment rows for that pool whenever its own scope
    runs, so it stays correct regardless of generation order without
    needing to see the other scopes' in-memory results."""
    start_date, end_date = run.start_date, run.end_date
    leave_rows_all = session.exec(select(Leave).where(Leave.day >= start_date, Leave.day <= end_date)).all()
    leaves_all = {(l.employee_id, l.day): l.code for l in leave_rows_all}
    starting_hours_all = _starting_hours_for_month(session, start_date, exclude_run_id=run.id)
    boundary_state_all = _boundary_state_for_run(session, start_date)
    standby_settings = _get_standby_settings(session)
    dispatch_assignments, dispatch_gaps = [], []

    # Call Center's "dispatch" scope is a completely separate, much simpler
    # problem — one facility's fixed staff rotating a day/night desk, no
    # ambulances/stations/crews involved — so it bypasses generate_roster()'s
    # CP-SAT machinery entirely and uses the same greedy round-robin as
    # schedule_drivers(), scoped to whichever facility the call_and_dispatch
    # role's employees are actually posted to. Runs standalone for
    # scope="dispatch" (Call Center's own generate), and also bundled into
    # scope="all" (admin generating everything in one pass).
    if scope in ("dispatch", "all"):
        staff = [e.dict() for e in session.exec(
            select(Employee).where(Employee.active == True, Employee.role == "call_and_dispatch")
        ).all()]
        by_facility = defaultdict(list)
        for e in staff:
            if e["facility_id"] is not None:
                by_facility[e["facility_id"]].append(e)
        days = date_range(start_date, end_date)
        # Call & Dispatch's OWN independent target (2 per shift, confirmed
        # directly) applies only at the real Regulation desk — everywhere
        # else keeps the old "aim for the facility's own required_headcount"
        # behavior, since there's no medical pool filling a gap there.
        regulation_facility_ids = _regulation_facility_ids(session)
        for facility_id, group in by_facility.items():
            facility = session.get(Facility, facility_id)
            if not facility or not facility.active:
                continue
            own_target = DISPATCH_OWN_REGULATION_TARGET if facility_id in regulation_facility_ids else None
            assignments, gaps, _, _ = schedule_facility_staff(
                group, facility.dict(), leaves_all, days, starting_hours_all, boundary_state_all,
                own_target=own_target,
            )
            dispatch_assignments.extend(assignments)
            dispatch_gaps.extend(gaps)

        existing_notes = json.loads(run.notes) if run.notes else {}
        existing_notes["dispatch_coverage_gaps"] = [
            {"facility_id": g["facility_id"], "day": g["day"].isoformat(),
             "shift": g["shift"], "needed": g["needed"], "filled": g["filled"]} for g in dispatch_gaps
        ]
        scopes_done = set(existing_notes.get("scopes_generated", []))
        scopes_done.add("dispatch")
        existing_notes["scopes_generated"] = sorted(scopes_done)
        run.notes = json.dumps(existing_notes)
        session.add(run)
        for a in dispatch_assignments:
            session.add(Assignment(run_id=run.id, employee_id=a["employee_id"], day=a["day"],
                                    code=a["code"], facility_id=a.get("facility_id")))
        session.flush()

        _recompute_standby_group(session, run, "call_center", staff,
                                  standby_settings.call_center_day, standby_settings.call_center_night,
                                  leaves_all, days)

        if scope == "dispatch":
            if not staff:
                raise HTTPException(400, "No active call_and_dispatch employees on file yet.")
            # Regulation's medical contribution is critical/anesthesiologist
            # ONLY — confirmed directly, correcting an earlier "open to the
            # whole medical pool" decision.
            nurse_pool = [e.dict() for e in session.exec(
                select(Employee).where(Employee.active == True, Employee.facility_id == None, Employee.is_driver == False,
                                        Employee.role.in_(("critical", "anesthesiologist")))
            ).all()]
            regulation_assignment_count = _recompute_regulation_nurses(session, run, nurse_pool, leaves_all, days)
            session.commit()
            return {"run_id": run.id, "status": "FEASIBLE",
                    "assignment_count": len(dispatch_assignments) + regulation_assignment_count,
                    "driver_coverage_gaps": 0, "dispatch_coverage_gaps": len(dispatch_gaps),
                    "quality": None, "scope": scope, "regulation_assignment_count": regulation_assignment_count}
        # scope == "all": fall through into the drivers+medical generation below,
        # then add the dispatch counts into that final response.

    # Facility-posted staff (Logistics, Head Office, ...) are a fixed, standing
    # assignment — not part of the ambulance day/night rotation at all — so
    # they're excluded from the scheduler's pool up front.
    employees = [e.dict() for e in session.exec(
        select(Employee).where(Employee.active == True, Employee.facility_id == None)
    ).all()]
    stations = [s.dict() for s in session.exec(select(Station).where(Station.active == True)).all()]
    ambulances = [a.dict() for a in session.exec(select(Ambulance)).all()]
    if not employees:
        raise HTTPException(400, "No employees on file yet — add employees first.")
    if not stations:
        raise HTTPException(400, "No stations on file yet — add stations first.")
    if not ambulances:
        raise HTTPException(400, "No ambulances on file yet — add at least one ambulance first.")

    regulation_facilities, call_center_committed = (
        _regulation_facilities_and_committed(session, run) if scope in ("all", "medical") else ([], {})
    )
    status, assignments, gaps, crew_assignments, quality, standby_assignments = generate_roster(
        employees, stations, ambulances, leaves_all, start_date, end_date,
        starting_hours=starting_hours_all, boundary_state=boundary_state_all, scope=scope,
        middle_site_codes=MIDDLE_SITE_CODES,
        regulation_facilities=regulation_facilities, call_center_committed=call_center_committed,
    )

    # Understaffing never blocks a roster from being generated — it's surfaced
    # as explicit flags (medical_coverage_gaps, overwork_flags, driver gaps)
    # instead. This branch only fires for a genuine solver failure (no
    # feasible solution found at all, which the soft-constraint design below
    # makes very unlikely), and even then the run stays in place so nothing
    # is silently lost — just flagged clearly as empty.
    if status not in ("OPTIMAL", "FEASIBLE"):
        # Merge rather than overwrite — a prior/bundled scope (e.g. this
        # same "all" call's dispatch pass, already flushed above) may have
        # already written real data into this run's notes.
        failure_notes = json.loads(run.notes) if run.notes else {}
        failure_notes["solver_status"] = status
        failure_notes["warning"] = (f"The solver could not produce any assignments (status: {status}). "
                                     f"This roster is empty — investigate station/employee data.")
        run.notes = json.dumps(failure_notes)
        session.add(run)
        session.commit()
        return {"run_id": run.id, "status": status, "assignment_count": len(dispatch_assignments),
                "driver_coverage_gaps": 0, "dispatch_coverage_gaps": len(dispatch_gaps), "quality": None,
                "warning": "No assignments could be made — see notes on the run."}

    existing_notes = json.loads(run.notes) if run.notes else {}
    if scope in ("all", "drivers"):
        existing_notes["driver_coverage_gaps"] = [
            {"station_id": g["station_id"], "ambulance_id": g["ambulance_id"], "day": g["day"].isoformat(),
             "shift": g["shift"], "needed": g["needed"], "filled": g["filled"]} for g in gaps
        ]
        existing_notes["driver_quality"] = quality
    if scope in ("all", "medical"):
        existing_notes["medical_quality"] = quality
    existing_notes["quality"] = quality  # latest pass — the live "just generated" display uses the API response directly, this is for re-loading the run later
    scopes_done = set(existing_notes.get("scopes_generated", []))
    scopes_done.update(("drivers", "medical") if scope == "all" else (scope,))
    existing_notes["scopes_generated"] = sorted(scopes_done)
    run.notes = json.dumps(existing_notes)
    session.add(run)

    for a in assignments:
        session.add(Assignment(run_id=run.id, employee_id=a["employee_id"], day=a["day"],
                                code=a["code"], station_id=a["station_id"], facility_id=a.get("facility_id")))

    for c in crew_assignments:
        existing_crew = session.exec(select(CrewAssignment).where(
            CrewAssignment.run_id == run.id, CrewAssignment.ambulance_id == c["ambulance_id"],
            CrewAssignment.day == c["day"], CrewAssignment.shift == c["shift"]
        )).first()
        if existing_crew:
            crew = existing_crew
            if scope in ("all", "drivers"):
                crew.driver_employee_id = c["driver_employee_id"]
                session.add(crew)
        else:
            crew = CrewAssignment(run_id=run.id, ambulance_id=c["ambulance_id"], day=c["day"], shift=c["shift"],
                                   driver_employee_id=c["driver_employee_id"] if scope in ("all", "drivers") else None)
            session.add(crew)
            session.flush()  # need crew.id before adding members
        if scope in ("all", "medical"):
            for eid in c["member_employee_ids"]:
                session.add(CrewMember(crew_assignment_id=crew.id, employee_id=eid, is_leader=False))

    # Driver and medical standby are two independent global pools now (see
    # StandbySettings) — each only needs to see its own group's current
    # Assignment rows, recomputed fresh so it stays correct regardless of
    # which scope ran, or in what order.
    session.flush()
    days = date_range(start_date, end_date)
    if scope in ("all", "drivers"):
        driver_pool = [e for e in employees if e["is_driver"]]
        _recompute_standby_group(session, run, "driver", driver_pool,
                                  standby_settings.driver_day, standby_settings.driver_night, leaves_all, days)
    if scope in ("all", "medical"):
        medical_pool = [e for e in employees if not e["is_driver"]]
        _recompute_standby_group(session, run, "medical", medical_pool,
                                  standby_settings.medical_day, standby_settings.medical_night, leaves_all, days)
    standby_count = session.exec(select(StandbyAssignment).where(StandbyAssignment.run_id == run.id)).all()

    # Regulation desk nurse gap-fill: reads Call & Dispatch's and the nurse
    # pool's CURRENT persisted state from the DB (like the standby recompute
    # above), not just what this particular call happened to compute in
    # memory — so it stays correct regardless of whether medical or dispatch
    # ran first, or in a separate call entirely, and a later medical-only
    # regenerate correctly refreshes it instead of silently wiping it.
    regulation_assignment_count = 0
    if scope in ("all", "medical", "dispatch"):
        nurse_pool = [e for e in employees if not e["is_driver"]]
        # Regulation's medical contribution is critical/anesthesiologist
        # ONLY — confirmed directly, correcting an earlier "open to the
        # whole medical pool" decision. rebalance_medical_hours below still
        # uses the full nurse_pool — hour-fairness spans the WHOLE medical
        # team regardless of who can staff Regulation specifically.
        regulation_eligible_pool = [e for e in nurse_pool if e["role"] in ("critical", "anesthesiologist")]
        regulation_assignment_count = _recompute_regulation_nurses(
            session, run, regulation_eligible_pool, leaves_all, days
        )

        # Final hours-based cleanup across the WHOLE medical team (field crew
        # + Regulation together) — confirmed directly: the CP-SAT medical
        # pass and the Regulation gap-fill above are two separate
        # optimizations that don't perfectly compose into one globally-tight
        # spread on their own. Reads current DB state fresh (same reasoning
        # as the standby/Regulation recomputes above) so it's correct
        # regardless of call order, and only runs once both halves it needs
        # actually exist.
        medical_ids = {e["id"] for e in nurse_pool}
        if medical_ids:
            medical_rows = session.exec(select(Assignment).where(
                Assignment.run_id == run.id, Assignment.employee_id.in_(medical_ids)
            )).all()
            row_dicts = [{"employee_id": a.employee_id, "day": a.day, "code": a.code,
                          "station_id": a.station_id, "facility_id": a.facility_id} for a in medical_rows]
            row_by_key = {(a.employee_id, a.day): a for a in medical_rows}
            mix_exempt = {e["id"] for e in nurse_pool if e.get("night_only") or e.get("restricted_night_shift")}
            regulation_eligible_ids = {e["id"] for e in nurse_pool if e["role"] in ("critical", "anesthesiologist")}
            rebalance_medical_hours(row_dicts, days, mix_exempt=mix_exempt,
                                     regulation_eligible_ids=regulation_eligible_ids)
            for d in row_dicts:
                orm_row = row_by_key[(d["employee_id"], d["day"])]
                if orm_row.code != d["code"]:
                    orm_row.code = d["code"]
                    orm_row.station_id = d["station_id"]
                    orm_row.facility_id = d["facility_id"]
                    session.add(orm_row)

    session.commit()

    return {"run_id": run.id, "status": status,
            "assignment_count": len(assignments) + len(dispatch_assignments) + regulation_assignment_count,
            "driver_coverage_gaps": len(gaps), "dispatch_coverage_gaps": len(dispatch_gaps),
            "quality": quality, "scope": scope, "standby_count": len(standby_count),
            "regulation_assignment_count": regulation_assignment_count}


def _clear_run_generated_data(session: Session, run_id: int, scope: str = "all"):
    """
    Deletes generated rows for a run so it can be regenerated or deleted.
    scope="all" (the default — used for a full delete, or an admin's full
    regenerate) wipes everything, same as before. scope="drivers", "medical",
    or "dispatch" only clears that third, since Drivers Manager, PHTL, and
    Call Center now each generate independently into the SAME run — one
    role's regenerate must not blow away another's work.
    """
    driver_ids = {e.id for e in session.exec(select(Employee).where(Employee.is_driver == True)).all()}
    dispatch_ids = {e.id for e in session.exec(select(Employee).where(Employee.role == "call_and_dispatch")).all()}

    if scope == "all":
        crew_ids = session.exec(select(CrewAssignment.id).where(CrewAssignment.run_id == run_id)).all()
        if crew_ids:
            for m in session.exec(select(CrewMember).where(CrewMember.crew_assignment_id.in_(crew_ids))).all():
                session.delete(m)
            # OperationalException.crew_assignment_id is a real FK — deleting
            # the crew assignment out from under it would violate it (same
            # issue as run_id, detached separately in delete_roster). The
            # exception itself is audit trail and just loses this pointer.
            for exc in session.exec(select(OperationalException).where(
                OperationalException.crew_assignment_id.in_(crew_ids)
            )).all():
                exc.crew_assignment_id = None
                session.add(exc)
            session.flush()
        for c in session.exec(select(CrewAssignment).where(CrewAssignment.run_id == run_id)).all():
            session.delete(c)
        for a in session.exec(select(Assignment).where(Assignment.run_id == run_id)).all():
            session.delete(a)
        for sb in session.exec(select(StandbyAssignment).where(StandbyAssignment.run_id == run_id)).all():
            session.delete(sb)
    elif scope == "drivers":
        for a in session.exec(select(Assignment).where(
            Assignment.run_id == run_id, Assignment.employee_id.in_(driver_ids)
        )).all():
            session.delete(a)
        for c in session.exec(select(CrewAssignment).where(CrewAssignment.run_id == run_id)).all():
            if c.driver_employee_id is not None:
                c.driver_employee_id = None
                session.add(c)
    elif scope == "medical":
        crew_ids = session.exec(select(CrewAssignment.id).where(CrewAssignment.run_id == run_id)).all()
        if crew_ids:
            for m in session.exec(select(CrewMember).where(CrewMember.crew_assignment_id.in_(crew_ids))).all():
                session.delete(m)
            session.flush()
        excluded_ids = driver_ids | dispatch_ids
        for a in session.exec(select(Assignment).where(
            Assignment.run_id == run_id, Assignment.employee_id.not_in(excluded_ids)
        )).all():
            session.delete(a)
    elif scope == "dispatch":
        for a in session.exec(select(Assignment).where(
            Assignment.run_id == run_id, Assignment.employee_id.in_(dispatch_ids)
        )).all():
            session.delete(a)
    session.flush()


@app.get("/api/roster")
def list_rosters(session: Session = Depends(get_session), _=Depends(any_role)):
    """All roster runs, newest first — powers the date-range picker everywhere
    a specific roster needs to be chosen, instead of typing a raw run ID."""
    runs = session.exec(select(RosterRun).order_by(RosterRun.id.desc())).all()
    return runs


def _scope_for_actor(actor: User) -> str:
    """Drivers Manager only ever generates/touches the driver half of a roster;
    PHTL only the medical half; Call Center only the dispatch-desk rotation for
    their own call_and_dispatch staff; admin generates the full thing (drivers
    + medical — dispatch is scoped separately even for admin, since it's a
    different facility-based rotation, not part of generate_roster() at all)."""
    if actor.role == "drivers_manager":
        return "drivers"
    if actor.role == "phtl":
        return "medical"
    if actor.role == "call_center_tl":
        return "dispatch"
    return "all"


def _start_generation_job(run_id: int, scope: str, actor: User, action: str, summary: str, clear_first: bool) -> dict:
    """Queues the (slow) clear + solve + persist for a run on a background
    thread and returns at once with a job id the page can poll. Runs in its
    own session — the request's session is closed as soon as this returns."""
    if jobs.active_job_for_run(run_id):
        raise HTTPException(409, "A generation is already running for this roster — wait for it to finish.")
    actor_id = actor.id

    def work() -> dict:
        with Session(engine) as s:
            run = s.get(RosterRun, run_id)
            if not run:
                raise HTTPException(404, "Roster run not found")
            if clear_first:
                _clear_run_generated_data(s, run_id, scope=scope)
            result = _run_solver_and_persist(s, run, scope=scope)
            log_activity(s, s.get(User, actor_id), action, "roster_run", run_id, summary)
            s.commit()
            return result

    job = jobs.start_job(run_id, f"{action} #{run_id} ({scope})", work)
    return {"job_id": job["job_id"], "run_id": run_id, "scope": scope, "status": job["status"]}


@app.post("/api/roster/generate")
def generate(start_date: date, end_date: date, range_type: str = "custom",
             session: Session = Depends(get_session),
             actor: User = Depends(require_role("admin", "drivers_manager", "phtl", "call_center_tl"))):
    if end_date < start_date:
        raise HTTPException(400, "end_date can't be before start_date.")
    if (end_date - start_date).days > 366:
        raise HTTPException(400, "That range is over a year — check the dates.")

    # An EXACT date-range match against an existing draft run is the normal
    # case of a second role joining the same roster — e.g. Drivers Manager
    # generating for Aug 17-23 after PHTL already created that run's medical
    # half. That's not a conflict, it's exactly how the 3-way split is
    # supposed to work, so route this actor's scope into the existing run
    # instead of creating a duplicate. A PARTIAL overlap against a
    # *different* date range is a genuine conflict, though: two runs
    # covering overlapping-but-not-identical dates would each separately
    # compute assignments for the shared days, double-counting hours in
    # _starting_hours_for_month and risking two contradictory published
    # schedules for the same person on the same day — that stays a hard
    # block, since it's a data-integrity error, not a staffing tradeoff.
    exact_match = session.exec(select(RosterRun).where(
        RosterRun.start_date == start_date, RosterRun.end_date == end_date,
    )).first()
    if exact_match:
        if exact_match.status != "draft":
            raise HTTPException(409, f"Roster run #{exact_match.id} for this exact date range already exists "
                                      f"and is {exact_match.status} — it's locked. Ask an admin if it needs "
                                      f"to be reopened before more scopes can be generated into it.")
        scope = _scope_for_actor(actor)
        return _start_generation_job(exact_match.id, scope, actor, "roster.generate",
                                     f"Generated into existing roster {exact_match.start_date.isoformat()} → "
                                     f"{exact_match.end_date.isoformat()} (scope: {scope}).", clear_first=True)

    overlapping = session.exec(select(RosterRun).where(
        RosterRun.start_date <= end_date, RosterRun.end_date >= start_date,
    )).all()
    if overlapping:
        conflicts = ", ".join(f"#{r.id} ({r.start_date.isoformat()} → {r.end_date.isoformat()}, {r.status})"
                               for r in overlapping)
        raise HTTPException(409, f"This date range partially overlaps an existing roster run: {conflicts}. "
                                  f"Pick a range that either matches it exactly (to add your scope to it) "
                                  f"or doesn't touch it at all.")

    scope = _scope_for_actor(actor)
    run = RosterRun(start_date=start_date, end_date=end_date, range_type=range_type,
                     created_at=date.today(), status="draft")
    session.add(run)
    session.commit()
    session.refresh(run)

    return _start_generation_job(run.id, scope, actor, "roster.generate",
                                 f"Generated roster {run.start_date.isoformat()} → {run.end_date.isoformat()} "
                                 f"(scope: {scope}).", clear_first=False)


@app.post("/api/roster/{run_id}/regenerate")
def regenerate(run_id: int, session: Session = Depends(get_session),
                actor: User = Depends(require_role("admin", "drivers_manager", "phtl", "call_center_tl"))):
    """Re-runs the solver for the same run's date range, replacing its generated
    data in place — the roster picker keeps pointing at the same run afterward.
    Drivers Manager/PHTL/Call Center only regenerate their own third
    (drivers/medical/dispatch); the other roles' already-generated data on
    this run is left untouched."""
    run = session.get(RosterRun, run_id)
    if not run:
        raise HTTPException(404, "Roster run not found")
    if run.status != "draft":
        raise HTTPException(409, f"Only a draft roster can be regenerated (current status: {run.status}). "
                                  f"Submitted/published rosters are locked to protect what's already been reviewed.")
    scope = _scope_for_actor(actor)
    return _start_generation_job(run.id, scope, actor, "roster.regenerate",
                                 f"Regenerated roster {run.start_date.isoformat()} → {run.end_date.isoformat()} "
                                 f"(scope: {scope}).", clear_first=True)


@app.get("/api/roster/jobs/{job_id}")
def get_generation_job(job_id: str, _: User = Depends(any_role)):
    """Poll target for a generation started by /generate or /regenerate."""
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found (the server may have restarted — check the roster list).")
    return job


SCOPE_ROLE_LABELS = {"drivers": "Drivers Manager", "medical": "PHTL", "dispatch": "Call Center"}


@app.delete("/api/roster/{run_id}")
def delete_roster(run_id: int, session: Session = Depends(get_session), actor: User = Depends(any_role)):
    """Deletes one roster run (and everything generated under it). A draft
    only one role has contributed to can be freely deleted by that role —
    that's routine cleanup. The moment a SECOND role's scope has also been
    generated into it, deleting the whole thing stops being one person's
    call — someone else's real work is now on the line, so it needs admin.
    Admin additionally has the authority to remove a submitted or published
    roster outright (e.g. it was generated against bad data and needs to
    go, not just be regenerated); every other role stays draft-only so a
    roster that's already been reviewed can't be silently pulled out from
    under them."""
    run = session.get(RosterRun, run_id)
    if not run:
        raise HTTPException(404, "Roster run not found")
    if run.status != "draft" and actor.role != "admin":
        raise HTTPException(409, f"Only a draft roster can be deleted (current status: {run.status}). "
                                  f"A roster that's been submitted or published can't be silently removed — "
                                  f"ask an admin if it needs to go.")
    notes = json.loads(run.notes) if run.notes else {}
    scopes_generated = notes.get("scopes_generated", [])
    if len(scopes_generated) > 1 and actor.role != "admin":
        contributors = ", ".join(SCOPE_ROLE_LABELS.get(s, s) for s in scopes_generated)
        raise HTTPException(409, f"More than one role has already contributed to this roster ({contributors}) — "
                                  f"deleting the whole thing would wipe their work too. Ask an admin to delete it, "
                                  f"or use Regenerate to only clear your own scope.")
    _clear_run_generated_data(session, run_id)
    # OperationalException.run_id is a real FK — deleting the run out from
    # under an exception row would violate it. The exception itself is kept
    # (it's the audit trail of a real command decision) and just detached.
    for exc in session.exec(select(OperationalException).where(OperationalException.run_id == run_id)).all():
        exc.run_id = None
        session.add(exc)
    log_activity(session, actor, "roster.delete", "roster_run", run.id,
                 f"Deleted roster {run.start_date.isoformat()} → {run.end_date.isoformat()}.")
    session.delete(run)
    session.commit()
    return {"ok": True}


def _employee_in_actor_lane(emp: Employee, actor_role: str) -> bool:
    """Same lane boundary as generation/override: Drivers Manager only
    manages drivers, PHTL only medical crew, Call Center only their C&D
    staff. Admin (and anyone outside these three) sees everyone."""
    if actor_role == "drivers_manager":
        return emp.is_driver
    if actor_role == "phtl":
        return not emp.is_driver and emp.role != "call_and_dispatch" and emp.facility_id is None
    if actor_role == "call_center_tl":
        return emp.role == "call_and_dispatch"
    return True


@app.get("/api/roster/{run_id}")
def get_roster(run_id: int, session: Session = Depends(get_session), actor: User = Depends(any_role)):
    run = session.get(RosterRun, run_id)
    if not run:
        raise HTTPException(404, "Roster run not found")
    assignments = session.exec(select(Assignment).where(Assignment.run_id == run_id)).all()
    employees = {e.id: e for e in session.exec(select(Employee)).all()}

    grid = {}
    for a in assignments:
        grid.setdefault(a.employee_id, {})[a.day.isoformat()] = a.code

    result = []
    for emp_id, days_dict in grid.items():
        emp = employees.get(emp_id)
        if emp and _employee_in_actor_lane(emp, actor.role):
            total_hours = sum(_shift_hours(code) for code in days_dict.values())
            result.append({"employee_id": emp_id, "name": emp.name, "role": emp.role, "days": days_dict,
                            "total_hours": total_hours})
    result.sort(key=lambda r: r["name"])
    return {"run": run, "rows": result}


@app.put("/api/roster/{run_id}/assignment")
def edit_assignment(run_id: int, employee_id: int, day: date, code: str,
                     session: Session = Depends(get_session), actor: User = Depends(any_role)):
    row = session.exec(select(Assignment).where(
        Assignment.run_id == run_id, Assignment.employee_id == employee_id, Assignment.day == day
    )).first()
    if not row:
        raise HTTPException(404, "Assignment not found")
    emp = session.get(Employee, employee_id)
    if emp and not _employee_in_actor_lane(emp, actor.role):
        raise HTTPException(403, f"Role '{actor.role}' can't edit this employee's assignment.")
    if code != "O" and code not in LEAVE_CODES and not is_work_shift_code(code):
        raise HTTPException(400, f"'{code}' isn't a recognized roster code. Use 'O' (off), a leave code "
                                  f"({', '.join(LEAVE_CODES)}), or a real shift code (e.g. D3, N12).")
    row.code = code
    row.manually_edited = True
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@app.post("/api/roster/{run_id}/submit")
def submit_roster(run_id: int, session: Session = Depends(get_session), user=Depends(any_role)):
    run = session.get(RosterRun, run_id)
    if not run:
        raise HTTPException(404, "Roster run not found")
    if run.status != "draft":
        raise HTTPException(409, f"Only a draft roster can be submitted (current status: {run.status}).")

    # A run is now built in up to three independent passes (Drivers Manager,
    # PHTL, Call Center). Submitting before all three that actually apply
    # have run would lock the others out (regenerate requires status=draft)
    # before they ever got a turn. This warns rather than blocks — a site
    # genuinely without, say, any call_and_dispatch staff has nothing to
    # generate for "dispatch" and that's not an error — so whoever submits
    # sees it and can decide.
    notes = json.loads(run.notes) if run.notes else {}
    scopes_done = set(notes.get("scopes_generated", []))
    expected = set()
    if session.exec(select(Employee).where(Employee.is_driver == True, Employee.active == True)).first():
        expected.add("drivers")
    if session.exec(select(Employee).where(Employee.is_driver == False, Employee.active == True,
                                            Employee.facility_id == None)).first():
        expected.add("medical")
    if session.exec(select(Employee).where(Employee.role == "call_and_dispatch", Employee.active == True)).first():
        expected.add("dispatch")
    missing = sorted(expected - scopes_done)

    run.status = "submitted"
    run.submitted_by = user.id
    run.submitted_at = datetime.utcnow()
    session.add(run)
    log_activity(session, user, "roster.submit", "roster_run", run.id,
                 f"Submitted roster {run.start_date.isoformat()} → {run.end_date.isoformat()} for review."
                 + (f" (missing: {', '.join(missing)})" if missing else ""))
    session.commit()
    session.refresh(run)
    warning = None
    if missing:
        labels = {"drivers": "drivers", "medical": "medical crew", "dispatch": "dispatch desk"}
        warning = (f"Submitted, but {', '.join(labels[m] for m in missing)} "
                   f"{'was' if len(missing) == 1 else 'were'} never generated for this run — "
                   f"the responsible manager may not have had a turn yet.")
    return {"run": run, "missing_scopes": missing, "warning": warning}


@app.post("/api/roster/{run_id}/reopen")
def reopen_roster(run_id: int, session: Session = Depends(get_session), user: User = Depends(require_role("admin"))):
    """Moves a submitted run back to draft — e.g. it was submitted before
    every applicable scope (drivers/medical/dispatch) had actually been
    generated, and the missing role is now locked out of contributing
    (generate/regenerate both require status=draft). Deliberately admin-only,
    and deliberately NOT available for a published run — publishing is the
    real, final lock; submission is just "ready for review" and needs to
    stay reversible or a run can end up permanently missing a whole scope."""
    run = session.get(RosterRun, run_id)
    if not run:
        raise HTTPException(404, "Roster run not found")
    if run.status != "submitted":
        raise HTTPException(409, f"Only a submitted roster can be reopened (current status: {run.status}). "
                                  f"A published roster can't be reopened this way.")
    run.status = "draft"
    run.submitted_by = None
    run.submitted_at = None
    session.add(run)
    log_activity(session, user, "roster.reopen", "roster_run", run.id,
                 f"Reopened roster {run.start_date.isoformat()} → {run.end_date.isoformat()} back to draft.")
    session.commit()
    session.refresh(run)
    return run


@app.get("/api/roster/{run_id}/validate")
def validate_roster(run_id: int, session: Session = Depends(get_session), _=Depends(any_role)):
    """Checks anyone can run before submitting/publishing (or just out of
    caution after a manual override): flags double-bookings, rest-rule
    violations, and Pro-coverage gaps across the whole run — including
    anything manual overrides introduced."""
    run = session.get(RosterRun, run_id)
    if not run:
        raise HTTPException(404, "Roster run not found")

    rows = session.exec(select(Assignment).where(Assignment.run_id == run_id)).all()
    by_employee_day = defaultdict(dict)
    for a in rows:
        by_employee_day[a.employee_id].setdefault(a.day, []).append(a.code)

    # Merge in the single day immediately before this run started, from ANY
    # other run, so a rest-rule violation straddling the run boundary is
    # still caught here even though it lives outside run_id's own rows.
    prior_day = run.start_date - timedelta(days=1)
    prior_rows = session.exec(select(Assignment).where(
        Assignment.day == prior_day, Assignment.run_id != run_id
    )).all()
    for a in prior_rows:
        by_employee_day[a.employee_id].setdefault(a.day, []).append(a.code)

    issues = []
    employees = {e.id: e for e in session.exec(select(Employee)).all()}
    for employee_id, day_codes in by_employee_day.items():
        emp_name = employees[employee_id].name if employee_id in employees else f"#{employee_id}"
        for day, codes in sorted(day_codes.items()):
            if len(codes) > 1:
                issues.append({"type": "double_booked", "employee": emp_name, "day": day.isoformat(), "codes": codes})
        sorted_days = sorted(day_codes)
        for i, day in enumerate(sorted_days[:-1]):
            codes = day_codes[day]
            if any(c.startswith("N") for c in codes):
                next_day = sorted_days[i + 1]
                if next_day == day + timedelta(days=1):
                    next_codes = day_codes[next_day]
                    if any(c[0] in ("D", "N") for c in next_codes):
                        issues.append({"type": "rest_violation", "employee": emp_name, "employee_id": employee_id,
                                       "day": next_day.isoformat(),
                                       "detail": "Scheduled the day right after a night shift."})

    notes = json.loads(run.notes) if run.notes else {}
    pro_gaps = notes.get("quality", {}).get("pro_coverage_gaps", [])
    for g in pro_gaps:
        issues.append({"type": "no_pro_on_crew", **g})
    driver_gaps = notes.get("driver_coverage_gaps", [])
    for g in driver_gaps:
        issues.append({"type": "no_driver_assigned", **g})

    # Structural checks a manual override can introduce that the generator
    # itself would never produce: an inactive employee left with a work
    # shift, a restricted-night employee on an N shift, a real leave day
    # that got overridden with a work code anyway, or a crew riding on an
    # ambulance that's since gone to maintenance/out of service.
    leave_rows_this_run = session.exec(
        select(Leave).where(Leave.day >= run.start_date, Leave.day <= run.end_date)
    ).all()
    leave_by_employee_day = {(l.employee_id, l.day): l.code for l in leave_rows_this_run}

    for a in rows:
        emp = employees.get(a.employee_id)
        if not is_work_shift_code(a.code):
            continue
        if emp and not emp.active:
            issues.append({"type": "inactive_employee_scheduled", "employee": emp.name,
                            "day": a.day.isoformat(), "detail": "This employee is marked inactive."})
        if emp and emp.restricted_night_shift and a.code[0] == "N":
            issues.append({"type": "restricted_night_violation", "employee": emp.name,
                            "day": a.day.isoformat(), "detail": "This employee isn't cleared for night shifts."})
        if (a.employee_id, a.day) in leave_by_employee_day:
            issues.append({"type": "leave_conflict", "employee": emp.name if emp else f"#{a.employee_id}",
                            "employee_id": a.employee_id, "day": a.day.isoformat(),
                            "detail": f"Scheduled to work despite approved "
                                      f"{LEAVE_CODES.get(leave_by_employee_day[(a.employee_id, a.day)], 'leave')}."})

    crews = session.exec(select(CrewAssignment).where(CrewAssignment.run_id == run_id)).all()
    ambulances = {amb.id: amb for amb in session.exec(select(Ambulance)).all()}
    for c in crews:
        amb = ambulances.get(c.ambulance_id)
        if amb and amb.status != "active":
            issues.append({"type": "inactive_ambulance_assigned", "ambulance": amb.plate_no,
                            "day": c.day.isoformat(), "shift": c.shift,
                            "detail": f"This ambulance's status is '{amb.status}'."})

    # A gap a leader deliberately approved (see operational_exceptions.py)
    # isn't a bug to fix before publish — it's already been signed off. Tag
    # matching issues instead of counting them as blockers. Matching is keyed
    # by rule_broken, not just day/ambulance/employee — two different issues
    # on the same ambulance/day (or employee/day) must not cross-match just
    # because they share a location, only because the SAME rule is covered:
    #   no_pro_on_crew      <- crew_assignment_id + rule_broken == "pro_coverage"
    #   no_driver_assigned  <- crew_assignment_id + rule_broken == "no_driver"
    #   rest_violation      <- (employee_id, day) + rule_broken == "rest_after_night"
    #   leave_conflict      <- (employee_id, day) + rule_broken == "leave_override"
    active_exceptions = session.exec(select(OperationalException).where(
        OperationalException.run_id == run_id, OperationalException.status == "active",
    )).all()
    crew_id_by_amb_day_shift = {(c.ambulance_id, c.day.isoformat(), c.shift): c.id for c in crews}

    exception_by_crew_rule, exception_by_day_amb, exception_by_employee_day_rule = {}, {}, {}
    for exc in active_exceptions:
        if exc.crew_assignment_id is not None:
            exception_by_crew_rule.setdefault((exc.crew_assignment_id, exc.rule_broken), exc.id)
        elif exc.ambulance_id is not None:  # legacy rows written before crew_assignment_id existed
            exception_by_day_amb.setdefault((exc.day.isoformat(), exc.ambulance_id), exc.id)
        if exc.employee_id is not None:
            exception_by_employee_day_rule.setdefault((exc.employee_id, exc.day.isoformat(), exc.rule_broken), exc.id)

    blocking_issues = []
    for issue in issues:
        exc_id = None
        if issue["type"] == "no_pro_on_crew":
            crew_id = crew_id_by_amb_day_shift.get((issue.get("ambulance_id"), issue.get("day"), issue.get("shift")))
            exc_id = exception_by_crew_rule.get((crew_id, "pro_coverage")) if crew_id else None
            if not exc_id and issue.get("ambulance_id") is not None:
                exc_id = exception_by_day_amb.get((issue.get("day"), issue["ambulance_id"]))
        elif issue["type"] == "no_driver_assigned":
            crew_id = crew_id_by_amb_day_shift.get((issue.get("ambulance_id"), issue.get("day"), issue.get("shift")))
            exc_id = exception_by_crew_rule.get((crew_id, "no_driver")) if crew_id else None
        elif issue["type"] == "rest_violation" and issue.get("employee_id") is not None:
            exc_id = exception_by_employee_day_rule.get((issue["employee_id"], issue.get("day"), "rest_after_night"))
        elif issue["type"] == "leave_conflict" and issue.get("employee_id") is not None:
            exc_id = exception_by_employee_day_rule.get((issue["employee_id"], issue.get("day"), "leave_override"))
        if exc_id:
            issue["approved_exception_id"] = exc_id
        else:
            blocking_issues.append(issue)

    return {"run_id": run_id, "ok": len(blocking_issues) == 0, "issue_count": len(blocking_issues), "issues": issues}


@app.post("/api/roster/{run_id}/publish")
def publish_roster(run_id: int, session: Session = Depends(get_session), user=Depends(require_role("admin"))):
    run = session.get(RosterRun, run_id)
    if not run:
        raise HTTPException(404, "Roster run not found")
    if run.status != "submitted":
        raise HTTPException(409, f"Only a submitted roster can be published (current status: {run.status}). "
                                  f"Ask the responsible manager to submit it first.")
    validation = validate_roster(run_id, session)
    if not validation["ok"]:
        raise HTTPException(
            409,
            {
                "message": "Resolve roster validation conflicts before publishing.",
                "issue_count": validation["issue_count"],
                "issues": validation["issues"][:25],
            },
        )
    run.status = "published"
    run.reviewed_by = user.id
    run.published_at = datetime.utcnow()
    session.add(run)
    session.flush()

    employee_ids = session.exec(
        select(Assignment.employee_id).where(Assignment.run_id == run_id).distinct()
    ).all()
    standby_days_by_employee = defaultdict(list)
    for sb in session.exec(select(StandbyAssignment).where(StandbyAssignment.run_id == run_id)).all():
        standby_days_by_employee[sb.employee_id].append(f"{sb.day.isoformat()} ({sb.shift})")

    period = _period_label(run)
    for employee_id in employee_ids:
        body = f"Your {period} roster has been published. Your personal timetable is attached."
        standby_days = standby_days_by_employee.get(employee_id)
        if standby_days:
            # A generic "roster published" note would leave a standby/backup
            # person no wiser that they're on-call, not just off — call it
            # out explicitly so they actually know to stay reachable.
            body += f" You're on standby/backup duty: {', '.join(standby_days)}."
        notify_employee(
            session, employee_id,
            title=f"Roster published — {period}",
            body=body,
            related_type="roster_publish", related_id=run_id, attach_run=run,
        )

    log_activity(session, user, "roster.publish", "roster_run", run.id,
                 f"Published roster {run.start_date.isoformat()} → {run.end_date.isoformat()} "
                 f"({len(employee_ids)} employee(s) notified).")
    session.commit()
    session.refresh(run)
    return run


@app.get("/api/roster/{run_id}/export")
def export_roster(run_id: int, session: Session = Depends(get_session), _=Depends(any_role)):
    run = session.get(RosterRun, run_id)
    if not run:
        raise HTTPException(404, "Roster run not found")
    assignments = session.exec(select(Assignment).where(Assignment.run_id == run_id)).all()
    employees = {e.id: e for e in session.exec(select(Employee)).all()}
    days = date_range(run.start_date, run.end_date)

    wb = Workbook()
    ws = wb.active
    ws.title = f"{run.start_date.isoformat()}_{run.end_date.isoformat()}"[:31]  # Excel sheet-name length limit

    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)

    ws.cell(row=1, column=1, value="No").font = header_font
    ws.cell(row=1, column=2, value="Name").font = header_font
    ws.cell(row=1, column=3, value="Role").font = header_font
    for i, d in enumerate(days):
        c = ws.cell(row=1, column=4 + i, value=d.strftime("%m-%d"))
        c.font = header_font
        c.alignment = Alignment(horizontal="center")
    for col in range(1, 4 + len(days)):
        ws.cell(row=1, column=col).fill = header_fill

    grid = {}
    for a in assignments:
        grid.setdefault(a.employee_id, {})[a.day] = a.code

    rows = sorted(grid.items(), key=lambda kv: employees[kv[0]].name if kv[0] in employees else "")
    for r, (emp_id, days_dict) in enumerate(rows, start=2):
        emp = employees.get(emp_id)
        ws.cell(row=r, column=1, value=r - 1)
        ws.cell(row=r, column=2, value=emp.name if emp else f"#{emp_id}")
        ws.cell(row=r, column=3, value=emp.role if emp else "")
        for i, d in enumerate(days):
            ws.cell(row=r, column=4 + i, value=days_dict.get(d, ""))

    ws.column_dimensions["B"].width = 28
    ws.column_dimensions["C"].width = 16
    ws.freeze_panes = "D2"

    path = os.path.join(OUTPUT_DIR, f"roster_{run.start_date.isoformat()}_to_{run.end_date.isoformat()}_run{run_id}.xlsx")
    wb.save(path)
    return FileResponse(path, filename=os.path.basename(path),
                         media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------- Frontend ----------
app.mount("/", StaticFiles(directory=os.path.join(BASE_DIR, "static"), html=True), name="static")
