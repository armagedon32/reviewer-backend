from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session
import random
from .auth import get_current_user
from .database import get_db
from .db_models import AppSetting, ExamResult, Question, StudentProfile, User
from .audit import log_event


router = APIRouter(prefix="/exam", tags=["Exam"])
@router.post("/start")
def start_exam(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    email = current_user["email"]

    profile = (
        db.query(StudentProfile)
        .join(User, StudentProfile.user_id == User.id)
        .filter(User.email == email)
        .first()
    )
    if not profile:
        raise HTTPException(status_code=400, detail="Profile not found")

    exam_type = profile.exam_type

    filtered = db.query(Question).filter(Question.exam_type == exam_type)

    if exam_type == "LET":
        if profile.let_track == "Elementary":
            filtered = filtered.filter(Question.subject == "GenEd")
        else:
            major = profile.let_major
            filtered = filtered.filter(Question.subject == major)

    if exam_type == "CPA":
        allowed = ["FAR", "AFAR", "Auditing", "MAS", "RFBT", "Taxation"]
        filtered = filtered.filter(Question.subject.in_(allowed))

    question_list = filtered.all()

    settings = db.query(AppSetting).first()
    total_questions = settings.exam_question_count if settings else 50

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
            "id": q.id,
            "question": q.question,
            "a": q.a,
            "b": q.b,
            "c": q.c,
            "d": q.d,
            "difficulty": q.difficulty,
        }
        for q in exam_questions
    ]


class ExamSubmission(BaseModel):
    answers: dict  # { question_id: "A" | "B" | "C" | "D" }


@router.post("/submit")
def submit_exam(
    payload: ExamSubmission,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    email = current_user["email"]

    profile = (
        db.query(StudentProfile)
        .join(User, StudentProfile.user_id == User.id)
        .filter(User.email == email)
        .first()
    )
    if not profile:
        raise HTTPException(status_code=400, detail="Profile not found")

    score = 0
    total = len(payload.answers)
    subject_stats = {}
    incorrect_questions = []

    def get_subject_bucket(question):
        subject = question.subject
        if profile.exam_type == "LET" and subject == "GenEd":
            topic = (question.topic or "").strip().lower()
            if topic.startswith("professional education"):
                return "Professional Ed"
        return subject

    question_ids = [int(qid) for qid in payload.answers.keys()]
    questions = db.query(Question).filter(Question.id.in_(question_ids)).all()
    question_map = {str(q.id): q for q in questions}

    for qid, selected in payload.answers.items():
        question = question_map.get(str(qid))
        if not question:
            continue
        bucket = get_subject_bucket(question)
        subject_stats.setdefault(bucket, {"correct": 0, "total": 0})
        subject_stats[bucket]["total"] += 1

        if selected == question.answer:
            score += 1
            subject_stats[bucket]["correct"] += 1
        else:
            reference = (
                f"Review: {question.topic}"
                if question.topic
                else "Review this topic"
            )
            incorrect_questions.append(
                {
                    "id": question.id,
                    "subject": question.subject,
                    "topic": question.topic,
                    "difficulty": question.difficulty,
                    "question": question.question,
                    "correct_answer": question.answer,
                    "student_answer": selected,
                    "reference": reference,
                }
            )

    percentage = round((score / total) * 100, 2) if total else 0
    result = "PASS" if percentage >= 60 else "FAIL"

    exam_result = ExamResult(
        user_id=profile.user_id,
        exam_type=profile.exam_type,
        score=score,
        total=total,
        percentage=percentage,
        result=result,
        subject_performance=subject_stats,
        incorrect_questions=incorrect_questions,
    )
    db.add(exam_result)
    db.commit()
    db.refresh(exam_result)
    log_event(db, profile.user_id, "exam_submit", f"Score {score}/{total} ({percentage}%)")

    return {
        "email": email,
        "exam_type": profile.exam_type,
        "score": score,
        "total": total,
        "percentage": percentage,
        "result": result,
        "subject_performance": subject_stats,
        "incorrect_questions": incorrect_questions,
    }


@router.get("/stats")
def get_exam_stats(
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user["role"] not in {"instructor", "admin"}:
        raise HTTPException(status_code=403, detail="Not authorized")

    attempts = db.query(ExamResult).count()
    avg_score = db.query(func.avg(ExamResult.percentage)).scalar() or 0
    total_answered = db.query(func.sum(ExamResult.total)).scalar() or 0
    settings = db.query(AppSetting).first()
    total_questions = settings.exam_question_count if settings else 50
    completion_rate = (
        round((total_answered / (attempts * total_questions)) * 100, 0)
        if attempts
        else 0
    )
    active_students = (
        db.query(User).filter(User.role == "student").count()
    )
    recent_results = (
        db.query(ExamResult)
        .order_by(ExamResult.created_at.desc())
        .limit(7)
        .all()
    )
    recent_scores = [result.percentage for result in reversed(recent_results)]

    return {
        "avg_score": round(avg_score, 2),
        "completion_rate": min(completion_rate, 100),
        "active_students": active_students,
        "recent_scores": recent_scores,
    }
