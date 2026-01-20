from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import random
from bson import ObjectId
from .auth import get_current_user
from .database import get_database
from .db_models import AppSetting, ExamResult, Question, StudentProfile, User
from .audit import log_event_async


router = APIRouter(prefix="/exam", tags=["Exam"])
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

    query = {"exam_type": exam_type}
    if subjects:
        query["subject"] = {"$in": subjects}

    question_list = await db.questions.find(query).to_list(length=None)

    settings = await db.app_settings.find_one({})
    total_questions = settings["exam_question_count"] if settings else 50

    if len(question_list) < total_questions:
        raise HTTPException(
            status_code=400,
            detail=(
                "Not enough questions available for this track. "
                f"Requested {total_questions}, but only {len(question_list)} available."
            ),
        )

    exam_questions = random.sample(question_list, total_questions)

    return [
        {
            "id": str(q["_id"]),
            "question": q["question"],
            "a": q["a"],
            "b": q["b"],
            "c": q["c"],
            "d": q["d"],
            "difficulty": q["difficulty"],
        }
        for q in exam_questions
    ]


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

        subject = question["subject"]
        bucket = subject
        if profile["target_licensure"] == "LET" and subject == "GenEd":
            topic = (question.get("topic") or "").strip().lower()
            if topic.startswith("professional education"):
                bucket = "Professional Ed"

        subject_stats.setdefault(bucket, {"correct": 0, "total": 0})
        subject_stats[bucket]["total"] += 1

        if selected == question["answer"]:
            score += 1
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
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    if current_user["role"] not in {"instructor", "admin"}:
        raise HTTPException(status_code=403, detail="Not authorized")

    attempts = await db.exam_results.count_documents({})
    pipeline = [
        {"$group": {"_id": None, "avg_score": {"$avg": "$percentage"}, "total_answered": {"$sum": "$total"}}}
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
    active_students = await db.users.count_documents({"role": "student"})
    recent_results = await db.exam_results.find().sort("created_at", -1).limit(7).to_list(length=7)
    recent_scores = [result["percentage"] for result in reversed(recent_results)]

    return {
        "avg_score": round(avg_score, 2),
        "completion_rate": min(completion_rate, 100),
        "active_students": active_students,
        "recent_scores": recent_scores,
    }
