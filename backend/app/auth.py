# backend/app/auth.py
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from passlib.context import CryptContext
from sqlalchemy.orm import Session
import json
from datetime import datetime

from backend.app.db import SessionLocal, User

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

SESSION_KEY = "user"


def _user_to_dict(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "is_admin": bool(u.is_admin),
        "permissions": u.permissions_list(),
        "is_active": bool(u.is_active),
    }


def _ensure_default_admin(db: Session):
    exists = db.query(User).first()
    if exists:
        return

    admin = User(
        username="admin",
        password_hash=pwd_context.hash("Admin@123"),
        is_admin=True,
        permissions=json.dumps(["*"]),
        is_active=True,
        created_at=datetime.utcnow(),
    )
    db.add(admin)
    db.commit()


def authenticate(username: str, password: str):
    with SessionLocal() as db:
        _ensure_default_admin(db)

        u = (
            db.query(User)
            .filter(User.username == username, User.is_active == True)  # noqa: E712
            .first()
        )
        if not u:
            return None

        if not pwd_context.verify(password, u.password_hash):
            return None

        return _user_to_dict(u)


def login_user(request: Request, username: str):
    request.session[SESSION_KEY] = username


def logout_user(request: Request):
    request.session.pop(SESSION_KEY, None)


def get_current_user(request: Request):
    username = request.session.get(SESSION_KEY)
    if not username:
        return None

    with SessionLocal() as db:
        u = (
            db.query(User)
            .filter(User.username == username, User.is_active == True)  # noqa: E712
            .first()
        )
        if not u:
            return None
        return _user_to_dict(u)


def login_required(request: Request):
    user = get_current_user(request)
    if not user:
        next_url = str(request.url.path)
        if request.url.query:
            next_url += "?" + request.url.query
        return RedirectResponse(url=f"/login?next={next_url}", status_code=302)
    return user

def has_perm(user: dict, perm: str) -> bool:
    perms = user.get("permissions") or []
    return user.get("is_admin") is True or "*" in perms or perm in perms

def require_perm(perm: str):
    def _dep(request: Request):
        user = get_current_user(request)
        if not user:
            next_url = str(request.url.path)
            if request.url.query:
                next_url += "?" + request.url.query
            return RedirectResponse(f"/login?next={next_url}", status_code=302)

        if not has_perm(user, perm):
            return RedirectResponse("/account/settings?err=no_perm", status_code=302)
        return user
    return _dep
