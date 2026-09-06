# -*- coding: utf-8 -*-
"""المرحلة 3 — تحليل الشكوى على جهاز المستخدم بأدوات مشروع Smax_V11 **كما هي**.

بيرجّع للـWorker:
    issue_type · class_source · delay_days · confirm[] · denial[] ·
    reply (sent_at / drafted / none)

⚠️ مافيش إعادة بناء لأي منطق: التصنيف من classifier.classify_with_learning،
   الأدلة من reason_evidence.extract_evidence، التأخير من
   delay_days.calculate_delay_days_v11، والرد من قاعدة cip.responses.
⚠️ أي فشل هنا **مابيوقفش** الشكوى — بنرجّع None والرد بيطلع زي ما هو.
"""
import json
import os
import sys
from pathlib import Path

SMAX_V11 = Path(os.environ.get("SMAX_V11_DIR", r"E:\Projects\ComplaintsBot\Smax_V11"))

_loaded = False


def _load():
    global _loaded
    if _loaded:
        return True
    if not (SMAX_V11 / "classifier.py").exists():
        return False
    if str(SMAX_V11) not in sys.path:
        sys.path.insert(0, str(SMAX_V11))
    _loaded = True
    return True


def _discussion_text(comments):
    """نفس شكل «Discussion Arabic» في Smax_V11: اسم | تاريخ: نص، مفصولين بشرطات."""
    parts = []
    for c in comments or []:
        head = "%s | %s:" % (c.get("author", ""), c.get("when", ""))
        parts.append(head + "\n" + (c.get("text") or ""))
    return "\n\n-----------------\n\n".join(parts)


def _top(bucket, n=3):
    """أعلى n عبارة من دلو أدلة {phrase: set(sources)} → [(phrase, [sources])]."""
    items = []
    for phrase, srcs in (bucket or {}).items():
        items.append((str(phrase).strip(), sorted(str(s) for s in (srcs or ())) ))
    items.sort(key=lambda x: (-len(x[1]), x[0]))
    return items[:n]


# نفس عرض Smax_V11 (_rows_for_source/_render_matrix) مع تعديلات المستخدم 2026-09-06:
#   • «Request Status» متشال من كل الأقسام
#   • أدلة Discussion بتتكتب مع رقم التعليق في قائمة المناقشات
#   • الأقسام حسب التصنيف: وهمي → تأكيد+نفي · تأخير/إجراءات/تعثر → نفي+إجراءات+تعثر
#     · فقد/سرقة → نفي+فقد · غير كده → الخمسة
_SOURCES = ["Shipment Last Status", "Last Comment", "Discussion", "Description"]
_SECTIONS = [
    ("confirmed", "✅ أدلة تأكيد تسليم الشحنة:"),
    ("denial",    "❌ أدلة نفي التسليم:"),
    ("procedure", "⚠️ أدلة إجراءات التسليم:"),
    ("partial",   "📄 أدلة التعثر الجزئي:"),
    ("loss",      "📦 أدلة الفقد / السرقة:"),
]


def _sections_for(issue_type):
    it = str(issue_type or "")
    if "وهمي" in it:
        return ["confirmed", "denial"]
    if "فقد" in it or "سرقة" in it:
        return ["denial", "loss"]
    if "تأخير" in it or "إجراءات" in it or "تعثر" in it:
        return ["denial", "procedure", "partial"]
    return [k for k, _ in _SECTIONS]


def _norm_key(s):
    import re as _re
    return _re.sub(r"[\s\W_]+", "", str(s or "")).lower()


def _comment_refs(phrase, comments):
    """أرقام التعليقات اللي فيها العبارة دي (بعد توحيد المسافات والعلامات)."""
    k = _norm_key(phrase)
    if len(k) < 6:
        return []
    out = []
    for i, c in enumerate(comments or [], start=1):
        if k in _norm_key(c.get("text")):
            out.append(i)
    return out


def _review_like_smax(ev, issue_type, comments):
    blocks = []
    for key, title in _SECTIONS:
        if key not in _sections_for(issue_type):
            continue
        section = ev.get(key) or {}
        rows = [title]
        for src in _SOURCES:
            hits = sorted([p for p, s in section.items() if src in (s or ())], key=len)
            if not hits:
                rows.append("%s: لا يوجد" % src)
                continue
            rows.append("%s:" % src)
            for h in hits:
                line = "- %s" % h
                if src == "Discussion":
                    refs = _comment_refs(h, comments)
                    if refs:
                        line += "  (تعليق %s)" % "، ".join(str(r) for r in refs[:4])
                rows.append(line)
        blocks.append("\n".join(rows))
    return "\n\n".join(blocks)


def _by_source(bucket, n=3, maxlen=200):
    """{phrase: set(sources)} → {source: [أول n عبارة]} بأسماء مصادر Smax_V11."""
    out = {}
    for phrase, srcs in (bucket or {}).items():
        p = str(phrase).strip()[:maxlen]
        if not p:
            continue
        for s in (srcs or ()):
            lst = out.setdefault(str(s), [])
            if p not in lst and len(lst) < n:
                lst.append(p)
    return out


