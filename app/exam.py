from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
import random
from bson import ObjectId
from .auth import get_current_user
from .database import get_database
from .db_models import AppSetting, ExamResult, Question, StudentProfile, User
from .audit import log_event_async


router = APIRouter(prefix="/exam", tags=["Exam"])

GENED_LABEL = "General Education"
PROFED_LABEL = "Professional Education"


def section_label_for(bucket, major, extra_major=False):
    if bucket == GENED_LABEL:
        return "General Education"
    if bucket == PROFED_LABEL:
        return "Professional Education"
    if major and bucket == major:
        return f"Major: {major}" + (" (Additional)" if extra_major else "")
    return bucket or "General"


def normalize_label(value: str) -> str:
    if not value:
        return ""
    cleaned = "".join(ch for ch in value.lower() if ch.isalnum())
    aliases = {
        "socialscience": "socialstudies",
        "socialstudies": "socialstudies",
        "socialscie": "socialstudies",
        "socialsci": "socialstudies",
        "socsci": "socialstudies",
        "mathematics": "math",
        "math": "math",
        "mathema": "math",
        "mathem": "math",
        "english": "english",
        "filipino": "filipino",
        "science": "science",
        "professionaleducation": "professionaleducation",
        "professionaledu": "professionaleducation",
        "professional": "professionaleducation",
        "profed": "professionaleducation",
        "generaleducation": "gened",
        "generale": "gened",
        "general": "gened",
        "gened": "gened",
    }
    return aliases.get(cleaned, cleaned)


def normalize_major_candidate(value: str) -> str:
    if not value:
        return ""
    cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in value.lower())
    tokens = [
        token
        for token in cleaned.split()
        if token not in {"major", "specialization", "specialisation", "track", "let", "secondary", "elementary"}
    ]
    compact = "".join(tokens)
    return normalize_label(compact)


def labels_equivalent(a: str, b: str) -> bool:
    if not a or not b:
        return False
    a_norm = normalize_label(a)
    b_norm = normalize_label(b)
    if a_norm == b_norm:
        return True
    if a_norm.endswith("s") and a_norm[:-1] == b_norm:
        return True
    if b_norm.endswith("s") and b_norm[:-1] == a_norm:
        return True
    a_major = normalize_major_candidate(a)
    b_major = normalize_major_candidate(b)
    if a_major and b_major and a_major == b_major:
        return True
    return False


def build_subject_filter(profile, subjects):
    if profile.get("target_licensure") != "LET":
        return subjects or []

    major = (profile.get("major_specialization") or "").strip()
    normalized = []
    for subject in subjects or []:
        if not subject:
            continue
        lowered = subject.strip().lower()
        if lowered in {"specialization", "major"}:
            if major:
                normalized.append(major)
            continue
        if lowered in {"gened", "general education"}:
            normalized.extend([GENED_LABEL, "General Education"])
            continue
        if lowered in {"profed", "professional education", "professional ed"}:
            normalized.extend([PROFED_LABEL, "ProfEd", "Professional Ed"])
            continue
        normalized.append(subject.strip())

    if not normalized:
        normalized.extend([GENED_LABEL, "General Education", PROFED_LABEL, "ProfEd", "Professional Ed"])
        if major:
            normalized.append(major)

    # de-duplicate while preserving order
    seen = set()
    output = []
    for item in normalized:
        if item and item not in seen:
            output.append(item)
            seen.add(item)
    return output


def subject_bucket_for(question, profile):
    subject = (question.get("subject") or "").strip()
    topic = (question.get("topic") or "").strip().lower()
    subject_lower = subject.lower()

    if profile.get("target_licensure") != "LET":
        return subject or "General"

    if (
        subject_lower in {"profed", "professional education", "professional ed"}
        or subject_lower.startswith("prof")
        or topic.startswith("professional education")
    ):
        return PROFED_LABEL
    if subject_lower in {"gened", "general education"} or subject_lower.startswith("gen") or subject_lower.startswith("general"):
        return GENED_LABEL

    major = (profile.get("major_specialization") or "").strip()
    if major:
        if labels_equivalent(subject, major) or labels_equivalent(topic, major):
            return major
        if subject_lower in {"specialization", "specialisation", "major"} and labels_equivalent(topic, major):
            return major
        return None
    return "Specialization"


def major_label_for_profile(profile: dict) -> str:
    major = (profile.get("major_specialization") or "").strip()
    if major:
        return major
    let_track = (profile.get("let_track") or "").strip()
    if let_track:
        return let_track
    return "Unspecified"

