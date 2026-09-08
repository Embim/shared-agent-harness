import asyncio
import json
from collections import defaultdict

from . import db


_loop: asyncio.AbstractEventLoop | None = None


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Запоминаем event loop приложения на старте: append() может быть вызван
    из синхронного хендлера (FastAPI выполняет их в threadpool), а asyncio.Queue
    из чужого потока трогать нельзя."""
    global _loop
    _loop = loop


class Hub:
    """Fan-out одной сессии на N наблюдателей. In-memory, поэтому API
    обязан крутиться в один воркер. Потеря очереди при разрыве не страшна:
    клиент переподключается с ?after=<last_id> и добирает из events."""

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, session_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs[session_id].add(q)
        return q

    def unsubscribe(self, session_id: str, q: asyncio.Queue) -> None:
        self._subs.get(session_id, set()).discard(q)
        if not self._subs.get(session_id):
            self._subs.pop(session_id, None)

    def viewers(self, session_id: str) -> int:
        return len(self._subs.get(session_id, ()))

    def publish(self, session_id: str, frame: dict) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # Нас позвали не из event loop (синхронный хендлер в threadpool).
            if _loop is not None:
                _loop.call_soon_threadsafe(self._deliver, session_id, frame)
            return
        self._deliver(session_id, frame)

    def _deliver(self, session_id: str, frame: dict) -> None:
        for q in list(self._subs.get(session_id, ())):
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                # Медленный читатель отваливается; он до-читает через ?after=
                self._subs[session_id].discard(q)


hub = Hub()


def append(
    session_id: str,
    kind: str,
    *,
    user_id: int | None = None,
    model: str | None = None,
    tool_name: str | None = None,
    tool_call_id: str | None = None,
    status: str | None = None,
    payload: dict | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    duration_ms: int | None = None,
) -> dict:
    payload = payload or {}
    eid = db.ex(
        "INSERT INTO events (session_id, user_id, kind, model, tool_name, tool_call_id,"
        " status, payload, tokens_in, tokens_out, duration_ms)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        session_id,
        user_id,
        kind,
        model,
        tool_name,
        tool_call_id,
        status,
        json.dumps(payload, ensure_ascii=False),
        tokens_in,
        tokens_out,
        duration_ms,
    )
    frame = db.row_to_dict(db.q1("SELECT * FROM events WHERE id = ?", eid))
    hub.publish(session_id, frame)
    return frame


def update(event_id: int, **fields) -> dict | None:
    """Закрытие 'pending' записи: статус, результат, длительность."""
    if "payload" in fields and isinstance(fields["payload"], dict):
        fields["payload"] = json.dumps(fields["payload"], ensure_ascii=False)
    cols = ", ".join(f"{k} = ?" for k in fields)
    db.ex(f"UPDATE events SET {cols} WHERE id = ?", *fields.values(), event_id)
    row = db.row_to_dict(db.q1("SELECT * FROM events WHERE id = ?", event_id))
    if row:
        hub.publish(row["session_id"], row)
    return row


def publish_state(session_id: str, state: str, extra: dict | None = None) -> None:
    """Состояние сессии — транзиентный кадр, в БД не пишем (иначе журнал
    заплывёт мусором). Актуальное значение всегда есть в sessions.state."""
    db.ex("UPDATE sessions SET state = ? WHERE id = ?", state, session_id)
    hub.publish(session_id, {"id": 0, "kind": "state", "session_id": session_id,
                             "state": state, "payload": extra or {}})


def since(session_id: str, after_id: int = 0, limit: int = 2000) -> list[dict]:
    rows = db.q(
        "SELECT * FROM events WHERE session_id = ? AND id > ? ORDER BY id LIMIT ?",
        session_id, after_id, limit,
    )
    return [db.row_to_dict(r) for r in rows]
