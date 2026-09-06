# -*- coding: utf-8 -*-
"""check_smax.py — فحص وصول معزول لـSMAX من بيئة الوكيل.

⚠️ قراءة فقط:
    • مافيش تسجيل دخول · مافيش كتابة في أي حقل · مافيش إرسال أي فورم
    • مافيش بحث · مافيش فتح شكوى · مافيش تعديل أي بيانات
    • مابيطبعش أي كوكي ولا credential — أسماء وأعداد بس
    • مافيش أي محاولة لتجاوز F5

بيجاوب على: هل الجهاز يوصل SMAX؟ وهل الجلسة المحفوظة لسه صالحة
ولا محتاجين دخول جديد؟
"""
import json
import os
import re
import socket
import ssl
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

HOST = "support.degypt.net"
URL = "https://support.degypt.net/saw/Requests"
AUTH_FILE = Path(r"E:\Projects\ComplaintsBot\Smax_V11\auth_state.json")


def layers():
    print("=== 1-2) الطبقات: DNS / TCP / TLS / HTTP ===")
    t0 = time.monotonic()
    try:
        infos = socket.getaddrinfo(HOST, 443, socket.AF_INET, socket.SOCK_STREAM)
        ip = infos[0][4][0]
        print("   DNS   : %4d ms   ->  %s" % ((time.monotonic()-t0)*1000, ip))
    except Exception as e:
        print("   DNS   : فشل — %s" % type(e).__name__); return False
    s = socket.socket(); s.settimeout(15)
    t0 = time.monotonic()
    try:
        s.connect((ip, 443))
        print("   TCP   : %4d ms" % ((time.monotonic()-t0)*1000))
    except Exception as e:
        print("   TCP   : فشل — %s" % type(e).__name__); return False
    t0 = time.monotonic()
    try:
        w = ssl.create_default_context().wrap_socket(s, server_hostname=HOST)
        print("   TLS   : %4d ms   %s" % ((time.monotonic()-t0)*1000, w.version()))
    except Exception as e:
        print("   TLS   : فشل — %s" % type(e).__name__); return False
    t0 = time.monotonic()
    try:
        w.sendall(("HEAD / HTTP/1.1\r\nHost: %s\r\nUser-Agent: smax-check/1.0\r\n"
                   "Connection: close\r\n\r\n" % HOST).encode())
        line = (w.recv(256) or b"").split(b"\r\n", 1)[0].decode("latin1", "replace")
        print("   HTTP  : %4d ms   %s" % ((time.monotonic()-t0)*1000, line[:40]))
    except Exception as e:
        print("   HTTP  : فشل — %s" % type(e).__name__)
    try: w.close()
    except Exception: pass
    return True


def browser():
    from playwright.sync_api import sync_playwright
    print("")
    print("=== 3-5) Chromium + الجلسة المحفوظة ===")
    has_auth = AUTH_FILE.exists()
    print("   ملف الجلسة: %s%s" % (
        "موجود" if has_auth else "مش موجود",
        ("   (%.1f KB · آخر تعديل %s)" % (
            AUTH_FILE.stat().st_size/1024,
            time.strftime("%Y-%m-%d %H:%M", time.localtime(AUTH_FILE.stat().st_mtime)))
         ) if has_auth else ""))

    tspd = []
    with sync_playwright() as pw:
        t0 = time.monotonic()
        b = pw.chromium.launch(headless=True,
                               args=["--no-sandbox", "--disable-dev-shm-usage"])
        print("   Chromium: %d ms   %s" % ((time.monotonic()-t0)*1000, b.version))

        for label, state in (("من غير جلسة", None),
                             ("بالجلسة المحفوظة", str(AUTH_FILE) if has_auth else None)):
            if label == "بالجلسة المحفوظة" and not has_auth:
                continue
            ctx = b.new_context(locale="ar-EG", viewport={"width":1440,"height":900},
                                storage_state=state)
            page = ctx.new_page()
            page.on("response", lambda r: tspd.append(1) if "/TSPD/" in r.url else None)
            t1 = time.monotonic()
            try:
                resp = page.goto(URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(7000)
                ms = round((time.monotonic()-t1)*1000)
                html = page.content()
                low = html.lower()
                final = page.url
                names = sorted({c.get("name","") for c in ctx.cookies()})
                ts = [n for n in names if n.upper().startswith("TS")]
                pw_in = page.locator("input[type=password]").count()
                is_login = ("/idm/v0/login" in final or "service portal" in low
                            or pw_in >= 1)
                print("")
                print("   --- %s ---" % label)
                print("     HTTP        : %s   (%d ms)" % (resp.status if resp else "?", ms))
                print("     العنوان     : %s" % final.split("?")[0][:70])
                print("     الصفحة      : %s" % (page.title() or "")[:50])
                print("     كوكيز F5    : %d  (%s)" % (len(ts), " ".join(ts) if ts else "مافيش"))
                print("     طلبات TSPD  : %d" % len(tspd))
                print("     حقل باسورد  : %d" % pw_in)
                print("     الحكم       : %s" % ("🔐 صفحة دخول — الجلسة مش صالحة"
                                                 if is_login else "✅ داخل — الجلسة صالحة"))
            except Exception as e:
                print("     فشل: %s" % type(e).__name__)
            finally:
                ctx.close()
        b.close()


if __name__ == "__main__":
    ok = layers()
    if ok:
        browser()
    else:
        print("\n   الشبكة مش واصلة — وقفت قبل المتصفح.")