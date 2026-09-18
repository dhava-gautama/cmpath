package engine

import (
	"fmt"
	"maps"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"cmpath.local/native/internal/sqlite"
)

// journalTables are the tables that carry a request_id, or reference cmp_turns.
var journalTables = []string{
	"cmp_turns", "cmp_tool_calls", "cmp_model_calls", "cmp_model_responses",
	"cmp_retired_turns", "cmp_turn_basis", "cmp_turn_provenance", "cmp_action_leases",
}

// legacyJournal is journal.sql as it stood while request_id was only a primary
// key. The substitution is asserted, so rewording the journal DDL fails this
// suite instead of quietly building a database that is already migrated.
func legacyJournal(t *testing.T) string {
	t.Helper()
	const migrated = "request_id TEXT PRIMARY KEY NOT NULL,"
	if !strings.Contains(journalSchema, migrated) {
		t.Fatalf("journal.sql no longer declares %q", migrated)
	}
	return strings.Replace(journalSchema, migrated, "request_id TEXT PRIMARY KEY,", 1)
}

// rawOpen opens the database without the engine, so a test can inspect or
// prepare state that Open would otherwise migrate.
func rawOpen(t *testing.T, path string) *sqlite.DB {
	t.Helper()
	db, err := sqlite.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	return db
}

func legacyBase(t *testing.T, db *sqlite.DB) {
	t.Helper()
	if err := db.Script("PRAGMA foreign_keys=ON"); err != nil {
		t.Fatal(err)
	}
	if err := db.Script(baseSchema); err != nil {
		t.Fatal(err)
	}
}

// legacyDatabase writes a pre-migration database: the base schema, the journal
// before request_id was NOT NULL, and one task whose turns and child rows cover
// every journal table that references cmp_turns.
func legacyDatabase(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "legacy.db")
	db := rawOpen(t, path)
	legacyBase(t, db)
	if err := db.Script(legacyJournal(t)); err != nil {
		t.Fatal(err)
	}
	mustExec(t, db, "INSERT INTO tasks(id,title,status,snapshot,revision,created_at) VALUES(1,'Legacy task','open','{}',1,'2020-01-01T00:00:00Z')")
	mustExec(t, db, "INSERT INTO messages(id,task_id,role,content,source,created_at) VALUES(1,1,'user','legacy evidence','{}','2020-01-01T00:00:00Z')")
	mustExec(t, db, "INSERT INTO cmp_turns VALUES('committed-turn',1,'f1','{}','committed',1,1,'{}',NULL,'hash','2020-01-01T00:00:00Z','2020-01-01T00:00:00Z')")
	mustExec(t, db, "INSERT INTO cmp_turns VALUES('pending-turn',1,'f2','{}','pending',1,1,'{}',NULL,NULL,'2020-01-01T00:00:00Z','2020-01-01T00:00:00Z')")
	mustExec(t, db, "INSERT INTO cmp_tool_calls VALUES('committed-turn','call-1','search','{}','f','started',NULL,NULL)")
	mustExec(t, db, "INSERT INTO cmp_model_calls VALUES('committed-turn','model-1','{}',10,'test-units')")
	mustExec(t, db, "INSERT INTO cmp_model_responses VALUES('committed-turn','model-1','{}','2020-01-01T00:00:00Z')")
	mustExec(t, db, "INSERT INTO cmp_turn_basis VALUES('committed-turn',1,0)")
	mustExec(t, db, "INSERT INTO cmp_turn_provenance VALUES('committed-turn',1,'supports','2020-01-01T00:00:00Z')")
	mustExec(t, db, "INSERT INTO cmp_action_leases VALUES('token-1','pending-turn',1,'call-2','hash',99,'2020-01-01T00:00:00Z')")
	mustExec(t, db, "INSERT INTO cmp_retired_turns VALUES('retired-turn','f0','committed','2020-01-01T00:00:00Z')")
	db.Close()
	return path
}

// journalCounts counts every journal table the database actually has.
func journalCounts(t *testing.T, db *sqlite.DB) map[string]int64 {
	t.Helper()
	out := map[string]int64{}
	for _, name := range journalTables {
		present, err := tableExists(db, name)
		if err != nil {
			t.Fatal(err)
		}
		if !present {
			continue
		}
		n, err := countRows(db, "SELECT count(*) AS n FROM "+name)
		if err != nil {
			t.Fatal(err)
		}
		out[name] = n
	}
	return out
}

