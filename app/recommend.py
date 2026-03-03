from datetime import datetime, timedelta
import hashlib
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from .auth import get_current_user
from .database import get_database

router = APIRouter(prefix="/recommend", tags=["Recommendations"])

ACTION_DEFINITIONS = {
    "subject_drill": "Focused Subject Drill",
    "mixed_quiz": "Mixed Topic Quiz",
    "timed_mock": "Timed Full Mock Board",
    "remedial_lesson": "Remedial Lesson Block",
}
POLICY_VERSION = "bandit-v2"
EXPERIMENT_SPLIT = 50  # 50% rule baseline, 50% bandit when rl_enabled=True


class RecommendationFeedback(BaseModel):
    action_id: str
    reward: float = Field(ge=-1.0, le=1.0)
    note: str | None = None
    recommendation_id: str | None = None


def _subject_mastery_from_attempts(attempts):
    totals = {}
    for attempt in attempts:
        subject_perf = attempt.get("subject_performance") or {}
        for subject, stats in subject_perf.items():
            if subject not in totals:
                totals[subject] = {"correct": 0.0, "total": 0.0}
            totals[subject]["correct"] += float(stats.get("correct", 0) or 0)
            totals[subject]["total"] += float(stats.get("total", 0) or 0)
    mastery = {}
    for subject, values in totals.items():
        if values["total"] > 0:
            mastery[subject] = round((values["correct"] / values["total"]) * 100, 2)
    return mastery


def _build_context(profile: dict, attempts: list, passing_threshold: int):
    latest = attempts[0] if attempts else None
    previous = attempts[1] if len(attempts) > 1 else latest
    latest_score = float(latest.get("percentage", 0) if latest else 0)
    previous_score = float(previous.get("percentage", latest_score) if previous else latest_score)
    score_delta = latest_score - previous_score

    streak = 0
    for attempt in attempts:
        if float(attempt.get("percentage", 0)) >= passing_threshold:
            streak += 1
        else:
            break

    mastery = _subject_mastery_from_attempts(attempts[:10])
    weak_subjects = [k for k, v in sorted(mastery.items(), key=lambda item: item[1]) if v < passing_threshold]
    return {
        "target_licensure": profile.get("target_licensure"),
        "latest_score": latest_score,
        "score_delta": score_delta,
        "attempt_count": len(attempts),
        "pass_streak": streak,
        "weak_subjects": weak_subjects[:3],
        "subject_mastery": mastery,
    }


async def _thompson_pick_action(db, user_id: str, context: dict):
    # Phase 1: deterministic guardrails + Thompson-like sampling from observed rewards.
    latest_score = context["latest_score"]
    score_delta = context["score_delta"]
    weak_subjects = context["weak_subjects"]

    # Guardrails first.
    if latest_score < 50:
        return "remedial_lesson", "Low mastery detected; start with remediation."
    if weak_subjects and latest_score < 75:
        return "subject_drill", f"Focus on weakest areas: {', '.join(weak_subjects)}."
    if score_delta < -5:
        return "mixed_quiz", "Recent decline detected; use mixed practice to stabilize retention."
    if latest_score >= 75:
        return "timed_mock", "Strong baseline; increase exam realism with timed mock."

    # Bandit fallback from historical rewards for this user.
    action_scores = {action_id: 0.0 for action_id in ACTION_DEFINITIONS}
    action_counts = {action_id: 0 for action_id in ACTION_DEFINITIONS}
    cursor = db.rl_events.find(
        {"user_id": user_id, "event_type": "feedback"},
        {"action_id": 1, "reward": 1},
    )
    events = await cursor.to_list(length=500)
    for event in events:
        action_id = event.get("action_id")
        if action_id not in action_scores:
            continue
        action_scores[action_id] += float(event.get("reward", 0))
        action_counts[action_id] += 1
    ranked = sorted(
        ACTION_DEFINITIONS.keys(),
        key=lambda action_id: (action_scores[action_id] / (action_counts[action_id] or 1)),
        reverse=True,
    )
    best_action = ranked[0] if ranked else "mixed_quiz"
    return best_action, "Personalized using your historical outcomes."


def _rule_pick_action(context: dict):
    latest_score = context["latest_score"]
    score_delta = context["score_delta"]
    weak_subjects = context["weak_subjects"]
    if latest_score < 50:
        return "remedial_lesson", "Low mastery detected; start with remediation."
    if weak_subjects and latest_score < 75:
        return "subject_drill", f"Focus on weakest areas: {', '.join(weak_subjects)}."
    if score_delta < -5:
        return "mixed_quiz", "Recent decline detected; use mixed practice to stabilize retention."
    if latest_score >= 75:
        return "timed_mock", "Strong baseline; increase exam realism with timed mock."
    return "mixed_quiz", "Maintain mixed practice to improve consistency."


def _experiment_group(user_id: str) -> str:
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % 100
    return "baseline" if bucket < EXPERIMENT_SPLIT else "bandit"


