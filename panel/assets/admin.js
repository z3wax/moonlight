'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
let CSRF = '';
let SCHEMA = [];
let VALUES = {};
const dirty = {};

// ---------- helpers ----------
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) if (c != null) node.append(c instanceof Node ? c : document.createTextNode(c));
  return node;
}

function toast(msg, bad = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast show' + (bad ? ' bad' : '');
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.className = 'toast' + (bad ? ' bad' : ''); }, 2600);
}

async function api(path, data, opts = {}) {
  const init = { credentials: 'same-origin', headers: {} };
  if (data !== undefined) {
    init.method = 'POST';
    init.headers['X-CSRF-Token'] = CSRF;
    if (data instanceof FormData) init.body = data;
    else { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(data); }
  }
  const res = await fetch(path, init);
  if (res.status === 401) { location.href = '/'; throw new Error('auth'); }
  const json = await res.json().catch(() => ({ ok: false, error: 'Server javobi noto‘g‘ri' }));
  if (!res.ok || json.ok === false) {
    if (!opts.quiet) toast(json.error || 'Xatolik', true);
    throw new Error(json.error || 'error');
  }
  return json;
}

function ago(ts) {
  if (!ts) return '—';
  return duration(Date.now() / 1000 - ts) + ' oldin';
}
function duration(sec) {
  sec = Math.max(0, Math.floor(sec));
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
  if (d) return `${d} kun ${h} soat`;
  if (h) return `${h} soat ${m} daq`;
  if (m) return `${m} daqiqa`;
  return `${sec} soniya`;
}
function date(ts) { return ts ? new Date(ts * 1000).toLocaleString() : '—'; }
function kv(target, rows) {
  target.replaceChildren(...rows.flatMap(([k, v]) => [el('dt', { text: k }), el('dd', {}, v ?? '—')]));
}
function pill(text, kind) { return el('span', { class: `pill ${kind}`, text }); }

function switchInput(checked, onchange) {
  const input = el('input', { type: 'checkbox' });
  input.checked = !!checked;
  input.addEventListener('change', () => onchange(input.checked));
  return el('label', { class: 'switch' }, input, el('span', { class: 'track' }));
}

// ---------- navigation ----------
function show(section) {
  $$('.section').forEach((s) => s.classList.toggle('active', s.id === `sec-${section}`));
  $$('#nav button').forEach((b) => b.classList.toggle('active', b.dataset.section === section));
  $('#mobile-nav').value = section;
  $('#title').textContent = $(`#nav button[data-section="${section}"]`).lastChild.textContent.trim();
  try { localStorage.setItem('section', section); } catch (e) { /* ignore */ }
  if (section === 'logs') loadLogs();
  if (section === 'telegram') refreshAvatar();
}
$$('#nav button').forEach((b) => b.addEventListener('click', () => show(b.dataset.section)));
$('#mobile-nav').addEventListener('change', (e) => show(e.target.value));

// ---------- settings rendering ----------
function markDirty(key, value) {
  if (JSON.stringify(value) === JSON.stringify(VALUES[key])) delete dirty[key];
  else dirty[key] = value;
  updateSavebar();
}

function updateSavebar() {
  const n = Object.keys(dirty).length;
  $('#savebar-count').textContent = n;
  $('#savebar').classList.toggle('show', n > 0);
}

function renderField(f) {
  const value = VALUES[f.key];
  const text = el('div', { class: 'text' },
    el('b', {}, f.label, f.restart ? el('span', { class: 'badge-restart', text: '↻ qayta ulanadi' }) : null),
    f.help ? el('small', { text: f.help }) : null);

  if (f.type === 'bool') {
    // Toggles apply instantly, like iOS.
    return el('div', { class: 'row' }, text, el('div', { class: 'control' }, switchInput(value, async (on) => {
      try {
        const r = await api('/api/settings', { values: { [f.key]: on } });
        VALUES = r.values;
        toast(`${f.label}: ${on ? 'yoqildi' : 'o‘chirildi'}`);
        if (f.key === 'ai_enabled') $('#ai-master').checked = on;
      } catch (e) { /* toast shown */ }
    })));
  }
  if (f.type === 'secret_list') return null; // managed in the keys card
  if (f.type === 'text' || f.type === 'list') {
    const area = el('textarea', { rows: f.type === 'list' ? 4 : 6 });
    area.value = f.type === 'list' ? (value || []).join('\n') : (value || '');
    area.addEventListener('input', () => markDirty(f.key, f.type === 'list'
      ? area.value.split('\n').map((s) => s.trim()).filter(Boolean) : area.value));
    return el('div', { class: 'row stack' }, text, area);
  }
  let input;
  if (f.type === 'secret') {
    input = el('input', { type: 'password', autocomplete: 'off', placeholder: value && value.set ? value.masked + ' (o‘zgartirish uchun yozing)' : 'kiritilmagan' });
    input.addEventListener('input', () => { if (input.value) dirty[f.key] = input.value; else delete dirty[f.key]; updateSavebar(); });
  } else {
    const type = f.type === 'int' || f.type === 'float' ? 'number' : f.type === 'time' ? 'time' : 'text';
    input = el('input', { type, step: f.type === 'float' ? 'any' : null, min: f.min, max: f.max });
    input.value = value ?? '';
    input.addEventListener('input', () => {
      let v = input.value;
      if (f.type === 'int') v = parseInt(v, 10);
      if (f.type === 'float') v = parseFloat(v);
      markDirty(f.key, v);
    });
  }
  return el('div', { class: 'row' }, text, el('div', { class: 'control' }, input));
}

