import json

from . import db
from .config import settings
from .security import hash_password

# Определения тулов лежат в БД как ДАННЫЕ (описание + JSON Schema), но их
# исполнение — это код в executor. Добавить строку в tools через морду и
# получить новый исполняемый тул нельзя: это был бы RCE поверх собственной
# аутентификации.
TOOLS = [
    {
        "name": "bash",
        "kind": "tool",
        "description": "Выполнить shell-команду в песочнице (cwd=/workspace, без сети). "
                       "Разрешён ограниченный набор команд.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "команда для bash -lc"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read",
        "kind": "tool",
        "description": "Прочитать файл (аналог cat). Путь относительно /workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "например src/main.py"},
                "offset": {"type": "integer", "description": "с какой строки, с 1"},
                "limit": {"type": "integer", "description": "сколько строк, по умолчанию 400"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "edit",
        "kind": "tool",
        "description": "Заменить точное вхождение old_string на new_string в файле. "
                       "Вхождение должно быть уникальным, иначе будет ошибка.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string", "description": "точный текст для замены"},
                "new_string": {"type": "string", "description": "на что заменить"},
                "replace_all": {"type": "boolean", "description": "заменить все вхождения"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "create",
        "kind": "tool",
        "description": "Создать новый файл с содержимым. По умолчанию не перезаписывает.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "overwrite": {"type": "boolean"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_tools",
        "kind": "meta_tool",
        "description": "Показать, какие инструменты доступны текущему пользователю.",
        "parameters": {"type": "object", "properties": {}},
    },
]

# Роль -> набор тулов. Ровно ваша матрица прав, но в виде связи, а не колонок.
ROLES = {
    "admin":     ["bash", "read", "edit", "create", "list_tools"],
    "developer": ["read", "edit", "create", "list_tools"],
    "read":      ["read", "list_tools"],
}


def seed() -> None:
    for t in TOOLS:
        db.ex(
            "INSERT INTO tools (name, kind, description, parameters_json) VALUES (?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, "
            "description=excluded.description, parameters_json=excluded.parameters_json, "
            "version=tools.version+1",
            t["name"], t["kind"], t["description"],
            json.dumps(t["parameters"], ensure_ascii=False),
        )

    for role_name, tool_names in ROLES.items():
        db.ex("INSERT INTO roles (name) VALUES (?) ON CONFLICT(name) DO NOTHING", role_name)
        rid = db.q1("SELECT id FROM roles WHERE name = ?", role_name)["id"]
        db.ex("DELETE FROM role_tool WHERE role_id = ?", rid)
        for tn in tool_names:
            tid = db.q1("SELECT id FROM tools WHERE name = ?", tn)["id"]
            db.ex("INSERT INTO role_tool (role_id, tool_id) VALUES (?,?)", rid, tid)

    if db.q1("SELECT id FROM users WHERE name = 'admin'") is None:
        admin_role = db.q1("SELECT id FROM roles WHERE name = 'admin'")["id"]
        db.ex(
            "INSERT INTO users (name, password_hash, role_id, status) VALUES (?,?,?,'active')",
            "admin", hash_password(settings.admin_password), admin_role,
        )