// turnShape describes the parsed declaration of every cmp_turns column, so two
// databases can be compared without depending on how their schema text is
// spelled.
func turnShape(t *testing.T, db *sqlite.DB) []string {
	t.Helper()
	rows, err := db.Query("PRAGMA table_info(cmp_turns)")
	if err != nil {
		t.Fatal(err)
	}
	out := make([]string, 0, len(rows))
	for _, row := range rows {
		name, _ := row.Text("name")
		kind, _ := row.Text("type")
		notNull, _ := row.Int("notnull")
		pk, _ := row.Int("pk")
		out = append(out, fmt.Sprintf("%s:%s:notnull=%d:pk=%d", name, kind, notNull, pk))
	}
	return out
}

func turnRequestIDNotNull(t *testing.T, db *sqlite.DB) bool {
	t.Helper()
	_, kind, notNull, err := turnRequestIDColumn(db)
	if err != nil {
		t.Fatal(err)
	}
	return kind == "TEXT" && notNull
}

// objectSQL reads the definition SQLite keeps for one named object. A rebuild
// renames its replacement table into place, which makes SQLite quote the table
// name, so callers compare with the quotes removed.
func objectSQL(t *testing.T, db *sqlite.DB, kind, name string) string {
	t.Helper()
	rows, err := db.Query("SELECT sql FROM sqlite_master WHERE type=? AND name=?", kind, name)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 {
		t.Fatalf("want one %s named %s, got %d", kind, name, len(rows))
	}
	return strings.ReplaceAll(rows[0]["sql"].(string), `"`, "")
}

func objectNames(t *testing.T, db *sqlite.DB, kind, pattern string) []string {
	t.Helper()
	rows, err := db.Query("SELECT name FROM sqlite_master WHERE type=? AND name LIKE ? ORDER BY name", kind, pattern)
	if err != nil {
		t.Fatal(err)
	}
	out := []string{}
	for _, row := range rows {
		out = append(out, row["name"].(string))
	}
	return out
}

// schemaObjects is every entry sqlite_master holds, which is the count that must
// not grow when journal.sql is applied to an already up-to-date database.
func schemaObjects(t *testing.T, db *sqlite.DB) int64 {
	t.Helper()
	n, err := countRows(db, "SELECT count(*) AS n FROM sqlite_master")
	if err != nil {
		t.Fatal(err)
	}
	return n
}

func schemaVersion(t *testing.T, db *sqlite.DB) int64 {
	t.Helper()
	rows, err := db.Query("PRAGMA schema_version")
	if err != nil {
		t.Fatal(err)
	}
	version, err := rows[0].Int("schema_version")
	if err != nil {
		t.Fatal(err)
	}
	return version
}

func foreignKeysOn(t *testing.T, db *sqlite.DB) bool {
	t.Helper()
	rows, err := db.Query("PRAGMA foreign_keys")
	if err != nil {
		t.Fatal(err)
	}
	on, err := rows[0].Int("foreign_keys")
	if err != nil {
		t.Fatal(err)
	}
	return on == 1
}

func foreignKeyViolations(t *testing.T, db *sqlite.DB) []sqlite.Row {
	t.Helper()
	rows, err := db.Query("PRAGMA foreign_key_check")
	if err != nil {
		t.Fatal(err)
	}
	return rows
}

