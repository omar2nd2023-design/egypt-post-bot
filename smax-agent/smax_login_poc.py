# -*- coding: utf-8 -*-
"""smax_login_poc.py — إثبات تسجيل الدخول وحفظ الجلسة. لا أكثر.

⚠️ ممنوع في الملف ده: بحث · فتح شكوى · استخراج · تعليقات · طابور.
⚠️ مابيطبعش: اسم مستخدم · باسورد · كوكيز · محتوى الجلسة · أي سر.

الخطوات:
    1) متصفح جديد -> SMAX -> إثبات إننا مش داخلين
    2) تسجيل دخول (نفس منطق Smax_V11/scraper.py:do_login)
    3) إثبات الحالة المصادَقة بمؤشر بنيوي
    4) حفظ الجلسة
    5) قفل المتصفح بالكامل
    6) متصفح جديد تمامًا + الجلسة المحفوظة -> إثبات إننا لسه داخلين
"""
import io
import os
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
SESSION = HERE / "smax_session.json"
ENVF = Path(r"E:\Projects\ComplaintsBot\Smax_V11\.env")

BASE = "https://support.degypt.net"
LOGIN_URL = BASE + "/saw/"
TARGET = BASE + "/saw/Requests"

_SECRETS = []


def redact(s):
    out = str(s)
    for x in _SECRETS:
        if x and len(x) >= 4:
            out = out.replace(x, "***")
    return out


def load_creds():
    d = {}
    for line in io.open(ENVF, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    u, p = d.get("SMAX_SITE_USER", ""), d.get("SMAX_SITE_PASSWORD", "")
    if not u or not p:
        raise SystemExit("ناقص SMAX_SITE_USER أو SMAX_SITE_PASSWORD")
    _SECRETS.extend([u, p])
    return u, p


def probe(page, ctx):
    """يرجّع صورة عن حالة الصفحة — من غير أي محتوى حساس."""
    html = page.content()
    low = html.lower()
    return {
        "url": page.url.split("?")[0],
        "title": (page.title() or "")[:40],
        "pw_inputs": page.locator("input[type=password]").count(),
        # مؤشر بنيوي مش نصّي — الصفحة بقت بالعربي فالنص الإنجليزي مابيلزمش
        "has_login_markers": (page.locator("#password").count() > 0
                              or page.locator("#username").count() > 0),
        "has_app_markers": ("service request management" in low
                            or "my requests" in low),
        "f5_cookies": len([c for c in ctx.cookies()
                           if str(c.get("name", "")).upper().startswith("TS")]),
        "cookie_count": len(ctx.cookies()),
    }


def verdict(p):
    """مصادَق = مافيش حقل باسورد + مافيش علامات دخول + فيه علامات التطبيق."""
    return (p["pw_inputs"] == 0 and not p["has_login_markers"]
            and p["has_app_markers"])


def show(label, p):
    print("   --- %s ---" % label)
    print("     العنوان        : %s" % p["url"][:64])
    print("     عنوان الصفحة   : %s" % p["title"])
    print("     حقول باسورد    : %d" % p["pw_inputs"])
    print("     علامات صفحة دخول: %s" % p["has_login_markers"])
    print("     علامات التطبيق  : %s" % p["has_app_markers"])
    print("     كوكيز (عدد/F5) : %d / %d" % (p["cookie_count"], p["f5_cookies"]))
    print("     الحكم          : %s" % ("✅ مصادَق" if verdict(p)
                                        else "🔐 مش مصادَق"))


def main():
    user, pwd = load_creds()
    from playwright.sync_api import sync_playwright

    print("=== المرحلة 1: متصفح نضيف — إثبات إننا مش داخلين ===")
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True,
                               args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = b.new_context(locale="ar-EG", viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.goto(TARGET, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(7000)
        before = probe(page, ctx)
        show("قبل الدخول", before)

        print("")
        print("=== المرحلة 2: تسجيل الدخول ===")
        t0 = time.monotonic()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        # 🔴 صفحة الدخول اتغيّرت عن اللي Smax_V11 متوقّعه.
        #    دلوقتي /idm-service/idm/v0/login وبالعربي، والزرار مكتوب
        #    عليه «تسجيل الدخول» مش "LOG IN" — فالبحث بالنص بيفشل.
        #    المعرّفات ثابتة وأوضح: #username · #password · #submit
        page.wait_for_selector("#password", timeout=30000)
        page.fill("#username", user)
        page.fill("#password", pwd)
        page.click("#submit")
        page.wait_for_timeout(9000)
        print("     زمن الدخول: %d ms" % ((time.monotonic() - t0) * 1000))

        # 🔴 كشف التحديات لازم يكون **بنيوي مش نصّي**.
        #    البحث عن "invalid" في الـHTML كله بيلقطها من أسماء الـCSS
        #    زي `invalid-feedback` ويعلن فشل والدخول ناجح فعلاً.
        #    الصح: نبص على الصفحة اللي إحنا واقفين عليها + رسائل ظاهرة.
        still_on_login = page.locator("input[type=password]").count() > 0
        if still_on_login:
            visible = page.evaluate(
                "() => Array.from(document.querySelectorAll("
                "'.alert,.error,.invalid-feedback,[role=alert],.text-danger'))"
                ".map(e => (e.innerText||'').trim()).filter(t => t).slice(0,5)")
            print("     ⚠ لسه على صفحة الدخول. رسائل ظاهرة: %s"
                  % (visible if visible else "مافيش"))
            import re
            low = page.content().lower()
            for name, mark in (("MFA / OTP", r"one-time|verification code|two-factor"),
                               ("CAPTCHA", r"captcha")):
                if re.search(mark, low):
                    print("     ⚠ ظهر تحدٍّ: %s — بيقف، مش هحاول أعديه." % name)
            b.close()
            return 2

        page.goto(TARGET, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(7000)
        after = probe(page, ctx)
        print("")
        print("=== المرحلة 3: إثبات الحالة المصادَقة ===")
        show("بعد الدخول", after)
        if not verdict(after):
            print("\n   ✘ الدخول مانجحش — مش هحفظ جلسة ولا هكمّل.")
            b.close()
            return 1

        print("")
        print("=== المرحلة 4: حفظ الجلسة ===")
        ctx.storage_state(path=str(SESSION))
        print("     اتحفظت: %s" % SESSION.name)
        print("     الحجم  : %.1f KB" % (SESSION.stat().st_size / 1024))
        b.close()
        print("")
        print("=== المرحلة 5: المتصفح اتقفل بالكامل ===")

    print("")
    print("=== المرحلة 6: متصفح جديد + الجلسة المحفوظة ===")
    with sync_playwright() as pw2:
        b2 = pw2.chromium.launch(headless=True,
                                 args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx2 = b2.new_context(locale="ar-EG", viewport={"width": 1440, "height": 900},
                              storage_state=str(SESSION))
        p2 = ctx2.new_page()
        p2.goto(TARGET, wait_until="domcontentloaded", timeout=60000)
        p2.wait_for_timeout(7000)
        reuse = probe(p2, ctx2)
        show("إعادة استخدام الجلسة", reuse)
        ok = verdict(reuse)
        b2.close()

    print("")
    print("=" * 54)
    print("   session reuse: %s" % ("PASS ✔" if ok else "FAIL ✘"))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        print("خطأ: %s" % redact("%s: %s" % (type(e).__name__, e))[:200])
        sys.exit(1)