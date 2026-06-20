import os
import pickle
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException

from .auth import get_current_user
from .database import get_database

router = APIRouter(prefix="/readiness", tags=["Readiness"])

MODEL_DIR = os.path.join(os.path.dirname(__file__), "..", "models")
MODEL_VERSION = "3.0-random-forest"

_let_model = None
_cpa_model = None


def ensure_models_exist():
    import csv
    import numpy as np
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.preprocessing import LabelEncoder

    let_path = os.path.join(MODEL_DIR, "let_model.pkl")
    cpa_path = os.path.join(MODEL_DIR, "cpa_model.pkl")

    if os.path.exists(let_path) and os.path.exists(cpa_path):
        return

    os.makedirs(MODEL_DIR, exist_ok=True)
    repo_root = os.path.join(os.path.dirname(__file__), "..", "..")

    def safe_float(v):
        if v is None:
            return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    def train_model(csv_path, subject_cols, feature_count):
        if not os.path.exists(csv_path):
            return None
        with open(csv_path, newline='', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))

        features, targets = [], []
        for r in rows:
            scores = [safe_float(r.get(c)) for c in subject_cols]
            mocks = [safe_float(r.get(f'Mock_Exam_{i}')) for i in range(1, 11)]
            att = safe_float(r.get('Attendance_Percent'))
            study = safe_float(r.get('Study_Hours_Per_Week'))
            if None in scores or None in mocks or att is None or study is None:
                continue
            feats = scores + mocks + [att, study]
            if len(feats) != feature_count:
                feats = (feats + [70.0] * feature_count)[:feature_count]
            features.append(feats)
            targets.append(r.get('Risk_Level'))

        if len(features) < 3:
            return None

        X = np.array(features)
        y = np.array(targets)
        le = LabelEncoder()
        y_enc = le.fit_transform(y)
        model = RandomForestClassifier(n_estimators=100, random_state=42, max_depth=10)
        model.fit(X, y_enc)
        return {'model': model, 'encoder': le}

    let_artifacts = train_model(os.path.join(repo_root, "student_data.csv"), ['General_Education', 'Professional_Education', 'Major_Subject'], 15)
    if let_artifacts:
        with open(let_path, 'wb') as f:
            pickle.dump(let_artifacts, f)
        print(f"[readiness] LET model trained ({len(let_artifacts['model'].estimators_)} trees)")

    cpa_artifacts = train_model(os.path.join(repo_root, "student_data_cpa.csv"), ['FAR', 'AFAR', 'AUD', 'MAS', 'RFBT', 'TAX'], 18)
    if cpa_artifacts:
        with open(cpa_path, 'wb') as f:
            pickle.dump(cpa_artifacts, f)
        print(f"[readiness] CPA model trained ({len(cpa_artifacts['model'].estimators_)} trees)")


def _safe_float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return fallback


def _load_model(licensure: str):
    global _let_model, _cpa_model

    key = licensure.upper()
    if key == "CPA":
        if _cpa_model is None:
            path = os.path.join(MODEL_DIR, "cpa_model.pkl")
            with open(path, "rb") as f:
                _cpa_model = pickle.load(f)
        return _cpa_model
    else:
        if _let_model is None:
            path = os.path.join(MODEL_DIR, "let_model.pkl")
            with open(path, "rb") as f:
                _let_model = pickle.load(f)
        return _let_model


def _extract_features_let(exam_results: List[Dict]) -> Optional[List[float]]:
    if not exam_results:
        return None

    scores = [_safe_float(r.get("percentage", 0), 0.0) for r in exam_results]
    latest = exam_results[-1]
    subject_perf = latest.get("subject_performance", {}) or {}

    ge = 70.0
    pe = 70.0
    ms = 70.0

    label_map = {
        "general education": "ge",
        "gened": "ge",
        "general": "ge",
        "professional education": "pe",
        "profed": "pe",
        "professional": "pe",
        "major": "ms",
        "specialization": "ms",
    }

    for subj, stat in subject_perf.items():
        key = str(subj).strip().lower()
        role = label_map.get(key)
        if role == "ge":
            ge = (stat.get("correct", 0) / max(stat.get("total", 1), 1)) * 100
        elif role == "pe":
            pe = (stat.get("correct", 0) / max(stat.get("total", 1), 1)) * 100
        elif role == "ms":
            ms = (stat.get("correct", 0) / max(stat.get("total", 1), 1)) * 100

    mocks = scores[:10] if len(scores) >= 10 else scores + [70.0] * (10 - len(scores))
    attendance = 80.0
    study_hours = 15.0

    return [ge, pe, ms] + mocks + [attendance, study_hours]


