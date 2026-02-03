from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .auth import router as auth_router
from .profile import router as profile_router
from .exam import router as exam_router
from .questions import router as questions_router, seed_questions
from .admin import router as admin_router
from .access import router as access_router
from .database import get_database
from .admin import get_or_create_settings
from .auth import ensure_admin_user
import os
from dotenv import load_dotenv
load_dotenv()

def get_cors_settings():
    origins_raw = os.getenv("CORS_ORIGINS", "")
    regex_raw = os.getenv("CORS_ORIGIN_REGEX", "")
    origins = [origin.strip() for origin in origins_raw.split(",") if origin.strip()]
    regex = regex_raw.strip() if regex_raw else ""
    if not origins:
        origins = ["http://localhost:5173"]
    return origins, regex




app = FastAPI(title="Reviewer Platform API")

cors_origins, cors_regex = get_cors_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_origin_regex=cors_regex or None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    db = get_database()
    await seed_questions(db)
    await get_or_create_settings(db)
    await ensure_admin_user(db)
    print("CORS_ORIGINS:", cors_origins)
    if cors_regex:
        print("CORS_ORIGIN_REGEX:", cors_regex)


app.include_router(auth_router)


@app.get("/")
def root():
    return {"status": "FastAPI backend is running"}


app.include_router(profile_router)
app.include_router(exam_router)
app.include_router(questions_router)
app.include_router(admin_router)
app.include_router(access_router)