DIFFICULTY_LEVELS = ("Easy", "Medium", "Hard")
DEFAULT_DIFFICULTY_MIX = {"Easy": 0.40, "Medium": 0.40, "Hard": 0.20}
PASS_DIFFICULTY_MIX = {"Easy": 0.20, "Medium": 0.40, "Hard": 0.40}
FAIL_DIFFICULTY_MIX = {"Easy": 0.60, "Medium": 0.30, "Hard": 0.10}


def difficulty_mix_for_result(latest_result):
    if not latest_result:
        return DEFAULT_DIFFICULTY_MIX
    result = (latest_result.get("result") or "").upper()
    if result == "PASS":
        return PASS_DIFFICULTY_MIX
    if result == "FAIL":
        return FAIL_DIFFICULTY_MIX
    return DEFAULT_DIFFICULTY_MIX


def allocate_by_weight(total, weights):
    if total <= 0:
        return {level: 0 for level in DIFFICULTY_LEVELS}
    raw = {level: total * float(weights.get(level, 0)) for level in DIFFICULTY_LEVELS}
    counts = {level: int(raw[level]) for level in DIFFICULTY_LEVELS}
    remainder = total - sum(counts.values())
    fractional = sorted(
        [(level, raw[level] - counts[level]) for level in DIFFICULTY_LEVELS],
        key=lambda item: item[1],
        reverse=True,
    )
    for level, _ in fractional:
        if remainder <= 0:
            break
        counts[level] += 1
        remainder -= 1
    return counts


def select_questions_with_mix(pool, total, weights):
    if total <= 0:
        return []
    if len(pool) < total:
        raise HTTPException(
            status_code=400,
            detail=(
                "Not enough questions available for this track. "
                f"Needed {total}, but only {len(pool)} available."
            ),
        )

    groups = {level: [] for level in DIFFICULTY_LEVELS}
    for q in pool:
        diff = q.get("difficulty")
        if diff in groups:
            groups[diff].append(q)

    desired = allocate_by_weight(total, weights)
    selected = []
    selected_ids = set()

    for level in DIFFICULTY_LEVELS:
        need = desired.get(level, 0)
        options = [q for q in groups.get(level, []) if q["_id"] not in selected_ids]
        if need <= 0:
            continue
        if len(options) <= need:
            chosen = options
        else:
            chosen = random.sample(options, need)
        selected.extend(chosen)
        selected_ids.update({q["_id"] for q in chosen})

    remaining = total - len(selected)
    if remaining > 0:
        remaining_pool = [q for q in pool if q["_id"] not in selected_ids]
        if len(remaining_pool) < remaining:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Not enough questions available for this track. "
                    f"Needed {total}, but only {len(pool)} available."
                ),
            )
        selected.extend(random.sample(remaining_pool, remaining))

    return selected

