"""rosterrun date range instead of year month

Revision ID: a919f6b16ff5
Revises: 1d9547d55147
Create Date: 2026-08-11 13:00:22.893692

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'a919f6b16ff5'
down_revision: Union[str, None] = '1d9547d55147'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add nullable first so we can backfill existing rows from year/month
    # before enforcing NOT NULL — this table may already have real data.
    op.add_column('rosterrun', sa.Column('start_date', sa.Date(), nullable=True))
    op.add_column('rosterrun', sa.Column('end_date', sa.Date(), nullable=True))
    op.add_column('rosterrun', sa.Column('range_type', sqlmodel.sql.sqltypes.AutoString(), nullable=True))

    # date-construction functions/casts aren't portable across dialects
    dialect = op.get_bind().dialect.name
    if dialect == 'mysql':
        op.execute("""
            UPDATE rosterrun SET
                start_date = STR_TO_DATE(CONCAT(year, '-', month, '-01'), '%Y-%m-%d'),
                end_date = LAST_DAY(STR_TO_DATE(CONCAT(year, '-', month, '-01'), '%Y-%m-%d')),
                range_type = 'month'
        """)
    else:
        op.execute("""
            UPDATE rosterrun SET
                start_date = make_date(year, month, 1),
                end_date = (make_date(year, month, 1) + interval '1 month - 1 day')::date,
                range_type = 'month'
        """)

    # MySQL's ALTER COLUMN needs the existing type restated explicitly (unlike
    # Postgres, which can change nullability alone) — harmless no-op for Postgres.
    op.alter_column('rosterrun', 'start_date', existing_type=sa.Date(), nullable=False)
    op.alter_column('rosterrun', 'end_date', existing_type=sa.Date(), nullable=False)
    op.alter_column('rosterrun', 'range_type', existing_type=sqlmodel.sql.sqltypes.AutoString(), nullable=False)

    op.drop_column('rosterrun', 'year')
    op.drop_column('rosterrun', 'month')


def downgrade() -> None:
    op.add_column('rosterrun', sa.Column('month', sa.INTEGER(), autoincrement=False, nullable=True))
    op.add_column('rosterrun', sa.Column('year', sa.INTEGER(), autoincrement=False, nullable=True))

    dialect = op.get_bind().dialect.name
    if dialect == 'mysql':
        op.execute("""
            UPDATE rosterrun SET
                year = YEAR(start_date),
                month = MONTH(start_date)
        """)
    else:
        op.execute("""
            UPDATE rosterrun SET
                year = EXTRACT(YEAR FROM start_date)::int,
                month = EXTRACT(MONTH FROM start_date)::int
        """)

    op.alter_column('rosterrun', 'year', existing_type=sa.INTEGER(), nullable=False)
    op.alter_column('rosterrun', 'month', existing_type=sa.INTEGER(), nullable=False)

    op.drop_column('rosterrun', 'range_type')
    op.drop_column('rosterrun', 'end_date')
    op.drop_column('rosterrun', 'start_date')
