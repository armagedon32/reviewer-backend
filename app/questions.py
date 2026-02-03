from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel, validator
from typing import Optional
from bson import ObjectId
from .auth import get_current_user
from .database import get_database
from .db_models import Question
import csv
import io
import re

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


class QuestionUpdate(BaseModel):
    question: Optional[str] = None
    a: Optional[str] = None
    b: Optional[str] = None
    c: Optional[str] = None
    d: Optional[str] = None
    answer: Optional[str] = None
    difficulty: Optional[str] = None


def normalize_subject(subject: str) -> str:
    if not subject:
        return subject
    subj = subject.strip()
    if subj.startswith("[") and "]" in subj:
        subj = subj.split("]", 1)[1].strip()
    return subj


def question_to_dict(question: dict):
    return {
        "id": str(question.get("_id")),
        "exam_type": question.get("exam_type"),
        "subject": question.get("subject"),
        "topic": question.get("topic"),
        "difficulty": question.get("difficulty"),
        "question": question.get("question"),
        "a": question.get("a"),
        "b": question.get("b"),
        "c": question.get("c"),
        "d": question.get("d"),
        "answer": question.get("answer"),
        "rationale": question.get("rationale"),
    }


def normalize_difficulty(value: str) -> str:
    if not value:
        return value
    cleaned = value.strip()
    lowered = cleaned.lower()
    if "easy" in lowered:
        return "Easy"
    if "medium" in lowered:
        return "Medium"
    if "hard" in lowered:
        return "Hard"
    # fallback: title-case short values
    return cleaned.title()


WATERMARK_PATTERNS = [
    r"This file was submitted to www\.teachpinas\.com.*",
    r"Get more Free LET Reviewers.*",
    r"www\.teachpinas\.com.*",
]

OPTION_MARKERS = re.compile(r"\b[A-D]\.\s", re.IGNORECASE)


def sanitize_text(value: str) -> str:
    if not value:
        return value
    text = value.strip()
    for pattern in WATERMARK_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE).strip()
    # If another question number was accidentally appended, truncate it.
    match = re.search(r"(?:^|\s|\.)\d{1,3}\.", text)
    if match:
        text = text[: match.start()].strip()
    else:
        # Handle cases like "weight59." (no space before number)
        match = re.search(r"(?<=[A-Za-z)])\d{1,3}\.", text)
        if match:
            text = text[: match.start()].strip()
    return text


def has_embedded_options(text: str) -> bool:
    if not text:
        return False
    return len(OPTION_MARKERS.findall(text)) >= 2


def is_invalid_question(data: dict) -> bool:
    required_fields = ["exam_type", "subject", "topic", "difficulty", "question", "a", "b", "c", "d", "answer"]
    for field in required_fields:
        value = (data.get(field) or "").strip()
        if not value:
            return True

    if data.get("difficulty") not in {"Easy", "Medium", "Hard"}:
        return True
    if data.get("answer") not in {"A", "B", "C", "D"}:
        return True

    # Guard against merged questions/options in a single field.
    if has_embedded_options(data.get("question", "")):
        return True
    for option_key in ["a", "b", "c", "d"]:
        if has_embedded_options(data.get(option_key, "")):
            return True

    return False


def map_csv_row(row: dict) -> dict:
    # Support both LET template headers and CPA template headers
    def pick(*keys):
        for key in keys:
            if key in row and row[key] is not None:
                return str(row[key]).strip()
        return ""

    return {
        "exam_type": pick("exam_type", "exam_typ"),
        "subject": normalize_subject(pick("subject", "major_sub")),
        "topic": sanitize_text(pick("topic")),
        "difficulty": normalize_difficulty(pick("difficulty")),
        "question": sanitize_text(pick("question")),
        "a": sanitize_text(pick("a", "choice_a")),
        "b": sanitize_text(pick("b", "choice_b")),
        "c": sanitize_text(pick("c", "choice_c")),
        "d": sanitize_text(pick("d", "choice_d")),
        "answer": pick("answer").upper(),
        "rationale": sanitize_text(pick("rationale")),
    }


def build_question_key(data: dict) -> str:
    # Normalize to detect duplicates across uploads/edits.
    def norm(value: str) -> str:
        return " ".join((value or "").strip().lower().split())

    return "|".join(
        [
            norm(data.get("exam_type")),
            norm(data.get("subject")),
            norm(data.get("topic")),
            norm(data.get("difficulty")),
            norm(data.get("question")),
            norm(data.get("a")),
            norm(data.get("b")),
            norm(data.get("c")),
            norm(data.get("d")),
            norm(data.get("answer")),
            norm(data.get("rationale")),
        ]
    )


def sanitize_question_doc(doc: dict) -> dict:
    cleaned = {}
    for field in ["topic", "question", "a", "b", "c", "d", "rationale"]:
        if field in doc and doc[field] is not None:
            cleaned[field] = sanitize_text(str(doc[field]))
    if cleaned:
        cleaned["question_key"] = build_question_key({**doc, **cleaned})
    return cleaned


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
        item["question_key"] = build_question_key(item)
        await db.questions.update_one(
            {"question_key": item["question_key"]},
            {"$set": item},
            upsert=True,
        )


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
    question_data["question_key"] = build_question_key(question_data)
    await db.questions.update_one(
        {"question_key": question_data["question_key"]},
        {"$set": question_data},
        upsert=True,
    )
    return question_to_dict(question_data)



