/**
 * بوت تليجرام لتتبّع شحنات البريد المصري — Cloudflare Worker
 *
 * بيرد على أي باركود بـ:
 *   1. بيانات المرسل من فهرس Turso (اسم/موبايل/رقم قومي/عنوان/خدمة)
 *   2. أيام ظهور الشحنة في ملفاتنا الأربعة
 *   3. رحلة الشحنة الحيّة من API مصر الرقمية
 *
 * لو الباركود مش في الفهرس، بيقول كده وبيجيب التتبّع الحيّ لوحده.
 * التوكن بيتخزّن في KV؛ لو خلص بيطلب من GitHub Actions يجدّده.
 */

const BC_RE = /\b[A-Z]{2,4}\d{7,}EG\b/;
const DE_API = 'https://apis.digital.gov.eg/actions';

const TYPE_NAMES = {
  R: '📤 اتطلب (REQ)',
  PO: '⏳ معلّق عند الجهة',
  PR: '📦 اتبعت للبريد (Send To Enpo)',
  T: '📥 البريد استلمه (RTP)',
};
const TYPE_ORDER = ['R', 'PO', 'PR', 'T'];

// ---------------------------------------------------------------- Turso
async function tursoQuery(env, sql, args = []) {
  const url = env.TURSO_URL.replace('libsql://', 'https://') + '/v2/pipeline';
  const body = {
    requests: [
      { type: 'execute', stmt: { sql, args: args.map((v) => ({ type: 'text', value: String(v) })) } },
      { type: 'close' },
    ],
  };
  const r = await fetch(url, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${env.TURSO_TOKEN}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`turso ${r.status}: ${await r.text()}`);
  const j = await r.json();
  const res = j.results?.[0];
  if (res?.type !== 'ok') throw new Error('turso query failed');
  const rs = res.response?.result;
  if (!rs) return [];
  const cols = rs.cols.map((c) => c.name);
  return rs.rows.map((row) => {
    const o = {};
    row.forEach((cell, i) => { o[cols[i]] = cell?.value ?? null; });
    return o;
  });
}

// ---------------------------------------------------------------- Token
function tokenPayload(tok) {
  try {
    return JSON.parse(atob(tok.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
  } catch { return null; }
}

function tokenValid(tok, marginSec = 30) {
  if (!tok) return false;
  const p = tokenPayload(tok);
  return !!(p && p.exp && p.exp - marginSec > Math.floor(Date.now() / 1000));
}

/** كام ثانية فاضلة — للتشخيص بس، مابيكشفش أي جزء من التوكن. */
function tokenExpIn(tok) {
  const p = tokenPayload(tok);
  if (!p || !p.exp) return null;
  return Math.max(0, p.exp - Math.floor(Date.now() / 1000));
}

// مفاتيح KV — أقل حاجة ممكنة. عمر التوكن جوّه الـJWT نفسه،
// فمافيش داعي نخزّن metadata منفصلة تروح تبقى متعارضة معاه.
const TOKEN_KEY = 'de_token';     // التوكن نفسه (سرّي، مابيخرجش من هنا)
const LOCK_KEY = 'renew_lock';    // قفل التجديد، بينتهي لوحده

const USABLE_MARGIN = 30;         // أقل من كده مانستخدموش
const RENEW_MARGIN = 120;         // أقل من كده نبدأ نجدّد
const LOCK_TTL = 120;             // ثانية — أقل قيمة تقبلها KV هي 60
const WAIT_MAX_SEC = 100;
// أقصى انتظار لمدير التوكن على المسار الحرج. Cloudflare بتقتل
// الاستدعاء عند 30 ثانية، والتجديد البارد لوحده بياخد ~35.
const GETTOKEN_MAX_WAIT_MS = 5000;
// ميزانية النداء الأول على البوابة. أقل من حد الـ30 ثانية بهامش
// يكفي إننا نسلّم المهمة لاستدعاء تاني لو التجديد طوّل.
const TRACK_BUDGET_MS = 18000;

/**
 * قفل التجديد. KV اتساقها مؤجّل، فالقفل ده **بيقلّل** الاستدعاءات
 * ومش بيمنعها رياضيًا. الضمان الحقيقي إن دخول واحد بس بيحصل موجود في
 * خدمة التجديد نفسها (حاوية واحدة، قفل داخل العملية، single-flight).
 * طبقتين: KV بيوفّر النداءات، والخدمة بتحسم النتيجة.
 */
async function acquireLock(env) {
  try {
    if (await env.KV.get(LOCK_KEY)) return false;
    await env.KV.put(LOCK_KEY, String(Date.now()), { expirationTtl: LOCK_TTL });
    return true;
  } catch { return false; }
}

async function releaseLock(env) {
  try { await env.KV.delete(LOCK_KEY); } catch { /* بينتهي لوحده برضه */ }
}

/** يطلب من GitHub Actions يجدّد التوكن (workflow_dispatch). */
async function requestRefresh(env) {
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) return false;
  const url = `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/refresh-token.yml/dispatches`;
  const r = await fetch(url, {
    method: 'POST',
    headers: {
      'Authorization': `token ${env.GITHUB_TOKEN}`,
      'Accept': 'application/vnd.github+json',
      'User-Agent': 'egypt-post-bot',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ ref: 'main' }),
  });
  return r.ok;
}

/**
 * يستنّى التوكن يظهر في KV بعد ما نطلب التجديد.
 * بيرجّع التوكن أو null لو الوقت خلص.
 * onTick بيتنده كل 5 ثواني عشان نحدّث رسالة تليجرام.
 */
async function waitForToken(env, maxSec = 100, onTick = null) {
  const step = 3000;
  const rounds = Math.ceil((maxSec * 1000) / step);
  for (let i = 0; i < rounds; i++) {
    await new Promise((r) => setTimeout(r, step));
    const tok = await env.KV.get(TOKEN_KEY);
    if (tokenValid(tok)) return tok;
    if (onTick && i % 2 === 1) {
      try { await onTick(Math.round(((i + 1) * step) / 1000)); } catch {}
    }
  }
  return null;
}

/**
 * يطلب تجديد التوكن. الأصل: خدمة التجديد على Orkestr (متصفح حقيقي).
 * الاحتياطي: GitHub Actions القديمة — سايبينها لأنها ماتضرش، مع إن
 * التحقيق أثبت إن runners بتاعة GitHub مابتوصلش للنطاقات دي أصلاً.
 * الرد من الخدمة metadata بس — مافيهوش توكن، ومابنلوجش منه حاجة.
 */
