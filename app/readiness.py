import csv
import logging
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import get_current_user
from .database import get_database
from .db_models import StudentProfile

router = APIRouter(prefix="/readiness", tags=["Readiness"])


logger = logging.getLogger(__name__)


class ReadinessResponse(BaseModel):
    score: float
    range_low: float
    range_high: float
    confidence: str
    recommendation: str
    predicted_let_score: Optional[float] = None
    model_version: str = "1.0-csv-heuristic"
    generated_at: str
    source: str = "backend"
    meta: Dict[str, Any] = {}


def _safe_float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return fallback


def _get_candidates() -> list:
    candidates_csv = os.getenv("READINESS_CSV")
    if candidates_csv and Path(candidates_csv).exists():
        return [Path(candidates_csv)]

    repo_root = Path(__file__).resolve().parents[2]
    default_csv = repo_root / "models" / "readiness_candidates.csv"
    if default_csv.exists():
        return [default_csv]

    return []


def _read_candidates(path: Path) -> list:
    with open(path, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _row_matches_profile(row: Dict[str, str], profile: Optional[StudentProfile]) -> bool:
    if profile is None:
        return False

    row_type = str(row.get("type") or row.get("track") or "").strip().upper()
    profile_target = str(profile.target_licensure or "").strip().upper()

    if profile_target and row_type and row_type != profile_target:
        return False

    return True


def _pick_candidate(exam_history: list, profile: Optional[StudentProfile]) -> Optional[Dict[str, str]]:
    candidates = _get_candidates()
    for path in candidates:
        try:
            rows = _read_candidates(path)
        except Exception as exc:
            logger.warning("readiness candidates read failed for %s: %s", path, exc)
            continue

        qualified = []

        for row in rows:
            try:
                threshold = float(row["threshold"])
            except Exception:
                continue

            if _row_matches_profile(row, profile):
                qualified.append((threshold, row, path.name))

        if not qualified:
            continue

        latest_score = max((_safe_float(entry.get("percentage", 0), 0.0) for entry in exam_history), default=0.0)

        best = None
        best_delta = None
        for threshold, row, source in qualified:
            delta = abs(latest_score - threshold)
            if best is None or delta < best_delta:
                best = (threshold, row, source)
                best_delta = delta

        if best is not None:
            return {
                "threshold": best[0],
                "row": best[1],
                "source": best[2],
            }

    return None


def _confidence_from_history(exam_history: list) -> str:
    if not exam_history:
        return "low"
    if len(exam_history) >= 5:
        return "high"
    return "medium"


@router.get("/prediction", response_model=ReadinessResponse)
async def get_predicted_readiness(
    current_user=Depends(get_current_user),
    db=Depends(get_database),
) -> ReadinessResponse:
    user = await db.users.find_one({"email": current_user["email"]})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    profile = await db.student_profiles.find_one({"user_id": str(user["_id"])})
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    exam_history = (
        await db.exam_results.find({"user_id": str(user["_id"])})
        .sort("created_at", -1)
        .to_list(length=None)
    )

    candidate = _pick_candidate(exam_history, profile)

    if candidate is None:
        return ReadinessResponse(
            score=0.0,
            range_low=0.0,
            range_high=0.0,
            confidence="low",
            recommendation="No matching readiness model found. Ensure candidates CSV is deployed.",
            predicted_let_score=None,
            meta={"candidates_checked": _get_candidates()},
            generated_at=datetime.utcnow().isoformat(),
        )

    threshold = float(candidate["threshold"])
    row = candidate["row"]
    source = candidate["source"]

    scores = [_safe_float(entry.get("percentage", 0), 0.0) for entry in exam_history]
    raw_score = max(scores, default=0.0)
    predicted = max(0.0, min(100.0, raw_score - threshold))
    predicted_let_score = round(predicted, 2)

    range_low = max(0.0, predicted_let_score - 5.0)
    range_high = min(100.0, predicted_let_score + 5.0)

    stability = row.get("stability", "")
    stability_factor = {
        "low": 6.0,
        "medium": 4.0,
        "high": 2.0,
    }.get(str(stability).strip().lower(), 4.0)

    range_low = max(0.0, round(predicted_let_score - stability_factor, 2))
    range_high = min(100.0, round(predicted_let_score + stability_factor, 2))

    confidence = _confidence_from_history(exam_history)
    recommendation = row.get("recommendation", "Continue reviewing and retake when ready.")
    predicted_let_score = round(_safe_float(predicted_let_score), 2)

    read_desc = row.get("readiness_description", "").strip()
    notes = []
    if read_desc:
        notes.append(read_desc)
    if row.get("notes"):
        notes.append(row["notes"])
    meta = {
        "readiness_description": read_desc,
        "notes": row.get("notes"),
        "threshold": threshold,
    }

    return ReadinessResponse(
        score=predicted_let_score,
        range_low=range_low,
        range_high=range_high,
        confidence=confidence,
        recommendation=recommendation,
        predicted_let_score=predicted_let_score,
        meta=meta,
        generated_at=datetime.utcnow().isoformat(),
        source=f"csv:{source}",
    )