// TestTurnRequestIDMigrationRebuildsExistingDatabase covers the upgrade: a
// database whose cmp_turns still admits a NULL is rebuilt on Open, keeping every
// row of every journal table, its index, its guards and its foreign keys.
func TestTurnRequestIDMigrationRebuildsExistingDatabase(t *testing.T) {
	path := legacyDatabase(t)
	raw := rawOpen(t, path)
	if turnRequestIDNotNull(t, raw) {
		t.Fatal("the pre-migration database already refuses a NULL request_id")
	}
	before := journalCounts(t, raw)
	if before["cmp_tool_calls"] != 1 || before["cmp_turns"] != 2 {
		t.Fatalf("the fixture is not populated: %v", before)
	}
	// The children name cmp_turns in their own DDL, so the rebuild must leave
	// their definitions exactly as they were.
	childDDL := map[string]string{}
	for _, name := range []string{"cmp_tool_calls", "cmp_model_calls", "cmp_model_responses", "cmp_turn_basis", "cmp_turn_provenance", "cmp_action_leases"} {
		childDDL[name] = objectSQL(t, raw, "table", name)
	}
	raw.Close()

	e, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()

	if !turnRequestIDNotNull(t, e.db) {
		t.Fatal("request_id is still nullable after Open")
	}
	for name, definition := range childDDL {
		if after := objectSQL(t, e.db, "table", name); after != definition {
			t.Fatalf("the rebuild rewrote %s:\nbefore %q\nafter  %q", name, definition, after)
		}
	}
	if after := journalCounts(t, e.db); !maps.Equal(before, after) {
		t.Fatalf("rows changed across the rebuild:\nbefore %v\nafter  %v", before, after)
	}
	// The partial index and both guards went with the dropped table.
	if names := objectNames(t, e.db, "index", "cmp_one_pending_turn"); len(names) != 1 {
		t.Fatalf("partial index not recreated: %v", names)
	}
	if names := objectNames(t, e.db, "trigger", "cmp_turns_retired%"); len(names) != 2 {
		t.Fatalf("retired guards not recreated: %v", names)
	}
	if names := objectNames(t, e.db, "table", "cmp_turns_rebuild"); len(names) != 0 {
		t.Fatalf("rebuild table left behind: %v", names)
	}
	// The primary key's unique index has to survive the copy.
	indexes, err := e.db.Query("PRAGMA index_list(cmp_turns)")
	if err != nil {
		t.Fatal(err)
	}
	unique := false
	for _, index := range indexes {
		if partOf, _ := index.Int("unique"); partOf != 1 {
			continue
		}
		name, _ := index.Text("name")
		columns, err := e.db.Query("PRAGMA index_info(" + name + ")")
		if err != nil {
			t.Fatal(err)
		}
		if len(columns) == 1 && columns[0]["name"] == "request_id" {
			unique = true
		}
	}
	if !unique {
		t.Fatal("request_id is no longer uniquely indexed")
	}
	if violations := foreignKeyViolations(t, e.db); len(violations) != 0 {
		t.Fatalf("foreign keys are not clean after the rebuild: %v", violations)
	}
	if !foreignKeysOn(t, e.db) {
		t.Fatal("foreign key enforcement was not restored")
	}
	// A NULL is refused now, and the surrounding constraints still bite.
	if err = e.db.Exec("INSERT INTO cmp_turns VALUES(NULL,1,'f3','{}','aborted',1,1,'{}',NULL,NULL,'2020-01-01T00:00:00Z','2020-01-01T00:00:00Z')"); err == nil || !strings.Contains(err.Error(), "NOT NULL constraint failed") {
		t.Fatalf("a NULL request_id was accepted: %v", err)
	}
	if err = e.db.Exec("INSERT INTO cmp_tool_calls VALUES('no-such-turn','call-3','search','{}','f','started',NULL,NULL)"); err == nil || !strings.Contains(err.Error(), "FOREIGN KEY constraint failed") {
		t.Fatalf("a child row without a turn was accepted: %v", err)
	}
	// The rebuilt table still declares cmp_turns.task_id ON DELETE RESTRICT.
	if err = e.db.Exec("DELETE FROM tasks WHERE id=1"); err == nil || !strings.Contains(err.Error(), "FOREIGN KEY constraint failed") {
		t.Fatalf("deleting a task with turns was accepted: %v", err)
	}
	// The engine reads the migrated rows and can journal a new turn.
	task, err := e.Task(1)
	if err != nil {
		t.Fatal(err)
	}
	if turn, err := e.Turn("committed-turn"); err != nil || turn.Status != "committed" {
		t.Fatal(turn, err)
	}
	if calls, err := e.Tools("committed-turn"); err != nil || len(calls) != 1 {
		t.Fatal(calls, err)
	}
	if calls, err := e.ModelCalls("committed-turn"); err != nil || len(calls) != 1 {
		t.Fatal(calls, err)
	}
	// The migrated pending turn is still recoverable and still holds the task's
	// single pending slot, which the partial index preserves.
	_, err = e.Begin(request(task, "another-turn"))
	mustCode(t, err, "busy")
	if err = e.Abort("pending-turn", 1); err != nil {
		t.Fatal(err)
	}
	if _, err = e.Begin(request(task, "fresh-turn")); err != nil {
		t.Fatal(err)
	}
	if _, err = e.Commit("fresh-turn", 1, Reply{Text: "journalled after the rebuild"}); err != nil {
		t.Fatal(err)
	}

	// The rebuilt definition is the one a new database is created with, so the
	// frozen target DDL cannot drift away from journal.sql unnoticed.
	fresh, err := Open(filepath.Join(t.TempDir(), "fresh.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer fresh.Close()
	if migrated, created := turnShape(t, e.db), turnShape(t, fresh.db); !slices.Equal(migrated, created) {
		t.Fatalf("rebuilt cmp_turns differs from a fresh one:\nrebuilt %v\nfresh   %v", migrated, created)
	}
	if rebuilt, created := objectSQL(t, e.db, "table", "cmp_turns"), objectSQL(t, fresh.db, "table", "cmp_turns"); rebuilt != created {
		t.Fatalf("rebuilt DDL differs from a fresh one:\nrebuilt %q\nfresh   %q", rebuilt, created)
	}
	for _, object := range [][2]string{
		{"index", "cmp_one_pending_turn"},
		{"trigger", "cmp_turns_retired_guard"},
		{"trigger", "cmp_turns_retired_update_guard"},
	} {
		if rebuilt, created := objectSQL(t, e.db, object[0], object[1]), objectSQL(t, fresh.db, object[0], object[1]); rebuilt != created {
			t.Fatalf("rebuilt %s %s differs from a fresh one:\nrebuilt %q\nfresh   %q", object[0], object[1], rebuilt, created)
		}
	}
	if !turnRequestIDNotNull(t, fresh.db) {
		t.Fatal("a fresh database does not declare request_id NOT NULL")
	}
}

// TestTurnRequestIDMigrationIsIdempotent covers the property journal.sql depends
// on: it runs on every Open, so a migrated database must not be rebuilt again. A
// canary index on cmp_turns is dropped by any rebuild, and a rebuild also moves
// PRAGMA schema_version.
func TestTurnRequestIDMigrationIsIdempotent(t *testing.T) {
	path := legacyDatabase(t)
	canary := "cmp_migration_canary"
	raw := rawOpen(t, path)
	mustExec(t, raw, "CREATE INDEX "+canary+" ON cmp_turns(status)")
	raw.Close()

	e, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if names := objectNames(t, e.db, "index", canary); len(names) != 0 {
		t.Fatal("the first Open did not rebuild cmp_turns")
	}
	before := journalCounts(t, e.db)
	mustExec(t, e.db, "CREATE INDEX "+canary+" ON cmp_turns(status)")
	version := schemaVersion(t, e.db)
	objects := schemaObjects(t, e.db)
	e.Close()

	for round := 0; round < 3; round++ {
		e, err = Open(path)
		if err != nil {
			t.Fatalf("reopen %d failed: %v", round, err)
		}
		if names := objectNames(t, e.db, "index", canary); len(names) != 1 {
			t.Fatalf("reopen %d rebuilt cmp_turns: the canary index is gone", round)
		}
		if names := objectNames(t, e.db, "index", "cmp_one_pending_turn"); len(names) != 1 {
			t.Fatalf("reopen %d duplicated the partial index: %v", round, names)
		}
		if names := objectNames(t, e.db, "trigger", "cmp_turns_retired%"); len(names) != 2 {
			t.Fatalf("reopen %d changed the guards: %v", round, names)
		}
		if names := objectNames(t, e.db, "table", "cmp_turns_rebuild"); len(names) != 0 {
			t.Fatalf("reopen %d left a rebuild table: %v", round, names)
		}
		if got := schemaVersion(t, e.db); got != version {
			t.Fatalf("reopen %d changed the schema version from %d to %d", round, version, got)
		}
		if got := schemaObjects(t, e.db); got != objects {
			t.Fatalf("reopen %d changed sqlite_master from %d to %d objects", round, objects, got)
		}
		if after := journalCounts(t, e.db); !maps.Equal(before, after) {
			t.Fatalf("reopen %d changed the rows:\nbefore %v\nafter  %v", round, before, after)
		}
		if !turnRequestIDNotNull(t, e.db) || !foreignKeysOn(t, e.db) {
			t.Fatalf("reopen %d left the engine without NOT NULL or foreign keys", round)
		}
		e.Close()
	}
}

// TestTurnRequestIDMigrationAcceptsFreshDatabase pins the fresh path: a database
// created by this code never carries the nullable form, so the probe that runs
// on every Open has nothing to do.
func TestTurnRequestIDMigrationAcceptsFreshDatabase(t *testing.T) {
	path := filepath.Join(t.TempDir(), "fresh.db")
	e, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if !turnRequestIDNotNull(t, e.db) {
		t.Fatal("a fresh database declares a nullable request_id")
	}
	if names := objectNames(t, e.db, "table", "cmp_turns_rebuild"); len(names) != 0 {
		t.Fatalf("a fresh database was rebuilt: %v", names)
	}
	if names := objectNames(t, e.db, "trigger", "cmp_turns_retired%"); len(names) != 2 {
		t.Fatalf("a fresh database is missing its guards: %v", names)
	}
	version := schemaVersion(t, e.db)
	e.Close()
	e, err = Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()
	if got := schemaVersion(t, e.db); got != version {
		t.Fatalf("reopening a fresh database changed the schema version from %d to %d", version, got)
	}
	if !turnRequestIDNotNull(t, e.db) {
		t.Fatal("reopening lost the constraint")
	}
}

// TestTurnRequestIDMigrationRefusesPreExistingNullRequestID pins the policy for
// the defect itself: the rows are named, nothing is deleted, and the database is
// left exactly as it was found so an operator can decide what to do with it.
func TestTurnRequestIDMigrationRefusesPreExistingNullRequestID(t *testing.T) {
	path := legacyDatabase(t)
	raw := rawOpen(t, path)
	mustExec(t, raw, "INSERT INTO cmp_turns VALUES(NULL,1,'f3','{}','aborted',1,1,'{}',NULL,NULL,'2020-01-01T00:00:00Z','2020-01-01T00:00:00Z')")
	before := journalCounts(t, raw)
	raw.Close()

	e, err := Open(path)
	if err == nil {
		e.Close()
		t.Fatal("a NULL request_id was migrated instead of refused")
	}
	mustCode(t, err, "integrity")
	for _, want := range []string{"request_id", "NULL"} {
		if !strings.Contains(err.Error(), want) {
			t.Fatalf("the refusal does not name %q: %v", want, err)
		}
	}

	raw = rawOpen(t, path)
	if turnRequestIDNotNull(t, raw) {
		t.Fatal("the refused migration changed the column")
	}
	if names := objectNames(t, raw, "table", "cmp_turns_rebuild"); len(names) != 0 {
		t.Fatalf("the refused migration left a rebuild table: %v", names)
	}
	nulls, err := countRows(raw, "SELECT count(*) AS n FROM cmp_turns WHERE request_id IS NULL")
	if err != nil {
		t.Fatal(err)
	}
	if nulls != 1 {
		t.Fatalf("the NULL row was dropped: %d remain", nulls)
	}
	if after := journalCounts(t, raw); !maps.Equal(before, after) {
		t.Fatalf("the refused migration changed rows:\nbefore %v\nafter  %v", before, after)
	}
	// Repairing the row is an explicit step, and then the migration completes.
	mustExec(t, raw, "DELETE FROM cmp_turns WHERE request_id IS NULL")
	raw.Close()

	e, err = Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()
	if !turnRequestIDNotNull(t, e.db) {
		t.Fatal("the repaired database was not migrated")
	}
	if after := journalCounts(t, e.db); after["cmp_turns"] != before["cmp_turns"]-1 {
		t.Fatalf("the repaired migration lost rows: %v", after)
	}
}

// TestTurnRequestIDMigrationPreservesRetiredGuards covers the objects the DROP
// takes with it: both guards must come back, not just the index.
func TestTurnRequestIDMigrationPreservesRetiredGuards(t *testing.T) {
	path := legacyDatabase(t)
	e, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()

	err = e.db.Exec("INSERT INTO cmp_turns VALUES('retired-turn',1,'f4','{}','aborted',1,1,'{}',NULL,NULL,'2020-01-01T00:00:00Z','2020-01-01T00:00:00Z')")
	if err == nil || !strings.Contains(err.Error(), "retired request ID cannot be reused") {
		t.Fatalf("the INSERT guard did not survive the rebuild: %v", err)
	}
	err = e.db.Exec("UPDATE cmp_turns SET request_id='retired-turn' WHERE request_id='committed-turn'")
	if err == nil || !strings.Contains(err.Error(), "retired request ID cannot be reused") {
		t.Fatalf("the UPDATE guard did not survive the rebuild: %v", err)
	}
	rows, err := e.db.Query("SELECT request_id FROM cmp_turns ORDER BY request_id")
	if err != nil || len(rows) != 2 || rows[0]["request_id"] != "committed-turn" {
		t.Fatalf("a guarded write changed the table: %v %v", rows, err)
	}
}

// TestTurnRequestIDMigrationUpgradesSchemaOneJournal covers the oldest upgrade
// path: a journal that predates cmp_retired_turns. The rebuilt table's guards
// would name a table that does not exist yet, so the migration leaves them to
// journal.sql, which creates the table and then the guards in the same Open.
func TestTurnRequestIDMigrationUpgradesSchemaOneJournal(t *testing.T) {
	path := filepath.Join(t.TempDir(), "schema-one.db")
	db := rawOpen(t, path)
	legacyBase(t, db)
	turns := strings.Replace(cmpTurnsRebuildDDL, "CREATE TABLE cmp_turns_rebuild (", "CREATE TABLE cmp_turns (", 1)
	script := "BEGIN IMMEDIATE;\n" +
		"CREATE TABLE cmp_harness_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL);\n" +
		"INSERT INTO cmp_harness_meta VALUES(1,1);\n" + turns + ";\n" +
		"UPDATE cmp_harness_meta SET version=1 WHERE singleton=1;\nCOMMIT;"
	if err := db.Script(script); err != nil {
		t.Fatal(err)
	}
	mustExec(t, db, "INSERT INTO tasks(id,title,status,snapshot,revision,created_at) VALUES(1,'Legacy task','open','{}',1,'2020-01-01T00:00:00Z')")
	mustExec(t, db, "INSERT INTO cmp_turns VALUES('committed-turn',1,'f1','{}','committed',1,1,'{}',NULL,'hash','2020-01-01T00:00:00Z','2020-01-01T00:00:00Z')")
	db.Close()

	e, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()
	if !turnRequestIDNotNull(t, e.db) {
		t.Fatal("a schema-1 journal was not migrated")
	}
	if names := objectNames(t, e.db, "table", "cmp_retired_turns"); len(names) != 1 {
		t.Fatalf("cmp_retired_turns was not created: %v", names)
	}
	if names := objectNames(t, e.db, "trigger", "cmp_turns_retired%"); len(names) != 2 {
		t.Fatalf("the guards were not created: %v", names)
	}
	if violations := foreignKeyViolations(t, e.db); len(violations) != 0 {
		t.Fatalf("foreign keys are not clean: %v", violations)
	}
	// The guards are armed against a table that now exists, so a journal write
	// is admitted rather than failing with "no such table".
	task, err := e.Task(1)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = e.Begin(request(task, "after-upgrade")); err != nil {
		t.Fatal(err)
	}
	if _, err = e.Commit("after-upgrade", 1, Reply{Text: "journalled"}); err != nil {
		t.Fatal(err)
	}
	if after := journalCounts(t, e.db); after["cmp_turns"] != 2 {
		t.Fatalf("the migrated turn was lost: %v", after)
	}
}

// TestTurnRequestIDMigrationKeepsHarnessSchemaVersion pins the version decision.
// The constraint is strictly stricter, so an older reader stays correct and the
// schema version stays 4. A database that has not been rebuilt is told apart by
// the column's own nullability instead: Info reports it through
// cmp_turns_request_id_not_null, and the same fact is one PRAGMA away.
func TestTurnRequestIDMigrationKeepsHarnessSchemaVersion(t *testing.T) {
	path := legacyDatabase(t)
	raw := rawOpen(t, path)
	if turnRequestIDNotNull(t, raw) {
		t.Fatal("the pre-migration database already declares NOT NULL")
	}
	rows, err := raw.Query("SELECT version FROM cmp_harness_meta WHERE singleton=1")
	if err != nil {
		t.Fatal(err)
	}
	if version, _ := rows[0].Int("version"); version != 4 {
		t.Fatalf("the pre-migration database is version %d, not 4", version)
	}
	raw.Close()

	e, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()
	info, err := e.Info()
	if err != nil {
		t.Fatal(err)
	}
	if info["harness_schema"] != 4 {
		t.Fatalf("the migrated database reports harness_schema %v", info["harness_schema"])
	}
	if info["cmp_turns_request_id_not_null"] != true {
		t.Fatalf("the migrated database does not report the constraint: %v", info)
	}
	rows, err = e.db.Query("SELECT version FROM cmp_harness_meta WHERE singleton=1")
	if err != nil {
		t.Fatal(err)
	}
	if version, _ := rows[0].Int("version"); version != 4 {
		t.Fatalf("the migration changed the stored harness version to %d", version)
	}
}

// TestTurnRequestIDProbeReadsTheFrozenLegacyDeclaration checks the probe's two
// answers directly, on a database that is never opened by the engine: the frozen
// pre-migration journal reports a nullable column and a fresh one does not.
func TestTurnRequestIDProbeReadsTheFrozenLegacyDeclaration(t *testing.T) {
	legacy := rawOpen(t, filepath.Join(t.TempDir(), "legacy.db"))
	legacyBase(t, legacy)
	if err := legacy.Script(legacyJournal(t)); err != nil {
		t.Fatal(err)
	}
	if turnRequestIDNotNull(t, legacy) {
		t.Fatal("the frozen pre-migration journal already declares NOT NULL")
	}
	needs, err := turnRequestIDNullable(legacy)
	if err != nil || !needs {
		t.Fatalf("the probe did not recognise the pre-migration declaration: %v %v", needs, err)
	}
	fresh := rawOpen(t, filepath.Join(t.TempDir(), "fresh.db"))
	legacyBase(t, fresh)
	if err := fresh.Script(journalSchema); err != nil {
		t.Fatal(err)
	}
	needs, err = turnRequestIDNullable(fresh)
	if err != nil || needs {
		t.Fatalf("the probe wants to rebuild an up-to-date database: %v %v", needs, err)
	}
}

// TestForeignKeyPragmaIsInertInsideTransaction pins the ordering the migration
// depends on. SQLite masks the foreign key flag out of a flag pragma while a
// transaction is open (sqlite3.c, PragTyp_FLAG: `if( db->autoCommit==0 ) mask
// &= ~(SQLITE_ForeignKeys)`), so switching enforcement off inside the rebuild's
// transaction would leave it on and the DROP would be refused by the child
// tables' ON DELETE RESTRICT. The pragma therefore goes before BEGIN.
func TestForeignKeyPragmaIsInertInsideTransaction(t *testing.T) {
	path := legacyDatabase(t)
	db := rawOpen(t, path)
	defer db.Close()
	mustExec(t, db, "PRAGMA foreign_keys=ON")
	mustExec(t, db, "BEGIN IMMEDIATE")
	mustExec(t, db, "PRAGMA foreign_keys=OFF")
	if !foreignKeysOn(t, db) {
		t.Fatal("the pragma took effect inside a transaction; the migration's ordering is no longer required")
	}
	if err := db.Exec("INSERT INTO cmp_tool_calls VALUES('no-such-turn','call-3','search','{}','f','started',NULL,NULL)"); err == nil {
		t.Fatal("foreign keys are not enforced inside the transaction")
	}
	if err := db.Exec("DROP TABLE cmp_turns"); err == nil {
		t.Fatal("dropping the parent table with child rows was accepted")
	}
	mustExec(t, db, "COMMIT")
	// Outside a transaction the same statement does take effect.
	mustExec(t, db, "PRAGMA foreign_keys=OFF")
	if foreignKeysOn(t, db) {
		t.Fatal("the pragma did not take effect outside a transaction")
	}
}
