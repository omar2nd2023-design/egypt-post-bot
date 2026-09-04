# -*- coding: utf-8 -*-
"""orkestr-f5-test — يثبت: Orkestr → Chromium → F5 → صفحة Keycloak الحقيقية.

الغرض الوحيد: نعرف هل Chromium بيشتغل في 512MB، وهل F5 Shape بيسمح
لمتصفح حقيقي من الـegress بتاع orkestr يوصل صفحة دخول Keycloak.

مفيش هنا — ولا هيتضاف:
  · username / password / أي credentials
  · ملء أي حقل أو الضغط على submit
  · التقاط JWT أو access/refresh token أو هيدر Authorization
  · أي تعديل على بصمة المتصفح أو محاولة تجاوز F5

بندوس على «تسجيل الدخول» في البوابة عشان **نفتح** الفورم وبس. ده المسار
الأساسي: auto_token المحلي الشغّال مابيبنيش authorization URL — بيسيب
البوابة تولّد الطلب بالـredirect_uri المسجّل عندها. فبدل ما نخمّن القيمة
دي (وKeycloak رفض تخميننا بـ"Invalid parameter: redirect_uri")، بنقرا
الـURL اللي البوابة ولّدته وبنقفل الـpopup. مفيش كتابة ولا إرسال.

بنستخدم Chromium headless بأعلام الحاويات القياسية بس
(--no-sandbox / --disable-dev-shm-usage / --disable-gpu) — دي مطلوبة
لتشغيل Chromium جوّه docker، ومالهاش علاقة بالتمويه.

  GET /         معلومات + health check
  GET /f5       بينفّذ الاختبار ويرجّع JSON
                (لو PROBE_KEY متعرّف في البيئة، لازم ?key=<القيمة>)

────────────────────────────────────────────────────────────────────
تشخيص الصفحات — ليه اتغيّر
────────────────────────────────────────────────────────────────────
النسخة الأولى كانت بتحكم «الصفحة دي حجب» بمجرّد ظهور كلمة زي
"forbidden" أو "غير مصرح" في الـHTML. ده أدّى لنتيجة خاطئة: بوابة
digital.gov.eg رجعت 200 بعنوان «مصر الرقمية» وحجم 265KB — يعني الصفحة
الحقيقية — ومع ذلك اتصنّفت «محجوبة»، لأنها SPA فيها قاموس رسايل أخطاء
HTTP مدمج جوّه الـbundle. الكلمات كانت **نصوص واجهة**، مش صفحة حجب.

دلوقتي الحكم بنيوي: حالة HTTP + الـURL النهائي + حجم الصفحة + طلبات
‎/TSPD/‎ على الشبكة. والكلمات النصّية بقت **دليل مساند بيتسجّل ومايحكمش**
لوحده — وبيتسجّل معاه إنها ظهرت في صفحة كبيرة سليمة عشان الضجيج ده
يبان بدل ما يقلب الحكم في صمت.

كوكيز TS*/BIGip مش دليل رفض — دي كوكيز جلسة F5 بتتحط لأي زائر مسموح له.
"""
import json
import os
import re
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8000"))
PROBE_KEY = os.environ.get("PROBE_KEY", "").strip()   # حارس للـendpoint فقط

PORTAL_URL = "https://digital.gov.eg/"

# نفس الـissuer والـclient اللي في التوكن الحالي (iss / azp).
# مفيش هنا أي سر — دي قيم عامة بتظهر في أي JWT.
KC_BASE = "https://login.di.gov.eg/realms/digitalegypt"
KC_AUTH = (
    KC_BASE + "/protocol/openid-connect/auth"
    "?client_id=de"
    "&response_type=code"
    "&scope=openid"
    "&redirect_uri=https%3A%2F%2Fdigital.gov.eg%2F"
    "&state=probe"
    "&nonce=probe"
)

# أعلام الحاويات القياسية — مطلوبة لتشغيل Chromium جوّه docker
CHROME_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]

F5_COOKIE_PREFIXES = ("TS", "BIGip", "BIGipServer")

PORTAL_TIMEOUT = 60_000       # مللي — تحميل الصفحة
F5_SETTLE_MS = 8_000          # نستنى F5 ينفّذ تحدّيه ويستقر
KC_TIMEOUT = 60_000
KC_SETTLE_MS = 6_000

