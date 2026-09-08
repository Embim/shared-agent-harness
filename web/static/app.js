'use strict';

// API живёт на соседнем порту той же машины. Хардкодить localhost нельзя:
// коллега открывает морду по http://192.168.x.x:3000 и его браузер начнёт
// стучаться в его собственный компьютер.
const API = new URLSearchParams(location.search).get('api')
         || `http://${location.hostname}:8000`;

const $ = (id) => document.getElementById(id);
const state = {
  token: localStorage.getItem('harness_token') || null,
  user: null,
  sid: null,
  lastId: 0,          // курсор журнала -> переподключение без потерь и дублей
  nodes: new Map(),   // event.id -> DOM (строка tool_call обновляется на месте)
  abort: null,
  backoff: 500,
  timers: [],         // чтобы logout/login не плодил дубли setInterval
  models: [],
};

function every(ms, fn) {
  state.timers.push(setInterval(fn, ms));
}
function clearTimers() {
  state.timers.forEach(clearInterval);
  state.timers = [];
}

// --------------------------------------------------------------- http
async function api(path, opts = {}) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
  if (state.token) headers['Authorization'] = 'Bearer ' + state.token;
  const res = await fetch(API + path, Object.assign({}, opts, { headers }));
  if (res.status === 401) { logout(); throw new Error('сессия истекла'); }
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const d = data && data.detail;
    throw new Error(typeof d === 'string' ? d : JSON.stringify(d || res.statusText));
  }
  return data;
}

// -------------------------------------------------------------- логин
$('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const err = $('login-error');
  err.hidden = true;
  try {
    const r = await api('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ name: $('login-name').value, password: $('login-pass').value }),
    });
    state.token = r.token;
    localStorage.setItem('harness_token', r.token);
    state.user = r.user;
    await boot();
  } catch (ex) {
    err.textContent = ex.message;
    err.hidden = false;
  }
});

function logout() {
  if (state.abort) state.abort.abort();
  clearTimers();
  localStorage.removeItem('harness_token');
  Object.assign(state, { token: null, user: null, sid: null, lastId: 0, abort: null });
  $('app').hidden = true;
  $('login').hidden = false;
}
$('btn-logout').addEventListener('click', logout);

// --------------------------------------------------------------- boot
async function boot() {
  clearTimers();                       // повторный boot не должен плодить таймеры
  state.user = await api('/api/auth/me');
  $('login').hidden = true;
  $('app').hidden = false;
  $('whoami').innerHTML = `<b>${esc(state.user.name)}</b> · ${esc(state.user.role)}`;
  $('tool-list').innerHTML = state.user.tools.map((t) => `<li>${esc(t)}</li>`).join('')
    || '<li class="muted">нет доступных</li>';
  $('admin-block').hidden = state.user.role !== 'admin';
  try { state.models = (await api('/api/models')).models || []; } catch { state.models = []; }
  await refreshSessions();
  refreshHealth();
  every(30000, refreshHealth);
  every(15000, refreshSessions);
  // Страховка на случай, если SSE не доехал (см. комментарий в attach): даже
  // при полностью мёртвом стриме лента отстаёт максимум на 2 секунды.
  every(2000, pull);
  if (state.user.role === 'admin') { refreshUsers(); every(30000, refreshUsers); }
}

function fillModels(current) {
  const sel = $('model-select');
  const opts = state.models.includes(current) ? state.models : [current, ...state.models];
  sel.innerHTML = opts.map((m) =>
    `<option value="${esc(m)}"${m === current ? ' selected' : ''}>${esc(m)}</option>`).join('');
  sel.title = state.models.includes(current)
    ? 'модель этой сессии' : `модель '${current}' недоступна у провайдера — выберите другую`;
  sel.style.borderColor = state.models.includes(current) ? '' : 'var(--err)';
}