@router.delete("")
async def clear_questions(
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    if current_user["role"] not in {"instructor", "admin"}:
        raise HTTPException(status_code=403, detail="Not authorized")
    result = await db.questions.delete_many({})
    return {"deleted": result.deleted_count}


@router.patch("/{question_id}")
async def update_question(
    question_id: str,
    payload: QuestionUpdate,
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    if current_user["role"] not in {"instructor", "admin"}:
        raise HTTPException(status_code=403, detail="Not authorized")

    try:
        oid = ObjectId(question_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid question id")

    existing = await db.questions.find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Question not found")

    updates = {}
    if payload.question is not None:
        updates["question"] = sanitize_text(payload.question)
    if payload.a is not None:
        updates["a"] = sanitize_text(payload.a)
    if payload.b is not None:
        updates["b"] = sanitize_text(payload.b)
    if payload.c is not None:
        updates["c"] = sanitize_text(payload.c)
    if payload.d is not None:
        updates["d"] = sanitize_text(payload.d)
    if payload.answer is not None:
        answer = payload.answer.strip().upper()
        if answer not in {"A", "B", "C", "D"}:
            raise HTTPException(status_code=400, detail="Answer must be A, B, C, or D")
        updates["answer"] = answer
    if payload.difficulty is not None:
        diff = normalize_difficulty(payload.difficulty)
        if diff not in {"Easy", "Medium", "Hard"}:
            raise HTTPException(status_code=400, detail="Invalid difficulty")
        updates["difficulty"] = diff

    if updates:
        merged = {**existing, **updates}
        updates["question_key"] = build_question_key(merged)
        await db.questions.update_one({"_id": oid}, {"$set": updates})

    updated = await db.questions.find_one({"_id": oid})
    return question_to_dict(updated)


@router.post("/cleanup")
async def cleanup_questions(
    current_user=Depends(get_current_user),
    db = Depends(get_database),
):
    if current_user["role"] not in {"instructor", "admin"}:
        raise HTTPException(status_code=403, detail="Not authorized")

    updated = 0
    deleted = 0
    async for doc in db.questions.find({}):
        cleaned = sanitize_question_doc(doc)
        merged = {**doc, **cleaned} if cleaned else doc
        if is_invalid_question(merged):
            await db.questions.delete_one({"_id": doc["_id"]})
            deleted += 1
            continue
        if cleaned:
            await db.questions.update_one({"_id": doc["_id"]}, {"$set": cleaned})
            updated += 1

    return {"updated": updated, "deleted": deleted}


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

    sniffer = csv.Sniffer()
    sample = decoded[:4096]
    delimiter = ","
    try:
        dialect = sniffer.sniff(sample, delimiters=[",", ";", "\t"])
        delimiter = dialect.delimiter
    except Exception:
        delimiter = ","

    raw_reader = csv.reader(io.StringIO(decoded), delimiter=delimiter)
    try:
        headers = next(raw_reader)
    except StopIteration:
        raise HTTPException(status_code=400, detail="CSV is empty.")

    headers = [h.strip() for h in headers]
    fieldnames = set(headers)
    required_any = {"exam_type", "exam_typ"}
    required_common = {"topic", "difficulty", "question", "answer"}
    required_choices = {"a", "b", "c", "d"}
    required_choices_alt = {"choice_a", "choice_b", "choice_c", "choice_d"}
    required_subject = {"subject", "major_sub"}

    if not (fieldnames & required_any):
        raise HTTPException(status_code=400, detail="CSV must include exam_type or exam_typ.")
    if not (fieldnames & required_subject):
        raise HTTPException(status_code=400, detail="CSV must include subject or major_sub.")
    if not required_common.issubset(fieldnames):
        raise HTTPException(
            status_code=400,
            detail="CSV must include topic, difficulty, question, and answer.",
        )
    if not (required_choices.issubset(fieldnames) or required_choices_alt.issubset(fieldnames)):
        raise HTTPException(
            status_code=400,
            detail="CSV must include either a,b,c,d or choice_a,choice_b,choice_c,choice_d.",
        )

    added = 0
    skipped = 0
    question_idx = headers.index("question") if "question" in headers else None

    for row in raw_reader:
        if not row or all(not str(cell).strip() for cell in row):
            continue
        # If unquoted commas caused extra columns, merge extras back into the question field.
        if question_idx is not None and len(row) > len(headers):
            extra = len(row) - len(headers)
            merged_question = ",".join(row[question_idx : question_idx + extra + 1])
            row = (
                row[:question_idx]
                + [merged_question]
                + row[question_idx + extra + 1 :]
            )
        # Pad missing columns
        if len(row) < len(headers):
            row = row + [""] * (len(headers) - len(row))

        row_dict = dict(zip(headers, row))
        mapped = map_csv_row(row_dict)
        if is_invalid_question(mapped):
            skipped += 1
            continue
        mapped["question_key"] = build_question_key(mapped)
        result = await db.questions.update_one(
            {"question_key": mapped["question_key"]},
            {"$set": mapped},
            upsert=True,
        )
        if result.upserted_id or result.modified_count:
            added += 1

    return {"added": added, "skipped": skipped}
