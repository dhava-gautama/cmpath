BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS cmp_harness_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL);
INSERT OR IGNORE INTO cmp_harness_meta VALUES(1,1);
-- request_id is declared NOT NULL, not just PRIMARY KEY: a TEXT primary key on
-- a rowid table is nullable in SQLite, and a NULL request_id reached the
-- retention scan. migrateTurnRequestID rebuilds a database written before the
-- constraint; this statement gives every new one the corrected definition.
CREATE TABLE IF NOT EXISTS cmp_turns (
 request_id TEXT PRIMARY KEY NOT NULL,
 task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
 fingerprint TEXT NOT NULL, request_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','committed','aborted')),
 generation INTEGER NOT NULL, task_revision INTEGER NOT NULL,
 package_json TEXT NOT NULL, reply_json TEXT, commit_hash TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS cmp_one_pending_turn ON cmp_turns(task_id) WHERE status='pending';
CREATE TABLE IF NOT EXISTS cmp_tool_calls (
 request_id TEXT NOT NULL REFERENCES cmp_turns(request_id), call_id TEXT NOT NULL,
 name TEXT NOT NULL, args_json TEXT NOT NULL, fingerprint TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('started','completed')),
 result_json TEXT, evidence_id INTEGER REFERENCES messages(id) ON DELETE RESTRICT,
 PRIMARY KEY(request_id,call_id)
);
CREATE TABLE IF NOT EXISTS cmp_model_calls (
 request_id TEXT NOT NULL REFERENCES cmp_turns(request_id), call_id TEXT NOT NULL,
 payload_json TEXT NOT NULL, units INTEGER NOT NULL, counting TEXT NOT NULL,
 PRIMARY KEY(request_id,call_id)
);
CREATE TABLE IF NOT EXISTS cmp_model_responses (
 request_id TEXT NOT NULL, call_id TEXT NOT NULL,
 response_json TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(request_id,call_id),
 FOREIGN KEY(request_id,call_id) REFERENCES cmp_model_calls(request_id,call_id)
);
CREATE TABLE IF NOT EXISTS cmp_retired_turns (
 request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('committed','aborted')),
 retired_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS cmp_turns_retired_guard
BEFORE INSERT ON cmp_turns
WHEN EXISTS(SELECT 1 FROM cmp_retired_turns WHERE request_id=NEW.request_id)
BEGIN
 SELECT RAISE(ABORT,'retired request ID cannot be reused');
END;
-- An UPDATE can move a live turn onto a retired ID just as an INSERT can
-- reintroduce it. Both are blocked; the tombstone still wins.
CREATE TRIGGER IF NOT EXISTS cmp_turns_retired_update_guard
BEFORE UPDATE OF request_id ON cmp_turns
WHEN EXISTS(SELECT 1 FROM cmp_retired_turns WHERE request_id=NEW.request_id)
BEGIN
 SELECT RAISE(ABORT,'retired request ID cannot be reused');
END;
-- Clocks deliberately have no task foreign key: deletion and ID reuse must
-- never reset a task's history. Key zero is the whole-database clock.
CREATE TABLE IF NOT EXISTS cmp_scope_epochs (
 task_id INTEGER PRIMARY KEY,
 epoch INTEGER NOT NULL CHECK(typeof(epoch)='integer' AND epoch>=0)
);
INSERT OR IGNORE INTO cmp_scope_epochs VALUES(0,0);
INSERT OR IGNORE INTO cmp_scope_epochs SELECT id,0 FROM tasks;
CREATE TABLE IF NOT EXISTS cmp_turn_basis (
 request_id TEXT NOT NULL REFERENCES cmp_turns(request_id),
 task_id INTEGER NOT NULL,
 epoch INTEGER NOT NULL CHECK(typeof(epoch)='integer' AND epoch>=0),
 PRIMARY KEY(request_id,task_id)
);
-- Explicit dependencies for state derived from earlier evidence. Edges are
-- immutable audit facts; a missing/retracted source makes a new derived write
-- ineligible rather than silently repairing it.
CREATE TABLE IF NOT EXISTS cmp_turn_provenance (
 request_id TEXT NOT NULL REFERENCES cmp_turns(request_id),
 evidence_id INTEGER NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('supports','contradicts')),
 created_at TEXT NOT NULL,
 PRIMARY KEY(request_id,evidence_id,kind)
);
-- A short-lived pre-effect certificate for a previously journaled tool intent.
-- It narrows (but cannot eliminate) the interval before an uncooperating remote
-- service performs its effect.
CREATE TABLE IF NOT EXISTS cmp_action_leases (
 token TEXT PRIMARY KEY,
 request_id TEXT NOT NULL REFERENCES cmp_turns(request_id),
 generation INTEGER NOT NULL,
 call_id TEXT NOT NULL,
 args_hash TEXT NOT NULL,
 expires_at INTEGER NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(request_id,generation,call_id,token)
);
CREATE TRIGGER IF NOT EXISTS cmp_epoch_tasks_insert
AFTER INSERT ON tasks BEGIN
 INSERT INTO cmp_scope_epochs(task_id,epoch) VALUES(NEW.id,1)
 ON CONFLICT(task_id) DO UPDATE SET epoch=epoch+1;
 UPDATE cmp_scope_epochs SET epoch=epoch+1 WHERE task_id=0;
