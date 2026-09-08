import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import agent, db, events, llm, tools
from ..config import settings
from ..security import current_user

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class CreateIn(BaseModel):
    title: str = Field(default="session", max_length=120)
    model: str | None = None
    system_prompt: str | None = None


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


class PatchIn(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    model: str | None = None
    system_prompt: str | None = None


def _session_or_404(sid: str) -> dict:
    s = db.row_to_dict(db.q1("SELECT * FROM sessions WHERE id = ?", sid))
    if s is None:
        raise HTTPException(status_code=404, detail="сессия не найдена")
    return s


def _info(s: dict) -> dict:
    r = agent.runner(s["id"])
    owner = db.q1("SELECT name FROM users WHERE id = ?", s["owner_id"])
    return {**s,
            "owner": owner["name"] if owner else None,
            "busy": r.busy(),
            "queued": r.queued(),
            "viewers": events.hub.viewers(s["id"])}


# --------------------------------------------------------------------------
# init: проверяем провайдера, наличие модели и свободные мощности песочницы,
# затем заводим сессию и пишем session_init в журнал.
# --------------------------------------------------------------------------
@router.post("")
async def create(body: CreateIn, user: dict = Depends(current_user)):
    model = body.model or settings.llm_model
    probe = await llm.probe(model, fresh=True)
    if not probe["ok"]:
        raise HTTPException(status_code=503, detail={"stage": "llm", **probe})
    if probe.get("model_available") is False:
        raise HTTPException(status_code=503, detail={
            "stage": "llm", "error": "модель '%s' недоступна у провайдера" % model,
            "models": probe.get("models", [])})

    capacity = await tools.sandbox_capacity()
    if not capacity["ok"]:
        raise HTTPException(status_code=503, detail={"stage": "sandbox", **capacity})

    sid = str(uuid.uuid4())
    db.ex(
        "INSERT INTO sessions (id, title, owner_id, model, system_prompt, state) "
        "VALUES (?,?,?,?,?, 'idle')",
        sid, body.title, user["id"], model,
        body.system_prompt or settings.system_prompt,
    )
    events.append(sid, "session_init", user_id=user["id"], model=model,
                  payload={"by": user["name"], "llm": probe, "sandbox": capacity})
    return _info(_session_or_404(sid))


@router.get("")
def index(_: dict = Depends(current_user)):
    rows = db.q("SELECT * FROM sessions ORDER BY created_at DESC LIMIT 100")
    return [_info(db.row_to_dict(r)) for r in rows]


@router.get("/{sid}")
def show(sid: str, _: dict = Depends(current_user)):
    return _info(_session_or_404(sid))


@router.patch("/{sid}")
async def patch(sid: str, body: PatchIn, user: dict = Depends(current_user)):
    """Модель пинится к сессии при создании — это нужно для воспроизводимости
    журнала. Но провайдера могли переключить, и тогда старая сессия остаётся
    привязанной к недоступной модели; здесь её можно перевести на живую."""
    s = _session_or_404(sid)
    if user["role_name"] != "admin" and user["id"] != s["owner_id"]:
        raise HTTPException(status_code=403, detail="менять может владелец или админ")
    if agent.runner(sid).busy():
        raise HTTPException(status_code=409, detail="сессия занята, дождитесь конца хода")

    if body.model and body.model != s["model"]:
        probe = await llm.probe(body.model, fresh=True)
        if not probe["ok"] or probe.get("model_available") is False:
            raise HTTPException(status_code=400, detail={
                "error": "модель '%s' недоступна у провайдера" % body.model,
                "models": probe.get("models", [])})
        db.ex("UPDATE sessions SET model = ? WHERE id = ?", body.model, sid)
        events.append(sid, "session_init", user_id=user["id"], model=body.model,
                      payload={"by": user["name"], "changed_model_from": s["model"]})
    if body.title:
        db.ex("UPDATE sessions SET title = ? WHERE id = ?", body.title, sid)
    if body.system_prompt is not None:
        db.ex("UPDATE sessions SET system_prompt = ? WHERE id = ?", body.system_prompt, sid)
    return _info(_session_or_404(sid))


@router.get("/{sid}/events")
def event_log(sid: str, after: int = 0, _: dict = Depends(current_user)):
    _session_or_404(sid)
    return events.since(sid, after)


@router.post("/{sid}/messages")
# ОБЯЗАТЕЛЬНО async: submit() поднимает таск раннера и публикует событие в
# in-memory hub. Синхронный хендлер FastAPI выполняет в threadpool, где нет
# запущенного event loop -> asyncio.create_task() падает с RuntimeError.
async def post_message(sid: str, body: MessageIn, user: dict = Depends(current_user)):
    s = _session_or_404(sid)
    if s["state"] == "closed":
        raise HTTPException(status_code=409, detail="сессия закрыта")
    r = agent.runner(sid)
    # Сообщение не вклинивается в текущий ход: оно встаёт в очередь и будет
    # обработано следующим ходом. Это и есть граница хода.
    event_id = r.submit(user, body.text)
    return {"event_id": event_id, "queued": r.queued(), "busy": r.busy()}


@router.post("/{sid}/cancel")
async def cancel(sid: str, user: dict = Depends(current_user)):
    _session_or_404(sid)
    stopped = await agent.runner(sid).cancel(user)
    return {"cancelled": stopped}


@router.post("/{sid}/close")
async def close(sid: str, user: dict = Depends(current_user)):   # тоже пишет в hub
    s = _session_or_404(sid)
    if user["role_name"] != "admin" and user["id"] != s["owner_id"]:
        raise HTTPException(status_code=403, detail="закрыть может владелец или админ")
    events.publish_state(sid, "closed", {"by": user["name"]})
    return {"ok": True}


# --------------------------------------------------------------------------
# SSE. Порядок важен: сначала подписка, потом реплей из БД, потом живые
# кадры с дедупликацией по id. Клиент переподключается с ?after=<last_id>,
# поэтому разрыв не теряет и не дублирует события.
# --------------------------------------------------------------------------
def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False, default=str) + "\n\n"