@router.post("/start")
async def start_exam(
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    email = current_user["email"]

    user = await db.users.find_one({"email": email})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    profile = await db.student_profiles.find_one({"user_id": str(user["_id"])})
    if not profile:
        raise HTTPException(status_code=400, detail="Profile not found")

    exam_type = profile["target_licensure"]
    subjects = profile.get("assigned_review_subjects") or []
    let_track = (profile.get("let_track") or "").strip().lower()
    is_elementary = let_track.startswith("elementary")

    query = {"exam_type": exam_type}
    subject_filter = build_subject_filter(profile, subjects)
    if exam_type != "LET" and subject_filter:
        query["subject"] = {"$in": subject_filter}

    question_list = await db.questions.find(query).to_list(length=None)
    # Fallback: if subject filter yields nothing, allow all questions for the exam type.
    if subjects and len(question_list) == 0:
        question_list = await db.questions.find({"exam_type": exam_type}).to_list(length=None)

    settings = await db.app_settings.find_one({})
    base_total = settings["exam_question_count"] if settings else 50
    major_setting = settings.get("exam_major_question_count", 50) if settings else 50
    major = (profile.get("major_specialization") or "").strip()
    extra_major_count = 0
    if exam_type == "LET" and not is_elementary:
        if not major:
            raise HTTPException(
                status_code=400,
                detail="Major specialization is required for secondary LET exams.",
            )
        extra_major_count = major_setting
    total_questions = base_total + extra_major_count

    latest_result = (
        await db.exam_results.find({"user_id": str(user["_id"]), "exam_type": exam_type})
        .sort("created_at", -1)
        .limit(1)
        .to_list(length=1)
    )
    difficulty_mix = difficulty_mix_for_result(latest_result[0] if latest_result else None)

    extra_major_ids = set()
    if exam_type == "LET":
        buckets = {
            GENED_LABEL: [],
            PROFED_LABEL: [],
        }
        if major and not is_elementary:
            buckets[major] = []

        for q in question_list:
            bucket = subject_bucket_for(q, profile)
            if bucket in buckets:
                buckets[bucket].append(q)

        # Percent split for base total: GenEd 50%, ProfEd 50%
        splits = [
            (GENED_LABEL, 0.50),
            (PROFED_LABEL, 0.50),
        ]

        # Compute target counts with rounding, then fix remainder.
        counts = {}
        fractional = []
        allocated = 0
        for label, pct in splits:
            raw = base_total * pct
            count = int(raw)
            counts[label] = count
            allocated += count
            fractional.append((label, raw - count))
        remainder = base_total - allocated
        for label, _ in sorted(fractional, key=lambda x: x[1], reverse=True):
            if remainder <= 0:
                break
            counts[label] += 1
            remainder -= 1

        selected = []
        selected_ids = set()
        extra_major_ids = set()
        for label in counts:
            pool = [q for q in buckets.get(label, []) if q["_id"] not in selected_ids]
            if len(pool) < counts[label]:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Not enough questions available for this track. "
                        f"Needed {counts[label]} from {label}, but only {len(pool)} available."
                    ),
                )
            chosen = select_questions_with_mix(pool, counts[label], difficulty_mix)
            selected.extend(chosen)
            selected_ids.update({q["_id"] for q in chosen})

        # Add extra major questions if applicable
        if major and extra_major_count:
            major_pool = [q for q in buckets.get(major, []) if q["_id"] not in selected_ids]
            if len(major_pool) < extra_major_count:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Not enough questions available for this track. "
                        f"Needed {extra_major_count} from Major, but only {len(major_pool)} available."
                    ),
                )
            extra_chosen = select_questions_with_mix(major_pool, extra_major_count, difficulty_mix)
            selected.extend(extra_chosen)
            extra_major_ids.update({q["_id"] for q in extra_chosen})
            selected_ids.update(extra_major_ids)

        exam_questions = selected
    else:
        exam_questions = select_questions_with_mix(question_list, total_questions, difficulty_mix)

class ExamSubmission(BaseModel):
    answers: dict  # { question_id: "A" | "B" | "C" | "D" }


