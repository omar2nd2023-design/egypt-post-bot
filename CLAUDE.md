# منظومة التتبّع — بوت تليجرام · وكيل SMAX · البالون

تلات واجهات بتشارك نفس مسار التوكن ونفس API مصر الرقمية:

| # | الواجهة | فين | بتشتغل فين |
|---|---|---|---|
| 1 | **بوت تليجرام** | `cloud-bot/src/worker.js` | Cloudflare (24/7 — من غير الجهاز) |
| 2 | **وكيل SMAX** | `cloud-bot/smax-agent/agent.py` | الجهاز — مهمة مجدولة كل 5 دقايق |
| 3 | **البالون** | `Post Report Tool/tray_agent.py` | الجهاز — أيقونة في الـtray |

المراجع: `OPERATIONS.md` التشغيل والأسرار والمراقبة · `INVESTIGATION.md` و
`PROBLEM_SUMMARY.md` تاريخ تحقيق F5/التوكن · `RESTORE_AFTER_WINDOWS.md`
جرد ما يعيش على `E:` وما يعيش على `C:`.

---

## ده مشروع إنتاج شغّال 24/7 لناس حقيقية

البوت بيرد على مستخدمين، والوكيل بيسحب شكاوى، والاتنين بيلمسوا حساب
حكومي حقيقي. **مافيش نشر ولا تعديل على السرّيات من غير إذن صريح.**

---

## 1) ليه المعمارية دي بالذات — مش تعقيد زايد

```
تليجرام --> Cloudflare Worker --> getToken() --> KV
                    |                             ^
                    | لو التوكن خلص                |
                    v                             | التوكن الجديد
            خدمة التجديد (Orkestr) ---------------+
                    |  متصفح حقيقي، دخول طبيعي
                    v
            digital.gov.eg -> Keycloak -> توكن
                    v
            apis.digital.gov.eg/actions -> رحلة الشحنة
```

- **التوكن عمره 900 ثانية**، والـscope `fnf nid email username` **مافيهوش
  `offline_access`** — يعني **مافيش refresh token**. التجديد الوحيد اللي
  بيعدّي هو **دخول طبيعي بمتصفح**.
- **F5 Shape** بيرفض أي طلب HTTP مش من متصفح حقيقي على `login.di.gov.eg`.
- **Cloudflare Workers ماتشغّلش متصفح** — عشان كده Orkestr (حاوية Docker
  فيها Chromium، بلا كارت ائتمان، من egress مثبت إنه بيوصل).
- **Chromium بيفتح عند التجديد بس** وبيتقفل في `finally`.

أي «تبسيط» بيحاول يشيل المتصفح أو Orkestr **اتجرّب وفشل**. اقرا
`INVESTIGATION.md` قبل ما تقترح ده تاني.

---

## 2) منع تكرار الدخول — تلات طبقات، ماتشيلش واحدة

| الطبقة | فين | الضمان |
|---|---|---|
| وعد موحَّد (`inflightRenew`) | Worker | **محسوم** جوّه الـisolate الواحد |
| قفل KV (`renew_lock`, TTL 120ث) | Worker | **احتمالي** — KV اتساقها مؤجّل |
| single-flight داخل العملية | خدمة التجديد | **محسوم نهائيًا** — حاوية واحدة |

الطبقة الأولى اتضافت بعد ما اختبار متزامن مسك باج حقيقي: `acquireLock`
بيعمل قراية-بعدين-كتابة، والـ`await` بينهم بيسمح لعشر طلبات تعدّي كلها.

---

## 3) سلوك مدير التوكن — تعاقد

| الحالة | التصرّف |
|---|---|
| فاضل أكتر من 120ث | يرجّعه على طول |
| 30–120ث | يرجّعه **حالًا** + يجدّد في الخلفية (`waitUntil`) |
| أقل من 30ث أو منتهي | يجدّد ويستنى (100ث حد أقصى)، والرسالة بتتحدّث كل ~6ث |
| الـAPI رد 401 | تجديد إجباري + إعادة **واحدة بس**، ولازم التوكن يكون مختلف |
| التجديد فشل + القديم صالح | يكمّل بالقديم |
| التجديد فشل + القديم منتهي | فشل محكوم ورسالة واضحة |

