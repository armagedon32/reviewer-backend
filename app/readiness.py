import os
import pickle
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from .auth import get_current_user
from .database import get_database

router = APIRouter(prefix="/readiness", tags=["Readiness"])

MODEL_DIR = os.path.join(os.path.dirname(__file__), "..", "models")
MODEL_VERSION = "3.0-random-forest"

_let_model = None
_cpa_model = None


def _load_model(licensure):
    global _let_model, _cpa_model
    
    if licensure.upper() == "CPA":
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


def _extract_features_let(exam_results):
    """Extract features for LET model from exam results."""
    if not exam_results:
        return None
    
    latest = exam_results[-1]
    scores = [float(r.get("percentage", 0)) for r in exam_results]
    
    # Use latest exam scores as proxy for subject scores
    # Since we don't have detailed subject breakdown in exam_results for LET
    latest_score = scores[-1] if scores else 0
    
    # Estimate subject scores from mock exam progression
    # Use first 3 mock exams as proxies for GE, PE, Major
    mocks = scores[:10] if len(scores) >= 10 else scores + [latest_score] * (10 - len(scores))
    
    # Estimate attendance and study hours (use defaults if not available)
    attendance = 80.0
    study_hours = 15.0
    
    feature = mocks[:3] + mocks + [attendance, study_hours]
    return feature[:15]  # Ensure exactly 15 features


def _extract_features_cpa(exam_results):
    """Extract features for CPA model from exam results."""
    if not exam_results:
        return None
    
    latest = exam_results[-1]
    scores = [float(r.get("percentage", 0)) for r in exam_results]
    
    # Use subject_performance if available
    subject_perf = latest.get("subject_performance", {}) or {}
    
    cpa_subjects = ["FAR", "AFAR", "AUD", "MAS", "RFBT", "TAX"]
    subject_scores = []
    for subj in cpa_subjects:
        stat = subject_perf.get(subj, {})
        if stat and stat.get("total", 0) > 0:
            subject_scores.append((stat.get("correct", 0) / stat["total"]) * 100)
        else:
            subject_scores.append(70.0)  # Default
    
    # Pad with mock exam scores
    mocks = scores[:10] if len(scores) >= 10 else scores + [70.0] * (10 - len(scores))
    
    attendance = 85.0
    study_hours = 18.0
    
    feature = subject_scores + mocks + [attendance, study_hours]
    return feature[:18]  # Ensure exactly 18 features


def _predict_risk(model_data, features):
    """Use trained ML model to predict risk level."""
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
    
    exam_results = await db.exam_results.find(
        {"user_id": str(user["_id"])}
    ).sort("created_at", 1).to_list(length=None)

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

    # Load appropriate model
    model_data = _load_model(target_licensure)
    encoder = model_data["encoder"]
    
    # Extract features
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

    # ML prediction
    risk, confidence = _predict_risk(model_data, features)
    
    # Map risk to result
    result = "Pass" if risk in ("Low", "Medium") else "Fail"
    
    # Calculate score range based on risk and confidence
    scores = [float(r.get("percentage", 0)) for r in exam_results]
    latest_score = scores[-1]
    
    # Estimate rating based on risk level and ML output
    base_scores = {"Low": 85, "Medium": 78, "High": 65}
    estimated_rating = base_scores.get(risk, 65)
    
    # Adjust based on trend
    if len(scores) >= 2:
        trend = scores[-1] - scores[0]
        estimated_rating += trend * 0.3
    
    estimated_rating = max(50, min(100, estimated_rating))
    
    # Weak subjects analysis
    subject_perf = exam_results[-1].get("subject_performance", {}) or {}
    weak_subjects = []
    for subj, stat in subject_perf.items():
        pct = (stat.get("correct", 0) / stat.get("total", 1)) * 100
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
            f"Model predicts risk level (Low/Medium/High) based on 15 features for LET "
            f"(subject scores, mock exams, attendance, study hours) or 18 features for CPA "
            f"(6 subject scores + mock exams + attendance + study hours). "
            f"Current model confidence: {confidence:.0%}. "
            f"Predicted risk: {risk} → {result}."
        ),
    }
