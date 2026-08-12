from datetime import datetime, timedelta
import bisect
import hashlib
import random
from collections import defaultdict
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
POLICY_VERSION = "bandit-v3"
EXPERIMENT_SPLIT = 50  # 50% no-learning baseline, 50% Thompson bandit when rl_enabled=True

REVIEW_MATERIALS = {
    "LET": {
        "GenEd": [
            "LET General Education modules (English, Math, Science, Social Studies, Filipino)",
            "PRC LET sample questions compilation",
            "General Education competency video lessons",
        ],
        "ProfEd": [
            "Professional Education reviewer (child development, assessment, curriculum)",
            "Recent LET Professional Education past papers",
            "Principles of teaching & classroom management guide",
        ],
        "Specialization": [
            "Major specialization review modules aligned to your track",
            "Field-specific practice drills and diagnostic sets",
        ],
    },
    "CPA": {
        "FAR": [
            "FAR practice problem sets",
            "PAS/PFRS summary guide",
            "Financial accounting video lectures",
        ],
        "AFAR": [
            "AFAR consolidated problem drills",
            "Advanced accounting concepts workbook",
        ],
        "Auditing": [
            "Auditing theory & practice reviewer",
            "PSA summary notes",
        ],
        "MAS": [
            "Management advisory services formula sheet",
            "MAS practice exams",
        ],
        "RFBT": [
            "Regulatory framework reviewer",
            "Business law & SEC regulations digest",
        ],
        "Taxation": [
            "Taxation law reviewer",
            "BIR tax computation practice sets",
        ],
    },
    "Internal Certification": {
        "Core": ["Core competency modules", "Foundational concept drills"],
        "Applied": ["Applied case studies", "Scenario-based practice sets"],
        "Practicum": ["Practicum checklists", "Hands-on simulation guides"],
    },
}

DIFFICULTY_BANDS = {
    "board": {
        "level": "board",
        "label": "Board-Level",
        "note": "Full-length timed mock boards at exam standard.",
    },
    "intermediate": {
        "level": "intermediate",
        "label": "Intermediate",
        "note": "Mixed-subject drills approaching board difficulty.",
    },
    "guided": {
        "level": "guided",
        "label": "Guided",
        "note": "Subject-focused practice with open review references.",
    },
    "foundational": {
        "level": "foundational",
        "label": "Foundational",
        "note": "Remedial drills focusing on fundamentals before board practice.",
    },
}

DIFFICULTY_BREAKPOINTS = [60, 75, 90]
DIFFICULTY_ORDER = ["foundational", "guided", "intermediate", "board"]


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


async def _load_action_history(db, user_id: str) -> dict:
    history = defaultdict(lambda: {"success": 0.0, "failure": 0.0})
    cursor = db.rl_events.find(
        {"user_id": user_id, "event_type": "feedback"},
        {"action_id": 1, "reward": 1},
    )
    for event in await cursor.to_list(length=500):
        reward = float(event.get("reward", 0))
        success = max(reward, 0.0)
        failure = max(-reward, 0.0)
        stats = history[event.get("action_id")]
        stats["success"] += success
        stats["failure"] += failure
    return history


def _thompson_sample(history: dict) -> str:
    # Draw one sample from each action's Beta posterior (prior Beta(1,1) =
    # uniform before any rewards). The action with the highest draw wins.
    samples = {
        action_id: random.betavariate(
            1.0 + history.get(action_id, {}).get("success", 0.0),
            1.0 + history.get(action_id, {}).get("failure", 0.0),
        )
        for action_id in ACTION_DEFINITIONS
    }
    return max(samples, key=samples.get)


def _explain_thompson(action_id: str, sample_count: int, context: dict) -> str:
    weak = " · ".join(context.get("weak_subjects") or [])
    return (
        f"Thompson sampling over {sample_count} observed outcome(s) selected "
        f"{ACTION_DEFINITIONS[action_id]}. Weak areas: {weak or 'none yet'}."
    )


def _baseline_pick_action(context: dict):
    action_id = random.choice(list(ACTION_DEFINITIONS.keys()))
    return action_id, "Uniform random exploration (baseline arm, no learning)."


def _recommend_difficulty(context: dict) -> dict:
    score = context["latest_score"]
    mastery = context["subject_mastery"]
    denominator = len(mastery) or 1
    avg_mastery = round(sum(mastery.values()) / denominator, 2)
    reference = max(score, avg_mastery)
    index = bisect.bisect_left(DIFFICULTY_BREAKPOINTS, reference)
    return dict(DIFFICULTY_BANDS[DIFFICULTY_ORDER[index]])


def _recommend_materials(context: dict) -> list:
    target = context.get("target_licensure") or ""
    catalog = REVIEW_MATERIALS.get(target, {})
    weak = context["weak_subjects"]
    ordered = []
    for subject in weak:
        if subject in catalog:
            ordered.append({"subject": subject, "items": catalog[subject]})
    # Cover remaining assigned subjects that have materials.
    for subject in catalog:
        if subject not in weak:
            ordered.append({"subject": subject, "items": catalog[subject]})
    return ordered[:4]