**التوكن مابيتمسحش أبدًا** — مافيش `KV.delete(TOKEN_KEY)` في الكود كله.
و`POST /token` بيرفض يخزّن توكن منتهي فوق واحد سليم.

---

## 4) الأمان — قواعد صارمة

- **صفر أسرار في git.** بس مراجع لمتغيّرات بيئة.
- **مافيش console.* في الـWorker إطلاقًا** — صفر احتمال تسريب في اللوجات.
- **التوكن مابيرجعش** في أي رد HTTP، ولا في أي URL، ولا لتليجرام.
- `redact()` بيمسح JWT/Bearer/Authorization/password من أي نص جاي من الصفحة.
- **اتشالت 5 نقاط تشخيصية كانت مفتوحة للعامة**: probe · probejs · kc-probe ·
  reach · debug. نقطة debug كانت بتنادي API مصر الرقمية بتوكننا وترجّع الرد
  الخام لأي حد، وprobejs كانت **SSRF كامل**.
  **ماترجّعش نقطة تشخيصية مفتوحة.**
- الطلبات الخارجية المسموحة **بس**: Telegram · Turso · مصر الرقمية · خدمة التجديد.

**السرّيات** (بـ`wrangler secret put`، مش في `wrangler.toml`):
`TELEGRAM_BOT_TOKEN` · `TURSO_URL` · `TURSO_TOKEN` · `ADMIN_SECRET` ·
`WEBHOOK_SECRET` · `RENEW_SECRET` · `GITHUB_TOKEN`

غير سرّية في `[vars]`: `RENEWER_URL` · `WORKER_URL` · `GITHUB_REPO`.

`ADMIN_SECRET` و`RENEW_SECRET` **لازم يكونوا نفس القيمة** في Worker وOrkestr.

---

## 5) وكيل SMAX — smax-agent/agent.py

بيشتغل على الجهاز، بيسحب شغل من الـWorker وبينفّذه على بوابة SMAX بـPlaywright.

```
agent.env          السر + عنوان الـWorker   <- مش في git، خد نسخة يدوي
agent_id.txt       معرّف الوكيل            <- لو اتغيّر بيتعمل صف جديد في agent_state
smax_session.json  جلسة SMAX              <- بتتجدد لوحدها
agent.log          اللوج
smax_search.py     منطق البحث
```

- بيانات دخول SMAX من `E:\Projects\ComplaintsBot\Smax_V11\.env`
  (`SMAX_SITE_USER` / `SMAX_SITE_PASSWORD`).
- متصفحات Playwright من `E:\Projects\Tools\playwright` — `agent.py` بيظبط
  `PLAYWRIGHT_BROWSERS_PATH` بنفسه.
- إيقاع الاستطلاع: `ACTIVE_SEC=5` · `IDLE_SEC=10` · `ACTIVE_WINDOW_SEC=120`.
- **حارس نسخة واحدة** (mutex `EgyptPost.SmaxAgent.SingleInstance`) — تشغيله
  كل 5 دقايق آمن، النسخة الزيادة بتخرج لوحدها.
- ملفات `diag_*.py` و`diag_*.png` **أدوات تشخيص قديمة** — مش جزء من التشغيل.

**Turso** = فهرس الباركود وحالة الوكيل. الهجرات في `migrations/`:
`001_agent_state` · `002_smax_jobs` · `003_smax_jobs_sent_at` ·
`004_smax_jobs_tracked_at` · `005_smax_jobs_track`.
بياناتها في `Post Report Tool/.env.cloud`.

---

## 6) البالون — Post Report Tool/tray_agent.py

أيقونة في الـtray بتراقب النسخ والتحديد؛ لو لقت باركود بريد بتطلّع بالونة،
والضغط عليها بيفتح رحلة الشحنة.

```
BC_RE               شكل الباركود المقبول: حرفين لأربعة + أرقام + EG
TokenKeeper         فحص كل 5 دقايق · تجديد لو فاضل أقل من 10 دقايق
ClipboardWatcher    النسخ
DragSelectionWatcher / Hotkey   التحديد بالماوس + الاختصار
Tracker             بيشغّل track.py في process منفصل — الـUI مابيعلّقش
logs/tray_agent.log
```

