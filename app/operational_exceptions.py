"""
Operational Exceptions ("Command Decisions"): the record of every deliberate
rule break — a rest day overridden, a crew accepted with no Pro-rated member,
someone pulled off a crew for an event with no replacement. The system stays
strict by default (overrides.py and events.py still hard-reject these by
default); this module is only reached once a leader has explicitly chosen to
break a specific rule and given a reason, so the resulting gap is recorded,
not hidden — see record_exception() below, called from overrides.py and
events.py at the moment of the actual override/pull.
"""
from datetime import date, datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from .database import get_session
from .models import OperationalException, Employee, User, EXCEPTION_STATUSES
from .auth import require_role

router = APIRouter(prefix="/api/exceptions", tags=["exceptions"])

any_role = require_role("admin", "call_center_tl", "drivers_manager", "phtl")
participant_role = require_role("admin", "drivers_manager", "phtl")


def record_exception(
    session: Session, actor: User, *, exception_type: str, rule_broken: str, severity: str,
    reason: str, justification: str, impact_summary: str,
    run_id: Optional[int] = None, event_id: Optional[int] = None,
    day, shift: Optional[str] = None, station_id: Optional[int] = None,
    ambulance_id: Optional[int] = None, employee_id: Optional[int] = None,
    crew_assignment_id: Optional[int] = None,
) -> OperationalException:
    """Builds and stages (session.add) an OperationalException row. Does not
    commit — the caller commits alongside its own change, same pattern as
    log_activity(), so the exception record and the change it explains land
    in the same transaction. A safety-severity exception is live immediately
    (nothing about the underlying assignment waits on this) but starts
    unreviewed — see admin_reviewed and POST /{id}/review below."""
    exc = OperationalException(
        run_id=run_id, event_id=event_id, day=day, shift=shift, station_id=station_id,
        ambulance_id=ambulance_id, employee_id=employee_id, crew_assignment_id=crew_assignment_id,
        exception_type=exception_type, rule_broken=rule_broken, severity=severity,
        reason=reason, justification=justification, impact_summary=impact_summary,
        approved_by=actor.id, approved_at=datetime.utcnow(), status="active",
        admin_reviewed=(severity != "safety"),
    )
    session.add(exc)
    return exc


def _can_manage(actor: User, exc: OperationalException, session: Session) -> bool:
    if actor.role == "admin":
        return True
    if exc.approved_by == actor.id:
        return True
    if not exc.employee_id:
        return False
    employee = session.get(Employee, exc.employee_id)
    if not employee:
        return False
    if actor.role == "drivers_manager" and employee.is_driver:
        return True
    if actor.role == "phtl" and not employee.is_driver:
        return True
    return False


@router.get("")
def list_exceptions(
    run_id: Optional[int] = None, event_id: Optional[int] = None,
    status: Optional[str] = None, severity: Optional[str] = None,
    session: Session = Depends(get_session), _: User = Depends(any_role),
):
    query = select(OperationalException)
    if run_id is not None:
        query = query.where(OperationalException.run_id == run_id)
    if event_id is not None:
        query = query.where(OperationalException.event_id == event_id)
    if status:
        if status not in EXCEPTION_STATUSES:
            raise HTTPException(400, f"status must be one of {EXCEPTION_STATUSES}")
        query = query.where(OperationalException.status == status)
    if severity:
        query = query.where(OperationalException.severity == severity)
    query = query.order_by(OperationalException.id.desc())
    rows = session.exec(query).all()

    employees = {e.id: e for e in session.exec(select(Employee)).all()}
    users = {u.id: u for u in session.exec(select(User)).all()}
    today = date.today()
    out = []
    for r in rows:
        emp = employees.get(r.employee_id) if r.employee_id else None
        approver = users.get(r.approved_by)
        resolver = users.get(r.resolved_by) if r.resolved_by else None
        reviewer = users.get(r.admin_reviewed_by) if r.admin_reviewed_by else None
        out.append({
            **r.dict(),
            "employee_name": emp.name if emp else None,
            "approved_by_username": approver.username if approver else None,
            "resolved_by_username": resolver.username if resolver else None,
            "admin_reviewed_by_username": reviewer.username if reviewer else None,
            # Deliberately not an auto-expiring status — a past-day exception
            # left active usually means the situation is still unresolved,
            # not that it stopped mattering. This just makes that visible.
            "is_past": r.day < today,
        })
    return out


@router.post("/{exception_id}/resolve")
def resolve_exception(exception_id: int, session: Session = Depends(get_session),
                       actor: User = Depends(participant_role)):
    exc = session.get(OperationalException, exception_id)
    if not exc:
        raise HTTPException(404, "Exception not found")
    if exc.status == "resolved":
        raise HTTPException(409, "Already resolved.")
    if not _can_manage(actor, exc, session):
        raise HTTPException(403, "You can't resolve an exception outside your lane.")
    exc.status = "resolved"
    exc.resolved_by = actor.id
    exc.resolved_at = datetime.utcnow()
    session.add(exc)
    session.commit()
    return {"ok": True}


@router.post("/{exception_id}/review")
def review_exception(exception_id: int, session: Session = Depends(get_session),
                      actor: User = Depends(require_role("admin"))):
    exc = session.get(OperationalException, exception_id)
    if not exc:
        raise HTTPException(404, "Exception not found")
    if exc.admin_reviewed:
        raise HTTPException(409, "Already reviewed.")
    exc.admin_reviewed = True
    exc.admin_reviewed_by = actor.id
    exc.admin_reviewed_at = datetime.utcnow()
    session.add(exc)
    session.commit()
    return {"ok": True}
