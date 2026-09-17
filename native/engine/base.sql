
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS tasks (
 id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('open','archived')),
 snapshot TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dependencies (
 child INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
 parent INTEGER NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
 PRIMARY KEY(child,parent), CHECK(child != parent)
);
CREATE TABLE IF NOT EXISTS aliases (
 task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
 alias TEXT NOT NULL, PRIMARY KEY(task_id,alias)
);
CREATE INDEX IF NOT EXISTS aliases_lookup ON aliases(alias);
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
 role TEXT NOT NULL CHECK(role IN ('user','assistant','tool','document')),
 content TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_task ON messages(task_id,id);
CREATE VIRTUAL TABLE IF NOT EXISTS evidence_index USING fts5(
 content, content='messages', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
 INSERT INTO evidence_index(rowid,content) VALUES(new.id,new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
 INSERT INTO evidence_index(evidence_index,rowid,content) VALUES('delete',old.id,old.content);
END;
CREATE TABLE IF NOT EXISTS facts (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
 key TEXT NOT NULL, revision INTEGER NOT NULL, value TEXT NOT NULL,
 evidence_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
 retracted INTEGER NOT NULL CHECK(retracted IN (0,1)), created_at TEXT NOT NULL,
 UNIQUE(task_id,key,revision)
);
CREATE INDEX IF NOT EXISTS facts_current ON facts(task_id,key,revision DESC);
CREATE TABLE IF NOT EXISTS runtime_state (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 active_task INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
 version INTEGER NOT NULL
);
INSERT OR IGNORE INTO runtime_state VALUES(1,NULL,0);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
 task_id INTEGER, created_at TEXT NOT NULL
);
PRAGMA user_version=1;
COMMIT;
