from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select
from pydantic import BaseModel

from .database import get_session
from .models import Ambulance, AMBULANCE_STATUSES, CrewAssignment, EventAssignment
from .auth import require_role

router = APIRouter(prefix="/api/ambulances", tags=["ambulances"])

any_role = require_role("admin", "call_center_tl", "drivers_manager", "phtl")
manage_role = require_role("admin", "drivers_manager")


class AmbulanceCreate(BaseModel):
    plate_no: str
    type: str = "standard"
    station_id: Optional[int] = None
    status: str = "active"


class AmbulanceUpdate(BaseModel):
    plate_no: Optional[str] = None
    type: Optional[str] = None
    station_id: Optional[int] = None
    status: Optional[str] = None


def _validate_status(status: str):
    if status not in AMBULANCE_STATUSES:
        raise HTTPException(400, f"status must be one of {AMBULANCE_STATUSES}")


@router.get("")
def list_ambulances(session: Session = Depends(get_session), _=Depends(any_role)):
    return session.exec(select(Ambulance)).all()


@router.post("")
def create_ambulance(payload: AmbulanceCreate, session: Session = Depends(get_session), _=Depends(manage_role)):
    _validate_status(payload.status)
    ambulance = Ambulance(**payload.dict())
    session.add(ambulance)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, f"Plate number '{payload.plate_no}' is already in use.")
    session.refresh(ambulance)
    return ambulance


@router.put("/{ambulance_id}")
def update_ambulance(ambulance_id: int, payload: AmbulanceUpdate, session: Session = Depends(get_session), _=Depends(manage_role)):
    ambulance = session.get(Ambulance, ambulance_id)
    if not ambulance:
        raise HTTPException(404, "Ambulance not found")
    data = payload.dict(exclude_unset=True)
    if "status" in data:
        _validate_status(data["status"])
    for k, v in data.items():
        setattr(ambulance, k, v)
    session.add(ambulance)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, f"Plate number '{payload.plate_no}' is already in use.")
    session.refresh(ambulance)
    return ambulance


@router.delete("/{ambulance_id}")
def delete_ambulance(ambulance_id: int, session: Session = Depends(get_session), _=Depends(manage_role)):
    ambulance = session.get(Ambulance, ambulance_id)
    if not ambulance:
        raise HTTPException(404, "Ambulance not found")
    # Deleting an ambulance that's on record for any generated roster or event
    # would either orphan that history's FK or blow up with a raw DB
    # IntegrityError — neither is useful. status="out_of_service" is the real
    # "this ambulance is gone" signal; delete stays for genuine mistakes only
    # (e.g. added with a typo'd plate, never actually used).
    has_crew_history = session.exec(
        select(CrewAssignment).where(CrewAssignment.ambulance_id == ambulance_id)
    ).first()
    has_event_history = session.exec(
        select(EventAssignment).where(EventAssignment.ambulance_id == ambulance_id)
    ).first()
    if has_crew_history or has_event_history:
        raise HTTPException(409, "This ambulance has roster or event history and can't be deleted. "
                                  "Set its status to 'out_of_service' instead.")
    session.delete(ambulance)
    session.commit()
    return {"ok": True}
