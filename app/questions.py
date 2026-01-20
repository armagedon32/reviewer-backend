from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel, validator
from .auth import get_current_user
from .database import get_database
from .db_models import Question
import csv
import io

router = APIRouter(prefix="/questions", tags=["Questions"])


class QuestionCreate(BaseModel):
    exam_type: str
    subject: str
    topic: str
    difficulty: str
    question: str
    a: str
    b: str
    c: str
    d: str
    answer: str

    @validator("answer")
    def answer_must_be_option(cls, v):
        if v not in {"A", "B", "C", "D"}:
            raise ValueError("Answer must be one of A, B, C, or D")
        return v


def normalize_subject(subject: str) -> str:
    if not subject:
        return subject
    subj = subject.strip()
    if subj.startswith("[") and "]" in subj:
        subj = subj.split("]", 1)[1].strip()
    return subj


def question_to_dict(question: dict):
    return {
        "id": str(question["_id"]),
        "exam_type": question["exam_type"],
        "subject": question["subject"],
        "topic": question["topic"],
        "difficulty": question["difficulty"],
        "question": question["question"],
        "a": question["a"],
        "b": question["b"],
        "c": question.c,
        "d": question.d,
        "answer": question.answer,
    }


DEFAULT_QUESTIONS = [
    {
        "exam_type": "LET",
        "subject": "GenEd",
        "topic": "Reading",
        "difficulty": "Easy",
        "question": "What is the main idea of a paragraph?",
        "a": "The supporting details",
        "b": "The topic sentence",
        "c": "The conclusion",
        "d": "The title",
        "answer": "B",
    },
    {
        "exam_type": "LET",
        "subject": "GenEd",
        "topic": "Math",
        "difficulty": "Medium",
        "question": "What is the value of 3/4 + 1/8?",
        "a": "5/8",
        "b": "7/8",
        "c": "1",
        "d": "9/8",
        "answer": "B",
    },
    {
        "exam_type": "LET",
        "subject": "GenEd",
        "topic": "Science",
        "difficulty": "Hard",
        "question": "Which layer of the Earth is liquid?",
        "a": "Inner core",
        "b": "Mantle",
        "c": "Outer core",
        "d": "Crust",
        "answer": "C",
    },
    {
        "exam_type": "LET",
        "subject": "Mathematics",
        "topic": "Algebra",
        "difficulty": "Medium",
        "question": "What is x if 2x + 4 = 10?",
        "a": "2",
        "b": "3",
        "c": "4",
        "d": "5",
        "answer": "B",
    },
    {
        "exam_type": "LET",
        "subject": "Science",
        "topic": "Biology",
        "difficulty": "Easy",
        "question": "Which organelle is the powerhouse of the cell?",
        "a": "Nucleus",
        "b": "Mitochondria",
        "c": "Ribosome",
        "d": "Golgi apparatus",
        "answer": "B",
    },
    {
        "exam_type": "LET",
        "subject": "Social Studies",
        "topic": "History",
        "difficulty": "Medium",
        "question": "Who wrote the Philippine novel Noli Me Tangere?",
        "a": "Jose Rizal",
        "b": "Andres Bonifacio",
        "c": "Emilio Aguinaldo",
        "d": "Apolinario Mabini",
        "answer": "A",
    },
    {
        "exam_type": "LET",
        "subject": "English",
        "topic": "Grammar",
        "difficulty": "Easy",
        "question": "Choose the correct verb: She ___ to the store yesterday.",
        "a": "go",
        "b": "goes",
        "c": "went",
        "d": "gone",
        "answer": "C",
    },
    {
        "exam_type": "LET",
        "subject": "Filipino",
        "topic": "Wika",
        "difficulty": "Medium",
        "question": "Alin ang tamang baybay?",
        "a": "Tagumpay",
        "b": "Tagumpaey",
        "c": "Tagumpai",
        "d": "Tagumpae",
        "answer": "A",
    },
    {
        "exam_type": "LET",
        "subject": "P.E",
        "topic": "Fitness",
        "difficulty": "Easy",
        "question": "Ilang minuto ang inirerekomendang moderate exercise kada linggo?",
        "a": "30",
        "b": "60",
        "c": "150",
        "d": "300",
        "answer": "C",
    },
    {
        "exam_type": "CPA",
        "subject": "FAR",
        "topic": "Assets",
        "difficulty": "Hard",
        "question": "Which asset is measured at amortized cost?",
        "a": "Equity securities",
        "b": "Trading securities",
        "c": "Held-to-maturity investments",
        "d": "Derivatives",
        "answer": "C",
    },
    {
        "exam_type": "CPA",
        "subject": "Taxation",
        "topic": "VAT",
        "difficulty": "Medium",
        "question": "What is the standard VAT rate in the Philippines?",
        "a": "8%",
        "b": "10%",
        "c": "12%",
        "d": "15%",
        "answer": "C",
    },
    {
        "exam_type": "CPA",
        "subject": "Auditing",
        "topic": "Opinion",
        "difficulty": "Easy",
        "question": "Which opinion is issued when statements are free of material misstatement?",
        "a": "Qualified",
        "b": "Adverse",
        "c": "Disclaimer",
        "d": "Unmodified",
        "answer": "D",
    },
]