$('model-select').addEventListener('change', async (e) => {
  try {
    await api('/api/sessions/' + state.sid,
      { method: 'PATCH', body: JSON.stringify({ model: e.target.value }) });
    await attach(state.sid, true);
  } catch (ex) { alert('Не удалось сменить модель:\n' + ex.message); }
});

// ------------------------------------------------------------- сессии
async function refreshSessions() {
  let list;
  try { list = await api('/api/sessions'); } catch { return; }
  $('session-list').innerHTML = list.map((s) => `
    <li data-id="${s.id}" class="${s.id === state.sid ? 'active' : ''}">
      <div>${esc(s.title)}</div>
      <div class="sub">${s.state} · ${s.viewers}👁 ${s.queued ? '· ' + s.queued + ' в очереди' : ''}</div>
    </li>`).join('') || '<li class="muted">пока нет</li>';
  for (const li of $('session-list').querySelectorAll('li[data-id]')) {
    li.addEventListener('click', () => attach(li.dataset.id));
  }
  if (!state.sid && list.length) attach(list[0].id);
}

$('btn-new').addEventListener('click', async () => {
  const title = prompt('Название сессии', 'session ' + new Date().toLocaleTimeString('ru-RU'));
  if (title === null) return;
  try {
    const s = await api('/api/sessions', { method: 'POST', body: JSON.stringify({ title }) });
    await refreshSessions();
    attach(s.id);
  } catch (ex) { alert('Не удалось создать сессию:\n' + ex.message); }
});

async function attach(sid, force) {
  // Клик по уже открытой сессии не должен рвать живой поток: пересоздание
  // соединения стоит паузы, пока сервер не дожуёт предыдущий ответ.
  if (!force && sid === state.sid && state.abort && !state.abort.signal.aborted) return;
  if (state.abort) state.abort.abort();
  state.sid = sid; state.lastId = 0; state.nodes.clear();
  $('feed').innerHTML = '';
  const s = await api('/api/sessions/' + sid);
  $('session-title').textContent = s.title;
  fillModels(s.model);
  setState(s.state);

  // Историю берём обычным запросом, а не из SSE. Проброс портов Docker Desktop
  // на Windows задерживает начало chunked-ответа примерно на 16 с (проверено:
  // изнутри контейнера тот же стрим отдаётся за 1 мс), и вторая вкладка видела
  // пустой диалог. Обычные запросы через тот же проброс идут мгновенно.
  await pull();

  await refreshSessions();
  connect();
}

// Догрузка журнала по курсору. Append-only лог + монотонный id делают это
// идемпотентным: без дублей и без пропусков, сколько ни вызывай.
let pulling = false;
async function pull() {
  if (pulling || !state.sid) return;
  pulling = true;
  const sid = state.sid;
  try {
    const evs = await api(`/api/sessions/${sid}/events?after=${state.lastId}`);
    if (state.sid !== sid) return;            // успели переключить сессию
    for (const ev of evs) handle(ev);
  } catch { /* сеть моргнула — повторим на следующем тике */ }
  finally { pulling = false; }
}

// ----------------------------------------------------------------- SSE
// EventSource не умеет кастомные заголовки, а тащить JWT в query string
// значит светить его в логах. Поэтому читаем поток вручную через fetch.
async function connect() {
  const ctl = new AbortController();
  state.abort = ctl;
  const sid = state.sid;
  try {
    const res = await fetch(`${API}/api/sessions/${sid}/stream?after=${state.lastId}`, {
      headers: { Authorization: 'Bearer ' + state.token },
      signal: ctl.signal,
    });
    if (!res.ok) throw new Error('stream ' + res.status);
    setConn(true);
    state.backoff = 500;

    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, i); buf = buf.slice(i + 2);
        if (!frame.startsWith('data:')) continue;      // ": ping"
        try { handle(JSON.parse(frame.slice(5).trim())); } catch { /* ignore */ }
      }
    }
  } catch (ex) {
    if (ctl.signal.aborted) return;
  }
  setConn(false);
  if (state.sid !== sid) return;
  state.backoff = Math.min(state.backoff * 2, 10000);
  setTimeout(() => { if (state.sid === sid) connect(); }, state.backoff);
}

