# -*- coding: utf-8 -*-
"""stub_worker — نسخة محلية من عقد الـWorker، للاختبار فقط.

⚠️ ملف اختبار. مايتنشرش ومالوش علاقة بالإنتاج. الغرض إثبات سلوك
   الوكيل الحقيقي (agent.py) على HTTP فعلي من غير ما نلمس الإنتاج.

بينفّذ نفس منطق worker.js بالحرف: نفس التحقق من السر، نفس الـupsert،
نفس حساب الحالة، نفس حد الـ90 ثانية.
"""
import json
import os
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("STUB_PORT", "8799"))
AGENT_SECRET = os.environ.get("AGENT_SECRET", "stub-agent-secret")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "stub-admin-secret")
OFFLINE_SEC = int(os.environ.get("AGENT_OFFLINE_SEC", "90"))

_db = sqlite3.connect(":memory:", check_same_thread=False)
_db.execute("""CREATE TABLE agent_state (
  agent_id TEXT PRIMARY KEY, last_seen INTEGER NOT NULL,
  agent_status TEXT, smax_status TEXT, version TEXT,
  host_os TEXT, updated_at INTEGER)""")
_lock = threading.Lock()

# ساعة قابلة للإزاحة — عشان نختبر الـ90 ثانية من غير ما نستنى 90 ثانية
CLOCK_SKEW = {"sec": 0}
HB_COUNT = {"n": 0}   # عدّاد النبضات — للاختبار بس


def now_sec():
    return int(time.time()) + CLOCK_SKEW["sec"]


# نفس القيم المغلقة اللي في worker.js بالحرف
SMAX_STATUS = {"ready", "not_configured", "starting", "error", "unknown"}
RUN_STATUS = {"running", "starting", "stopping"}
FIELDS = {"agent_id", "agent_status", "smax_status", "version", "host_os"}
MAX_BODY = 1024
MAX_FIELD = 32
import re as _re


def validate_heartbeat(a):
    if not isinstance(a, dict):
        return "bad_payload"
    for k in a:
        if k not in FIELDS:
            return "unexpected_field"
    aid = a.get("agent_id")
    if not isinstance(aid, str) or not aid or len(aid) > 64:
        return "bad_agent_id"
    if not _re.match(r"^[A-Za-z0-9._-]+$", aid):
        return "bad_agent_id"
    if not isinstance(a.get("smax_status"), str) or a["smax_status"] not in SMAX_STATUS:
        return "bad_smax_status"
    if "agent_status" in a and (not isinstance(a["agent_status"], str)
                                or a["agent_status"] not in RUN_STATUS):
        return "bad_agent_status"
    for k in ("version", "host_os"):
        if k in a and (not isinstance(a[k], str) or len(a[k]) > MAX_FIELD):
            return "bad_" + k
    return None


def compute_status(row, now):
    if row is None:
        return ("OFFLINE", "🔴", "never_seen", None)
    age = now - int(row["last_seen"])
    if age > OFFLINE_SEC:
        return ("OFFLINE", "🔴", "stale_heartbeat", age)
    if (row["smax_status"] or "") != "ready":
        return ("ONLINE_SMAX_NOT_READY", "🟡", row["smax_status"] or "unknown", age)
    return ("ONLINE", "🟢", "ok", age)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _auth(self, secret):
        return (self.headers.get("Authorization") or "") == "Bearer " + secret

    def do_POST(self):
        p = self.path.split("?", 1)[0].rstrip("/") or "/"
        # المرحلة 2: /agent/poll = نبضة + طلب مهمة. المحاكي مالوش طابور فبيرجّع job=null
        if p in ("/agent/heartbeat", "/agent/poll"):
            HB_COUNT["n"] += 1
            if not self._auth(AGENT_SECRET):
                self.send_response(401); self.end_headers()
                self.wfile.write(b"unauthorized"); return
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            if len(raw) > MAX_BODY:
                self._send({"ok": False, "error": "payload_too_large"}, 400); return
            try:
                a = json.loads(raw.decode("utf-8"))
            except Exception:
                a = None
            bad = validate_heartbeat(a)
            if bad:
                self._send({"ok": False, "error": bad}, 400); return
            aid = a["agent_id"]
            ts = now_sec()
            with _lock:
                _db.execute(
                    """INSERT INTO agent_state
                         (agent_id,last_seen,agent_status,smax_status,version,host_os,updated_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(agent_id) DO UPDATE SET
                         last_seen=excluded.last_seen,
                         agent_status=excluded.agent_status,
                         smax_status=excluded.smax_status,
                         version=excluded.version,
                         host_os=excluded.host_os,
                         updated_at=excluded.updated_at""",
                    (aid, ts, a.get("agent_status", "running"),
                     a.get("smax_status", "unknown"), a.get("version", ""),
                     a.get("host_os", ""), ts))
                _db.commit()
            code, icon, _, _ = compute_status(
                {"last_seen": ts, "smax_status": a.get("smax_status")}, ts)
            self._send({"ok": True, "status": code, "icon": icon,
                        "offline_after_sec": OFFLINE_SEC, "job": None})
            return
        # مسارات اختبار الساعة — للاختبار بس، مش جزء من عقد الـWorker
        if p == "/__skew":
            n = int(self.headers.get("Content-Length") or 0)
            CLOCK_SKEW["sec"] = int(json.loads(self.rfile.read(n))["sec"])
            self._send({"ok": True, "skew": CLOCK_SKEW["sec"]}); return
        self._send({"ok": False, "error": "not found"}, 404)

    def do_GET(self):
        p = self.path.split("?", 1)[0].rstrip("/") or "/"
        if p == "/agent/status":
            if not self._auth(ADMIN_SECRET):
                self.send_response(401); self.end_headers()
                self.wfile.write(b"unauthorized"); return
            n = now_sec()
            with _lock:
                cur = _db.execute(
                    "SELECT * FROM agent_state ORDER BY last_seen DESC LIMIT 10")
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            out = []
            for r in rows:
                code, icon, reason, age = compute_status(r, n)
                out.append({"agent_id": r["agent_id"], "status": code,
                            "icon": icon, "reason": reason, "age_sec": age,
                            "smax_status": r["smax_status"],
                            "version": r["version"], "host_os": r["host_os"]})
            self._send({"ok": True, "offline_after_sec": OFFLINE_SEC,
                        "agents": out}); return
        if p == "/__hbcount":
            self._send({"n": HB_COUNT["n"]}); return
        if p == "/__count":
            with _lock:
                c = _db.execute("SELECT COUNT(*) FROM agent_state").fetchone()[0]
            self._send({"rows": c}); return
        self._send({"ok": False, "error": "not found"}, 404)


def serve():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    serve()
    print("stub on 127.0.0.1:%d" % PORT, flush=True)
    while True:
        time.sleep(3600)