# المسار الأساسي — البوابة هي اللي تبدأ الـflow (زي auto_token بالظبط)
POPUP_TIMEOUT = 30_000
POPUP_SETTLE_MS = 6_000
# نفس الـselectors المستعملة في auto_token.py الشغّال محليًا
LOGIN_ENTRY_SELECTORS = (
    "text=تسجيل الدخول",
    "a:has-text('تسجيل الدخول')",
    "button:has-text('تسجيل الدخول')",
)
# بارامترات بنيوية — دي اللي بنقارن بيها، فبتتعرض بقيمتها
AUTHORIZE_PARAM_KEYS = (
    "client_id", "response_type", "scope", "redirect_uri",
    "response_mode", "code_challenge_method", "ui_locales",
    "kc_idp_hint", "prompt", "display", "login_hint",
)
# قيم عشوائية مالهاش معنى تشخيصي — بنسجّل وجودها وطولها بس، مش قيمتها
OPAQUE_PARAM_KEYS = ("state", "nonce", "code_challenge", "session_state")

# صفحة حجب/تحدي من F5 صغيرة جدًا. الصفحة الحقيقية مئات الكيلوبايت.
SMALL_PAGE_BYTES = 30_000

# نص صفحة الرفض القياسية بتاعة F5 ASM/Shape
F5_BLOCK_MARKERS = (
    "the requested url was rejected",
    "request rejected",
    "please consult with your administrator",
    "support id is",
    "support id:",
)
# سكربت تحدّي F5 Shape — بيتحقن قبل ما الصفحة الحقيقية تتحمّل
F5_CHALLENGE_MARKERS = (
    "/tspd/",
    "tspd_101",
    "_setfpcookie",
    "window.tsp",
)
# كلمات عامة — ضجيج في أي SPA. بتتسجّل للتوثيق ومابتحكمش لوحدها.
GENERIC_DENY_WORDS = (
    "access denied", "forbidden", "unauthorized",
    "تم رفض", "غير مصرح",
)


# ------------------------------------------------------------ تعقيم المخرجات
_REDACTIONS = (
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-.]{10,}"), "[JWT]"),
    (re.compile(r"(?i)\bbearer\s+\S+"), "[BEARER]"),
    (re.compile(r"(?i)\bauthorization\b\s*[:=]\s*\S+"), "authorization=[X]"),
    (re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|client[_-]?secret)"
                r"\b\s*[:=]\s*\S+"), r"\1=[X]"),
    (re.compile(r"[A-Za-z0-9_\-]{40,}"), "[LONGTOKEN]"),
)


def redact(s, limit=400):
    """أي نص جاي من الصفحة بيعدّي من هنا قبل ما يتحط في الـJSON."""
    if not s:
        return None
    s = " ".join(str(s).split())
    for rx, rep in _REDACTIONS:
        s = rx.sub(rep, s)
    return s[:limit]


def safe_url(u, limit=300):
    """URL من غير الـfragment — الفراجمنت هو اللي بيحمل التوكنات."""
    if not u:
        return None
    u = str(u).split("#", 1)[0]
    return redact(u, limit)


