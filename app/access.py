from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from .auth import get_current_user, get_current_user_allow_inactive
from .database import get_db
from .db_models import AuditLog, User
from .audit import log_event

REQUEST_TTL_SECONDS = 60 * 60
ACCESS_ACTIONS = ("access_request", "access_approved", "access_denied")

router = APIRouter(prefix="/access", tags=["Access"])


class AccessRequest(BaseModel):
    detail: Optional[str] = None


def _latest_access_log(db: Session, user_id: int):
    return (
        db.query(AuditLog)
        .filter(AuditLog.user_id == user_id, AuditLog.action.in_(ACCESS_ACTIONS))
        .order_by(AuditLog.created_at.desc())
        .first()
    )


@router.post("/request")
def request_access(
    payload: AccessRequest,
    current_user=Depends(get_current_user_allow_inactive),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == current_user["email"]).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    detail = payload.detail or f"Requested access ({user.role})"
    if user.role != "admin":
        user.active = False
        db.commit()
    log_event(db, user.id, "access_request", detail)
    return {
        "status": "pending",
        "requested_at": datetime.utcnow().isoformat(),
    }


@router.get("/status")
def access_status(
    current_user=Depends(get_current_user_allow_inactive),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == current_user["email"]).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role == "admin":
        return {"status": "approved"}
    latest = _latest_access_log(db, user.id)
    if not latest:
        return {"status": "approved" if user.active else "pending"}
    if latest.action == "access_approved":
        return {"status": "approved", "updated_at": latest.created_at.isoformat()}
    if latest.action == "access_denied":
        return {"status": "denied", "updated_at": latest.created_at.isoformat()}
    if latest.action == "access_request":
        cutoff = datetime.utcnow() - timedelta(seconds=REQUEST_TTL_SECONDS)
        if latest.created_at < cutoff:
            return {"status": "expired", "requested_at": latest.created_at.isoformat()}
        return {"status": "pending", "requested_at": latest.created_at.isoformat()}
    return {"status": "pending"}
