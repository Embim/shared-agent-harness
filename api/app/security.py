import hashlib
import hmac
import os
import uuid
from datetime import date, datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import db
from .config import settings

_ITERATIONS = 200_000
_bearer = HTTPBearer(auto_error=False)


# ------------------------------------------------------------------ пароли
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# -------------------------------------------------------------------- JWT
def create_token(user_id: int) -> tuple[str, datetime]:
    exp = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_ttl_min)
    payload = {
        # В токене ТОЛЬКО идентичность. Роль, status и окно дат намеренно
        # не кладём: они меняются админом, а выданный токен уже не отозвать.
        "sub": str(user_id),
        "jti": uuid.uuid4().hex,
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg), exp


def decode_token(token: str) -> dict:
    # algorithms пиним явно: иначе alg=none / algorithm confusion.
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_alg])


# --------------------------------------------------------- проверка юзера
def _deny(msg: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=msg)


def load_active_user(user_id: int) -> dict:
    """Единственная точка правды о правах. Дёргается на КАЖДОМ запросе и
    ещё раз непосредственно перед execute каждого тула."""
    row = db.q1(
        "SELECT u.*, r.name AS role_name FROM users u "
        "JOIN roles r ON r.id = u.role_id WHERE u.id = ?",
        user_id,
    )
    if row is None:
        raise _deny("user not found")
    u = dict(row)
    if u["status"] != "active":
        raise _deny(f"user is {u['status']}")
    today = date.today().isoformat()
    if u["start_date"] and today < u["start_date"]:
        raise _deny("access window has not started")
    if u["end_date"] and today > u["end_date"]:
        raise _deny("access window has expired")
    return u


async def current_user(
    cred: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    if cred is None:
        raise _deny("missing bearer token")
    try:
        payload = decode_token(cred.credentials)
    except jwt.ExpiredSignatureError:
        raise _deny("token expired")
    except jwt.InvalidTokenError:
        raise _deny("invalid token")
    return load_active_user(int(payload["sub"]))


async def require_admin(user: dict = Depends(current_user)) -> dict:
    if user["role_name"] != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    return user


# ----------------------------------------------------------------- права
def allowed_tools(role_id: int) -> list[dict]:
    rows = db.q(
        "SELECT t.* FROM tools t "
        "JOIN role_tool rt ON rt.tool_id = t.id "
        "WHERE rt.role_id = ? AND t.enabled = 1 ORDER BY t.name",
        role_id,
    )
    return [dict(r) for r in rows]


def allowed_tool_names(role_id: int) -> set[str]:
    return {t["name"] for t in allowed_tools(role_id)}