function handle(ev) {
  if (ev.kind === 'state') {
    setState(ev.state);
    const q = (ev.payload && ev.payload.queued) || 0;
    $('queue-note').textContent = q ? `в очереди: ${q}` : '';
    return;
  }
  if (ev.id > state.lastId) state.lastId = ev.id;
  render(ev);
}

// -------------------------------------------------------------- рендер
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
const time = (ts) => (ts || '').slice(11, 19);

function render(ev) {
  const feed = $('feed');
  const stick = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 120;
  const existing = state.nodes.get(ev.id);
  const node = build(ev);
  if (!node) return;
  if (existing) existing.replaceWith(node); else feed.appendChild(node);
  state.nodes.set(ev.id, node);
  if (stick) feed.scrollTop = feed.scrollHeight;
}

function build(ev) {
  const p = ev.payload || {};
  const el = document.createElement('div');
  el.className = 'ev';

  switch (ev.kind) {
    case 'session_init':
      el.classList.add('system');
      el.innerHTML = body('', `сессия создана — ${esc(p.by)} · модель ${esc(ev.model)}`);
      break;

    case 'user_msg':
      el.classList.add('user');
      el.innerHTML = `<div class="ev-head"><span class="who">${esc(p.user)}</span>
        <span>${esc(p.role || '')}</span><span>${time(ev.ts)}</span></div>
        <div class="ev-body">${esc(p.text)}</div>`;
      break;

    case 'assistant_msg': {
      const hasText = p.content && String(p.content).trim();
      const n = (p.tool_calls || []).length;
      if (!hasText && n) return null;          // «намерение» покажут карточки тулов
      el.classList.add('assistant');
      const meta = [ev.model, ev.duration_ms ? ev.duration_ms + 'ms' : null,
        ev.tokens_in != null ? `${ev.tokens_in}→${ev.tokens_out}tok` : null]
        .filter(Boolean).map(esc).join(' · ');
      // .trim(): модели с reasoning отдают content с ведущими переносами,
      // а pre-wrap честно рисует их как пустоту в начале сообщения.
      el.innerHTML = `<div class="ev-head"><span>агент</span><span>${meta}</span>
        <span>${time(ev.ts)}</span></div>
        <div class="ev-body">${esc(String(p.content).trim())}</div>`;
      break;
    }

    case 'tool_call':
    case 'tool_result':
    case 'blocked': {
      el.classList.add('tool');
      if (ev.status === 'ok') el.classList.add('collapsed');
      const st = ev.status || (ev.kind === 'blocked' ? 'blocked' : 'ok');
      const args = p.args ? JSON.stringify(p.args, null, 2) : (p.args_raw || '');
      const brief = p.args ? Object.values(p.args)[0] : (p.reason || '');
      const dur = ev.duration_ms ? ` · ${ev.duration_ms}ms` : '';
      el.innerHTML = `
        <div class="ev-body">
          <div class="tool-head">
            <span class="name">${esc(ev.tool_name)}</span>
            <span class="arg">${esc(String(brief).slice(0, 120))}</span>
            <span class="st ${esc(st)}">${esc(st)}${esc(dur)}</span>
          </div>
          <div class="tool-body">${args ? `<div class="args">${esc(args)}</div>` : ''}${esc(
            p.result != null ? p.result : (st === 'pending' ? 'выполняется…' : ''))}</div>
        </div>`;
      el.querySelector('.tool-head').addEventListener('click',
        () => el.classList.toggle('collapsed'));
      break;
    }

    case 'cancelled':
      el.classList.add('system');
      el.innerHTML = body('', `ход отменён (${esc(p.by)})`);
      break;

    case 'error':
      el.classList.add('error');
      el.innerHTML = body(time(ev.ts), esc(p.error));
      break;

    default:
      return null;
  }
  return el;
}