- بيتشغّل بـ`tray_agent - شغّل مخفي.bat` أو المهمة المجدولة.
- **حارس نسخة واحدة** برضه — التشغيل المتكرر آمن.
- `pynput` **اختياري** — لو فشل البرنامج بيكمّل من غير مراقبة الماوس.
  **ماتخليهش إجباري.**
- `_backup_tray_agent_20260905.py` نسخة قديمة — **متعدّلش فيها.**

---

## 7) المهام المجدولة الخمسة

XML محفوظ في `E:\Projects\Tools\tasks\` — استرجاع بأمر واحد:

```
powershell -ExecutionPolicy Bypass -File E:\Projects\Tools\tasks\restore_tasks.ps1
```

| المهمة | الأمر | الجدول |
|---|---|---|
| `MCIT Post Report` | `Tools\python\pythonw.exe run_daily.py` | يوميًا 5:30ص · إعادة كل ساعة لمدة 9 ساعات |
| `MCIT SMAX Agent` | `pythonw.exe cloud-bot\smax-agent\agent.py` | كل **5 دقايق** |
| `MCIT Tray Agent` | `pythonw.exe Post Report Tool\tray_agent.py` | عند الدخول + كل **5 دقايق** |
| `MCIT Weekly Projects Backup` | `powershell -File Tools\scripts\backup_all.ps1` | أسبوعيًا — الاتنين 11:00ص |
| `MCIT Weekly Claude Context Backup` | `powershell -File Tools\scripts\backup_claude_context.ps1` | أسبوعيًا — الاتنين 11:30ص |

⚠️ **حارس الوكيل والبالون بقى كل 5 دقايق** (كان 10) بعد حادثة 2026-09-08:
الوكيل مات 16:33 من غير أي أثر في اللوج، والحارس رجّعه بعد 10 دقايق —
فطلب تتبّع استنى 6 دقايق، منهم 5 انتظار لعملية ميتة.

⚠️ **`MCIT Weekly Claude Context Backup` مهيّأة بس ماشتغلتش ولا مرة**
(`LastRunTime = 11/30/1999`). النسخة الأساسية (`Projects Backup`) شغّالة عادي.
السبب لسه مش متفحوص.

**البايثون المضمّن** (playwright · pynput · pywin32 · openpyxl) في
`E:\Projects\Tools\python\` — **مافيش اعتماد على أي بايثون على `C:`.**

---

## 8) النشر والرجوع

```
cloud-bot\deploy.bat             نشر الـWorker (Node محمول من Tools\node)
npx wrangler secret put NAME     سر جديد
npx wrangler rollback            رجوع
```

خدمة التجديد: Orkestr ثم Redeploy · build context `orkestr-f5-test/` ·
Dockerfile `orkestr-f5-test/Dockerfile`.

**الرجوع آمن** — التغييرات كلها كود، مافيش migration ولا schema في الـWorker.

توكن Cloudflare في `E:\Projects\Tools\config\.wrangler\config\default.toml`
— **المجلد ده ماينشاركش.**

---

## 9) المراقبة

`GET /health` على الـWorker — أرقام وحالات بس، صفر أسرار:

```json
{ "ok": true, "token": "valid", "token_exp_in_sec": 612,
  "needs_renew": false, "renew_lock_held": false, "renewer_configured": true }
```

**إنذار:** `token_exp_in_sec` بصفر أو null لأكتر من 5 دقايق معناه **التجديد واقف**.

---

## 10) قبل أي تعديل

1. **اقرا `OPERATIONS.md`** — التشغيل والأسرار والرجوع وأنماط الفشل.
2. **ماتنشرش** (Worker أو Orkestr) من غير إذن صريح — دي خدمة حية.
3. **ماتضيفش نقطة HTTP جديدة** من غير حارس. تاريخ المشروع فيه SSRF مفتوح فعلًا.
4. **ماتطبعش توكن ولا بيانات دخول** في أي لوج أو رد أو رسالة.
5. الاختبارات: `test_agent*.py` · `test_token_manager.mjs` · `test_renew.py` ·
   `test_notify.py` — شغّلها قبل أي «تم».
6. **حارس النسخة الواحدة** في الوكيل والبالون تعاقد تشغيلي — ماتشيلهوش.

قواعد الشغل العامة (CHANGE SAFETY · كفاءة التوكنز) في `~/.claude/CLAUDE.md`.
