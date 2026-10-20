// Forwards /api/* from the Vercel panel to the bot backend (BACKEND_URL, e.g.
// https://z3wax.enzonic.dev, or a private Hugging Face Space). The shared proxy
// secret (and the HF token, only for *.hf.space) stay on the server - the
// browser never sees them, and the panel's cookies stay first-party here.
// Node.js runtime (not Edge): Edge hides network errors as "internal error".
export const config = { maxDuration: 30 };

const PATH_RE = /^[a-z0-9_\/-]{1,80}$/;
const REQUEST_HEADERS = ['content-type', 'cookie', 'x-csrf-token', 'accept'];
const RESPONSE_HEADERS = ['content-type', 'cache-control'];
const MAX_BODY = 9 * 1024 * 1024;

function json(status, error) {
  return new Response(JSON.stringify({ ok: false, error }), {
    status,
    headers: { 'content-type': 'application/json', 'cache-control': 'no-store' },
  });
}

// Human-readable reason for a failed fetch (DNS, refused, TLS, timeout).
function explain(e) {
  const cause = (e && e.cause) || {};
  const raw = `${e && e.name} ${e && e.message} ${cause.code || ''} ${cause.message || ''}`;
  console.error('backend fetch failed:', raw);
  if (/TimeoutError|aborted|timed? ?out/i.test(raw)) return 'server 25 soniyada javob bermadi';
  if (/ENOTFOUND|EAI_AGAIN|getaddrinfo|dns/i.test(raw)) return 'domen topilmadi (DNS)';
  if (/ECONNREFUSED/i.test(raw)) return 'ulanish rad etildi (port yopiq yoki server o‘chiq)';
  if (/CERT|SSL|TLS|self.signed|certificate/i.test(raw)) return 'SSL sertifikat xatosi (https ishlamayapti)';
  if (/ECONNRESET|socket/i.test(raw)) return 'ulanish uzildi';
  return raw.trim().slice(0, 160) || 'noma‘lum xato';
}

async function handler(request) {
  const { HF_TOKEN, PROXY_SECRET } = process.env;
  const BACKEND_URL = process.env.BACKEND_URL || process.env.HF_SPACE_URL;
  if (!BACKEND_URL || !PROXY_SECRET) {
    return json(500, 'Vercel sozlanmagan: BACKEND_URL va PROXY_SECRET kerak');
  }
  const backend = new URL(BACKEND_URL);
  const isHfSpace = backend.hostname.endsWith('.hf.space');
  const url = new URL(request.url);
  const path = url.searchParams.get('path') || '';
  if (!PATH_RE.test(path) || path.includes('..')) return json(400, 'Noto‘g‘ri manzil');

  const headers = new Headers();
  for (const name of REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  // Never hand the HF token to any host other than Hugging Face.
  if (isHfSpace && HF_TOKEN) headers.set('authorization', `Bearer ${HF_TOKEN}`);
  headers.set('x-proxy-secret', PROXY_SECRET);
  const ip = request.headers.get('x-real-ip') || (request.headers.get('x-forwarded-for') || '').split(',')[0].trim();
  if (ip) headers.set('x-client-ip', ip);

  const init = { method: request.method, headers, redirect: 'manual', signal: AbortSignal.timeout(25000) };
  if (!['GET', 'HEAD'].includes(request.method)) {
    const body = await request.arrayBuffer();
    if (body.byteLength > MAX_BODY) return json(413, 'Fayl juda katta');
    init.body = body;
  }

  let upstream;
  try {
    upstream = await fetch(`${BACKEND_URL.replace(/\/+$/, '')}/api/${path}`, init);
  } catch (e) {
    return json(502, `Backend bilan aloqa yo‘q (${backend.hostname}): ${explain(e)}`);
  }
  if (upstream.status >= 300 && upstream.status < 400) {
    return json(502, isHfSpace ? 'Hugging Face tokeni noto‘g‘ri yoki Space topilmadi' : 'Backend yo‘naltirish qaytardi (manzilni tekshiring)');
  }
  const type = upstream.headers.get('content-type') || '';
  if (!type.includes('application/json') && !type.startsWith('image/')) {
    // An HTML page here means the backend is starting (or a host error page).
    return json(503, 'Backend javob bermayapti yoki ishga tushmoqda. 1–2 daqiqadan so‘ng yangilang.');
  }

  const out = new Headers();
  for (const name of RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) out.set(name, value);
  }
  const cookies = typeof upstream.headers.getSetCookie === 'function'
    ? upstream.headers.getSetCookie()
    : [upstream.headers.get('set-cookie')].filter(Boolean);
  for (const cookie of cookies) out.append('set-cookie', cookie);
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

export const GET = handler;
export const POST = handler;
export const HEAD = handler;
