"""notes and notification body as text not varchar

Revision ID: d19d9b0e2639
Revises: dfe883a562fa
Create Date: 2026-08-23 20:33:15.530273

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'd19d9b0e2639'
down_revision: Union[str, None] = 'dfe883a562fa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # notes/body were an unbounded string under Postgres's forgiving varchar,
    # but MySQL enforces the default 255-char cap — notes in particular
    # routinely holds a multi-KB JSON coverage/quality blob. TEXT removes
    # the cap on both dialects.
    op.alter_column('rosterrun', 'notes',
                     existing_type=sqlmodel.sql.sqltypes.AutoString(),
                     type_=sa.Text(), existing_nullable=True)
    op.alter_column('notification', 'body',
                     existing_type=sqlmodel.sql.sqltypes.AutoString(),
                     type_=sa.Text(), existing_nullable=False)


def downgrade() -> None:
    op.alter_column('notification', 'body',
                     existing_type=sa.Text(),
                     type_=sqlmodel.sql.sqltypes.AutoString(), existing_nullable=False)
    op.alter_column('rosterrun', 'notes',
                     existing_type=sa.Text(),
                     type_=sqlmodel.sql.sqltypes.AutoString(), existing_nullable=True)
