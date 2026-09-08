"""Песочница исполнения инструментов.

Наружу не публикуется (нет ports: в compose) и живёт в сети с internal: true,
поэтому из неё нет выхода в интернет. Это и есть настоящая граница
безопасности; строковый safety-check на стороне API — лишь первый дешёвый слой.
"""
import asyncio
import os
import shutil
import signal
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

TOKEN = os.getenv("EXECUTOR_TOKEN", "dev-executor-token")
WORKSPACE = Path(os.getenv("WORKSPACE", "/workspace")).resolve()
DEFAULT_TIMEOUT = int(os.getenv("TOOL_TIMEOUT", "60"))
MAX_OUTPUT = int(os.getenv("MAX_OUTPUT_BYTES", "32768"))

app = FastAPI(title="Harness executor")
RUNNING: dict[str, asyncio.subprocess.Process] = {}


class RunIn(BaseModel):
    run_id: str
    tool: str
    args: dict = {}
    timeout: int | None = None


class CancelIn(BaseModel):
    run_id: str


def auth(token: str | None) -> None:
    if token != TOKEN:
        raise HTTPException(status_code=401, detail="bad executor token")


def jail(raw: str) -> Path:
    """Единственная авторитетная проверка пути. resolve() снимает '..' и
    симлинки — только после него можно сравнивать с корнем."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("пустой путь")
    if "\x00" in raw:
        raise ValueError("нулевой байт в пути")
    p = Path(raw)
    target = (WORKSPACE / p).resolve() if not p.is_absolute() else p.resolve()
    if target != WORKSPACE and WORKSPACE not in target.parents:
        raise ValueError("путь вне рабочего каталога: %s" % raw)
    return target


def clip(data: str) -> tuple[str, bool]:
    b = data.encode("utf-8", "replace")
    if len(b) <= MAX_OUTPUT:
        return data, False
    head = b[: MAX_OUTPUT // 2].decode("utf-8", "replace")
    tail = b[-MAX_OUTPUT // 2:].decode("utf-8", "replace")
    return head + "\n...[обрезано %d байт]...\n" % (len(b) - MAX_OUTPUT) + tail, True


def rel(p: Path) -> str:
    try:
        return str(p.relative_to(WORKSPACE))
    except ValueError:
        return str(p)


# ------------------------------------------------------------------- bash
async def t_bash(run_id: str, args: dict, timeout: int) -> dict:
    cmd = args.get("command", "")
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp", "TMPDIR": "/tmp",
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "TERM": "dumb", "PWD": str(WORKSPACE),
    }
    proc = await asyncio.create_subprocess_exec(
        "bash", "-lc", cmd,
        cwd=str(WORKSPACE),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
        env=env,
        start_new_session=True,      # своя группа процессов -> можно убить всё дерево
    )
    RUNNING[run_id] = proc
    killed = False
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        killed = True
        _killpg(proc)
        out, _ = await proc.communicate()     # обязательно дожать, иначе зомби
    finally:
        RUNNING.pop(run_id, None)

    text, truncated = clip((out or b"").decode("utf-8", "replace"))
    if killed:
        text += "\n[убито по таймауту %d c]" % timeout
    return {"ok": (proc.returncode == 0) and not killed, "output": text,
            "error": "" if proc.returncode == 0 and not killed else
                     ("timeout" if killed else "exit code %s" % proc.returncode),
            "exit_code": proc.returncode, "truncated": truncated}


def _killpg(proc) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


# ------------------------------------------------------- файловые тулы
def t_read(args: dict) -> dict:
    path = jail(args["path"])
    if not path.is_file():
        return {"ok": False, "output": "", "error": "файл не найден: %s" % rel(path)}
    offset = max(int(args.get("offset") or 1), 1)
    limit = min(int(args.get("limit") or 400), 5000)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    chunk = lines[offset - 1: offset - 1 + limit]
    body = "\n".join("%6d\t%s" % (offset + i, ln) for i, ln in enumerate(chunk))
    text, truncated = clip(body)
    more = ""
    if offset - 1 + limit < len(lines):
        more = "\n[показаны строки %d-%d из %d]" % (offset, offset - 1 + len(chunk), len(lines))
    return {"ok": True, "output": text + more, "error": "", "truncated": truncated}


def t_edit(args: dict) -> dict:
    path = jail(args["path"])
    if not path.is_file():
        return {"ok": False, "output": "", "error": "файл не найден: %s" % rel(path)}
    old, new = args["old_string"], args["new_string"]
    src = path.read_text(encoding="utf-8", errors="replace")
    n = src.count(old)
    if n == 0:
        return {"ok": False, "output": "",
                "error": "old_string не найден в %s — прочитай файл (read) и повтори"
                         % rel(path)}
    if n > 1 and not args.get("replace_all"):
        return {"ok": False, "output": "",
                "error": "old_string встречается %d раз — сделай его уникальным "
                         "или передай replace_all=true" % n}
    dst = src.replace(old, new) if args.get("replace_all") else src.replace(old, new, 1)
    path.write_text(dst, encoding="utf-8")
    return {"ok": True, "error": "", "truncated": False,
            "output": "%s: заменено вхождений %d" % (rel(path), n if args.get("replace_all") else 1)}


def t_create(args: dict) -> dict:
    path = jail(args["path"])
    if path.exists() and not args.get("overwrite"):
        return {"ok": False, "output": "",
                "error": "файл уже существует: %s (передай overwrite=true или "
                         "правь через edit)" % rel(path)}
    path.parent.mkdir(parents=True, exist_ok=True)
    content = args["content"]
    path.write_text(content, encoding="utf-8")
    return {"ok": True, "error": "", "truncated": False,
            "output": "создан %s (%d байт)" % (rel(path), len(content.encode()))}


# ------------------------------------------------------------------ HTTP
@app.post("/run")
async def run(body: RunIn, x_executor_token: str | None = Header(default=None)):
    auth(x_executor_token)
    t0 = time.monotonic()
    timeout = min(body.timeout or DEFAULT_TIMEOUT, 600)
    try:
        if body.tool == "bash":
            res = await t_bash(body.run_id, body.args, timeout)
        elif body.tool == "read":
            res = await asyncio.to_thread(t_read, body.args)
        elif body.tool == "edit":
            res = await asyncio.to_thread(t_edit, body.args)
        elif body.tool == "create":
            res = await asyncio.to_thread(t_create, body.args)
        else:
            res = {"ok": False, "output": "", "error": "неизвестный инструмент '%s'" % body.tool}
    except (ValueError, KeyError) as e:
        res = {"ok": False, "output": "", "error": "%s: %s" % (type(e).__name__, e)}
    except OSError as e:
        res = {"ok": False, "output": "", "error": "ошибка ФС: %s" % e}
    res.setdefault("truncated", False)
    res["duration_ms"] = int((time.monotonic() - t0) * 1000)
    return res


@app.post("/cancel")
async def cancel(body: CancelIn, x_executor_token: str | None = Header(default=None)):
    auth(x_executor_token)
    proc = RUNNING.get(body.run_id)
    if proc is None:
        return {"cancelled": False}
    _killpg(proc)
    return {"cancelled": True}


@app.get("/health")
async def health():
    return {"ok": True, "workspace": str(WORKSPACE)}


@app.get("/capacity")
async def capacity(x_executor_token: str | None = Header(default=None)):
    auth(x_executor_token)
    try:
        load1, load5, load15 = os.getloadavg()
    except OSError:
        load1 = load5 = load15 = -1.0
    usage = shutil.disk_usage(str(WORKSPACE))
    return {
        "running": len(RUNNING),
        "cpu_count": os.cpu_count(),
        "load": [round(load1, 2), round(load5, 2), round(load15, 2)],
        "disk_free_mb": usage.free // (1024 * 1024),
        "workspace": str(WORKSPACE),
    }
