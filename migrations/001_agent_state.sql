-- المرحلة 1 — جدول واحد بس: حالة الوكيل.
-- ⚠️ smax_jobs مش هنا عن قصد. المرحلة دي مافيهاش طابور.
-- ⚠️ جدول bc ما بيتلمسش. ده إضافة مستقلة تمامًا.

CREATE TABLE IF NOT EXISTS agent_state (
  agent_id      TEXT PRIMARY KEY,
  last_seen     INTEGER NOT NULL,
  agent_status  TEXT,
  smax_status   TEXT,
  version       TEXT,
  host_os       TEXT,
  updated_at    INTEGER
);