"""add users is_admin

Revision ID: f91c0e2a7b44
Revises: d8b21f4c6a30
Create Date: 2026-10-08 15:40:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f91c0e2a7b44"
down_revision: Union[str, Sequence[str], None] = "d8b21f4c6a30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "is_admin",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "is_admin")
