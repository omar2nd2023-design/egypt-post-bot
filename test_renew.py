# -*- coding: utf-8 -*-
"""اختبارات دورة التجديد — بمتصفح مزيّف، من غير شبكة ولا بيانات حقيقية.

بنزرع playwright وهمي في sys.modules قبل ما probe يستورده، فـ_do_renew
بيشتغل على صفحات مزيّفة. مفيش أي اتصال خارجي ولا أي credential حقيقي.

    python test_renew.py
"""
import base64
import json
import os
import sys
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "orkestr-f5-test"))

# بيانات وهمية لازم تتحط قبل الاستيراد (probe بيقراها وقت التحميل)
os.environ.update({
    "DE_PHONE": "01000000000", "DE_PASSWORD": "fake-not-real",
    "WORKER_URL": "https://worker.test", "ADMIN_SECRET": "fake-admin",
    "RENEW_SECRET": "fake-renew",
})

FAILED = []


def check(label, got, want):
    ok = got == want
    if not ok:
        FAILED.append(label)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}   got={got!r}")


def mk_jwt(ttl=900, typ="Bearer"):
    now = int(time.time())
    p = {"exp": now + ttl, "iat": now, "azp": "de", "typ": typ,
         "scope": "fnf nid email username"}
    b = base64.urlsafe_b64encode(json.dumps(p).encode()).decode().rstrip("=")
    return "eyJhbGciOiJSUzI1NiJ9." + b + ".sig" + "x" * 80


# ---------------------------------------------------------- متصفح مزيّف
SUBMITISH = ("#kc-login", "input[type='submit']", "button[type='submit']",
             "input[value='التالى']", "input[value='تسجيل الدخول']")


class FakeLocator:
    def __init__(self, n=1, visible=True, page=None, sel=""):
        self._n, self._vis, self._page, self._sel = n, visible, page, sel
    @property
    def first(self): return self
    def count(self): return self._n
    def is_visible(self): return self._vis
    def click(self, **kw):
        s = self._page.s if self._page else {}
        if self._sel in SUBMITISH and s.get("_pwd_filled"):
            s["_done"] = True
            s.get("emit", lambda p: None)(self._page)
    def fill(self, v, **kw):
        if "password" in self._sel:
            (self._page.s if self._page else {})["_pwd_filled"] = True
    def inner_text(self, **kw): return "نص من الصفحة"


class FakePage:
    def __init__(self, scenario, ctx, is_popup=False):
        self.s, self.ctx, self.is_popup = scenario, ctx, is_popup
        self.url = "https://digital.gov.eg/"
        self.closed = False
        self._handlers = []
    def on(self, ev, fn):
        if ev == "request":
            self._handlers.append(fn)
    def goto(self, url, **kw):
        self.url = url
        if self.s.get("portal_raises"):
            raise RuntimeError("net down")
        if "services" in url and self.s.get("_done"):
            self.s.get("emit", lambda p: None)(self)
        return None
    def wait_for_timeout(self, ms): pass
    def content(self): return "<html>x</html>"
    def title(self):
        if self.closed:
            raise RuntimeError("page closed")
        return "مصر الرقمية"
    def locator(self, sel):
        L = lambda n: FakeLocator(n, page=self, sel=sel)
        if "password" in sel:
            return L(1 if self.s.get("password_field", True) else 0)
        if sel in ("#username", "input[name='username']", "input[type='tel']",
                   "input[type='text']"):
            return L(1 if self.s.get("phone_field", True) else 0)
        if "تسجيل الدخول" in sel and sel not in SUBMITISH:
            gone = self.s.get("logged_in_after") and self.s.get("_done")
            return L(0 if gone else 1)
        return L(1)
    def expect_popup(self, **kw):
        page, s = self, self.s
        class CM:
            def __enter__(self_):
                if not s.get("popup", True):
                    raise RuntimeError("no popup")
                self_.p = FakePage(s, page.ctx, is_popup=True)
                page.ctx.pages.append(self_.p)
                for h in page.ctx.page_handlers:
                    h(self_.p)
                return self_
            def __exit__(self_, *a): return False
            @property
            def value(self_): return self_.p
        return CM()
    def close(self): self.closed = True


