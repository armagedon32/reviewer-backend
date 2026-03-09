from datetime import datetime, timedelta
from typing import Optional, Literal
import secrets
import string
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, EmailStr
from bson import ObjectId
from .auth import get_current_user, hash_password
from .database import get_database
from .db_models import AppSetting, AuditLog, ExamResult, Question, User
from .audit import log_event_async
from .licensure import DEFAULT_TARGET_LICENSURE_OPTIONS

router = APIRouter(prefix="/admin", tags=["Admin"])

ACCESS_ACTIONS = ("access_request", "access_approved", "access_denied")
REQUEST_TTL_SECONDS = 60 * 60
TEMP_PASSWORD_TTL_MINUTES = 15
BACKUP_TIMESTAMP_FMT = "%Y%m%d-%H%M%S"

BACKEND_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = BACKEND_ROOT.parent
FRONTEND_ROOT = WORKSPACE_ROOT / "reviewer-ui"
BACKUP_ROOT = BACKEND_ROOT / "backups"


def _database_name() -> str:
    return os.getenv("DATABASE_NAME", "reviewer_ui")


def _mongo_uri() -> str:
    return os.getenv("MONGODB_URL", "mongodb://localhost:27017")


def _safe_timestamp() -> str:
    return datetime.utcnow().strftime(BACKUP_TIMESTAMP_FMT)


def _find_mongo_tool(tool: str) -> str:
    env_key = f"{tool.upper()}_PATH"
    env_path = os.getenv(env_key)
    if env_path and Path(env_path).exists():
        return env_path

    direct = shutil.which(tool)
    if direct:
        return direct

    candidates = []
    # Common Linux locations (Railway, Docker, etc.)
    for base in ("/usr/local/bin", "/usr/bin", "/bin"):
        candidate = Path(base) / tool
        if candidate.exists():
            candidates.append(candidate)

    tools_root = Path("C:/Program Files/MongoDB/Tools")
    if tools_root.exists():
        candidates.extend(tools_root.glob(f"*/bin/{tool}.exe"))
    server_root = Path("C:/Program Files/MongoDB/Server")
    if server_root.exists():
        candidates.extend(server_root.glob(f"*/bin/{tool}.exe"))

    if not candidates:
        raise HTTPException(
            status_code=500,
            detail=f"{tool} was not found. Install MongoDB Database Tools.",
        )

    candidates = sorted(candidates, key=lambda item: str(item), reverse=True)
    return str(candidates[0])


