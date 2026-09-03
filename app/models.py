"""
Data models for the SAMU Fair Rostering System.

Kept intentionally close to the real roster you're already using:
- Stations are numbered teams (matches D1..D13 / N1..N13 codes on your sheet)
- Employees have a role, a driving flag, a team-leader flag, and a home station
- Leave records use the same short codes your paper roster already uses
"""
from datetime import date, datetime
from typing import Optional
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, Text


class Station(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)                 # e.g. "Station 1", "Kacyiru"
    code: str = Field(index=True, unique=True)     # e.g. "1"  -> produces D1/N1
    day_crew_size: int = 3
    night_crew_size: int = 3
    min_drivers_per_crew: int = 1
    active: bool = True                            # whole station on/off — you flip this manually as operational needs change
    night_active: bool = True                      # False = day-shift-only station; no night crew/driver is ever scheduled here


ROLE_CHOICES = ["driver", "dispatch", "call_and_dispatch", "anesthesiologist", "critical", "nurse", "unassigned"]
# "unassigned" is kept as the placeholder for a new/not-yet-classified
# employee (drives the "Needs role" flag on the Employees tab) — it isn't
# an operational role choice itself, everything else is.
# dispatch/call_and_dispatch are call-center-side roles, not ambulance crew — an
# employee in one of these still needs a Facility posting (see Facility model)
# to actually be excluded from crew/driver scheduling; role alone doesn't
# drive eligibility, only is_driver and facility_id do.
#
# anesthesiologist/critical/nurse replace the old combined "anesthesiologist_nurse"
# and "paramedic" labels — real medical-team specialties, each professionally
# distinct. Anesthesiologist/critical are the specialists cleared for tough
# interventions (see MIDDLE_SITE_CODES) AND the only specialties PHTL can send
# to the Regulation desk — confirmed directly, correcting an earlier
# "nurse-only"/"open to everyone" back-and-forth. Employees still sitting on
# the old "paramedic" or "anesthesiologist_nurse" values weren't auto-migrated
# to one of the three — there's no way to infer which real person is actually
# which specialty — they need manual reclassification (Employees tab flags
# them "Needs specialty").

# Anesthesiologist/critical staff are hard-restricted to these "middle site"
# stations (by Station.code, not id — ids can differ across environments) so
# they stay positioned for tough interventions rather than scattered across
# every station. CHUK, Kacyiru, Muhima, Kicukiro — confirmed directly with
# PHTL (an earlier version of this set had Remera instead of Kacyiru; Remera
# corrected out, Kacyiru corrected in).
MIDDLE_SITE_CODES = {"1", "5", "3", "7"}


class Facility(SQLModel, table=True):
    """A non-ambulance post (Logistics, Head Office, ...) staff can be
    permanently assigned to. Facility staff are a fixed/standing posting —
    they're excluded entirely from the day/night ambulance rotation, not
    scheduled shift-by-shift the way station crews are."""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)      # e.g. "Logistics", "Head Office"
    required_headcount: int = 1
    active: bool = True