END;
CREATE TRIGGER IF NOT EXISTS cmp_epoch_tasks_update
AFTER UPDATE ON tasks BEGIN
 INSERT INTO cmp_scope_epochs(task_id,epoch) VALUES(OLD.id,1)
 ON CONFLICT(task_id) DO UPDATE SET epoch=epoch+1;
 INSERT INTO cmp_scope_epochs(task_id,epoch) SELECT NEW.id,1 WHERE NEW.id!=OLD.id
 ON CONFLICT(task_id) DO UPDATE SET epoch=epoch+1;
 UPDATE cmp_scope_epochs SET epoch=epoch+1 WHERE task_id=0;
END;
CREATE TRIGGER IF NOT EXISTS cmp_epoch_tasks_delete
AFTER DELETE ON tasks BEGIN
 INSERT INTO cmp_scope_epochs(task_id,epoch) VALUES(OLD.id,1)
 ON CONFLICT(task_id) DO UPDATE SET epoch=epoch+1;
 UPDATE cmp_scope_epochs SET epoch=epoch+1 WHERE task_id=0;
END;
CREATE TRIGGER IF NOT EXISTS cmp_epoch_messages_insert
AFTER INSERT ON messages BEGIN
 INSERT INTO cmp_scope_epochs(task_id,epoch) VALUES(NEW.task_id,1)
 ON CONFLICT(task_id) DO UPDATE SET epoch=epoch+1;
 UPDATE cmp_scope_epochs SET epoch=epoch+1 WHERE task_id=0;
END;
CREATE TRIGGER IF NOT EXISTS cmp_epoch_messages_update
AFTER UPDATE ON messages BEGIN
 INSERT INTO cmp_scope_epochs(task_id,epoch) VALUES(OLD.task_id,1)
 ON CONFLICT(task_id) DO UPDATE SET epoch=epoch+1;
 INSERT INTO cmp_scope_epochs(task_id,epoch) SELECT NEW.task_id,1 WHERE NEW.task_id!=OLD.task_id
 ON CONFLICT(task_id) DO UPDATE SET epoch=epoch+1;
 UPDATE cmp_scope_epochs SET epoch=epoch+1 WHERE task_id=0;
END;
CREATE TRIGGER IF NOT EXISTS cmp_epoch_messages_delete
AFTER DELETE ON messages BEGIN
 INSERT INTO cmp_scope_epochs(task_id,epoch) VALUES(OLD.task_id,1)
 ON CONFLICT(task_id) DO UPDATE SET epoch=epoch+1;
 UPDATE cmp_scope_epochs SET epoch=epoch+1 WHERE task_id=0;
END;
CREATE TRIGGER IF NOT EXISTS cmp_epoch_facts_insert
AFTER INSERT ON facts BEGIN
 INSERT INTO cmp_scope_epochs(task_id,epoch) VALUES(NEW.task_id,1)
 ON CONFLICT(task_id) DO UPDATE SET epoch=epoch+1;
 UPDATE cmp_scope_epochs SET epoch=epoch+1 WHERE task_id=0;
