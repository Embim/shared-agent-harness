import json
import re
import shlex

import httpx

from . import db
from .config import settings

# ---------------------------------------------------------------------------
# Список тулов, уходящий провайдеру, строится из таблицы tools и режется
# правами роли. Пользователю с ролью read инструмент bash не приходит вовсе —
# модель не может вызвать то, чего не видит. Это надёжнее, чем запрещать
# постфактум, и заменяет отдельный "аналитический" вызов "нужен ли тул".
# ---------------------------------------------------------------------------


def specs_for(tool_rows: list[dict]) -> list[dict]:
    out = []
    for t in tool_rows:
        try:
            params = json.loads(t["parameters_json"])
        except json.JSONDecodeError:
            params = {"type": "object", "properties": {}}
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": params,
            },
        })
    return out


# ------------------------------------------------------------ safety-check
# Дешёвый первый слой. НЕ граница безопасности: настоящая граница —
# контейнер executor (non-root, internal-сеть, mem/pids limits, таймаут,
# path jail). Блэклист символов ловит опечатки и явную дурь, но обходится
# тривиально, поэтому основа здесь — allowlist первой команды.

BASH_ALLOWLIST = {
    "ls", "cat", "head", "tail", "grep", "rg", "find", "wc", "echo", "pwd",
    "sed", "awk", "sort", "uniq", "cut", "tr", "diff", "stat", "file", "du",
    "df", "tree", "which", "env", "date", "mkdir", "touch", "cp", "mv", "rm",
    "python", "python3", "pip", "pytest", "git", "node", "npm", "make", "bash", "sh",
}

DANGEROUS = [
    (re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rf][a-zA-Z]*\s+/(\s|$)"), "rm -rf по корню"),
    (re.compile(r":\s*\(\s*\)\s*\{.*\|.*&\s*\}\s*;"), "fork bomb"),
    (re.compile(r"\bmkfs(\.|\s)"), "форматирование ФС"),
    (re.compile(r"\bdd\s+.*\bof=/dev/"), "запись в блочное устройство"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"), "управление питанием"),
    (re.compile(r"\b(sudo|su)\b"), "повышение привилегий"),
    (re.compile(r">\s*/dev/(sd|nvme|hd)"), "запись в диск"),
    (re.compile(r"\b(curl|wget)\b"), "сетевой доступ (у песочницы его нет)"),
    (re.compile(r"/etc/(passwd|shadow|sudoers)"), "системные файлы"),
]

_BAD_PATH = re.compile(r"(^|/)\.\.(/|$)")


def _check_path(raw) -> tuple[bool, str]:
    if not isinstance(raw, str) or not raw.strip():
        return False, "параметр path обязателен"
    if raw.startswith("/") and not raw.startswith("/workspace"):
        return False, f"абсолютный путь вне /workspace: {raw}"
    if _BAD_PATH.search(raw):
        return False, f"выход из рабочего каталога через '..': {raw}"
    if "\x00" in raw:
        return False, "нулевой байт в пути"
    return True, ""


def safety_check(tool: str, args: dict) -> tuple[bool, str]:
    if tool == "bash":
        cmd = args.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            return False, "параметр command обязателен"
        for rx, why in DANGEROUS:
            if rx.search(cmd):
                return False, f"запрещённая конструкция: {why}"
        try:
            tokens = shlex.split(cmd)
        except ValueError as e:
            return False, f"неразбираемая команда: {e}"
        # Проверяем первое слово каждого сегмента конвейера/цепочки.
        heads, expect = [], True
        for tok in tokens:
            if tok in ("|", "||", "&&", ";", "&"):
                expect = True
                continue
            if expect:
                heads.append(tok.split("/")[-1])
                expect = False
        for h in heads:
            if "=" in h:            # VAR=value перед командой
                continue
            if h not in BASH_ALLOWLIST:
                return False, f"команда '{h}' не входит в allowlist"
        return True, ""

    if tool in ("read", "edit", "create"):
        ok, why = _check_path(args.get("path"))
        if not ok:
            return False, why
        if tool == "edit":
            if not isinstance(args.get("old_string"), str) or args["old_string"] == "":
                return False, "old_string обязателен и не может быть пустым"
            if not isinstance(args.get("new_string"), str):
                return False, "new_string обязателен"
        if tool == "create" and not isinstance(args.get("content"), str):
            return False, "content обязателен"
        return True, ""

    if tool == "list_tools":
        return True, ""

    return False, f"неизвестный инструмент '{tool}'"


# --------------------------------------------------------------- исполнение
async def run_in_sandbox(run_id: str, tool: str, args: dict) -> dict:
    async with httpx.AsyncClient(timeout=settings.tool_timeout + 15) as c:
        r = await c.post(
            f"{settings.executor_url}/run",
            headers={"X-Executor-Token": settings.executor_token},
            json={"run_id": run_id, "tool": tool, "args": args,
                  "timeout": settings.tool_timeout},
        )
    if r.status_code >= 400:
        return {"ok": False, "output": "", "error": f"executor {r.status_code}: {r.text[:500]}"}
    return r.json()


async def cancel_in_sandbox(run_id: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(
                f"{settings.executor_url}/cancel",
                headers={"X-Executor-Token": settings.executor_token},
                json={"run_id": run_id},
            )
    except Exception:  # noqa: BLE001 - отмена best-effort
        pass


async def sandbox_capacity() -> dict:
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{settings.executor_url}/capacity",
                            headers={"X-Executor-Token": settings.executor_token})
            r.raise_for_status()
            return {"ok": True, **r.json()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def run_meta_tool(tool: str, args: dict, tool_rows: list[dict]) -> dict:
    """meta_tool исполняется самим харнесом, в песочницу не ходит."""
    if tool == "list_tools":
        listing = "\n".join(
            f"- {t['name']} ({t['kind']}): {t['description']}" for t in tool_rows
        ) or "(нет доступных инструментов)"
        return {"ok": True, "output": f"Доступные тебе инструменты:\n{listing}", "error": ""}
    return {"ok": False, "output": "", "error": f"неизвестный meta_tool '{tool}'"}


def tool_row(name: str) -> dict | None:
    return db.row_to_dict(db.q1("SELECT * FROM tools WHERE name = ?", name))