def _extract_features_cpa(exam_results: List[Dict]) -> Optional[List[float]]:
    if not exam_results:
        return None

    latest = exam_results[-1]
    scores = [_safe_float(r.get("percentage", 0), 0.0) for r in exam_results]
    subject_perf = latest.get("subject_performance", {}) or {}

    cpa_subjects = ["FAR", "AFAR", "AUD", "MAS", "RFBT", "TAX"]
    subject_scores: List[float] = []

    for subj in cpa_subjects:
        stat = subject_perf.get(subj, {})
        if stat and stat.get("total", 0) > 0:
            subject_scores.append((stat.get("correct", 0) / stat["total"]) * 100)
        else:
            subject_scores.append(70.0)

    mocks = scores[:10] if len(scores) >= 10 else scores + [70.0] * (10 - len(scores))
    attendance = 85.0
    study_hours = 18.0

    return subject_scores + mocks + [attendance, study_hours]


def _predict_risk(model_data: Dict[str, Any], features: List[float]):
    model = model_data["model"]
    encoder = model_data["encoder"]

    prediction = model.predict([features])[0]
    probabilities = model.predict_proba([features])[0]

    risk = encoder.inverse_transform([prediction])[0]
    confidence = float(max(probabilities))

    return risk, confidence


@router.get("/prediction")
async def get_predicted_readiness(
    current_user=Depends(get_current_user),
    db=Depends(get_database),
):
    user = await db.users.find_one({"email": current_user["email"]})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    profile = await db.student_profiles.find_one({"user_id": str(user["_id"])})
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    target_licensure = profile.get("target_licensure", "LET")

    exam_results = (
        await db.exam_results.find({"user_id": str(user["_id"])})
        .sort("created_at", 1)
        .to_list(length=None)
    )

    if not exam_results:
        return {
            "readiness_low": 0,
            "readiness_high": 0,
            "latest_score": 0,
            "risk_level": "High",
            "result": "Fail",
            "confidence": 0.0,
            "attempts": 0,
            "model_version": MODEL_VERSION,
            "methodology": "No exam data available for prediction.",
        }

    model_data = _load_model(target_licensure)

    if target_licensure.upper() == "CPA":
        features = _extract_features_cpa(exam_results)
    else:
        features = _extract_features_let(exam_results)

    if features is None:
        return {
            "readiness_low": 0,
            "readiness_high": 0,
            "latest_score": 0,
            "risk_level": "High",
            "result": "Fail",
            "confidence": 0.0,
            "attempts": len(exam_results),
            "model_version": MODEL_VERSION,
            "methodology": "Insufficient data for feature extraction.",
        }

    risk, confidence = _predict_risk(model_data, features)
    result = "Pass" if risk in ("Low", "Medium") else "Fail"

    scores = [_safe_float(r.get("percentage", 0), 0.0) for r in exam_results]
    latest_score = scores[-1]
    base_scores = {"Low": 85, "Medium": 78, "High": 65}
    estimated_rating = base_scores.get(risk, 65)

    if len(scores) >= 2:
        trend = scores[-1] - scores[0]
        estimated_rating += trend * 0.3

    estimated_rating = max(50, min(100, estimated_rating))

    subject_perf = exam_results[-1].get("subject_performance", {}) or {}
    weak_subjects: List[str] = []
    for subj, stat in subject_perf.items():
        pct = (stat.get("correct", 0) / max(stat.get("total", 1), 1)) * 100
        if pct < 60:
            weak_subjects.append(subj)

    return {
        "readiness_low": max(50, int(estimated_rating - 10)),
        "readiness_high": min(100, int(estimated_rating + 10)),
        "latest_score": round(latest_score, 1),
        "estimated_rating": round(estimated_rating, 1),
        "risk_level": risk,
        "result": result,
        "confidence": round(confidence, 2),
        "weak_subjects": weak_subjects[:3],
        "attempts": len(scores),
        "model_version": MODEL_VERSION,
        "methodology": (
            "Random Forest classifier trained on historical student records. "
            "Model predicts risk level (Low/Medium/High) based on feature patterns learned from training data. "
            f"Current confidence: {confidence:.0%}. Predicted risk: {risk} → {result}."
        ),
    }
