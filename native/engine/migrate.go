package engine

import (
	"fmt"
	"slices"
	"strings"

	"cmpath.local/native/internal/sqlite"
)

// cmp_turns declares request_id as its primary key, and a TEXT primary key on a
// rowid table is nullable in SQLite. The column that every child journal table
// references and that every retention statement matches on could therefore hold
// a NULL. journal.sql now declares NOT NULL; this file rebuilds the databases
// that were written before it, because SQLite cannot change a column's
// nullability in place. The rebuild is the procedure documented under
// "Making Other Kinds Of Table Schema Changes": create the corrected table, copy
// the rows, drop the old table, rename, then recreate the partial index and the
// two retired-ID guards that the drop takes with it.
//
// cmpTurnsRebuildDDL and the three objects below are frozen copies of the target
// shape rather than a reference to journal.sql. A migration is a statement about
// one upgrade, so it must not follow a later journal edit;
// TestTurnRequestIDMigrationRebuildsExistingDatabase pins the two together by
// comparing a rebuilt database's parsed schema against a fresh one's.
const (
	cmpTurnsRebuildTable = "cmp_turns_rebuild"

	cmpTurnsRebuildDDL = `CREATE TABLE cmp_turns_rebuild (
 request_id TEXT PRIMARY KEY NOT NULL,
 task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
 fingerprint TEXT NOT NULL, request_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','committed','aborted')),
 generation INTEGER NOT NULL, task_revision INTEGER NOT NULL,
 package_json TEXT NOT NULL, reply_json TEXT, commit_hash TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
)`

	cmpTurnsOnePendingIndex = `CREATE UNIQUE INDEX cmp_one_pending_turn ON cmp_turns(task_id) WHERE status='pending'`

	cmpTurnsRetiredGuard = `CREATE TRIGGER cmp_turns_retired_guard
BEFORE INSERT ON cmp_turns
WHEN EXISTS(SELECT 1 FROM cmp_retired_turns WHERE request_id=NEW.request_id)
BEGIN
 SELECT RAISE(ABORT,'retired request ID cannot be reused');
END`

	cmpTurnsRetiredUpdateGuard = `CREATE TRIGGER cmp_turns_retired_update_guard
BEFORE UPDATE OF request_id ON cmp_turns
WHEN EXISTS(SELECT 1 FROM cmp_retired_turns WHERE request_id=NEW.request_id)
BEGIN
 SELECT RAISE(ABORT,'retired request ID cannot be reused');
END`
)

// cmpTurnsColumns is the frozen column order of the pre-migration table. The
// copy names every column, so a table that does not carry exactly this set is
// refused rather than copied with a column dropped or invented.
var cmpTurnsColumns = []string{
	"request_id", "task_id", "fingerprint", "request_json", "status",
	"generation", "task_revision", "package_json", "reply_json",
	"commit_hash", "created_at", "updated_at",
}

// migrateTurnRequestID gives cmp_turns.request_id its NOT NULL constraint. Open
// calls it before journal.sql, so a fresh database is created with the
// constraint by the CREATE TABLE there and nothing is rebuilt, and a database
// written before the constraint is rebuilt once and then skips the probe.
func migrateTurnRequestID(db *sqlite.DB) error {
	rebuild, err := turnRequestIDNullable(db)
	if err != nil || !rebuild {
		return err
	}
	// A NULL request_id is the defect this migration exists to prevent, and the
	// rebuilt table cannot carry one. The engine treats removal as an explicit
	// audited operation, so the migration names the rows and stops instead of
	// dropping them: the database is left exactly as it was found.
	nulls, err := countRows(db, "SELECT count(*) AS n FROM cmp_turns WHERE request_id IS NULL")
	if err != nil {
		return err
	}
	if nulls != 0 {
		return problem("integrity", fmt.Sprintf(
			"cmp_turns.request_id is NULL in %d row(s), which NOT NULL cannot admit and this engine will not delete silently; inspect them with SELECT rowid,typeof(request_id) FROM cmp_turns WHERE request_id IS NULL, retire or repair them explicitly, and reopen",
			nulls))
	}
	// Foreign key enforcement may only be switched outside a transaction, so it
	// goes before the rebuild's BEGIN: the DROP would otherwise run the implicit
	// DELETE FROM cmp_turns that the child tables' ON DELETE RESTRICT refuses.
	if err = db.Exec("PRAGMA foreign_keys=OFF"); err != nil {
		return err
	}
	if err = rebuildTurnRequestID(db); err != nil {
		_ = db.Exec("PRAGMA foreign_keys=ON")
		return err
	}
	// Every connection the engine opens declares that it enforces foreign keys.
	// A connection that cannot restore that is refused rather than handed out.
	return db.Exec("PRAGMA foreign_keys=ON")
}

