from datetime import datetime, timedelta
from typing import Optional, Literal
import secrets
import string
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, EmailStr
from .auth import get_current_user, hash_password
from .database import get_database
from .db_models import AppSetting, AuditLog, ExamResult, Question, User
from .audit import log_event_async

router = APIRouter(prefix="/admin", tags=["Admin"])

ACCESS_ACTIONS = ("access_request", "access_approved", "access_denied")
REQUEST_TTL_SECONDS = 60 * 60
TEMP_PASSWORD_TTL_MINUTES = 15


class SettingsUpdate(BaseModel):
    exam_time_limit_minutes: int = Field(ge=10, le=240)
    exam_question_count: int = Field(ge=10, le=200)
    exam_major_question_count: int = Field(ge=0, le=200)


class CreateUserRequest(BaseModel):
    email: EmailStr
    role: Literal["student", "instructor", "admin"]
    password: Optional[str] = None
    require_password_change: bool = True


class ResetSelectedExamsRequest(BaseModel):
    user_ids: list[str] = Field(default_factory=list, min_items=1)


def _generate_temp_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def get_or_create_settings(db):
    settings = await db.app_settings.find_one({})
    if settings:
        return settings
    settings_data = {
        "exam_time_limit_minutes": 90,
        "exam_question_count": 50,
        "exam_major_question_count": 50,
    }
    result = await db.app_settings.insert_one(settings_data)
    settings_data["_id"] = result.inserted_id
    return settings_data