def _recommend_schedule(context: dict, action_id: str) -> list:
    weak = context["weak_subjects"]
    weak_focus = " · ".join(weak) if weak else "weakest areas"
    base = {
        "subject_drill": [
            ("Day 1", f"Focused drill on {weak_focus}", "60 min"),
            ("Day 2", f"Continue {weak_focus} remediation", "60 min"),
            ("Day 3", "Mixed quiz: 30 items across all subjects", "45 min"),
            ("Day 4", f"Re-test {weak_focus} after review", "45 min"),
            ("Day 5", "Timed practice set at target difficulty", "60 min"),
            ("Day 6", "Full mock exam", "120 min"),
            ("Day 7", "Review mistakes & create summary notes", "45 min"),
        ],
        "mixed_quiz": [
            ("Day 1", "Mixed quiz: 40 items (all subjects)", "50 min"),
            ("Day 2", f"Targeted review of {weak_focus}", "60 min"),
            ("Day 3", "Timed mixed quiz: 30 items", "45 min"),
            ("Day 4", f"Drill {weak_focus}", "60 min"),
            ("Day 5", "Mixed quiz: 50 items", "60 min"),
            ("Day 6", "Full mock exam", "120 min"),
            ("Day 7", "Analyze errors & plan next week", "30 min"),
        ],
        "timed_mock": [
            ("Day 1", "Full-length timed mock exam", "120 min"),
            ("Day 2", f"Score review & {weak_focus} remediation", "60 min"),
            ("Day 3", "Timed practice set", "60 min"),
            ("Day 4", f"Drill {weak_focus}", "60 min"),
            ("Day 5", "Timed mixed quiz: 40 items", "50 min"),
            ("Day 6", "Full mock exam", "120 min"),
            ("Day 7", "Review & recovery / light reading", "45 min"),
        ],
        "remedial_lesson": [
            ("Day 1", f"Remedial lesson: {weak_focus}", "60 min"),
            ("Day 2", "Fundamental concept drills", "60 min"),
            ("Day 3", f"Continue {weak_focus} remediation", "60 min"),
            ("Day 4", "Guided practice set (open references)", "60 min"),
            ("Day 5", f"Re-test {weak_focus}", "45 min"),
            ("Day 6", "Light timed quiz: 20 items", "40 min"),
            ("Day 7", "Rest & consolidate notes", "30 min"),
        ],
    }
    rows = base.get(action_id, base["mixed_quiz"])
    return [{"day": d, "activity": a, "duration": dur} for d, a, dur in rows]


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

    if not attempts:
        return {
            "has_recommendation": False,
            "recommendation_id": None,
            "rl_enabled": rl_enabled,
            "action_id": None,
            "action_label": None,
            "reason": None,
            "focus_subjects": [],
            "latest_score": 0,
            "difficulty": None,
            "materials": [],
            "schedule": [],
        }

    context = _build_context(profile, attempts, passing_threshold)

    experiment_group = _experiment_group(str(user["_id"]))
    recommendation_id = str(uuid4())
    policy_mode = "bandit" if (rl_enabled and experiment_group == "bandit") else "baseline"
    if policy_mode == "bandit":
        history = await _load_action_history(db, str(user["_id"]))
        action_id = _thompson_sample(history)
        sample_count = int(
            sum(
                history[action_id]["success"] + history[action_id]["failure"]
                for action_id in ACTION_DEFINITIONS
            )
        )
        reason = _explain_thompson(action_id, sample_count, context)
    else:
        action_id, reason = _baseline_pick_action(context)

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
        "has_recommendation": True,
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
        "difficulty": _recommend_difficulty(context),
        "materials": _recommend_materials(context),
        "schedule": _recommend_schedule(context, action_id),
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
    policy_mode_counts = {
        "baseline": 0,
        "bandit": 0,
    }

    for event in recommendations:
        group = event.get("experiment_group")
        if group in by_group:
            by_group[group]["recommendations"] += 1
        mode = event.get("policy_mode")
        if mode in policy_mode_counts:
            policy_mode_counts[mode] += 1

    for event in feedback:
        group = event.get("experiment_group")
        if group in by_group:
            by_group[group]["feedback_count"] += 1
            reward_acc[group] += float(event.get("reward", 0))

    for group in ("baseline", "bandit"):
        count = by_group[group]["feedback_count"]
        by_group[group]["avg_reward"] = round(reward_acc[group] / count, 4) if count else 0.0

    settings = await db.app_settings.find_one({}) or {}
    rl_enabled = bool(settings.get("rl_enabled", False))

    return {
        "policy_version": POLICY_VERSION,
        "window_days": 30,
        "rl_enabled": rl_enabled,
        "experiment_split": EXPERIMENT_SPLIT,
        "recommendations_total": len(recommendations),
        "feedback_total": len(feedback),
        "action_distribution": action_counts,
        "policy_mode_counts": policy_mode_counts,
        "ab_groups": by_group,
    }