# Разрыв соединения ловит сам StreamingResponse (у него есть listen_for_disconnect),
# поэтому тут ничего проверять не надо — и НЕЛЬЗЯ: второй читатель ASGI-канала
# receive() конфликтует с ним и подвешивает ответ.
PING_SECONDS = 15            # keep-alive

# Проброс портов Docker Desktop на Windows буферизует chunked-ответ, пока не
# наберётся заметный объём: изнутри контейнера стрим отдаётся за 1 мс, а с хоста
# маленький реплей (~2 КБ) висел в буфере по 8-16 с, и вторая вкладка видела
# пустой диалог. Преамбула из SSE-комментария сразу переполняет этот буфер.
# Клиенты строки, начинающиеся с ':', игнорируют по спецификации.
_PREAMBLE = ":" + " " * 8192 + "\n\n"


@router.get("/{sid}/stream")
async def stream(sid: str, after: int = 0,
                 _: dict = Depends(current_user)):
    s = _session_or_404(sid)

    async def gen():
        q = events.hub.subscribe(sid)
        try:
            yield _PREAMBLE
            last = after
            for row in events.since(sid, after):
                last = row["id"]
                yield _sse(row)
            r = agent.runner(sid)
            yield _sse({"id": 0, "kind": "state", "state": s["state"],
                        "payload": {"queued": r.queued(), "busy": r.busy()}})
            while True:
                # НЕ вызываем request.is_disconnected(): StreamingResponse уже
                # слушает разрыв через receive(), и второй потребитель того же
                # ASGI-канала подвешивает ответ (заголовки уезжали на секунды).
                try:
                    frame = await asyncio.wait_for(q.get(), timeout=PING_SECONDS)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                fid = frame.get("id") or 0
                if fid and fid <= last:
                    continue
                if fid:
                    last = fid
                yield _sse(frame)
        finally:
            events.hub.unsubscribe(sid, q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })
