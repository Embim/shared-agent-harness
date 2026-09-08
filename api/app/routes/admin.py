from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..security import hash_password, require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


class UserIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=4, max_length=256)
    role: str = "read"
    start_date: str | None = None      # YYYY-MM-DD
    end_date: str | None = None


class UserPatch(BaseModel):
    status: str | None = None          # active | blocked
    role: str | None = None
    password: str | None = None
    start_date: str | None = None
    end_date: str | None = None


def _role_id(name: str) -> int:
    row = db.q1("SELECT id FROM roles WHERE name = ?", name)
    if row is None:
        raise HTTPException(status_code=400, detail="неизвестная роль '%s'" % name)
    return row["id"]


@router.get("/users")
def users(_: dict = Depends(require_admin)):
    rows = db.q(
        "SELECT u.id, u.name, u.status, u.start_date, u.end_date, u.created_at,"
        " r.name AS role FROM users u JOIN roles r ON r.id = u.role_id ORDER BY u.id")
    return [dict(r) for r in rows]


@router.post("/users")
def create_user(body: UserIn, _: dict = Depends(require_admin)):
    if db.q1("SELECT id FROM users WHERE name = ?", body.name):
        raise HTTPException(status_code=409, detail="пользователь уже существует")
    uid = db.ex(
        "INSERT INTO users (name, password_hash, role_id, status, start_date, end_date) "
        "VALUES (?,?,?,'active',?,?)",
        body.name, hash_password(body.password), _role_id(body.role),
        body.start_date, body.end_date)
    return {"id": uid}


@router.patch("/users/{uid}")
def patch_user(uid: int, body: UserPatch, admin: dict = Depends(require_admin)):
    if db.q1("SELECT id FROM users WHERE id = ?", uid) is None:
        raise HTTPException(status_code=404, detail="пользователь не найден")
    if uid == admin["id"] and body.status == "blocked":
        raise HTTPException(status_code=400, detail="нельзя заблокировать себя")

    sets, vals = [], []
    if body.status is not None:
        if body.status not in ("active", "blocked"):
            raise HTTPException(status_code=400, detail="status: active | blocked")
        sets.append("status = ?"); vals.append(body.status)
    if body.role is not None:
        sets.append("role_id = ?"); vals.append(_role_id(body.role))
    if body.password is not None:
        sets.append("password_hash = ?"); vals.append(hash_password(body.password))
    if body.start_date is not None:
        sets.append("start_date = ?"); vals.append(body.start_date or None)
    if body.end_date is not None:
        sets.append("end_date = ?"); vals.append(body.end_date or None)
    if not sets:
        return {"ok": True, "changed": 0}

    db.ex("UPDATE users SET %s WHERE id = ?" % ", ".join(sets), *vals, uid)
    # Токен у пользователя остаётся валидным, но права и статус читаются из БД
    # на каждом запросе и перед каждым тулом — изменение вступает в силу сразу.
    return {"ok": True, "changed": len(sets)}


@router.get("/roles")
def roles(_: dict = Depends(require_admin)):
    out = []
    for r in db.q("SELECT * FROM roles ORDER BY id"):
        names = db.q("SELECT t.name FROM tools t JOIN role_tool rt ON rt.tool_id = t.id "
                     "WHERE rt.role_id = ? ORDER BY t.name", r["id"])
        out.append({"id": r["id"], "name": r["name"], "tools": [x["name"] for x in names]})
    return out


@router.get("/tools")
def tools_list(_: dict = Depends(require_admin)):
    return [dict(r) for r in db.q("SELECT id, name, kind, description, enabled, version "
                                  "FROM tools ORDER BY name")]


@router.get("/logs")
def logs(limit: int = 200, session: str | None = None, user: str | None = None,
         kind: str | None = None, _: dict = Depends(require_admin)):
    sql = ("SELECT e.*, u.name AS user_name FROM events e "
           "LEFT JOIN users u ON u.id = e.user_id WHERE 1=1")
    params: list = []
    if session:
        sql += " AND e.session_id = ?"; params.append(session)
    if user:
        sql += " AND u.name = ?"; params.append(user)
    if kind:
        sql += " AND e.kind = ?"; params.append(kind)
    sql += " ORDER BY e.id DESC LIMIT ?"
    params.append(min(max(limit, 1), 2000))
    return [db.row_to_dict(r) for r in db.q(sql, *params)]


@router.get("/stats")
def stats(_: dict = Depends(require_admin)):
    row = db.q1(
        "SELECT COUNT(*) AS events, "
        " SUM(CASE WHEN kind IN ('tool_call','tool_result') AND status='ok' THEN 1 ELSE 0 END) AS tool_ok, "
        " SUM(CASE WHEN kind='blocked' THEN 1 ELSE 0 END) AS blocked, "
        " SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors, "
        " COALESCE(SUM(tokens_in),0) AS tokens_in, "
        " COALESCE(SUM(tokens_out),0) AS tokens_out FROM events")
    return dict(row)
