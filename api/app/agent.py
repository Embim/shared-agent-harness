import asyncio
import json
import time
import uuid

from . import db, events, llm, tools
from .config import settings
from .security import allowed_tools, load_active_user

# Глобальный ограничитель одновременных исполнений тулов. Проверка "хватит
# ли мощностей" на инициализации ничего не гарантирует: нагрузка возникает
# во время работы, когда N человек параллельно запустили bash.
_tool_sem = asyncio.Semaphore(settings.max_concurrent_tools)

_MISSING = ("ERROR: результат инструмента отсутствует (харнес был перезапущен "
            "во время выполнения). Вызов не завершился, считай его неуспешным.")


# ---------------------------------------------------------------------------
# Сборка контекста для провайдера из журнала событий.
#
# Инвариант формата OpenAI: за assistant-сообщением с tool_calls обязаны идти
# tool-сообщения на КАЖДЫЙ tool_call_id. Мы не собираем их из разрозненных
# строк по порядку, а порождаем из самого списка tool_calls — тогда инвариант
# держится структурно, а "осиротевшие" pending-вызовы после падения процесса
# автоматически получают синтетический результат.
# ---------------------------------------------------------------------------
def build_messages(session_id: str) -> list[dict]:
    sess = db.row_to_dict(db.q1("SELECT * FROM sessions WHERE id = ?", session_id))
    msgs: list[dict] = [{"role": "system", "content": sess["system_prompt"]}]

    rows = [db.row_to_dict(r) for r in db.q(
        "SELECT * FROM events WHERE session_id = ? ORDER BY id", session_id)]

    # Результат ищем по наличию ключа 'result', а не по kind: строка tool_call
    # закрывается обновлением НА МЕСТЕ (pending -> ok/error), kind при этом не
    # меняется. Привязка к kind давала бы модели заглушку вместо вывода тула.
    results: dict[str, str] = {}
    for r in rows:
        if r["tool_call_id"] and "result" in (r["payload"] or {}):
            results[r["tool_call_id"]] = str(r["payload"]["result"])

    for r in rows:
        if r["kind"] == "user_msg":
            who = r["payload"].get("user", "user")
            text = r["payload"].get("text", "")
            msgs.append({"role": "user", "content": "[%s] %s" % (who, text)})

        elif r["kind"] == "assistant_msg":
            tcs = r["payload"].get("tool_calls") or []
            m: dict = {"role": "assistant", "content": r["payload"].get("content") or ""}
            if tcs:
                m["tool_calls"] = tcs
            msgs.append(m)
            for tc in tcs:
                msgs.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": results.get(tc["id"], _MISSING),
                })
    return msgs


