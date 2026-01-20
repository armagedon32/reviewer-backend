from pydantic import BaseModel, EmailStr, Field
from typing import Optional, Literal, List


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    role: str

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class AdminRegisterRequest(BaseModel):
    email: EmailStr
    password: str
    admin_key: str

class StudentProfile(BaseModel):
    student_id_number: str
    first_name: str
    middle_name: Optional[str] = None
    last_name: str
    email_address: EmailStr
    username: str
    program_degree: str
    year_level: Optional[int] = Field(default=None, ge=1, le=6)
    section_class: Optional[str] = None
    status: Literal["Active", "Inactive", "Graduated"]
    target_licensure: Literal["LET", "CPA", "Internal Certification"]
    let_track: Optional[Literal["Elementary", "Secondary"]] = None
    major_specialization: str
    assigned_review_subjects: List[str]
    required_passing_threshold: int = Field(ge=1, le=100)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str




    
class Question(BaseModel):
    id: str
    exam_type: str            # LET or CPA
    subject: str              # GenEd, Math, FAR, AFAR, etc.
    topic: str
    difficulty: str           # Easy, Medium, Hard
    question: str
    a: str
    b: str
    c: str
    d: str
    answer: str               # A, B, C, or D
