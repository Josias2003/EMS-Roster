"""assignment facility id for dispatch desk shifts

Revision ID: 428d7fc02e54
Revises: d19d9b0e2639
Create Date: 2026-08-23 20:58:40.654269

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '428d7fc02e54'
down_revision: Union[str, None] = 'd19d9b0e2639'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('assignment', sa.Column('facility_id', sa.Integer(), nullable=True))
    op.create_foreign_key(None, 'assignment', 'facility', ['facility_id'], ['id'])


def downgrade() -> None:
    op.drop_constraint(None, 'assignment', type_='foreignkey')
    op.drop_column('assignment', 'facility_id')
