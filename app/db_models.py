from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import relationship
from .database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(String, nullable=False)
    active = Column(Boolean, nullable=False, default=True)
    must_change_password = Column(Boolean, nullable=False, default=False)
    temp_password_expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    profile = relationship("StudentProfile", back_populates="user", uselist=False)
    exam_results = relationship("ExamResult", back_populates="user")


class StudentProfile(Base):
    __tablename__ = "student_profiles"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    student_id = Column(String, nullable=False)
    name = Column(String, nullable=False)
    course = Column(String, nullable=False)
    exam_type = Column(String, nullable=False)
    let_track = Column(String, nullable=True)
    let_major = Column(String, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="profile")



class Question(Base):
    __tablename__ = "questions"

    id = Column(Integer, primary_key=True)
    exam_type = Column(String, nullable=False)
    subject = Column(String, nullable=False)
    topic = Column(String, nullable=False)
    difficulty = Column(String, nullable=False)
    question = Column(String, nullable=False)
    a = Column(String, nullable=False)
    b = Column(String, nullable=False)
    c = Column(String, nullable=False)
    d = Column(String, nullable=False)
    answer = Column(String, nullable=False)


class ExamResult(Base):
    __tablename__ = "exam_results"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    exam_type = Column(String, nullable=False)
    score = Column(Integer, nullable=False)
    total = Column(Integer, nullable=False)
    percentage = Column(Float, nullable=False)
    result = Column(String, nullable=False)
    subject_performance = Column(JSON, nullable=False)
    incorrect_questions = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="exam_results")


class AppSetting(Base):
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True)
    exam_time_limit_minutes = Column(Integer, nullable=False, default=90)
    exam_question_count = Column(Integer, nullable=False, default=50)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String, nullable=False)
    detail = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