function renderSettings() {
  $$('[data-group]').forEach((container) => {
    const group = container.dataset.group;
    const fields = SCHEMA.filter((f) => f.group === group);
    const toggles = fields.filter((f) => f.type === 'bool').map(renderField);
    const others = fields.filter((f) => f.type !== 'bool').map(renderField).filter(Boolean);
    const parts = [];
    if (toggles.length) parts.push(el('div', { class: 'list-title', text: 'Funksiyalar' }), el('div', { class: 'list' }, toggles));
    if (others.length) parts.push(el('div', { class: 'list-title', text: 'Sozlamalar' }), el('div', { class: 'list' }, others));
    container.replaceChildren(...parts);
  });
}

async function loadSettings() {
  const r = await api('/api/settings');
  SCHEMA = r.schema;
  VALUES = r.values;
  for (const k of Object.keys(dirty)) delete dirty[k];
  updateSavebar();
  renderSettings();
}

$('#save').addEventListener('click', async () => {
  const values = { ...dirty };
  try {
    const r = await api('/api/settings', { values });
    VALUES = r.values;
    for (const k of Object.keys(dirty)) delete dirty[k];
    updateSavebar();
    renderSettings();
    toast(r.restart && r.restart.length ? 'Saqlandi — Telegram qayta ulanmoqda' : 'Saqlandi ✓');
    refreshStatus();
  } catch (e) { /* toast shown */ }
});
$('#discard').addEventListener('click', () => { loadSettings(); });

// ---------- status ----------
let STATUS = null;
const STATE_KIND = { online: 'ok', connecting: 'warn', offline: 'warn', code_sent: 'warn', password_needed: 'warn' };

