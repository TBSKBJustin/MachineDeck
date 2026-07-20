"""Add single-administrator authentication and server sessions.

Revision ID: 0003
Revises: 0002
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "administrators",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("singleton_key", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("singleton_key", name="uq_administrators_singleton"),
        sa.UniqueConstraint("username"),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("administrator_id", sa.String(length=36), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("csrf_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["administrator_id"], ["administrators.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_auth_sessions_administrator_id", "auth_sessions", ["administrator_id"])
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])
    op.create_index("ix_auth_sessions_token_digest", "auth_sessions", ["token_digest"], unique=True)
    op.create_table(
        "login_failures",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("remote_digest", sa.String(length=64), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_login_failures_attempted_at", "login_failures", ["attempted_at"])
    op.create_index("ix_login_failures_remote_digest", "login_failures", ["remote_digest"])


def downgrade() -> None:
    op.drop_index("ix_login_failures_remote_digest", table_name="login_failures")
    op.drop_index("ix_login_failures_attempted_at", table_name="login_failures")
    op.drop_table("login_failures")
    op.drop_index("ix_auth_sessions_token_digest", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_expires_at", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_administrator_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_table("administrators")
