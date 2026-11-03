-- Email Boss schema. Ported from the reference v2/schema.sql with the
-- domain-specific columns removed. Status and work_type carry NO CHECK:
-- both are validated in code against the workflow roster / profession.
--
-- Design principles kept from the original:
--   1. Normalize where it reduces corruption risk (contacts, subtasks, notes)
--   2. JSON1 text columns where shape varies per row (key_details, signals)
--   3. ISO 8601 timestamps (with offset where known)
--   4. Soft-delete via status flags, not row deletion
--   5. Append-only event log for the audit trail

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 30000;

-- ============================================================
-- CORE: Projects
-- ============================================================
CREATE TABLE IF NOT EXISTS projects (
    id              TEXT PRIMARY KEY,                -- 'proj_068'
    name            TEXT NOT NULL,
    location        TEXT NOT NULL DEFAULT '',        -- display name of the place / account
    location_slug   TEXT NOT NULL DEFAULT '',        -- filesystem-safe
    workflow_id     TEXT NOT NULL DEFAULT '',        -- which workflow this project walks
    status          TEXT NOT NULL DEFAULT '',        -- a stage id inside workflow_id
    work_type       TEXT DEFAULT '',                 -- profession work_type id

    scope           TEXT DEFAULT '',
    open_question   TEXT DEFAULT '',
    key_details     TEXT DEFAULT '{}',               -- JSON object

    -- Optional money columns (profession features.money)
    quote_amount    REAL,
    approved_amount REAL,
    quote_sent_date TEXT,                            -- YYYY-MM-DD

    -- Dates
    created         TEXT NOT NULL,                   -- YYYY-MM-DD
    follow_up_date  TEXT,
    snoozed_until   TEXT,
    closed_date     TEXT,
    won_date        TEXT,                            -- YYYY-MM-DD, client said yes
    last_activity   TEXT,                            -- ISO 8601
    last_communication TEXT,                         -- YYYY-MM-DD

    emails_sent     INTEGER DEFAULT 0,
    tags            TEXT DEFAULT '[]',               -- JSON array of strings
    source          TEXT DEFAULT '',                 -- 'triage', 'manual', 'assistant', ...

    archived        INTEGER DEFAULT 0,
    updated_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);
CREATE INDEX IF NOT EXISTS idx_projects_location_slug ON projects(location_slug);
CREATE INDEX IF NOT EXISTS idx_projects_follow_up ON projects(follow_up_date);
CREATE INDEX IF NOT EXISTS idx_projects_archived ON projects(archived);
CREATE INDEX IF NOT EXISTS idx_projects_workflow ON projects(workflow_id);

-- ============================================================
-- CORE: Contacts (per-project)
-- ============================================================
CREATE TABLE IF NOT EXISTS contacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name            TEXT NOT NULL DEFAULT '',
    role            TEXT DEFAULT '',
    email           TEXT DEFAULT '',
    phone           TEXT DEFAULT '',
    direct          TEXT DEFAULT '',
    mobile          TEXT DEFAULT '',
    cell            TEXT DEFAULT '',
    company         TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_contacts_project ON contacts(project_id);
CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email) WHERE email != '';

-- ============================================================
-- CORE: Subtasks
-- ============================================================
CREATE TABLE IF NOT EXISTS subtasks (
    id              TEXT NOT NULL,                   -- 'st_8a1b2c3d'
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    text            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'complete')),
    source          TEXT DEFAULT 'manual',           -- manual | system | email_response | assistant | ...
    created         TEXT,                            -- YYYY-MM-DD
    follow_up_date  TEXT,
    date_type       TEXT,                            -- follow_up | deadline | sla | quote | <profession date type>
    completed_date  TEXT,
    completed_by    TEXT,
    blocked_by      TEXT,

    -- SLA columns
    sla_type        TEXT,                            -- 'response' for a reply SLA
    sla_created     TEXT,                            -- ISO local wall-clock when the clock started
    sla_hours       REAL,

    category        TEXT DEFAULT '',
    priority        INTEGER DEFAULT 0,
    metadata        TEXT DEFAULT '{}',               -- JSON

    PRIMARY KEY (id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_subtasks_project ON subtasks(project_id);
CREATE INDEX IF NOT EXISTS idx_subtasks_status ON subtasks(status, project_id);
CREATE INDEX IF NOT EXISTS idx_subtasks_priority ON subtasks(priority DESC) WHERE status = 'pending';

-- ============================================================
-- CORE: Notes (project activity log)
-- ============================================================
CREATE TABLE IF NOT EXISTS notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    text            TEXT NOT NULL,
    time            TEXT,                            -- display string
    source          TEXT DEFAULT '',
    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    text_hash       TEXT
);

