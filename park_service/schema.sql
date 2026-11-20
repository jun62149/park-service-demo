PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS demo_meta(id INTEGER PRIMARY KEY CHECK(id=1), generation TEXT NOT NULL);
INSERT OR IGNORE INTO demo_meta VALUES(1,lower(hex(randomblob(16))));
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, display_name TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('reporter','service','engineer','manager')),
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), is_demo INTEGER NOT NULL DEFAULT 1 CHECK(is_demo=1)
);
CREATE TABLE IF NOT EXISTS tickets (
 id INTEGER PRIMARY KEY, number TEXT NOT NULL UNIQUE,
 reporter_id INTEGER NOT NULL REFERENCES users(id), reporter_name TEXT NOT NULL,
 reporter_contact TEXT NOT NULL, company_name TEXT NOT NULL DEFAULT '',
 location TEXT NOT NULL, original_text TEXT NOT NULL, issue_description TEXT NOT NULL,
 summary TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '',
 priority TEXT CHECK(priority IS NULL OR priority IN ('普通','加急','特急')),
 priority_confirmed_by INTEGER REFERENCES users(id), priority_confirmed_at TEXT,
 assigned_engineer_id INTEGER REFERENCES users(id), current_assignment_id INTEGER REFERENCES assignments(id),
 due_at TEXT, due_version INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL CHECK(status IN ('pending_assignment','pending_acceptance','in_progress','pending_service_review','returned','closed')),
 version INTEGER NOT NULL DEFAULT 1 CHECK(version>=1), is_demo INTEGER NOT NULL DEFAULT 1 CHECK(is_demo=1),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, state_changed_at TEXT NOT NULL,
 accepted_at TEXT, submitted_at TEXT, closed_at TEXT
);
CREATE TABLE IF NOT EXISTS assignments (
 id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id),
 sequence INTEGER NOT NULL, engineer_id INTEGER NOT NULL REFERENCES users(id),
 assigned_by INTEGER NOT NULL REFERENCES users(id), assigned_at TEXT NOT NULL,
 pushed_at TEXT NOT NULL, accepted_at TEXT, ended_at TEXT, reason TEXT NOT NULL DEFAULT '',
 UNIQUE(ticket_id,sequence)
);
CREATE TABLE IF NOT EXISTS completion_submissions (
 id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id),
 round INTEGER NOT NULL, engineer_id INTEGER NOT NULL REFERENCES users(id),
 resolution_note TEXT NOT NULL CHECK(length(trim(resolution_note))>0), submitted_at TEXT NOT NULL,
 reviewed_by INTEGER REFERENCES users(id), reviewed_at TEXT,
 review_result TEXT CHECK(review_result IN ('rejected','approved')), reject_reason TEXT,
 UNIQUE(ticket_id,round)
);
CREATE TABLE IF NOT EXISTS attachments (
 id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id),
 submission_id INTEGER NOT NULL REFERENCES completion_submissions(id),
 generated_name TEXT NOT NULL UNIQUE, original_name TEXT NOT NULL,
 detected_type TEXT NOT NULL CHECK(detected_type IN ('image/png','image/jpeg')),
 size INTEGER NOT NULL, hash TEXT NOT NULL, uploaded_by INTEGER NOT NULL REFERENCES users(id),
 uploaded_at TEXT NOT NULL, is_placeholder INTEGER NOT NULL DEFAULT 1 CHECK(is_placeholder=1)
);
CREATE TABLE IF NOT EXISTS ticket_events (
 id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id),
 ticket_version INTEGER NOT NULL, actor_id INTEGER NOT NULL REFERENCES users(id),
 actor_role TEXT NOT NULL, actor_name TEXT NOT NULL, action TEXT NOT NULL,
 from_status TEXT CHECK(from_status IS NULL OR from_status IN ('pending_assignment','pending_acceptance','in_progress','pending_service_review','returned','closed')),
 to_status TEXT NOT NULL CHECK(to_status IN ('pending_assignment','pending_acceptance','in_progress','pending_service_review','returned','closed')),
 before_values TEXT NOT NULL, after_values TEXT NOT NULL,
 effective_at TEXT NOT NULL, real_recorded_at TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
 assignment_id INTEGER REFERENCES assignments(id), submission_id INTEGER REFERENCES completion_submissions(id),
 request_id TEXT NOT NULL UNIQUE, UNIQUE(ticket_id,ticket_version)
);
CREATE TABLE IF NOT EXISTS demo_clock(id INTEGER PRIMARY KEY CHECK(id=1), offset_seconds INTEGER NOT NULL DEFAULT 0 CHECK(offset_seconds>=0));
INSERT OR IGNORE INTO demo_clock(id) VALUES(1);
CREATE TABLE IF NOT EXISTS clock_events(id INTEGER PRIMARY KEY, actor_id INTEGER NOT NULL REFERENCES users(id), seconds INTEGER NOT NULL, real_recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reminders(
 id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id),
 kind TEXT NOT NULL CHECK(kind IN ('phone','overdue')), scope TEXT NOT NULL,
 first_detected_at TEXT NOT NULL, resolved_at TEXT, channel TEXT NOT NULL DEFAULT 'demo_in_app',
 UNIQUE(ticket_id,kind,scope)
);
CREATE TABLE IF NOT EXISTS parse_drafts(
 id TEXT PRIMARY KEY, owner_session TEXT NOT NULL, actor_id INTEGER NOT NULL REFERENCES users(id),
 version INTEGER NOT NULL, source_text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS parse_previews(
 id TEXT PRIMARY KEY, draft_id TEXT NOT NULL REFERENCES parse_drafts(id), draft_version INTEGER NOT NULL,
 source_hash TEXT NOT NULL, outbound_text TEXT NOT NULL, outbound_hash TEXT NOT NULL,
 fingerprint TEXT NOT NULL, boot_id TEXT NOT NULL, created_monotonic REAL NOT NULL,
 confirmed_monotonic REAL, consumed_monotonic REAL
);
CREATE TABLE IF NOT EXISTS extraction_runs(
 id INTEGER PRIMARY KEY, actor_id INTEGER NOT NULL REFERENCES users(id), owner_session TEXT NOT NULL,
 ticket_id INTEGER REFERENCES tickets(id), source_text TEXT NOT NULL,
 requested_provider TEXT NOT NULL, provider TEXT NOT NULL, model TEXT, duration_ms INTEGER NOT NULL,
 fallback_reason TEXT, schema_version TEXT NOT NULL, normalized_result TEXT NOT NULL,
 created_at TEXT NOT NULL, redaction_applied INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS feishu_bindings(
 local_user_id INTEGER PRIMARY KEY REFERENCES users(id), open_id TEXT NOT NULL,
 enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
 verification_status TEXT NOT NULL DEFAULT 'unverified', last_verified_at REAL,
 note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS notification_outbox(
 id TEXT PRIMARY KEY, generation TEXT NOT NULL,
 event_id INTEGER REFERENCES ticket_events(id), ticket_id INTEGER NOT NULL REFERENCES tickets(id),
 kind TEXT NOT NULL, target_user_id INTEGER NOT NULL REFERENCES users(id), payload_text TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','sending','sent','failed','uncertain','unconfigured','disabled','cancelled')),
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL,
 sent_at REAL, error_code TEXT, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS notification_pending ON notification_outbox(status,next_attempt_at);
