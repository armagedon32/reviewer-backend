from datetime import datetime, timedelta
import os
from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import OAuth2PasswordBearer
from jose import jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session
from .models import AdminRegisterRequest, ChangePasswordRequest, RegisterRequest, LoginRequest
from .config import SECRET_KEY, ALGORITHM
from .database import get_db
from .db_models import User
from .audit import log_event


ACCESS_TOKEN_EXPIRE_MINUTES = 120  # Set token validity to 2 hours

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email = payload.get("sub")
        role = payload.get("role")
        if not email:
            raise HTTPException(status_code=401, detail="Invalid token")
        user = db.query(User).filter(User.email == email).first()
        if not user or not user.active:
            raise HTTPException(status_code=403, detail="User is inactive")
        if user.must_change_password:
            raise HTTPException(status_code=403, detail="Password reset required")
        return {"email": email, "role": role}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")


def get_current_user_allow_inactive(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email = payload.get("sub")
        role = payload.get("role")
        if not email:
            raise HTTPException(status_code=401, detail="Invalid token")
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(status_code=401, detail="Invalid token")
        return {"email": email, "role": role}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")


def get_current_user_allow_password_reset(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email = payload.get("sub")
        role = payload.get("role")
        if not email:
            raise HTTPException(status_code=401, detail="Invalid token")
        user = db.query(User).filter(User.email == email).first()
        if not user or not user.active:
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


def ensure_admin_user(db: Session):
    admin_email = os.getenv("ADMIN_EMAIL", "admin@local")
    admin_password = os.getenv("ADMIN_PASSWORD", "admin123")
    admin_exists = db.query(User).filter(User.role == "admin").first()
    if admin_exists:
        return
    user = User(
        email=admin_email,
        password_hash=hash_password(admin_password),
        role="admin",
        active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    log_event(db, user.id, "admin_seed", f"Seeded admin {admin_email}")


@router.post("/register")
def register(data: RegisterRequest, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.email == data.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="User already exists")
    if data.role == "admin":
        raise HTTPException(status_code=403, detail="Admin registration is restricted")

    user = User(
        email=data.email,
        password_hash=hash_password(data.password),
        role=data.role,
        active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    log_event(db, user.id, "register", f"Registered as {user.role}")

    return {
        "message": "User registered successfully",
        "email": user.email,
        "role": user.role,
    }


@router.post("/login")
def login(data: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == data.email).first()

    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.active:
        raise HTTPException(status_code=403, detail="User is inactive")
    if user.must_change_password and user.temp_password_expires_at:
        if datetime.utcnow() > user.temp_password_expires_at:
            raise HTTPException(status_code=403, detail="Temporary password expired")

    access_token = create_access_token({
        "sub": user.email,
        "role": user.role,
    })

    log_event(db, user.id, "login", "User logged in")
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "role": user.role,
        "must_change_password": user.must_change_password,
    }


@router.post("/register-admin")
def register_admin(data: AdminRegisterRequest, db: Session = Depends(get_db)):
    admin_key = os.getenv("ADMIN_REGISTER_KEY", "")
    if not admin_key or data.admin_key != admin_key:
        raise HTTPException(status_code=403, detail="Invalid admin key")

    existing = db.query(User).filter(User.email == data.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="User already exists")

    user = User(
        email=data.email,
        password_hash=hash_password(data.password),
        role="admin",
        active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    log_event(db, user.id, "register", "Registered as admin")
    return {
        "message": "Admin registered successfully",
        "email": user.email,
        "role": user.role,
    }


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    current_user=Depends(get_current_user_allow_password_reset),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == current_user["email"]).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password is too short")
    if verify_password(payload.new_password, user.password_hash):
        raise HTTPException(status_code=400, detail="New password must be different")

    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    user.temp_password_expires_at = None
    db.commit()
    log_event(db, user.id, "password_change", "Password updated")

    access_token = create_access_token({
        "sub": user.email,
        "role": user.role,
    })
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "role": user.role,
        "must_change_password": False,
    }
