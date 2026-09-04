# الجزء الناقص: `POST /renew` في خدمة التجديد

> مافيش أسرار في الملف ده.

اتمنعت مني 3 محاولات تعديل على منطقة الدخول في
`orkestr-f5-test/probe.py` من مصنِّف الصلاحيات في auto mode.
ماحاولتش ألتف. الملف ده بيوصّف بالظبط اللي ناقص عشان أي حد
(أو أنا بعد فك المنع) ينفّذه في خطوة واحدة.

---

## الحالة

| الجزء | الحالة |
|---|---|
| `_do_renew()` · `renew_token()` · `_verify_logged_in()` | ✅ موجودين محليًا، الملف بيترجم |
| مسار HTTP يوصّل ليهم | ❌ **مامتنعش يتضاف** |
| إصلاح صحّة `login_success` | ❌ **مامتنعش يتطبّق** |

يعني الكود المحلي فيه منطق تجديد كامل **مالوش أي طريقة يتنادى بيها**،
وفيه باج صحّة معروف. **عشان كده مادفعتوش لـgit.**

---

## أ) إصلاح الصحّة — إجباري قبل أي نشر

**الباج:** في `_do_renew()`، `out["login_success"] = True` بتتحط بعد لوب
انتظار قفل الـpopup **من غير أي تحقق**. لو بيانات الدخول غلط، الفورم
بيفضل مفتوح، اللوب بتخلص بعد 30 ثانية، والكود بيقول «الدخول نجح».

**كمان:** لو مافيش popup (`target is page`)، `target.title()` عمره ما
هيرمي استثناء، فاللوب بتستهلك 30 ثانية على الفاضي كل مرة.

**البديل** — استبدل الخطوات 5 و6 بده:

```python
        # 5) استنى الـpopup تقفل — أول مؤشر إن الدخول عدّى.
        #    لو مافيش popup أصلاً مانستناش عالفاضي.
        popup_closed = False
        if target is not page:
            for _ in range(30):
                if time.monotonic() - t0 > RENEW_DEADLINE_SEC:
                    break
                try:
                    target.title()
                except Exception:
                    popup_closed = True
                    break
                time.sleep(1)
        page.wait_for_timeout(4000)

        # 6) استنى التوكن يظهر في طلبات الشبكة
        for i in range(TOKEN_WAIT_SEC):
            if captured["tok"]:
                out["token_source"] = "network"
                break
            if time.monotonic() - t0 > RENEW_DEADLINE_SEC:
                out["failure_reason"] = "deadline_exceeded"
                break
            if i == 12:
                try:
                    page.goto("https://digital.gov.eg/services",
                              wait_until="domcontentloaded", timeout=45_000)
                except Exception:
                    pass
            page.wait_for_timeout(1000)

        # احتياطي الكوكي: KEYCLOAK_IDENTITY غالبًا Serialized-ID مش
        # توكن وصول، و_is_access_token بيرفضه. فده عمليًا **مسار
        # تشخيصي** مش مصدر متوقّع — المصدر الحقيقي هو التقاط الشبكة.
        if not captured["tok"]:
            try:
                for c in ctx.cookies():
                    if c.get("name") in COOKIE_NAMES and _is_access_token(
                            c.get("value", "")):
                        captured["tok"] = c["value"]
                        out["token_source"] = "cookie"
                        break
            except Exception:
                pass

        # 7) الحكم على الدخول — بدليل، مش بافتراض
        out["post_login"] = _verify_logged_in(page, target, popup_closed,
                                              bool(captured["tok"]))
        out["login_success"] = bool(out["post_login"].get("logged_in"))

        tok = captured["tok"]
        if not tok:
            out["failure_reason"] = (
                out["failure_reason"]
                or ("token_not_observed" if out["login_success"]
                    else "login_not_confirmed"))
            raise RuntimeError(out["failure_reason"])
```

ولازم يتضاف ثابت جنب باقي الثوابت:

```python
RENEW_DEADLINE_SEC = 210     # سقف كلي للدورة، أقل من مهلة الانضمام
```

> `_verify_logged_in()` **موجودة فعلاً** في الملف المحلي وبتترجم.

---

## ب) مسار HTTP — الجزء الرئيسي الناقص

### العقد

