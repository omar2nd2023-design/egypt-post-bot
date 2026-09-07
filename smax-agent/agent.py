# -*- coding: utf-8 -*-
"""وكيل بوابة SMAX - المرحلة 1: النبض والحالة فقط.

⚠️ المرحلة دي **مافيهاش** متصفح ولا Playwright ولا طابور مهام ولا
   دخول على SMAX. الغرض الوحيد إثبات السلسلة:

       الجهاز  --HTTPS صادر-->  Worker  -->  agent_state

الوكيل بيبعت حقائق بس. **الـWorker هو اللي بيقرر** الحالة
(ONLINE / OFFLINE) - الوكيل عمره ما بيقول عن نفسه إنه offline،
لأنه لو مقفول مش هيقدر يقول حاجة أصلاً.

الاتصال **صادر بس**: مافيش بورت مفتوح على الجهاز ولا تغيير راوتر.

التشغيل:
    set AGENT_SECRET=...        (أو ملف agent.env جنب الملف)
    set WORKER_URL=https://...
    python agent.py

الإيقاف: Ctrl+C  (أو إنهاء العملية).
"""
import io
import json
import os
import platform
import random
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
SESSION_FILE = HERE / "smax_session.json"
# متصفحات Playwright جوه E:\Projects (مش C:) — عشان تنزيل ويندوز جديد مايكسرش
# حاجة (طلب المستخدم 2026-09-05). لو المجلد مش موجود بنسيب الافتراضي.
_PW = Path(r"E:\Projects\Tools\playwright")
if _PW.exists():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_PW))
SMAX_ENV = Path(os.environ.get("SMAX_ENV_FILE",
                                r"E:\Projects\ComplaintsBot\Smax_V11\.env"))
ID_FILE = HERE / "agent_id.txt"
ENV_FILE = HERE / "agent.env"
LOG_FILE = HERE / "agent.log"

# النبض المتكيّف: أسرع بعد أي نشاط، وبيهدى في السكون.
# المرحلة 1 مافيهاش نشاط، فالمعدّل هيفضل IDLE - بس الآلية جاهزة
# للمرحلة 2 من غير تغيير في البنية.
# القيم دي قابلة للضبط من البيئة عشان الاختبارات ماتستناش دقايق.
# الافتراضي هو المستخدم في التشغيل الحقيقي.
ACTIVE_SEC = float(os.environ.get("AGENT_ACTIVE_SEC", "5"))
IDLE_SEC = float(os.environ.get("AGENT_IDLE_SEC", "10"))   # كان 20 — تحسين 2026-09-06
ACTIVE_WINDOW_SEC = float(os.environ.get("AGENT_ACTIVE_WINDOW_SEC", "120"))
HTTP_TIMEOUT = float(os.environ.get("AGENT_HTTP_TIMEOUT", "20"))
# اسم الـMutex قابل للتغيير من البيئة **للاختبارات بس** — عشان اختبار
# يشتغل والوكيل الإنتاجي شغّال على نفس الجهاز. الافتراضي هو الإنتاج.
MUTEX_NAME = os.environ.get("AGENT_MUTEX_NAME", "EgyptPost.SmaxAgent.SingleInstance")

# ⚠️ الأسرار **ماتتطبعش** أبداً. الدالة دي بتتنده على أي نص رايح للوج.
_SECRETS = []


def redact(s):
    out = str(s)
    for sec in _SECRETS:
        if sec and len(sec) >= 6 and sec in out:
            out = out.replace(sec, "***")
    return out


def log(msg):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), redact(msg))
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_env():
    """agent.env جنب الملف - سطور KEY=VALUE. مايدخلش git."""
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            k, v = raw.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def agent_id():
    """معرّف ثابت. إعادة التشغيل **بتستخدم نفس المعرّف** فمافيش صفوف مكررة."""
    if ID_FILE.exists():
        v = ID_FILE.read_text(encoding="utf-8").strip()
        if v:
            return v
    v = "agent-" + uuid.uuid4().hex[:12]
    ID_FILE.write_text(v, encoding="utf-8")
    return v


