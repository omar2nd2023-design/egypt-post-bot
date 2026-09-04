# -*- coding: utf-8 -*-
"""orkestr-f5-test — يثبت: Orkestr → Chromium → F5 → صفحة Keycloak الحقيقية.

الغرض الوحيد: نعرف هل Chromium بيشتغل في 512MB، وهل F5 Shape بيسمح
لمتصفح حقيقي من الـegress بتاع orkestr يوصل صفحة دخول Keycloak.

مفيش هنا — ولا هيتضاف:
  · username / password / أي credentials
  · تسجيل دخول أو ملء أي حقل
  · دوس على زرار تسجيل الدخول
  · التقاط JWT أو أي توكن
  · أي تعديل على بصمة المتصفح أو محاولة تجاوز F5

بنستخدم Chromium headless بأعلام الحاويات القياسية بس
(--no-sandbox / --disable-dev-shm-usage / --disable-gpu) — دي مطلوبة
لتشغيل Chromium جوّه docker، ومالهاش علاقة بالتمويه.

  GET /         معلومات + health check
  GET /f5       بينفّذ الاختبار ويرجّع JSON
                (لو PROBE_KEY متعرّف في البيئة، لازم ?key=<القيمة>)
"""
import json
import os
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


def _looks_blocked(html, title):
    """مؤشرات صفحة حجب/تحدي بدل المحتوى الحقيقي."""
    h = (html or "").lower()
    t = (title or "").lower()
    marks = ["access denied", "request rejected", "forbidden",
             "the requested url was rejected", "support id",
             "تم رفض", "غير مصرح"]
    return any(m in h or m in t for m in marks)


def _is_keycloak(html, url):
    """هل الصفحة دي من Keycloak فعلاً؟ (حتى لو صفحة خطأ)"""
    h = (html or "").lower()
    return ("login.di.gov.eg" in (url or "")
            or "kc-form" in h or "kc-error" in h or "kc-page" in h
            or "keycloak" in h or "realms/digitalegypt" in h)


