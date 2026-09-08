import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

import asyncio

from . import agent, db, events, llm, seed, tools
from .config import settings
from .routes import admin, auth, sessions
from .security import current_user

log = logging.getLogger("harness")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    seed.seed()
    events.bind_loop(asyncio.get_running_loop())
    n = agent.recover_pending()
    if n:
        log.warning("восстановлено %d незакрытых вызовов инструментов", n)
    log.info("LLM: %s model=%s", settings.llm_base_url, settings.llm_model)
    yield
    await llm.aclose()


app = FastAPI(title="Harness", version="1.0", lifespan=lifespan)

# Морда и API живут на разных портах => разные origin => CORS обязателен.
# Токен ходит заголовком Authorization (не кукой), поэтому credentials не нужны
# и '*' допустим. Если переедете на куки — заменить на явный список origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(sessions.router)
app.include_router(admin.router)


@app.get("/api/models")
async def models(_: dict = Depends(current_user)):
    """Список моделей провайдера — для выбора модели сессии в морде."""
    try:
        return {"default": settings.llm_model, "models": await llm.list_models()}
    except Exception as e:  # noqa: BLE001
        return {"default": settings.llm_model, "models": [],
                "error": "%s: %s" % (type(e).__name__, e)}


@app.get("/api/health")
async def health():
    try:
        db.q1("SELECT 1")
        db_ok = True
    except Exception as e:  # noqa: BLE001
        db_ok = False
        log.error("db health: %s", e)
    return {
        "ok": db_ok,
        "db": db_ok,
        "llm": await llm.probe(),
        "sandbox": await tools.sandbox_capacity(),
        "limits": {
            "max_iterations": settings.max_iterations,
            "max_concurrent_tools": settings.max_concurrent_tools,
            "tool_timeout": settings.tool_timeout,
        },
    }