CREATE INDEX IF NOT EXISTS idx_notes_project ON notes(project_id);
CREATE INDEX IF NOT EXISTS idx_notes_hash ON notes(text_hash);

-- ============================================================
-- LOOKUPS: thread routing
-- ============================================================
CREATE TABLE IF NOT EXISTS conversation_routing (
    conversation_id TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    source          TEXT DEFAULT '',
    created_at      TEXT
);

-- ============================================================
-- TRIAGE: staging cards (pending user approval)
-- ============================================================
CREATE TABLE IF NOT EXISTS triage_cards (
    triage_id       TEXT PRIMARY KEY,
    card_type       TEXT NOT NULL DEFAULT 'email',
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'dismissed', 'auto_committed')),
    staged_at       TEXT NOT NULL,
    edited          INTEGER DEFAULT 0,
    edited_at       TEXT,

    -- Email fields
    subject         TEXT,
    from_name       TEXT,
    from_email      TEXT,
    "to"            TEXT,
    cc              TEXT,
    direction       TEXT CHECK (direction IN ('inbound', 'outbound', 'system')),
    received_at     TEXT,
    message_id      TEXT,
    conversation_id TEXT,
    thread_key_alt  TEXT,
    body_preview    TEXT,
    body_raw        TEXT,
    source_file     TEXT,
    source_folder   TEXT DEFAULT '',
    internet_message_id TEXT DEFAULT '',

    -- Matching
    matched_project TEXT DEFAULT '',
    confidence      TEXT DEFAULT 'low',
    match_reason    TEXT DEFAULT '',
    project_matches TEXT DEFAULT '[]',               -- JSON array
    auto_matched_project TEXT DEFAULT '',
    auto_match_reason TEXT DEFAULT '',
    auto_reason     TEXT DEFAULT '',

    -- Classification
    classification  TEXT DEFAULT '',
    needs_reply     INTEGER DEFAULT 0,
    summary         TEXT DEFAULT '',
    auto_action     TEXT DEFAULT '',
    auto_follow_up  TEXT,
    auto_date_type  TEXT DEFAULT 'follow_up',
    dollar_amount   REAL,
    work_type       TEXT DEFAULT '',
    workflow_id     TEXT DEFAULT '',
    signals         TEXT DEFAULT '{}',               -- JSON object
    suggestion      TEXT,                            -- JSON object or null
    is_quote_request INTEGER DEFAULT 0,
    is_self_note    INTEGER DEFAULT 0,
    doc_links       TEXT DEFAULT '[]',               -- JSON array

    -- Attachments
    has_attachments INTEGER DEFAULT 0,
    attachments     TEXT DEFAULT '[]',               -- JSON array
    attachment_data TEXT DEFAULT '[]',               -- JSON array (extracted)
    missing_inline_images INTEGER DEFAULT 0,
    inline_image_refs TEXT,

    -- New-project prefill
    new_project_location TEXT,
    new_project_name TEXT,

    -- Non-email cards
    project_id      TEXT DEFAULT '',
    reason          TEXT,

    -- Actions and signals
    triage_actions  TEXT DEFAULT '[]',
    completion_signals TEXT DEFAULT '[]',
    subtask_completions TEXT DEFAULT '[]',

    -- Resolution
    resolved_at     TEXT,
    resolved_by     TEXT,
    dismiss_reason  TEXT
);

CREATE INDEX IF NOT EXISTS idx_triage_status ON triage_cards(status);
CREATE INDEX IF NOT EXISTS idx_triage_type ON triage_cards(card_type);
CREATE INDEX IF NOT EXISTS idx_triage_message ON triage_cards(message_id);
CREATE INDEX IF NOT EXISTS idx_triage_conversation ON triage_cards(conversation_id);

-- ============================================================
-- PROCESSED MESSAGES (dedup tracker)
-- ============================================================
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id      TEXT PRIMARY KEY,
    triage_id       TEXT,
    project_id      TEXT,
    subject         TEXT,
    received_at     TEXT,
    status          TEXT DEFAULT 'processed',
    processed_at    TEXT
);