# ------------------------------------------------------------ الذاكرة
def container_mem():
    """استهلاك الذاكرة للحاوية (cgroup v2 ثم v1). بالميجابايت."""
    out = {}
    pairs = [
        ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
        ("/sys/fs/cgroup/memory/memory.usage_in_bytes",
         "/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    ]
    for cur, mx in pairs:
        try:
            with open(cur) as f:
                out["used_mb"] = round(int(f.read().strip()) / 1048576, 1)
            try:
                with open(mx) as f:
                    v = f.read().strip()
                if v != "max":
                    lim = int(v) / 1048576
                    if lim < 1_000_000:          # نتجاهل قيم "بلا حد"
                        out["limit_mb"] = round(lim, 1)
            except Exception:
                pass
            return out
        except Exception:
            continue
    return out or {"note": "cgroup غير متاح"}


def mem_pressure():
    """هل احنا قرّبنا على السقف؟ (نسبة الاستخدام للحد)."""
    m = container_mem()
    u, l = m.get("used_mb"), m.get("limit_mb")
    if u and l:
        return round(100.0 * u / l, 1)
    return None


# ------------------------------------------------------------ تصنيف الصفحة
def _hits(text, markers):
    h = (text or "").lower()
    return [m for m in markers if m in h]


def classify_page(status, url, html, title, tspd_requests):
    """تصنيف بنيوي للصفحة. بيرجّع dict فيه الحكم + الأدلة اللي بنى عليها.

    page_class:
      normal        الصفحة الحقيقية اتحمّلت
      f5_challenge  صفحة تحدّي مؤقتة من F5 (سكربت TSPD / طلبات TSPD)
      f5_block      صفحة رفض من F5
      error_page    خطأ من التطبيق نفسه (مش F5)
    """
    html = html or ""
    n = len(html)
    u = (url or "").lower()
    small = n < SMALL_PAGE_BYTES

    block_hits = _hits(html, F5_BLOCK_MARKERS) + _hits(title, F5_BLOCK_MARKERS)
    chal_hits = _hits(html, F5_CHALLENGE_MARKERS)
    generic_hits = _hits(html, GENERIC_DENY_WORDS)

    tspd_in_url = "/tspd/" in u
    tspd_net = bool(tspd_requests)

    # ── الحجب: لازم دليل بنيوي، مش كلمة في نص
    is_block = (
        tspd_in_url
        or (bool(block_hits) and small)
        or (status in (401, 403, 503) and small)
    )
    # ── التحدي: سكربت/طلبات TSPD على صفحة صغيرة مؤقتة
    is_chal = (not is_block) and small and (bool(chal_hits) or tspd_net)

    if is_block:
        page_class = "f5_block"
    elif is_chal:
        page_class = "f5_challenge"
    elif status and 200 <= status < 400 and not small:
        page_class = "normal"
    elif status and status >= 400:
        page_class = "error_page"
    else:
        page_class = "normal" if not small else "error_page"

    ev = {
        "page_class": page_class,
        "html_len": n,
        "small_page": small,
        "tspd_in_final_url": tspd_in_url,
        "tspd_network_requests": tspd_requests[:5],
        "f5_block_markers": block_hits,
        "f5_challenge_markers": chal_hits,
    }
    # الضجيج: كلمات عامة ظهرت في صفحة كبيرة سليمة — دي i18n مش حجب.
    if generic_hits:
        ev["generic_deny_words_seen"] = generic_hits
        ev["generic_words_are_noise"] = (not small) and page_class == "normal"
    return ev


def _is_keycloak(html, url):
    """هل الصفحة دي من Keycloak فعلاً؟ (حتى لو صفحة خطأ)"""
    h = (html or "").lower()
    return ("login.di.gov.eg" in (url or "")
            or "kc-form" in h or "kc-error" in h or "kc-page" in h
            or "keycloak" in h or "realms/digitalegypt" in h)


def _oidc_error_from_url(url):
    """Keycloak ساعات بيرجّع error/error_description في الـquery."""
    out = {}
    try:
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(url or "").query)
        for k in ("error", "error_description"):
            if k in q and q[k]:
                out[k] = redact(q[k][0], 200)
    except Exception:
        pass
    return out or None


def _authorize_params(url):
    """بارامترات طلب الـauth من الـURL اللي البوابة ولّدته.

    القيم البنيوية (client_id/scope/redirect_uri…) بتتعرض بقيمتها لأنها
    هي محل المقارنة. القيم العشوائية (state/nonce/PKCE) بيتسجّل وجودها
    وطولها بس — مالهاش معنى تشخيصي وماينفعش تتطبع.
    """
    out, opaque, extra = {}, {}, []
    try:
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(url or "").query, keep_blank_values=True)
        for k, v in q.items():
            val = v[0] if v else ""
            if k in AUTHORIZE_PARAM_KEYS:
                out[k] = redact(val, 300)
            elif k in OPAQUE_PARAM_KEYS:
                opaque[k] = {"present": bool(val), "len": len(val)}
            else:
                extra.append(k)
    except Exception:
        return None
    if not (out or opaque or extra):
        return None
    r = {"params": out}
    if opaque:
        r["opaque"] = opaque
    if extra:
        r["other_param_names"] = sorted(extra)
    return r