async def seed_questions(db):
    count = await db.questions.count_documents({})
    if count > 0:
        return
    for item in DEFAULT_QUESTIONS:
        await db.questions.insert_one(item)


@router.get("")
async def list_questions(
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    if current_user["role"] not in {"instructor", "admin"}:
        raise HTTPException(status_code=403, detail="Not authorized")
    questions = await db.questions.find().to_list(length=None)
    return [question_to_dict(q) for q in questions]


@router.post("")
async def add_question(
    payload: QuestionCreate,
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    if current_user["role"] not in {"instructor", "admin"}:
        raise HTTPException(status_code=403, detail="Not authorized")

    allowed_difficulty = {"Easy", "Medium", "Hard"}
    if payload.difficulty not in allowed_difficulty:
        raise HTTPException(status_code=400, detail="Invalid difficulty")

    question_data = {
        "exam_type": payload.exam_type,
        "subject": normalize_subject(payload.subject),
        "topic": payload.topic,
        "difficulty": payload.difficulty,
        "question": payload.question,
        "a": payload.a,
        "b": payload.b,
        "c": payload.c,
        "d": payload.d,
        "answer": payload.answer,
    }
    result = await db.questions.insert_one(question_data)
    db.refresh(question)
    return question_to_dict(question)


@router.delete("")
async def clear_questions(
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    if current_user["role"] not in {"instructor", "admin"}:
        raise HTTPException(status_code=403, detail="Not authorized")
    result = await db.questions.delete_many({})
    return {"deleted": result.deleted_count}


@router.post("/upload")
async def upload_questions_csv(
    file: UploadFile = File(...),
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    if current_user["role"] not in {"instructor", "admin"}:
        raise HTTPException(status_code=403, detail="Not authorized")

    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are supported")

    content = await file.read()
    try:
        decoded = content.decode("utf-8-sig")
    except Exception:
        raise HTTPException(status_code=400, detail="Unable to decode CSV (expected UTF-8)")

    reader = csv.DictReader(io.StringIO(decoded))
    required_fields = {
        "exam_type",
        "subject",
        "topic",
        "difficulty",
        "question",
        "a",
        "b",
        "c",
        "d",
        "answer",
    }
    if set(reader.fieldnames or []) != required_fields:
        raise HTTPException(
            status_code=400,
            detail=f"CSV must have headers exactly: {', '.join(sorted(required_fields))}",
        )

    added = 0
    for row in reader:
        if row["answer"] not in {"A", "B", "C", "D"}:
            continue
        if row["difficulty"] not in {"Easy", "Medium", "Hard"}:
            continue
        question_data = {
            "exam_type": row["exam_type"],
            "subject": normalize_subject(row["subject"]),
            "topic": row["topic"],
            "difficulty": row["difficulty"],
            "question": row["question"],
            "a": row["a"],
            "b": row["b"],
            "c": row["c"],
            "d": row["d"],
            "answer": row["answer"],
        }
        await db.questions.insert_one(question_data)
        added += 1

    return {"added": added}
