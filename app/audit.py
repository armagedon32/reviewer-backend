from datetime import datetime


async def log_event_async(db, user_id, action: str, detail: str):
    entry = {
        "user_id": user_id,
        "action": action,
        "detail": detail,
        "created_at": datetime.utcnow()
    }
    await db.audit_logs.insert_one(entry)
