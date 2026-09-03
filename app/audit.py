"""
Admin-facing activity trail. Every meaningful mutation (leave entered,
employee edited, roster generated/submitted/published, a crew slot
replaced, an event created, ...) writes one row here, in the same
transaction as the change itself — so the log always matches what
actually happened, not a reconstruction after the fact.
"""
from datetime import datetime, date
from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select

from .database import get_session
from .models import AuditLog, User
from .auth import require_role

router = APIRouter(prefix="/api", tags=["audit"])

admin_only = require_role("admin")


def log_activity(session: Session, actor: User, action: str, entity_type: str,
                  entity_id: Optional[int], summary: str):
    session.add(AuditLog(
        at=datetime.utcnow(), actor_username=actor.username, actor_role=actor.role,
        action=action, entity_type=entity_type, entity_id=entity_id, summary=summary,
    ))


@router.get("/audit-log")
def list_audit_log(
    actor: Optional[str] = None,
    action: Optional[str] = None,
    entity_type: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    limit: int = Query(default=200, le=1000),
    session: Session = Depends(get_session), _: User = Depends(admin_only),
):
    query = select(AuditLog)
    if actor:
        query = query.where(AuditLog.actor_username == actor)
    if action:
        query = query.where(AuditLog.action == action)
    if entity_type:
        query = query.where(AuditLog.entity_type == entity_type)
    if start_date:
        query = query.where(AuditLog.at >= datetime.combine(start_date, datetime.min.time()))
    if end_date:
        query = query.where(AuditLog.at <= datetime.combine(end_date, datetime.max.time()))
    query = query.order_by(AuditLog.id.desc()).limit(limit)
    return session.exec(query).all()