| | |
|---|---|
| المسار | `POST /renew` |
| المصادقة | `Authorization: Bearer <RENEW_SECRET>` |
| الجسم | متجاهَل (بيتقرا ويترمي، بحد 4KB) |
| بدون `RENEW_SECRET` في البيئة | `503` — الـendpoint مقفول تمامًا |
| سر غلط | `401` |
| النجاح | `200` + metadata |
| المهلة | مضبوطة من `RENEW_DEADLINE_SEC` جوّه، والـWorker بيستنى 100ث |

### الرد — metadata بس، مفيش توكن ولا بيانات دخول

```json
{
  "ok": true,
  "login_success": true,
  "token_observed": true,
  "token_source": "network",
  "token": { "exp_in_sec": 880, "lifetime_sec": 900,
             "azp": "de", "scope": "fnf nid email username" },
  "api_status": 200,
  "api_records": 7,
  "pushed_to_worker": true,
  "elapsed_ms": 41320,
  "memory_mb": { "after": 214.6, "pressure_pct": 41.9 },
  "failure_reason": null
}
```

عند الفشل: نفس الشكل بـ`ok: false` و`failure_reason` من المجموعة —
`missing_credentials` · `login_entry_not_found` · `phone_field_not_found` ·
`password_field_not_found` · `login_not_confirmed` · `token_not_observed` ·
`deadline_exceeded` · `worker_push_failed` · `api_status_<n>`.

### الكود — يتحط في `class Handler`

استبدل آخر جزء من `do_GET` بده:

```python
                        "note": "/f5 فحص وصول بدون credentials · "
                                "/renew تجديد التوكن (محمي بسر)"})
        elif path == "/health":
            last = _renew_state["result"] or {}
            self._send({
                "ok": True,
                "service": "orkestr-f5-test",
                "renew_configured": bool(DE_PHONE and DE_PASSWORD
                                         and WORKER_URL and ADMIN_SECRET),
                "renew_guarded": bool(RENEW_SECRET),
                "renew_running": _renew_state["running"],
                "last_renew_ok": last.get("ok"),
                "last_renew_age_sec": (
                    round(time.time() - _renew_state["finished_at"])
                    if _renew_state["finished_at"] else None),
                "memory_mb": container_mem(),
            })
        else:
            self._send({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        """POST /renew — يجدّد التوكن ويرفعه للـWorker. الرد metadata بس."""
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path != "/renew":
            self._send({"ok": False, "error": "not found"}, 404)
            return
        # الحارس إجباري: من غير سر الـendpoint مقفول — مش مفتوح
        # للعالم يشغّل تسجيل دخول.
        if not RENEW_SECRET:
            self._send({"ok": False,
                        "error": "renew disabled: RENEW_SECRET غير متعرّف"},
                       503)
            return
        if self.headers.get("Authorization", "") != f"Bearer {RENEW_SECRET}":
            self._send({"ok": False, "error": "unauthorized"}, 401)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 0:
                self.rfile.read(min(n, 4096))     # نستهلك الجسم ونتجاهله
        except Exception:
            pass
        try:
            self._send(renew_token())
        except Exception as e:
            self._send({"ok": False, "failure_reason":
                        redact(f"{type(e).__name__}: {e}", 150)}, 500)
```

كمان ضيف `login_entry_not_found` و`popup_not_opened` موجودين أصلاً في
`diagnosis_values`، وممكن تضيف قيم الفشل بتاعة التجديد لو حبيت.

---

## ج) ليه الـendpoint ده مطلوب

الـWorker اتنشر وهو بينادي `POST {RENEWER_URL}/renew`. من غير المسار ده
بيرجع 404، فـ`triggerRenew` ترجّع `false`، فمافيش تجديد أوتوماتيك خالص.
**ده الحلقة الوحيدة الناقصة في السلسلة كلها.**

---

## د) الاختبار بعد التنفيذ

```bash
# 1) الصحّة — لازم renew_configured: true و renew_guarded: true
curl https://egypt-post-f5-test.orkestr.run/health

# 2) بدون سر — لازم 401
curl -X POST https://egypt-post-f5-test.orkestr.run/renew

# 3) بالسر — لازم ok: true و api_status: 200
curl -X POST -H "Authorization: Bearer $RENEW_SECRET" \
     https://egypt-post-f5-test.orkestr.run/renew

# 4) الـWorker لازم يشوف التوكن
curl https://egypt-post-bot.awladywebanaty.workers.dev/health
```
