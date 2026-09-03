"""exception review fields and crew_assignment link

Revision ID: 01950266575a
Revises: e459dd9f37c8
Create Date: 2026-08-24 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '01950266575a'
down_revision: Union[str, None] = 'e459dd9f37c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('operationalexception', sa.Column('crew_assignment_id', sa.Integer(), nullable=True))
    op.add_column('operationalexception', sa.Column('admin_reviewed', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('operationalexception', sa.Column('admin_reviewed_by', sa.Integer(), nullable=True))
    op.add_column('operationalexception', sa.Column('admin_reviewed_at', sa.DateTime(), nullable=True))
    op.create_index(op.f('ix_operationalexception_crew_assignment_id'), 'operationalexception', ['crew_assignment_id'], unique=False)
    op.create_foreign_key(None, 'operationalexception', 'crewassignment', ['crew_assignment_id'], ['id'])
    op.create_foreign_key(None, 'operationalexception', 'user', ['admin_reviewed_by'], ['id'])


def downgrade() -> None:
    op.drop_index(op.f('ix_operationalexception_crew_assignment_id'), table_name='operationalexception')
    op.drop_column('operationalexception', 'admin_reviewed_at')
    op.drop_column('operationalexception', 'admin_reviewed_by')
    op.drop_column('operationalexception', 'admin_reviewed')
    op.drop_column('operationalexception', 'crew_assignment_id')