class FakeRequest:
    def __init__(self, tok):
        self.headers = {"authorization": f"Bearer {tok}"} if tok else {}


class FakeAPIResponse:
    def __init__(self, status, body):
        self.status, self._b = status, body
    @property
    def ok(self): return 200 <= self.status < 300
    def json(self): return self._b


class FakeAPIRequest:
    def __init__(self, s): self.s = s
    def post(self, url, **kw):
        if self.s.get("api_raises"):
            raise RuntimeError("api unreachable")
        return FakeAPIResponse(self.s.get("api_status", 200),
                               {"response": {"itemTrackingRecords": [1, 2, 3]}})


class FakeContext:
    def __init__(self, s):
        self.s, self.pages, self.page_handlers = s, [], []
        self.request = FakeAPIRequest(s)
    def on(self, ev, fn):
        if ev == "page":
            self.page_handlers.append(fn)
    def new_page(self):
        p = FakePage(self.s, self)
        self.pages.append(p)
        return p
    def cookies(self): return self.s.get("cookies", [])


class FakeBrowser:
    def __init__(self, s):
        self.s, self.closed = s, False
        self.version = "130.0.fake"
    def new_context(self, **kw): return FakeContext(self.s)
    def close(self):
        self.closed = True
        self.s["browser_closed"] = True


class FakeChromium:
    def __init__(self, s): self.s = s
    def launch(self, **kw):
        if self.s.get("launch_raises"):
            raise RuntimeError("Cannot allocate memory")
        b = FakeBrowser(self.s)
        self.s["browser"] = b
        return b


class FakePW:
    def __init__(self, s):
        self.s, self.chromium = s, FakeChromium(s)
        self.stopped = False
    def start(self): return self
    def stop(self):
        self.stopped = True
        self.s["pw_stopped"] = True


SCEN = {}
fake_mod = types.ModuleType("playwright")
fake_sync = types.ModuleType("playwright.sync_api")
fake_sync.sync_playwright = lambda: FakePW(SCEN)
fake_mod.sync_api = fake_sync
sys.modules["playwright"] = fake_mod
sys.modules["playwright.sync_api"] = fake_sync

import probe  # noqa: E402


def run(scenario, pushed=True):
    """يشغّل دورة تجديد واحدة على سيناريو، ويرجّع الملخّص الخارجي."""
    SCEN.clear()
    SCEN.update(scenario)
    probe._push_to_worker = lambda tok: (pushed, "fake")
    probe._renew_state.update({"running": False, "result": None,
                               "finished_at": 0.0})
    return probe._do_renew()


def emit_token(tok):
    """بيخلّي الصفحة تطلق طلب فيه Authorization لما تروح /services."""
    def _e(page):
        if True:
            for h in page._handlers:
                h(FakeRequest(tok))
            for p in page.ctx.pages:
                for h in p._handlers:
                    h(FakeRequest(tok))
    return _e


BASE = {"popup": True, "phone_field": True, "password_field": True,
        "logged_in_after": True, "emit_now": True,
        "emit": emit_token(mk_jwt()), "api_status": 200}

print("\n--- 1) المسار السعيد ---")
r = run(dict(BASE))
check("ok", r["ok"], True)
check("login_success", r["login_success"], True)
check("token_observed", r["token_observed"], True)
check("api_status", r["api_status"], 200)
check("المتصفح اتقفل", SCEN.get("browser_closed"), True)
check("playwright اتوقّف", SCEN.get("pw_stopped"), True)
s = probe._renew_summary(r)
check("الملخّص مفيهوش توكن", "token" in s, False)
check("مفاتيح الملخّص", sorted(k for k in s if k in
      ("success", "token_available", "expires_at", "elapsed_ms",
       "failure_reason")),
      ["elapsed_ms", "expires_at", "failure_reason", "success",
       "token_available"])

print("\n--- 2) فشل الدخول: مفيش توكن والفورم لسه مفتوح ---")
r = run({**BASE, "emit": lambda p: None, "emit_now": False,
         "logged_in_after": False})
check("ok", r["ok"], False)
check("login_success مش True", r["login_success"], False)
check("السبب", r["failure_reason"], "login_not_confirmed")
check("المتصفح اتقفل", SCEN.get("browser_closed"), True)