class Employee(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    phone: Optional[str] = None
    email: Optional[str] = None
    role: str = Field(index=True)                   # one of ROLE_CHOICES
    is_driver: bool = False
    is_team_leader: bool = False
    station_id: Optional[int] = Field(default=None, foreign_key="station.id")
    facility_id: Optional[int] = Field(default=None, foreign_key="facility.id")
    # A facility posting is a fixed assignment, not a rotation — an employee
    # with facility_id set is excluded from ambulance crew/driver scheduling
    # entirely (see scheduler._eligible_pool). Mutually exclusive with really
    # participating in station rotation, though station_id is left as-is on
    # the record rather than cleared, since it's still useful as "home base."
    restricted_night_shift: bool = False            # covers any policy reason staff can't work nights
    night_only: bool = False                        # ad-hoc opposite: staff who told their manager they're
                                                      # only available at night right now — manager-toggled,
                                                      # same as restricted_night_shift, no separate request flow
    active: bool = True
    skill_score: int = 1                            # 1-5, fine-grained fairness/eligibility input
    is_pro: bool = False                            # gates crew-leader eligibility; every crew needs >=1 Pro
    floats_freely: bool = False                     # if False (default), a strong soft preference keeps
                                                     # this medical staffer at their home station — they're
                                                     # only sent elsewhere when their own station's needs are
                                                     # already met and another station is short
    event_eligible: bool = True                     # can this person be pulled into event staffing at all —
                                                     # not everyone on the medical team is cleared for events


class Certification(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    employee_id: int = Field(foreign_key="employee.id", index=True)
    name: str                       # e.g. "Advanced Airway", "Trauma"
    proficiency: int = 1             # 1-5
    expiry_date: Optional[date] = None


ROLES = ["admin", "call_center_tl", "drivers_manager", "phtl"]


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    role: str = Field(index=True)                     # one of ROLES
    employee_id: Optional[int] = Field(default=None, foreign_key="employee.id")
    active: bool = True


AMBULANCE_STATUSES = ["active", "maintenance", "out_of_service"]


class Ambulance(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    plate_no: str = Field(index=True, unique=True)
    type: str = "standard"
    station_id: Optional[int] = Field(default=None, foreign_key="station.id")
    status: str = "active"          # one of AMBULANCE_STATUSES


LEAVE_CODES = {
    "AL": "Annual leave",
    "SL": "Sick leave",
    "ML": "Maternity leave",
    "PL": "Paternity leave",
    "TR": "Training",
    "AD": "Administration",
    "P": "Permission",
    "O": "Forced off (manual)",
    # Real operational codes from RBC SAMU's existing paper/Excel roster —
    # matched against its printed legend. The solver never assigns these; it
    # only respects them exactly where they're entered, same as the codes above.
    "DR": "Day regulation",
    "NR": "Night shift regulation",
    "MS": "Mission",
    "DE": "Day event",
    "NE": "Night event",
    "ST": "Stock",
    "CY": "Cyclism",
    "CL": "Circumstance leave",
    "DT": "Data",
    "DM": "Day meeting",
    "SB": "Standby",
    "DC": "Day checking",
    "R": "Roster",
    # Seen in real data but not defined in the source roster's own legend —
    # imported as-is; ask PHTL what these actually mean.
    "EV": "Event (undocumented in source legend — likely shorthand for DE/NE)",
    "NESA": "Undocumented code from source roster — meaning unconfirmed",
}


class Leave(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    employee_id: int = Field(foreign_key="employee.id", index=True)
    day: date = Field(index=True)
    code: str  # key from LEAVE_CODES


class RosterRun(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    start_date: date
    end_date: date
    range_type: str = "month"       # "day" | "week" | "month" | "custom" — display/UX label only
    created_at: date
    status: str = "draft"   # draft -> submitted -> published
    # Holds the full JSON coverage-gaps/quality-score blob, which routinely
    # runs to several thousand characters — must not be capped like a normal
    # VARCHAR (Postgres tolerated an uncapped varchar; MySQL enforces it).
    notes: Optional[str] = Field(default=None, sa_column=Column(Text))
    submitted_by: Optional[int] = Field(default=None, foreign_key="user.id")
    submitted_at: Optional[datetime] = None
    reviewed_by: Optional[int] = Field(default=None, foreign_key="user.id")
    published_at: Optional[datetime] = None


class Assignment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="rosterrun.id", index=True)
    employee_id: int = Field(foreign_key="employee.id", index=True)
    day: date = Field(index=True)
    code: str        # e.g. "D3", "N7", "DF1"/"NF1" (facility desk shift), "O", "AL", "SL", ...
    station_id: Optional[int] = Field(default=None, foreign_key="station.id")
    facility_id: Optional[int] = Field(default=None, foreign_key="facility.id")
    manually_edited: bool = False


class CrewAssignment(SQLModel, table=True):
    """One ambulance's crew for one shift — the station -> ambulance -> crew rollup."""
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="rosterrun.id", index=True)
    ambulance_id: int = Field(foreign_key="ambulance.id", index=True)
    day: date = Field(index=True)
    shift: str                       # "day" | "night"
    driver_employee_id: Optional[int] = Field(default=None, foreign_key="employee.id")


class CrewMember(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    crew_assignment_id: int = Field(foreign_key="crewassignment.id", index=True)
    employee_id: int = Field(foreign_key="employee.id", index=True)
    is_leader: bool = False          # must be an is_pro employee


class Notification(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    employee_id: int = Field(foreign_key="employee.id", index=True)
    title: str
    body: str = Field(sa_column=Column(Text))
    related_type: Optional[str] = None   # e.g. "roster_publish", "crew_replace"
    related_id: Optional[int] = None     # e.g. run_id
    channel: str = "in_app"              # "in_app" | "email"
    sent_at: Optional[datetime] = None
    read_at: Optional[datetime] = None


EVENT_TYPES = ["football_match", "cecafa", "presidential_escort", "disaster", "concert", "national_celebration"]
EVENT_STATUSES = ["planned", "assigned", "cancelled"]


class Event(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    type: str                        # one of EVENT_TYPES
    date: date
    time: Optional[str] = None       # "HH:MM", kept as free text — no timezone/duration modeling yet
    location: str
    priority: int = 1                # 1 (low) - 5 (high)
    ambulances_required: int
    medical_crew_required: int
    drivers_required: int
    created_by: int = Field(foreign_key="user.id")
    status: str = "planned"          # one of EVENT_STATUSES


class EventAssignment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="event.id", index=True)
    ambulance_id: int = Field(foreign_key="ambulance.id")
    driver_employee_id: Optional[int] = Field(default=None, foreign_key="employee.id")


class EventCrewMember(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    event_assignment_id: int = Field(foreign_key="eventassignment.id", index=True)
    employee_id: int = Field(foreign_key="employee.id")
    is_leader: bool = False   # this crew's TL for the event — preferentially drawn from the standby/regulation team


class AuditLog(SQLModel, table=True):
    """Who did what, when — admin-facing activity trail. Written at the
    point of each meaningful mutation (see log_activity() in audit.py),
    not reconstructed after the fact, so it stays accurate even once the
    underlying record is edited again or deleted."""
    id: Optional[int] = Field(default=None, primary_key=True)
    at: datetime = Field(index=True)
    actor_username: str = Field(index=True)
    actor_role: str
    action: str = Field(index=True)          # e.g. "leave.create", "roster.publish"
    entity_type: str = Field(index=True)     # e.g. "leave", "employee", "roster_run"
    entity_id: Optional[int] = None
    summary: str                             # human-readable one-liner for the log list


EXCEPTION_TYPES = ["pull_for_event", "rest_override", "pro_coverage_override", "approved_shortage",
                    "event_replan", "leave_override"]
EXCEPTION_SEVERITIES = ["safety", "operational"]
EXCEPTION_REASONS = ["accident_surge", "major_event", "disaster", "vip_escort",
                      "staff_shortage", "ambulance_shortage", "manual_command_decision"]
EXCEPTION_STATUSES = ["active", "resolved"]


class OperationalException(SQLModel, table=True):
    """A deliberately-approved rule break: a leader chose to pull a specific
    person off a crew, accept a crew short a Pro, or break a rest day, when
    reality (an event, a shortage) left no compliant option. Recorded so the
    resulting gap shows up on the roster board as 'approved', not as a bug —
    see record_exception() in operational_exceptions.py."""
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: Optional[int] = Field(default=None, foreign_key="rosterrun.id", index=True)
    event_id: Optional[int] = Field(default=None, foreign_key="event.id", index=True)
    day: date = Field(index=True)
    shift: Optional[str] = None
    station_id: Optional[int] = Field(default=None, foreign_key="station.id")
    ambulance_id: Optional[int] = Field(default=None, foreign_key="ambulance.id")
    employee_id: Optional[int] = Field(default=None, foreign_key="employee.id")
    crew_assignment_id: Optional[int] = Field(default=None, foreign_key="crewassignment.id", index=True)
    exception_type: str                 # one of EXCEPTION_TYPES
    rule_broken: str                    # short label, e.g. "rest_after_night", "pro_coverage", "no_replacement"
    severity: str                       # one of EXCEPTION_SEVERITIES — computed from actual impact, never user-chosen
    reason: str                         # one of EXCEPTION_REASONS
    justification: str = Field(sa_column=Column(Text))
    impact_summary: str
    approved_by: int = Field(foreign_key="user.id")
    approved_at: datetime
    status: str = "active"              # one of EXCEPTION_STATUSES
    resolved_by: Optional[int] = Field(default=None, foreign_key="user.id")
    resolved_at: Optional[datetime] = None
    # Separate from resolved_*: a safety exception is live the instant it's
    # created (an emergency can't wait on admin sign-off) but stays flagged
    # for admin to endorse after the fact — review and resolve are
    # independent, an exception can be reviewed while still active.
    admin_reviewed: bool = False
    admin_reviewed_by: Optional[int] = Field(default=None, foreign_key="user.id")
    admin_reviewed_at: Optional[datetime] = None


STANDBY_GROUPS = ["driver", "medical", "call_center"]


class StandbySettings(SQLModel, table=True):
    """Singleton (always id=1) admin-set headcount for each group's shared,
    district-wide standby pool — replaces the old per-station
    Station.standby_per_shift, which wrongly implied every station held its
    own separate reserve team instead of drawing from one shared pool."""
    id: Optional[int] = Field(default=None, primary_key=True)
    driver_day: int = 0
    driver_night: int = 0
    medical_day: int = 0
    medical_night: int = 0
    call_center_day: int = 0
    call_center_night: int = 0


class StandbyAssignment(SQLModel, table=True):
    """A reserve/on-call staffer for one group's shared standby pool — not
    actively working, but held ready and counted toward fair rotation over
    time. Global, not tied to any one station: see STANDBY_GROUPS."""
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="rosterrun.id", index=True)
    group: str = Field(index=True)   # one of STANDBY_GROUPS
    day: date = Field(index=True)
    shift: str                       # "day" | "night"
    employee_id: int = Field(foreign_key="employee.id", index=True)