@router.get("/next-action")
async def get_next_action(current_user=Depends(get_current_user), db=Depends(get_database)):
    user = await db.users.find_one({"email": current_user["email"]})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    profile = await db.student_profiles.find_one({"user_id": str(user["_id"])})
    if not profile:
        raise HTTPException(status_code=400, detail="Profile not found")

    settings = await db.app_settings.find_one({}) or {}
    rl_enabled = bool(settings.get("rl_enabled", False))
    passing_threshold = int(
        profile.get("required_passing_threshold")
        or settings.get("passing_threshold_default")
        or 75
    )

    attempts = await db.exam_results.find(
        {"user_id": str(user["_id"]), "exam_type": profile.get("target_licensure")}
    ).sort("created_at", -1).to_list(length=50)

    context = _build_context(profile, attempts, passing_threshold)

    experiment_group = _experiment_group(str(user["_id"]))
    recommendation_id = str(uuid4())
    if rl_enabled and experiment_group == "bandit":
        action_id, reason = await _thompson_pick_action(db, str(user["_id"]), context)
        policy_mode = "bandit"
    else:
        action_id, reason = _rule_pick_action(context)
        policy_mode = "rule_baseline" if rl_enabled else "rule_disabled"

    event = {
        "recommendation_id": recommendation_id,
        "user_id": str(user["_id"]),
        "event_type": "recommendation",
        "action_id": action_id,
        "policy_mode": policy_mode,
        "policy_version": POLICY_VERSION,
        "experiment_group": experiment_group,
        "context": context,
        "created_at": datetime.utcnow(),
    }
    await db.rl_events.insert_one(event)

    return {
        "recommendation_id": recommendation_id,
        "rl_enabled": rl_enabled,
        "policy_mode": policy_mode,
        "policy_version": POLICY_VERSION,
        "experiment_group": experiment_group,
        "action_id": action_id,
        "action_label": ACTION_DEFINITIONS[action_id],
        "reason": reason,
        "focus_subjects": context["weak_subjects"],
        "latest_score": context["latest_score"],
        "score_delta": context["score_delta"],
        "pass_streak": context["pass_streak"],
    }


@router.post("/feedback")
async def post_feedback(
    payload: RecommendationFeedback,
    current_user=Depends(get_current_user),
    db=Depends(get_database),
):
    user = await db.users.find_one({"email": current_user["email"]})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if payload.action_id not in ACTION_DEFINITIONS:
        raise HTTPException(status_code=400, detail="Invalid action_id")

    recommendation = None
    if payload.recommendation_id:
        recommendation = await db.rl_events.find_one(
            {
                "recommendation_id": payload.recommendation_id,
                "user_id": str(user["_id"]),
                "event_type": "recommendation",
            }
        )

    await db.rl_events.insert_one(
        {
            "user_id": str(user["_id"]),
            "event_type": "feedback",
            "action_id": payload.action_id,
            "reward": float(payload.reward),
            "note": payload.note or "",
            "recommendation_id": payload.recommendation_id,
            "policy_mode": recommendation.get("policy_mode") if recommendation else None,
            "policy_version": recommendation.get("policy_version") if recommendation else POLICY_VERSION,
            "experiment_group": recommendation.get("experiment_group") if recommendation else None,
            "created_at": datetime.utcnow(),
        }
    )
    return {"saved": True}


@router.get("/admin/metrics")
async def get_admin_rl_metrics(current_user=Depends(get_current_user), db=Depends(get_database)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")

    since = datetime.utcnow() - timedelta(days=30)
    events = await db.rl_events.find({"created_at": {"$gte": since}}).to_list(length=5000)

    recommendations = [event for event in events if event.get("event_type") == "recommendation"]
    feedback = [event for event in events if event.get("event_type") == "feedback"]

    action_counts = {action_id: 0 for action_id in ACTION_DEFINITIONS}
    for event in recommendations:
        action_id = event.get("action_id")
        if action_id in action_counts:
            action_counts[action_id] += 1

    by_group = {
        "baseline": {"recommendations": 0, "feedback_count": 0, "avg_reward": 0.0},
        "bandit": {"recommendations": 0, "feedback_count": 0, "avg_reward": 0.0},
    }
    reward_acc = {"baseline": 0.0, "bandit": 0.0}

    for event in recommendations:
        group = event.get("experiment_group")
        if group in by_group:
            by_group[group]["recommendations"] += 1

    for event in feedback:
        group = event.get("experiment_group")
        if group in by_group:
            by_group[group]["feedback_count"] += 1
            reward_acc[group] += float(event.get("reward", 0))

    for group in ("baseline", "bandit"):
        count = by_group[group]["feedback_count"]
        by_group[group]["avg_reward"] = round(reward_acc[group] / count, 4) if count else 0.0

    return {
        "policy_version": POLICY_VERSION,
        "window_days": 30,
        "recommendations_total": len(recommendations),
        "feedback_total": len(feedback),
        "action_distribution": action_counts,
        "ab_groups": by_group,
    }