END;
CREATE TRIGGER IF NOT EXISTS cmp_epoch_facts_update
AFTER UPDATE ON facts BEGIN
 INSERT INTO cmp_scope_epochs(task_id,epoch) VALUES(OLD.task_id,1)
 ON CONFLICT(task_id) DO UPDATE SET epoch=epoch+1;
 INSERT INTO cmp_scope_epochs(task_id,epoch) SELECT NEW.task_id,1 WHERE NEW.task_id!=OLD.task_id
 ON CONFLICT(task_id) DO UPDATE SET epoch=epoch+1;
 UPDATE cmp_scope_epochs SET epoch=epoch+1 WHERE task_id=0;
END;
CREATE TRIGGER IF NOT EXISTS cmp_epoch_facts_delete
AFTER DELETE ON facts BEGIN
 INSERT INTO cmp_scope_epochs(task_id,epoch) VALUES(OLD.task_id,1)
 ON CONFLICT(task_id) DO UPDATE SET epoch=epoch+1;
 UPDATE cmp_scope_epochs SET epoch=epoch+1 WHERE task_id=0;
END;
CREATE TRIGGER IF NOT EXISTS cmp_epoch_dependencies_insert
AFTER INSERT ON dependencies BEGIN
 INSERT INTO cmp_scope_epochs(task_id,epoch) VALUES(NEW.child,1)
 ON CONFLICT(task_id) DO UPDATE SET epoch=epoch+1;
 UPDATE cmp_scope_epochs SET epoch=epoch+1 WHERE task_id=0;
END;
CREATE TRIGGER IF NOT EXISTS cmp_epoch_dependencies_update
AFTER UPDATE ON dependencies BEGIN
 INSERT INTO cmp_scope_epochs(task_id,epoch) VALUES(OLD.child,1)
 ON CONFLICT(task_id) DO UPDATE SET epoch=epoch+1;
 INSERT INTO cmp_scope_epochs(task_id,epoch) SELECT NEW.child,1 WHERE NEW.child!=OLD.child
 ON CONFLICT(task_id) DO UPDATE SET epoch=epoch+1;
 UPDATE cmp_scope_epochs SET epoch=epoch+1 WHERE task_id=0;
END;
CREATE TRIGGER IF NOT EXISTS cmp_epoch_dependencies_delete
AFTER DELETE ON dependencies BEGIN
 INSERT INTO cmp_scope_epochs(task_id,epoch) VALUES(OLD.child,1)
 ON CONFLICT(task_id) DO UPDATE SET epoch=epoch+1;
 UPDATE cmp_scope_epochs SET epoch=epoch+1 WHERE task_id=0;
END;
CREATE TRIGGER IF NOT EXISTS cmp_epoch_aliases_insert
AFTER INSERT ON aliases BEGIN
 INSERT INTO cmp_scope_epochs(task_id,epoch) VALUES(NEW.task_id,1)
 ON CONFLICT(task_id) DO UPDATE SET epoch=epoch+1;
 UPDATE cmp_scope_epochs SET epoch=epoch+1 WHERE task_id=0;
END;
CREATE TRIGGER IF NOT EXISTS cmp_epoch_aliases_update
AFTER UPDATE ON aliases BEGIN
 INSERT INTO cmp_scope_epochs(task_id,epoch) VALUES(OLD.task_id,1)
 ON CONFLICT(task_id) DO UPDATE SET epoch=epoch+1;
 INSERT INTO cmp_scope_epochs(task_id,epoch) SELECT NEW.task_id,1 WHERE NEW.task_id!=OLD.task_id
 ON CONFLICT(task_id) DO UPDATE SET epoch=epoch+1;
 UPDATE cmp_scope_epochs SET epoch=epoch+1 WHERE task_id=0;
END;
CREATE TRIGGER IF NOT EXISTS cmp_epoch_aliases_delete
AFTER DELETE ON aliases BEGIN
 INSERT INTO cmp_scope_epochs(task_id,epoch) VALUES(OLD.task_id,1)
 ON CONFLICT(task_id) DO UPDATE SET epoch=epoch+1;
 UPDATE cmp_scope_epochs SET epoch=epoch+1 WHERE task_id=0;
END;
UPDATE cmp_harness_meta SET version=4 WHERE singleton=1;
COMMIT;
