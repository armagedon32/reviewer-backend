"""add admin features

Revision ID: 20260109_add_admin_features
Revises: 
Create Date: 2026-01-09 00:00:00
"""
from alembic import op

revision = "20260109_add_admin_features"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
          id SERIAL PRIMARY KEY,
          email VARCHAR NOT NULL UNIQUE,
          password_hash VARCHAR NOT NULL,
          role VARCHAR NOT NULL,
          active BOOLEAN NOT NULL DEFAULT TRUE,
          created_at TIMESTAMP NOT NULL
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS student_profiles (
          id SERIAL PRIMARY KEY,
          user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
          student_id VARCHAR NOT NULL,
          name VARCHAR NOT NULL,
          course VARCHAR NOT NULL,
          exam_type VARCHAR NOT NULL,
          let_track VARCHAR NULL,
          let_major VARCHAR NULL,
          updated_at TIMESTAMP NOT NULL
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS questions (
          id SERIAL PRIMARY KEY,
          exam_type VARCHAR NOT NULL,
          subject VARCHAR NOT NULL,
          topic VARCHAR NOT NULL,
          difficulty VARCHAR NOT NULL,
          question VARCHAR NOT NULL,
          a VARCHAR NOT NULL,
          b VARCHAR NOT NULL,
          c VARCHAR NOT NULL,
          d VARCHAR NOT NULL,
          answer VARCHAR NOT NULL
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS exam_results (
          id SERIAL PRIMARY KEY,
          user_id INTEGER NOT NULL REFERENCES users(id),
          exam_type VARCHAR NOT NULL,
          score INTEGER NOT NULL,
          total INTEGER NOT NULL,
          percentage FLOAT NOT NULL,
          result VARCHAR NOT NULL,
          subject_performance JSON NOT NULL,
          incorrect_questions JSON NOT NULL,
          created_at TIMESTAMP NOT NULL
        );
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
          id SERIAL PRIMARY KEY,
          exam_time_limit_minutes INTEGER NOT NULL DEFAULT 90,
          exam_question_count INTEGER NOT NULL DEFAULT 50
        );
        """
    )
    op.execute(
        """
        ALTER TABLE app_settings
        ADD COLUMN IF NOT EXISTS exam_question_count INTEGER NOT NULL DEFAULT 50;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
          id SERIAL PRIMARY KEY,
          user_id INTEGER REFERENCES users(id),
          action VARCHAR NOT NULL,
          detail VARCHAR NOT NULL,
          created_at TIMESTAMP NOT NULL
        );
        """
    )
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;")


def downgrade():
    op.execute("DROP TABLE IF EXISTS audit_logs;")
    op.execute("DROP TABLE IF EXISTS app_settings;")
    op.execute("DROP TABLE IF EXISTS exam_results;")
    op.execute("DROP TABLE IF EXISTS questions;")
    op.execute("DROP TABLE IF EXISTS student_profiles;")
    op.execute("DROP TABLE IF EXISTS users;")