// turnRequestIDNullable reports whether cmp_turns still admits a NULL
// request_id, which is true only for the journal's own pre-migration
// declaration.
func turnRequestIDNullable(db *sqlite.DB) (bool, error) {
	columns, kind, notNull, err := turnRequestIDColumn(db)
	if err != nil || kind != "TEXT" || notNull {
		return false, err
	}
	if !slices.Equal(columns, cmpTurnsColumns) {
		return false, problem("schema", fmt.Sprintf(
			"cmp_turns has columns %v, want the journal's %v; refusing to rebuild an unrecognised table", columns, cmpTurnsColumns))
	}
	return true, nil
}

// turnRequestIDColumn reads the declaration of cmp_turns.request_id from
// PRAGMA table_info, which reports the parsed schema. The pragma distinguishes
// the two states exactly -- TEXT PRIMARY KEY has a notnull flag of 0 and TEXT
// PRIMARY KEY NOT NULL has 1 -- and unlike the text in sqlite_master it is
// unaffected by whitespace, quoting or column order. A missing table, or a
// request_id declared as anything else, reports an empty kind so that callers
// treat it as a column this migration has nothing to say about.
func turnRequestIDColumn(db *sqlite.DB) (columns []string, kind string, notNull bool, err error) {
	rows, err := db.Query("PRAGMA table_info(cmp_turns)")
	if err != nil || len(rows) == 0 {
		return nil, "", false, err
	}
	columns = make([]string, 0, len(rows))
	for _, row := range rows {
		name, er := row.Text("name")
		if er != nil {
			return nil, "", false, er
		}
		columns = append(columns, name)
		if name != "request_id" {
			continue
		}
		if kind, er = row.Text("type"); er != nil {
			return nil, "", false, er
		}
		flag, er := row.Int("notnull")
		if er != nil {
			return nil, "", false, er
		}
		notNull = flag != 0
	}
	return columns, kind, notNull, nil
}

// rebuildTurnRequestID performs the rebuild in one transaction. The caller has
// already switched foreign key enforcement off.
func rebuildTurnRequestID(db *sqlite.DB) error {
	if err := db.Exec("BEGIN IMMEDIATE"); err != nil {
		return err
	}
	committed := false
	defer func() {
		if !committed {
			_ = db.Exec("ROLLBACK")
		}
	}()
	before, err := countRows(db, "SELECT count(*) AS n FROM cmp_turns")
	if err != nil {
		return err
	}
	copyColumns := strings.Join(cmpTurnsColumns, ",")
	for _, step := range []string{
		// A rebuild interrupted before its commit cannot leave this table, but
		// dropping it is harmless and keeps a repeated attempt deterministic.
		"DROP TABLE IF EXISTS " + cmpTurnsRebuildTable,
		cmpTurnsRebuildDDL,
		"INSERT INTO " + cmpTurnsRebuildTable + "(" + copyColumns + ") SELECT " + copyColumns + " FROM cmp_turns",
	} {
		if err = db.Exec(step); err != nil {
			return err
		}
	}
	copied, err := countRows(db, "SELECT count(*) AS n FROM "+cmpTurnsRebuildTable)
	if err != nil {
		return err
	}
	if copied != before {
		return problem("integrity", fmt.Sprintf("cmp_turns rebuild copied %d of %d row(s); nothing was changed", copied, before))
	}
	for _, step := range []string{
		"DROP TABLE cmp_turns",
		"ALTER TABLE " + cmpTurnsRebuildTable + " RENAME TO cmp_turns",
		cmpTurnsOnePendingIndex,
	} {
		if err = db.Exec(step); err != nil {
			return err
		}
	}
	// A schema-1 journal has no cmp_retired_turns to guard yet, and a guard that
	// names an absent table would abort the next insert with "no such table"
	// instead of admitting it. journal.sql creates that table and then both
	// guards later in this same Open.
	guarded, err := tableExists(db, "cmp_retired_turns")
	if err != nil {
		return err
	}
	if guarded {
		for _, guard := range []string{cmpTurnsRetiredGuard, cmpTurnsRetiredUpdateGuard} {
			if err = db.Exec(guard); err != nil {
				return err
			}
		}
	}
	// The documented procedure checks the referential state before committing,
	// so a rebuild that orphaned a child row rolls back instead of publishing.
	violations, err := db.Query("PRAGMA foreign_key_check")
	if err != nil {
		return err
	}
	if len(violations) != 0 {
		return problem("integrity", fmt.Sprintf("cmp_turns rebuild left %d foreign key violation(s); nothing was changed", len(violations)))
	}
	if err = db.Exec("COMMIT"); err != nil {
		return err
	}
	committed = true
	return nil
}

func countRows(db *sqlite.DB, sql string) (int64, error) {
	rows, err := db.Query(sql)
	if err != nil {
		return 0, err
	}
	if len(rows) != 1 {
		return 0, problem("integrity", "expected one row from a count query")
	}
	return rows[0].Int("n")
}

func tableExists(db *sqlite.DB, name string) (bool, error) {
	rows, err := db.Query("SELECT name FROM sqlite_master WHERE type='table' AND name=?", name)
	if err != nil {
		return false, err
	}
	return len(rows) != 0, nil
}
