"""global standby pools

Revision ID: 0cc24014ec47
Revises: 01950266575a
Create Date: 2026-08-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '0cc24014ec47'
down_revision: Union[str, None] = '01950266575a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'standbysettings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('driver_day', sa.Integer(), nullable=False),
        sa.Column('driver_night', sa.Integer(), nullable=False),
        sa.Column('medical_day', sa.Integer(), nullable=False),
        sa.Column('medical_night', sa.Integer(), nullable=False),
        sa.Column('call_center_day', sa.Integer(), nullable=False),
        sa.Column('call_center_night', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    conn = op.get_bind()
    inspector = sa.inspect(conn)

    for fk in inspector.get_foreign_keys('standbyassignment'):
        if fk.get('constrained_columns') == ['station_id']:
            op.drop_constraint(fk['name'], 'standbyassignment', type_='foreignkey')
    for idx in inspector.get_indexes('standbyassignment'):
        if idx.get('column_names') == ['station_id']:
            op.drop_index(idx['name'], table_name='standbyassignment')
    op.drop_column('standbyassignment', 'station_id')

    op.add_column('standbyassignment',
                   sa.Column('group', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default='medical'))
    op.create_index(op.f('ix_standbyassignment_group'), 'standbyassignment', ['group'], unique=False)

    op.drop_column('station', 'standby_per_shift')


def downgrade() -> None:
    op.add_column('station', sa.Column('standby_per_shift', sa.Integer(), nullable=False, server_default='0'))

    op.drop_index(op.f('ix_standbyassignment_group'), table_name='standbyassignment')
    op.drop_column('standbyassignment', 'group')

    op.add_column('standbyassignment', sa.Column('station_id', sa.Integer(), nullable=True))
    op.create_index(op.f('ix_standbyassignment_station_id'), 'standbyassignment', ['station_id'], unique=False)
    op.create_foreign_key(None, 'standbyassignment', 'station', ['station_id'], ['id'])

    op.drop_table('standbysettings')
