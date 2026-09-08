-- Роль = именованный набор прав. Права выдаются на ТУЛЫ, а не на колонки:
-- новый тул добавляется строкой в tools + строкой в role_tool, без ALTER TABLE.
CREATE TABLE IF NOT EXISTS roles (
  id   INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS tools (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  name            TEXT NOT NULL UNIQUE,
  kind            TEXT NOT NULL DEFAULT 'tool',   -- tool | meta_tool | skill
  description     TEXT NOT NULL,
  parameters_json TEXT NOT NULL,                  -- JSON Schema параметров
  enabled         INTEGER NOT NULL DEFAULT 1,
  version         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS role_tool (
  role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
  tool_id INTEGER NOT NULL REFERENCES tools(id) ON DELETE CASCADE,
  PRIMARY KEY (role_id, tool_id)
);

CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role_id       INTEGER NOT NULL REFERENCES roles(id),
  status        TEXT NOT NULL DEFAULT 'active',   -- active | blocked
  start_date    TEXT,                             -- YYYY-MM-DD, окно доступа
  end_date      TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
  id            TEXT PRIMARY KEY,                 -- uuid4
  title         TEXT NOT NULL DEFAULT 'session',
  owner_id      INTEGER NOT NULL REFERENCES users(id),
  model         TEXT NOT NULL,
  system_prompt TEXT NOT NULL DEFAULT '',         -- живёт здесь, а не в каждой строке лога
  state         TEXT NOT NULL DEFAULT 'idle',     -- idle | thinking | tool_running | closed
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Append-only журнал. Он же:
--   * источник контекста для LLM (проекция по session_id ORDER BY id)
--   * аудит-лог
--   * лента событий для фронта (WHERE id > last_seen_id -> resume без дублей)
CREATE TABLE IF NOT EXISTS events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT, -- он же монотонный seq
  session_id   TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  user_id      INTEGER REFERENCES users(id),      -- NULL для сообщений модели
  ts           TEXT NOT NULL DEFAULT (datetime('now')),
  kind         TEXT NOT NULL,                     -- session_init | user_msg | assistant_msg
                                                  -- | tool_call | blocked | cancelled | error
                                                  -- строка tool_call живёт весь вызов:
                                                  -- pending -> ok/error, результат в payload
  model        TEXT,
  tool_name    TEXT,
  tool_call_id TEXT,                              -- ключ склейки вызов <-> результат
  status       TEXT,                              -- pending | ok | error | blocked
  payload      TEXT NOT NULL DEFAULT '{}',        -- JSON
  tokens_in    INTEGER,
  tokens_out   INTEGER,
  duration_ms  INTEGER
);

CREATE INDEX IF NOT EXISTS ix_events_session ON events(session_id, id);
CREATE INDEX IF NOT EXISTS ix_events_pending ON events(status) WHERE status = 'pending';
