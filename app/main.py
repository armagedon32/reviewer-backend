from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .auth import router as auth_router
from .profile import router as profile_router
from .exam import router as exam_router
from .questions import router as questions_router, seed_questions
from .admin import router as admin_router
from .access import router as access_router
from .database import SessionLocal, engine
from .admin import get_or_create_settings
from .auth import ensure_admin_user
from alembic import command
from alembic.config import Config
import os
from dotenv import load_dotenv
load_dotenv()

def get_cors_origins():
    origins = os.getenv("CORS_ORIGINS", "")
    if origins:
        return [origin.strip() for origin in origins.split(",") if origin.strip()]
    return ["http://localhost:5173"]




app = FastAPI(title="Reviewer Platform API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
  if os.getenv("AUTO_MIGRATE", "0") == "1":
    alembic_cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    command.upgrade(alembic_cfg, "head")
  db = SessionLocal()
  try:
    seed_questions(db)
    get_or_create_settings(db)
    ensure_admin_user(db)
  finally:
    db.close()


app.include_router(auth_router)


@app.get("/")
def root():
    return {"status": "FastAPI backend is running"}


app.include_router(profile_router)
app.include_router(exam_router)
app.include_router(questions_router)
app.include_router(admin_router)
app.include_router(access_router)
