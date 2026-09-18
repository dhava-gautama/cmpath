package engine

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"cmpath.local/native/internal/sqlite"
)

// ExportReport describes a completed version-1 JSON-lines archive.
type ExportReport struct {
	Format  string           `json:"format"`
	Version int              `json:"version"`
	Rows    map[string]int64 `json:"rows"`
	Path    string           `json:"path,omitempty"`
}

var exportTables = []string{"tasks", "dependencies", "aliases", "messages", "facts", "runtime_state", "events", "sqlite_sequence", "cmp_harness_meta", "cmp_turns", "cmp_tool_calls", "cmp_model_calls", "cmp_model_responses", "cmp_retired_turns", "cmp_scope_epochs", "cmp_turn_basis", "cmp_turn_provenance", "cmp_action_leases"}

// The hard-link publisher below is deliberately kept as the first choice. A
// hard link gives us an atomic, no-replace commit on POSIX filesystems: the
// destination is either absent or points at the already-synced archive, and a
// concurrent creator wins or loses without either side overwriting the other.
//
// Some Windows-mounted filesystems (notably WSL DrvFs mounts) allow a
// same-directory rename but reject hard links. On those filesystems the
// fallback reserves the destination with this marker before renaming the
// completed temporary file over it. The marker is intentionally recognizable:
// if the process dies after reservation and before rename, a later operator can
// distinguish the incomplete reservation from an archive and remove it after
// confirming that no exporter is still running. There is no portable
// compare-and-rename primitive for arbitrary POSIX filesystems, so this is the
// strongest no-overwrite fallback available there.
const exportReservationPrefix = "cmpath-export-reservation-v1\n"

// These indirections keep publication failure and interruption paths
// deterministic in focused tests without changing the public API. They are
// intentionally unexported and should not be modified by production callers.
var (
	exportLink     = os.Link
	exportLstat    = os.Lstat
	exportOpenFile = os.OpenFile
	exportRename   = os.Rename
)

// eachRow bounds buffering to one database row. The table/where clauses are
// internal constants; all caller values must be bound parameters.
func (e *Engine) eachRow(table, where string, args []any, fn func(sqlite.Row) error) error {
	var last int64
	first := true
	for {
		params := append([]any{}, args...)
		clause := where
		if !first {
			if strings.TrimSpace(clause) == "" {
				clause = " WHERE rowid>?"
			} else {
				clause += " AND rowid>?"
			}
			params = append(params, last)
		}
		rows, err := e.db.Query("SELECT rowid AS __cmp_export_rowid,* FROM "+table+clause+" ORDER BY rowid LIMIT 1", params...)
		if err != nil {
			return err
		}
		if len(rows) == 0 {
			return nil
		}
		last = rows[0]["__cmp_export_rowid"].(int64)
		first = false
		delete(rows[0], "__cmp_export_rowid")
		if err = fn(rows[0]); err != nil {
			return err
		}
	}

}

// Export writes a deterministic logical archive in one coherent transaction.
// JSON-valued database columns remain strings, preserving their exact spelling.
// It is an audit export, not a supported restore format; use SQLite backup for recovery.
func (e *Engine) Export(w io.Writer) (out ExportReport, err error) {
	out = ExportReport{Format: "cmpath-journal-jsonl", Version: 1, Rows: map[string]int64{}}
	if w == nil {
		return out, problem("invalid", "export writer is required")
	}
	err = e.transaction(func() error {
		enc := json.NewEncoder(w)
		schema, er := e.db.Query("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name")
		if er != nil {
			return er
		}
		if er = enc.Encode(map[string]any{"kind": "header", "format": out.Format, "version": out.Version, "schema": schema}); er != nil {
			return er
		}
		for _, table := range exportTables {
			out.Rows[table] = 0
			if er = e.eachRow(table, "", nil, func(row sqlite.Row) error {
				if er := enc.Encode(map[string]any{"kind": "row", "table": table, "row": row}); er != nil {
					return er
				}
				out.Rows[table]++
				return nil
			}); er != nil {
				return er
			}
		}
		return enc.Encode(map[string]any{"kind": "footer", "rows": out.Rows})
	})
	return
}

