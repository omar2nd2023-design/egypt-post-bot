# -*- coding: utf-8 -*-
"""koyeb-egress-test — اختبار وصول (egress) من Koyeb، لا أكثر.

الغرض الوحيد: نعرف هل الـpublic IP بتاع Koyeb مقبول من مواقع مصر الرقمية.

مفيش هنا: Playwright / Chromium / login / password / JWT / cookies /
KV / Turso / Telegram / Cloudflare. ومفيش أي credential — الخدمة
بتعمل GET عادي على 3 دومينات عامة وبس.

  GET /        صفحة معلومات بسيطة (وbealth check لـKoyeb)
  GET /test    بينفّذ الاختبار ويرجّع JSON

المكتبات: stdlib بس — مفيش requirements.txt.
"""
import json
import os
import socket
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8000"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# GET عادي (مش HEAD) عشان نختبر نفس نوع الاتصال اللي التطبيق هيحتاجه بعدين
TARGETS = [
    "https://digital.gov.eg/",
    "https://login.di.gov.eg/",
    "https://apis.digital.gov.eg/",
]

TARGET_TIMEOUT = 12      # ثانية لكل هدف — مايعلّقش الـendpoint
LOOKUP_TIMEOUT = 8


def _host(url):
    return url.split("//", 1)[-1].split("/", 1)[0]


def probe(url):
    """GET على الهدف. بيرجّع status/ms أو نوع الخطأ — من غير ما يرمي."""
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", UA)
        req.add_header("Accept", "text/html,application/json,*/*")
        with urllib.request.urlopen(req, timeout=TARGET_TIMEOUT) as r:
            body = r.read(2048)          # جزء صغير — إثبات إن البيانات بتوصل
            return {"status": r.status,
                    "ms": round((time.monotonic() - t0) * 1000),
                    "bytes": len(body)}
    except urllib.error.HTTPError as e:
        # وصلنا للسيرفر ورد بكود خطأ — ده نجاح من ناحية الـegress
        return {"status": e.code,
                "ms": round((time.monotonic() - t0) * 1000),
                "note": "HTTP error (وصل السيرفر)"}
    except socket.timeout:
        return {"error": "timeout",
                "ms": round((time.monotonic() - t0) * 1000)}
    except urllib.error.URLError as e:
        return {"error": f"urlerror: {str(e.reason)[:120]}",
                "ms": round((time.monotonic() - t0) * 1000)}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:120]}",
                "ms": round((time.monotonic() - t0) * 1000)}


def lookup_egress():
    """الـpublic IP والـASN. لو فشل، مابيوقفش باقي الاختبار."""
    for url in ("https://ipinfo.io/json", "https://ifconfig.co/json"):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "koyeb-egress-test/1.0")
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=LOOKUP_TIMEOUT) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            return {
                "ip": d.get("ip"),
                "asn": d.get("org") or d.get("asn_org") or d.get("asn"),
                "org": d.get("org") or d.get("asn_org"),
                "country": d.get("country") or d.get("country_iso"),
                "region": d.get("region"),
                "city": d.get("city"),
                "source": _host(url),
            }
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:100]}"
            continue
    return {"error": f"IP lookup فشل — {last}"}


def run_test():
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    egress = lookup_egress()          # لو فشل، بنكمّل عادي
    targets = {}
    for url in TARGETS:
        targets[_host(url)] = probe(url)
    reachable = sum(1 for v in targets.values() if "status" in v)
    return {
        "ok": True,
        "started_utc": started,
        "koyeb_region": os.environ.get("KOYEB_REGION"),
        "koyeb_service": os.environ.get("KOYEB_SERVICE_NAME"),
        "egress": egress,
        "targets": targets,
        "summary": {
            "reachable": reachable,
            "total": len(TARGETS),
            "verdict": "PASS" if reachable == len(TARGETS)
                       else ("PARTIAL" if reachable else "FAIL"),
        },
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/test":
            try:
                self._send(run_test())
            except Exception as e:
                self._send({"ok": False,
                            "error": f"{type(e).__name__}: {str(e)[:200]}"}, 500)
        elif path == "/":
            self._send({"ok": True,
                        "service": "koyeb-egress-test",
                        "usage": "GET /test",
                        "targets": [_host(u) for u in TARGETS]})
        else:
            self._send({"ok": False, "error": "not found"}, 404)

    def log_message(self, fmt, *args):
        # لوج مختصر: method + path بس. مفيش headers ولا bodies ولا أي secrets.
        print(f"{self.command} {self.path}", flush=True)


if __name__ == "__main__":
    print(f"listening on 0.0.0.0:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
