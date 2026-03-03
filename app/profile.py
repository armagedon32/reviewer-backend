from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from .models import StudentProfile as StudentProfileSchema
from .auth import get_current_user
from .database import get_database
from .db_models import StudentProfile, User
from .audit import log_event_async
from .licensure import DEFAULT_TARGET_LICENSURE_OPTIONS

router = APIRouter(prefix="/profile", tags=["Profile"])

def profile_to_dict(profile):
    return {
        "student_id_number": profile["student_id_number"],
        "first_name": profile["first_name"],
        "middle_name": profile["middle_name"],
        "last_name": profile["last_name"],
        "email_address": profile["email_address"],
        "username": profile["username"],
        "program_degree": profile["program_degree"],
        "year_level": profile["year_level"],
        "section_class": profile.get("section_class"),
        "status": profile["status"],
        "target_licensure": profile["target_licensure"],
        "let_track": profile.get("let_track"),
        "major_specialization": profile["major_specialization"],
        "assigned_review_subjects": profile["assigned_review_subjects"],
        "required_passing_threshold": profile["required_passing_threshold"],
        "can_edit_profile": bool(profile.get("can_edit_profile", False)),
    }


@router.get("")
async def get_profile(
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    user = await db.users.find_one({"email": current_user["email"]})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    profile = await db.student_profiles.find_one({"user_id": str(user["_id"])})
    if not profile:
        return None
    profile["can_edit_profile"] = bool(user.get("profile_edit_allowed", False))
    return profile_to_dict(profile)


@router.post("")
async def save_profile(
    profile: StudentProfileSchema,
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    user = await db.users.find_one({"email": current_user["email"]})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    if profile.email_address.lower() != user["email"].lower():
        raise HTTPException(status_code=400, detail="Email must match account email")

    app_settings = await db.app_settings.find_one({}) or {}
    licensure_options = app_settings.get("target_licensure_options") or DEFAULT_TARGET_LICENSURE_OPTIONS
    licensure_rules = {}
    for option in licensure_options:
        name = str(option.get("name", "")).strip()
        subjects = [str(subject).strip() for subject in option.get("subjects", []) if str(subject).strip()]
        threshold = option.get("passing_threshold", 75)
        if not name or not subjects:
            continue
        licensure_rules[name.lower()] = {
            "name": name,
            "subjects": subjects,
            "passing_threshold": int(threshold),
        }

    target_licensure_name = str(profile.target_licensure or "").strip()
    rule = licensure_rules.get(target_licensure_name.lower())
    if not rule:
        raise HTTPException(status_code=400, detail="Invalid target licensure")
    profile.target_licensure = rule["name"]

    if profile.target_licensure == "LET":
        if profile.let_track not in {"Elementary", "Secondary"}:
            raise HTTPException(status_code=400, detail="LET track is required")
        if profile.let_track == "Secondary":
            if profile.major_specialization not in {"Mathematics", "Filipino", "Social Studies", "English"}:
                raise HTTPException(status_code=400, detail="LET major is required for Secondary")
        else:
            profile.major_specialization = "Elementary"
    else:
        profile.let_track = None
        if not (profile.major_specialization or "").strip():
            profile.major_specialization = profile.target_licensure

    expected_threshold = rule["passing_threshold"]
    if profile.required_passing_threshold != expected_threshold:
        raise HTTPException(
            status_code=400,
            detail=f"Passing threshold must be {expected_threshold} for {profile.target_licensure}",
        )

    allowed_subjects = set(rule["subjects"])
    if profile.target_licensure == "LET" and profile.let_track == "Elementary":
        allowed_subjects.discard("Specialization")
    if not profile.assigned_review_subjects:
        raise HTTPException(status_code=400, detail="Assigned review subjects are required")
    if not set(profile.assigned_review_subjects).issubset(allowed_subjects):
        raise HTTPException(status_code=400, detail="Invalid review subjects for licensure")

    existing_student_id = await db.student_profiles.find_one(
        {"student_id_number": profile.student_id_number, "user_id": {"$ne": str(user["_id"])}}
    )
    if existing_student_id:
        raise HTTPException(status_code=400, detail="Student ID already in use")
    existing_username = await db.student_profiles.find_one(
        {"username": profile.username, "user_id": {"$ne": str(user["_id"])}}
    )
    if existing_username:
        raise HTTPException(status_code=400, detail="Username already in use")

    existing = await db.student_profiles.find_one({"user_id": str(user["_id"])})
    if existing:
        if not user.get("profile_edit_allowed", False):
            raise HTTPException(
                status_code=403,
                detail="Profile editing is disabled. Ask admin for permission.",
            )
        await db.student_profiles.update_one(
            {"_id": existing["_id"]},
            {"$set": {
                "student_id_number": profile.student_id_number,
                "first_name": profile.first_name,
                "middle_name": profile.middle_name,
                "last_name": profile.last_name,
                "email_address": profile.email_address,
                "username": profile.username,
                "program_degree": profile.program_degree,
                "year_level": profile.year_level,
                "section_class": profile.section_class,
                "status": profile.status,
                "target_licensure": profile.target_licensure,
                "let_track": profile.let_track,
                "major_specialization": profile.major_specialization,
                "assigned_review_subjects": profile.assigned_review_subjects,
                "required_passing_threshold": profile.required_passing_threshold,
                "updated_at": datetime.utcnow()
            }}
        )
        await db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"profile_edit_allowed": False}},
        )
        saved = await db.student_profiles.find_one({"_id": existing["_id"]})
    else:
        profile_data = {
            "user_id": str(user["_id"]),
            "student_id_number": profile.student_id_number,
            "first_name": profile.first_name,
            "middle_name": profile.middle_name,
            "last_name": profile.last_name,
            "email_address": profile.email_address,
            "username": profile.username,
            "program_degree": profile.program_degree,
            "year_level": profile.year_level,
            "section_class": profile.section_class,
            "status": profile.status,
            "target_licensure": profile.target_licensure,
            "let_track": profile.let_track,
            "major_specialization": profile.major_specialization,
            "assigned_review_subjects": profile.assigned_review_subjects,
            "required_passing_threshold": profile.required_passing_threshold,
            "updated_at": datetime.utcnow(),
        }
        result = await db.student_profiles.insert_one(profile_data)
        saved = await db.student_profiles.find_one({"_id": result.inserted_id})

    await log_event_async(db, str(user["_id"]), "profile_save", "Student profile saved")

    return profile_to_dict(saved)
