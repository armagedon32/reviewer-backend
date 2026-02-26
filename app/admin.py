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
    direct = shutil.which(tool)
    if direct:
        return direct

    candidates = []
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
        if needs_update:
            await db.app_settings.update_one(
                {"_id": settings["_id"]},
                {"$set": {
                    "passing_threshold_default": settings["passing_threshold_default"],
                    "mastery_threshold": settings["mastery_threshold"],
                    "target_licensure_options": settings["target_licensure_options"],
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
            f"licensure categories: {len(target_licensure_options)}"
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
