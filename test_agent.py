# -*- coding: utf-8 -*-
"""اختبارات قبول المرحلة 1 — الوكيل الحقيقي على HTTP فعلي.

بيشغّل agent.py كعملية مستقلة ضد stub_worker المحلي (نفس عقد الـWorker
بالحرف). مافيش mock للوكيل — دي سلسلة حقيقية:

    agent.py  --HTTP-->  stub_worker  -->  agent_state

    python test_agent.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent

PORT = 8799
BASE = "http://127.0.0.1:%d" % PORT
AGENT_SECRET = "test-agent-secret-abc123xyz"
ADMIN_SECRET = "test-admin-secret-def456uvw"

os.environ["STUB_PORT"] = str(PORT)
os.environ["AGENT_SECRET"] = AGENT_SECRET
os.environ["ADMIN_SECRET"] = ADMIN_SECRET
sys.path.insert(0, str(HERE))
import stub_worker  # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    if not ok:
        FAILED.append(label)
    print("  %s  %s   got=%r" % ("PASS" if ok else "FAIL", label, got))


def call(path, secret, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Authorization": "Bearer " + secret,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None


def status():
    _, j = call("/agent/status", ADMIN_SECRET)
    return (j or {}).get("agents", [])


def skew(sec):
    urllib.request.urlopen(urllib.request.Request(
        BASE + "/__skew", data=json.dumps({"sec": sec}).encode(),
        method="POST"), timeout=10).read()


def rows():
    with urllib.request.urlopen(BASE + "/__count", timeout=10) as r:
        return json.loads(r.read().decode())["rows"]


def wait_for(fn, timeout=25, step=0.5):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return None


def start_agent(workdir, secret=AGENT_SECRET, tries=4):
    """يشغّل الوكيل ويتأكد إنه فعلاً قام.

    ⚠️ حارس النسخة الواحدة على مستوى الجهاز (Mutex مُسمّى). لو اختبار
       تاني لسه بيقفل عملياته، الوكيل بتاعنا هيخرج بكود 3 فورًا.
       ده **سلوك صح** من الحارس، بس بيخلي الاختبار متقطّع — فبنستنى
       ونعيد المحاولة بدل ما نبني على سباق.
    """
    env = {**os.environ,
           "WORKER_URL": BASE, "AGENT_SECRET": secret,
           "AGENT_IDLE_SEC": "2", "AGENT_ACTIVE_SEC": "2",
           "AGENT_MUTEX_NAME": "EgyptPost.SmaxAgent.Test.%d" % PORT,
           "PYTHONIOENCODING": "utf-8"}
    for i in range(tries):
        p = subprocess.Popen(
            [sys.executable, "-u", str(workdir / "agent.py")],
            cwd=str(workdir), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.2)
        if p.poll() is None:
            return p
        if p.returncode == 3 and i < tries - 1:
            time.sleep(2)          # نسخة تانية لسه بتقفل — نستنى
            continue
        return p
    return p


def main():
    stub_worker.serve()
    time.sleep(0.5)
    work = Path(tempfile.mkdtemp(prefix="agent_test_"))
    shutil.copy(HERE / "smax-agent" / "agent.py", work / "agent.py")

    print("=== المرحلة 1 — اختبارات القبول ===\n")

    # 1) الوكيل يبدأ -> الـWorker يسجّله ONLINE
    p = start_agent(work)
    got = wait_for(lambda: status() or None)
    check("1. الوكيل بدأ -> الـWorker سجّله",
          bool(got) and got[0]["status"].startswith("ONLINE"), True)
    aid1 = got[0]["agent_id"] if got else None
    check("1b. الحالة 🟡 (SMAX مش جاهزة في المرحلة 1)",
          got[0]["status"] if got else None, "ONLINE_SMAX_NOT_READY")

    # 2) النبض مستمر -> يفضل ONLINE
    first_age = got[0]["age_sec"]
    time.sleep(5)
    g2 = status()
    check("2. النبض مستمر -> لسه ONLINE",
          g2[0]["status"].startswith("ONLINE"), True)
    check("2b. الـage بيتجدّد (نبضة جديدة وصلت)",
          g2[0]["age_sec"] <= max(first_age, 4), True)

    # 3) نوقف الوكيل -> بعد 90 ثانية يبقى OFFLINE
    p.terminate(); p.wait(timeout=10)
    time.sleep(1)
    skew(100)                      # ساعة مزاحة بدل ما نستنى 90 ثانية
    g3 = status()
    check("3. الوكيل واقف + عدّى 100 ث -> OFFLINE", g3[0]["status"], "OFFLINE")
    check("3b. السبب مسجّل", g3[0]["reason"], "stale_heartbeat")

    # 4) نشغّله تاني -> يرجع ONLINE
    skew(0)
    p2 = start_agent(work)
    g4 = wait_for(lambda: [a for a in status()
                           if a["status"].startswith("ONLINE")] or None)
    check("4. اشتغل تاني -> رجع ONLINE", bool(g4), True)

    # 5 + 6) إعادة التشغيل مابتعملش صف تاني، ونفس المعرّف
    check("5. مافيش صفوف مكررة بعد إعادة التشغيل", rows(), 1)
    check("6. نفس agent_id بعد إعادة التشغيل",
          g4[0]["agent_id"] if g4 else None, aid1)
    p2.terminate(); p2.wait(timeout=10)

    # 7) سر غلط -> 401
    code, _ = call("/agent/heartbeat", "wrong-secret", "POST",
                   {"agent_id": "intruder"})
    check("7. سر غلط -> 401", code, 401)
    check("7b. الدخيل مادخلش الجدول", rows(), 1)
    code2, _ = call("/agent/status", "wrong-secret")
    check("7c. /agent/status بسر غلط -> 401", code2, 401)

    # 8 + 9) اللوج نضيف
    logf = work / "agent.log"
    text = logf.read_text(encoding="utf-8") if logf.exists() else ""
    check("8. مافيش AGENT_SECRET في اللوج", AGENT_SECRET in text, False)
    bad = {
        "JWT": r"eyJ[A-Za-z0-9_-]{10,}\.",
        "كوكي": r"(?i)(TS01[0-9a-f]{6}|JSESSIONID|set-cookie)",
        "باسورد": r"(?i)(password|passwd)\s*[=:]\s*\S",
        "رقم قومي": r"\b[23]\d{13}\b",
    }
    for name, pat in bad.items():
        check("9. مافيش %s في اللوج" % name, bool(re.search(pat, text)), False)

    # 8 + 9) الحالة بتتحسب من الوقت + smax_status، مش من إبلاغ الوكيل
    call("/agent/heartbeat", AGENT_SECRET, "POST",
         {"agent_id": "probe-agent", "smax_status": "ready",
          "agent_status": "running"})
    st = {a["agent_id"]: a for a in status()}
    check("8. smax=ready + نبضة حديثة -> ONLINE",
          st["probe-agent"]["status"], "ONLINE")
    call("/agent/heartbeat", AGENT_SECRET, "POST",
         {"agent_id": "probe-agent", "smax_status": "not_configured"})
    st = {a["agent_id"]: a for a in status()}
    check("8b. smax مش ready + نبضة حديثة -> 🟡",
          st["probe-agent"]["status"], "ONLINE_SMAX_NOT_READY")

    call("/agent/heartbeat", AGENT_SECRET, "POST",
         {"agent_id": "probe-agent", "smax_status": "ready"})
    skew(200)
    st = {a["agent_id"]: a for a in status()}
    check("9. نبضة قديمة + آخر smax=ready -> OFFLINE",
          st["probe-agent"]["status"], "OFFLINE")
    skew(0)

    # 10) الحالة بتتحسب في الـWorker مش في الوكيل
    code, j = call("/agent/heartbeat", AGENT_SECRET, "POST",
                   {"agent_id": "probe-agent", "smax_status": "not_configured",
                    "agent_status": "running"})
    check("10. الـWorker بيرجّع الحالة اللي حسبها هو",
          (j or {}).get("status"), "ONLINE_SMAX_NOT_READY")

    # 11) تحقق صارم على المحاكي كمان
    for label, payload, want in [
        ("smax_status غلط", {"agent_id": "z", "smax_status": "nope"}, 400),
        ("حقل زيادة", {"agent_id": "z", "smax_status": "ready", "x": 1}, 400),
        ("من غير agent_id", {"smax_status": "ready"}, 400),
    ]:
        c, _ = call("/agent/heartbeat", AGENT_SECRET, "POST", payload)
        check("11. %s -> 400" % label, c, want)

    # 12) حدود الـschema: 001 بتعرّف agent_state بس، وsmax_jobs عايشة في 002
    #     (المرحلة 2) — الـWorker بيستخدمها فعلاً.
    ddl = (HERE / "migrations" / "001_agent_state.sql").read_text(encoding="utf-8")
    # ⚠️ بندوّر على **تعريفات الجداول** مش على ورود الكلمة — الملف فيه
    #    تعليق بيقول إن smax_jobs مش هنا عن قصد، وده مش تعريف جدول.
    tables = set(re.findall(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
                            ddl, re.I))
    check("12. الـschema 001 بتعرّف agent_state بس", sorted(tables), ["agent_state"])
    ddl2 = (HERE / "migrations" / "002_smax_jobs.sql").read_text(encoding="utf-8")
    tables2 = set(re.findall(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
                             ddl2, re.I))
    check("12b. الـschema 002 بتعرّف smax_jobs بس", sorted(tables2), ["smax_jobs"])
    wsrc = (HERE / "src" / "worker.js").read_text(encoding="utf-8")
    check("12c. worker.js بيستخدم smax_jobs (المرحلة 2)", "smax_jobs" in wsrc, True)

    shutil.rmtree(work, ignore_errors=True)
    print("\n" + "=" * 52)
    if FAILED:
        print("❌ فشل %d: %s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("✔ كل الاختبارات نجحت")
    return 0


if __name__ == "__main__":
    sys.exit(main())