async function triggerRenew(env) {
  const base = (env.RENEWER_URL || '').replace(/\/+$/, '');
  if (base && env.RENEW_SECRET) {
    try {
      const r = await fetch(base + '/renew', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${env.RENEW_SECRET}`,
          'Content-Type': 'application/json',
        },
        body: '{}',
      });
      if (r.ok) return true;
      // مانرجعش فورًا للاحتياطي لو الخدمة ردّت بخطأ مفهوم —
      // الاحتياطي مثبت إنه مابيوصلش، فمفيش فايدة من نداء زيادة.
      return false;
    } catch { return false; }
  }
  return requestRefresh(env);
}

/**
 * مدير التوكن — نقطة واحدة لأي طلب توكن في الـWorker.
 *
 *   صالح ومريّح           → رجّعه على طول
 *   صالح بس قرب يخلص      → رجّعه، وجدّد في الخلفية (المستخدم مايستناش)
 *   مش صالح               → جدّد واستنى، وبإطار زمني محدود
 *
 * فشل التجديد **مابيمسحش** التوكن القديم أبدًا: مافيش أي delete على
 * TOKEN_KEY في الكود كله. لو التجديد فشل والقديم لسه فيه رمق،
 * بنرجّعه — استعمال متأخّر أحسن من عطل كامل.
 */
// تجديد واحد جوّه نفس الـisolate. من غير ده، عشر طلبات متزامنة بتعدّي
// كلها من acquireLock: القفل قراية-بعدين-كتابة، والـawait بينهم بيسمح
// للتشابك، فالكل بيلاقي القفل فاضي. الاختبار المتزامن أمسك ده فعلاً.
let inflightRenew = null;

/**
 * ثلاث طبقات ضد الـlogin storm، كل واحدة بتغطّي اللي قبلها:
 *   1. الوعد ده        — بيوحّد الطلبات جوّه نفس الـisolate (محسوم)
 *   2. قفل KV          — بيقلّل التداخل بين isolates (احتمالي، اتساق مؤجّل)
 *   3. single-flight في خدمة التجديد — حاوية واحدة، قفل داخل العملية،
 *      وده الضمان النهائي إن دخول واحد بس بيحصل مهما كان اللي فوق
 */
function renewOnce(env, onWait = null) {
  if (!inflightRenew) {
    inflightRenew = (async () => {
      const holder = await acquireLock(env);
      try {
        if (holder) {
          let ok = false;
          try { ok = await triggerRenew(env); } catch { ok = false; }
          // التجديد ماقدرش يبدأ — مانستناش 100 ثانية على الفاضي
          if (!ok) return null;
        }
        // مش ماسكين القفل؟ يبقى في حد بيجدّد — نستنى نتيجته
        return await waitForToken(env, WAIT_MAX_SEC, onWait);
      } finally {
        if (holder) await releaseLock(env);
        inflightRenew = null;
      }
    })();
  }
  return inflightRenew;
}

async function getToken(env, ctx = null, onWait = null) {
  const tok = await env.KV.get(TOKEN_KEY);
  const usable = tokenValid(tok, USABLE_MARGIN);
  const comfortable = tokenValid(tok, RENEW_MARGIN);

  if (usable && comfortable) return tok;

  // لسه شغّال بس قرب يخلص — نجدّد في الخلفية ونرجّع الحالي حالًا
  if (usable) {
    if (ctx) ctx.waitUntil(renewOnce(env).catch(() => {}));
    return tok;
  }

  // مش صالح — محتاجين نستنى. آخر ملاذ: قديم لسه ماخلصش.
  const fresh = await renewOnce(env, onWait);
  return fresh || (tokenValid(tok, 0) ? tok : null);
}

/**
 * تجديد إجباري: بيتنده لما الـAPI يرد 401 على توكن شكله لسه صالح
 * (الجلسة اتلغت من الخادم مثلاً). بيتجاهل هامش الوقت ويطلب واحد جديد.
 * بيرجّع التوكن الجديد أو null — ومابيمسحش القديم في كل الأحوال.
 */
async function renewNow(env, onWait = null) {
  const before = await env.KV.get(TOKEN_KEY);
  const fresh = await renewOnce(env, onWait);
  // لازم يكون **مختلف** عن اللي اترفض، وإلا مافيش فايدة من إعادة المحاولة
  return fresh && fresh !== before ? fresh : null;
}

// ---------------------------------------------------------------- DE API
/**
 * التتبّع الحيّ — بيعدّي على بوابة Orkestr بدل ما ينادي API مصر الرقمية
 * مباشرة.
 *
 * ليه؟ القياس أثبت إن شبكة Cloudflare مش قادرة توصل الأصل المصري
 * (41.33.95.173): الطلب من هنا بياخد 522 بعد ~850 ثانية، بينما نفس
 * الطلب من Orkestr بياخد 200 ومن جهاز محلي بياخد رد في 147ms. الفرق
 * الوحيد هو الشبكة اللي الطلب طالع منها — مش الكود ولا التوكن.
 *
 * الشكل اللي بيرجع زي ما هو بالظبط ({records, status} أو {err})،
 * فـbuildReply وكل سلوك تليجرام ماتغيروش.
 *
 * التوكن مابيسافرش: البوابة هي اللي أصدرته وبتستعمله من ذاكرتها،
 * وبتتولّى إعادة المحاولة مرة واحدة لو الـAPI رفضه. الباراميتر `token`
 * باقي عشان مواضع النداء ماتتغيّرش أكتر من اللازم.
 */
async function fetchJourney(barcode, token, env, budgetMs = 25000,
                            notify = null) {
  const base = (env?.RENEWER_URL || '').replace(/\/+$/, '');
  if (!base || !env?.RENEW_SECRET) return { err: 'gateway-not-configured' };
  const started = Date.now();
  // notify اختياري. من غيره البوابة بتشتغل زي ما هي بالظبط: بتنفّذ
  // وترد وخلاص. لو موجود وطوّلت أكتر من ميزانيتنا، بتوصّل النتيجة
  // على /finish — لأن مهلتنا بتكون خلصت وقفلنا الاتصال.
  const payload = { barcode: String(barcode).trim() };
  if (notify) payload.notify = { ...notify, budget_ms: budgetMs };
  const send = () => fetch(base + '/track', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${env.RENEW_SECRET}`,
      'Content-Type': 'application/json',
      'User-Agent': 'egypt-post-bot/1.0',
    },
    body: JSON.stringify(payload),
    // ميزانية صريحة: الاستدعاء كله عنده 30 ثانية، والتجديد البارد
    // لوحده بياخد ~30. لو مالحقناش، بنرجّع timeout والمنادي بيقرر.
    signal: AbortSignal.timeout(Math.max(1000, budgetMs - (Date.now() - started))),
  });
  try {
    let r = await send();
    // البوابة بتنام لما تقعد فاضية، وأول طلب بيوصلها وهي بتصحى بياخد
    // 503. محاولة واحدة تانية بعد شوية بتعدّي الاستيقاظ — مفيش تالتة.
    if (r.status === 502 || r.status === 503 || r.status === 504) {
      await new Promise((s) => setTimeout(s, 3000));
      r = await send();
    }
    if (!r.ok) return { err: `gateway http ${r.status}` };
    const j = await r.json();
    if (j?.err) return { err: j.err };
    return { records: j?.records || [], status: j?.status || '' };
  } catch (e) {
    // الميزانية خلصت والبوابة لسه شغّالة. لو بعتنا notify، هي اللي
    // هتوصّل النتيجة على /finish — فمافيش حاجة علينا نعملها.
    if (e?.name === 'TimeoutError' || e?.name === 'AbortError') {
      return { err: 'timeout' };
    }
    return { err: String(e).slice(0, 60) };
  }
}

/**
 * بيرسم النتيجة النهائية في نفس رسالة تليجرام.
 *
 * بيتنده من /finish بس، لما البوابة تكون خلّصت شغلها بعد ما مهلتنا
 * خلصت وبعتتلنا النتيجة. مابيجيبش التتبّع تاني — النتيجة جايّة معاه،
 * فمفيش نداء زيادة على البوابة ولا على API البريد.
 *
 * بيعيد استعلام Turso بدل ما الصف يتنقل في جسم HTTP — الصف فيه
 * بيانات شخصية، والاستعلام رخيص.
 */
async function renderResult(env, chatId, msgId, bc, journey) {
  let row = null;
  try {
    const rows = await tursoQuery(env, 'SELECT * FROM bc WHERE code = ?', [bc]);
    row = rows[0] || null;
  } catch (e) { /* الفهرس مش متاح — نكمّل بالتتبّع الحيّ */ }
  let text = buildReply(bc, row, journey);

  // ---- ربط طابور SMAX — إضافة معزولة (المستخدم 2026-09-05: «رسالة واحدة») ----
  // المهمة اتعملت في مسار الـtimeout من غير نص تتبّع. هنا بنحفظه عشان نتيجة
  // الوكيل تتكتب في **نفس الرسالة**، ولو النتيجة سبقتنا بنلحقها دلوقتي.
  // ⚠️ أي فشل هنا = السلوك القديم بالحرف (تعديل الرسالة بنص التتبّع).
  let smaxTail = '';
  try {
    const jid = `${chatId}:${msgId}`;
    const jobs = await tursoQuery(env,
      `SELECT status, result, sent_at, created_at, claimed_at, completed_at, tracked_at
       FROM smax_jobs WHERE job_id=?`, [jid]);
    if (jobs.length) {
      const nowSec = Math.floor(Date.now() / 1000);
      await tursoQuery(env,
        `UPDATE smax_jobs SET tracking_text=?,
                tracked_at=CASE WHEN CAST(tracked_at AS INTEGER) > 0
                                THEN tracked_at ELSE ? END,
                track=?
         WHERE job_id=? AND (tracking_text IS NULL OR tracking_text='')`,
        [text, nowSec, trackSummary(row, journey), jid]);
      const j = { ...jobs[0], tracked_at: Number(jobs[0].tracked_at) || nowSec };
      if (j.status === 'SUCCESS' && j.result) {
        let res = null; try { res = JSON.parse(j.result); } catch {}
        smaxTail = res ? renderSmax(res) + timingLine(j, nowSec) : '';
      } else if (j.status === 'FAILED' || j.status === 'TIMEOUT') {
        smaxTail = SMAX_FAIL_TAIL + timingLine(j, nowSec);
      } else {
        smaxTail = '\n━━━━━━━━━━━━━━━━━━━━\n🔎 جاري البحث عن الشكوى في SMAX...';
      }
    }
  } catch (e) { smaxTail = ''; }

  // مهام البالون: مافيش رسالة تليجرام — النص اتحفظ في المهمة والبالون بيقراه من GET /bubble
  if (String(chatId) === 'bubble') return;
  if (smaxTail) {
    try { await sendSmaxParts(env, chatId, msgId, text + smaxTail); return; }
    catch (e) { /* نقع على التعديل العادي تحت */ }
  }
  await tg(env, 'editMessageText', {
    chat_id: chatId, message_id: msgId,
    text,
    parse_mode: 'HTML', disable_web_page_preview: true,
  });
}

const SMAX_FAIL_TAIL =
  '\n━━━━━━━━━━━━━━━━━━━━\n📋 <b>الشكوى</b>: تعذّر البحث دلوقتي — جرّب تاني بعد شوية.';

/** يكتب التتبّع + الشكوى في **نفس الرسالة**؛ لو النص عدّى حد تليجرام
 *  الباقي بيتبعت كردود متتابعة على نفس الرسالة. */
async function sendSmaxParts(env, chatId, msgId, fullText) {
  const parts = splitTg(fullText, 3900);
  const common = { chat_id: chatId, parse_mode: 'HTML', disable_web_page_preview: true };
  for (let i = 0; i < parts.length; i++) {
    await tg(env, i === 0 ? 'editMessageText' : 'sendMessage', i === 0
      ? { ...common, message_id: Number(msgId), text: parts[i] }
      : { ...common, reply_to_message_id: Number(msgId), text: parts[i] });
  }
}

