// Forwards /api/* from the Vercel panel to the private Hugging Face Space.
// The HF token and the shared proxy secret stay on the server - the browser
// never sees them, and the panel's cookies stay first-party on this domain.
export const config = { runtime: 'edge' };

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

export default async function handler(request) {
  const { HF_SPACE_URL, HF_TOKEN, PROXY_SECRET } = process.env;
  if (!HF_SPACE_URL || !HF_TOKEN || !PROXY_SECRET) {
    return json(500, 'Vercel sozlanmagan: HF_SPACE_URL, HF_TOKEN, PROXY_SECRET kerak');
  }
  const url = new URL(request.url);
  const path = url.searchParams.get('path') || '';
  if (!PATH_RE.test(path) || path.includes('..')) return json(400, 'Noto‘g‘ri manzil');

  const headers = new Headers();
  for (const name of REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  headers.set('authorization', `Bearer ${HF_TOKEN}`);
  headers.set('x-proxy-secret', PROXY_SECRET);
  const ip = request.headers.get('x-real-ip') || (request.headers.get('x-forwarded-for') || '').split(',')[0].trim();
  if (ip) headers.set('x-client-ip', ip);

  const init = { method: request.method, headers, redirect: 'manual' };
  if (!['GET', 'HEAD'].includes(request.method)) {
    const body = await request.arrayBuffer();
    if (body.byteLength > MAX_BODY) return json(413, 'Fayl juda katta');
    init.body = body;
  }

  let upstream;
  try {
    upstream = await fetch(`${HF_SPACE_URL.replace(/\/+$/, '')}/api/${path}`, init);
  } catch (e) {
    return json(502, 'Backend (Hugging Face) bilan aloqa yo‘q');
  }
  if (upstream.status >= 300 && upstream.status < 400) {
    return json(502, 'Hugging Face tokeni noto‘g‘ri yoki Space topilmadi');
  }
  const type = upstream.headers.get('content-type') || '';
  if (!type.includes('application/json') && !type.startsWith('image/')) {
    // HF shows an HTML page while the Space is building or waking up.
    return json(503, 'Backend ishga tushmoqda (Hugging Face Space uyg‘onmoqda). 1–2 daqiqadan so‘ng yangilang.');
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