const body = (head, html) =>
  `<div class="ev-head"><span>${head}</span></div><div class="ev-body">${html}</div>`;

function setState(s) {
  const pill = $('state-pill');
  pill.className = 'pill ' + s;
  pill.textContent = s;
  $('btn-cancel').disabled = !(s === 'thinking' || s === 'tool_running');
}
function setConn(on) {
  const p = $('conn-pill');
  p.className = 'pill ' + (on ? 'conn-on' : 'conn-off');
  p.textContent = on ? 'live' : 'offline';
}

// ------------------------------------------------------------ отправка
async function send() {
  const ta = $('input');
  const text = ta.value.trim();
  if (!text || !state.sid) return;
  ta.value = '';
  try {
    const r = await api(`/api/sessions/${state.sid}/messages`,
      { method: 'POST', body: JSON.stringify({ text }) });
    if (r.busy) $('queue-note').textContent = `в очереди: ${r.queued} (агент занят)`;
  } catch (ex) { alert(ex.message); ta.value = text; }
}
$('btn-send').addEventListener('click', send);
$('input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
$('btn-cancel').addEventListener('click', async () => {
  try { await api(`/api/sessions/${state.sid}/cancel`, { method: 'POST' }); }
  catch (ex) { alert(ex.message); }
});

// -------------------------------------------------------------- админка
async function refreshUsers() {
  let list;
  try { list = await api('/api/admin/users'); } catch { return; }
  $('user-list').innerHTML = list.map((u) => `
    <li>
      <span class="dot ${u.status === 'blocked' ? 'blocked' : ''}"></span>
      <span class="u-name">${esc(u.name)}</span>
      <span class="u-role">${esc(u.role)}</span>
      <button class="mini" data-uid="${u.id}" data-to="${u.status === 'active' ? 'blocked' : 'active'}">
        ${u.status === 'active' ? 'блок' : 'вкл'}</button>
    </li>`).join('');
  for (const b of $('user-list').querySelectorAll('button[data-uid]')) {
    b.addEventListener('click', async () => {
      try {
        await api('/api/admin/users/' + b.dataset.uid,
          { method: 'PATCH', body: JSON.stringify({ status: b.dataset.to }) });
        refreshUsers();
      } catch (ex) { alert(ex.message); }
    });
  }
}

$('btn-adduser').addEventListener('click', async () => {
  const name = prompt('Имя пользователя'); if (!name) return;
  const password = prompt('Пароль (минимум 4 символа)'); if (!password) return;
  const role = prompt('Роль: admin | developer | read', 'developer'); if (!role) return;
  try {
    await api('/api/admin/users',
      { method: 'POST', body: JSON.stringify({ name, password, role }) });
    refreshUsers();
  } catch (ex) { alert(ex.message); }
});

// -------------------------------------------------------------- здоровье
async function refreshHealth() {
  try {
    const h = await api('/api/health');
    const mark = (ok) => `<span class="${ok ? 'ok' : 'bad'}">${ok ? '✓' : '✗'}</span>`;
    $('health').innerHTML = [
      `${mark(h.db)} sqlite`,
      `${mark(h.llm.ok)} llm ${esc(h.llm.model)}`,
      `${mark(h.sandbox.ok)} sandbox${h.sandbox.ok ? ` · running ${h.sandbox.running}` : ''}`,
      h.sandbox.ok ? `<span class="muted">load ${h.sandbox.load[0]} · free ${h.sandbox.disk_free_mb}MB</span>` : '',
    ].filter(Boolean).join('<br>');
  } catch {
    $('health').innerHTML = '<span class="bad">✗ API недоступен</span>';
  }
}

// ---------------------------------------------------------------- старт
if (state.token) {
  boot().catch(() => logout());
} else {
  $('login').hidden = false;
}