class SingleInstance:
    """حارس نسخة واحدة — Mutex مُسمّى من نواة ويندوز.

    ليه Mutex مش ملف قفل:
      • النواة بتحرّره **تلقائيًا** لو العملية ماتت أو اتقفلت بالعافية،
        فمافيش حالة "قفل بايت" محتاجة تنضيف زي اللي حصلت في tray_agent.
      • مافيش سباق بين قراية الملف وكتابته.

    بيجرّب Global أول (كل الجهاز)، ولو الصلاحية مش متاحة بيقع على
    Local (نفس جلسة المستخدم). ولو ctypes مش متاح خالص بيقع على ملف
    PID — عشان يفضل فيه حارس في كل الأحوال.

    مافيش مكتبات جديدة: ctypes من المكتبة القياسية.
    """
    _ALREADY_EXISTS = 183          # ERROR_ALREADY_EXISTS

    def __init__(self):
        self.held = False
        self.scope = "none"
        self._h = None
        self._pidfile = None
        if not self._try_mutex():
            self._try_pidfile()

    def _try_mutex(self):
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return False
        try:
            k = ctypes.windll.kernel32
            k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                       wintypes.LPCWSTR]
            k.CreateMutexW.restype = wintypes.HANDLE
        except Exception:
            return False
        for scope in ("Global", "Local"):
            name = "%s\\%s" % (scope, MUTEX_NAME)
            try:
                h = k.CreateMutexW(None, True, name)
                err = k.GetLastError()
            except Exception:
                continue
            if not h:
                continue          # الصلاحية مرفوضة — نجرّب النطاق اللي بعده
            self._h = h
            self.scope = scope
            self.held = (err != self._ALREADY_EXISTS)
            return True
        return False

    def _try_pidfile(self):
        """احتياطي لو الـAPI مش متاح. بيفحص إن العملية عايشة فعلاً."""
        f = HERE / "agent.pid"
        self._pidfile = f
        self.scope = "pidfile"
        try:
            if f.exists():
                try:
                    other = int((f.read_text(encoding="utf-8") or "0").strip())
                except Exception:
                    other = 0
                if other and other != os.getpid() and _pid_alive(other):
                    self.held = False
                    return
            f.write_text(str(os.getpid()), encoding="utf-8")
            self.held = True
        except Exception:
            self.held = True      # مش عارفين؟ ماتوقفش التشغيل

    def release(self):
        try:
            if self._h:
                import ctypes
                ctypes.windll.kernel32.ReleaseMutex(self._h)
                ctypes.windll.kernel32.CloseHandle(self._h)
        except Exception:
            pass
        try:
            if self._pidfile and self.held and self._pidfile.exists():
                self._pidfile.unlink()
        except Exception:
            pass


def _pid_alive(pid):
    """هل العملية دي لسه عايشة؟ (للاحتياطي بس)"""
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    except Exception:
        return True


