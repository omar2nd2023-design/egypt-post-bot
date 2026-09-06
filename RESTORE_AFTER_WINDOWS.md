# استعادة الشغل بعد تنزيل ويندوز جديد — cloud-bot + وكيل SMAX + البالون

> جرد اتعمل 2026-09-05. الهدف: تعرف إيه اللي **عايش جوه `E:\Projects`** (بيرجع لوحده)
> وإيه اللي **عايش على `C:`** (لازم يتعمل تاني).

## ✅ موجود في `E:\Projects` — بيرجع زي ما هو

| الحاجة | المكان |
|---|---|
| كود الـWorker (Cloudflare) | `E:\Projects\Egypt Post Reports\cloud-bot\src\worker.js` + `migrations\` + الاختبارات |
| وكيل SMAX | `E:\Projects\Egypt Post Reports\cloud-bot\smax-agent\agent.py` · `smax_search.py` |
| سر الوكيل + عنوان الـWorker | `smax-agent\agent.env` (**مش في git** — خد نسخة يدوي) |
| معرّف الوكيل | `smax-agent\agent_id.txt` (لو اتغيّر بيتعمل صف جديد في `agent_state` — مش مشكلة) |
| جلسة SMAX | `smax-agent\smax_session.json` (بتتجدد لوحدها — لو ضاعت الوكيل بيسجّل دخول) |
| بيانات دخول SMAX | `E:\Projects\ComplaintsBot\Smax_V11\.env` (`SMAX_SITE_USER` / `SMAX_SITE_PASSWORD`) |
| بيانات Turso | `E:\Projects\Egypt Post Reports\Post Report Tool\.env.cloud` |
| البالون | `Post Report Tool\tray_agent.py` + `E:\Projects\Egypt Post Reports\tray_agent - شغّل مخفي.bat` |
| بايثون مضمّن (فيه playwright · pynput · pywin32 · openpyxl) | `E:\Projects\Tools\python\` |
| الأداة اليومية | `Post Report Tool\run_daily.py` وكل الـ`.bat` |
| النسخة الأسبوعية | `E:\Projects\Tools\scripts\backup_all.ps1` |
| التوثيق | `E:\Projects\Egypt Post Reports\.claude\skills\smax-live-complaint-search\SKILL.md` |

## ✅ اتنقلوا لـ`E:\Projects` كمان (2026-09-05)

| الحاجة | المكان الجديد | ملاحظة |
|---|---|---|
| **متصفحات Playwright** (Chromium) | `E:\Projects\Tools\playwright\` | `agent.py` بيظبط `PLAYWRIGHT_BROWSERS_PATH` عليه لوحده |
| **المهام المجدولة الأربعة** (XML) | `E:\Projects\Tools\tasks\*.xml` + `restore_tasks.ps1` | أمر واحد يرجّعهم كلهم |

| **Node.js (portable) + wrangler** | `E:\Projects\Tools\node\` + `cloud-bot\node_modules\` | النشر بـ`cloud-bot\deploy.bat` — مش محتاج أي تنصيب |
| **تسجيل دخول Cloudflare (wrangler)** | `E:\Projects\Tools\config\.wrangler\config\default.toml` | ⚠️ فيه توكن حسابك — **ماتشاركش المجلد ده**. لو انتهى: `deploy.bat login` مرة واحدة |

## ⚠️ لسه على `C:` — برامج مش ملفات، لازم تنصيب بعد ويندوز جديد

| الحاجة | ليه | إزاي ترجّعه |
|---|---|---|
| Outlook (للأداة اليومية `run_daily.py` بس) | برنامج | تنصيب + نفس الحساب |
| Telegram Desktop / Chrome | للاختبار بس | اختياري |

> البوت + الوكيل + البالون **مش محتاجين أي حاجة من C:** بعد النهاردة (2026-09-06).
> Git كمان بقى محمول في `E:\Projects\Tools\git` (MinGit 2.55) — لأوامر git والريبو.
> المراجعة الكاملة لكل المشاريع: `E:\Projects\Tools\بعد تنزيل ويندوز - اقرأ ده الأول.md`.

### المهام المجدولة — أمر واحد

```bat
powershell -ExecutionPolicy Bypass -File E:\Projects\Tools\tasks\restore_tasks.ps1
```

(أو يدوي لكل مهمة: `schtasks /Create /F /TN "MCIT SMAX Agent" /XML "E:\Projects\Tools\tasks\MCIT SMAX Agent.xml"`)

- `MCIT Post Report`: `E:\Projects\Tools\python\pythonw.exe run_daily.py` — WorkingDirectory
  `E:\Projects\Egypt Post Reports\Post Report Tool` — يوميًا الساعة 5:30 ص مع إعادة كل ساعة لحد 8
  (التفاصيل في `Post Report Tool\CLAUDE.md`).
- `MCIT Weekly Projects Backup`: `powershell -File E:\Projects\Tools\scripts\backup_all.ps1` أسبوعيًا.

> الاتنين (البالون + وكيل SMAX) عندهم **حارس نسخة واحدة** — تشغيلهم كل 10 دقايق آمن،
> النسخة الزيادة بتخرج لوحدها.

## اللي **مش** على الجهاز خالص (مافيش حاجة تتعمل)

- الـWorker نفسه شغّال على Cloudflare (آخر نسخة منشورة موجودة هناك).
- الأسرار (`AGENT_SECRET` · `ADMIN_SECRET` · `TELEGRAM_BOT_TOKEN` · `TURSO_*` · `RENEW_SECRET`) محفوظة
  في Cloudflare Secrets — **ماتتطبعش وماتتنقلش**.
- قاعدة البيانات (Turso): `bc` · `agent_state` · `smax_jobs` (migrations 001–004 متطبّقة).
- الكود على GitHub: `omar2nd2023-design/egypt-post-bot`.

## ترتيب التشغيل بعد التنصيب — **خطوة واحدة**

1. **ماتفرمتش `E:`** وقت تنزيل ويندوز (C: بس).
2. دبل كليك على: `E:\Projects\Tools\بعد تنزيل ويندوز - شغّل ده مرة واحدة.bat`
   (بيرجّع المهام المجدولة الأربعة ويشغّل البالون ووكيل SMAX).
3. ابعت `/health` للبوت — لازم يقول الوكيل `ONLINE` خلال دقيقة.
4. للأداة اليومية للتقارير بس: نصّب Outlook بنفس الحساب.

> ليه لسه فيه خطوة؟ المهام المجدولة بتتخزّن **جوه ويندوز نفسه** (مش ملفات)،
> فأي ويندوز جديد لازم يتقاله عليها مرة — الـbat بيعمل ده في ثواني.
