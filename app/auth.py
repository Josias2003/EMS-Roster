"""
Auth router and role-gate dependencies.

Bootstrap: the very first user (when the User table is empty) can be created
via POST /api/auth/users with no token — that user must be role=admin. Every
user after that requires an authenticated admin to create.
"""
from typing import Optional, List
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlmodel import Session, select

from .database import get_session
from .models import User, ROLES
from .security import hash_password, verify_password, create_access_token, decode_access_token, password_strength_error
# log_activity is imported inside update_user(), not here — audit.py imports
# require_role from this module, so a module-level import here would be circular.

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    username: str


class UserCreate(BaseModel):
    username: str
    password: str
    role: str
    employee_id: Optional[int] = None


class UserUpdate(BaseModel):
    role: Optional[str] = None
    employee_id: Optional[int] = None
    active: Optional[bool] = None
    password: Optional[str] = None  # set to reset it; omitted leaves it unchanged


class UserOut(BaseModel):
    id: int
    username: str
    role: str
    employee_id: Optional[int] = None
    active: bool


# Per-username failed-login tracking, in-process. Good enough for a single
# uvicorn worker (this app's actual deployment shape); a multi-worker or
# multi-instance deployment would need this in a shared store (e.g. Redis)
# instead, since each process would otherwise track its own count.
_FAILED_LOGIN_LIMIT = 5
_LOCKOUT_MINUTES = 15
_failed_logins: dict = {}  # username -> [datetime, ...] of recent failures


def _login_locked_out(username: str) -> Optional[int]:
    """None if not locked out, else minutes remaining."""
    attempts = _failed_logins.get(username, [])
    cutoff = datetime.utcnow() - timedelta(minutes=_LOCKOUT_MINUTES)
    attempts = [a for a in attempts if a > cutoff]
    _failed_logins[username] = attempts
    if len(attempts) >= _FAILED_LOGIN_LIMIT:
        oldest_relevant = attempts[-_FAILED_LOGIN_LIMIT]
        remaining = _LOCKOUT_MINUTES - int((datetime.utcnow() - oldest_relevant).total_seconds() // 60)
        return max(remaining, 1)
    return None


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, session: Session = Depends(get_session)):
    locked_minutes = _login_locked_out(payload.username)
    if locked_minutes is not None:
        raise HTTPException(429, f"Too many failed attempts for this username. Try again in "
                                  f"{locked_minutes} minute(s).")
    user = session.exec(select(User).where(User.username == payload.username)).first()
    if not user or not user.active or not verify_password(payload.password, user.password_hash):
        _failed_logins.setdefault(payload.username, []).append(datetime.utcnow())
        raise HTTPException(401, "Invalid username or password.")
    _failed_logins.pop(payload.username, None)
    token = create_access_token(user.id, user.username, user.role)
    return TokenResponse(access_token=token, role=user.role, username=user.username)


def get_current_user(
    authorization: Optional[str] = Header(default=None),
    session: Session = Depends(get_session),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header.")
    token = authorization.split(" ", 1)[1]
    try:
        payload = decode_access_token(token)
    except Exception:
        raise HTTPException(401, "Invalid or expired token.")
    user = session.get(User, int(payload["sub"]))
    if not user or not user.active:
        raise HTTPException(401, "User no longer active.")
    return user


def require_role(*allowed_roles: str):
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed_roles:
            raise HTTPException(403, f"Role '{user.role}' cannot perform this action.")
        return user
    return dependency


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.get("/users", response_model=List[UserOut])
def list_users(session: Session = Depends(get_session), _: User = Depends(require_role("admin"))):
    return session.exec(select(User)).all()


@router.post("/users", response_model=UserOut)
def create_user(
    payload: UserCreate,
    session: Session = Depends(get_session),
    authorization: Optional[str] = Header(default=None),
):
    if payload.role not in ROLES:
        raise HTTPException(400, f"role must be one of {ROLES}")
    pw_error = password_strength_error(payload.password)
    if pw_error:
        raise HTTPException(400, pw_error)

    any_users_exist = session.exec(select(User)).first() is not None
    if not any_users_exist:
        if payload.role != "admin":
            raise HTTPException(400, "The first user created must be an admin.")
    else:
        # not the bootstrap case — require an authenticated admin
        get_current_user_result = get_current_user(authorization=authorization, session=session)
        if get_current_user_result.role != "admin":
            raise HTTPException(403, "Only an admin can create users.")

    existing = session.exec(select(User).where(User.username == payload.username)).first()
    if existing:
        raise HTTPException(409, f"Username '{payload.username}' is already taken.")

    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        role=payload.role,
        employee_id=payload.employee_id,
        active=True,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@router.put("/users/{user_id}", response_model=UserOut)
def update_user(user_id: int, payload: UserUpdate, session: Session = Depends(get_session),
                 actor: User = Depends(require_role("admin"))):
    from .audit import log_activity  # deferred — see the module-level note on why

    user = session.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")

    if payload.role is not None and payload.role not in ROLES:
        raise HTTPException(400, f"role must be one of {ROLES}")

    # Deactivating or demoting the last active admin would lock everyone out
    # of user/role management entirely — including the person trying to do
    # it, if it's themselves. Block it rather than let an admin brick the
    # system by accident.
    losing_admin = (
        user.role == "admin" and user.active
        and ((payload.active is False) or (payload.role is not None and payload.role != "admin"))
    )
    if losing_admin:
        other_active_admins = session.exec(select(User).where(
            User.role == "admin", User.active == True, User.id != user_id
        )).first()
        if not other_active_admins:
            raise HTTPException(409, "This is the last active admin — deactivate or demote another "
                                      "admin first, or promote someone else, before changing this one.")

    changes = []
    if payload.role is not None and payload.role != user.role:
        changes.append(f"role {user.role} -> {payload.role}")
        user.role = payload.role
    if payload.employee_id is not None and payload.employee_id != user.employee_id:
        changes.append("employee_id changed")
        user.employee_id = payload.employee_id
    if payload.active is not None and payload.active != user.active:
        changes.append(f"active -> {payload.active}")
        user.active = payload.active
    if payload.password:
        pw_error = password_strength_error(payload.password)
        if pw_error:
            raise HTTPException(400, pw_error)
        user.password_hash = hash_password(payload.password)
        changes.append("password reset")

    session.add(user)
    log_activity(session, actor, "user.update", "user", user.id,
                 f"Updated user '{user.username}': {', '.join(changes) if changes else 'no changes'}.")
    session.commit()
    session.refresh(user)
    return user
