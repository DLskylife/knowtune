-- knowtune schema v1
-- 所有时间统一 UTC ISO8601 字符串，避免时区在个人项目里搞出玄学 bug。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS kb (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  description TEXT,
  -- 知识库级参数（JSON）：全量快照，与 kbconfig.py 的 __full__ 标记配套。
  -- 早期按差异存储，全局改了会静默影响老库；现改为建库时冻结一整套值，
  -- 「用户手动改过」的判据是与出厂基线比对，而不是看键是否存在。
  config_json TEXT,
  created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS document (
  id            INTEGER PRIMARY KEY,
  kb_id         INTEGER NOT NULL REFERENCES kb(id) ON DELETE CASCADE,
  source_type   TEXT NOT NULL CHECK (source_type IN ('file','url','text','dir')),
  source_uri    TEXT NOT NULL,
  title         TEXT,
  content_hash  TEXT NOT NULL,
  extractor     TEXT NOT NULL,
  status        TEXT NOT NULL CHECK (status IN ('pending','indexing','ready','failed','stale')),
  fail_reason   TEXT,
  n_pages       INTEGER,
  n_chunks      INTEGER DEFAULT 0,
  active_gen    INTEGER NOT NULL DEFAULT 0,
  -- 导入时用的清洗方案（cleaner.CleanPlan 的 JSON）。重索引默认沿用同一套，
  -- NULL = 未清洗。清洗从不写回源文件，只影响入库的文本。
  clean_json    TEXT,
  imported_at   TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE (kb_id, source_uri)
);
CREATE INDEX IF NOT EXISTS idx_doc_kb_status ON document(kb_id, status);

CREATE TABLE IF NOT EXISTS chunk (
  id            INTEGER PRIMARY KEY,
  doc_id        INTEGER NOT NULL REFERENCES document(id) ON DELETE CASCADE,
  generation    INTEGER NOT NULL,
  seq           INTEGER NOT NULL,
  text          TEXT NOT NULL,
  text_seg      TEXT NOT NULL,
  token_count   INTEGER NOT NULL,
  page_no       INTEGER,
  heading_path  TEXT,
  char_start    INTEGER,
  char_end      INTEGER,
  content_hash  TEXT NOT NULL,
  is_hidden     INTEGER NOT NULL DEFAULT 0,
  UNIQUE (doc_id, generation, seq)
);
CREATE INDEX IF NOT EXISTS idx_chunk_doc_gen ON chunk(doc_id, generation);
CREATE INDEX IF NOT EXISTS idx_chunk_hash    ON chunk(content_hash);

CREATE TABLE IF NOT EXISTS embedding (
  content_hash TEXT NOT NULL,
  model        TEXT NOT NULL,
  dim          INTEGER NOT NULL,
  vec          BLOB NOT NULL,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  PRIMARY KEY (content_hash, model)
);

-- 中文必须预分词后写入 text_seg，见 docs/ARCHITECTURE.md §4.3 实测结论。
-- rowid 与 chunk.id 对齐，删除时按 rowid 精确删。
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
  text_seg,
  tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS session (
  id         TEXT PRIMARY KEY,
  kb_id      INTEGER REFERENCES kb(id) ON DELETE SET NULL,
  title      TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message (
  id             INTEGER PRIMARY KEY,
  session_id     TEXT NOT NULL REFERENCES session(id) ON DELETE CASCADE,
  role           TEXT NOT NULL CHECK (role IN ('user','assistant')),
  content        TEXT NOT NULL,
  trace_id       TEXT,
  citations_json TEXT,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_session ON message(session_id, id);

CREATE TABLE IF NOT EXISTS query_log (
  trace_id          TEXT PRIMARY KEY,
  session_id        TEXT,
  kb_id             INTEGER,
  question_digest   TEXT NOT NULL,
  question_chars    INTEGER,
  retrieve_ms       INTEGER,
  rerank_ms         INTEGER,
  gen_ms            INTEGER,
  total_ms          INTEGER,
  prompt_tokens     INTEGER,
  completion_tokens INTEGER,
  model             TEXT,
  rerank_used       INTEGER,
  rerank_model      TEXT,
  refused           INTEGER NOT NULL DEFAULT 0,
  refuse_reason     TEXT,
  top_score         REAL,
  score_margin      REAL,
  candidates_json   TEXT,
  degraded          TEXT,
  created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qlog_time ON query_log(created_at);

CREATE TABLE IF NOT EXISTS feedback (
  message_id INTEGER PRIMARY KEY REFERENCES message(id) ON DELETE CASCADE,
  verdict    TEXT NOT NULL CHECK (verdict IN ('up','down')),
  comment    TEXT,
  created_at TEXT NOT NULL
);