def _kc_error_text(page):
    """نص خطأ Keycloak لو موجود (مثلاً redirect_uri غير صالح)."""
    for sel in ("#kc-error-message", ".alert-error", ".kc-feedback-text",
                "#kc-content-wrapper .instruction"):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                txt = (loc.inner_text(timeout=2000) or "").strip()
                if txt:
                    return txt[:300]
        except Exception:
            continue
    return None


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
        res["error"] = f"playwright import: {type(e).__name__}: {e}"[:200]
        return res

    browser = None
    pw = None
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
            res["failure"] = ("memory_failure"
                              if ("out of memory" in low or "oom" in low
                                  or "cannot allocate" in low)
                              else "chromium_launch_failure")
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

        # ---------- 2) البوابة — نسيب F5 ينفّذ تحدّيه ----------
        t1 = time.monotonic()
        try:
            resp = page.goto(PORTAL_URL, wait_until="domcontentloaded",
                             timeout=PORTAL_TIMEOUT)
            page.wait_for_timeout(F5_SETTLE_MS)
            html = page.content()
            title = page.title()
            res["portal"] = {
                "status": resp.status if resp else None,
                "final_url": page.url,
                "title": title[:150],
                "ms": round((time.monotonic() - t1) * 1000),
                "html_len": len(html),
                "tspd_script_present": "/TSPD/" in html,
                "looks_blocked": _looks_blocked(html, title),
            }
        except Exception as e:
            res["portal"] = {"error": f"{type(e).__name__}: {e}"[:250],
                             "ms": round((time.monotonic() - t1) * 1000)}
            res["memory_mb"]["at_failure"] = container_mem()
            res["verdict"] = "FAIL"
            res["failure"] = ("timeout" if "Timeout" in type(e).__name__
                              else "portal_failure")
            return res

        # كوكيز F5 — دليل مساند، مش شرط الحكم
        try:
            cookies = ctx.cookies()
            res["portal"]["f5_cookies"] = sorted(
                {c["name"] for c in cookies
                 if c["name"].startswith(F5_COOKIE_PREFIXES)}
            )
            res["portal"]["cookie_count"] = len(cookies)
        except Exception:
            res["portal"]["f5_cookies"] = []

        res["memory_mb"]["after_portal"] = container_mem()

        # ---------- 3) صفحة دخول Keycloak — الحكم ----------
        t2 = time.monotonic()
        try:
            resp = page.goto(KC_AUTH, wait_until="domcontentloaded",
                             timeout=KC_TIMEOUT)
            page.wait_for_timeout(KC_SETTLE_MS)
            html = page.content()
            title = page.title()
            has_user = False
            for sel in ("#username", "input[name='username']"):
                try:
                    if page.locator(sel).count() > 0:
                        has_user = True
                        break
                except Exception:
                    continue
            res["keycloak"] = {
                "status": resp.status if resp else None,
                "final_url": page.url,
                "title": title[:150],
                "ms": round((time.monotonic() - t2) * 1000),
                "html_len": len(html),
                "login_form_present": has_user,     # ← الحكم الأساسي
                "is_keycloak_page": _is_keycloak(html, page.url),
                "looks_blocked": _looks_blocked(html, title),
                "kc_error_text": _kc_error_text(page),
            }
        except Exception as e:
            res["keycloak"] = {"error": f"{type(e).__name__}: {e}"[:250],
                               "ms": round((time.monotonic() - t2) * 1000)}
            res["memory_mb"]["at_failure"] = container_mem()
            res["verdict"] = "FAIL"
            res["failure"] = ("timeout" if "Timeout" in type(e).__name__
                              else "keycloak_not_reached")
            return res

        res["memory_mb"]["peak_observed"] = container_mem()

        # ---------- 4) الحكم ----------
        kc = res["keycloak"]
        if kc.get("login_form_present"):
            res["verdict"] = "PASS"
            res["conclusion"] = (
                "Chromium اشتغل، وF5 سمح، ووصلنا فورم دخول Keycloak الحقيقي.")
        else:
            res["verdict"] = "FAIL"
            if kc.get("looks_blocked") or res["portal"].get("looks_blocked"):
                res["failure"] = "f5_blocked"
                res["conclusion"] = "صفحة حجب — F5 رفض المتصفح."
            elif kc.get("is_keycloak_page"):
                # وصلنا Keycloak فعلاً بس الفورم مظهرش (غالبًا parameters)
                res["failure"] = "login_form_not_found"
                res["conclusion"] = (
                    "🟡 مهم: وصلنا Keycloak فعلاً (يعني F5 عدّى)، بس الفورم "
                    "مظهرش — على الأرجح الـauth parameters محتاجة ظبط "
                    "(redirect_uri/scope). راجع kc_error_text.")
            else:
                res["failure"] = "keycloak_not_reached"
                res["conclusion"] = "الصفحة مش بتاعة Keycloak — لسه ما وصلناش."
        return res

    except Exception as e:
        res["verdict"] = "FAIL"
        res["failure"] = "unknown"
        res["error"] = f"{type(e).__name__}: {e}"[:300]
        res["trace_tail"] = traceback.format_exc()[-600:]
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
                            "error": f"{type(e).__name__}: {e}"[:200]}, 500)
        elif path == "/":
            self._send({"ok": True,
                        "service": "orkestr-f5-test",
                        "usage": "GET /f5" + (" ?key=…" if PROBE_KEY else ""),
                        "note": "مفيش credentials ولا login — فحص وصول فقط"})
        else:
            self._send({"ok": False, "error": "not found"}, 404)

    def log_message(self, fmt, *args):
        # method + path بس (بدون query عشان المفتاح مايتسجّلش)
        print(f"{self.command} {self.path.split('?', 1)[0]}", flush=True)


if __name__ == "__main__":
    print(f"orkestr-f5-test listening on 0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
