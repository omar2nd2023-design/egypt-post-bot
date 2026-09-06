# -*- coding: utf-8 -*-
"""اختبارات حارس النسخة الواحدة لوكيل SMAX.

بيشغّل agent.py الحقيقي كعمليات مستقلة ضد المحاكي المحلي، وبيتأكد إن
النسخة التانية **مابتبعتش ولا نبضة واحدة**.

    python test_agent_guard.py
"""
import json, os, shutil, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
PORT = 8801
BASE = "http://127.0.0.1:%d" % PORT
AGENT_SECRET = "guard-test-secret-1234567890"
os.environ.update({"STUB_PORT": str(PORT), "AGENT_SECRET": AGENT_SECRET,
                   "ADMIN_SECRET": "guard-admin"})
sys.path.insert(0, str(HERE))
import stub_worker  # noqa: E402

FAILED = []
def check(label, got, want):
    ok = got == want
    if not ok: FAILED.append(label)
    print("  %s  %-50s got=%r" % ("PASS" if ok else "FAIL", label, got))

def hb_count():
    with urllib.request.urlopen(BASE + "/__hbcount", timeout=10) as r:
        return json.loads(r.read().decode())["n"]

def start(work, cap=False):
    env = {**os.environ, "WORKER_URL": BASE, "AGENT_SECRET": AGENT_SECRET,
           "AGENT_IDLE_SEC": "2", "PYTHONIOENCODING": "utf-8",
           "AGENT_MUTEX_NAME": "EgyptPost.SmaxAgent.Test.%d" % PORT}
    return subprocess.Popen(
        [sys.executable, "-u", str(work / "agent.py")], cwd=str(work), env=env,
        stdout=subprocess.PIPE if cap else subprocess.DEVNULL,
        stderr=subprocess.STDOUT if cap else subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace")

def main():
    stub_worker.serve(); time.sleep(0.5)
    w1 = Path(tempfile.mkdtemp(prefix="guard1_"))
    w2 = Path(tempfile.mkdtemp(prefix="guard2_"))
    src = HERE / "smax-agent" / "agent.py"
    shutil.copy(src, w1 / "agent.py"); shutil.copy(src, w2 / "agent.py")

    print("=== حارس النسخة الواحدة ===\n")
    p1 = start(w1)
    time.sleep(4)
    n1 = hb_count()
    check("1. النسخة الأولى بتبعت نبضات", n1 > 0, True)

    # نسخة تانية من **مجلد مختلف** — الحارس بالاسم مش بالمسار
    before = hb_count()
    p2 = start(w2, cap=True)
    out, _ = p2.communicate(timeout=30)
    check("2. النسخة التانية خرجت لوحدها", p2.returncode, 3)
    check("3. برسالة واضحة", "نسخة تانية" in (out or ""), True)
    check("3b. الرسالة بتقول إنها مابعتتش", "من غير ما يبعت" in (out or ""), True)
    time.sleep(1)
    after = hb_count()
    check("4. مابعتتش ولا نبضة", after - before <= 1, True)
    print("       (نبضات قبل=%d بعد=%d — الفرق من النسخة الأولى بس)" % (before, after))

    check("5. النسخة الأولى لسه شغّالة", p1.poll() is None, True)
    n_mid = hb_count(); time.sleep(3)
    check("6. ولسه بتبعت", hb_count() > n_mid, True)

    # بعد ما الأولى تقفل، تالتة تقدر تشتغل
    p1.terminate(); p1.wait(timeout=10); time.sleep(1.5)
    p3 = start(w2, cap=True)
    time.sleep(4)
    check("7. بعد قفل الأولى، نسخة جديدة بتشتغل", p3.poll() is None, True)
    n_before3 = hb_count(); time.sleep(3)
    check("8. والجديدة بتبعت نبضات", hb_count() > n_before3, True)
    p3.terminate(); p3.wait(timeout=10)

    # الحارس مالوش أي أثر على ملفات tray_agent
    tray = Path(r"E:\Projects\Egypt Post Reports\Post Report Tool")
    check("9. مافيش agent.pid في مجلد tray_agent",
          (tray / "agent.pid").exists(), False)
    check("10. قفل tray_agent ما اتلمسش",
          (tray / "logs" / "tray_agent.lock").exists(), True)

    shutil.rmtree(w1, ignore_errors=True); shutil.rmtree(w2, ignore_errors=True)
    print("\n" + "=" * 52)
    if FAILED:
        print("❌ فشل %d: %s" % (len(FAILED), ", ".join(FAILED))); return 1
    print("✔ كل اختبارات الحارس نجحت")
    return 0

if __name__ == "__main__":
    sys.exit(main())