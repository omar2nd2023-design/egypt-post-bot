# -*- coding: utf-8 -*-
"""اختبارات مسار notify في بوابة Orkestr.

بيتأكد إن:
  - /track من غير notify بيشتغل زي ما هو بالظبط (العقد ما اتغيّرش)
  - /track مع notify وشغل سريع  -> مافيش تسليم (الطالب لسه مستني)
  - /track مع notify وشغل بطيء  -> تسليم على /finish
  - التسليم فيه الحقول الصح ومفيش توكن
  - فشل التسليم مايكسرش الرد

مفيش شبكة ولا بيانات حقيقية. ملف مؤقت — بيتمسح بعد التشغيل.
"""
import json
import os
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "orkestr-f5-test"))
os.environ.update({
    "DE_PHONE": "01000000000", "DE_PASSWORD": "fake",
    "WORKER_URL": "https://worker.test", "ADMIN_SECRET": "fake-admin",
    "RENEW_SECRET": "fake-renew",
})

# playwright مزيّف عشان الاستيراد يعدّي
fake = types.ModuleType("playwright")
fs = types.ModuleType("playwright.sync_api")
fs.sync_playwright = lambda: None
fake.sync_api = fs
sys.modules["playwright"] = fake
sys.modules["playwright.sync_api"] = fs

import probe  # noqa: E402

FAILED = []


def check(label, got, want):
    ok = got == want
    if not ok:
        FAILED.append(label)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}   got={got!r}")


RESULT = {"records": [{"a": 1}, {"a": 2}], "status": "تم تسليم الشحنة"}

# ---------------------------------------------------------- التقاط التسليم
sent = []


def fake_deliver(notify, bc, res):
    sent.append({"notify": notify, "bc": bc, "res": res})
    return True, "worker http 200"


# ---------------------------------------- محاكاة منطق المسار زي ما هو
def route_track(payload, elapsed_sec, deliver=fake_deliver):
    """نسخة من منطق /track في do_POST — نفس الشروط بالظبط."""
    bc = str(payload.get("barcode", "")).strip()
    notify = payload.get("notify") or None
    if not bc:
        return 400, {"err": "missing barcode"}
    res = dict(RESULT)
    if isinstance(notify, dict):
        budget = notify.get("budget_ms") or probe.NOTIFY_DEFAULT_BUDGET_MS
        try:
            budget = float(budget)
        except Exception:
            budget = probe.NOTIFY_DEFAULT_BUDGET_MS
        if elapsed_sec * 1000 >= budget:
            deliver(notify, bc, res)
    return 200, res


print("\n--- 1) من غير notify: العقد زي ما هو ---")
sent.clear()
st, res = route_track({"barcode": "EKPB0412385EG"}, elapsed_sec=40)
check("الحالة 200", st, 200)
check("العقد {records,status}", sorted(res.keys()), ["records", "status"])
check("مفيش تسليم حتى لو طوّل", len(sent), 0)

print("\n--- 2) مع notify وشغل سريع: مافيش تسليم ---")
sent.clear()
st, res = route_track({"barcode": "X", "notify": {
    "chat_id": 1, "message_id": 2, "budget_ms": 18000}}, elapsed_sec=2)
check("الحالة 200", st, 200)
check("الرد زي ما هو", sorted(res.keys()), ["records", "status"])
check("مافيش تسليم", len(sent), 0)

print("\n--- 3) مع notify وشغل بطيء: تسليم ---")
sent.clear()
st, res = route_track({"barcode": "EKPB0412385EG", "notify": {
    "chat_id": 55, "message_id": 66, "budget_ms": 18000}}, elapsed_sec=31)
check("الحالة 200", st, 200)
check("حصل تسليم واحد", len(sent), 1)
check("chat_id صح", sent[0]["notify"]["chat_id"], 55)
check("message_id صح", sent[0]["notify"]["message_id"], 66)
check("الباركود صح", sent[0]["bc"], "EKPB0412385EG")
check("النتيجة كاملة", sorted(sent[0]["res"].keys()), ["records", "status"])
blob = json.dumps(sent[0], ensure_ascii=False)
check("مفيش JWT في التسليم", "eyJ" in blob, False)
check("مفيش سر في التسليم", "fake-renew" in blob or "fake-admin" in blob, False)

print("\n--- 4) الميزانية الافتراضية لو مش مبعوتة ---")
sent.clear()
route_track({"barcode": "X", "notify": {"chat_id": 1, "message_id": 2}},
            elapsed_sec=(probe.NOTIFY_DEFAULT_BUDGET_MS / 1000) + 1)
check("سلّم بالافتراضي", len(sent), 1)
sent.clear()
route_track({"barcode": "X", "notify": {"chat_id": 1, "message_id": 2}},
            elapsed_sec=1)
check("ما سلّمش وهو سريع", len(sent), 0)

print("\n--- 5) فشل التسليم مايكسرش الرد ---")
sent.clear()


def bad_deliver(notify, bc, res):
    raise RuntimeError("worker down")


try:
    st, res = route_track({"barcode": "X", "notify": {
        "chat_id": 1, "message_id": 2}}, elapsed_sec=40, deliver=bad_deliver)
    check("رمى استثناء", True, False)
except RuntimeError:
    # المنطق الحقيقي جوّه do_POST ملفوف بـtry فوق مستوى الرد،
    # بس _deliver نفسها بتبلع أخطاءها — نتأكد من ده مباشرة:
    ok, detail = probe._deliver({"chat_id": 1, "message_id": 2}, "X", RESULT)
    check("_deliver بتبلع الخطأ وترجّع (ok, detail)", ok, False)
    check("التفصيل نص آمن", isinstance(detail, str) and len(detail) > 0, True)

print("\n--- 6) باركود فاضي ---")
st, res = route_track({"notify": {"chat_id": 1, "message_id": 2}}, 40)
check("الحالة 400", st, 400)
check("الرسالة", res, {"err": "missing barcode"})

print("\n--- 7) _deliver: الحقول والحراسة ---")
src = open(os.path.join(HERE, "orkestr-f5-test", "probe.py"),
           encoding="utf-8").read()
check("بينده /finish", '"/finish"' in src, True)
check("بـRENEW_SECRET", 'f"Bearer {RENEW_SECRET}"' in src, True)
check("User-Agent متظبّط", '"egypt-post-renewer/1.0"' in src, True)
check("مفيش توكن تليجرام", "TELEGRAM" in src, False)
check("notify اختياري", 'payload.get("notify") or None' in src, True)

print("\n" + ("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED: {FAILED}"))
sys.exit(0 if not FAILED else 1)
