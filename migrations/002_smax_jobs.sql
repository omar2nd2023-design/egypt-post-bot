-- المرحلة 2 — طابور مهام SMAX.
-- ⚠️ جدول bc و agent_state ما بيتلمسوش. ده جدول جديد مستقل.

CREATE TABLE IF NOT EXISTS smax_jobs (
  job_id        TEXT PRIMARY KEY,     -- chat_id:message_id — حتمي، بيمنع التكرار
  corr_id       TEXT NOT NULL,        -- REQ-YYYYMMDD-XXXXXX للتتبّع في اللوجات
  chat_id       TEXT NOT NULL,
  message_id    TEXT NOT NULL,
  barcode       TEXT NOT NULL,
  request_no    TEXT,                 -- من ملفات البريد لو متاح
  national_id   TEXT,                 -- من فهرس Turso لو متاح
  status        TEXT NOT NULL,        -- PENDING/CLAIMED/RUNNING/SUCCESS/FAILED/TIMEOUT
  attempts      INTEGER NOT NULL DEFAULT 0,
  created_at    INTEGER NOT NULL,
  claimed_at    INTEGER,
  completed_at  INTEGER,
  lease_until   INTEGER,              -- بعده المهمة ترجع PENDING (لو فيه محاولات)
  expires_at    INTEGER,              -- بعده تبقى TIMEOUT نهائيًا — مافيش انتظار للأبد
  agent_id      TEXT,
  error         TEXT,
  result        TEXT
);

CREATE INDEX IF NOT EXISTS idx_smax_jobs_pending
  ON smax_jobs (status, created_at);