"""add password reset and instructor profiles

Revision ID: 20260111_add_password_reset_and_instructor_profiles
Revises: 20260109_add_admin_features
Create Date: 2026-01-11 00:00:00
"""
from alembic import op

revision = "20260111_add_password_reset_and_instructor_profiles"
down_revision = "20260109_add_admin_features"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE;
        """
    )
    op.execute(
        """
        ALTER TABLE users
        ADD COLUMN IF NOT EXISTS temp_password_expires_at TIMESTAMP NULL;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS instructor_profiles (
          id SERIAL PRIMARY KEY,
          user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
          employee_id VARCHAR NOT NULL,
          name VARCHAR NOT NULL,
          department VARCHAR NOT NULL,
          position VARCHAR NOT NULL,
          program VARCHAR NOT NULL,
          updated_at TIMESTAMP NOT NULL
        );
        """
    )


def downgrade():
    op.execute("DROP TABLE IF EXISTS instructor_profiles;")
    op.execute(
        "ALTER TABLE users DROP COLUMN IF EXISTS temp_password_expires_at;"
    )
    op.execute(
        "ALTER TABLE users DROP COLUMN IF EXISTS must_change_password;"
    )
