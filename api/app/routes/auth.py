from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..security import (allowed_tools, create_token, current_user,
                        load_active_user, verify_password)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


def _public(user: dict) -> dict:
    return {
        "id": user["id"],
        "name": user["name"],
        "role": user["role_name"],
        "status": user["status"],
        "start_date": user["start_date"],
        "end_date": user["end_date"],
        "tools": sorted(t["name"] for t in allowed_tools(user["role_id"])),
    }


@router.post("/login")
def login(body: LoginIn):
    row = db.q1("SELECT * FROM users WHERE name = ?", body.name)
    # Одинаковый ответ на «нет юзера» и «неверный пароль»: не даём перечислять
    # имена. Хеш проверяем всегда, чтобы не было тайминг-разницы.
    stored = row["password_hash"] if row else "pbkdf2_sha256$1$00$00"
    if not verify_password(body.password, stored) or row is None:
        raise HTTPException(status_code=401, detail="неверный логин или пароль")

    user = load_active_user(row["id"])          # здесь же status и окно дат
    token, exp = create_token(user["id"])
    return {"token": token, "expires_at": exp.isoformat(), "user": _public(user)}


@router.get("/me")
def me(user: dict = Depends(current_user)):
    return _public(user)
