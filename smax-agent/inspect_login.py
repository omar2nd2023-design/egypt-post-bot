# -*- coding: utf-8 -*-
"""فحص بنية صفحة الدخول — قراءة فقط، مافيش كتابة ولا إرسال."""
import sys, json
sys.stdout.reconfigure(encoding="utf-8")
from playwright.sync_api import sync_playwright

for URL in ("https://support.degypt.net/saw/",
            "https://support.degypt.net/saw/Requests"):
    print("=== %s ===" % URL)
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
        ctx = b.new_context(locale="ar-EG", viewport={"width":1440,"height":900})
        p = ctx.new_page()
        p.goto(URL, wait_until="domcontentloaded", timeout=60000)
        p.wait_for_timeout(8000)
        print("   العنوان النهائي: %s" % p.url[:80])
        print("   عنوان الصفحة   : %s" % (p.title() or "")[:50])
        print("   إطارات (frames): %d" % len(p.frames))
        for i, fr in enumerate(p.frames):
            try:
                n = fr.locator("input").count()
                bt = fr.locator("button, input[type=submit]").count()
            except Exception:
                n = bt = -1
            if n or bt:
                print("     frame[%d] %s   inputs=%d buttons=%d"
                      % (i, (fr.url or "")[:48], n, bt))
        info = p.evaluate("""() => {
          const out = {inputs: [], buttons: [], forms: 0};
          document.querySelectorAll('input').forEach(e => out.inputs.push({
            type: e.type, name: e.name, id: e.id,
            ph: e.placeholder, aria: e.getAttribute('aria-label'),
            cls: (e.className||'').slice(0,40)}));
          document.querySelectorAll('button, input[type=submit], a[role=button]').forEach(e =>
            out.buttons.push({tag: e.tagName, type: e.type||'',
              text: (e.innerText||e.value||'').trim().slice(0,30),
              id: e.id, cls: (e.className||'').slice(0,40)}));
          out.forms = document.querySelectorAll('form').length;
          return out;
        }""")
        print("   forms: %d" % info["forms"])
        print("   --- inputs ---")
        for x in info["inputs"]:
            print("     type=%-9s name=%-14s id=%-16s ph=%-14s aria=%s"
                  % (x["type"], (x["name"] or "-")[:14], (x["id"] or "-")[:16],
                     (x["ph"] or "-")[:14], (x["aria"] or "-")[:18]))
        print("   --- buttons ---")
        for x in info["buttons"]:
            print("     <%s> text='%s'  id=%s  cls=%s"
                  % (x["tag"], x["text"], (x["id"] or "-")[:18], (x["cls"] or "-")[:34]))
        labels = p.evaluate("""() => Array.from(document.querySelectorAll('label,span,div'))
            .map(e => (e.innerText||'').trim())
            .filter(t => t && t.length < 30 &&
                    /user|name|pass|log|sign|login/i.test(t)).slice(0,10)""")
        print("   --- نصوص ذات صلة ---")
        for t in dict.fromkeys(labels):
            print("     '%s'" % t)
        b.close()
    print("")