def _login_entry(page):
    """أول زرار «تسجيل الدخول» في البوابة — نفس selectors auto_token.

    بنجرّب الظاهر الأول، وبعدين أي واحد موجود (auto_token بيدوس
    بـforce من غير ما يتحقق من الظهور).
    """
    for require_visible in (True, False):
        for sel in LOGIN_ENTRY_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                if require_visible and not loc.is_visible():
                    continue
                return loc, sel
            except Exception:
                continue
    return None, None


def _page_error_text(page):
    """نص الخطأ من الصفحة. بنجرّب selectors الثيم الافتراضي، وبعدين
    ثيمات مخصّصة، وأخيرًا أول نص ظاهر في الـbody — لأن ثيم «مصر الرقمية»
    مخصّص والـselectors القديمة كانت بترجع فاضي فيضيع السبب."""
    selectors = (
        "#kc-error-message", "#kc-error-message .instruction",
        ".alert-error", ".pf-c-alert__title", ".kc-feedback-text",
        "#kc-content-wrapper .instruction", "#kc-content-wrapper",
        "[class*='error']", "[id*='error']", "main", "body",
    )
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                txt = (loc.inner_text(timeout=2000) or "").strip()
                if txt:
                    return {"selector": sel, "text": redact(txt, 400)}
        except Exception:
            continue
    return None


def _form_fields(page):
    """أي حقول إدخال ظاهرة؟ الأسماء بس — مفيش قيم ولا كتابة."""
    found = {}
    probes = {
        "username": ("#username", "input[name='username']",
                     "input[type='email']"),
        "password": ("#password", "input[name='password']"),
        "submit": ("#kc-login", "input[type='submit']",
                   "button[type='submit']"),
    }
    for key, sels in probes.items():
        for sel in sels:
            try:
                if page.locator(sel).count() > 0:
                    found[key] = True
                    break
            except Exception:
                continue
        found.setdefault(key, False)
    try:
        found["input_count"] = page.locator("input").count()
        found["form_count"] = page.locator("form").count()
    except Exception:
        pass
    return found