class SessionRunner:
    """Один владелец на сессию.

    Сообщения нескольких пользователей встают в общую FIFO-очередь и
    обрабатываются по одному: ход = сообщение одного автора. Так границы
    хода не нарушаются (между assistant с tool_calls и его tool-результатами
    ничего не вклинивается), а права на тулы однозначны — это права автора
    хода, без эскалации через общую сессию.
    """

    def __init__(self, session_id: str) -> None:
        self.sid = session_id
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.loop_task: asyncio.Task | None = None
        self.turn: asyncio.Task | None = None
        self.active_runs: set[str] = set()

    # ---------------------------------------------------------------- API
    def ensure_running(self) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # Явная диагностика вместо голого RuntimeError из create_task:
            # хендлер, который дёргает раннер, обязан быть async def.
            raise RuntimeError(
                "SessionRunner.ensure_running вызван вне event loop — "
                "вызывающий HTTP-хендлер должен быть 'async def'") from None
        if self.loop_task is None or self.loop_task.done():
            self.loop_task = asyncio.create_task(self._serve())

    def submit(self, user: dict, text: str) -> int:
        ev = events.append(self.sid, "user_msg", user_id=user["id"],
                           payload={"text": text, "user": user["name"],
                                    "role": user["role_name"]})
        self.inbox.put_nowait({"user_id": user["id"], "event_id": ev["id"]})
        self.ensure_running()
        return ev["id"]

    async def cancel(self, by: dict) -> bool:
        if self.turn is None or self.turn.done():
            return False
        for rid in list(self.active_runs):
            await tools.cancel_in_sandbox(rid)
        self.turn.cancel()
        events.append(self.sid, "cancelled", user_id=by["id"],
                      payload={"by": by["name"]})
        return True

    def queued(self) -> int:
        return self.inbox.qsize()

    def busy(self) -> bool:
        return self.turn is not None and not self.turn.done()

    # -------------------------------------------------------------- цикл
    async def _serve(self) -> None:
        while True:
            item = await self.inbox.get()
            self.turn = asyncio.create_task(self._run_turn(item))
            try:
                await self.turn
            except asyncio.CancelledError:
                pass                       # отменили ход, но не сам runner
            except Exception as e:         # noqa: BLE001
                events.append(self.sid, "error",
                              payload={"error": "%s: %s" % (type(e).__name__, e)})
            finally:
                self.turn = None
                self.active_runs.clear()
                events.publish_state(self.sid, "idle", {"queued": self.inbox.qsize()})

    async def _run_turn(self, item: dict) -> None:
        sess = db.row_to_dict(db.q1("SELECT * FROM sessions WHERE id = ?", self.sid))
        model = sess["model"]

        for i in range(settings.max_iterations):
            # Права резолвим заново на каждой итерации: между раундтрипами
            # к провайдеру админ мог поменять роль или заблокировать юзера.
            try:
                user = load_active_user(item["user_id"])
            except Exception as e:  # noqa: BLE001
                events.append(self.sid, "error",
                              payload={"error": "автор хода потерял доступ: %s" % e})
                return
            tool_rows = allowed_tools(user["role_id"])
            specs = tools.specs_for(tool_rows)

            events.publish_state(self.sid, "thinking",
                                 {"iteration": i + 1, "by": user["name"]})
            t0 = time.monotonic()
            data = await llm.chat(model, build_messages(self.sid), specs)
            dt = int((time.monotonic() - t0) * 1000)

            msg = data["choices"][0].get("message", {}) or {}
            usage = data.get("usage") or {}
            tool_calls = msg.get("tool_calls") or []

            events.append(
                self.sid, "assistant_msg", model=model,
                payload={"content": msg.get("content"), "tool_calls": tool_calls,
                         "finish_reason": data["choices"][0].get("finish_reason")},
                tokens_in=usage.get("prompt_tokens"),
                tokens_out=usage.get("completion_tokens"),
                duration_ms=dt,
            )

            if not tool_calls:
                return                                   # ход закончен текстом

            events.publish_state(self.sid, "tool_running", {"count": len(tool_calls)})
            for tc in tool_calls:
                await self._do_tool(tc, user, tool_rows)

        events.append(self.sid, "error",
                      payload={"error": "достигнут лимит итераций (%d)"
                                        % settings.max_iterations})

    # -------------------------------------------------------------- тулы
    async def _do_tool(self, tc: dict, user: dict, tool_rows: list[dict]) -> None:
        tcid = tc.get("id") or uuid.uuid4().hex
        fn = tc.get("function") or {}
        name = fn.get("name") or "?"

        def refuse(reason: str, status: str = "blocked") -> None:
            # Отказ ТОЖЕ обязан стать tool-сообщением: иначе следующий запрос
            # к провайдеру уедет с незакрытым tool_call_id и вернётся 400.
            events.append(self.sid, "blocked", user_id=user["id"], tool_name=name,
                          tool_call_id=tcid, status=status,
                          payload={"reason": reason, "args_raw": fn.get("arguments"),
                                   "result": "ОТКАЗАНО: " + reason})

        # 1. Аргументы. Кривой JSON не роняет цикл — модель получает ошибку
        #    как результат тула и может исправиться.
        try:
            args = json.loads(fn.get("arguments") or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments должен быть JSON-объектом")
        except (json.JSONDecodeError, ValueError) as e:
            refuse("не удалось разобрать arguments: %s" % e, status="error")
            return

        # 2. Права — ещё раз, из БД, прямо перед исполнением.
        if name not in {t["name"] for t in tool_rows}:
            refuse("у роли '%s' нет прав на инструмент '%s'" % (user["role_name"], name))
            return

        # 3. Safety-check.
        ok, reason = tools.safety_check(name, args)
        if not ok:
            refuse(reason)
            return

        row = tools.tool_row(name)
        if row and row["kind"] == "meta_tool":
            res = tools.run_meta_tool(name, args, tool_rows)
            events.append(self.sid, "tool_call", user_id=user["id"], tool_name=name,
                          tool_call_id=tcid, status="ok" if res["ok"] else "error",
                          payload={"args": args, "result": res["output"] or res["error"]})
            return

        # 4. Исполнение. Строку пишем со status='pending' ДО запуска: если
        #    процесс упадёт здесь, вызов останется видимым и не подвесит сессию.
        ev = events.append(self.sid, "tool_call", user_id=user["id"], tool_name=name,
                           tool_call_id=tcid, status="pending", payload={"args": args})
        run_id = uuid.uuid4().hex
        self.active_runs.add(run_id)
        t0 = time.monotonic()
        try:
            async with _tool_sem:
                res = await tools.run_in_sandbox(run_id, name, args)
        except asyncio.CancelledError:
            await tools.cancel_in_sandbox(run_id)
            events.update(ev["id"], status="error",
                          payload={"args": args, "result": "ОТМЕНЕНО пользователем"})
            raise
        except Exception as e:  # noqa: BLE001
            events.update(ev["id"], status="error",
                          payload={"args": args, "result": "ОШИБКА ХАРНЕСА: %s" % e})
            return
        finally:
            self.active_runs.discard(run_id)

        text = res.get("output") or ""
        if res.get("error"):
            text = (text + "\n" if text else "") + "STDERR/ERROR: " + str(res["error"])
        if res.get("truncated"):
            text += "\n[вывод обрезан харнесом]"

        events.update(
            ev["id"],
            status="ok" if res.get("ok") else "error",
            duration_ms=int((time.monotonic() - t0) * 1000),
            payload={"args": args, "result": text or "(пустой вывод)",
                     "exit_code": res.get("exit_code")},
        )


_runners: dict[str, SessionRunner] = {}


def runner(session_id: str) -> SessionRunner:
    r = _runners.get(session_id)
    if r is None:
        r = _runners[session_id] = SessionRunner(session_id)
    return r


def recover_pending() -> int:
    """Старт харнеса: закрываем вызовы, оставшиеся 'pending' после падения."""
    rows = db.q("SELECT id, payload FROM events WHERE status = 'pending'")
    for r in rows:
        try:
            payload = json.loads(r["payload"])
        except json.JSONDecodeError:
            payload = {}
        payload["result"] = _MISSING
        events.update(r["id"], status="error", payload=payload)
    return len(rows)
