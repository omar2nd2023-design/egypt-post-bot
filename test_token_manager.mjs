/**
 * اختبارات دورة حياة التوكن في الـWorker — من غير انتظار 15 دقيقة.
 *
 * بنستورد worker.js كنص وناخد منه الدوال المطلوبة، وبنركّب KV وهمي
 * وساعة مزيّفة. مفيش أي توكن حقيقي هنا — التوكنات مبنية بـexp محسوب.
 *
 *   node test_token_manager.mjs
 */
import { readFileSync } from 'node:fs';

const SRC = new URL('./src/worker.js', import.meta.url);
let code = readFileSync(SRC, 'utf8');

// الملف بيصدّر default object فيه fetch — بنشيله ونصدّر الدوال الداخلية
code = code.replace(/^export default \{[\s\S]*$/m, '');
code += `
export { tokenValid, tokenExpIn, getToken, renewNow, acquireLock,
         releaseLock, triggerRenew, waitForToken, fetchJourney,
         TOKEN_KEY, LOCK_KEY, USABLE_MARGIN, RENEW_MARGIN,
         TRACK_BUDGET_MS };
`;
const mod = await import(
  'data:text/javascript;base64,' + Buffer.from(code, 'utf8').toString('base64'));

// ---------------------------------------------------------------- أدوات
let FAILED = 0;
function check(label, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) FAILED++;
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}   got=${JSON.stringify(got)}`);
}

/** توكن وهمي بـexp نسبي. التوقيع مزيّف — الكود بيقرا الـpayload بس. */
function mkToken(secondsLeft, id = 'a') {
  const p = { exp: Math.floor(Date.now() / 1000) + secondsLeft, id };
  const b64 = (o) => Buffer.from(JSON.stringify(o)).toString('base64url');
  return `eyJhbGciOiJSUzI1NiJ9.${b64(p)}.sig_${id}`;
}

/** KV وهمي: بيدعم TTL، وبيعدّ كل عملية عشان نتأكد من السلوك. */
function mkKV(initial = {}) {
  const store = new Map(Object.entries(initial));
  const exp = new Map();
  const ops = { get: 0, put: 0, delete: 0 };
  return {
    ops, store,
    async get(k) {
      ops.get++;
      const e = exp.get(k);
      if (e && Date.now() > e) { store.delete(k); exp.delete(k); }
      return store.has(k) ? store.get(k) : null;
    },
    async put(k, v, o = {}) {
      ops.put++;
      store.set(k, v);
      if (o.expirationTtl) exp.set(k, Date.now() + o.expirationTtl * 1000);
    },
    async delete(k) { ops.delete++; store.delete(k); exp.delete(k); },
  };
}

/** بيئة وهمية. renewImpl بيحدّد شكل التجديد في كل حالة. */
function mkEnv(kv, renewImpl) {
  const calls = { renew: 0 };
  globalThis.fetch = async (u, init) => {
    if (String(u).endsWith('/renew')) {
      calls.renew++;
      const r = await renewImpl(calls.renew, init);
      return { ok: r.ok, status: r.ok ? 200 : 500, json: async () => ({}) };
    }
    throw new Error('unexpected fetch: ' + u);
  };
  return {
    env: { KV: kv, RENEWER_URL: 'https://renewer.test', RENEW_SECRET: 's3cr3t' },
    calls,
  };
}

const ctxOf = () => {
  const waits = [];
  return { waitUntil: (p) => waits.push(p), waits };
};

// ---------------------------------------------------------------- الحالات
console.log('\n--- Case 1: توكن صالح ومريّح → مفيش تجديد ---');
{
  const kv = mkKV({ de_token: mkToken(800, 'A') });
  const { env, calls } = mkEnv(kv, async () => ({ ok: true }));
  const t = await mod.getToken(env, ctxOf());
  check('رجّع نفس التوكن', t === kv.store.get('de_token'), true);
  check('مفيش نداء تجديد', calls.renew, 0);
}

console.log('\n--- Case 2: قرب يخلص → تجديد في الخلفية، والمستخدم مايستناش ---');
{
  const old = mkToken(60, 'A');
  const kv = mkKV({ de_token: old });
  const { env, calls } = mkEnv(kv, async () => {
    await kv.put('de_token', mkToken(900, 'B'));
    return { ok: true };
  });
  const ctx = ctxOf();
  const t = await mod.getToken(env, ctx);
  check('رجّع القديم فورًا (مافيش انتظار)', t === old, true);
  await Promise.all(ctx.waits);
  check('التجديد اتنده مرة', calls.renew, 1);
  check('KV فيه التوكن الجديد', kv.store.get('de_token') !== old, true);
  check('القفل اتفك', await kv.get('renew_lock'), null);
}

console.log('\n--- Case 3: خلص → تجديد وانتظار → توكن جديد ---');
{
  const dead = mkToken(-10, 'A');
  const kv = mkKV({ de_token: dead });
  const { env, calls } = mkEnv(kv, async () => {
    await kv.put('de_token', mkToken(900, 'B'));
    return { ok: true };
  });
  const t = await mod.getToken(env, ctxOf());
  check('رجّع الجديد', t !== dead && mod.tokenValid(t, 30), true);
  check('تجديد واحد بس', calls.renew, 1);
}

console.log('\n--- Case 4: التجديد فشل + القديم لسه صالح → نكمّل بالقديم ---');
{
  const old = mkToken(90, 'A');            // أقل من RENEW_MARGIN، أكبر من USABLE
  const kv = mkKV({ de_token: old });
  const { env } = mkEnv(kv, async () => ({ ok: false }));
  const ctx = ctxOf();
  const t = await mod.getToken(env, ctx);
  await Promise.all(ctx.waits);
  check('رجّع القديم', t === old, true);
  check('القديم لسه موجود في KV (ما اتمسحش)', kv.store.get('de_token'), old);
  check('صفر عمليات delete على التوكن', kv.ops.delete <= 1, true);
}

console.log('\n--- Case 5: خلص + التجديد فشل → فشل محكوم (null) ---');
{
  const kv = mkKV({ de_token: mkToken(-100, 'A') });
  const { env, calls } = mkEnv(kv, async () => ({ ok: false }));
  const t = await mod.getToken(env, ctxOf());
  check('رجّع null', t, null);
  check('محاولة تجديد واحدة بس', calls.renew, 1);
  check('التوكن القديم لسه في KV', kv.store.has('de_token'), true);
}

console.log('\n--- Case 6: 10 طلبات متزامنة → تجديد واحد ---');
{
  const kv = mkKV({ de_token: mkToken(-10, 'A') });
  let renewing = false;
  const { env, calls } = mkEnv(kv, async () => {
    if (renewing) throw new Error('تجديدين في نفس الوقت!');
    renewing = true;
    await new Promise((r) => setTimeout(r, 50));
    await kv.put('de_token', mkToken(900, 'B'));
    renewing = false;
    return { ok: true };
  });
  const out = await Promise.all(
    Array.from({ length: 10 }, () => mod.getToken(env, ctxOf())));
  check('نداء تجديد واحد بس', calls.renew, 1);
  check('كلهم خدوا توكن', out.every((t) => mod.tokenValid(t, 30)), true);
  check('كلهم نفس التوكن', new Set(out).size, 1);
}

console.log('\n--- Case 7: القفل متاخد → الباقي يستنى بدل ما يجدّد ---');
{
  const kv = mkKV({ de_token: mkToken(-10, 'A'), renew_lock: '1' });
  const { env, calls } = mkEnv(kv, async () => ({ ok: true }));
  setTimeout(() => kv.put('de_token', mkToken(900, 'B')), 60);
  const t = await mod.getToken(env, ctxOf());
  check('ما نداش تجديد (القفل مع غيره)', calls.renew, 0);
  check('استنى وخد الجديد', mod.tokenValid(t, 30), true);
}

console.log('\n--- Case 8: القفل انتهى → التعافي وأخذ القفل ---');
{
  const kv = mkKV({ de_token: mkToken(-10, 'A') });
  await kv.put('renew_lock', '1', { expirationTtl: 0.05 });   // 50ms
  await new Promise((r) => setTimeout(r, 120));               // نسيبه ينتهي
  const { env, calls } = mkEnv(kv, async () => {
    await kv.put('de_token', mkToken(900, 'B'));
    return { ok: true };
  });
  const t = await mod.getToken(env, ctxOf());
  check('القفل المنتهي ما عطّلش التجديد', calls.renew, 1);
  check('خد توكن جديد', mod.tokenValid(t, 30), true);
}

console.log('\n--- Case 9: إعادة تشغيل (KV فاضية) → مفيش حالة تالفة ---');
{
  const kv = mkKV({});                       // زي worker بدأ من الأول
  const { env, calls } = mkEnv(kv, async () => {
    await kv.put('de_token', mkToken(900, 'B'));
    return { ok: true };
  });
  const t = await mod.getToken(env, ctxOf());
  check('اشتغل من غير توكن سابق', mod.tokenValid(t, 30), true);
  check('تجديد واحد', calls.renew, 1);
  check('القفل اتفك', await kv.get('renew_lock'), null);
}

console.log('\n--- Case 10: renewNow لازم يرجّع توكن مختلف ---');
{
  const same = mkToken(900, 'A');
  const kv = mkKV({ de_token: same });
  const { env } = mkEnv(kv, async () => ({ ok: true }));   // ما غيّرش حاجة
  const t = await mod.renewNow(env);
  check('نفس التوكن = فشل (يمنع إعادة لا نهائية)', t, null);

  const kv2 = mkKV({ de_token: mkToken(900, 'A') });
  const { env: e2 } = mkEnv(kv2, async () => {
    await kv2.put('de_token', mkToken(900, 'B'));
    return { ok: true };
  });
  const t2 = await mod.renewNow(e2);
  check('توكن مختلف = نجاح', t2 !== null, true);
}

console.log('\n--- إضافي: tokenExpIn مابيكشفش أي جزء من التوكن ---');
{
  const tk = mkToken(500, 'SECRETID');
  const n = mod.tokenExpIn(tk);
  check('رقم منطقي', n > 480 && n <= 500, true);
  check('مش نص', typeof n, 'number');
}

console.log('\n--- Case 11: الحد الزمني مايأثرش على المسار السريع ---');
{
  const env = { RENEWER_URL: 'https://gw.test', RENEW_SECRET: 's3cr3t' };
  const payload = { records: [{ a: 1 }, { a: 2 }], status: 'تم تسليم الشحنة' };
  let sawSignal = false;
  globalThis.fetch = async (u, init) => {
    sawSignal = !!init.signal;                 // الميزانية اتبعتت فعلاً
    return { ok: true, status: 200, json: async () => payload };
  };
  const t0 = Date.now();
  const r = await mod.fetchJourney('EKPB0412385EG', null, env,
                                   mod.TRACK_BUDGET_MS);
  check('العقد زي ما هو {records,status}', r, payload);
  check('مافيش err', 'err' in r, false);
  check('رجّع فورًا', Date.now() - t0 < 500, true);
  check('الـsignal اتبعت للـfetch', sawSignal, true);
}

console.log('\n--- Case 12: البطء بيرجّع timeout (مش خطأ تاني) ---');
{
  const env = { RENEWER_URL: 'https://gw.test', RENEW_SECRET: 's3cr3t' };
  // fetch بيحترم الـsignal زي الحقيقي
  globalThis.fetch = (u, init) => new Promise((res, rej) => {
    const t = setTimeout(
      () => res({ ok: true, status: 200, json: async () => ({}) }), 5000);
    init.signal.addEventListener('abort', () => {
      clearTimeout(t);
      const e = new Error('The operation was aborted due to timeout');
      e.name = 'TimeoutError';
      rej(e);
    });
  });
  const t0 = Date.now();
  const r = await mod.fetchJourney('X', null, env, 300);
  const ms = Date.now() - t0;
  check('رجّع err=timeout', r, { err: 'timeout' });
  check('وقف عند الميزانية مش عند الـ5 ثواني', ms < 2000, true);
}

console.log('\n--- Case 13: خطأ عادي مابيتلبسش لبس timeout ---');
{
  const env = { RENEWER_URL: 'https://gw.test', RENEW_SECRET: 's3cr3t' };
  globalThis.fetch = async () => { throw new TypeError('network down'); };
  const r = await mod.fetchJourney('X', null, env, 5000);
  check('مش timeout', r.err === 'timeout', false);
  check('فيه err واضح', typeof r.err === 'string' && r.err.length > 0, true);
}

console.log('\n--- Case 14: gateway 4xx بيفضل زي ما هو ---');
{
  const env = { RENEWER_URL: 'https://gw.test', RENEW_SECRET: 's3cr3t' };
  globalThis.fetch = async () => ({ ok: false, status: 401 });
  const r = await mod.fetchJourney('X', null, env, 5000);
  check('gateway http 401', r, { err: 'gateway http 401' });
}

console.log('\n--- Case 15: البوابة نايمة (503) → إعادة واحدة ---');
{
  const env = { RENEWER_URL: 'https://gw.test', RENEW_SECRET: 's3cr3t' };
  let n = 0;
  globalThis.fetch = async () => {
    n++;
    return n === 1
      ? { ok: false, status: 503 }
      : { ok: true, status: 200,
          json: async () => ({ records: [{ a: 1 }], status: 'ok' }) };
  };
  const r = await mod.fetchJourney('X', null, env, 20000);
  check('نجح بعد الاستيقاظ', r.records.length, 1);
  check('نداءين بالظبط — مفيش تالت', n, 2);
}
console.log('\n--- Case 16: notify بيتبعت للبوابة لما يكون متاح ---');
{
  const env = { RENEWER_URL: 'https://gw.test', RENEW_SECRET: 's3cr3t' };
  let seen = null;
  globalThis.fetch = async (u, init) => {
    seen = JSON.parse(init.body);
    return { ok: true, status: 200,
             json: async () => ({ records: [], status: '' }) };
  };
  await mod.fetchJourney('EKPB0412385EG', null, env, 18000,
                         { chat_id: 55, message_id: 66 });
  check('الباركود اتبعت', seen.barcode, 'EKPB0412385EG');
  check('notify موجود', !!seen.notify, true);
  check('chat_id', seen.notify.chat_id, 55);
  check('message_id', seen.notify.message_id, 66);
  check('الميزانية اتبعتت معاه', seen.notify.budget_ms, 18000);
}

console.log('\n--- Case 17: من غير notify الجسم زي ما هو ---');
{
  const env = { RENEWER_URL: 'https://gw.test', RENEW_SECRET: 's3cr3t' };
  let seen = null;
  globalThis.fetch = async (u, init) => {
    seen = JSON.parse(init.body);
    return { ok: true, status: 200,
             json: async () => ({ records: [], status: '' }) };
  };
  await mod.fetchJourney('X', null, env, 18000);
  check('مفاتيح الجسم: barcode بس', Object.keys(seen), ['barcode']);
  check('مفيش notify', 'notify' in seen, false);
}

console.log('\n--- Case 18: مفيش نداء ذاتي في الكود ---');
{
  const fs = await import('node:fs');
  const src = fs.readFileSync(
    new URL('./src/worker.js', import.meta.url), 'utf8');
  check('صفر handoff', /handoff/.test(src), false);
  check('صفر WORKER_URL', /WORKER_URL/.test(src), false);
  check('/finish بـRENEW_SECRET',
        /finish[\s\S]{0,200}RENEW_SECRET/.test(src), true);
  check('renderResult موجودة', /async function renderResult/.test(src), true);
  const selfCalls = (src.match(/fetch\(\s*env\.\w*WORKER/g) || []).length;
  check('مفيش fetch لعنوان الـWorker', selfCalls, 0);
}
console.log(FAILED === 0 ? '\nALL PASS' : `\n${FAILED} FAILED`);
process.exit(FAILED === 0 ? 0 : 1);
