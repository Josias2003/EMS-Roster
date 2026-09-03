"""operational exception table

Revision ID: e459dd9f37c8
Revises: d2cb00414b03
Create Date: 2026-08-24 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'e459dd9f37c8'
down_revision: Union[str, None] = 'd2cb00414b03'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'operationalexception',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=True),
        sa.Column('event_id', sa.Integer(), nullable=True),
        sa.Column('day', sa.Date(), nullable=False),
        sa.Column('shift', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('station_id', sa.Integer(), nullable=True),
        sa.Column('ambulance_id', sa.Integer(), nullable=True),
        sa.Column('employee_id', sa.Integer(), nullable=True),
        sa.Column('exception_type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('rule_broken', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('severity', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('reason', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('justification', sa.Text(), nullable=False),
        sa.Column('impact_summary', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('approved_by', sa.Integer(), nullable=False),
        sa.Column('approved_at', sa.DateTime(), nullable=False),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('resolved_by', sa.Integer(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['run_id'], ['rosterrun.id']),
        sa.ForeignKeyConstraint(['event_id'], ['event.id']),
        sa.ForeignKeyConstraint(['station_id'], ['station.id']),
        sa.ForeignKeyConstraint(['ambulance_id'], ['ambulance.id']),
        sa.ForeignKeyConstraint(['employee_id'], ['employee.id']),
        sa.ForeignKeyConstraint(['approved_by'], ['user.id']),
        sa.ForeignKeyConstraint(['resolved_by'], ['user.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_operationalexception_run_id'), 'operationalexception', ['run_id'], unique=False)
    op.create_index(op.f('ix_operationalexception_event_id'), 'operationalexception', ['event_id'], unique=False)
    op.create_index(op.f('ix_operationalexception_day'), 'operationalexception', ['day'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_operationalexception_day'), table_name='operationalexception')
    op.drop_index(op.f('ix_operationalexception_event_id'), table_name='operationalexception')
    op.drop_index(op.f('ix_operationalexception_run_id'), table_name='operationalexception')
    op.drop_table('operationalexception')