// ---------------------------------------------------------------- Format
function fmtDays(v) {
  if (!v) return '';
  const [first, last, n] = String(v).split('|');
  if (first === last) return first;
  return `${first} ← ${last}  (${n} يوم)`;
}

function buildReply(bc, row, journey) {
  const L = [];
  L.push(`🔍 <b>${bc}</b>`);
  L.push('━━━━━━━━━━━━━━━━━━━━');

  if (row) {
    L.push('👤 <b>بيانات الطلب</b>');
    if (row.n) L.push(`   الاسم: ${row.n}`);
    if (row.p) L.push(`   الموبايل: <code>${row.p}</code>`);
    if (row.nid) L.push(`   الرقم القومي: <code>${row.nid}</code>`);
    if (row.s) L.push(`   الخدمة: ${row.s}`);
    if (row.gf || row.gt) L.push(`   المحافظة: ${row.gf || '?'} ← ${row.gt || '?'}`);
    if (row.r) L.push(`   تاريخ الطلب: ${row.r}`);
    if (row.a) L.push(`   العنوان: ${row.a}`);

    let files = {};
    try { files = JSON.parse(row.f || '{}'); } catch {}
    const keys = TYPE_ORDER.filter((k) => files[k]);
    if (keys.length) {
      L.push('');
      L.push('📂 <b>ظهرت في ملفاتنا</b>');
      for (const k of keys) L.push(`   ${TYPE_NAMES[k]}: ${fmtDays(files[k])}`);
    }
  } else {
    L.push('ℹ️ مش موجودة في ملفاتنا المحلية.');
  }

  L.push('');
  if (journey?.err === 'refresh-failed' || journey?.err === 'no-token') {
    L.push('🌐 <b>التتبّع الحيّ</b>: تعذّر تجديد التوكن.');
    L.push('   <i>جرّب تاني بعد شوية — أو شوف GitHub Actions.</i>');
  } else if (journey?.err) {
    L.push(`🌐 <b>التتبّع الحيّ</b>: مش متاح (${journey.err})`);
  } else if (!journey?.records?.length) {
    L.push('🌐 <b>التتبّع الحيّ</b>: البريد مالوش سجل للباركود ده.');
  } else {
    const recs = journey.records;
    L.push(`🌐 <b>رحلة الشحنة</b> — ${recs.length} حالة`);
    if (journey.status) L.push(`   ◀ <b>آخر حالة: ${journey.status}</b>`);
    L.push('');
    for (const r of recs.slice(0, 15)) {
      const when = (r.EventDateAndTime || '').trim();
      const st = (r.ItemStatus || '').trim();
      const loc = [(r.Location || '').trim(), (r.City || '').trim()]
        .filter(Boolean).join(' — ');
      L.push(`   ${when}`);
      L.push(`   ${st}${loc ? ` (${loc})` : ''}`);
    }
    if (recs.length > 15) L.push(`   … و${recs.length - 15} حالة أقدم`);
  }
  return L.join('\n');
}

// ------------------------------------------------------- SMAX Agent (P1)
/**
 * وكيل بوابة الشكاوى — المرحلة 1: نبض وحالة فقط.
 *
 * ⚠️ مالوش أي علاقة بالتتبّع. لو الوكيل مقفول، التتبّع بيشتغل زي ما هو
 *    بالحرف — المسارات دي منفصلة تمامًا عن /track و/finish.
 *
 * الوكيل بيبعت **حقائق** بس (شغّال / حالة SMAX). **الـWorker هو اللي
 * بيقرر** ONLINE أو OFFLINE — لأن الوكيل لو مقفول مش هيقدر يقول عن
 * نفسه إنه مقفول. مصدر الحقيقة هنا، مش هناك.
 */
const AGENT_OFFLINE_SEC = 90;

// قيم مغلقة — مش أي نص. أي حاجة بره دي بتترفض بـ400.
const AGENT_SMAX_STATUS = new Set(
  ['ready', 'not_configured', 'starting', 'error', 'unknown']);
const AGENT_RUN_STATUS = new Set(['running', 'starting', 'stopping']);
const AGENT_FIELDS = new Set(
  ['agent_id', 'agent_status', 'smax_status', 'version', 'host_os']);
const AGENT_MAX_BODY = 1024;      // بايت — النبضة الشرعية ~150
const AGENT_MAX_FIELD = 32;

/**
 * تحقق صارم من جسم النبضة. بيرجّع سبب الرفض أو null لو سليم.
 *
 * ⚠️ مافيش metadata عشوائية: أي مفتاح بره AGENT_FIELDS بيترفض.
 *    ده بيمنع إن الوكيل (أو حد ماسك السر) يحقن حقول في الجدول.
 */
function validateHeartbeat(a) {
  if (!a || typeof a !== 'object' || Array.isArray(a)) return 'bad_payload';
  for (const k of Object.keys(a)) {
    if (!AGENT_FIELDS.has(k)) return 'unexpected_field';
  }
  const id = a.agent_id;
  if (typeof id !== 'string' || id.length === 0 || id.length > 64) {
    return 'bad_agent_id';
  }
  if (!/^[A-Za-z0-9._-]+$/.test(id)) return 'bad_agent_id';
  if (typeof a.smax_status !== 'string'
      || !AGENT_SMAX_STATUS.has(a.smax_status)) {
    return 'bad_smax_status';
  }
  if (a.agent_status !== undefined
      && (typeof a.agent_status !== 'string'
          || !AGENT_RUN_STATUS.has(a.agent_status))) {
    return 'bad_agent_status';
  }
  for (const k of ['version', 'host_os']) {
    const v = a[k];
    if (v === undefined) continue;
    if (typeof v !== 'string' || v.length > AGENT_MAX_FIELD) return 'bad_' + k;
  }
  return null;
}

/**
 * ⚠️ `agent_status` الجاي من الوكيل **مابيدخلش الحساب ده إطلاقًا**.
 *    بيتخزّن كحقيقة مُبلَّغة للتشخيص وبس. الحالة بتتحسب من:
 *        last_seen  +  smax_status  +  AGENT_OFFLINE_SEC
 *    لأن وكيل مقفول مش هيقدر يبلّغ إنه مقفول — فالإبلاغ مش مصدر ثقة.
 */
function computeAgentStatus(row, nowSec, offlineSec = AGENT_OFFLINE_SEC) {
  if (!row) {
    return { code: 'OFFLINE', icon: '🔴', reason: 'never_seen', age_sec: null };
  }
  const last = Number(row.last_seen) || 0;
  const age = nowSec - last;
  if (age > offlineSec) {
    return { code: 'OFFLINE', icon: '🔴', reason: 'stale_heartbeat', age_sec: age };
  }
  if ((row.smax_status || '') !== 'ready') {
    return {
      code: 'ONLINE_SMAX_NOT_READY', icon: '🟡',
      reason: row.smax_status || 'unknown', age_sec: age,
    };
  }
  return { code: 'ONLINE', icon: '🟢', reason: 'ok', age_sec: age };
}

/** نفس المعرّف بيتحدّث — إعادة تشغيل الوكيل مابتعملش صف تاني. */
async function upsertAgent(env, a, nowSec) {
  await tursoQuery(env,
    `INSERT INTO agent_state
       (agent_id, last_seen, agent_status, smax_status, version, host_os, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT(agent_id) DO UPDATE SET
       last_seen = excluded.last_seen,
       agent_status = excluded.agent_status,
       smax_status = excluded.smax_status,
       version = excluded.version,
       host_os = excluded.host_os,
       updated_at = excluded.updated_at`,
    [a.agent_id, nowSec, a.agent_status || 'running',
     a.smax_status, a.version || '', a.host_os || '', nowSec]);
}

// -------------------------------------------------- SMAX Jobs (Phase 2)
/**
 * طابور مهام SMAX. معزول تمامًا عن التتبّع:
 *   • لو الوكيل مقفول، التتبّع بيشتغل زي ما هو ويرد عادي
 *   • لو الطابور وقع، رسالة التتبّع بتفضل زي ما هي
 *
 * ⚠️ مافيش انتظار للأبد: كل مهمة ليها lease (لو الوكيل مات) و
 *    expires_at (مهلة نهائية) و attempts (سقف محاولات).
 */
const JOB_LEASE_SEC = 180;        // الوكيل لازم يخلص فيها أو المهمة ترجع
// 12 ساعة (كانت ساعة): لو الجهاز مقفول والمستخدم بعت الصبح، المهمة تستنى لحد
// ما الوكيل يقوم مع تسجيل الدخول بدل ما تنتهي بـ«تعذّر البحث» (المستخدم 2026-09-06).
const JOB_EXPIRY_SEC = 12 * 3600;      // ساعة — بعدها TIMEOUT نهائي
const JOB_MAX_ATTEMPTS = 3;

function newCorrId() {
  const d = new Date();
  const ymd = d.toISOString().slice(0, 10).replace(/-/g, '');
  const rnd = Math.random().toString(36).slice(2, 8).toUpperCase();
  return `REQ-${ymd}-${rnd}`;
}

