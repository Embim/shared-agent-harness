"""Отдача статики. Отдельный порт, никакого nginx."""
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Harness UI")


@app.middleware("http")
async def no_cache(request: Request, call_next):
    # index.html/app.js закешированные браузером — источник "я поправил, а
    # ничего не изменилось". Для локального харнеса кеш не нужен вовсе.
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    return resp


app.mount("/", StaticFiles(directory="static", html=True), name="static")
