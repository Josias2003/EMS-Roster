"""employee night_only flag

Revision ID: a1b2c3d4e5f6
Revises: 3fce212562a3
Create Date: 2026-08-26 00:00:00.000000

Ad-hoc "only available at night" accommodation, symmetric opposite of the
existing restricted_night_shift flag — for staff who tell their manager
they're only available at night for some personal circumstance right now.
Manager-toggled the same way restricted_night_shift already is; no separate
request/approval workflow.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '3fce212562a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('employee', sa.Column('night_only', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column('employee', 'night_only')