/** إنشاء مهمة — حتمي بالمفتاح، فتكرار webhook مابيعملش صف تاني. */
async function createSmaxJob(env, j) {
  const now = Math.floor(Date.now() / 1000);
  const jobId = `${j.chat_id}:${j.message_id}`;
  await tursoQuery(env,
    `INSERT INTO smax_jobs
       (job_id, corr_id, chat_id, message_id, barcode, national_id,
        status, attempts, created_at, expires_at, tracking_text, sent_at, tracked_at, track)
     VALUES (?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, ?, ?, ?, ?, ?)
     ON CONFLICT(job_id) DO NOTHING`,
    [jobId, j.corr_id, String(j.chat_id), String(j.message_id), j.barcode,
     j.national_id || '', now, now + JOB_EXPIRY_SEC, j.tracking_text || '',
     // ⚠️ tursoQuery بيبعت الوسائط كنص — null بيتحوّل لكلمة "null". بنستخدم 0
     //    كـ«لسه» وrenderResult بيملاه لما التتبّع يوصل.
     j.sent_at || now, j.tracking_text ? now : 0, j.track || '']);
  return jobId;
}

/** ملخص التتبّع اللي محتاجه تحليل الشكوى على الوكيل (المرحلة 3):
 *  آخر حالة وقبل الأخيرة (بتاريخهم) · تاريخ استلام الشحنة من الجهة · تاريخ الطلب.
 *  بيتخزّن كـJSON في smax_jobs.track — من غير أي بيانات شخصية. */
