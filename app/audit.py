from sqlalchemy.orm import Session
from .db_models import AuditLog


def log_event(db: Session, user_id, action: str, detail: str):
    entry = AuditLog(user_id=user_id, action=action, detail=detail)
    db.add(entry)
    db.commit()