async function refreshStatus() {
  let r;
  try { r = await api('/api/status', undefined, { quiet: true }); } catch (e) { return; }
  STATUS = r;
  const tg = r.telegram, ai = r.ai, srv = r.server, act = r.activity;
  const kind = STATE_KIND[tg.state] || 'bad';

  $('#ai-master').checked = ai.enabled;
  $('#ai-hero-sub').textContent = ai.enabled
    ? `Yoqilgan · ${ai.last_model || ai.model}`
    : 'O‘chirilgan — bot hech kimga javob bermaydi';

  $('#st-tg').replaceChildren(pill(tg.state_text, kind));
  $('#st-tg-sub').textContent = tg.state === 'online' ? `ulangan: ${ago(tg.connected_at)}` : (tg.last_error || '');
  $('#st-replies').textContent = act.replies_session;
  $('#st-events').textContent = `24 soatda ${act.events_24h} ta suhbat`;
  const okKeys = ai.keys.filter((k) => k.status === 'ok').length;
  $('#st-keys').textContent = `${okKeys}/${ai.keys.length}`;
  $('#st-keys-sub').textContent = ai.keys.length ? 'ishlayotgan / jami' : 'kalit qo‘shing!';
  $('#st-uptime').textContent = duration(srv.uptime);
  $('#st-mem').textContent = `${srv.memory_mb} MB xotira`;

  const acc = tg.account;
  const dash = $('#dash-account');
  if (acc) {
    dash.replaceChildren(el('img', { class: 'avatar', src: '/api/account/photo?' + Math.floor(Date.now() / 60000), alt: '' }),
      el('div', {}, el('b', { text: acc.name }), el('div', { class: 'muted small', text: `${acc.username ? '@' + acc.username + ' · ' : ''}${acc.phone} · ID ${acc.id}` })));
    const img = $('img', dash);
    img.addEventListener('error', () => img.replaceWith(el('div', { class: 'avatar', text: (acc.first_name || '?')[0] })));
  } else {
    dash.replaceChildren(el('div', { class: 'avatar', text: '?' }), el('div', { class: 'muted', text: tg.state_text }));
  }
  kv($('#dash-ai'), [
    ['Asosiy model', ai.model],
    ['Oxirgi ishlagan', ai.last_model || '—'],
    ['Javob tezligi', ai.last_latency ? ai.last_latency + ' s' : '—'],
    ['So‘rovlar', String(ai.requests)],
    ['Oxirgi xato', ai.last_error || 'yo‘q'],
  ]);
  kv($('#dash-server'), [
    ['Holat', pill('Ishlayapti', 'ok')],
    ['Python', srv.python],
    ['Yuklanish', srv.load == null ? '—' : srv.load.toFixed(2)],
    ['Disk (bo‘sh)', srv.disk_free_gb + ' GB'],
    ['Zaxira', srv.backup.enabled ? (srv.backup.last_backup_at ? ago(srv.backup.last_backup_at) : 'kutilmoqda') : 'o‘chiq'],
  ]);
  kv($('#dash-activity'), [
    ['Suhbatlar', String(act.events_24h)],
    ['Qarz mavzusi', String(act.debt_24h)],
    ['Navbatda', String(act.pending)],
    ['Oxirgi xabar', ago(tg.last_update_at)],
    ['Ikkinchi akkaunt', act.owner_id ? 'ulangan ✓' : 'sozlanmagan'],
  ]);

  // telegram section
  const loggedIn = tg.state === 'online' && acc;
  $('#login-card').classList.toggle('hidden', !!loggedIn || tg.state === 'no_credentials');
  $('#account-cards').classList.toggle('hidden', !loggedIn);
  $('#login-state').textContent = tg.state_text;
  $('#login-step-phone').classList.toggle('hidden', tg.state === 'code_sent' || tg.state === 'password_needed');
  $('#login-step-code').classList.toggle('hidden', tg.state !== 'code_sent');
  $('#login-step-password').classList.toggle('hidden', tg.state !== 'password_needed');
  if (!$('#tg-phone').value && VALUES.phone) $('#tg-phone').value = VALUES.phone;
  if (acc) {
    $('#tg-name').textContent = acc.name;
    $('#tg-username').textContent = acc.username ? '@' + acc.username : '';
    if (document.activeElement !== $('#tg-first')) $('#tg-first').value = acc.first_name;
    if (document.activeElement !== $('#tg-last')) $('#tg-last').value = acc.last_name;
  }
  const tp = $('#tg-pill');
  tp.className = `pill ${kind}`;
  tp.textContent = tg.state_text;
  kv($('#tg-session'), [
    ['Holat', `${tg.state_text} (${ago(tg.state_since)})`],
    ['Qurilma nomi', tg.device],
    ['Data-markaz', tg.dc ? 'DC' + tg.dc : '—'],
    ['Sessiya fayli', tg.session_file ? 'bor ✓' : 'yo‘q'],
    ['Zaxira nusxa', tg.session_backup ? 'bor ✓' : 'yo‘q'],
    ['Oxirgi xato', tg.last_error || 'yo‘q'],
  ]);

  const op = $('#owner-pill');
  op.className = 'pill ' + (act.owner_id ? 'ok' : 'warn');
  op.textContent = act.owner_id ? `Ulangan · ID ${act.owner_id}` : 'Topilmadi / sozlanmagan';

  kv($('#sec-info'), [
    ['Parol', 'scrypt bilan shifrlangan'],
    ['Brute-force', '5 xato → 15 daqiqa blok'],
    ['Panel sessiyasi', '12 soat, 2 soat harakatsizlikda tugaydi'],
    ['Zaxira', srv.backup.enabled ? `shifrlangan → ${srv.backup.repo}` : 'o‘chiq (HF_TOKEN/STATE_REPO)'],
    ['Oxirgi zaxira', srv.backup.last_backup_at ? date(srv.backup.last_backup_at) : '—'],
    ['Zaxira xatosi', srv.backup.last_error || 'yo‘q'],
  ]);

  renderKeys(ai.keys);
}

