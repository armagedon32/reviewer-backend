from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from .models import StudentProfile as StudentProfileSchema
from .auth import get_current_user
from .database import get_db
from .db_models import StudentProfile, User
from .audit import log_event

router = APIRouter(prefix="/profile", tags=["Profile"])


def profile_to_dict(profile: StudentProfile):
    return {
        "student_id": profile.student_id,
        "name": profile.name,
        "course": profile.course,
        "exam_type": profile.exam_type,
        "let_track": profile.let_track,
        "let_major": profile.let_major,
    }


@router.get("")
def get_profile(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == current_user["email"]).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    profile = db.query(StudentProfile).filter(StudentProfile.user_id == user.id).first()
    return profile_to_dict(profile) if profile else None


@router.post("")
def save_profile(
    profile: StudentProfileSchema,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == current_user["email"]).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    if profile.exam_type == "LET":
        if profile.let_track not in ["Elementary", "Secondary"]:
            raise HTTPException(status_code=400, detail="LET track is required")
        if profile.let_track == "Secondary" and not profile.let_major:
            raise HTTPException(
                status_code=400, detail="LET major is required for Secondary"
            )

    if profile.exam_type == "CPA":
        profile.let_track = None
        profile.let_major = None

    existing = db.query(StudentProfile).filter(StudentProfile.user_id == user.id).first()
    if existing:
        existing.student_id = profile.student_id
        existing.name = profile.name
        existing.course = profile.course
        existing.exam_type = profile.exam_type
        existing.let_track = profile.let_track
        existing.let_major = profile.let_major
        existing.updated_at = datetime.utcnow()
        saved = existing
    else:
        saved = StudentProfile(
            user_id=user.id,
            student_id=profile.student_id,
            name=profile.name,
            course=profile.course,
            exam_type=profile.exam_type,
            let_track=profile.let_track,
            let_major=profile.let_major,
            updated_at=datetime.utcnow(),
        )
        db.add(saved)

    db.commit()
    db.refresh(saved)
    log_event(db, user.id, "profile_save", "Student profile saved")

    return profile_to_dict(saved)

