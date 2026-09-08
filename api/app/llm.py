import asyncio
import time

import httpx

from .config import settings


class LLMError(RuntimeError):
    pass


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if settings.llm_api_key:
        h["Authorization"] = f"Bearer {settings.llm_api_key}"
    return h


# Один клиент на процесс: раньше каждый вызов создавал свой AsyncClient, и при
# частых опросах /api/health это давало новый TLS-хендшейк на каждый запрос —
# event loop захлёбывался, а страдали в первую очередь долгоживущие SSE-стримы
# (заголовки /stream уезжали на секунды).
_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=10.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def list_models(attempts: int = 3) -> list[str]:
    """GET /models идемпотентен, поэтому его ретраить безопасно — в отличие от
    /chat/completions, где повтор после уже выполненного тула означал бы
    двойное исполнение побочных эффектов. Разовая 502 у провайдера не должна
    ронять создание сессии."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            r = await client().get(f"{settings.llm_base_url}/models",
                                   headers=_headers(), timeout=15)
            r.raise_for_status()
            data = r.json().get("data", [])
            return [m.get("id") for m in data if m.get("id")]
        except Exception as e:  # noqa: BLE001
            last = e
            if i + 1 < attempts:
                await asyncio.sleep(0.5 * (2 ** i))
    raise last  # type: ignore[misc]


# Проба ходит по сети, а /api/health опрашивается морд(ами) регулярно. Без кеша
# health превращается во внешний запрос на каждый вызов — самый быстрый способ
# положить event loop и вместе с ним живость SSE.
_PROBE_TTL = 15.0
_probe_cache: dict[str, tuple[float, dict]] = {}
_probe_locks: dict[str, asyncio.Lock] = {}


def _cached(model: str) -> dict | None:
    hit = _probe_cache.get(model)
    if hit and (time.monotonic() - hit[0]) < _PROBE_TTL:
        return {**hit[1], "cached": True}
    return None


async def probe(model: str | None = None, fresh: bool = False) -> dict:
    """Стадия init: доступен ли провайдер и есть ли нужная модель."""
    model = model or settings.llm_model
    if not fresh:
        hit = _cached(model)
        if hit:
            return hit

    # Лок схлопывает параллельные пробы в одну: без него N одновременных
    # /api/health дают N внешних запросов, и кеш никогда не успевает помочь.
    lock = _probe_locks.setdefault(model, asyncio.Lock())
    async with lock:
        if not fresh:
            hit = _cached(model)
            if hit:
                return hit
        return await _probe_now(model)


async def _probe_now(model: str) -> dict:
    try:
        models = await list_models()
        res = {
            "ok": True,
            "base_url": settings.llm_base_url,
            "model": model,
            "model_available": (model in models) if models else None,
            "models": models[:50],
        }
    except Exception as e:  # noqa: BLE001 - наружу отдаём как диагностику
        res = {"ok": False, "base_url": settings.llm_base_url,
               "model": model, "error": f"{type(e).__name__}: {e}"}
    _probe_cache[model] = (time.monotonic(), res)
    return res


async def chat(model: str, messages: list[dict], tools: list[dict] | None) -> dict:
    """Один вызов /chat/completions.

    Намеренно НЕ стримим: при стриминге аргументы tool_calls приходят
    фрагментами и собираются по index (id есть только в первом чанке) —
    источник тонких багов. Живость UI обеспечивает журнал событий:
    фронт видит каждый tool_call/tool_result по мере появления.
    """
    body: dict = {"model": model, "messages": messages}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    r = await client().post(f"{settings.llm_base_url}/chat/completions",
                            headers=_headers(), json=body)
    if r.status_code >= 400:
        # Ретрай осознанно не делаем: повтор после уже выполненного тула
        # означал бы двойное исполнение побочных эффектов.
        raise LLMError(f"provider {r.status_code}: {r.text[:800]}")
    data = r.json()
    if not data.get("choices"):
        raise LLMError(f"provider returned no choices: {str(data)[:400]}")
    return data