// exportDestinationExists distinguishes an existing destination from a
// filesystem capability failure. A failed hard-link attempt must never fall
// through to a publisher that could replace an existing caller file.
func exportDestinationExists(path string) (bool, error) {
	_, err := exportLstat(path)
	if err == nil {
		return true, nil
	}
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	return false, err
}

// removeExportReservation removes only a marker that still contains our
// exact reservation bytes. This keeps normal rename/open failures from
// needlessly leaking a marker while avoiding removal of a path replaced by a
// different file in the meantime. A process interruption cannot run this
// cleanup; the recognizable marker is therefore part of the documented
// recovery procedure.
func removeExportReservation(path string, marker []byte) {
	contents, err := os.ReadFile(path)
	if err != nil || !bytes.Equal(contents, marker) {
		return
	}
	_ = os.Remove(path)
}

// syncExportDirectory is best effort. POSIX local filesystems generally
// support directory fsync, which makes the rename/link durable across a
// metadata crash. Windows and several mounted/network filesystems reject
// opening or syncing directories; the already-synced file remains the best
// available guarantee there, and doctor/docs report that reduced durability.
func syncExportDirectory(path string) {
	directory, err := os.Open(filepath.Dir(path))
	if err != nil {
		return
	}
	defer directory.Close()
	_ = directory.Sync()
}

// reserveExportDestination claims an absent destination with an owner-only
// marker. The marker is synced before it is used as the rename target, so a
// crash after this function returns cannot silently lose the no-overwrite
// reservation on filesystems that support file fsync but not directory fsync.
func reserveExportDestination(path, temporary string) ([]byte, error) {
	marker := []byte(exportReservationPrefix + filepath.Base(temporary) + "\n")
	f, err := exportOpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return nil, err
	}
	closed := false
	owned := false
	defer func() {
		if !closed {
			_ = f.Close()
		}
		if !owned {
			removeExportReservation(path, marker)
		}
	}()
	if n, writeErr := f.Write(marker); writeErr != nil {
		return nil, writeErr
	} else if n != len(marker) {
		return nil, io.ErrShortWrite
	}
	if err = f.Sync(); err != nil {
		return nil, err
	}
	if err = f.Close(); err != nil {
		closed = true
		return nil, err
	}
	closed = true
	owned = true
	return marker, nil
}

// publishExport commits a fully written temporary archive. The returned
// method is intentionally internal; it makes the fallback observable in
// focused tests without changing the wire report.
func publishExport(temporary, destination string) (method string, err error) {
	if err = exportLink(temporary, destination); err == nil {
		syncExportDirectory(destination)
		return "hard_link", nil
	} else {
		linkErr := err
		if exists, statErr := exportDestinationExists(destination); statErr != nil {
			// A destination we cannot inspect is not safe to replace. Retain the
			// original link error, which preserves the existing storage error
			// contract while failing closed.
			return "", linkErr
		} else if exists {
			// Existing destinations are never candidates for the fallback.
			return "", linkErr
		}
	}

	// On native Windows, os.Rename maps to MoveFile, whose documented
	// no-replace behavior gives us the same destination race protection as the
	// hard-link path. This also avoids creating a marker that MoveFile would
	// itself refuse to replace. On POSIX, rename replaces an existing path, so
	// reserve the destination first.
	if runtime.GOOS == "windows" {
		if err = exportRename(temporary, destination); err != nil {
			return "", err
		}
		syncExportDirectory(destination)
		return "rename_fallback", nil
	}

	marker, err := reserveExportDestination(destination, temporary)
	if err != nil {
		return "", err
	}
	if err = exportRename(temporary, destination); err != nil {
		removeExportReservation(destination, marker)
		return "", err
	}
	syncExportDirectory(destination)
	return "rename_fallback", nil
}

// ExportFile publishes a complete archive without replacing an existing path.
// A temporary file in the destination directory is synced, then published by
// hard link where supported. On POSIX filesystems that reject hard links, an
// exclusive reservation marker followed by same-directory rename provides a
// portable fallback. Native Windows uses its no-replace MoveFile rename. In
// either case, an existing destination is left untouched. The POSIX fallback
// cannot prevent a non-cooperating writer from replacing its reservation
// before the final rename; callers that require that stronger guarantee should
// use a hard-link-capable filesystem.
func (e *Engine) ExportFile(path string) (out ExportReport, err error) {
	if !textOK(path) {
		return out, problem("invalid", "export path is required")
	}
	f, err := os.CreateTemp(filepath.Dir(path), ".cmpath-export-*")
	if err != nil {
		return out, err
	}
	defer os.Remove(f.Name())
	defer f.Close()
	if out, err = e.Export(f); err != nil {
		return out, err
	}
	if err = f.Sync(); err != nil {
		return out, err
	}
	if err = f.Close(); err != nil {
		return out, err
	}
	if _, err = publishExport(f.Name(), path); err != nil {
		return out, err
	}
	out.Path = path
	return out, nil
}

