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
async function fetchJourney(barcode, token, env, budgetMs = 25000) {
  const base = (env?.RENEWER_URL || '').replace(/\/+$/, '');
  if (!base || !env?.RENEW_SECRET) return { err: 'gateway-not-configured' };
  const started = Date.now();
  const send = () => fetch(base + '/track', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${env.RENEW_SECRET}`,
      'Content-Type': 'application/json',
      'User-Agent': 'egypt-post-bot/1.0',
    },
    body: JSON.stringify({ barcode: String(barcode).trim() }),
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
    // الميزانية خلصت والبوابة لسه بتجدّد — علم خاص عشان المنادي
    // يسلّم المهمة لاستدعاء جديد بدل ما الرسالة تتجمّد.
    if (e?.name === 'TimeoutError' || e?.name === 'AbortError') {
      return { err: 'timeout' };
    }
    return { err: String(e).slice(0, 60) };
  }
}

/**
 * يكمّل الطلب ويعدّل نفس رسالة تليجرام. بيتنده من مسارين:
 * من handleUpdate في الحالة العادية، ومن /resume لما الاستدعاء
 * الأول ما يلحقش في ميزانيته.
 *
 * بيعيد استعلام Turso بدل ما يمرّر الصف في جسم HTTP — الصف فيه
 * بيانات شخصية، والاستعلام رخيص.
 */
async function completeTrack(env, chatId, msgId, bc, budgetMs) {
  let row = null;
  try {
    const rows = await tursoQuery(env, 'SELECT * FROM bc WHERE code = ?', [bc]);
    row = rows[0] || null;
  } catch (e) { /* الفهرس مش متاح — نكمّل بالتتبّع الحيّ */ }
  const journey = await fetchJourney(bc, null, env, budgetMs);
  await tg(env, 'editMessageText', {
    chat_id: chatId, message_id: msgId,
    text: buildReply(bc, row, journey),
    parse_mode: 'HTML', disable_web_page_preview: true,
  });
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
  // وقت نسلّم فيه المهمة لو التجديد طوّل.
  try {
    journey = await fetchJourney(bc, token, env, TRACK_BUDGET_MS);
    if (journey.err === 'expired') {
      // التوكن اترفض وإحنا بنشتغل. نجدّد ونعيد **مرة واحدة بس** —
      // العلم ده بيمنع أي دورة إعادة لا نهائية.
      await edit(`🔍 <b>${bc}</b>\n━━━━━━━━━━━━━━━━━━━━\n🔑 بنجدّد التوكن...`);
      const fresh = await renewNow(env, notify);
      journey = fresh
        ? await fetchJourney(bc, fresh, env, TRACK_BUDGET_MS)
        : { err: 'refresh-failed' };
      // لو التاني برضه اترفض، بنوقف هنا — مفيش محاولة تالتة.
      if (journey.err === 'expired') journey = { err: 'refresh-failed' };
    }
  } catch (e) {
    journey = { err: String(e).slice(0, 60) };
  }

  // البوابة لسه بتجدّد والميزانية خلصت. بدل ما الرسالة تتجمّد،
  // بنسلّم المهمة لاستدعاء تاني بميزانية جديدة — **مرة واحدة بس**،
  // ومسار /resume مابيسلّمش تاني فمفيش أي سلسلة لا نهائية.
  // المستخدم مابيعملش حاجة: نفس الرسالة هي اللي هتتعدّل بالنتيجة.
  if (journey?.err === 'timeout' && msgId && ctx && handoffReady(env)) {
    await edit(`🔍 <b>${bc}</b>\n━━━━━━━━━━━━━━━━━━━━\n⏳ بندوّر... (بناخد وقت زيادة شوية)`);
    ctx.waitUntil(handoff(env, chatId, msgId, bc));
    return;
  }

  await edit(buildReply(bc, row, journey));
}

/** هل التسليم متظبّط؟ محتاج عنوان الـWorker نفسه وسر الإدارة. */
function handoffReady(env) {
  return !!(env.WORKER_URL && env.ADMIN_SECRET);
}

/**
 * بينده الـWorker على نفسه عشان ياخد ميزانية 30 ثانية جديدة.
 * استدعاء واحد إضافي، وبس في الحالة الباردة — مفيش cron ولا polling.
 */
async function handoff(env, chatId, msgId, bc) {
  try {
    await fetch(env.WORKER_URL.replace(/\/+$/, '') + '/resume', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${env.ADMIN_SECRET}`,
        'Content-Type': 'application/json',
        'User-Agent': 'egypt-post-bot/1.0',
      },
      body: JSON.stringify({ chat_id: chatId, message_id: msgId, barcode: bc }),
    });
  } catch (e) { /* فشل التسليم — الرسالة هتفضل على آخر حالة اتكتبت */ }
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

    // الاستدعاء الأول ما لحقش في ميزانيته، فسلّم المهمة هنا.
    // ميزانية جديدة كاملة. **مافيش تسليم تاني من هنا** — النتيجة
    // بتتكتب في نفس الرسالة أيًا كانت، فمفيش سلسلة لا نهائية.
    if (url.pathname === '/resume' && request.method === 'POST') {
      const auth = request.headers.get('Authorization') || '';
      if (auth !== `Bearer ${env.ADMIN_SECRET}`) {
        return new Response('unauthorized', { status: 401 });
      }
      const { chat_id, message_id, barcode } = await request.json();
      if (!chat_id || !message_id || !barcode) {
        return new Response('missing fields', { status: 400 });
      }
      ctx.waitUntil(
        completeTrack(env, chat_id, message_id, String(barcode), 26000)
          .catch(() => {}));
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

    return new Response('Egypt Post Tracking Bot', { status: 200 });
  },
};