class Agent:
    def __init__(self, worker_url, secret, aid):
        self.base = worker_url.rstrip("/")
        self.secret = secret
        self.aid = aid
        self.stop = threading.Event()
        self._last_active = 0.0
        self.sent = 0
        self.failed = 0
        self.last_error = ""

    def mark_active(self):
        """المرحلة 2 هتنده دي لما تستلم شغل - عشان الإيقاع يسرّع."""
        self._last_active = time.time()

    def interval(self):
        if time.time() - self._last_active < ACTIVE_WINDOW_SEC:
            base = ACTIVE_SEC
        else:
            base = IDLE_SEC
        # تشويش بسيط عشان لو فيه أكتر من وكيل مايتزامنوش على نفس اللحظة
        return base + random.uniform(0, base * 0.15)

    # ---------------- المرحلة 2: المتصفح والمهام ----------------
    _pw = None
    _browser = None
    _ctx = None
    _page = None
    smax_status = "starting"
    _creds = None

    def _load_creds(self):
        if self._creds is not None:
            return self._creds
        d = {}
        try:
            for line in io.open(SMAX_ENV, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    d[k.strip()] = v.strip()
        except Exception:
            pass
        u, p = d.get("SMAX_SITE_USER", ""), d.get("SMAX_SITE_PASSWORD", "")
        if u and p:
            _SECRETS.extend([u, p])
            self._creds = (u, p)
        else:
            self._creds = ("", "")
            self.smax_status = "not_configured"
        return self._creds

    def _ensure_browser(self):
        """متصفح واحد دائم. لو وقع بنعيد فتحه في المهمة اللي بعدها."""
        if self._page is not None:
            try:
                self._page.evaluate("() => 1")
                return self._page
            except Exception:
                self._close_browser()
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        kw = {"locale": "ar-EG", "viewport": {"width": 1600, "height": 950}}
        if SESSION_FILE.exists():
            kw["storage_state"] = str(SESSION_FILE)
        self._ctx = self._browser.new_context(**kw)
        self._page = self._ctx.new_page()
        return self._page

    def _close_browser(self):
        for f in (lambda: self._browser and self._browser.close(),
                  lambda: self._pw and self._pw.stop()):
            try:
                f()
            except Exception:
                pass
        self._pw = self._browser = self._ctx = self._page = None

    def execute(self, job):
        """ينفّذ مهمة واحدة ويرجّع (ok, result_or_error)."""
        import smax_search as S
        user, pwd = self._load_creds()
        if not user:
            return False, "smax_credentials_missing"
        bc = str(job.get("barcode") or "").strip().upper()
        nid = str(job.get("national_id") or "").strip() or None
        try:
            page = self._ensure_browser()
            how = S.ensure_session(page, user, pwd, log=log)
            if how == "logged_in":
                try:
                    self._ctx.storage_state(path=str(SESSION_FILE))
                    log("جلسة جديدة اتحفظت")
                except Exception:
                    pass
            self.smax_status = "ready"
            res = self._search_with_retry(page, bc, nid, log)
            # المرحلة 3: تحليل الشكوى بأدوات Smax_V11 (معزول — فشله مابيأثرش)
            if isinstance(res, dict) and res.get("found"):
                job = self._with_fresh_track(job, log)
                try:
                    import smax_analysis as A
                    a = A.analyze(res, job, log=log)
                    if a:
                        res["analysis"] = a
                        log("   تحليل: %s · تأخير %s يوم · رد %s" % (
                            a.get("issue_type"), a.get("delay_days"),
                            (a.get("reply") or {}).get("state")))
                except Exception as e:
                    log("   تحليل الشكوى اتخطّى: %s" % type(e).__name__)
            return True, res
        except Exception as e:
            self.smax_status = "error"
            self._close_browser()
            return False, redact("%s: %s" % (type(e).__name__, e))[:200]

    def _search_with_retry(self, page, bc, nid, log):
        """2026-09-06 18:44: «تعذّر البحث» وصلت للمستخدم بسبب عطل واجهة SMAX
        (filter_button_hidden ثم Locator.click timeout) — مش بسبب الشكوى.
        عطل الواجهة بيتصلّح بإعادة تحميل الشبكة، فبنعيد المحاولة **مرة واحدة**
        بعد إعادة التحميل قبل ما نبلّغ فشل."""
        import smax_search as S          # زي execute — الاستيراد محلي مش على مستوى الملف
        for attempt in (1, 2):
            try:
                res = S.search_and_extract(page, bc, request_no=None,
                                           national_id=nid, log=log)
                infra = isinstance(res, dict) and not res.get("found") and bool(
                    res.get("error") or res.get("tried") == "baseline_failed")
                if not infra or attempt == 2:
                    return res
                log("   البحث وقع في عطل واجهة (%s) — إعادة تحميل ومحاولة تانية"
                    % (res.get("error") or res.get("tried")))
            except Exception as e:
                if attempt == 2:
                    raise
                log("   البحث رمى %s — إعادة تحميل ومحاولة تانية" % type(e).__name__)
            # 🔴 2026-09-07 14:39: بعد إعادة التحميل المحاولة التانية وقعت على
            #    زرار «Add filter» (30 ث) — الشبكة كانت لسه مش جاهزة. بنستنى
            #    شريط الفلاتر نفسه يبان، ولو الصفحة رجعت للدخول بنسجّل.
            try:
                page.goto(S.GRID_URL, wait_until="domcontentloaded", timeout=60000)
                how = S._wait_grid_or_login(page)
                if how == "login":
                    user, pwd = self._load_creds()
                    S.ensure_session(page, user, pwd, log=log)
                try:
                    page.locator('a[title="Add filter"]').first.wait_for(state="visible", timeout=20000)
                except Exception:
                    log("   شريط الفلاتر مابانش بعد إعادة التحميل — تحميل تاني")
                    page.goto(S.GRID_URL, wait_until="domcontentloaded", timeout=60000)
                    S._wait_grid_or_login(page)
                    page.wait_for_timeout(3000)
            except Exception:
                pass

    def payload(self):
        return {
            "agent_id": self.aid,
            "agent_status": "running",
            "smax_status": self.smax_status,
            "version": "phase2",
            "host_os": platform.system(),
        }

    def _get(self, path):
        req = urllib.request.Request(
            self.base + path, method="GET",
            headers={"Authorization": "Bearer " + self.secret,
                     "User-Agent": "smax-agent/2.0"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))

    def _with_fresh_track(self, job, log, max_wait=45):
        """2026-09-07 (المدير): مهمة SMAX بقت تتعمل **قبل** التتبّع الحيّ عشان
        الملفات تتعرض فورًا والباقي يمشي بالتوازي. بوّابة Smax_V11 محتاجة أحداث
        الرحلة، فقبل التحليل بنجيب أحدث track من الـWorker ونستنّى لحد ما
        التتبّع يوصل (بحد أقصى 45 ث — البحث نفسه بياخد أكتر من كده عادةً)."""
        import urllib.parse
        jid = str(job.get("job_id") or "")
        if not jid:
            return job
        t0 = time.time()
        while True:
            try:
                code, j = self._get("/agent/track?job_id=" + urllib.parse.quote(jid, safe=""))
                if code == 200 and isinstance(j, dict):
                    if j.get("track"):
                        job = dict(job, track=j["track"])
                    if j.get("tracking_ready"):
                        return job
            except Exception:
                pass
            if time.time() - t0 > max_wait:
                log("   التتبّع الحيّ ماوصلش خلال %d ث — التحليل بأحداث ناقصة" % max_wait)
                return job
            time.sleep(3)

    def _post(self, path, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=body, method="POST",
            headers={"Authorization": "Bearer " + self.secret,
                     "Content-Type": "application/json",
                     "User-Agent": "smax-agent/2.0"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))

    def beat(self):
        """الاستعلام هو النبضة: /agent/poll بيسجّل الحالة وبيرجّع مهمة لو فيه."""
        code, j = self._post("/agent/poll", self.payload())
        job = j.get("job") if isinstance(j, dict) else None
        # 2026-09-06: لو فيه مهمة تانية مستنية في الطابور نبدأها **فورًا** بعد
        # تسليم النتيجة، من غير تجهيز الأساس (الأساس بيتصلّح في أول المهمة أصلًا).
        # كان التجهيز (~36 ث) بيتحسب على الشحنة اللي مستنية — شفناه: انتظار 1:16.
        while job:
            self.mark_active()
            corr = job.get("corr_id", "?")
            log("%s مهمة اتاستلمت: %s" % (corr, job.get("job_id")))
            t0 = time.time()
            ok, res = self.execute(job)
            log("%s خلصت في %.0f ث — %s" % (corr, time.time() - t0,
                "لقيت" if ok and isinstance(res, dict) and res.get("found")
                else ("مالقتش" if ok else "فشل")))
            body = {"job_id": job["job_id"], "ok": ok}
            if ok:
                body["result"] = res
            else:
                body["error"] = str(res)[:300]
            try:
                self._post("/agent/result", body)
            except Exception as e:
                log("%s تعذّر تسليم النتيجة: %s" % (corr, type(e).__name__))
            # 🔴 استعلام تاني فورًا: لو فيه مهمة مستنية نبدأها على طول.
            #    (خطأ 14:51 — الحلقة كانت بتعيد نفس المهمة بلا نهاية لأن `job`
            #    ماكانش بيتجدّد. المهمة اللي في تليجرام فضلت مستنية 20 دقيقة.)
            try:
                code, j = self._post("/agent/poll", self.payload())
            except Exception:
                code, j = 0, {}
            job = j.get("job") if isinstance(j, dict) else None
            if job:
                continue
            # مافيش مهمة مستنية: نجهّز الشبكة والأساس للمهمة الجاية **بعد**
            # ما النتيجة اتسلّمت — التكلفة دي كانت على حساب وقت المستخدم.
            try:
                if self._page is not None:
                    import smax_search as S
                    S.prepare_next(self._page, log=log)
            except Exception:
                pass
        return code, j

    def run(self):
        log("الوكيل بدأ - المعرّف %s" % self.aid)
        log("الـWorker: %s" % self.base)
        while not self.stop.is_set():
            try:
                code, j = self.beat()
                self.sent += 1
                self.last_error = ""
                log("نبضة #%d -> HTTP %d  الحالة عند الـWorker: %s"
                    % (self.sent, code, j.get("status", "?")))
            except urllib.error.HTTPError as e:
                self.failed += 1
                self.last_error = "HTTP %s" % e.code
                # ⚠️ 401 معناه سر غلط. بنقول كده من غير ما نطبع السر.
                log("نبضة فشلت: HTTP %s%s" % (
                    e.code, "  (السر مرفوض)" if e.code == 401 else ""))
            except Exception as e:
                self.failed += 1
                self.last_error = type(e).__name__
                log("نبضة فشلت: %s: %s" % (type(e).__name__, redact(e)))
            self.stop.wait(self.interval())
        log("الوكيل وقف - نبضات ناجحة %d / فاشلة %d" % (self.sent, self.failed))


def main():
    # 🔴 الحارس **قبل أي حاجة تانية** — قبل قراءة الأسرار وقبل أي نبضة.
    #    نسخة تانية بتخرج من غير ما تبعت ولا طلب واحد للـWorker.
    guard = SingleInstance()
    if not guard.held:
        msg = ("نسخة تانية من وكيل SMAX شغّالة على الجهاز ده "
               "(%s) — بيخرج من غير ما يبعت أي نبضة." % guard.scope)
        print(msg, flush=True)
        log(msg)
        return 3

    load_env()
    worker = (os.environ.get("WORKER_URL") or "").strip()
    secret = (os.environ.get("AGENT_SECRET") or "").strip()
    if not worker or not secret:
        print("ناقص WORKER_URL أو AGENT_SECRET "
              "(متغيّر بيئة أو ملف agent.env جنب الملف).")
        return 2
    _SECRETS.append(secret)

    a = Agent(worker, secret, agent_id())

    def _sig(*_):
        log("إشارة إيقاف - بيقفل")
        a.stop.set()
    try:
        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)
    except Exception:
        pass

    log("حارس النسخة الواحدة: %s" % guard.scope)
    try:
        a.run()
    finally:
        a._close_browser()
        guard.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())