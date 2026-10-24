"""
Non-ambulance postings (Logistics, Head Office, ...). A facility staffer
is a fixed/standing assignment, not part of the day/night ambulance
rotation — the scheduler excludes anyone with facility_id set entirely
(see scheduler.py's employee pool filtering).
"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from .database import get_session
from .models import Facility, Employee, User, Assignment
from .auth import require_role
from .audit import log_activity

router = APIRouter(prefix="/api/facilities", tags=["facilities"])

any_role = require_role("admin", "call_center_tl", "drivers_manager", "phtl")
admin_only = require_role("admin")


class FacilityCreate(BaseModel):
    name: str
    required_headcount: int = 1
    active: bool = True


class FacilityUpdate(BaseModel):
    name: Optional[str] = None
    required_headcount: Optional[int] = None
    active: Optional[bool] = None


@router.get("")
def list_facilities(session: Session = Depends(get_session), _=Depends(any_role)):
    facilities = session.exec(select(Facility)).all()
    counts = {}
    for e in session.exec(select(Employee).where(Employee.active == True, Employee.facility_id != None)).all():
        counts[e.facility_id] = counts.get(e.facility_id, 0) + 1
    return [
        {**f.dict(), "current_headcount": counts.get(f.id, 0)}
        for f in facilities
    ]


@router.post("")
def create_facility(payload: FacilityCreate, session: Session = Depends(get_session),
                     actor: User = Depends(admin_only)):
    facility = Facility(**payload.dict())
    session.add(facility)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, f"Facility '{payload.name}' already exists.")
    session.refresh(facility)
    log_activity(session, actor, "facility.create", "facility", facility.id,
                 f"Created facility '{facility.name}' (needs {facility.required_headcount}).")
    session.commit()
    session.refresh(facility)  # the second commit expires it; without this the response body is empty
    return facility


@router.put("/{facility_id}")
def update_facility(facility_id: int, payload: FacilityUpdate, session: Session = Depends(get_session),
                     actor: User = Depends(admin_only)):
    facility = session.get(Facility, facility_id)
    if not facility:
        raise HTTPException(404, "Facility not found")
    for k, v in payload.dict(exclude_unset=True).items():
        setattr(facility, k, v)
    session.add(facility)
    log_activity(session, actor, "facility.update", "facility", facility.id,
                 f"Updated facility '{facility.name}'.")
    session.commit()
    session.refresh(facility)
    return facility


@router.delete("/{facility_id}")
def delete_facility(facility_id: int, session: Session = Depends(get_session),
                     actor: User = Depends(admin_only)):
    facility = session.get(Facility, facility_id)
    if not facility:
        raise HTTPException(404, "Facility not found")
    linked = session.exec(
        select(Employee).where(Employee.facility_id == facility_id, Employee.active == True)
    ).first()
    if linked:
        raise HTTPException(409, "This facility still has active employees assigned to it. Reassign them first.")
    has_history = session.exec(select(Assignment).where(Assignment.facility_id == facility_id)).first()
    if has_history:
        raise HTTPException(409, "This facility has roster history and can't be deleted — "
                                  "set it inactive instead to keep it out of future generation.")
    log_activity(session, actor, "facility.delete", "facility", facility.id,
                 f"Deleted facility '{facility.name}'.")
    session.delete(facility)
    session.commit()
    return {"ok": True}