@router.post("/submit")
async def submit_exam(
    payload: ExamSubmission,
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    email = current_user["email"]

    user = await db.users.find_one({"email": email})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    profile = await db.student_profiles.find_one({"user_id": str(user["_id"])})
    if not profile:
        raise HTTPException(status_code=400, detail="Profile not found")

    score = 0
    total = len(payload.answers)
    subject_stats = {}
    incorrect_questions = []

    for question_id, selected in payload.answers.items():
        question = await db.questions.find_one({"_id": ObjectId(question_id)})
        if not question:
            continue

        bucket = subject_bucket_for(question, profile)
        if bucket:
            subject_stats.setdefault(bucket, {"correct": 0, "total": 0})
            subject_stats[bucket]["total"] += 1

        if selected == question["answer"]:
            score += 1
            if bucket:
                subject_stats[bucket]["correct"] += 1
        else:
            reference = (
                f"Review: {question.get('topic')}"
                if question.get("topic")
                else "Review this topic"
            )
            incorrect_questions.append(
                {
                    "id": str(question["_id"]),
                    "subject": question["subject"],
                    "topic": question.get("topic"),
                    "difficulty": question["difficulty"],
                    "question": question["question"],
                    "correct_answer": question["answer"],
                    "student_answer": selected,
                    "reference": reference,
                }
            )

    percentage = round((score / total) * 100, 2) if total else 0
    passing_threshold = profile.get("required_passing_threshold", 60)
    result = "PASS" if percentage >= passing_threshold else "FAIL"

    exam_result_data = {
        "user_id": str(user["_id"]),
        "exam_type": profile["target_licensure"],
        "score": score,
        "total": total,
        "percentage": percentage,
        "result": result,
        "subject_performance": subject_stats,
        "incorrect_questions": incorrect_questions,
        "created_at": datetime.utcnow()
    }
    result_insert = await db.exam_results.insert_one(exam_result_data)
    await log_event_async(db, str(user["_id"]), "exam_submit", f"Score {score}/{total} ({percentage}%)")

    return {
        "email": email,
        "exam_type": profile["target_licensure"],
        "score": score,
        "total": total,
        "percentage": percentage,
        "result": result,
        "subject_performance": subject_stats,
        "incorrect_questions": incorrect_questions,
    }


@router.get("/stats")
async def get_exam_stats(
    program: Optional[str] = Query(default=None),
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    if current_user["role"] not in {"instructor", "admin"}:
        raise HTTPException(status_code=403, detail="Not authorized")

    program_filter = program.strip() if program else None
    exam_filter = {"exam_type": program_filter} if program_filter else {}

    attempts = await db.exam_results.count_documents(exam_filter)
    pipeline = [
        {"$match": exam_filter} if exam_filter else {"$match": {}},
        {"$group": {"_id": None, "avg_score": {"$avg": "$percentage"}, "total_answered": {"$sum": "$total"}}},
    ]
    agg_result = await db.exam_results.aggregate(pipeline).to_list(length=1)
    avg_score = agg_result[0]["avg_score"] if agg_result else 0
    total_answered = agg_result[0]["total_answered"] if agg_result else 0

    settings = await db.app_settings.find_one({})
    total_questions = settings["exam_question_count"] if settings else 50
    completion_rate = (
        round((total_answered / (attempts * total_questions)) * 100, 0)
        if attempts
        else 0
    )
    active_users = await db.users.find({"role": "student", "active": True}).to_list(length=None)
    active_user_ids = [str(user["_id"]) for user in active_users]
    if program_filter:
        active_students = await db.student_profiles.count_documents(
            {"user_id": {"$in": active_user_ids}, "target_licensure": program_filter}
        )
    else:
        active_students = len(active_user_ids)

    let_profiles = []
    if not program_filter or program_filter == "LET":
        let_profiles = await db.student_profiles.find(
            {"user_id": {"$in": active_user_ids}, "target_licensure": "LET"}
        ).to_list(length=None)
    major_counts = {}
    for profile in let_profiles:
        label = major_label_for_profile(profile)
        major_counts[label] = major_counts.get(label, 0) + 1
    let_major_counts = [
        {"major": label, "count": count}
        for label, count in sorted(major_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    recent_results = (
        await db.exam_results.find(exam_filter).sort("created_at", -1).limit(7).to_list(length=7)
    )
    recent_scores = [result["percentage"] for result in reversed(recent_results)]
    recent_attempts = (
        await db.exam_results.find(exam_filter).sort("created_at", -1).limit(12).to_list(length=12)
    )
    attempt_user_ids = [result.get("user_id") for result in recent_attempts if result.get("user_id")]
    attempt_user_ids = list({uid for uid in attempt_user_ids})
    user_id_objects = []
    for uid in attempt_user_ids:
        try:
            user_id_objects.append(ObjectId(uid))
        except Exception:
            continue
    users = (
        await db.users.find({"_id": {"$in": user_id_objects}}).to_list(length=None)
        if user_id_objects
        else []
    )
    users_by_id = {str(user["_id"]): user for user in users}
    profile_ids = [str(user["_id"]) for user in users]
    profiles = (
        await db.student_profiles.find({"user_id": {"$in": profile_ids}}).to_list(length=None)
        if profile_ids
        else []
    )
    profiles_by_user = {profile["user_id"]: profile for profile in profiles}
    recent_attempt_log = []
    for result in recent_attempts:
        user = users_by_id.get(result.get("user_id", ""))
        profile = profiles_by_user.get(result.get("user_id", ""))
        recent_attempt_log.append(
            {
                "email": user.get("email") if user else "Unknown",
                "exam_type": result.get("exam_type"),
                "percentage": result.get("percentage"),
                "score": result.get("score"),
                "total": result.get("total"),
                "major": major_label_for_profile(profile or {}),
                "created_at": result.get("created_at").isoformat() if result.get("created_at") else None,
            }
        )

    return {
        "avg_score": round(avg_score, 2),
        "completion_rate": min(completion_rate, 100),
        "active_students": active_students,
        "recent_scores": recent_scores,
        "let_major_counts": let_major_counts,
        "recent_attempts": recent_attempt_log,
    }


@router.get("/history")
async def get_exam_history(
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    user = await db.users.find_one({"email": current_user["email"]})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    results = (
        await db.exam_results.find({"user_id": str(user["_id"])})
        .sort("created_at", -1)
        .limit(20)
        .to_list(length=20)
    )
    return [
        {
            "date": result["created_at"].isoformat(),
            "score": result["score"],
            "total": result["total"],
            "percentage": result["percentage"],
            "result": result["result"],
            "subject_performance": result.get("subject_performance", {}),
        }
        for result in results
    ]




