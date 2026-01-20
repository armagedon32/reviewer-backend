from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from bson import ObjectId


class PyObjectId(ObjectId):
    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    @classmethod
    def validate(cls, v):
        if not ObjectId.is_valid(v):
            raise ValueError("Invalid objectid")
        return ObjectId(v)

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        return {"type": "string"}


class User(BaseModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    email: str
    password_hash: str
    role: str
    active: bool = True
    must_change_password: bool = False
    temp_password_expires_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        populate_by_name = True
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}


class StudentProfile(BaseModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    user_id: str  # ObjectId as string
    student_id_number: str
    first_name: str
    middle_name: Optional[str] = None
    last_name: str
    email_address: str
    username: str
    program_degree: str
    year_level: Optional[int] = None
    section_class: Optional[str] = None
    status: str
    target_licensure: str
    let_track: Optional[str] = None
    major_specialization: str
    assigned_review_subjects: List[str]
    required_passing_threshold: int
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        populate_by_name = True
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}


class Question(BaseModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    exam_type: str
    subject: str
    topic: str
    difficulty: str
    question: str
    a: str
    b: str
    c: str
    d: str
    answer: str

    class Config:
        populate_by_name = True
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}


class ExamResult(BaseModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    user_id: str  # ObjectId as string
    exam_type: str
    score: int
    total: int
    percentage: float
    result: str
    subject_performance: Dict[str, Any]
    incorrect_questions: List[Dict[str, Any]]
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        populate_by_name = True
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}


class AppSetting(BaseModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    exam_time_limit_minutes: int = 90
    exam_question_count: int = 50

    class Config:
        populate_by_name = True
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}


class AuditLog(BaseModel):
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    user_id: Optional[str] = None  # ObjectId as string
    action: str
    detail: str
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        populate_by_name = True
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}