-- ============================================================
-- EVENT LOG (append-only)
-- ============================================================
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    machine         TEXT NOT NULL,
    action          TEXT NOT NULL,
    payload         TEXT NOT NULL DEFAULT '{}',
    CHECK (json_valid(payload))
);

CREATE INDEX IF NOT EXISTS idx_events_action ON events(action);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(
    json_extract(payload, '$.project_id')
) WHERE json_extract(payload, '$.project_id') IS NOT NULL;

-- ============================================================
-- HP HISTORY (daily game-layer snapshots)
-- ============================================================
CREATE TABLE IF NOT EXISTS hp_history (
    date            TEXT PRIMARY KEY,
    start_hp        INTEGER,
    start_time      TEXT,
    peak_hp         INTEGER,
    daily_low       INTEGER,
    snapshots       TEXT DEFAULT '[]',
    damage_events   TEXT DEFAULT '[]'
);

-- ============================================================
-- PROJECT INTAKE (text and file drops per project)
-- ============================================================
CREATE TABLE IF NOT EXISTS project_intake (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    intake_type     TEXT NOT NULL DEFAULT 'text'
                    CHECK (intake_type IN ('text', 'file')),
    content         TEXT DEFAULT '',
    file_path       TEXT,
    file_name       TEXT DEFAULT '',
    file_type       TEXT DEFAULT '',
    file_size       INTEGER DEFAULT 0,
    processed       INTEGER DEFAULT 0,
    extraction      TEXT DEFAULT '{}',
    extraction_error TEXT,
    needs_review    INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime')),
    source          TEXT DEFAULT 'manual'
);

CREATE INDEX IF NOT EXISTS idx_intake_project ON project_intake(project_id);

-- ============================================================
-- PROJECT CORRESPONDENCE (emails + attachments per project)
-- ============================================================
CREATE TABLE IF NOT EXISTS project_correspondence (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    message_id      TEXT,
    conversation_id TEXT,
    subject         TEXT NOT NULL DEFAULT '',
    from_name       TEXT DEFAULT '',
    from_email      TEXT DEFAULT '',
    to_list         TEXT DEFAULT '',
    cc_list         TEXT DEFAULT '',
    direction       TEXT CHECK (direction IN ('inbound', 'outbound')),
    received_at     TEXT,
    body_preview    TEXT DEFAULT '',
    body_html       TEXT DEFAULT '',
    source          TEXT DEFAULT 'triage'
                    CHECK (source IN ('triage', 'auto_commit', 'manual', 'assistant')),
    triage_id       TEXT,
    attachment_summary TEXT DEFAULT '[]',
    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_correspondence_project ON project_correspondence(project_id);
CREATE INDEX IF NOT EXISTS idx_correspondence_message ON project_correspondence(message_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_correspondence_dedup
    ON project_correspondence(project_id, message_id)
    WHERE message_id IS NOT NULL;

-- ============================================================
-- CORRESPONDENCE FILES (attachment BLOBs)
-- ============================================================
CREATE TABLE IF NOT EXISTS correspondence_files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    correspondence_id INTEGER NOT NULL REFERENCES project_correspondence(id) ON DELETE CASCADE,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    filename        TEXT NOT NULL,
    file_type       TEXT DEFAULT 'other',
    mime_type       TEXT DEFAULT '',
    file_size       INTEGER DEFAULT 0,
    file_data       BLOB,
    extracted_data  TEXT DEFAULT '{}',
    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_corr_files_correspondence ON correspondence_files(correspondence_id);
CREATE INDEX IF NOT EXISTS idx_corr_files_project ON correspondence_files(project_id);

-- ============================================================
-- APP META
-- ============================================================
CREATE TABLE IF NOT EXISTS app_meta (
    key             TEXT PRIMARY KEY,
    value           TEXT
);

INSERT OR IGNORE INTO app_meta (key, value) VALUES ('next_id', '1');
INSERT OR IGNORE INTO app_meta (key, value) VALUES ('last_triage', '');
INSERT OR IGNORE INTO app_meta (key, value) VALUES ('last_updated', '');
INSERT OR IGNORE INTO app_meta (key, value) VALUES ('schema_version', '1');

-- ============================================================
-- QUESTIONS (the blocking-decision queue; no priority column on purpose,
-- the pane inherits the board's own ordering through the project)
-- ============================================================
CREATE TABLE IF NOT EXISTS questions (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    question    TEXT NOT NULL,
    options     TEXT DEFAULT '[]',
    recommend   TEXT DEFAULT '',
    evidence    TEXT DEFAULT '',
    unblocks    TEXT DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'answered', 'moot')),
    answer      TEXT DEFAULT '',
    asked_by    TEXT DEFAULT 'assistant',
    created     TEXT NOT NULL,
    answered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_questions_status  ON questions(status);
CREATE INDEX IF NOT EXISTS idx_questions_project ON questions(project_id, status);

-- ============================================================
-- ALERTS (board banner). Deduped on key.
-- ============================================================
CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key          TEXT NOT NULL,
    severity     TEXT NOT NULL DEFAULT 'warn'
                 CHECK (severity IN ('hard', 'warn', 'info')),
    title        TEXT NOT NULL,
    detail       TEXT DEFAULT '',
    source       TEXT DEFAULT '',
    created_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    seen_count   INTEGER DEFAULT 1,
    status       TEXT NOT NULL DEFAULT 'open'
                 CHECK (status IN ('open', 'acked', 'resolved')),
    acked_at     TEXT,
    acked_by     TEXT,
    resolved_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status);
CREATE INDEX IF NOT EXISTS idx_alerts_key ON alerts(key);

-- ============================================================
-- DRAFTS (nothing ever sends; the user clicks Send in their own client)
-- ============================================================
CREATE TABLE IF NOT EXISTS drafts (
    id              TEXT PRIMARY KEY,
    project_id      TEXT DEFAULT '',
    "to"            TEXT DEFAULT '',
    cc              TEXT DEFAULT '',
    subject         TEXT DEFAULT '',
    body            TEXT DEFAULT '',
    reply_to_corr_id INTEGER,
    provider_draft_id TEXT,
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'sent', 'discarded')),
    created_by      TEXT DEFAULT 'assistant',
    created_at      TEXT NOT NULL,
    updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_drafts_project ON drafts(project_id);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);

