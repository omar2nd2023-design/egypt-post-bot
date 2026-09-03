# -*- coding: utf-8 -*-
"""refresh_token.py — يجدّد توكن مصر الرقمية ويبعته للـWorker.

بيشتغل على GitHub Actions (Ubuntu + Playwright headless).
بيسجّل دخول بوابة مصر الرقمية ويلتقط التوكن من طلبات الشبكة،
وبعدين يبعته للـCloudflare Worker اللي بيخزّنه في KV.

المتغيّرات المطلوبة (GitHub Secrets):
  DE_PHONE       رقم الموبايل
  DE_PASSWORD    كلمة السر
  WORKER_URL     رابط الـWorker
  ADMIN_SECRET   السر المشترك مع الـWorker
"""
import base64
import json
import os
import sys
import time
import urllib.request

LOGIN_URL = "https://digital.gov.eg/"
CAPTURED = {"token": None}


def log(m):
    print(m, flush=True)


def decode_exp(tok):
    try:
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:
        return None


def looks_like_access_token(tok):
    """التوكن الصح بيبدأ بـeyJ وعنده exp — مش Serialized-ID."""
    if not tok or not tok.startswith("eyJ"):
        return False
    try:
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        d = json.loads(base64.urlsafe_b64decode(payload))
        return bool(d.get("exp")) and d.get("typ") != "Serialized-ID"
    except Exception:
        return False


def capture(request):
    """بيمسك التوكن من هيدر Authorization في أي طلب."""
    if CAPTURED["token"]:
        return
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        tok = auth[7:].strip()
        if looks_like_access_token(tok):
            CAPTURED["token"] = tok
            log("🔑 التوكن اتمسك من طلب شبكة")


def main():
    phone = os.environ.get("DE_PHONE", "").strip()
    pwd = os.environ.get("DE_PASSWORD", "").strip()
    worker = os.environ.get("WORKER_URL", "").strip().rstrip("/")
    admin = os.environ.get("ADMIN_SECRET", "").strip()
    if not all([phone, pwd, worker, admin]):
        log("!! متغيّرات ناقصة")
        return 1

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage",
        ])
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"),
            locale="ar-EG",
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.new_page()
        page.on("request", capture)

        log("→ بنفتح البوابة...")
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        # ندوّر على زرار الدخول
        for sel in ("text=تسجيل الدخول", "text=دخول", "a[href*='login']",
                    "button:has-text('تسجيل')"):
            try:
                el = page.locator(sel).first
                if el.count() and el.is_visible():
                    log(f"→ بندوس {sel}")
                    el.click(timeout=10000)
                    page.wait_for_timeout(4000)
                    break
            except Exception:
                continue

        # نملا البيانات
        log("→ بنملا البيانات...")
        filled = False
        for user_sel in ("input[name='username']", "input[type='tel']",
                         "input[name='phone']", "#username", "input[type='text']"):
            try:
                el = page.locator(user_sel).first
                if el.count() and el.is_visible():
                    el.fill(phone, timeout=10000)
                    filled = True
                    break
            except Exception:
                continue
        for pw_sel in ("input[name='password']", "input[type='password']", "#password"):
            try:
                el = page.locator(pw_sel).first
                if el.count() and el.is_visible():
                    el.fill(pwd, timeout=10000)
                    break
            except Exception:
                continue

        if filled:
            for sub in ("button[type='submit']", "input[type='submit']",
                        "text=دخول", "#kc-login"):
                try:
                    el = page.locator(sub).first
                    if el.count() and el.is_visible():
                        el.click(timeout=10000)
                        break
                except Exception:
                    continue

        # نستنى التوكن يظهر
        log("→ بنستنى التوكن...")
        for _ in range(40):
            if CAPTURED["token"]:
                break
            page.wait_for_timeout(1000)

        # لو لسه مامسكناش، نروح لصفحة فيها API calls
        if not CAPTURED["token"]:
            log("→ بنجرّب صفحة الخدمات...")
            try:
                page.goto("https://digital.gov.eg/services",
                          wait_until="domcontentloaded", timeout=45000)
                for _ in range(20):
                    if CAPTURED["token"]:
                        break
                    page.wait_for_timeout(1000)
            except Exception:
                pass

        browser.close()

    tok = CAPTURED["token"]
    if not tok:
        log("!! ماقدرناش نجيب التوكن")
        return 2

    exp = decode_exp(tok)
    left = int(exp - time.time()) if exp else 0
    log(f"✓ التوكن جاهز — فاضل {left} ثانية")

    # نبعته للـWorker
    body = json.dumps({"token": tok}).encode()
    req = urllib.request.Request(
        worker + "/token", data=body, method="POST",
        headers={"Authorization": f"Bearer {admin}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        log("✓ اتبعت للـWorker: " + r.read().decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
