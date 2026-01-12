from datetime import datetime, timedelta
import secrets
import string
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from .auth import get_current_user, hash_password
from .database import get_db
from .db_models import AppSetting, AuditLog, ExamResult, Question, User
from .audit import log_event

router = APIRouter(prefix="/admin", tags=["Admin"])

ACCESS_ACTIONS = ("access_request", "access_approved", "access_denied")
REQUEST_TTL_SECONDS = 60 * 60
TEMP_PASSWORD_TTL_MINUTES = 15


class SettingsUpdate(BaseModel):
    exam_time_limit_minutes: int = Field(ge=10, le=240)
    exam_question_count: int = Field(ge=10, le=200)


def _generate_temp_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def get_or_create_settings(db: Session) -> AppSetting:
    settings = db.query(AppSetting).first()
    if settings:
        return settings
    settings = AppSetting(exam_time_limit_minutes=90, exam_question_count=50)
    db.add(settings)
    db.commit()
    db.refresh(settings)
    return settings


@router.get("/settings")
def get_settings(current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    settings = get_or_create_settings(db)
    return {
        "exam_time_limit_minutes": settings.exam_time_limit_minutes,
        "exam_question_count": settings.exam_question_count,
    }


@router.get("/settings/public")
def get_settings_public(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings = get_or_create_settings(db)
    return {
        "exam_time_limit_minutes": settings.exam_time_limit_minutes,
        "exam_question_count": settings.exam_question_count,
    }


@router.put("/settings")
def update_settings(
    payload: SettingsUpdate,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    total_questions = db.query(Question).count()
    if payload.exam_question_count > total_questions:
        raise HTTPException(
            status_code=400,
            detail=(
                "Exam item count exceeds the available question bank. "
                f"Requested {payload.exam_question_count}, but only {total_questions} available."
            ),
        )
    settings = get_or_create_settings(db)
    settings.exam_time_limit_minutes = payload.exam_time_limit_minutes
    settings.exam_question_count = payload.exam_question_count
    db.commit()
    log_event(
        db,
        None,
        "settings_update",
        (
            f"Exam timer set to {payload.exam_time_limit_minutes} minutes; "
            f"exam questions set to {payload.exam_question_count}"
        ),
    )
    return {
        "exam_time_limit_minutes": settings.exam_time_limit_minutes,
        "exam_question_count": settings.exam_question_count,
    }


@router.get("/users")
def list_users(current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    users = db.query(User).order_by(User.created_at.desc()).all()
    return [
        {
            "id": user.id,
            "email": user.email,
            "role": user.role,
            "active": user.active,
            "created_at": user.created_at.isoformat(),
        }
        for user in users
    ]


@router.put("/users/{user_id}/status")
def update_user_status(
    user_id: int,
    active: bool,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.active = active
    db.commit()
    log_event(
        db,
        user.id,
        "user_status",
        f"User {'activated' if active else 'deactivated'}",
    )
    return {"id": user.id, "active": user.active}


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.active:
        raise HTTPException(status_code=400, detail="Deactivate user before deleting")
    db.query(ExamResult).filter(ExamResult.user_id == user.id).delete()
    db.query(AuditLog).filter(AuditLog.user_id == user.id).delete()
    db.delete(user)
    db.commit()
    log_event(db, None, "user_delete", f"Deleted user {user.email}")
    return {"deleted": user_id}


@router.delete("/users/{user_id}/exams")
def reset_user_exams(
    user_id: int,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    deleted = db.query(ExamResult).filter(ExamResult.user_id == user.id).delete()
    db.commit()
    log_event(db, user.id, "exam_reset", f"Deleted {deleted} exam results")
    return {"deleted": deleted}


@router.post("/users/{user_id}/password-reset")
def reset_user_password(
    user_id: int,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    temp_password = _generate_temp_password()
    user.password_hash = hash_password(temp_password)
    user.must_change_password = True
    user.temp_password_expires_at = datetime.utcnow() + timedelta(
        minutes=TEMP_PASSWORD_TTL_MINUTES
    )
    db.commit()
    log_event(
        db,
        user.id,
        "password_reset_issued",
        f"Reset by {current_user['email']}; expires {user.temp_password_expires_at.isoformat()}",
    )
    return {
        "temporary_password": temp_password,
        "expires_at": user.temp_password_expires_at.isoformat(),
    }






@router.get("/audit-logs")
def list_audit_logs(current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    logs = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(100).all()
    return [
        {
            "id": log.id,
            "user_id": log.user_id,
            "action": log.action,
            "detail": log.detail,
            "created_at": log.created_at.isoformat(),
        }
        for log in logs
    ]


@router.get("/access-requests")
def list_access_requests(current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    cutoff = datetime.utcnow() - timedelta(seconds=REQUEST_TTL_SECONDS)
    logs = (
        db.query(AuditLog)
        .filter(AuditLog.action.in_(ACCESS_ACTIONS), AuditLog.created_at >= cutoff)
        .order_by(AuditLog.created_at.desc())
        .all()
    )
    latest_by_user = {}
    for log in logs:
        if log.user_id not in latest_by_user:
            latest_by_user[log.user_id] = log
    requests = []
    for user_id, log in latest_by_user.items():
        if log.action != "access_request":
            continue
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            continue
        requests.append(
            {
                "id": user.id,
                "email": user.email,
                "role": user.role,
                "requested_at": log.created_at.isoformat(),
            }
        )
    return requests


@router.get("/access-statuses")
def list_access_statuses(current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    users = db.query(User).all()
    logs = (
        db.query(AuditLog)
        .filter(AuditLog.action.in_(ACCESS_ACTIONS))
        .order_by(AuditLog.created_at.desc())
        .all()
    )
    latest_by_user = {}
    for log in logs:
        if log.user_id not in latest_by_user:
            latest_by_user[log.user_id] = log
    cutoff = datetime.utcnow() - timedelta(seconds=REQUEST_TTL_SECONDS)
    statuses = []
    for user in users:
        if user.role == "admin":
            statuses.append({"id": user.id, "status": "approved"})
            continue
        latest = latest_by_user.get(user.id)
        if not latest:
            statuses.append({"id": user.id, "status": "pending"})
            continue
        if latest.action == "access_approved":
            statuses.append({"id": user.id, "status": "approved"})
            continue
        if latest.action == "access_denied":
            statuses.append({"id": user.id, "status": "denied"})
            continue
        if latest.action == "access_request":
            if latest.created_at < cutoff:
                statuses.append(
                    {"id": user.id, "status": "expired", "detail": latest.detail}
                )
            else:
                statuses.append(
                    {"id": user.id, "status": "pending", "detail": latest.detail}
                )
            continue
        statuses.append({"id": user.id, "status": "pending"})
    return statuses


@router.post("/access-requests/{user_id}/approve")
def approve_access_request(
    user_id: int,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.active = True
    db.commit()
    log_event(db, user.id, "access_approved", "Access approved")
    return {"id": user.id, "status": "approved"}


@router.post("/access-requests/{user_id}/deny")
def deny_access_request(
    user_id: int,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.active = False
    db.commit()
    log_event(db, user.id, "access_denied", "Access denied")
    return {"id": user.id, "status": "denied"}
