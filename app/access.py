from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from .auth import get_current_user, get_current_user_allow_inactive
from .database import get_database
from .db_models import AuditLog, User
from .audit import log_event_async

REQUEST_TTL_SECONDS = 60 * 60
ACCESS_ACTIONS = ("access_request", "access_approved", "access_denied")

router = APIRouter(prefix="/access", tags=["Access"])


class AccessRequest(BaseModel):
    detail: Optional[str] = None


async def _latest_access_log(db, user_id: str):
    return await db.audit_logs.find_one(
        {"user_id": user_id, "action": {"$in": list(ACCESS_ACTIONS)}},
        sort=[("created_at", -1)]
    )


@router.post("/request")
async def request_access(
    payload: AccessRequest,
    current_user=Depends(get_current_user_allow_inactive),
    db = Depends(get_database),
):
    user = await db.users.find_one({"email": current_user["email"]})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    detail = payload.detail or f"Requested access ({user['role']})"
    if user["role"] != "admin":
        await db.users.update_one({"_id": user["_id"]}, {"$set": {"active": False}})
    await log_event_async(db, str(user["_id"]), "access_request", detail)
    return {
        "status": "pending",
        "requested_at": datetime.utcnow().isoformat(),
    }


@router.get("/status")
async def access_status(
    current_user=Depends(get_current_user_allow_inactive),
    db = Depends(get_database),
):
    user = await db.users.find_one({"email": current_user["email"]})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user["role"] == "admin":
        return {"status": "approved"}
    latest = await _latest_access_log(db, str(user["_id"]))
    if not latest:
        return {"status": "approved" if user.get("active", True) else "pending"}
    if latest["action"] == "access_approved":
        return {"status": "approved", "updated_at": latest["created_at"].isoformat()}
    if latest["action"] == "access_denied":
        return {"status": "denied", "updated_at": latest["created_at"].isoformat()}
    if latest["action"] == "access_request":
        cutoff = datetime.utcnow() - timedelta(seconds=REQUEST_TTL_SECONDS)
        if latest["created_at"] < cutoff:
            return {"status": "expired", "requested_at": latest["created_at"].isoformat()}
        return {"status": "pending", "requested_at": latest["created_at"].isoformat()}
    return {"status": "pending"}