// ---------- AI master toggle ----------
$('#ai-master').addEventListener('change', async (e) => {
  try {
    await api('/api/ai/toggle', { enabled: e.target.checked });
    VALUES.ai_enabled = e.target.checked;
    toast(e.target.checked ? 'AI yoqildi' : 'AI to‘xtatildi');
    renderSettings();
    refreshStatus();
  } catch (err) { e.target.checked = !e.target.checked; }
});

// ---------- keys ----------
function renderKeys(keys) {
  const list = $('#keys-list');
  if (!keys.length) {
    list.replaceChildren(el('div', { class: 'key-row muted', text: 'Hali kalit qo‘shilmagan. Pastdan qo‘shing — kamida bittasi kerak.' }));
    return;
  }
  list.replaceChildren(...keys.map((k, i) => el('div', { class: 'key-row' },
    el('span', { class: 'num', text: String(i + 1) }),
    el('div', { class: 'grow' }, el('code', { text: k.key }),
      el('div', { class: 'muted small', text: k.status === 'ok'
        ? `✓ ${k.successes} ta muvaffaqiyatli${k.last_ok ? ' · ' + ago(k.last_ok) : ''}`
        : `⏳ dam olmoqda ${duration(k.cooldown_seconds)} · ${k.last_error}` })),
    el('div', { class: 'key-actions' },
      pill(k.status === 'ok' ? 'faol' : 'kutish', k.status === 'ok' ? 'ok' : 'warn'),
      el('button', { class: 'btn ghost sm', text: '↑', title: 'Yuqoriga', disabled: i === 0, onclick: () => moveKey(i, -1) }),
      el('button', { class: 'btn ghost sm', text: '↓', title: 'Pastga', disabled: i === keys.length - 1, onclick: () => moveKey(i, 1) }),
      el('button', { class: 'btn danger sm', text: 'O‘chirish', onclick: () => deleteKey(i) })))));
}
async function moveKey(index, direction) {
  const r = await api('/api/keys/move', { index, direction });
  renderKeys(r.keys);
}
async function deleteKey(index) {
  if (!confirm('Bu kalitni o‘chirasizmi?')) return;
  const r = await api('/api/keys/delete', { index });
  renderKeys(r.keys);
  toast('Kalit o‘chirildi');
}
$('#key-add').addEventListener('click', async () => {
  const input = $('#key-new');
  try {
    const r = await api('/api/keys/add', { key: input.value.trim() });
    input.value = '';
    renderKeys(r.keys);
    const c = r.check || {};
    toast(c.ok ? `Kalit qo‘shildi ✓${c.is_free_tier ? ' (bepul tarif)' : ''}` : `Qo‘shildi, lekin tekshiruv: ${c.error}`, !c.ok);
  } catch (e) { /* toast */ }
});
$('#ai-test').addEventListener('click', async () => {
  const out = $('#ai-test-out');
  out.textContent = 'Kutilmoqda...';
  try {
    const r = await api('/api/ai/test', { text: $('#ai-test-text').value });
    out.textContent = `${r.model} · ${r.seconds} s\n\n${r.answer}`;
  } catch (e) { out.textContent = e.message; }
});

// ---------- telegram ----------
function tgError(msg) { $('#tg-error').textContent = msg || ''; }
async function tgStep(path, data) {
  tgError('');
  try { const r = await api(path, data, { quiet: true }); await refreshStatus(); return r; }
  catch (e) { tgError(e.message); return null; }
}
$('#tg-send-code').addEventListener('click', () => tgStep('/api/telegram/send_code', { phone: $('#tg-phone').value.trim() }));
$('#tg-submit-code').addEventListener('click', async () => {
  const r = await tgStep('/api/telegram/code', { code: $('#tg-code').value.trim() });
  if (r && r.result === 'ok') toast('Telegram ulandi ✓');
  setTimeout(refreshStatus, 1500);
});
$('#tg-submit-password').addEventListener('click', async () => {
  const r = await tgStep('/api/telegram/password', { password: $('#tg-2fa').value });
  $('#tg-2fa').value = '';
  if (r) toast('Telegram ulandi ✓');
  setTimeout(refreshStatus, 1500);
});
$('#tg-back').addEventListener('click', () => {
  $('#login-step-code').classList.add('hidden');
  $('#login-step-phone').classList.remove('hidden');
});
$('#tg-reconnect').addEventListener('click', async () => { await api('/api/telegram/reconnect', {}); toast('Qayta ulanmoqda...'); setTimeout(refreshStatus, 3000); });
$('#tg-logout').addEventListener('click', async () => {
  if (!confirm('Sessiya Telegram tomonidan butunlay bekor qilinadi. Qayta ishlatish uchun yana login qilish kerak bo‘ladi. Davom etasizmi?')) return;
  await api('/api/telegram/logout', {});
  toast('Sessiya bekor qilindi');
  refreshStatus();
});
$('#tg-save-name').addEventListener('click', async () => {
  const r = await api('/api/account/name', { first_name: $('#tg-first').value, last_name: $('#tg-last').value });
  toast(r.message || 'Saqlandi');
  refreshStatus();
});
$('#tg-photo').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('photo', file);
  try {
    const r = await api('/api/account/photo', fd);
    toast(r.message || 'Rasm yangilandi');
    setTimeout(refreshAvatar, 1500);
  } catch (err) { /* toast */ }
  e.target.value = '';
});
function refreshAvatar() {
  const img = $('#tg-avatar');
  img.src = '/api/account/photo?' + Date.now();
}
$('#tg-avatar').addEventListener('error', (e) => { e.target.removeAttribute('src'); });