type RetentionReport struct {
	Cutoff              string           `json:"cutoff"`
	PlanHash            string           `json:"plan_hash"`
	Rows                map[string]int64 `json:"rows"`
	ProtectedPending    int64            `json:"protected_pending"`
	ProtectedUnresolved int64            `json:"protected_unresolved"`
	Applied             bool             `json:"applied"`
}

func retentionCutoff(cutoff string) (time.Time, error) {
	t, err := time.Parse(time.RFC3339Nano, cutoff)
	if err != nil {
		return t, problem("invalid", "cutoff must be an RFC3339 timestamp with timezone")
	}
	return t, nil
}

// retentionChildTables are the six journal tables holding a turn's child rows.
// Rows counts and the plan hash read them in this order; ApplyRetention and
// retentionConverged cover their rows plus the parent turn row.
var retentionChildTables = []string{"cmp_tool_calls", "cmp_model_calls", "cmp_model_responses", "cmp_turn_basis", "cmp_turn_provenance", "cmp_action_leases"}

// retentionCorpusTables is the order the plan hash reads a candidate in: the
// turn row first, then its child tables. The order is part of the hash, which
// is otherwise opaque.
var retentionCorpusTables = append([]string{"cmp_turns"}, retentionChildTables...)

// retentionApplyTables is the deletion order: child rows before the parent turn
// row, as the journal's foreign keys require. It is a separate list from
// retentionCorpusTables because that one is read-only and hashed.
var retentionApplyTables = []string{"cmp_model_responses", "cmp_model_calls", "cmp_tool_calls", "cmp_turn_basis", "cmp_turn_provenance", "cmp_action_leases", "cmp_turns"}

// retentionConverged reports whether id reached the exact post-apply state: no
// journal row survives anywhere (a surviving child row would otherwise be
// unreachable, because its parent turn is gone) and the tombstone exists
// exactly once. The plan hash covers each of these rows, so a surviving row is
// evidence that some delete did not converge rather than a change to the plan.
// Must run inside the apply transaction, after the deletes.
func retentionConverged(e *Engine, id string) (bool, error) {
	for _, table := range retentionCorpusTables {
		rows, err := e.db.Query("SELECT request_id FROM "+table+" WHERE request_id=?", id)
		if err != nil {
			return false, err
		}
		if len(rows) != 0 {
			return false, nil
		}
	}
	rows, err := e.db.Query("SELECT request_id FROM cmp_retired_turns WHERE request_id=?", id)
	if err != nil {
		return false, err
	}
	return len(rows) == 1, nil
}

// retentionCorpus fingerprints, under the already-open encoder, every row that
// ApplyRetention may remove or leave behind for the candidate IDs. It reads each
// candidate's turn and child rows in one fixed order so the plan hash describes
// the pre-apply state regardless of the order in which SQLite evaluates the
// deletes; it also reads the existing tombstones for those IDs, so one inserted
// between dry run and apply is visible to the stale-plan check. Must run inside
// a transaction, before the deletes.
func retentionCorpus(e *Engine, enc *json.Encoder, ids []string) error {
	if err := enc.Encode([]any{"cmpath-retention-corpus-v1", ids}); err != nil {
		return err
	}
	for _, id := range ids {
		for _, table := range retentionCorpusTables {
			if err := e.eachRow(table, " WHERE request_id=?", []any{id}, func(row sqlite.Row) error {
				return enc.Encode([]any{table, row})
			}); err != nil {
				return err
			}
		}
	}
	for _, id := range ids {
		if err := e.eachRow("cmp_retired_turns", " WHERE request_id=?", []any{id}, func(row sqlite.Row) error {
			return enc.Encode([]any{"cmp_retired_turns", row})
		}); err != nil {
			return err
		}
	}
	return nil
}

