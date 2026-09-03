"""employee event eligible flag

Revision ID: d2cb00414b03
Revises: 428d7fc02e54
Create Date: 2026-08-24 09:51:57.856832

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'd2cb00414b03'
down_revision: Union[str, None] = '428d7fc02e54'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('employee', sa.Column('event_eligible', sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column('employee', 'event_eligible')