$('#tg-load-sessions').addEventListener('click', async () => {
  const table = $('#tg-sessions');
  try {
    const r = await api('/api/telegram/sessions');
    const head = el('tr', {}, ...['Qurilma', 'Ilova', 'Joylashuv', 'Oxirgi faollik', ''].map((h) => el('th', { text: h })));
    const rows = r.sessions.map((s) => el('tr', {},
      el('td', {}, el('b', { text: s.device }), el('div', { class: 'muted small', text: s.platform })),
      el('td', { text: s.app }),
      el('td', {}, `${s.ip}`, el('div', { class: 'muted small', text: s.country })),
      el('td', { text: s.current ? 'hozir' : ago(s.active) }),
      el('td', {}, s.current ? pill('Shu bot', 'ok') : el('button', {
        class: 'btn danger sm', text: 'Tugatish',
        onclick: async () => {
          if (!confirm(`${s.device} sessiyasini tugatasizmi?`)) return;
          await api('/api/telegram/terminate', { hash: s.hash });
          toast('Sessiya tugatildi');
          $('#tg-load-sessions').click();
        },
      }))));
    table.replaceChildren(el('thead', {}, head), el('tbody', {}, rows));
  } catch (e) {
    table.replaceChildren(el('tbody', {}, el('tr', {}, el('td', { class: 'muted', text: e.message }))));
  }
});

// ---------- owner / channel ----------
$('#owner-report').addEventListener('click', async () => { await api('/api/owner/report', {}); toast('Hisobot yuborildi'); });
$('#channel-post').addEventListener('click', async () => { await api('/api/owner/post', {}); toast('E‘lon chiqdi'); });

// ---------- security ----------
$('#pw-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target;
  $('#pw-error').textContent = '';
  try {
    await api('/api/password', { current: f.current.value, new: f.new.value, username: f.username.value }, { quiet: true });
    toast('Saqlandi. Qaytadan kiring.');
    setTimeout(() => { location.href = '/'; }, 1200);
  } catch (err) { $('#pw-error').textContent = err.message; }
});
$('#backup-now').addEventListener('click', async () => { await api('/api/backup', {}); toast('Zaxira saqlandi ✓'); refreshStatus(); });

// ---------- logs ----------
async function loadLogs() {
  const r = await api('/api/logs');
  const box = $('#logs');
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 20;
  box.replaceChildren(...r.lines.map((line) => el('div', {
    class: / (ERROR|CRITICAL) /.test(line) ? 'e' : / WARNING /.test(line) ? 'w' : '', text: line,
  })));
  if (atBottom) box.scrollTop = box.scrollHeight;
}
$('#logs-refresh').addEventListener('click', loadLogs);

$('#logout-btn').addEventListener('click', async () => {
  await api('/api/logout', {}).catch(() => {});
  location.href = '/';
});

// ---------- boot ----------
(async () => {
  const me = await api('/api/me');
  CSRF = me.csrf;
  $('#who').textContent = me.username;
  await loadSettings();
  await refreshStatus();
  let saved = 'dashboard';
  try { saved = localStorage.getItem('section') || 'dashboard'; } catch (e) { /* ignore */ }
  show($(`#sec-${saved}`) ? saved : 'dashboard');
  setInterval(refreshStatus, 5000);
  setInterval(() => { if ($('#sec-logs').classList.contains('active')) loadLogs(); }, 5000);
})();