def analyze(res, job, log=print):
    """res = نتيجة search_and_extract (لقيت) · job = المهمة من الـWorker (فيها track)."""
    try:
        if not _load():
            log("تحليل: مجلد Smax_V11 مش موجود — اتخطّى")
            return None
        from models import ComplaintSourcesRaw
        from classifier import classify_with_learning
        from reason_evidence import extract_evidence
        from delay_days import calculate_delay_days_v11

        track = {}
        try:
            track = json.loads(job.get("track") or "{}") or {}
        except Exception:
            track = {}

        comments = res.get("comments") or []
        last = res.get("last_comment") or {}
        sources = ComplaintSourcesRaw(
            description=str(res.get("description") or ""),
            discussion=_discussion_text(comments),
            last_comment=str(last.get("text") or ""),
            shipment_last_status=str(track.get("last_status") or ""),
            before_last_status=str(track.get("before_last_status") or ""),
            received_from_third_party=str(track.get("received_date") or ""),
            request_status="",
            shipment_no=str(job.get("barcode") or ""),
        )
        issue_type, evidence_log, class_source = classify_with_learning(sources)
        delay = calculate_delay_days_v11(
            issue_type=issue_type,
            last_status=sources.shipment_last_status,
            received_date=track.get("received_date") or None,
            request_date=track.get("request_date") or None,
        )
        ev = extract_evidence(sources) or {}
        # نفس عرض Smax_V11 بالحرف (المستخدم 2026-09-06: «خلي كل حاجة ترجع زي
        # smax v11»): build_review_evidence = 5 أقسام × كل مصدر بترتيب SOURCE_ORDER
        # وكل الأدلة من غير حد، و«لا يوجد» للفاضي. وسبب التصنيف من build_reason_v11.
        review_text, reason = "", ""
        try:
            from reason_evidence import build_reason_v11
            review_text = _review_like_smax(ev, issue_type, comments)
            reason = str(build_reason_v11(sources, issue_type) or "")
        except Exception as e:
            log("عرض الأدلة (Smax_V11) فشل: %s" % type(e).__name__)
        out = {
            "review_text": review_text[:6000],
            "reason": reason[:300],
            "issue_type": str(issue_type or ""),
            "class_source": str(class_source or ""),
            "delay_days": int(delay or 0),
            "delay_basis": "received" if track.get("received_date") else
                           ("request" if track.get("request_date") else "none"),
            # الأربعة بيرجعوا دايمًا (المستخدم 2026-09-06) — الفاضي بيتعرض «مافيش»
            "confirm": _top(ev.get("confirmed")),
            "denial": _top(ev.get("denial")),
            "procedure": _top(ev.get("procedure")),
            "partial": _top(ev.get("partial")),
            "loss": _top(ev.get("loss")),
            # مجمّعة بالمصدر (المستخدم 2026-09-06): المصدر عنوان وتحته أدلته —
            # زي شاشة Smax_V11 بالظبط، وبترتيب مصادر ثابت في الـWorker.
            "confirm_by": _by_source(ev.get("confirmed")),
            "denial_by": _by_source(ev.get("denial")),
            "procedure_by": _by_source(ev.get("procedure")),
            "partial_by": _by_source(ev.get("partial")),
            "loss_by": _by_source(ev.get("loss")),
            "reply": _reply_from_comments(comments) or _reply(res.get("id"), log),
        }
        return out
    except Exception as e:
        log("تحليل الشكوى فشل: %s: %s" % (type(e).__name__, str(e)[:120]))
        return None


MANAGER_NAME = os.environ.get("SMAX_MANAGER_NAME", "Saleh, Omar")


def _reply_from_comments(comments):
    """رد مدير المشروع = **تعليقه هو** في المناقشات (المصدر الأساسي — المستخدم
    2026-09-06: تاريخ سجل الأداة مش هو تاريخ الرد الفعلي). بيرجّع آخر تعليق
    باسمه مع تاريخه ورقمه في القائمة، أو None لو مافيش."""
    key = [p.strip().lower() for p in MANAGER_NAME.replace(",", " ").split() if p.strip()]
    items = []
    for i, c in enumerate(comments or [], start=1):
        author = str(c.get("author") or "").lower()
        if key and all(k in author for k in key):
            items.append({"when": str(c.get("when") or ""), "index": i})
    if not items:
        return None
    # كل تعليقاته بترتيب الصفحة (الأقدم → الأحدث) — المستخدم 2026-09-06
    return {"state": "sent", "source": "discussions", "count": len(items),
            "items": items, "when": items[-1]["when"], "index": items[-1]["index"]}


def _reply(complaint_id, log=print):
    """رد مدير المشروع من قاعدة الردود بتاعة Smax_V11 (قراءة بس)."""
    try:
        from cip import responses as RS
        con = RS.connect()
        try:
            r = RS.get(con, str(complaint_id or "").strip())
        finally:
            con.close()
        if r and r.get("sent_at"):
            return {"state": "sent", "when": str(r["sent_at"])[:10]}
        if r and (r.get("text") or "").strip():
            return {"state": "drafted", "when": ""}
        return {"state": "none", "when": ""}
    except Exception as e:
        log("قراءة قاعدة الردود فشلت: %s" % type(e).__name__)
        return {"state": "unknown", "when": ""}
