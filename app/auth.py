from datetime import datetime, timedelta
import os
from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import OAuth2PasswordBearer
from jose import jwt
from passlib.context import CryptContext
from .models import AdminRegisterRequest, ChangePasswordRequest, RegisterRequest, LoginRequest
from .config import SECRET_KEY, ALGORITHM
from .database import get_database
from .db_models import User
from .audit import log_event_async


ACCESS_TOKEN_EXPIRE_MINUTES = 120  # Set token validity to 2 hours

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db = Depends(get_database),
):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email = payload.get("sub")
        role = payload.get("role")
        if not email:
            raise HTTPException(status_code=401, detail="Invalid token")
        user = await db.users.find_one({"email": email})
        if not user or not user.get("active", True):
            raise HTTPException(status_code=403, detail="User is inactive")
        if user.get("must_change_password", False):
            raise HTTPException(status_code=403, detail="Password reset required")
        return {"email": email, "role": role}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")


async def get_current_user_allow_inactive(
    token: str = Depends(oauth2_scheme),
    db = Depends(get_database),
):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email = payload.get("sub")
        role = payload.get("role")
        if not email:
            raise HTTPException(status_code=401, detail="Invalid token")
        user = await db.users.find_one({"email": email})
        if not user:
            raise HTTPException(status_code=401, detail="Invalid token")
        return {"email": email, "role": role}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")


async def get_current_user_allow_password_reset(
    token: str = Depends(oauth2_scheme),
    db = Depends(get_database),
):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email = payload.get("sub")
        role = payload.get("role")
        if not email:
            raise HTTPException(status_code=401, detail="Invalid token")
        user = await db.users.find_one({"email": email})
        if not user or not user.get("active", True):
            raise HTTPException(status_code=403, detail="User is inactive")
        return {"email": email, "role": role}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")


router = APIRouter(prefix="/auth", tags=["Auth"])

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


async def ensure_admin_user(db):
    admin_email = os.getenv("ADMIN_EMAIL", "admin@example.com")
    admin_password = os.getenv("ADMIN_PASSWORD", "admin123")
    admin_exists = await db.users.find_one({"role": "admin"})
    if admin_exists:
        return
    user_data = {
        "email": admin_email,
        "password_hash": hash_password(admin_password),
        "role": "admin",
        "active": True,
        "must_change_password": False,
        "created_at": datetime.utcnow()
    }
    result = await db.users.insert_one(user_data)
    await log_event_async(db, str(result.inserted_id), "admin_seed", f"Seeded admin {admin_email}")


@router.post("/register")
async def register(data: RegisterRequest, db = Depends(get_database)):
    raise HTTPException(status_code=403, detail="Self-registration is disabled. Contact an admin.")


@router.post("/login")
async def login(data: LoginRequest, db = Depends(get_database)):
    user = await db.users.find_one({"email": data.email})

    if not user or not verify_password(data.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.get("active", True):
        raise HTTPException(status_code=403, detail="User is inactive")
    if user.get("must_change_password", False) and user.get("temp_password_expires_at"):
        if datetime.utcnow() > user["temp_password_expires_at"]:
            raise HTTPException(status_code=403, detail="Temporary password expired")

    access_token = create_access_token({
        "sub": user["email"],
        "role": user["role"],
    })

    await log_event_async(db, str(user["_id"]), "login", "User logged in")
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "role": user["role"],
        "must_change_password": user.get("must_change_password", False),
    }


@router.post("/register-admin")
async def register_admin(data: AdminRegisterRequest, db = Depends(get_database)):
    admin_key = os.getenv("ADMIN_REGISTER_KEY", "")
    if not admin_key or data.admin_key != admin_key:
        raise HTTPException(status_code=403, detail="Invalid admin key")

    existing = await db.users.find_one({"email": data.email})
    if existing:
        raise HTTPException(status_code=400, detail="User already exists")

    user_data = {
        "email": data.email,
        "password_hash": hash_password(data.password),
        "role": "admin",
        "active": True,
        "must_change_password": False,
        "created_at": datetime.utcnow()
    }
    result = await db.users.insert_one(user_data)
    await log_event_async(db, str(result.inserted_id), "register", "Registered as admin")
    return {
        "message": "Admin registered successfully",
        "email": user_data["email"],
        "role": user_data["role"],
    }


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    current_user=Depends(get_current_user_allow_password_reset),
    db = Depends(get_database),
):
    user = await db.users.find_one({"email": current_user["email"]})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if not verify_password(payload.current_password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password is too short")
    if verify_password(payload.new_password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="New password must be different")

    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {
            "password_hash": hash_password(payload.new_password),
            "must_change_password": False,
            "temp_password_expires_at": None
        }}
    )
    await log_event_async(db, str(user["_id"]), "password_change", "Password updated")

    access_token = create_access_token({
        "sub": user["email"],
        "role": user["role"],
    })
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "role": user["role"],
        "must_change_password": False,
    }