// retention computes a bounded-output plan fingerprint from every candidate's
// full journal contents. Must run inside a transaction.
func (e *Engine) retention(cutoff time.Time) (out RetentionReport, ids []string, err error) {
	out = RetentionReport{Cutoff: cutoff.UTC().Format(time.RFC3339Nano), Rows: map[string]int64{"cmp_turns": 0, "cmp_tool_calls": 0, "cmp_model_calls": 0, "cmp_model_responses": 0, "cmp_retired_turns": 0, "cmp_turn_basis": 0, "cmp_turn_provenance": 0, "cmp_action_leases": 0}}
	h := sha256.New()
	enc := json.NewEncoder(h)
	_ = enc.Encode([]any{"cmpath-retention-v1", out.Cutoff})
	err = e.eachRow("cmp_turns", "", nil, func(row sqlite.Row) error {
		status := row["status"].(string)
		if status == "pending" {
			out.ProtectedPending++
			return nil
		}
		if status != "committed" && status != "aborted" {
			return nil
		}
		id := row["request_id"].(string)
		unresolved, er := e.db.Query("SELECT count(*) AS n FROM cmp_tool_calls WHERE request_id=? AND status='started'", id)
		if er != nil {
			return er
		}
		if unresolved[0]["n"].(int64) > 0 {
			out.ProtectedUnresolved++
			return nil
		}
		updated, er := time.Parse(time.RFC3339Nano, row["updated_at"].(string))
		if er != nil {
			return problem("invalid", "turn has an invalid updated_at timestamp")
		}
		if !updated.Before(cutoff) {
			return nil
		}
		if er = enc.Encode(row); er != nil {
			return er
		}
		for _, table := range retentionChildTables {
			if er = e.eachRow(table, " WHERE request_id=?", []any{id}, func(child sqlite.Row) error {
				out.Rows[table]++
				return enc.Encode([]any{table, child})
			}); er != nil {
				return er
			}
		}
		out.Rows["cmp_turns"]++
		out.Rows["cmp_retired_turns"]++
		ids = append(ids, id)
		return nil
	})
	if err == nil {
		err = retentionCorpus(e, enc, ids)
	}
	out.PlanHash = hex.EncodeToString(h.Sum(nil))
	return
}

// RetentionPlan is read-only. Its fixed-size output remains safe for stdio even
// when a database contains many eligible turns. Rows counts tombstones to create.
func (e *Engine) RetentionPlan(cutoff string) (out RetentionReport, err error) {
	t, err := retentionCutoff(cutoff)
	if err != nil {
		return out, err
	}
	err = e.transaction(func() error { out, _, err = e.retention(t); return err })
	return
}

// ApplyRetention rejects a changed candidate set or changed journal contents.
// Tombstones and deletes commit together; base evidence and facts are untouched.
// A tombstone that already exists for a candidate is left exactly as it is, so
// a row that was retired out of band still converges instead of failing.
func (e *Engine) ApplyRetention(cutoff, planHash string) (out RetentionReport, err error) {
	t, err := retentionCutoff(cutoff)
	if err != nil {
		return out, err
	}
	if len(planHash) != 64 {
		return out, problem("invalid", "a dry-run plan hash is required")
	}
	err = e.transaction(func() error {
		var ids []string
		var er error
		out, ids, er = e.retention(t)
		if er != nil {
			return er
		}
		if out.PlanHash != planHash {
			return problem("stale_plan", "retention candidates changed; repeat dry-run")
		}
		retiredAt := now()
		for _, id := range ids {
			if er = e.db.Exec("INSERT OR IGNORE INTO cmp_retired_turns(request_id,fingerprint,status,retired_at) SELECT request_id,fingerprint,status,? FROM cmp_turns WHERE request_id=?", retiredAt, id); er != nil {
				return er
			}
			for _, table := range retentionApplyTables {
				if er = e.db.Exec("DELETE FROM "+table+" WHERE request_id=?", id); er != nil {
					return er
				}
			}
			converged, er := retentionConverged(e, id)
			if er != nil {
				return er
			}
			if !converged {
				return problem("integrity", "retention did not remove every journal row for a retired request ID")
			}
		}
		return nil
	})
	out.Applied = err == nil
	return
}