print("\n--- 3) فشل الـAPI (500) ---")
r = run({**BASE, "api_status": 500})
check("ok", r["ok"], False)
check("token_observed لسه True", r["token_observed"], True)
check("السبب", r["failure_reason"], "api_status_500")
check("المتصفح اتقفل", SCEN.get("browser_closed"), True)

print("\n--- 4) الـAPI رمى استثناء ---")
r = run({**BASE, "api_raises": True})
check("ok", r["ok"], False)
check("api_error اتسجّل", "api_error" in r, True)
check("المتصفح اتقفل", SCEN.get("browser_closed"), True)

print("\n--- 5) حقل الموبايل مش موجود ---")
r = run({**BASE, "phone_field": False})
check("السبب", r["failure_reason"], "phone_field_not_found")
check("المتصفح اتقفل", SCEN.get("browser_closed"), True)

print("\n--- 6) فشل تشغيل المتصفح (ذاكرة) ---")
r = run({**BASE, "launch_raises": True})
check("ok", r["ok"], False)
check("اتصنّف memory_failure", r["failure_reason"], "memory_failure")

print("\n--- 7) الرفع للـWorker فشل ---")
r = run(dict(BASE), pushed=False)
check("ok", r["ok"], False)
check("السبب", r["failure_reason"], "worker_push_failed")

print("\n--- 8) بيانات دخول ناقصة ---")
_p, _w = probe.DE_PHONE, probe.DE_PASSWORD
probe.DE_PHONE, probe.DE_PASSWORD = "", ""
r = run(dict(BASE))
check("السبب", r["failure_reason"], "missing_credentials")
check("ماشغّلش متصفح أصلاً", SCEN.get("browser") is None, True)
probe.DE_PHONE, probe.DE_PASSWORD = _p, _w

print("\n--- 9) تجديد متزامن: 8 خيوط → دخول واحد ---")
SCEN.clear()
SCEN.update(BASE)
probe._push_to_worker = lambda tok: (True, "fake")
probe._renew_state.update({"running": False, "result": None,
                           "finished_at": 0.0})
runs = {"n": 0}
orig = probe._do_renew


def slow():
    runs["n"] += 1
    time.sleep(0.4)
    return {"ok": True, "token_observed": True, "elapsed_ms": 400,
            "token": {"exp_in_sec": 880}}


probe._do_renew = slow
out = []
threads = [threading.Thread(target=lambda: out.append(probe.renew_token()))
           for _ in range(8)]
for t in threads: t.start()
for t in threads: t.join()
check("دخول واحد بس", runs["n"], 1)
check("الكل خد نتيجة", len(out), 8)
check("الكل نجح", all(o.get("ok") for o in out), True)
check("فيه واللي انضموا", sum(1 for o in out if o.get("joined")) >= 1, True)

print("\n--- 10) منع التكرار القريب ---")
runs["n"] = 0
probe.renew_token()
check("استعمل نتيجة قريبة بدل دخول جديد", runs["n"], 0)
probe._do_renew = orig

print("\n--- 11) الملخّص عند الفشل مافيهوش أي تفاصيل داخلية ---")
s = probe._renew_summary({"ok": False, "failure_reason": "login_not_confirmed",
                          "post_login": {"form_message": "سرّي"},
                          "token": {"exp_in_sec": 100}, "elapsed_ms": 5})
check("success", s["success"], False)
check("مفيش post_login", "post_login" in s, False)
blob = json.dumps(s, ensure_ascii=False)
check("مفيش أي نص داخلي", "سرّي" in blob, False)

print("\n--- 12) بيانات الدخول من البيئة بس ---")
src = open(os.path.join(HERE, "orkestr-f5-test", "probe.py"),
           encoding="utf-8").read()
for name in ("DE_PHONE", "DE_PASSWORD", "WORKER_URL", "ADMIN_SECRET",
             "RENEW_SECRET"):
    check(f"{name} من os.environ",
          f'{name} = os.environ.get("{name}"' in src, True)

print("\n" + ("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}"))
sys.exit(0 if not FAILED else 1)