-- ============================================================
-- AI USAGE LEDGER
-- ============================================================
CREATE TABLE IF NOT EXISTS ai_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    task        TEXT NOT NULL,
    provider    TEXT DEFAULT '',
    model       TEXT DEFAULT '',
    in_tok      INTEGER DEFAULT 0,
    out_tok     INTEGER DEFAULT 0,
    cost_usd    REAL DEFAULT 0,
    latency_ms  INTEGER DEFAULT 0,
    ok          INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_ts ON ai_usage(ts);

-- ============================================================
-- ASSISTANT LOG
-- ============================================================
CREATE TABLE IF NOT EXISTS assistant_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    project_id  TEXT DEFAULT '',
    item_type   TEXT DEFAULT '',
    outcome     TEXT DEFAULT '',
    detail      TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_assistant_log_run ON assistant_log(run_id);
CREATE INDEX IF NOT EXISTS idx_assistant_log_ts ON assistant_log(ts);

-- ============================================================
-- LICENSE STATE (the cached activation; the key itself is in config)
-- ============================================================
CREATE TABLE IF NOT EXISTS license_state (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT
);

-- ============================================================
-- VIEWS
-- ============================================================
CREATE VIEW IF NOT EXISTS v_project_summary AS
SELECT
    p.id, p.name, p.location, p.location_slug, p.workflow_id, p.status, p.work_type,
    p.quote_amount, p.approved_amount, p.follow_up_date, p.snoozed_until,
    p.last_activity, p.last_communication, p.emails_sent, p.created,
    p.closed_date, p.won_date, p.tags, p.archived,
    (SELECT COUNT(*) FROM subtasks s WHERE s.project_id = p.id) AS subtask_count,
    (SELECT COUNT(*) FROM subtasks s WHERE s.project_id = p.id AND s.status = 'complete') AS subtask_done,
    (SELECT json_group_array(c.email)
     FROM contacts c WHERE c.project_id = p.id AND c.email != '') AS contact_emails
FROM projects p;

CREATE VIEW IF NOT EXISTS v_contact_lookup AS
SELECT LOWER(c.email) AS lookup_key, c.project_id
FROM contacts c
WHERE c.email != '' AND EXISTS (
    SELECT 1 FROM projects p WHERE p.id = c.project_id AND p.archived = 0
)
UNION ALL
SELECT LOWER(c.name) AS lookup_key, c.project_id
FROM contacts c
WHERE c.name != '' AND EXISTS (
    SELECT 1 FROM projects p WHERE p.id = c.project_id AND p.archived = 0
);
