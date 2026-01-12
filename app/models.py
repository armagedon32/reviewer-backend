from pydantic import BaseModel, EmailStr
from typing import Optional, Literal


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
    student_id: str
    name: str
    course: str

    exam_type: Literal["LET", "CPA"]
    let_track: Optional[Literal["Elementary", "Secondary"]] = None
    let_major: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str




    
class Question(BaseModel):
    id: int
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


