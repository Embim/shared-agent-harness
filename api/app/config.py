import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


class Settings:
    db_path = os.getenv("HARNESS_DB", "./harness.db")

    jwt_secret = os.getenv("JWT_SECRET", "dev-secret-change-me")
    jwt_alg = "HS256"
    jwt_ttl_min = _int("JWT_TTL_MIN", 720)
    admin_password = os.getenv("ADMIN_PASSWORD", "admin")

    executor_url = os.getenv("EXECUTOR_URL", "http://executor:9000").rstrip("/")
    executor_token = os.getenv("EXECUTOR_TOKEN", "dev-executor-token")

    llm_base_url = os.getenv("LLM_BASE_URL", "http://host.docker.internal:11434/v1").rstrip("/")
    llm_api_key = os.getenv("LLM_API_KEY", "")
    llm_model = os.getenv("LLM_MODEL", "qwen2.5-coder:7b")

    cors_origins = [s.strip() for s in os.getenv("CORS_ORIGINS", "*").split(",") if s.strip()]

    max_iterations = _int("MAX_ITERATIONS", 12)
    max_concurrent_tools = _int("MAX_CONCURRENT_TOOLS", 4)
    tool_timeout = _int("TOOL_TIMEOUT", 60)

    system_prompt = os.getenv(
        "SYSTEM_PROMPT",
        "Ты инженерный агент внутри локального харнеса. У тебя есть инструменты для работы "
        "с файлами в каталоге /workspace и для запуска команд.\n"
        "Правила:\n"
        "- Пути в инструментах указывай ОТНОСИТЕЛЬНО /workspace (например: src/main.py).\n"
        "- Перед правкой файла сначала прочитай его инструментом read.\n"
        "- Инструмент edit заменяет точное вхождение old_string; оно должно быть уникальным.\n"
        "- Если инструмент вернул ошибку или отказ safety-check, не повторяй тот же вызов "
        "дважды — измени подход или объясни пользователю проблему текстом.\n"
        "- В сессии могут участвовать несколько пользователей; их сообщения приходят "
        "с префиксом [имя]. Отвечай тому, кто спросил последним.\n"
        "- Отвечай по-русски, кратко и по делу.",
    )


settings = Settings()
