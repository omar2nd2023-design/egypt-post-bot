# -*- coding: utf-8 -*-
"""تشخيص ما بعد الدخول — قراءة فقط. مابيطبعش أي بيانات دخول."""
import io, sys, re, time
sys.stdout.reconfigure(encoding="utf-8")
from playwright.sync_api import sync_playwright

d = {}
for line in io.open(r"E:\Projects\ComplaintsBot\Smax_V11\.env", encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); d[k.strip()] = v.strip()
U, P = d["SMAX_SITE_USER"], d["SMAX_SITE_PASSWORD"]
SEC = [U, P]
def red(s):
    s = str(s)
    for x in SEC:
        if x and len(x) >= 3: s = s.replace(x, "***")
    return s

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
    ctx = b.new_context(locale="ar-EG", viewport={"width":1440,"height":900})
    p = ctx.new_page()
    p.goto("https://support.degypt.net/saw/", wait_until="domcontentloaded", timeout=60000)
    p.wait_for_timeout(6000)
    print("قبل: %s" % p.url.split("?")[0])
    p.wait_for_selector("#password", timeout=30000)
    p.fill("#username", U); p.fill("#password", P)
    print("الحقول اتملت. بيدوس...")
    p.click("#submit")
    p.wait_for_timeout(10000)

    print("")
    print("=== بعد الضغط ===")
    print("  العنوان     : %s" % p.url.split("?")[0][:70])
    print("  عنوان الصفحة: %s" % (p.title() or "")[:50])
    print("  حقل باسورد  : %d" % p.locator("input[type=password]").count())
    vis = p.evaluate("""() => Array.from(document.querySelectorAll(
        '.alert,.error,.invalid-feedback,[role=alert],.help-block,.text-danger,.message'))
        .map(e => (e.innerText||'').trim()).filter(t => t).slice(0,6)""")
    print("  رسائل ظاهرة : %s" % (vis if vis else "مافيش"))
    body = p.evaluate("() => document.body.innerText.slice(0,600)")
    print("  أول 300 حرف من النص الظاهر:")
    for ln in red(body)[:300].split("\n"):
        if ln.strip(): print("     " + ln.strip()[:70])
    low = p.content().lower()
    for name, pat in (("invalid", r"invalid"), ("incorrect", r"incorrect"),
                      ("failed to log", r"failed to log"), ("captcha", r"captcha"),
                      ("otp/2fa", r"one-time|verification code|two-factor")):
        m = re.search(pat, low)
        print("  نمط '%s': %s" % (name, "موجود" if m else "لأ"))

    p.goto("https://support.degypt.net/saw/Requests", wait_until="domcontentloaded", timeout=60000)
    p.wait_for_timeout(8000)
    print("")
    print("=== بعد الذهاب لـ/saw/Requests ===")
    print("  العنوان     : %s" % p.url.split("?")[0][:70])
    print("  حقل باسورد  : %d" % p.locator("input[type=password]").count())
    low2 = p.content().lower()
    print("  علامة التطبيق (service request management): %s"
          % ("service request management" in low2))
    print("  علامة MY REQUESTS: %s" % ("my requests" in low2))
    b.close()