def _zip_directory(source_dir: Path, output_zip_path: Path):
    with zipfile.ZipFile(output_zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in source_dir.rglob("*"):
            if path.is_file():
                archive.write(path, arcname=path.relative_to(source_dir))


def _zip_system(output_zip_path: Path):
    skip_dirs = {"node_modules", ".venv", "__pycache__", ".git", "backups", ".vite"}
    roots = [
        ("reviewer-backend", BACKEND_ROOT),
        ("reviewer-ui", FRONTEND_ROOT),
    ]

    with zipfile.ZipFile(output_zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for prefix, root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                rel = path.relative_to(root)
                parts = set(rel.parts)
                if parts.intersection(skip_dirs):
                    continue
                if path.is_file():
                    archive.write(path, arcname=Path(prefix) / rel)


class SettingsUpdate(BaseModel):
    exam_time_limit_minutes: int = Field(ge=10, le=240)
    exam_question_count: int = Field(ge=10, le=200)
    exam_major_question_count: int = Field(ge=0, le=200)
    passing_threshold_default: int = Field(ge=1, le=100)
    mastery_threshold: int = Field(ge=1, le=100)
    target_licensure_options: list[dict] = Field(default_factory=list)
    rl_enabled: bool = False


class CreateUserRequest(BaseModel):
    email: EmailStr
    role: Literal["student", "instructor", "admin"]
    password: Optional[str] = None
    require_password_change: bool = True


class ResetSelectedExamsRequest(BaseModel):
    user_ids: list[str] = Field(default_factory=list, min_items=1)


class CertificationApprovalRequest(BaseModel):
    override: bool = False


class ProfileEditPermissionRequest(BaseModel):
    allowed: bool = True


def _generate_temp_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def get_or_create_settings(db):
    settings = await db.app_settings.find_one({})
    if settings:
        needs_update = False
        if "passing_threshold_default" not in settings:
            settings["passing_threshold_default"] = 75
            needs_update = True
        if "mastery_threshold" not in settings:
            settings["mastery_threshold"] = 90
            needs_update = True
        if (
            "target_licensure_options" not in settings
            or not isinstance(settings.get("target_licensure_options"), list)
            or not settings.get("target_licensure_options")
        ):
            settings["target_licensure_options"] = DEFAULT_TARGET_LICENSURE_OPTIONS
            needs_update = True
        else:
            merged_options = _merge_default_licensure_options(settings["target_licensure_options"])
            if len(merged_options) != len(settings["target_licensure_options"]):
                settings["target_licensure_options"] = merged_options
                needs_update = True
        if "rl_enabled" not in settings:
            settings["rl_enabled"] = False
            needs_update = True
        if needs_update:
            await db.app_settings.update_one(
                {"_id": settings["_id"]},
                {"$set": {
                    "passing_threshold_default": settings["passing_threshold_default"],
                    "mastery_threshold": settings["mastery_threshold"],
                    "target_licensure_options": settings["target_licensure_options"],
                    "rl_enabled": settings["rl_enabled"],
                }},
            )
        return settings
    settings_data = {
        "exam_time_limit_minutes": 90,
        "exam_question_count": 50,
        "exam_major_question_count": 50,
        "passing_threshold_default": 75,
        "mastery_threshold": 90,
        "target_licensure_options": DEFAULT_TARGET_LICENSURE_OPTIONS,
        "rl_enabled": False,
    }
    result = await db.app_settings.insert_one(settings_data)
    settings_data["_id"] = result.inserted_id
    return settings_data


def _normalize_licensure_options(options: list[dict]) -> list[dict]:
    if not options:
        return DEFAULT_TARGET_LICENSURE_OPTIONS

    cleaned = []
    seen_names = set()
    for option in options:
        name = str(option.get("name", "")).strip()
        raw_subjects = option.get("subjects", [])
        subjects = []
        for subject in raw_subjects:
            subject_name = str(subject).strip()
            if subject_name:
                subjects.append(subject_name)
        passing_threshold = int(option.get("passing_threshold", 75))

        if not name:
            raise HTTPException(status_code=400, detail="Licensure name is required")
        key = name.lower()
        if key in seen_names:
            raise HTTPException(status_code=400, detail=f"Duplicate licensure: {name}")
        seen_names.add(key)
        if not subjects:
            raise HTTPException(status_code=400, detail=f"Licensure '{name}' must have at least one subject")
        if passing_threshold < 1 or passing_threshold > 100:
            raise HTTPException(status_code=400, detail=f"Licensure '{name}' threshold must be 1-100")

        cleaned.append(
            {
                "name": name,
                "subjects": subjects,
                "passing_threshold": passing_threshold,
            }
        )

    return cleaned


def _merge_default_licensure_options(existing: list[dict]) -> list[dict]:
    merged = list(existing or [])
    existing_names = {
        str(item.get("name", "")).strip().lower()
        for item in merged
        if isinstance(item, dict)
    }
    for default_option in DEFAULT_TARGET_LICENSURE_OPTIONS:
        default_name = str(default_option.get("name", "")).strip().lower()
        if default_name and default_name not in existing_names:
            merged.append(default_option)
    return merged


def _generate_certificate_id(seed: str) -> str:
    compact = "".join(ch for ch in seed.upper() if ch.isalnum())
    suffix = compact[-10:] if compact else secrets.token_hex(5).upper()
    return f"RUI-{suffix}"


def _generate_verification_code() -> str:
    return f"VRF-{secrets.token_hex(6).upper()}"


def _safe_object_id(value: str):
    if not ObjectId.is_valid(value):
        raise HTTPException(status_code=400, detail="Invalid identifier")
    return ObjectId(value)


def _to_iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else None


async def _student_cert_snapshot(db, user: dict, settings: dict) -> dict:
    profile = await db.student_profiles.find_one({"user_id": str(user["_id"])})
    if not profile:
        return {}

    target = str(profile.get("target_licensure") or "").strip().upper()
    threshold = int(
        profile.get("required_passing_threshold")
        or settings.get("passing_threshold_default")
        or 75
    )
    exam_results = await db.exam_results.find(
        {"user_id": str(user["_id"])}
    ).sort("created_at", -1).to_list(length=200)
    relevant = [
        row for row in exam_results if str(row.get("exam_type") or "").strip().upper() == target
    ]
    if not relevant:
        relevant = exam_results

    percentages = [float(row.get("percentage") or 0) for row in relevant]
    average_score = round(sum(percentages) / len(percentages), 2) if percentages else 0.0
    latest_score = percentages[0] if percentages else 0.0
    highest_score = max(percentages) if percentages else 0.0

    consecutive_passes = 0
    for row in relevant:
        if float(row.get("percentage") or 0) >= threshold:
            consecutive_passes += 1
        else:
            break

    eligible = consecutive_passes >= 3
    full_name = " ".join(
        part for part in [
            profile.get("first_name", ""),
            profile.get("middle_name", ""),
            profile.get("last_name", ""),
        ] if str(part).strip()
    ).strip()
    if not full_name:
        full_name = profile.get("username") or user.get("email") or "Learner"

    return {
        "user_id": str(user["_id"]),
        "learner_name": full_name,
        "email": user.get("email"),
        "category": profile.get("target_licensure") or "N/A",
        "required_threshold": threshold,
        "attempt_count": len(relevant),
        "latest_score": round(latest_score, 2),
        "highest_score": round(highest_score, 2),
        "average_score": average_score,
        "consecutive_passes": consecutive_passes,
        "eligible": eligible,
    }


@router.get("/settings")
async def get_settings(current_user=Depends(get_current_user), db = Depends(get_database)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    settings = await get_or_create_settings(db)
    return {
        "exam_time_limit_minutes": settings["exam_time_limit_minutes"],
        "exam_question_count": settings["exam_question_count"],
        "exam_major_question_count": settings.get("exam_major_question_count", 50),
        "passing_threshold_default": settings.get("passing_threshold_default", 75),
        "mastery_threshold": settings.get("mastery_threshold", 90),
        "target_licensure_options": settings.get(
            "target_licensure_options", DEFAULT_TARGET_LICENSURE_OPTIONS
        ),
        "rl_enabled": bool(settings.get("rl_enabled", False)),
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
        "passing_threshold_default": settings.get("passing_threshold_default", 75),
        "mastery_threshold": settings.get("mastery_threshold", 90),
        "target_licensure_options": settings.get(
            "target_licensure_options", DEFAULT_TARGET_LICENSURE_OPTIONS
        ),
        "rl_enabled": bool(settings.get("rl_enabled", False)),
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
    target_licensure_options = _normalize_licensure_options(payload.target_licensure_options)
    settings = await get_or_create_settings(db)
    await db.app_settings.update_one(
        {"_id": settings["_id"]},
        {"$set": {
            "exam_time_limit_minutes": payload.exam_time_limit_minutes,
            "exam_question_count": payload.exam_question_count,
            "exam_major_question_count": payload.exam_major_question_count,
            "passing_threshold_default": payload.passing_threshold_default,
            "mastery_threshold": payload.mastery_threshold,
            "target_licensure_options": target_licensure_options,
            "rl_enabled": payload.rl_enabled,
        }}
    )
    await log_event_async(
        db,
        None,
        "settings_update",
        (
            f"Exam timer set to {payload.exam_time_limit_minutes} minutes; "
            f"exam questions set to {payload.exam_question_count}; "
            f"major questions set to {payload.exam_major_question_count}; "
            f"licensure categories: {len(target_licensure_options)}; "
            f"rl_enabled: {payload.rl_enabled}"
        ),
    )
    updated_settings = await db.app_settings.find_one({"_id": settings["_id"]})
    return {
        "exam_time_limit_minutes": updated_settings["exam_time_limit_minutes"],
        "exam_question_count": updated_settings["exam_question_count"],
        "exam_major_question_count": updated_settings.get("exam_major_question_count", 50),
        "passing_threshold_default": updated_settings.get("passing_threshold_default", 75),
        "mastery_threshold": updated_settings.get("mastery_threshold", 90),
        "target_licensure_options": updated_settings.get(
            "target_licensure_options", DEFAULT_TARGET_LICENSURE_OPTIONS
        ),
        "rl_enabled": bool(updated_settings.get("rl_enabled", False)),
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
            "profile_edit_allowed": bool(user.get("profile_edit_allowed", False)),
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
        "profile_edit_allowed": False,
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
        "profile_edit_allowed": False,
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


@router.post("/users/{user_id}/profile-edit-permission")
async def set_profile_edit_permission(
    user_id: str,
    payload: ProfileEditPermissionRequest,
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.get("role") != "student":
        raise HTTPException(status_code=400, detail="Only student profiles are controlled here")
    await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"profile_edit_allowed": bool(payload.allowed)}},
    )
    await log_event_async(
        db,
        user_id,
        "profile_edit_permission",
        f"Profile edit permission set to {bool(payload.allowed)}",
    )
    return {"id": user_id, "profile_edit_allowed": bool(payload.allowed)}


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


@router.get("/certifications")
async def list_certification_management_data(
    current_user=Depends(get_current_user),
    db=Depends(get_database),
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")

    settings = await get_or_create_settings(db)
    users = await db.users.find({"role": "student"}).to_list(length=None)
    snapshots = []
    for user in users:
        snapshot = await _student_cert_snapshot(db, user, settings)
        if snapshot:
            snapshots.append(snapshot)

    certificates_raw = await db.certificates.find().sort("created_at", -1).to_list(length=500)
    issued = []
    issued_by_user = {}
    for cert in certificates_raw:
        uid = cert.get("user_id")
        if uid and uid not in issued_by_user and cert.get("status") == "Issued":
            issued_by_user[uid] = cert
        issued.append(
            {
                "id": str(cert.get("_id")),
                "certificate_id": cert.get("certificate_id"),
                "user_id": cert.get("user_id"),
                "learner_name": cert.get("learner_name"),
                "category": cert.get("category"),
                "issue_date": _to_iso(cert.get("issue_date")),
                "verification_code": cert.get("verification_code"),
                "status": cert.get("status", "Issued"),
                "override": bool(cert.get("override", False)),
                "created_at": _to_iso(cert.get("created_at")),
                "revoked_at": _to_iso(cert.get("revoked_at")),
            }
        )

    pending = []
    streaks = []
    for snapshot in snapshots:
        streaks.append(snapshot)
        if snapshot["eligible"] and snapshot["user_id"] not in issued_by_user:
            pending.append(snapshot)

    return {
        "issued_certificates": issued,
        "pending_eligibility": pending,
        "consecutive_results": streaks,
    }


@router.post("/certifications/{user_id}/approve")
async def approve_certification_eligibility(
    user_id: str,
    payload: CertificationApprovalRequest,
    current_user=Depends(get_current_user),
    db=Depends(get_database),
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")

    user = await db.users.find_one({"_id": _safe_object_id(user_id), "role": "student"})
    if not user:
        raise HTTPException(status_code=404, detail="Student not found")

    settings = await get_or_create_settings(db)
    snapshot = await _student_cert_snapshot(db, user, settings)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Student profile not found")

    existing_issued = await db.certificates.find_one(
        {"user_id": str(user["_id"]), "status": "Issued"}
    )
    if existing_issued:
        return {
            "certificate": {
                "id": str(existing_issued.get("_id")),
                "certificate_id": existing_issued.get("certificate_id"),
                "user_id": existing_issued.get("user_id"),
                "learner_name": existing_issued.get("learner_name"),
                "category": existing_issued.get("category"),
                "issue_date": _to_iso(existing_issued.get("issue_date")),
                "verification_code": existing_issued.get("verification_code"),
                "status": existing_issued.get("status", "Issued"),
                "override": bool(existing_issued.get("override", False)),
            },
            "message": "Certificate already issued.",
        }

    if not snapshot["eligible"] and not payload.override:
        raise HTTPException(
            status_code=400,
            detail="Student is not yet eligible. Use override to issue manually.",
        )

    issue_date = datetime.utcnow()
    cert_seed = f"{snapshot['user_id']}-{snapshot['category']}-{issue_date.timestamp()}"
    cert_doc = {
        "user_id": snapshot["user_id"],
        "learner_name": snapshot["learner_name"],
        "category": snapshot["category"],
        "required_threshold": snapshot["required_threshold"],
        "average_score": snapshot["average_score"],
        "consecutive_passes": snapshot["consecutive_passes"],
        "certificate_id": _generate_certificate_id(cert_seed),
        "verification_code": _generate_verification_code(),
        "issue_date": issue_date,
        "status": "Issued",
        "override": bool(payload.override),
        "created_at": issue_date,
        "issued_by": current_user.get("email"),
    }
    created = await db.certificates.insert_one(cert_doc)
    cert_doc["_id"] = created.inserted_id
    await log_event_async(
        db,
        snapshot["user_id"],
        "certification_issued",
        (
            f"Certificate issued ({cert_doc['certificate_id']}) "
            f"for {snapshot['category']}; override={bool(payload.override)}"
        ),
    )
    return {
        "certificate": {
            "id": str(cert_doc.get("_id")),
            "certificate_id": cert_doc.get("certificate_id"),
            "user_id": cert_doc.get("user_id"),
            "learner_name": cert_doc.get("learner_name"),
            "category": cert_doc.get("category"),
            "issue_date": _to_iso(cert_doc.get("issue_date")),
            "verification_code": cert_doc.get("verification_code"),
            "status": cert_doc.get("status"),
            "override": bool(cert_doc.get("override", False)),
        },
        "message": "Certificate generated successfully.",
    }


@router.post("/certifications/{certificate_id}/revoke")
async def revoke_certificate(
    certificate_id: str,
    current_user=Depends(get_current_user),
    db=Depends(get_database),
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")

    cert = await db.certificates.find_one({"_id": _safe_object_id(certificate_id)})
    if not cert:
        raise HTTPException(status_code=404, detail="Certificate not found")
    if cert.get("status") == "Revoked":
        return {"revoked": True, "message": "Certificate already revoked."}

    revoked_at = datetime.utcnow()
    await db.certificates.update_one(
        {"_id": cert["_id"]},
        {
            "$set": {
                "status": "Revoked",
                "revoked_at": revoked_at,
                "revoked_by": current_user.get("email"),
            }
        },
    )
    await log_event_async(
        db,
        cert.get("user_id"),
        "certification_revoked",
        f"Certificate revoked ({cert.get('certificate_id')})",
    )
    return {"revoked": True, "message": "Certificate revoked."}


@router.get("/certifications/verify/{verification_code}")
async def verify_certificate_code(
    verification_code: str,
    current_user=Depends(get_current_user),
    db=Depends(get_database),
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")

    cert = await db.certificates.find_one({"verification_code": verification_code})
    if not cert:
        raise HTTPException(status_code=404, detail="Verification code not found")
    return {
        "certificate_id": cert.get("certificate_id"),
        "learner_name": cert.get("learner_name"),
        "category": cert.get("category"),
        "issue_date": _to_iso(cert.get("issue_date")),
        "status": cert.get("status", "Issued"),
        "verification_code": cert.get("verification_code"),
    }


@router.get("/backup/database")
async def backup_database(current_user=Depends(get_current_user)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")

    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    timestamp = _safe_timestamp()
    db_name = _database_name()
    backup_zip = BACKUP_ROOT / f"database-backup-{db_name}-{timestamp}.zip"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        mongodump = _find_mongo_tool("mongodump")
        result = subprocess.run(
            [
                mongodump,
                "--uri",
                _mongo_uri(),
                "--db",
                db_name,
                "--out",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"Database backup failed: {result.stderr.strip() or result.stdout.strip()}",
            )

        dumped_db_path = tmp_path / db_name
        if not dumped_db_path.exists():
            raise HTTPException(status_code=500, detail="Backup output not found")
        _zip_directory(dumped_db_path, backup_zip)

    return FileResponse(
        path=str(backup_zip),
        media_type="application/zip",
        filename=backup_zip.name,
    )


@router.get("/backup/system")
async def backup_system(current_user=Depends(get_current_user)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")

    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    timestamp = _safe_timestamp()
    backup_zip = BACKUP_ROOT / f"system-backup-{timestamp}.zip"
    _zip_system(backup_zip)
    return FileResponse(
        path=str(backup_zip),
        media_type="application/zip",
        filename=backup_zip.name,
    )


@router.post("/restore/database")
async def restore_database(
    file: UploadFile = File(...),
    current_user=Depends(get_current_user),
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Upload a .zip database backup")

    db_name = _database_name()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        upload_path = tmp_path / "upload.zip"
        with upload_path.open("wb") as fp:
            fp.write(await file.read())
        with zipfile.ZipFile(upload_path, "r") as archive:
            archive.extractall(tmp_path / "extract")

        extract_root = tmp_path / "extract"
        candidate_db_dir = extract_root / db_name
        if not candidate_db_dir.exists():
            matches = list(extract_root.rglob(db_name))
            if matches:
                candidate_db_dir = matches[0]
        if not candidate_db_dir.exists():
            raise HTTPException(status_code=400, detail=f"Database folder '{db_name}' not found in backup")

        mongorestore = _find_mongo_tool("mongorestore")
        result = subprocess.run(
            [
                mongorestore,
                "--uri",
                _mongo_uri(),
                "--drop",
                "--db",
                db_name,
                str(candidate_db_dir),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"Database restore failed: {result.stderr.strip() or result.stdout.strip()}",
            )

    return {"restored": True, "database": db_name}


@router.post("/restore/system")
async def restore_system(
    file: UploadFile = File(...),
    current_user=Depends(get_current_user),
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Upload a .zip system backup")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        upload_path = tmp_path / "upload.zip"
        with upload_path.open("wb") as fp:
            fp.write(await file.read())
        with zipfile.ZipFile(upload_path, "r") as archive:
            archive.extractall(tmp_path / "extract")

        extract_root = tmp_path / "extract"
        restored_files = 0
        for folder_name, target_root in [("reviewer-backend", BACKEND_ROOT), ("reviewer-ui", FRONTEND_ROOT)]:
            source_root = extract_root / folder_name
            if not source_root.exists():
                continue
            for source_path in source_root.rglob("*"):
                rel = source_path.relative_to(source_root)
                target_path = target_root / rel
                if source_path.is_dir():
                    target_path.mkdir(parents=True, exist_ok=True)
                    continue
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, target_path)
                restored_files += 1

    return {"restored": True, "files": restored_files}
