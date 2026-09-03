from typing import Optional
from datetime import date
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from .database import get_session
from .models import RosterRun, Station, Ambulance, Employee, CrewAssignment, CrewMember, StandbyAssignment
from .auth import require_role

router = APIRouter(prefix="/api", tags=["crew"])

any_role = require_role("admin", "call_center_tl", "drivers_manager", "phtl")
set_leader_role = require_role("admin", "phtl")


@router.get("/roster/{run_id}/crew")
def crew_rollup(run_id: int, day: Optional[date] = None, session: Session = Depends(get_session), _=Depends(any_role)):
    """Station -> ambulance -> crew rollup for a roster run, optionally filtered to one day."""
    run = session.get(RosterRun, run_id)
    if not run:
        raise HTTPException(404, "Roster run not found")

    query = select(CrewAssignment).where(CrewAssignment.run_id == run_id)
    if day is not None:
        query = query.where(CrewAssignment.day == day)
    crews = session.exec(query).all()

    stations = {s.id: s for s in session.exec(select(Station)).all()}
    ambulances = {a.id: a for a in session.exec(select(Ambulance)).all()}
    employees = {e.id: e for e in session.exec(select(Employee)).all()}

    crew_ids = [c.id for c in crews]
    members_by_crew = {}
    if crew_ids:
        rows = session.exec(select(CrewMember).where(CrewMember.crew_assignment_id.in_(crew_ids))).all()
        for m in rows:
            members_by_crew.setdefault(m.crew_assignment_id, []).append(m)

    def emp_out(emp_id):
        e = employees.get(emp_id)
        if not e:
            return None
        return {"id": e.id, "name": e.name, "role": e.role, "is_pro": e.is_pro}

    by_station = {}
    for c in crews:
        amb = ambulances.get(c.ambulance_id)
        st_id = amb.station_id if amb else None
        st = stations.get(st_id)
        station_bucket = by_station.setdefault(st_id, {
            "station": {"id": st.id, "name": st.name, "code": st.code} if st else None,
            "ambulances": {},
        })
        amb_bucket = station_bucket["ambulances"].setdefault(c.ambulance_id, {
            "ambulance": {"id": amb.id, "plate_no": amb.plate_no, "status": amb.status} if amb else None,
            "shifts": [],
        })
        members = [
            {**emp_out(m.employee_id), "is_leader": m.is_leader}
            for m in members_by_crew.get(c.id, []) if emp_out(m.employee_id)
        ]
        amb_bucket["shifts"].append({
            "crew_assignment_id": c.id,
            "day": c.day.isoformat(),
            "shift": c.shift,
            "driver": emp_out(c.driver_employee_id) if c.driver_employee_id else None,
            "members": members,
        })

    result = []
    for st_id, bucket in by_station.items():
        result.append({
            "station": bucket["station"],
            "ambulances": list(bucket["ambulances"].values()),
        })
    return result


@router.get("/roster/{run_id}/standby")
def standby_rollup(run_id: int, day: Optional[date] = None, session: Session = Depends(get_session), _=Depends(any_role)):
    """Global reserve/on-call standby pools (driver/medical/call_center) for
    a roster run, optionally filtered to one day — not tied to any station,
    since standby is one shared district-wide pool per group, not something
    each site holds its own copy of."""
    run = session.get(RosterRun, run_id)
    if not run:
        raise HTTPException(404, "Roster run not found")

    query = select(StandbyAssignment).where(StandbyAssignment.run_id == run_id)
    if day is not None:
        query = query.where(StandbyAssignment.day == day)
    rows = session.exec(query).all()

    employees = {e.id: e for e in session.exec(select(Employee)).all()}

    by_group = {}
    for r in rows:
        bucket = by_group.setdefault(r.group, {"group": r.group, "standby": []})
        emp = employees.get(r.employee_id)
        bucket["standby"].append({
            "day": r.day.isoformat(), "shift": r.shift,
            "employee": {"id": emp.id, "name": emp.name, "role": emp.role} if emp else None,
        })
    return list(by_group.values())


class SetLeaderRequest(BaseModel):
    employee_id: int


@router.put("/crew/{crew_assignment_id}/leader")
def set_crew_leader(crew_assignment_id: int, payload: SetLeaderRequest,
                     session: Session = Depends(get_session), _=Depends(set_leader_role)):
    crew = session.get(CrewAssignment, crew_assignment_id)
    if not crew:
        raise HTTPException(404, "Crew assignment not found")
    members = session.exec(select(CrewMember).where(CrewMember.crew_assignment_id == crew_assignment_id)).all()
    target = next((m for m in members if m.employee_id == payload.employee_id), None)
    if not target:
        raise HTTPException(400, "That employee is not a member of this crew.")
    employee = session.get(Employee, payload.employee_id)
    if not employee or not employee.is_pro:
        raise HTTPException(400, "Only a Pro-rated crew member can be set as crew leader.")
    for m in members:
        m.is_leader = (m.id == target.id)
        session.add(m)
    session.commit()
    return {"ok": True}


@router.get("/employees/{employee_id}/slot-today")
def employee_slot_for_day(employee_id: int, day: Optional[date] = None,
                           session: Session = Depends(get_session), _=Depends(any_role)):
    """Find which crew slot an employee is on for a given day (defaults to
    today) — the lookup behind the 'mark unavailable / emergency replacement'
    shortcut, so a TL doesn't have to hunt through Crew Rollup by hand."""
    target_day = day or date.today()
    run = session.exec(
        select(RosterRun).where(RosterRun.start_date <= target_day, RosterRun.end_date >= target_day)
        .order_by(RosterRun.id.desc())
    ).first()
    if not run:
        raise HTTPException(404, "No roster run found covering that date.")

    crews_that_day = session.exec(select(CrewAssignment).where(
        CrewAssignment.run_id == run.id, CrewAssignment.day == target_day
    )).all()

    for c in crews_that_day:
        if c.driver_employee_id == employee_id:
            return {"run_id": run.id, "crew_assignment_id": c.id, "slot": "driver",
                    "day": target_day.isoformat(), "shift": c.shift}

    crew_ids = [c.id for c in crews_that_day]
    if crew_ids:
        member = session.exec(select(CrewMember).where(
            CrewMember.crew_assignment_id.in_(crew_ids), CrewMember.employee_id == employee_id
        )).first()
        if member:
            c = next(c for c in crews_that_day if c.id == member.crew_assignment_id)
            return {"run_id": run.id, "crew_assignment_id": c.id, "slot": "member",
                    "day": target_day.isoformat(), "shift": c.shift}

    raise HTTPException(404, "This employee isn't assigned to a crew on that day "
                              "(may be off, on leave, or not yet scheduled).")
