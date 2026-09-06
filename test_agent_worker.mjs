/**
 * اختبارات جانب الـWorker للمرحلة 1 + عزل المسارات القائمة.
 *
 * بنستورد worker.js **من غير ما نشيل الـdefault** — عشان نقدر نختبر
 * المسارات نفسها زي ما Cloudflare بتناديها بالظبط.
 *
 *   node test_agent_worker.mjs
 */
import { readFileSync } from 'node:fs';

const SRC = new URL('./src/worker.js', import.meta.url);
let code = readFileSync(SRC, 'utf8');
code += `
export { computeAgentStatus, AGENT_OFFLINE_SEC, tokenValid };
`;
const mod = await import(
  'data:text/javascript;base64,' + Buffer.from(code, 'utf8').toString('base64'));

let FAILED = 0;
let TOTAL = 0;
function check(label, got, want) {
  TOTAL++;
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) FAILED++;
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}   got=${JSON.stringify(got)}`);
}

function mkKV(initial = {}) {
  const store = new Map(Object.entries(initial));
  return {
    async get(k) { return store.has(k) ? store.get(k) : null; },
    async put(k, v) { store.set(k, v); },
    async delete(k) { store.delete(k); },
  };
}

/** Turso وهمي: بيرد بشكل الـpipeline الحقيقي. بيسجّل الـSQL اللي وصله. */
function installFakeTurso(rowsToReturn = []) {
  const seen = [];
  globalThis.fetch = async (url, opts) => {
    const body = JSON.parse(opts.body);
    const sql = body.requests[0].stmt.sql;
    seen.push(sql);
    const isSelect = /^\s*SELECT/i.test(sql);
    const result = isSelect
      ? {
          cols: Object.keys(rowsToReturn[0] || { agent_id: 1 }).map((n) => ({ name: n })),
          rows: rowsToReturn.map((r) => Object.values(r).map((v) => ({ value: v }))),
        }
      : undefined;
    return {
      ok: true, status: 200,
      async json() { return { results: [{ type: 'ok', response: { result } }] }; },
      async text() { return ''; },
    };
  };
  return seen;
}

const ENV = {
  KV: mkKV(),
  AGENT_SECRET: 'agent-sec',
  ADMIN_SECRET: 'admin-sec',
  RENEW_SECRET: 'renew-sec',
  WEBHOOK_SECRET: 'hook-sec',
  TURSO_URL: 'libsql://x',
  TURSO_TOKEN: 't',
  RENEWER_URL: 'https://gw.example',
};
const CTX = { waitUntil() {} };
const req = (path, { method = 'GET', headers = {}, body } = {}) =>
  new Request('https://w.example' + path, { method, headers, body });

console.log('=== حساب الحالة — الـWorker مصدر الحقيقة ===');
const S = mod.computeAgentStatus;
check('مافيش صف -> OFFLINE', S(null, 1000).code, 'OFFLINE');
check('مافيش صف -> السبب', S(null, 1000).reason, 'never_seen');
check('نبضة توّها + SMAX جاهزة -> ONLINE',
      S({ last_seen: 1000, smax_status: 'ready' }, 1000).code, 'ONLINE');
check('نبضة توّها + SMAX مش جاهزة -> 🟡',
      S({ last_seen: 1000, smax_status: 'not_configured' }, 1000).code,
      'ONLINE_SMAX_NOT_READY');
check('عند الحد بالظبط (90) -> لسه ONLINE',
      S({ last_seen: 1000, smax_status: 'ready' }, 1090).code, 'ONLINE');
check('بعد الحد بثانية (91) -> OFFLINE',
      S({ last_seen: 1000, smax_status: 'ready' }, 1091).code, 'OFFLINE');
check('OFFLINE بيغلب حالة SMAX',
      S({ last_seen: 1000, smax_status: 'ready' }, 5000).code, 'OFFLINE');
check('الحد 90 ثانية', mod.AGENT_OFFLINE_SEC, 90);

console.log('\n=== مسارات الوكيل ===');
{
  installFakeTurso();
  let r = await mod.default.fetch(
    req('/agent/heartbeat', { method: 'POST', body: '{"agent_id":"a1"}' }), ENV, CTX);
  check('نبضة من غير سر -> 401', r.status, 401);

  r = await mod.default.fetch(req('/agent/heartbeat', {
    method: 'POST', headers: { Authorization: 'Bearer wrong' },
    body: '{"agent_id":"a1"}' }), ENV, CTX);
  check('نبضة بسر غلط -> 401', r.status, 401);

  const seen = installFakeTurso();
  r = await mod.default.fetch(req('/agent/heartbeat', {
    method: 'POST', headers: { Authorization: 'Bearer agent-sec' },
    body: JSON.stringify({ agent_id: 'a1', smax_status: 'not_configured' }) }),
    ENV, CTX);
  check('نبضة بسر صح -> 200', r.status, 200);
  const j = await r.json();
  check('الحالة المحسوبة', j.status, 'ONLINE_SMAX_NOT_READY');
  check('الـupsert استخدم ON CONFLICT (مافيش صف مكرر)',
        /ON CONFLICT\(agent_id\)/.test(seen[0] || ''), true);
  check('اتكتب في agent_state بس', /INSERT INTO agent_state/.test(seen[0] || ''), true);

  r = await mod.default.fetch(req('/agent/heartbeat', {
    method: 'POST', headers: { Authorization: 'Bearer agent-sec' },
    body: '{}' }), ENV, CTX);
  check('نبضة من غير agent_id -> 400', r.status, 400);

  // AGENT_SECRET مش متظبّط -> المسار مقفول بالكامل
  r = await mod.default.fetch(req('/agent/heartbeat', {
    method: 'POST', headers: { Authorization: 'Bearer ' },
    body: '{"agent_id":"a1"}' }), { ...ENV, AGENT_SECRET: undefined }, CTX);
  check('من غير AGENT_SECRET في البيئة -> مقفول', r.status, 401);

  installFakeTurso([{ agent_id: 'a1', last_seen: String(Math.floor(Date.now() / 1000)),
                      agent_status: 'running', smax_status: 'not_configured',
                      version: 'phase1', host_os: 'Windows' }]);
  r = await mod.default.fetch(req('/agent/status', {
    headers: { Authorization: 'Bearer admin-sec' } }), ENV, CTX);
  check('/agent/status بسر الإدارة -> 200', r.status, 200);
  const js = await r.json();
  check('بيرجّع وكيل واحد', js.agents.length, 1);
  check('من غير أي سر في الرد',
        JSON.stringify(js).includes('agent-sec')
        || JSON.stringify(js).includes('admin-sec'), false);

  r = await mod.default.fetch(req('/agent/status', {
    headers: { Authorization: 'Bearer nope' } }), ENV, CTX);
  check('/agent/status بسر غلط -> 401', r.status, 401);
}

console.log('\n=== عزل المسارات القائمة — لازم تفضل زي ما هي ===');
{
  installFakeTurso();
  let r = await mod.default.fetch(req('/health'), ENV, CTX);
  check('/health -> 200', r.status, 200);
  const h = await r.json();
  check('/health شكله زي ما هو',
        Object.keys(h).sort(),
        ['needs_renew', 'ok', 'renew_lock_held', 'renewer_configured',
         'token', 'token_exp_in_sec'].sort());

  r = await mod.default.fetch(req('/token', { method: 'POST', body: '{}' }), ENV, CTX);
  check('/token من غير سر -> 401', r.status, 401);

  r = await mod.default.fetch(req('/finish', { method: 'POST', body: '{}' }), ENV, CTX);
  check('/finish من غير سر -> 401', r.status, 401);

  r = await mod.default.fetch(req('/webhook', {
    method: 'POST', headers: { 'X-Telegram-Bot-Api-Secret-Token': 'bad' },
    body: '{}' }), ENV, CTX);
  check('/webhook بسر غلط -> 403', r.status, 403);

  r = await mod.default.fetch(req('/'), ENV, CTX);
  check('catch-all زي ما هو', await r.text(), 'Egypt Post Tracking Bot');

  r = await mod.default.fetch(req('/agent/heartbeat'), ENV, CTX);
  check('GET على مسار نبضة POST -> catch-all', await r.text(),
        'Egypt Post Tracking Bot');
}


console.log('\n=== الاختبارات الأمنية العشرة ===');
{
  const hb = (env, headers, body) => mod.default.fetch(
    req('/agent/heartbeat', { method: 'POST', headers, body }), env, CTX);
  const good = JSON.stringify({ agent_id: 'a1', smax_status: 'not_configured' });

  installFakeTurso([{ agent_id: 'a1', last_seen: String(Math.floor(Date.now()/1000)),
                      agent_status: 'running', smax_status: 'ready',
                      version: 'p1', host_os: 'Windows' }]);
  let r = await mod.default.fetch(req('/agent/status', {
    headers: { Authorization: 'Bearer admin-sec' } }), ENV, CTX);
  check('أمن 1. ADMIN_SECRET صحيح -> 200', r.status, 200);

  r = await mod.default.fetch(req('/agent/status', {
    headers: { Authorization: 'Bearer WRONG' } }), ENV, CTX);
  check('أمن 2. ADMIN_SECRET خاطئ -> 401', r.status, 401);

  // 🔴 الحالة اللي المستخدم رصدها: السر مش موجود
  r = await mod.default.fetch(req('/agent/status', {
    headers: { Authorization: 'Bearer undefined' } }),
    { ...ENV, ADMIN_SECRET: undefined }, CTX);
  check('أمن 3. ADMIN_SECRET مفقود + "Bearer undefined" -> 401', r.status, 401);
  r = await mod.default.fetch(req('/agent/status', {
    headers: { Authorization: 'Bearer ' } }),
    { ...ENV, ADMIN_SECRET: '' }, CTX);
  check('أمن 3b. ADMIN_SECRET فاضي -> 401', r.status, 401);

  installFakeTurso();
  r = await hb(ENV, { Authorization: 'Bearer agent-sec' }, good);
  check('أمن 4. AGENT_SECRET صحيح -> 200', r.status, 200);

  r = await hb(ENV, { Authorization: 'Bearer WRONG' }, good);
  check('أمن 5. AGENT_SECRET خاطئ -> 401', r.status, 401);

  r = await hb({ ...ENV, AGENT_SECRET: undefined },
               { Authorization: 'Bearer undefined' }, good);
  check('أمن 6. AGENT_SECRET مفقود -> 401', r.status, 401);

  const errOf = async (res) => (await res.json()).error;

  r = await hb(ENV, { Authorization: 'Bearer agent-sec' },
               JSON.stringify({ smax_status: 'ready' }));
  check('أمن 7. من غير agent_id -> 400', r.status, 400);
  check('أمن 7b. السبب', await errOf(r), 'bad_agent_id');

  r = await hb(ENV, { Authorization: 'Bearer agent-sec' },
               JSON.stringify({ agent_id: 'x'.repeat(65), smax_status: 'ready' }));
  check('أمن 8. agent_id > 64 -> 400', r.status, 400);

  r = await hb(ENV, { Authorization: 'Bearer agent-sec' },
               JSON.stringify({ agent_id: 'a1', smax_status: 'whatever' }));
  check('أمن 9. smax_status غير مسموح -> 400', r.status, 400);
  check('أمن 9b. السبب', await errOf(r), 'bad_smax_status');

  r = await hb(ENV, { Authorization: 'Bearer agent-sec' },
               JSON.stringify({ agent_id: 'a1', smax_status: 'ready',
                                evil: 'x' }));
  check('أمن 10. حقل غير متوقّع -> 400', r.status, 400);
  check('أمن 10b. السبب', await errOf(r), 'unexpected_field');

  r = await hb(ENV, { Authorization: 'Bearer agent-sec' },
               JSON.stringify({ agent_id: 'a1', smax_status: 'ready',
                                version: 'v'.repeat(33) }));
  check('أمن 10c. version طويل -> 400', r.status, 400);

  r = await hb(ENV, { Authorization: 'Bearer agent-sec' },
               JSON.stringify({ agent_id: 'a1', smax_status: 'ready',
                                host_os: 'h'.repeat(2000) }));
  check('أمن 10d. جسم ضخم -> 400', r.status, 400);
  check('أمن 10e. السبب', await errOf(r), 'payload_too_large');

  r = await hb(ENV, { Authorization: 'Bearer agent-sec' }, 'not-json');
  check('أمن 10f. JSON تالف -> 400', r.status, 400);

  r = await hb(ENV, { Authorization: 'Bearer agent-sec' },
               JSON.stringify({ agent_id: 'a b/c', smax_status: 'ready' }));
  check('أمن 10g. agent_id بحروف ممنوعة -> 400', r.status, 400);

  r = await hb(ENV, { Authorization: 'Bearer agent-sec' },
               JSON.stringify({ agent_id: 'a1', smax_status: 'ready',
                                agent_status: 'pretending' }));
  check('أمن 10h. agent_status غير مسموح -> 400', r.status, 400);
}

console.log('\n=== مصدر الحقيقة — إبلاغ الوكيل مش بيغيّر الحكم ===');
{
  const S = mod.computeAgentStatus;
  const now = 1000;
  check('وكيل بيقول running ونبضته قديمة -> OFFLINE برضه',
        S({ last_seen: 900, smax_status: 'ready', agent_status: 'running' },
          now + 100).code, 'OFFLINE');
  check('وكيل بيقول stopping ونبضته حديثة -> ONLINE برضه',
        S({ last_seen: now, smax_status: 'ready', agent_status: 'stopping' },
          now).code, 'ONLINE');
  check('نبضة قديمة + smax=ready -> OFFLINE (الوقت بيغلب)',
        S({ last_seen: 0, smax_status: 'ready' }, 1000).code, 'OFFLINE');
  check('نبضة حديثة + smax مش ready -> 🟡 مش OFFLINE',
        S({ last_seen: now, smax_status: 'error' }, now).code,
        'ONLINE_SMAX_NOT_READY');
  check('agent_status مش موجود خالص -> الحكم شغّال عادي',
        S({ last_seen: now, smax_status: 'ready' }, now).code, 'ONLINE');
}

console.log('\n=== أمن اللوجات — مافيش سر في أي رد ===');
{
  installFakeTurso([{ agent_id: 'a1', last_seen: String(Math.floor(Date.now()/1000)),
                      agent_status: 'running', smax_status: 'ready',
                      version: 'p1', host_os: 'Windows' }]);
  const r = await mod.default.fetch(req('/agent/status', {
    headers: { Authorization: 'Bearer admin-sec' } }), ENV, CTX);
  const body = await r.text();
  for (const s of ['agent-sec', 'admin-sec', 'renew-sec', 'hook-sec', 't']) {
    if (s === 't') continue;
    check('مافيش "' + s + '" في الرد', body.includes(s), false);
  }
  const r2 = await mod.default.fetch(req('/agent/heartbeat', {
    method: 'POST', headers: { Authorization: 'Bearer agent-sec' },
    body: JSON.stringify({ agent_id: 'a1', smax_status: 'ready' }) }), ENV, CTX);
  const b2 = await r2.text();
  check('مافيش سر في رد النبضة',
        b2.includes('agent-sec') || b2.includes('admin-sec'), false);
}

console.log('\n' + '='.repeat(52));
if (FAILED) { console.log(`❌ فشل ${FAILED} من ${TOTAL}`); process.exit(1); }
console.log(`✔ كل الاختبارات نجحت — ${TOTAL} اختبار`);