function trackSummary(row, journey) {
  const recs = journey?.records || [];
  const line = (r) => r ? `${(r.ItemStatus || '').trim()} ${(r.EventDateAndTime || '').trim()}`.trim() : '';
  const received = recs.find((r) => /استلام الشحن[هة] من الجه[هة]/.test(r.ItemStatus || ''));
  const out = {
    last_status: line(recs[0]),
    before_last_status: line(recs[1]),
    received_date: received ? (received.EventDateAndTime || '').trim() : '',
    request_date: (row?.r || '').trim(),
  };
  return JSON.stringify(out);
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/** يقسم نص طويل على رسائل تليجرام (حد 4096) عند حدود الأسطر — الوسوم
 *  HTML في renderSmax كلها جوه سطر واحد فالقطع بين الأسطر آمن. */
function splitTg(text, max = 3900) {
  const parts = [];
  let cur = '';
  for (const line of String(text).split('\n')) {
    const ln = line.length > max ? line.slice(0, max) : line;
    if (cur && cur.length + 1 + ln.length > max) { parts.push(cur); cur = ln; }
    else cur = cur ? cur + '\n' + ln : ln;
  }
  if (cur) parts.push(cur);
  return parts.length ? parts : [''];
}

// كلمات كاملة (دقيقة/ثانية) بدل «د/ث» — الحروف المفردة جنب الأرقام واللاتيني
// كانت بتتلخبط في اتجاه النص على تليجرام (ملاحظة المستخدم 2026-09-05).
function fmtSec(s) {
  s = Math.max(0, Math.round(Number(s) || 0));
  if (s < 60) return `${s} ثانية`;
  const m = Math.floor(s / 60), r = s % 60;
  return r ? `${m} دقيقة و${r} ثانية` : `${m} دقيقة`;
}

/** التوقيت تحت الشكوى (صيغة المستخدم 2026-09-05) — 3 أسطر، كل رقم في
 *  آخر سطره عشان اتجاه النص يفضل سليم:
 *    ✅ تمت العملية في مدة: X
 *       التتبّع من الملفات والتتبّع الحي استغرق: Y
 *       البحث في SMAX فقط استغرق: Z */
function timingLine(j, nowSec) {
  const sent = Number(j?.sent_at) || 0;
  if (!sent) return '';
  const tracked = Number(j?.tracked_at) || 0;
  const claimed = Number(j?.claimed_at) || 0;
  const done = Number(j?.completed_at) || nowSec;
  const total = nowSec - sent;
  const L = ['', `✅ <b>تمت العملية في مدة: ${fmtSec(total)}</b>`];
  let accounted = 0;
  if (tracked >= sent) { L.push(`   📦 التتبّع من الملفات والتتبّع الحي استغرق: ${fmtSec(tracked - sent)}`); accounted += tracked - sent; }
  if (claimed && done >= claimed) { L.push(`   🔎 البحث في الشكاوى (SMAX) فقط استغرق: ${fmtSec(done - claimed)}`); accounted += done - claimed; }
  // انتظار الوكيل = من اكتمال التتبّع لحد ما الوكيل لقط المهمة. في مسار
  // الـtimeout الوكيل بيبدأ **قبل** ما التتبّع يخلص (بيشتغلوا بالتوازي) فالانتظار
  // صفر والأرقام مابتجمعش على الإجمالي — بنقولها صراحة (المستخدم 2026-09-06).
  if (claimed && tracked) {
    const wait = claimed - tracked;
    if (wait > 0) L.push(`   ⏳ انتظار الوكيل في الطابور: ${fmtSec(wait)}`);
    else if (wait < -3) L.push(`   ℹ️ <i>البحث بدأ قبل ما التتبّع يخلص (بالتوازي) — فالأرقام مش بتتجمع</i>`);
  }
  const deliver = nowSec - done;
  if (deliver > 3) L.push(`   📬 تسليم النتيجة: ${fmtSec(deliver)}`);
  return L.join('\n');
}

/** /health — حالة الوكيل + إحصاء مهام SMAX (النهاردة · 7 أيام · 30 يوم). */
async function healthText(env) {
  const now = Math.floor(Date.now() / 1000);
  const L = ['🩺 <b>حالة النظام</b>', '━━━━━━━━━━━━━━━━━━━━'];
  const agents = await tursoQuery(env,
    'SELECT * FROM agent_state ORDER BY last_seen DESC LIMIT 3');
  if (!agents.length) L.push('🔴 الوكيل: مافيش نبضات خالص');
  for (const a of agents) {
    const s = computeAgentStatus(a, now);
    const age = s.age_sec == null ? '' : ` — آخر نبضة من ${fmtSec(s.age_sec)}`;
    L.push(`${s.icon} الوكيل <code>${esc(a.agent_id)}</code>: ${s.code}${age}`);
    L.push(`   SMAX: ${esc(a.smax_status || '?')} · الإصدار ${esc(a.version || '?')}`);
  }
  const DAY = 86400;
  const stats = async (since) => (await tursoQuery(env,
    `SELECT COUNT(*) n,
            SUM(status='SUCCESS') ok,
            SUM(status IN ('FAILED','TIMEOUT')) bad,
            SUM(status IN ('PENDING','CLAIMED','RUNNING')) open,
            SUM(status='SUCCESS' AND result LIKE '%"found":true%') found,
            AVG(CASE WHEN status='SUCCESS' AND sent_at IS NOT NULL
                     THEN completed_at - sent_at END) avg_total,
            AVG(CASE WHEN status='SUCCESS' AND claimed_at IS NOT NULL
                     THEN completed_at - claimed_at END) avg_search
     FROM smax_jobs WHERE created_at >= ?`, [since]))[0] || {};
  const n = (v) => Number(v) || 0;
  L.push('');
  L.push('📊 <b>مهام الشكاوى</b>');
  for (const [label, since] of [['آخر 24 ساعة', now - DAY],
                                ['آخر 7 أيام', now - 7 * DAY],
                                ['آخر 30 يوم', now - 30 * DAY]]) {
    const s = await stats(since);
    const ok = n(s.ok), found = n(s.found);
    L.push(`▪️ <b>${label}</b>: ${n(s.n)} طلب`);
    L.push(`   اتعمل منهم: ${ok} — لقينا ${found} شكوى · مالقيناش شكوى لـ${ok - found}`);
    L.push(`   فشل: ${n(s.bad)} · <b>متبقي في الطابور: ${n(s.open)}</b>`);
    // متوسط الإجمالي بيتحسب بس للطلبات اللي عندها وقت إرسال (الجديدة)
    if (ok && Number(s.avg_total)) L.push(`   متوسط المدة الكلية من الإرسال: ${fmtSec(s.avg_total)}`);
    if (ok && Number(s.avg_search)) L.push(`   متوسط البحث في الشكاوى فقط: ${fmtSec(s.avg_search)}`);
  }
  const open = await tursoQuery(env,
    `SELECT barcode, status, created_at FROM smax_jobs
     WHERE status IN ('PENDING','CLAIMED','RUNNING') ORDER BY created_at LIMIT 5`);
  if (open.length) {
    L.push('');
    L.push('⏳ <b>في الطابور دلوقتي</b>');
    for (const o of open) L.push(`   ${esc(o.barcode)} — ${esc(o.status)} من ${fmtSec(now - Number(o.created_at))}`);
  }
  return L.join('\n');
}

/** يرسم قسم الشكوى تحت نص التتبّع. بيقص عشان حد تليجرام 4096. */
function renderSmax(res) {
  // الترتيب المعتمد (المستخدم 2026-09-05):
  //   1) ID رقم الشكوى  2) Creation Time  3) Current assignment group
  //   4) آخر تعليق لوحده  5) المناقشات من الأقدم للأحدث مرقّمة
  if (!res) return '';
  const L = ['', '━━━━━━━━━━━━━━━━━━━━'];
  if (res.found === false) {
    L.push('📋 <b>الشكوى</b>: مالقيناش شكوى في SMAX.');
    if (res.tried) L.push(`   <i>جرّبنا: ${esc(res.tried)}</i>`);
    return L.join('\n');
  }
  L.push('📋 <b>بيانات الشكوى</b>');
  // قرار المستخدم 2026-09-06: لو مالقيناش بـActive=Yes بنبحث بـActive=No
  // ونقول صراحة إنها مقفولة.
  if (res.closed) L.push('   ⚠️ <b>الشكوى مغلقة</b> — اتلقت بعد تغيير الفلتر Active إلى No');
  if (res.id) L.push(`   1️⃣ رقم الشكوى: <code>${esc(res.id)}</code>`);
  if (res.creation_time) L.push(`   2️⃣ تاريخ الإنشاء: ${esc(res.creation_time)}`);
  if (res.assignment_group) L.push(`   3️⃣ الجهة: ${esc(res.assignment_group)}`);
  // وصف الشكوى (Description) — رجع بطلب المستخدم 2026-09-06
  // نص حقل Description من صفحة الشكوى في SMAX كما هو (مش وصف متولّد) —
  // التسمية بالإنجليزي زي SMAX عشان مايتلخبطش مع أي وصف تاني (المدير 2026-09-06)
  if (res.description) {
    // نفس شكل الحقل في SMAX: العنوان ثم النص سطر سطر (المدير 2026-09-06)
    L.push('   📝 Description (نص الشكوى):');
    for (const ln of String(res.description).slice(0, 700).split(/\n+/)) {
      if (ln.trim()) L.push(`      ${esc(ln.trim())}`);
    }
  }
  if (res.found_by) L.push(`   <i>اتلقت بـ${esc(res.found_by)}</i>`);

  // ---- المرحلة 3: كارت التحليل (المستخدم 2026-09-06) ----
  // التصنيف · التأخير · رد مدير المشروع · أدلة التأكيد والنفي — بيتحسب على
  // الوكيل بأدوات Smax_V11. لو مش موجود (فشل/مش متاح) الكارت مابيظهرش.
  const an = res.analysis;
  if (an && typeof an === 'object') {
    const SRC = { Discussion: 'المناقشات', Description: 'الوصف', 'Last Comment': 'آخر تعليق',
                  'Shipment Last Status': 'آخر حالة شحنة', 'Before Last Status': 'الحالة قبل الأخيرة' };
    L.push('');
    L.push('━━━━━━━━━━━━━━━━━━━━');
    L.push('🧠 <b>تحليل الشكوى</b>');
    if (an.issue_type) {
      const src = an.class_source && an.class_source !== 'تلقائي' ? ` <i>(${esc(an.class_source)})</i>` : '';
      L.push(`   🏷 التصنيف: <b>${esc(an.issue_type)}</b>${src}`);
    }
    const basis = an.delay_basis === 'received' ? ' <i>(من استلام الجهة)</i>'
      : an.delay_basis === 'request' ? ' <i>(من تاريخ الطلب)</i>' : '';
    L.push(`   ⏱ التأخير: <b>${Number(an.delay_days) || 0} يوم</b>${basis}`);
    const rp = an.reply || {};
    if (rp.state === 'sent' && rp.source === 'discussions') {
      // المصدر الأساسي: تعليقات مدير المشروع نفسه في المناقشات — كلها،
      // من الأقدم للأحدث مع تاريخ ورقم كل واحد (المستخدم 2026-09-06)
      const items = Array.isArray(rp.items) && rp.items.length ? rp.items : [{ when: rp.when, index: rp.index }];
      if (items.length === 1) {
        L.push(`   📨 رد مدير المشروع: ✅ فيها رد — ${esc(items[0].when)}${items[0].index ? ` <i>(تعليق رقم ${items[0].index})</i>` : ''}`);
      } else {
        const ORD = ['الأول', 'التاني', 'التالت', 'الرابع', 'الخامس', 'السادس'];
        L.push(`   📨 رد مدير المشروع: ✅ فيها ${items.length} ردود`);
        items.forEach((it, i) => L.push(`      ${ORD[i] || (i + 1)}: ${esc(it.when)}${it.index ? ` <i>(تعليق رقم ${it.index})</i>` : ''}`));
      }
    } else if (rp.state === 'sent') {
      L.push(`   📨 رد مدير المشروع: ✅ مسجّل في الأداة بتاريخ ${esc(rp.when)} <i>(مش لاقي تعليقك في المناقشات)</i>`);
    } else if (rp.state === 'drafted') {
      L.push('   📨 رد مدير المشروع: ✎ فيه رد متكتوب في الأداة — لسه مااتبعتش');
    } else if (rp.state === 'none') {
      L.push('   📨 رد مدير المشروع: ❌ مافيش رد');
    } else {
      L.push('   📨 رد مدير المشروع: غير معروف');
    }
    // الأربعة بيظهروا دايمًا — الفاضي «مافيش». الأدلة مجمّعة بالمصدر: المصدر
    // عنوان وتحته أدلته، بترتيب مصادر ثابت زي شاشة Smax_V11 (المستخدم 2026-09-06)
    const ORDER_ALL = ['Shipment Last Status', 'Last Comment', 'Discussion', 'Description'];
    const ORDER_DENIAL = ['Last Comment', 'Discussion', 'Description'];
    const evBy = (by, legacy, icon, title, order) => {
      let map = by && typeof by === 'object' ? by : null;
      if (!map && Array.isArray(legacy)) {          // توافق مع نتائج قديمة
        map = {};
        for (const [p, s] of legacy) for (const src of (s || [])) (map[src] = map[src] || []).push(p);
      }
      const srcs = order.filter((s) => map && Array.isArray(map[s]) && map[s].length);
      for (const s of Object.keys(map || {})) if (!order.includes(s) && map[s].length) srcs.push(s);
      if (!srcs.length) { L.push(`   ${icon} <b>${title}</b>: مافيش`); return; }
      L.push(`   ${icon} <b>${title}</b>`);
      for (const s of srcs) {
        L.push(`      <u>${esc(SRC[s] || s)}</u>`);
        for (const p of map[s].slice(0, 3)) L.push(`      • ${esc(String(p).slice(0, 180))}`);
      }
    };
    if (an.reason) L.push(`   💡 <i>${esc(an.reason)}</i>`);
    if (an.review_text) {
      // نص عرض Smax_V11 نفسه (build_review_evidence) — بالحرف، مع تنسيق خفيف:
      // عناوين الأقسام عريضة، وأسماء المصادر بالعربي بخط تحته خط، والأدلة كنقاط.
      for (const raw of String(an.review_text).split('\n')) {
        const ln = raw.trimEnd();
        if (!ln.trim()) { L.push(''); continue; }
        if (/^[✅❌⚠️📄📦]/.test(ln)) { L.push(`   <b>${esc(ln.replace(/:$/, ''))}</b>`); continue; }
        const m = ln.match(/^([A-Za-z][A-Za-z ]+):\s*(.*)$/);
        if (m) {
          const name = m[1].trim();   // أسماء المصادر بالإنجليزي زي Smax_V11 بالظبط
          L.push(m[2] ? `      <u>${esc(name)}</u>: ${esc(m[2])}` : `      <u>${esc(name)}</u>`);
          continue;
        }
        if (ln.startsWith('- ')) { L.push(`      • ${esc(ln.slice(2).slice(0, 220))}`); continue; }
        L.push(`      ${esc(ln)}`);
      }
    } else {
      evBy(an.confirm_by, an.confirm, '✅', 'أدلة التأكيد', ORDER_ALL);
      evBy(an.denial_by, an.denial, '❌', 'أدلة النفي', ORDER_DENIAL);
      evBy(an.procedure_by, an.procedure, '🛠', 'أدلة الإجراءات', ORDER_ALL);
      evBy(an.partial_by, an.partial, '⏳', 'أدلة التعثر/التأخير', ORDER_ALL);
      if ((an.loss_by && Object.keys(an.loss_by).length) || (Array.isArray(an.loss) && an.loss.length)) {
        evBy(an.loss_by, an.loss, '📦', 'أدلة الفقد', ORDER_ALL);
      }
    }
  }
  const lc = res.last_comment;
  if (lc) {
    L.push('');
    L.push('4️⃣ 💬 <b>آخر تعليق</b>');
    L.push(`   <i>${esc(lc.when || '')}${lc.author ? ' — ' + esc(lc.author) : ''}</i>`);
    L.push(esc(String(lc.text || '').slice(0, 500)));
  }
  const cm = res.comments || [];
  if (cm.length) {
    L.push('');
    L.push(`5️⃣ 🗨 <b>المناقشات</b> (${cm.length}) — من الأقدم للأحدث`);
    // كل التعليقات بتتبعت (قرار المستخدم 2026-09-05) — اللي مايساعش
    // رسالة واحدة بيتكمّل في رسائل تالية (splitTg في معالج النتيجة).
    // الوسوم HTML كلها جوه السطر الواحد عشان التقسيم على الأسطر يفضل آمن.
    for (let i = 0; i < cm.length; i++) {
      const c = cm[i];
      L.push(`   <b>${i + 1}.</b> <i>${esc(c.when || '')}${c.author ? ' — ' + esc(c.author) : ''}</i>`);
      L.push(esc(String(c.text || '').slice(0, 1500)));
    }
  } else if (lc) {
    L.push('');
    L.push('5️⃣ 🗨 <b>المناقشات</b>: مافيش تعليقات تانية.');
  }
  return L.join('\n');
}

// ---------------------------------------------------------------- Telegram
async function tg(env, method, payload) {
  const url = `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`;
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

const HELP = [
  '👋 أهلاً! ابعتلي رقم شحنة وأنا أجيبلك كل بياناتها.',
  '',
  'مثال: <code>EKNA0567911EG</code>',
  '',
  'هجيبلك:',
  '  👤 بيانات المرسل (اسم/موبايل/رقم قومي/عنوان)',
  '  📂 ظهرت في أنهي ملف وامتى',
  '  🌐 رحلة الشحنة الحيّة من البريد المصري',
].join('\n');

async function handleUpdate(env, update, ctx) {
  const msg = update.message || update.edited_message;
  if (!msg?.chat?.id) return;
  const chatId = msg.chat.id;
  const text = (msg.text || '').trim();

  if (!text) return;
  if (text === '/start' || text === '/help') {
    await tg(env, 'sendMessage', { chat_id: chatId, text: HELP, parse_mode: 'HTML' });
    return;
  }
  // /health — حالة الوكيل وإحصاء مهام SMAX (طلب المستخدم 2026-09-05)
  if (text === '/health') {
    let out;
    try { out = await healthText(env); }
    catch (e) { out = '⚠️ تعذّر قراءة الحالة دلوقتي.'; }
    await tg(env, 'sendMessage', { chat_id: chatId, text: out, parse_mode: 'HTML' });
    return;
  }
  // وقت إرسال رسالة المستخدم — لحساب الزمن الكلي لحد وصول الشكوى
  const sentAt = Number(msg.date) || Math.floor(Date.now() / 1000);

  const m = text.toUpperCase().match(BC_RE);
  if (!m) {
    await tg(env, 'sendMessage', {
      chat_id: chatId,
      text: '❌ مالقيتش رقم شحنة في رسالتك.\nالشكل الصح: <code>EKNA0567911EG</code>',
      parse_mode: 'HTML',
    });
    return;
  }
  const bc = m[0];
  // أكتر من رقم شحنة في رسالة واحدة (المستخدم 2026-09-06): بنشتغل على الأول
  // وبنقول له يبعت الباقي كل رقم في رسالة — كل رسالة بتاخد طابورها لوحدها،
  // ومعالجتهم كلهم في طلب واحد هتعدّي حد وقت الـWorker.
  const extra = [...new Set((text.toUpperCase().match(new RegExp(BC_RE.source, 'g')) || []))]
    .filter((x) => x !== bc);
  if (extra.length) {
    await tg(env, 'sendMessage', {
      chat_id: chatId, parse_mode: 'HTML',
      text: `ℹ️ لقيت ${extra.length + 1} أرقام في رسالتك — هبدأ بـ<code>${bc}</code>.\n`
        + `ابعت الباقي كل رقم في رسالة لوحده:\n` + extra.map((x) => `<code>${x}</code>`).join('\n'),
    });
  }

  // نبعت رسالة انتظار فورًا، وبعدين نعدّلها بالنتيجة —
  // زي بالون التتبّع على الكمبيوتر بالظبط.
  const wait = await tg(env, 'sendMessage', {
    chat_id: chatId,
    text: `🔍 <b>${bc}</b>\n━━━━━━━━━━━━━━━━━━━━\n⏳ بندوّر...`,
    parse_mode: 'HTML',
  });
  let msgId = null;
  try { msgId = (await wait.json())?.result?.message_id; } catch {}

  const edit = (text) => msgId
    ? tg(env, 'editMessageText', {
        chat_id: chatId, message_id: msgId, text,
        parse_mode: 'HTML', disable_web_page_preview: true,
      })
    : tg(env, 'sendMessage', {
        chat_id: chatId, text, parse_mode: 'HTML',
        disable_web_page_preview: true,
      });

  // 1) الفهرس المحلي
  let row = null;
  try {
    const rows = await tursoQuery(env, 'SELECT * FROM bc WHERE code = ?', [bc]);
    row = rows[0] || null;
  } catch (e) { /* الفهرس مش متاح — نكمّل بالتتبّع الحيّ */ }

  // 2) التتبّع الحيّ — مدير التوكن بيتصرّف: صالح يرجّعه، قرب يخلص
  //    يجدّد في الخلفية، خلص يجدّد ويستنّى وإحنا بنحدّث نفس الرسالة.
  let journey = null;
  const notify = (s) =>
    edit(`🔍 <b>${bc}</b>\n━━━━━━━━━━━━━━━━━━━━\n🔑 بنجدّد التوكن... ${s}ث`);

  // مدير التوكن بيفضل شغّال زي ما هو بالحرف — بس بحد زمني على
  // الانتظار. في الحالة الدافية بيرجّع في أقل من عُشر ثانية فالحد
  // مابيتفعّلش أصلاً والسلوك مطابق. في الحالة الباردة كان بيقعد ~35
  // ثانية فالاستدعاء يتقتل عند 30 (حد Cloudflare) والرسالة تتجمّد
  // عند «بندوّر...».
  //
  // والانتظار ده مالوش فايدة: البوابة بتملك توكنها وبتجدّده بنفسها،
  // وfetchJourney بيتجاهل التوكن اللي بيرجع من هنا. Promise.race
  // مابيلغيش الوعد الخاسر — التجديد بيكمّل في الخلفية ويحدّث KV زي
  // ما هو.
  const token = await Promise.race([
    getToken(env, ctx),
    new Promise((r) => setTimeout(() => r(null), GETTOKEN_MAX_WAIT_MS)),
  ]);

  // ميزانية النداء الأول أقل من حد الـ30 ثانية بهامش، عشان يفضل
  // وقت نكتب فيه رسالة مؤقتة لو التجديد طوّل.
  //
  // deliverTo: بنقول للبوابة فين تبعت النتيجة لو طوّلت ومهلتنا خلصت.
  // من غير msgId مافيش رسالة نعدّلها، فمابنبعتوش.
  const deliverTo = msgId ? { chat_id: chatId, message_id: msgId } : null;

  try {
    journey = await fetchJourney(bc, token, env, TRACK_BUDGET_MS, deliverTo);
    if (journey.err === 'expired') {
      // التوكن اترفض وإحنا بنشتغل. نجدّد ونعيد **مرة واحدة بس** —
      // العلم ده بيمنع أي دورة إعادة لا نهائية.
      await edit(`🔍 <b>${bc}</b>\n━━━━━━━━━━━━━━━━━━━━\n🔑 بنجدّد التوكن...`);
      const fresh = await renewNow(env, notify);
      journey = fresh
        ? await fetchJourney(bc, fresh, env, TRACK_BUDGET_MS, deliverTo)
        : { err: 'refresh-failed' };
      // لو التاني برضه اترفض، بنوقف هنا — مفيش محاولة تالتة.
      if (journey.err === 'expired') journey = { err: 'refresh-failed' };
    }
  } catch (e) {
    journey = { err: String(e).slice(0, 60) };
  }

  // البوابة لسه بتجدّد والميزانية خلصت. بدل ما الرسالة تتجمّد،
  // البوابة لسه بتشتغل ومهلتنا خلصت. مش هننده على نفسنا — ده اتجرّب
  // وCloudflare بتمنعه. بدل كده بعتنا notify مع الطلب، والبوابة هي
  // اللي هتنده علينا على /finish أول ما تخلّص، وهي تعدّل الرسالة.
  // المستخدم مابيعملش حاجة — نفس الرسالة هتتحدّث بالنتيجة.
  if (journey?.err === 'timeout' && deliverTo) {
    // البوابة هي اللي هتكتب التتبّع في الرسالة دي عن طريق /finish (مجمّد).
    // مهمة SMAX بتتعمل هنا برضه — **من غير نص تتبّع**، والوكيل لما يخلّص
    // بيرد برسالة جديدة على الرسالة دي بدل ما يعدّلها (عشان مانمسحش
    // اللي /finish كتبه). لولا كده أي شحنة تتبّعها بطيء ماكانتش هتتبحث.
    let waitTxt = `🔍 <b>${bc}</b>\n━━━━━━━━━━━━━━━━━━━━\n⏳ بندوّر... (بناخد وقت زيادة شوية)`;
    if (msgId && env.AGENT_SECRET) {
      try {
        await createSmaxJob(env, {
          corr_id: newCorrId(), chat_id: chatId, message_id: msgId, barcode: bc,
          national_id: row?.nid || '', tracking_text: '', sent_at: sentAt,
          track: trackSummary(row, null),   // التتبّع الحي بيتكمّل في renderResult
        });
        waitTxt += '\n🔎 وجاري البحث عن الشكوى في SMAX...';
      } catch (e) { /* الطابور مش متاح — التتبّع شغّال زي ما هو */ }
    }
    await edit(waitTxt);
    return;
  }

  const trackingText = buildReply(bc, row, journey);
  await edit(trackingText);

  // ---- طابور SMAX — إضافة معزولة ----
  // ⚠️ أي فشل هنا **مابيأثرش** على رد التتبّع اللي اتبعت فوق.
  //    الرسالة بتفضل زي ما هي والمستخدم واخد اللي طلبه.
  if (msgId && env.AGENT_SECRET) {
    try {
      const corr = newCorrId();
      await createSmaxJob(env, {
        corr_id: corr, chat_id: chatId, message_id: msgId, barcode: bc,
        national_id: row?.nid || '', tracking_text: trackingText, sent_at: sentAt,
        track: trackSummary(row, journey),
      });
      await edit(trackingText
        + '\n━━━━━━━━━━━━━━━━━━━━\n🔎 جاري البحث عن الشكوى في SMAX...');
    } catch (e) { /* الطابور مش متاح — التتبّع اتبعت خلاص */ }
  }
}


// ---------------------------------------------------------------- Entry
export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // مراقبة — كلها أرقام وحالات، مافيهاش أي جزء من التوكن ولا أي سر
    if (url.pathname === '/health') {
      const tok = await env.KV.get(TOKEN_KEY);
      const lock = await env.KV.get(LOCK_KEY);
      const expIn = tokenExpIn(tok);
      return Response.json({
        ok: true,
        token: tokenValid(tok, USABLE_MARGIN) ? 'valid' : 'expired',
        token_exp_in_sec: expIn,
        needs_renew: expIn === null || expIn <= RENEW_MARGIN,
        renew_lock_held: !!lock,
        renewer_configured: !!(env.RENEWER_URL && env.RENEW_SECRET),
      });
    }

    // البوابة بتنده هنا لما تخلص شغل طول أكتر من مهلتنا. النتيجة
    // جاية معاها جاهزة — إحنا بنرسمها في الرسالة وبس، مفيش نداء
    // زيادة على البوابة ولا على API البريد.
    if (url.pathname === '/finish' && request.method === 'POST') {
      const auth = request.headers.get('Authorization') || '';
      if (auth !== `Bearer ${env.RENEW_SECRET}`) {
        return new Response('unauthorized', { status: 401 });
      }
      const { chat_id, message_id, barcode, result } = await request.json();
      if (!chat_id || !message_id || !barcode) {
        return new Response('missing fields', { status: 400 });
      }
      ctx.waitUntil(
        renderResult(env, chat_id, message_id, String(barcode),
                     result || { err: 'no-result' }).catch(() => {}));
      return new Response('ok');
    }

    // GitHub Actions بيحط التوكن الجديد هنا
    if (url.pathname === '/token' && request.method === 'POST') {
      const auth = request.headers.get('Authorization') || '';
      if (auth !== `Bearer ${env.ADMIN_SECRET}`) {
        return new Response('unauthorized', { status: 401 });
      }
      const { token } = await request.json();
      if (!token) return new Response('missing token', { status: 400 });
      // مانستبدلش توكن سليم بواحد تالف/منتهي. ده الحارس اللي بيخلّي
      // "فشل التجديد مايكسرش اللي شغّال" صحيح من ناحية التخزين كمان.
      if (!tokenValid(token, USABLE_MARGIN)) {
        return Response.json({ ok: false, reason: 'token_not_valid' },
                             { status: 400 });
      }
      await env.KV.put(TOKEN_KEY, token);
      return Response.json({ ok: true, exp_in: tokenExpIn(token) });
    }

    // Telegram webhook
    if (url.pathname === '/webhook' && request.method === 'POST') {
      const secret = request.headers.get('X-Telegram-Bot-Api-Secret-Token');
      if (env.WEBHOOK_SECRET && secret !== env.WEBHOOK_SECRET) {
        return new Response('forbidden', { status: 403 });
      }
      const update = await request.json();
      ctx.waitUntil(handleUpdate(env, update, ctx).catch(() => {}));
      return new Response('ok');
    }

    // ---- وكيل بوابة الشكاوى — المرحلة 1 ----
    // ⚠️ سر منفصل تمامًا. مش RENEW_SECRET ولا ADMIN_SECRET.
    //    لو AGENT_SECRET مش متظبّط، المسارات دي مقفولة بالكامل.
    if (url.pathname === '/agent/heartbeat' && request.method === 'POST') {
      const auth = request.headers.get('Authorization') || '';
      if (!env.AGENT_SECRET || auth !== `Bearer ${env.AGENT_SECRET}`) {
        return new Response('unauthorized', { status: 401 });
      }
      // حد حجم قبل أي parse — مانستهلكش ذاكرة على جسم ضخم
      let raw;
      try { raw = await request.text(); } catch { raw = ''; }
      if (raw.length > AGENT_MAX_BODY) {
        return Response.json({ ok: false, error: 'payload_too_large' },
                             { status: 400 });
      }
      let a;
      try { a = JSON.parse(raw); } catch { a = null; }
      const bad = validateHeartbeat(a);
      if (bad) return Response.json({ ok: false, error: bad }, { status: 400 });
      const nowSec = Math.floor(Date.now() / 1000);
      try {
        await upsertAgent(env, a, nowSec);
      } catch (e) {
        return Response.json({ ok: false, error: 'store_failed' }, { status: 503 });
      }
      const st = computeAgentStatus(
        { last_seen: nowSec, smax_status: a.smax_status }, nowSec);
      return Response.json({ ok: true, status: st.code, icon: st.icon,
                             offline_after_sec: AGENT_OFFLINE_SEC });
    }

    // حالة الوكلاء — للإدارة. مافيهاش أي سر ولا بيانات شخصية.
    if (url.pathname === '/agent/status' && request.method === 'GET') {
      const auth = request.headers.get('Authorization') || '';
      // ⚠️ من غير الشرط الأول، لو ADMIN_SECRET مش متظبّط بيبقى القالب
      //    `Bearer undefined` — وأي حد يبعت النص ده حرفيًا بيعدّي.
      //    المسار لازم يبقى **مقفول بالكامل** لو السر مش موجود.
      if (!env.ADMIN_SECRET || auth !== `Bearer ${env.ADMIN_SECRET}`) {
        return new Response('unauthorized', { status: 401 });
      }
      const nowSec = Math.floor(Date.now() / 1000);
      let rows = [];
      try {
        rows = await tursoQuery(env,
          'SELECT * FROM agent_state ORDER BY last_seen DESC LIMIT 10');
      } catch (e) {
        return Response.json({ ok: false, error: 'store_failed' }, { status: 503 });
      }
      return Response.json({
        ok: true,
        offline_after_sec: AGENT_OFFLINE_SEC,
        agents: rows.map((r) => {
          const st = computeAgentStatus(r, nowSec);
          return {
            agent_id: r.agent_id, status: st.code, icon: st.icon,
            reason: st.reason, age_sec: st.age_sec,
            smax_status: r.smax_status, version: r.version, host_os: r.host_os,
          };
        }),
      });
    }

    // ---- الوكيل بيطلب شغل: نبضة + استحواذ ذرّي ----
    if (url.pathname === '/agent/poll' && request.method === 'POST') {
      const auth = request.headers.get('Authorization') || '';
      if (!env.AGENT_SECRET || auth !== `Bearer ${env.AGENT_SECRET}`) {
        return new Response('unauthorized', { status: 401 });
      }
      let raw; try { raw = await request.text(); } catch { raw = ''; }
      if (raw.length > AGENT_MAX_BODY) {
        return Response.json({ ok: false, error: 'payload_too_large' }, { status: 400 });
      }
      let a; try { a = JSON.parse(raw); } catch { a = null; }
      const bad = validateHeartbeat(a);
      if (bad) return Response.json({ ok: false, error: bad }, { status: 400 });

      const now = Math.floor(Date.now() / 1000);
      try {
        await upsertAgent(env, a, now);           // الاستعلام نفسه هو النبضة

        // مهلة نهائية — مافيش مهمة بتستنى للأبد
        await tursoQuery(env,
          `UPDATE smax_jobs SET status='TIMEOUT', completed_at=?,
             error='expired before execution'
           WHERE status IN ('PENDING','CLAIMED','RUNNING') AND expires_at < ?`,
          [now, now]);
        // وكيل مات وهو ماسك مهمة؟ ترجع للطابور — بحد محاولات
        await tursoQuery(env,
          `UPDATE smax_jobs SET status='PENDING', lease_until=NULL
           WHERE status IN ('CLAIMED','RUNNING') AND lease_until < ?
             AND attempts < ?`, [now, JOB_MAX_ATTEMPTS]);
        // استنفدت المحاولات؟ فشل نهائي
        await tursoQuery(env,
          `UPDATE smax_jobs SET status='FAILED', completed_at=?,
             error='max attempts exceeded'
           WHERE status IN ('CLAIMED','RUNNING') AND lease_until < ?
             AND attempts >= ?`, [now, now, JOB_MAX_ATTEMPTS]);

        const cand = await tursoQuery(env,
          `SELECT job_id FROM smax_jobs WHERE status='PENDING'
           ORDER BY created_at ASC LIMIT 1`);
        if (!cand.length) {
          return Response.json({ ok: true, job: null, status: 'idle' });
        }
        const jid = cand[0].job_id;
        // 🔴 الاستحواذ الذرّي: الشرط status='PENDING' جوه UPDATE نفسه.
        //    وكيلين بيحاولوا في نفس اللحظة -> واحد بس بيكسب.
        await tursoQuery(env,
          `UPDATE smax_jobs SET status='CLAIMED', claimed_at=?, lease_until=?,
             attempts=attempts+1, agent_id=?
           WHERE job_id=? AND status='PENDING'`,
          [now, now + JOB_LEASE_SEC, a.agent_id, jid]);
        const got = await tursoQuery(env,
          `SELECT job_id, corr_id, barcode, national_id, attempts, track
           FROM smax_jobs WHERE job_id=? AND agent_id=? AND status='CLAIMED'`,
          [jid, a.agent_id]);
        if (!got.length) {
          return Response.json({ ok: true, job: null, status: 'lost_race' });
        }
        return Response.json({ ok: true, job: got[0], lease_sec: JOB_LEASE_SEC });
      } catch (e) {
        return Response.json({ ok: false, error: 'store_failed' }, { status: 503 });
      }
    }

    // ---- البالون على الكمبيوتر (المرحلة 4، المستخدم 2026-09-06) ----
    // نفس نص البوت بالحرف: التتبّع من نفس الكود (buildReply) + الشكوى من نفس
    // الوكيل والطابور + نفس العرض (renderSmax/timingLine). البالون مالوش أي
    // كود عرض خاص — أي تعديل في البوت بيظهر فيه تلقائيًا.
    //   POST /bubble {barcode}     -> {job_id, text}   (التتبّع + «جاري البحث»)
    //   GET  /bubble?job_id=...    -> {status, text}   (لما تخلص: النص الكامل)
    // محمي بسر الوكيل (نفس الجهاز). مافيش تليجرام هنا خالص.
    if (url.pathname === '/bubble' && request.method === 'POST') {
      const auth = request.headers.get('Authorization') || '';
      if (!env.AGENT_SECRET || auth !== `Bearer ${env.AGENT_SECRET}`) {
        return new Response('unauthorized', { status: 401 });
      }
      let body; try { body = await request.json(); } catch { body = null; }
      const m = String(body?.barcode || '').toUpperCase().match(BC_RE);
      if (!m) return Response.json({ ok: false, error: 'bad_barcode' }, { status: 400 });
      const bc = m[0];
      const sentAt = Math.floor(Date.now() / 1000);
      let row = null;
      try {
        const rows = await tursoQuery(env, 'SELECT * FROM bc WHERE code = ?', [bc]);
        row = rows[0] || null;
      } catch (e) { /* الفهرس مش متاح — نكمّل بالتتبّع الحيّ */ }
      // نفس مسار تليجرام: لو البوابة طوّلت عن الميزانية بتكمّل عن طريق /finish
      // (deliverTo) وrenderResult بيحفظ نص التتبّع في المهمة — والبالون بيقراه.
      const msgId = `b${sentAt}${Math.random().toString(36).slice(2, 7)}`;
      const deliverTo = { chat_id: 'bubble', message_id: msgId };
      let journey = null;
      try {
        const token = await Promise.race([
          getToken(env, ctx),
          new Promise((r) => setTimeout(() => r(null), GETTOKEN_MAX_WAIT_MS)),
        ]);
        journey = await fetchJourney(bc, token, env, TRACK_BUDGET_MS, deliverTo);
        if (journey.err === 'expired') {
          const fresh = await renewNow(env, () => {});
          journey = fresh ? await fetchJourney(bc, fresh, env, TRACK_BUDGET_MS, deliverTo)
                          : { err: 'refresh-failed' };
          if (journey.err === 'expired') journey = { err: 'refresh-failed' };
        }
      } catch (e) { journey = { err: String(e).slice(0, 60) }; }
      const pending = journey?.err === 'timeout';
      const trackingText = pending
        ? `🔍 <b>${bc}</b>\n━━━━━━━━━━━━━━━━━━━━\n⏳ التتبّع الحيّ لسه بيتجمّع من البوابة...`
        : buildReply(bc, row, journey);
      let jobId = null;
      try {
        jobId = await createSmaxJob(env, {
          corr_id: newCorrId(), chat_id: 'bubble', message_id: msgId, barcode: bc,
          national_id: row?.nid || '', tracking_text: pending ? '' : trackingText,
          sent_at: sentAt, track: trackSummary(row, pending ? null : journey),
        });
      } catch (e) { jobId = null; }
      const tail = jobId ? '\n━━━━━━━━━━━━━━━━━━━━\n🔎 جاري البحث عن الشكوى في SMAX...'
                         : '\n━━━━━━━━━━━━━━━━━━━━\n📋 <b>الشكوى</b>: الطابور مش متاح دلوقتي.';
      return Response.json({ ok: true, job_id: jobId, text: trackingText + tail });
    }
    if (url.pathname === '/bubble' && request.method === 'GET') {
      const auth = request.headers.get('Authorization') || '';
      if (!env.AGENT_SECRET || auth !== `Bearer ${env.AGENT_SECRET}`) {
        return new Response('unauthorized', { status: 401 });
      }
      const jid = url.searchParams.get('job_id') || '';
      if (!jid || jid.length > 80) return new Response('bad job_id', { status: 400 });
      let rows;
      try {
        rows = await tursoQuery(env,
          `SELECT status, tracking_text, result, sent_at, created_at, claimed_at,
                  completed_at, tracked_at, barcode FROM smax_jobs WHERE job_id=?`, [jid]);
      } catch (e) { return Response.json({ ok: false, error: 'store_failed' }, { status: 503 }); }
      if (!rows.length) return new Response('not found', { status: 404 });
      const j = rows[0];
      const nowSec = Math.floor(Date.now() / 1000);
      // نص التتبّع فاضي = البوابة لسه ماكمّلتش عن طريق /finish
      const head = String(j.tracking_text || '')
        || `🔍 <b>${esc(j.barcode || '')}</b>\n━━━━━━━━━━━━━━━━━━━━\n⏳ التتبّع الحيّ لسه بيتجمّع من البوابة...`;
      let tail = '';
      if (j.status === 'SUCCESS' && j.result) {
        let res = null; try { res = JSON.parse(j.result); } catch {}
        tail = (res ? renderSmax(res) : '') + timingLine(j, Number(j.completed_at) || nowSec);
      } else if (j.status === 'FAILED' || j.status === 'TIMEOUT') {
        tail = SMAX_FAIL_TAIL;
      } else {
        tail = '\n━━━━━━━━━━━━━━━━━━━━\n🔎 جاري البحث عن الشكوى في SMAX...';
      }
      return Response.json({ ok: true, status: j.status, tracking_ready: !!j.tracking_text,
                             text: head + tail });
    }

    // ---- الوكيل بيسلّم النتيجة ----
    if (url.pathname === '/agent/result' && request.method === 'POST') {
      const auth = request.headers.get('Authorization') || '';
      if (!env.AGENT_SECRET || auth !== `Bearer ${env.AGENT_SECRET}`) {
        return new Response('unauthorized', { status: 401 });
      }
      let body; try { body = await request.json(); } catch { body = null; }
      const jid = body?.job_id;
      if (!jid || typeof jid !== 'string' || jid.length > 80) {
        return new Response('bad job_id', { status: 400 });
      }
      const now = Math.floor(Date.now() / 1000);
      const ok = body.ok === true;
      let rows;
      try {
        // idempotent: مابنكتبش فوق مهمة خلصت خلاص
        rows = await tursoQuery(env,
          `SELECT status, chat_id, message_id, tracking_text, barcode,
                  sent_at, created_at, claimed_at, tracked_at
           FROM smax_jobs WHERE job_id=?`, [jid]);
        if (!rows.length) return new Response('not found', { status: 404 });
        if (['SUCCESS', 'FAILED', 'TIMEOUT'].includes(rows[0].status)) {
          return Response.json({ ok: true, note: 'already_final' });
        }
        await tursoQuery(env,
          `UPDATE smax_jobs SET status=?, completed_at=?, result=?, error=?
           WHERE job_id=? AND status IN ('CLAIMED','RUNNING','PENDING')`,
          [ok ? 'SUCCESS' : 'FAILED', now,
           ok ? JSON.stringify(body.result || {}).slice(0, 60000) : '',
           ok ? '' : String(body.error || 'unknown').slice(0, 300), jid]);
      } catch (e) {
        return Response.json({ ok: false, error: 'store_failed' }, { status: 503 });
      }
      const r0 = rows[0];
      // سطر التوقيت (طلب المستخدم): من إرسال الرسالة لحد وصول الشكوى كاملة
      const tail = (ok ? renderSmax(body.result) : SMAX_FAIL_TAIL)
        + timingLine({ ...r0, completed_at: now }, now);
      const base = String(r0.tracking_text || '');
      // مهمة من مسار الـtimeout (من غير نص تتبّع): الرسالة الأصلية ملك
      // /finish، فبنرد عليها برسالة جديدة بدل ما نكتب فوقها.
      // النص الطويل (كل المناقشات) بيتقسم على رسائل ≤ 3900 حرف —
      // الأولى تعديل/رد، والباقي ردود على نفس الرسالة الأصلية.
      // من غير نص تتبّع = مسار الـtimeout و/finish لسه ماوصلش: النتيجة
      // محفوظة في المهمة، وrenderResult هي اللي هتكتبها في **نفس الرسالة**
      // لما التتبّع يوصل (قرار المستخدم: رسالة واحدة، مش رد منفصل).
      // مهام البالون (chat_id='bubble') مش بتروح لتليجرام — البالون بيقراها من GET /bubble
      if (base && r0.chat_id !== 'bubble') {
        ctx.waitUntil(sendSmaxParts(env, r0.chat_id, r0.message_id, base + tail)
          .catch(() => {}));
      }
      return Response.json({ ok: true, delivered: !!base });
    }

    return new Response('Egypt Post Tracking Bot', { status: 200 });
  },
};