# ------------------------------------------------------------ الاختبار
def run_probe():
    res = {
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "memory_mb": {"before": container_mem()},
        "chromium": {},
        "portal": {},
        "keycloak": {},
    }

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        res["verdict"] = "FAIL"
        res["failure"] = "chromium_launch_failure"
        res["diagnosis"] = "chromium_launch_failure"
        res["error"] = f"playwright import: {type(e).__name__}: {e}"[:200]
        return res

    browser = None
    pw = None
    tspd_seen = []          # أي طلب شبكة فيه /TSPD/ — دليل F5 مباشر
    try:
        pw = sync_playwright().start()

        # ---------- 1) تشغيل Chromium ----------
        t0 = time.monotonic()
        try:
            browser = pw.chromium.launch(headless=True, args=CHROME_ARGS)
        except Exception as e:
            res["chromium"] = {"launched": False,
                               "error": f"{type(e).__name__}: {e}"[:300]}
            res["memory_mb"]["at_failure"] = container_mem()
            res["verdict"] = "FAIL"
            low = str(e).lower()
            oom = ("out of memory" in low or "oom" in low
                   or "cannot allocate" in low)
            res["failure"] = ("memory_failure" if oom
                              else "chromium_launch_failure")
            res["diagnosis"] = res["failure"]
            return res

        res["chromium"] = {
            "launched": True,
            "launch_ms": round((time.monotonic() - t0) * 1000),
            "browser_version": browser.version,
            "playwright_version": getattr(pw, "_playwright_version", None)
                                  or os.environ.get("PW_VERSION"),
            "args": CHROME_ARGS,
        }
        res["memory_mb"]["after_launch"] = container_mem()

        ctx = browser.new_context(
            locale="ar-EG",
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.new_page()

        # مراقبة طلبات F5 على الشبكة — دليل مباشر بدل التخمين النصّي.
        # بنسجّل كمان حالة أول تنقّل لكل صفحة، عشان الـpopup ماعندهاش
        # response object نقرا منه الحالة.
        nav_status = {}

        def _watch(p, tag):
            def _on_response(r):
                try:
                    if "/TSPD/" in r.url:
                        tspd_seen.append({"url": safe_url(r.url, 120),
                                          "status": r.status})
                    if tag not in nav_status and r.request.is_navigation_request():
                        nav_status[tag] = r.status
                except Exception:
                    pass
            p.on("response", _on_response)

        _watch(page, "main")
        # أي popup بتتراقب من لحظة إنشائها — قبل ما تنقّلها يخلص
        ctx.on("page", lambda p: _watch(p, "popup"))

        # ---------- 2) البوابة — نسيب F5 ينفّذ تحدّيه ----------
        t1 = time.monotonic()
        try:
            resp = page.goto(PORTAL_URL, wait_until="domcontentloaded",
                             timeout=PORTAL_TIMEOUT)
            page.wait_for_timeout(F5_SETTLE_MS)
            html = page.content()
            title = page.title()
            status = resp.status if resp else None
            res["portal"] = {
                "status": status,
                "final_url": safe_url(page.url),
                "redirected": bool(resp and resp.url != PORTAL_URL),
                "title": redact(title, 150),
                "ms": round((time.monotonic() - t1) * 1000),
                "tspd_script_present": "/TSPD/" in html,
                "evidence": classify_page(status, page.url, html, title,
                                          list(tspd_seen)),
            }
        except Exception as e:
            res["portal"] = {"error": f"{type(e).__name__}: {e}"[:250],
                             "ms": round((time.monotonic() - t1) * 1000)}
            res["memory_mb"]["at_failure"] = container_mem()
            res["verdict"] = "FAIL"
            res["failure"] = ("timeout" if "Timeout" in type(e).__name__
                              else "portal_failure")
            res["diagnosis"] = res["failure"]
            return res

        # كوكيز F5 — الأسماء بس، مفيش قيم. دليل مساند مش شرط حكم:
        # الكوكيز دي بتتحط لأي زائر مسموح له، فوجودها ≠ رفض.
        try:
            cookies = ctx.cookies()
            res["portal"]["f5_cookie_names"] = sorted(
                {c["name"] for c in cookies
                 if c["name"].startswith(F5_COOKIE_PREFIXES)}
            )
            res["portal"]["cookie_count"] = len(cookies)
            res["portal"]["f5_cookies_note"] = (
                "أسماء فقط — كوكيز جلسة F5 بتتحط لأي زائر مسموح له، "
                "فوجودها مش دليل رفض.")
        except Exception:
            res["portal"]["f5_cookie_names"] = []

        res["memory_mb"]["after_portal"] = container_mem()

        # لو البوابة نفسها اتصنّفت حجب/تحدي، مافيش داعي نكمّل
        pclass = res["portal"]["evidence"]["page_class"]
        if pclass in ("f5_block", "f5_challenge"):
            res["verdict"] = "FAIL"
            res["failure"] = "f5_blocked"
            res["diagnosis"] = pclass
            res["f5_passed"] = False
            res["conclusion"] = (
                "F5 وقف عند البوابة — الأدلة في portal.evidence.")
            return res

        # ---------- 3) صفحة دخول Keycloak — الحكم ----------
        t2 = time.monotonic()
        tspd_before_kc = len(tspd_seen)
        try:
            resp = page.goto(KC_AUTH, wait_until="domcontentloaded",
                             timeout=KC_TIMEOUT)
            page.wait_for_timeout(KC_SETTLE_MS)
            html = page.content()
            title = page.title()
            status = resp.status if resp else None
            fields = _form_fields(page)
            res["keycloak"] = {
                "status": status,
                "final_url": safe_url(page.url),
                "title": redact(title, 150),
                "ms": round((time.monotonic() - t2) * 1000),
                "login_form_present": bool(fields.get("username")),
                "form_fields": fields,
                "is_keycloak_page": _is_keycloak(html, page.url),
                "oidc_error_in_url": _oidc_error_from_url(page.url),
                "page_error_text": _page_error_text(page),
                "auth_params_sent": {
                    "client_id": "de",
                    "response_type": "code",
                    "scope": "openid",
                    "redirect_uri": "https://digital.gov.eg/",
                },
                "evidence": classify_page(status, page.url, html, title,
                                          tspd_seen[tspd_before_kc:]),
            }
        except Exception as e:
            res["keycloak"] = {"error": f"{type(e).__name__}: {e}"[:250],
                               "ms": round((time.monotonic() - t2) * 1000)}
            res["memory_mb"]["at_failure"] = container_mem()
            res["verdict"] = "FAIL"
            res["failure"] = ("timeout" if "Timeout" in type(e).__name__
                              else "keycloak_not_reached")
            res["diagnosis"] = res["failure"]
            return res

        # ---------- 4) المسار الأساسي: البوابة هي اللي تبدأ الـflow ----------
        # auto_token المحلي مابيبنيش authorization URL — بيدوس «تسجيل
        # الدخول» وبيسيب البوابة تولّد الطلب بالـredirect_uri المسجّل
        # عندها. بنكرّر نفس الحاجة: **فتح** الفورم بس، وقراية الـURL،
        # وقفل الـpopup. مفيش كتابة ولا submit ولا التقاط توكن.
        pi = {"popup_opened": False}
        t3 = time.monotonic()
        tspd_before_pi = len(tspd_seen)
        try:
            page.goto(PORTAL_URL, wait_until="domcontentloaded",
                      timeout=PORTAL_TIMEOUT)
            page.wait_for_timeout(F5_SETTLE_MS)
            entry, sel = _login_entry(page)
            pi["login_entry_selector"] = sel
            if entry is None:
                pi["error"] = "زرار «تسجيل الدخول» مالقيناهوش في البوابة"
            else:
                # نفضّي حالة التنقّل عشان نمسك تنقّل الدخول هو اللي يتسجّل
                nav_status.pop("main", None)
                target = page       # لو مافيش popup، البوابة بتنقّل نفسها
                try:
                    with page.expect_popup(timeout=POPUP_TIMEOUT) as pinfo:
                        entry.click(timeout=15_000)
                    target = pinfo.value
                    pi["popup_opened"] = True
                except Exception:
                    page.wait_for_timeout(POPUP_SETTLE_MS)
                target.wait_for_timeout(POPUP_SETTLE_MS)

                html = target.content()
                title = target.title()
                status = nav_status.get("popup") or nav_status.get("main")
                fields = _form_fields(target)
                ev = classify_page(status, target.url, html, title,
                                   tspd_seen[tspd_before_pi:])
                pi.update({
                    "popup_url": safe_url(target.url),
                    "final_url": safe_url(target.url),
                    "status": status,
                    "title": redact(title, 150),
                    "authorize_params": _authorize_params(target.url),
                    "login_form_present": bool(fields.get("username")),
                    "form_fields": fields,
                    "is_keycloak_page": _is_keycloak(html, target.url),
                    "oidc_error_in_url": _oidc_error_from_url(target.url),
                    "page_error_text": _page_error_text(target),
                    "page_class": ev["page_class"],
                    "evidence": ev,
                })
                if target is not page:
                    try:
                        target.close()
                    except Exception:
                        pass
        except Exception as e:
            pi["error"] = f"{type(e).__name__}: {e}"[:250]
        pi["ms"] = round((time.monotonic() - t3) * 1000)
        res["portal_initiated"] = pi

        res["memory_mb"]["peak_observed"] = container_mem()
        res["memory_mb"]["pressure_pct"] = mem_pressure()

        # ---------- 5) الحكم — المسار الأساسي هو portal_initiated ----------
        kc = res["keycloak"]              # ← control سالب للمقارنة بس
        kclass = kc["evidence"]["page_class"]
        piclass = pi.get("page_class")

        if pi.get("login_form_present"):
            res["verdict"] = "PASS"
            res["diagnosis"] = "login_form_reached"
            res["conclusion"] = (
                "Chromium اشتغل، وF5 سمح، والبوابة فتحت فورم دخول Keycloak "
                "الحقيقي بالـredirect_uri المسجّل عندها.")
        elif piclass in ("f5_block", "f5_challenge"):
            res["verdict"] = "FAIL"
            res["failure"] = "f5_blocked"
            res["diagnosis"] = piclass
            res["conclusion"] = (
                "F5 وقف الـpopup — الأدلة في portal_initiated.evidence.")
        elif pi.get("login_entry_selector") is None:
            res["verdict"] = "FAIL"
            res["failure"] = "login_entry_not_found"
            res["diagnosis"] = "login_entry_not_found"
            res["conclusion"] = (
                "البوابة اتحمّلت بس زرار «تسجيل الدخول» مالقيناهوش — يا إما "
                "الصفحة اتغيّرت يا إما لسه بتحمّل.")
        elif pi.get("is_keycloak_page"):
            res["verdict"] = "FAIL"
            res["failure"] = "oidc_invalid_request"
            res["diagnosis"] = "keycloak_reachable_oidc_invalid"
            res["conclusion"] = (
                "وصلنا Keycloak بالبارامترات اللي البوابة نفسها ولّدتها "
                "وبرضه مافيش فورم — راجع portal_initiated.authorize_params "
                "و page_error_text.")
        elif not pi.get("popup_opened"):
            res["verdict"] = "FAIL"
            res["failure"] = "popup_not_opened"
            res["diagnosis"] = "popup_not_opened"
            res["conclusion"] = (
                "دوسنا على الزرار بس مافتحش popup ولا اتنقّلنا لـKeycloak.")
        else:
            res["verdict"] = "FAIL"
            res["failure"] = "keycloak_not_reached"
            res["diagnosis"] = "keycloak_not_reached"
            res["conclusion"] = "الصفحة مش بتاعة Keycloak — لسه ما وصلناش."

        # الـcontrol السالب: طلب auth مبني يدوي بـredirect_uri مخمّن.
        # موجود للمقارنة بس — مش بيدخل في الحكم.
        res["control_direct_auth"] = {
            "note": ("طلب auth مبني يدوي بـredirect_uri مخمّن — control "
                     "سالب، مش المسار الأساسي."),
            "status": kc.get("status"),
            "login_form_present": kc.get("login_form_present"),
            "is_keycloak_page": kc.get("is_keycloak_page"),
            "page_class": kclass,
        }

        # هل F5 عدّى؟ سؤال مستقل تمامًا عن صحة طلب الـOIDC.
        res["f5_passed"] = (
            res["portal"]["evidence"]["page_class"] == "normal"
            and kclass not in ("f5_block", "f5_challenge")
            and piclass not in ("f5_block", "f5_challenge")
        )
        return res

    except Exception as e:
        res["verdict"] = "FAIL"
        res["failure"] = "unknown"
        res["diagnosis"] = "unknown"
        res["error"] = f"{type(e).__name__}: {e}"[:300]
        res["trace_tail"] = redact(traceback.format_exc()[-600:], 600)
        return res
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        try:
            if pw:
                pw.stop()
        except Exception:
            pass


# ------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        raw = self.path
        path = raw.split("?", 1)[0].rstrip("/") or "/"
        query = raw.split("?", 1)[1] if "?" in raw else ""

        if path == "/f5":
            if PROBE_KEY:
                ok = any(p == f"key={PROBE_KEY}" for p in query.split("&"))
                if not ok:
                    self._send({"ok": False, "error": "unauthorized"}, 401)
                    return
            try:
                self._send(run_probe())
            except Exception as e:
                self._send({"verdict": "FAIL", "failure": "unknown",
                            "diagnosis": "unknown",
                            "error": f"{type(e).__name__}: {e}"[:200]}, 500)
        elif path == "/":
            self._send({"ok": True,
                        "service": "orkestr-f5-test",
                        "usage": "GET /f5" + (" ?key=…" if PROBE_KEY else ""),
                        "diagnosis_values": [
                            "login_form_reached",
                            "keycloak_reachable_oidc_invalid",
                            "login_entry_not_found", "popup_not_opened",
                            "f5_challenge", "f5_block",
                            "keycloak_not_reached", "portal_failure",
                            "memory_failure", "chromium_launch_failure",
                            "timeout", "unknown"],
                        "note": "مفيش credentials ولا login — فحص وصول فقط"})
        else:
            self._send({"ok": False, "error": "not found"}, 404)

    def log_message(self, fmt, *args):
        # method + path بس (بدون query عشان المفتاح مايتسجّلش)
        print(f"{self.command} {self.path.split('?', 1)[0]}", flush=True)


if __name__ == "__main__":
    print(f"orkestr-f5-test listening on 0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