@router.get("/settings")
async def get_settings(current_user=Depends(get_current_user), db = Depends(get_database)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    settings = await get_or_create_settings(db)
    return {
        "exam_time_limit_minutes": settings["exam_time_limit_minutes"],
        "exam_question_count": settings["exam_question_count"],
        "exam_major_question_count": settings.get("exam_major_question_count", 50),
    }


@router.get("/settings/public")
async def get_settings_public(
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    settings = await get_or_create_settings(db)
    return {
        "exam_time_limit_minutes": settings["exam_time_limit_minutes"],
        "exam_question_count": settings["exam_question_count"],
        "exam_major_question_count": settings.get("exam_major_question_count", 50),
    }


@router.put("/settings")
async def update_settings(
    payload: SettingsUpdate,
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    total_questions = await db.questions.count_documents({})
    if payload.exam_question_count > total_questions:
        raise HTTPException(
            status_code=400,
            detail=(
                "Exam item count exceeds the available question bank. "
                f"Requested {payload.exam_question_count}, but only {total_questions} available."
            ),
        )
    if payload.exam_major_question_count > total_questions:
        raise HTTPException(
            status_code=400,
            detail=(
                "Major item count exceeds the available question bank. "
                f"Requested {payload.exam_major_question_count}, but only {total_questions} available."
            ),
        )
    settings = await get_or_create_settings(db)
    await db.app_settings.update_one(
        {"_id": settings["_id"]},
        {"$set": {
            "exam_time_limit_minutes": payload.exam_time_limit_minutes,
            "exam_question_count": payload.exam_question_count,
            "exam_major_question_count": payload.exam_major_question_count,
        }}
    )
    await log_event_async(
        db,
        None,
        "settings_update",
        (
            f"Exam timer set to {payload.exam_time_limit_minutes} minutes; "
            f"exam questions set to {payload.exam_question_count}; "
            f"major questions set to {payload.exam_major_question_count}"
        ),
    )
    updated_settings = await db.app_settings.find_one({"_id": settings["_id"]})
    return {
        "exam_time_limit_minutes": updated_settings["exam_time_limit_minutes"],
        "exam_question_count": updated_settings["exam_question_count"],
        "exam_major_question_count": updated_settings.get("exam_major_question_count", 50),
    }


@router.get("/users")
async def list_users(current_user=Depends(get_current_user), db = Depends(get_database)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    users = await db.users.find().sort("created_at", -1).to_list(length=None)
    return [
        {
            "id": str(user["_id"]),
            "email": user["email"],
            "role": user["role"],
            "active": user.get("active", True),
            "created_at": user["created_at"].isoformat(),
        }
        for user in users
    ]


@router.post("/users")
async def create_user(
    payload: CreateUserRequest,
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    existing = await db.users.find_one({"email": payload.email})
    if existing:
        raise HTTPException(status_code=400, detail="User already exists")

    generated_password = None
    expires_at = None
    if payload.password:
        password_to_set = payload.password
        must_change_password = payload.require_password_change
        if must_change_password:
            expires_at = datetime.utcnow() + timedelta(minutes=TEMP_PASSWORD_TTL_MINUTES)
    else:
        generated_password = _generate_temp_password()
        password_to_set = generated_password
        must_change_password = True
        expires_at = datetime.utcnow() + timedelta(minutes=TEMP_PASSWORD_TTL_MINUTES)

    user_data = {
        "email": payload.email,
        "password_hash": hash_password(password_to_set),
        "role": payload.role,
        "active": True,
        "must_change_password": must_change_password,
        "temp_password_expires_at": expires_at,
        "created_at": datetime.utcnow(),
    }
    result = await db.users.insert_one(user_data)
    await log_event_async(db, str(result.inserted_id), "user_create", f"Created {payload.role} {payload.email}")
    response = {
        "id": str(result.inserted_id),
        "email": payload.email,
        "role": payload.role,
        "active": True,
        "created_at": user_data["created_at"].isoformat(),
    }
    if generated_password:
        response["temporary_password"] = generated_password
        response["expires_at"] = expires_at.isoformat() if expires_at else None
    return response


@router.put("/users/{user_id}/status")
async def update_user_status(
    user_id: str,
    active: bool,
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    from bson import ObjectId
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await db.users.update_one({"_id": ObjectId(user_id)}, {"$set": {"active": active}})
    await log_event_async(
        db,
        user_id,
        "user_status",
        f"User {'activated' if active else 'deactivated'}",
    )
    return {"id": user_id, "active": active}


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    from bson import ObjectId
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.get("active", True):
        raise HTTPException(status_code=400, detail="Deactivate user before deleting")
    await db.exam_results.delete_many({"user_id": user_id})
    await db.audit_logs.delete_many({"user_id": user_id})
    await db.users.delete_one({"_id": ObjectId(user_id)})
    await log_event_async(db, None, "user_delete", f"Deleted user {user['email']}")
    return {"deleted": user_id}


@router.delete("/users/{user_id}/exams")
async def reset_user_exams(
    user_id: str,
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    from bson import ObjectId
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    result = await db.exam_results.delete_many({"user_id": user_id})
    await log_event_async(db, user_id, "exam_reset", f"Deleted {result.deleted_count} exam results")
    return {"deleted": result.deleted_count}


@router.delete("/exams/students")
async def reset_student_exams(
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    students = await db.users.find({"role": "student"}).to_list(length=None)
    student_ids = [str(user["_id"]) for user in students]
    if not student_ids:
        return {"deleted": 0}
    result = await db.exam_results.delete_many({"user_id": {"$in": student_ids}})
    await log_event_async(db, None, "exam_reset_bulk", f"Deleted {result.deleted_count} student exam results")
    return {"deleted": result.deleted_count}


@router.post("/exams/students/selected")
async def reset_selected_student_exams(
    payload: ResetSelectedExamsRequest,
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    from bson import ObjectId
    try:
        object_ids = [ObjectId(uid) for uid in payload.user_ids]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid user id list")
    users = await db.users.find({"_id": {"$in": object_ids}, "role": "student"}).to_list(length=None)
    student_ids = [str(user["_id"]) for user in users]
    if not student_ids:
        return {"deleted": 0}
    result = await db.exam_results.delete_many({"user_id": {"$in": student_ids}})
    await log_event_async(
        db,
        None,
        "exam_reset_selected",
        f"Deleted {result.deleted_count} exam results for {len(student_ids)} students",
    )
    return {"deleted": result.deleted_count, "students": len(student_ids)}

@router.post("/users/{user_id}/password-reset")
async def reset_user_password(
    user_id: str,
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    from bson import ObjectId
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    temp_password = _generate_temp_password()
    expires_at = datetime.utcnow() + timedelta(minutes=TEMP_PASSWORD_TTL_MINUTES)
    await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {
            "password_hash": hash_password(temp_password),
            "must_change_password": True,
            "temp_password_expires_at": expires_at
        }}
    )
    await log_event_async(
        db,
        user_id,
        "password_reset_issued",
        f"Reset by {current_user['email']}; expires {expires_at.isoformat()}",
    )
    return {
        "temporary_password": temp_password,
        "expires_at": expires_at.isoformat(),
    }


@router.get("/audit-logs")
async def list_audit_logs(current_user=Depends(get_current_user), db = Depends(get_database)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    logs = await db.audit_logs.find().sort("created_at", -1).limit(100).to_list(length=100)
    return [
        {
            "id": str(log["_id"]),
            "user_id": log.get("user_id"),
            "action": log["action"],
            "detail": log["detail"],
            "created_at": log["created_at"].isoformat(),
        }
        for log in logs
    ]


@router.get("/access-requests")
async def list_access_requests(current_user=Depends(get_current_user), db = Depends(get_database)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    cutoff = datetime.utcnow() - timedelta(seconds=REQUEST_TTL_SECONDS)
    logs = await db.audit_logs.find({
        "action": {"$in": list(ACCESS_ACTIONS)},
        "created_at": {"$gte": cutoff}
    }).sort("created_at", -1).to_list(length=None)
    latest_by_user = {}
    for log in logs:
        uid = log.get("user_id")
        if uid and uid not in latest_by_user:
            latest_by_user[uid] = log
    requests = []
    for user_id, log in latest_by_user.items():
        if log["action"] != "access_request":
            continue
        user = await db.users.find_one({"_id": ObjectId(user_id)})
        if not user:
            continue
        requests.append(
            {
                "id": str(user["_id"]),
                "email": user["email"],
                "role": user["role"],
                "requested_at": log["created_at"].isoformat(),
            }
        )
    return requests


@router.get("/access-statuses")
async def list_access_statuses(current_user=Depends(get_current_user), db = Depends(get_database)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    users = await db.users.find().to_list(length=None)
    logs = await db.audit_logs.find({"action": {"$in": list(ACCESS_ACTIONS)}}).sort("created_at", -1).to_list(length=None)
    latest_by_user = {}
    for log in logs:
        uid = log.get("user_id")
        if uid and uid not in latest_by_user:
            latest_by_user[uid] = log
    cutoff = datetime.utcnow() - timedelta(seconds=REQUEST_TTL_SECONDS)
    statuses = []
    for user in users:
        uid = str(user["_id"])
        if user["role"] == "admin":
            statuses.append({"id": uid, "status": "approved"})
            continue
        latest = latest_by_user.get(uid)
        if not latest:
            statuses.append({"id": uid, "status": "pending"})
            continue
        if latest["action"] == "access_approved":
            statuses.append({"id": uid, "status": "approved"})
            continue
        if latest["action"] == "access_denied":
            statuses.append({"id": uid, "status": "denied"})
            continue
        if latest["action"] == "access_request":
            if latest["created_at"] < cutoff:
                statuses.append(
                    {"id": uid, "status": "expired", "detail": latest.get("detail")}
                )
            else:
                statuses.append(
                    {"id": uid, "status": "pending", "detail": latest.get("detail")}
                )
            continue
        statuses.append({"id": uid, "status": "pending"})
    return statuses


@router.post("/access-requests/{user_id}/approve")
async def approve_access_request(
    user_id: str,
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    from bson import ObjectId
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await db.users.update_one({"_id": ObjectId(user_id)}, {"$set": {"active": True}})
    await log_event_async(db, user_id, "access_approved", "Access approved")
    return {"id": user_id, "status": "approved"}


@router.post("/access-requests/{user_id}/deny")
async def deny_access_request(
    user_id: str,
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    from bson import ObjectId
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await db.users.update_one({"_id": ObjectId(user_id)}, {"$set": {"active": False}})
    await log_event_async(db, user_id, "access_denied", "Access denied")
    return {"id": user_id, "status": "denied"}
