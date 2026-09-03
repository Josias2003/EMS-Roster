"""unique crew and assignment slots

Revision ID: 3fce212562a3
Revises: 0cc24014ec47
Create Date: 2026-08-25 00:00:00.000000

Data-integrity hardening after a real incident: a roster run ended up with
duplicate CrewAssignment rows (same ambulance/day/shift) and duplicate
Assignment rows (same employee/day, with CONFLICTING codes — one row said
a person worked a night shift, another said they were off) after a
regenerate request appears to have been interrupted mid-transaction. The
application code already assumes at most one row per slot everywhere (the
`existing_crew` lookup in main.py, the double_booked check in
validate_roster); this migration makes that assumption a real, enforced
guarantee, so any future occurrence — whatever the cause — fails loudly
with an IntegrityError instead of silently leaving inconsistent data for
someone to discover in the UI.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '3fce212562a3'
down_revision: Union[str, None] = '0cc24014ec47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        'uq_crewassignment_run_amb_day_shift', 'crewassignment',
        ['run_id', 'ambulance_id', 'day', 'shift'],
    )
    op.create_unique_constraint(
        'uq_assignment_run_employee_day', 'assignment',
        ['run_id', 'employee_id', 'day'],
    )


def downgrade() -> None:
    op.drop_constraint('uq_assignment_run_employee_day', 'assignment', type_='unique')
    op.drop_constraint('uq_crewassignment_run_amb_day_shift', 'crewassignment', type_='unique')
