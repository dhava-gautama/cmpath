package engine

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
)

func TestExportExactDeterministicAndNoOverwrite(t *testing.T) {
	e, task := fresh(t)
	content := "Unicode 雪\x00after" + strings.Repeat("x", 1024*1024)
	if _, err := e.AppendBatch([]Append{{TaskID: task.ID, Role: "document", Content: content}}); err != nil {
		t.Fatal(err)
	}
	turn, err := e.Begin(request(task, "export"))
	if err != nil {
		t.Fatal(err)
	}
	payload := "{ \"integer\": 9007199254740993123456789, \"unicode\": \"雪\\u0000tail\" }"
	if err = e.RecordModelRequest(turn.RequestID, turn.Generation, "model", payload, 1, "test"); err != nil {
		t.Fatal(err)
	}
	if err = e.db.Exec("INSERT INTO cmp_model_responses VALUES(?,?,?,?)", turn.RequestID, "model", payload, now()); err != nil {
		t.Fatal(err)
	}
	var a, b bytes.Buffer
	report, err := e.Export(&a)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = e.Export(&b); err != nil || !bytes.Equal(a.Bytes(), b.Bytes()) {
		t.Fatal("not deterministic", err)
	}
	if report.Rows["cmp_model_responses"] != 1 || report.Rows["messages"] != 2 {
		t.Fatal(report)
	}
	dec := json.NewDecoder(&a)
	foundContent, foundPayload := false, false
	for {
		var record map[string]any
		err = dec.Decode(&record)
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		row, _ := record["row"].(map[string]any)
		if record["table"] == "messages" && row["content"] == content {
			foundContent = true
		}
		if record["table"] == "cmp_model_responses" && row["response_json"] == payload {
			foundPayload = true
		}
	}
	if !foundContent || !foundPayload {
		t.Fatal("lost exact content")
	}
	path := filepath.Join(t.TempDir(), "export.jsonl")
	if _, err = e.ExportFile(path); err != nil {
		t.Fatal(err)
	}
	before, _ := os.ReadFile(path)
	if _, err = e.ExportFile(path); err == nil {
		t.Fatal("overwrote destination")
	}
	after, _ := os.ReadFile(path)
	if !bytes.Equal(before, after) {
		t.Fatal("changed destination")
	}
	if _, err = e.Export(failingWriter{}); err == nil {
		t.Fatal("ignored write error")
	}
	if _, err = e.Info(); err != nil {
		t.Fatal("export left transaction open", err)
	}
}

func TestExportFileFallsBackWhenHardLinksAreUnsupported(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("native Windows uses no-replace MoveFile directly")
	}
	e, _ := fresh(t)
	directory := t.TempDir()
	destination := filepath.Join(directory, "fallback.jsonl")
	oldLink := exportLink
	exportLink = func(string, string) error { return errors.New("hard links are unsupported") }
	t.Cleanup(func() { exportLink = oldLink })

	if _, err := e.ExportFile(destination); err != nil {
		t.Fatal(err)
	}
	contents, err := os.ReadFile(destination)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(contents, []byte(`"kind":"header"`)) || !bytes.Contains(contents, []byte(`"kind":"footer"`)) {
		t.Fatalf("fallback did not publish a complete archive: %q", contents[:min(len(contents), 256)])
	}
	if matches, err := filepath.Glob(filepath.Join(directory, ".cmpath-export-*")); err != nil || len(matches) != 0 {
		t.Fatalf("temporary export leaked: %v", matches)
	}
}

func TestExportFileFallbackNeverReplacesExistingDestination(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("native Windows uses no-replace MoveFile directly")
	}
	e, _ := fresh(t)
	directory := t.TempDir()
	destination := filepath.Join(directory, "existing.jsonl")
	original := []byte("caller-owned archive\n")
	if err := os.WriteFile(destination, original, 0600); err != nil {
		t.Fatal(err)
	}
	oldLink := exportLink
	exportLink = func(string, string) error { return errors.New("hard links are unsupported") }
	t.Cleanup(func() { exportLink = oldLink })

	if _, err := e.ExportFile(destination); err == nil {
		t.Fatal("existing destination was unexpectedly accepted")
	}
	contents, err := os.ReadFile(destination)
	if err != nil || !bytes.Equal(contents, original) {
		t.Fatalf("existing destination changed: %q (%v)", contents, err)
	}
}

func TestExportFileFallbackClosesRaceBeforeReservation(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("native Windows uses no-replace MoveFile directly")
	}
	e, _ := fresh(t)
	directory := t.TempDir()
	destination := filepath.Join(directory, "raced.jsonl")
	oldLink, oldOpen := exportLink, exportOpenFile
	exportLink = func(string, string) error { return errors.New("hard links are unsupported") }
	exportOpenFile = func(name string, flags int, perm os.FileMode) (*os.File, error) {
		// A competing creator wins between the fallback's lstat and O_EXCL
		// reservation. The fallback must return without replacing it.
		if name == destination {
			if err := os.WriteFile(name, []byte("racer won\n"), 0600); err != nil {
				return nil, err
			}
		}
		return os.OpenFile(name, flags, perm)
	}
	t.Cleanup(func() { exportLink, exportOpenFile = oldLink, oldOpen })

	if _, err := e.ExportFile(destination); err == nil {
		t.Fatal("race unexpectedly published")
	}
	contents, err := os.ReadFile(destination)
	if err != nil || string(contents) != "racer won\n" {
		t.Fatalf("racing destination changed: %q (%v)", contents, err)
	}
}

func TestExportFileFallbackAllowsOnlyOneConcurrentPublisher(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("native Windows uses no-replace MoveFile directly")
	}
	e, _ := fresh(t)
	directory := t.TempDir()
	destination := filepath.Join(directory, "concurrent.jsonl")
	oldLink := exportLink
	exportLink = func(string, string) error { return errors.New("hard links are unsupported") }
	t.Cleanup(func() { exportLink = oldLink })

	const workers = 8
	results := make(chan error, workers)
	var group sync.WaitGroup
	for i := 0; i < workers; i++ {
		group.Add(1)
		go func() {
			defer group.Done()
			_, err := e.ExportFile(destination)
			results <- err
		}()
	}
	group.Wait()
	close(results)

	successes := 0
	for err := range results {
		if err == nil {
			successes++
		}
	}
	if successes != 1 {
		t.Fatalf("expected one concurrent publisher, got %d", successes)
	}
	contents, err := os.ReadFile(destination)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(contents, []byte(`"kind":"header"`)) || !bytes.Contains(contents, []byte(`"kind":"footer"`)) {
		t.Fatalf("concurrent fallback published an incomplete archive: %q", contents[:min(len(contents), 256)])
	}
	if matches, err := filepath.Glob(filepath.Join(directory, ".cmpath-export-*")); err != nil || len(matches) != 0 {
		t.Fatalf("temporary export leaked after concurrent publication: %v", matches)
	}
}

func TestExportReservationMarkerSurvivesInterruptedPublicationPhase(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("native Windows uses no-replace MoveFile directly")
	}
	directory := t.TempDir()
	destination := filepath.Join(directory, "interrupted.jsonl")
	temporary := filepath.Join(directory, ".cmpath-export-interrupted")
	if err := os.WriteFile(temporary, []byte("complete archive bytes\n"), 0600); err != nil {
		t.Fatal(err)
	}
	marker, err := reserveExportDestination(destination, temporary)
	if err != nil {
		t.Fatal(err)
	}
	// This is the state a process kill leaves after the durable reservation
	// and before rename. It is not an archive and must be recognizable rather
	// than mistaken for a partial successful export.
	contents, err := os.ReadFile(destination)
	if err != nil || !bytes.Equal(contents, marker) {
		t.Fatalf("reservation marker was not durable/recognizable: %q (%v)", contents, err)
	}
	if bytes.Contains(contents, []byte(`"kind":"header"`)) || bytes.Contains(contents, []byte(`"kind":"footer"`)) {
		t.Fatal("interrupted reservation looked like an archive")
	}
	removeExportReservation(destination, marker)
	if _, err := os.Stat(destination); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("reservation cleanup failed: %v", err)
	}
}

type failingWriter struct{}

func (failingWriter) Write([]byte) (int, error) { return 0, errors.New("write failed") }

// retireTurn commits one turn and ages it so it becomes a retention candidate.
func retireTurn(t *testing.T, e *Engine, task Task, id string) {
	t.Helper()
	if _, err := e.Begin(request(task, id)); err != nil {
		t.Fatal(err)
	}
	if _, err := e.Commit(id, 1, Reply{Text: "saved"}); err != nil {
		t.Fatal(err)
	}
	if err := e.db.Exec("UPDATE cmp_turns SET updated_at='2020-01-01T00:00:00Z' WHERE request_id=?", id); err != nil {
		t.Fatal(err)
	}
}

// TestRetentionConvergesWithPreExistingTombstone pins the fixed defect: an
// out-of-band tombstone for a live journal row must not wedge retirement. The
// apply converges, the row is gone, and the tombstone that survives is the one
// that already existed -- its foreign fingerprint is never rewritten.
func TestRetentionConvergesWithPreExistingTombstone(t *testing.T) {
	e, task := fresh(t)
	retireTurn(t, e, task, "done")
	if err := e.db.Exec("INSERT INTO cmp_retired_turns VALUES('done','foreign-fingerprint','committed','2019-01-01T00:00:00Z')"); err != nil {
		t.Fatal(err)
	}
	plan, err := e.RetentionPlan("2021-01-01T00:00:00Z")
	if err != nil || plan.Rows["cmp_turns"] != 1 || plan.Rows["cmp_retired_turns"] != 1 {
		t.Fatal(plan, err)
	}
	applied, err := e.ApplyRetention(plan.Cutoff, plan.PlanHash)
	if err != nil || !applied.Applied {
		t.Fatal("apply did not converge", applied, err)
	}
	_, err = e.Turn("done")
	mustCode(t, err, "retired")
	rows, err := e.db.Query("SELECT fingerprint,status,retired_at FROM cmp_retired_turns WHERE request_id='done'")
	if err != nil || len(rows) != 1 {
		t.Fatal(rows, err)
	}
	if rows[0]["fingerprint"] != "foreign-fingerprint" || rows[0]["status"] != "committed" || rows[0]["retired_at"] != "2019-01-01T00:00:00Z" {
		t.Fatalf("existing tombstone was rewritten: %v", rows[0])
	}
	// The stale-plan contract still holds with nothing left to remove.
	_, err = e.ApplyRetention(plan.Cutoff, plan.PlanHash)
	mustCode(t, err, "stale_plan")
}

// TestRetentionConvergenceCheckFailsClosed blocks the turn delete with a
// database trigger and asserts the apply reports a coded error and leaves no
// partial state, rather than claiming applied=true with rows surviving. The
// trigger raises IGNORE, so the blocked delete is a silent no-op; only the
// convergence check notices it.
func TestRetentionConvergenceCheckFailsClosed(t *testing.T) {
	e, task := fresh(t)
	retireTurn(t, e, task, "held")
	if err := e.db.Exec("CREATE TRIGGER cmp_test_hold_delete BEFORE DELETE ON cmp_turns BEGIN SELECT RAISE(IGNORE); END"); err != nil {
		t.Fatal(err)
	}
	plan, err := e.RetentionPlan("2021-01-01T00:00:00Z")
	if err != nil || plan.Rows["cmp_turns"] != 1 {
		t.Fatal(plan, err)
	}
	report, err := e.ApplyRetention(plan.Cutoff, plan.PlanHash)
	mustCode(t, err, "integrity")
	if report.Applied {
		t.Fatal("reported applied with rows surviving", report)
	}
	rows, err := e.db.Query("SELECT request_id FROM cmp_turns WHERE request_id='held'")
	if err != nil || len(rows) != 1 {
		t.Fatal("partial state left behind", rows, err)
	}
	rows, err = e.db.Query("SELECT request_id FROM cmp_retired_turns WHERE request_id='held'")
	if err != nil || len(rows) != 0 {
		t.Fatal("rolled-back tombstone leaked", rows, err)
	}
	if _, err = e.Turn("held"); err != nil {
		t.Fatal(err)
	}
}

// TestRetiredGuardRefusesRenamingOntoRetiredID covers the UPDATE hole: a live
// turn cannot be renamed onto a retired ID, so the guard is not INSERT-only.
func TestRetiredGuardRefusesRenamingOntoRetiredID(t *testing.T) {
	e, task := fresh(t)
	retireTurn(t, e, task, "retired-id")
	if _, err := e.Begin(request(task, "old-id")); err != nil {
		t.Fatal(err)
	}
	if _, err := e.Commit("old-id", 1, Reply{Text: "saved"}); err != nil {
		t.Fatal(err)
	}
	plan, err := e.RetentionPlan("2021-01-01T00:00:00Z")
	if err != nil || plan.Rows["cmp_turns"] != 1 {
		t.Fatal(plan, err)
	}
	if _, err = e.ApplyRetention(plan.Cutoff, plan.PlanHash); err != nil {
		t.Fatal(err)
	}
	if err = e.db.Exec("UPDATE cmp_turns SET request_id='retired-id' WHERE request_id='old-id'"); err == nil || !strings.Contains(err.Error(), "retired request ID cannot be reused") {
		t.Fatal("rename onto a retired ID was not refused", err)
	}
	rows, err := e.db.Query("SELECT request_id FROM cmp_turns")
	if err != nil || len(rows) != 1 || rows[0]["request_id"] != "old-id" {
		t.Fatal(rows, err)
	}
	// The tombstone is still the only owner of the retired ID, and the engine
	// still refuses to execute it.
	_, err = e.Commit("retired-id", 1, Reply{Text: "again"})
	mustCode(t, err, "retired")
}

// TestRetentionNeverDeletesTombstones pins the chosen rule for the audit's
// tombstone-deletion finding: no delete guard is added. The engine's retention
// path only inserts tombstones -- it never deletes them -- so the documented
// "never pruned" claim describes the engine's behavior rather than an enforced
// invariant. Direct SQL can still remove a tombstone; the docs say so now.
func TestRetentionNeverDeletesTombstones(t *testing.T) {
	e, task := fresh(t)
	retireTurn(t, e, task, "pruned-check")
	plan, err := e.RetentionPlan("2021-01-01T00:00:00Z")
	if err != nil {
		t.Fatal(err)
	}
	if _, err = e.ApplyRetention(plan.Cutoff, plan.PlanHash); err != nil {
		t.Fatal(err)
	}
	rows, err := e.db.Query("SELECT request_id FROM cmp_retired_turns")
	if err != nil || len(rows) != 1 {
		t.Fatal(rows, err)
	}
	if err = e.db.Exec("DELETE FROM cmp_retired_turns"); err != nil {
		t.Fatal("unguarded tombstone delete changed behavior", err)
	}
	reported, err := e.Info()
	if err != nil || reported["harness_schema"] == nil {
		t.Fatal(reported, err)
	}
}

// TestRetentionPlanHashCoversTombstones pins the chosen rule for the audit's
// plan-hash finding: the hash now folds in the tombstones that already exist
// for the candidate IDs, so a tombstone inserted between the dry run and the
// apply is visible as a stale plan instead of being silently accepted.
func TestRetentionPlanHashCoversTombstones(t *testing.T) {
	e, task := fresh(t)
	retireTurn(t, e, task, "hashed")
	plan, err := e.RetentionPlan("2021-01-01T00:00:00Z")
	if err != nil {
		t.Fatal(err)
	}
	if err = e.db.Exec("INSERT INTO cmp_retired_turns VALUES('hashed','out-of-band','committed','2019-01-01T00:00:00Z')"); err != nil {
		t.Fatal(err)
	}
	if _, err = e.ApplyRetention(plan.Cutoff, plan.PlanHash); Code(err) != "stale_plan" {
		t.Fatal("tombstone inserted after dry run was invisible to the hash", err)
	}
	// Once a fresh plan accounts for the tombstone the apply still converges,
	// and the out-of-band tombstone is the one that survives.
	plan, err = e.RetentionPlan("2021-01-01T00:00:00Z")
	if err != nil {
		t.Fatal(err)
	}
	if _, err = e.ApplyRetention(plan.Cutoff, plan.PlanHash); err != nil {
		t.Fatal(err)
	}
	rows, err := e.db.Query("SELECT fingerprint FROM cmp_retired_turns WHERE request_id='hashed'")
	if err != nil || len(rows) != 1 || rows[0]["fingerprint"] != "out-of-band" {
		t.Fatal(rows, err)
	}
}

func TestRetentionProtectsUncertainAndRejectsStale(t *testing.T) {
	e, task := fresh(t)
	old := "2020-01-01T00:00:00Z"
	cutoff := "2021-01-01T00:00:00Z"
	done, err := e.Begin(request(task, "done"))
	if err != nil {
		t.Fatal(err)
	}
	if err = e.RecordModelRequest("done", 1, "m", "{}", 1, "test"); err != nil {
		t.Fatal(err)
	}
	if err = e.db.Exec("INSERT INTO cmp_model_responses VALUES('done','m','{}',?)", old); err != nil {
		t.Fatal(err)
	}
	if _, err = e.StartTool("done", 1, "t", "tool", map[string]any{}); err != nil {
		t.Fatal(err)
	}
	if _, err = e.FinishTool("done", 1, "t", map[string]any{"ok": true}); err != nil {
		t.Fatal(err)
	}
	if _, err = e.Commit(done.RequestID, 1, Reply{Text: "saved"}); err != nil {
		t.Fatal(err)
	}
	uncertain, err := e.Begin(request(task, "uncertain"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err = e.StartTool(uncertain.RequestID, 1, "t", "effect", nil); err != nil {
		t.Fatal(err)
	}
	if err = e.Abort(uncertain.RequestID, 1); err != nil {
		t.Fatal(err)
	}
	if _, err = e.Begin(request(task, "pending")); err != nil {
		t.Fatal(err)
	}
	if err = e.db.Exec("UPDATE cmp_turns SET updated_at=?", old); err != nil {
		t.Fatal(err)
	}
	before, _ := e.Info()
	plan, err := e.RetentionPlan(cutoff)
	if err != nil {
		t.Fatal(err)
	}
	if plan.Rows["cmp_turns"] != 1 || plan.Rows["cmp_model_responses"] != 1 || plan.ProtectedPending != 1 || plan.ProtectedUnresolved != 1 || plan.Applied {
		t.Fatal(plan)
	}
	if _, err = e.Turn("done"); err != nil {
		t.Fatal("dry run deleted turn")
	}
	if err = e.db.Exec("UPDATE cmp_model_responses SET response_json='changed' WHERE request_id='done'"); err != nil {
		t.Fatal(err)
	}
	_, err = e.ApplyRetention(cutoff, plan.PlanHash)
	mustCode(t, err, "stale_plan")
	if _, err = e.Turn("done"); err != nil {
		t.Fatal("stale plan deleted turn")
	}
	plan, err = e.RetentionPlan(cutoff)
	if err != nil {
		t.Fatal(err)
	}
	applied, err := e.ApplyRetention(cutoff, plan.PlanHash)
	if err != nil || !applied.Applied {
		t.Fatal(applied, err)
	}
	_, err = e.Turn("done")
	mustCode(t, err, "retired")
	for _, id := range []string{"pending", "uncertain"} {
		if _, err = e.Turn(id); err != nil {
			t.Fatal(id, err)
		}
	}
	after, _ := e.Info()
	if before["messages"] != after["messages"] || before["tasks"] != after["tasks"] {
		t.Fatal("lost base data")
	}
	tombstones, err := e.db.Query("SELECT * FROM cmp_retired_turns WHERE request_id='done'")
	if err != nil || len(tombstones) != 1 {
		t.Fatal(tombstones, err)
	}
	_, err = e.Begin(request(task, "done"))
	mustCode(t, err, "retired")
	_, err = e.ApplyRetention(cutoff, plan.PlanHash)
	mustCode(t, err, "stale_plan")
}

func TestRetentionTimestampBoundaryAndRollback(t *testing.T) {
	e, task := fresh(t)
	for _, id := range []string{"first", "second", "boundary"} {
		if _, err := e.Begin(request(task, id)); err != nil {
			t.Fatal(err)
		}
		if err := e.Abort(id, 1); err != nil {
			t.Fatal(err)
		}
	}
	if err := e.db.Exec("UPDATE cmp_turns SET updated_at='2020-01-01T01:00:00.09+01:00'"); err != nil {
		t.Fatal(err)
	}
	if err := e.db.Exec("UPDATE cmp_turns SET updated_at='2020-01-01T00:00:00.1Z' WHERE request_id='boundary'"); err != nil {
		t.Fatal(err)
	}
	plan, err := e.RetentionPlan("2020-01-01T00:00:00.100Z")
	if err != nil || plan.Rows["cmp_turns"] != 2 {
		t.Fatal(plan, err)
	}
	// Force a storage conflict after the first tombstone, testing atomic
	// rollback. A pre-existing tombstone is now an idempotent case, so the
	// conflict is provoked explicitly instead of via a UNIQUE violation.
	if err = e.db.Exec("CREATE TRIGGER cmp_test_tombstone_conflict BEFORE INSERT ON cmp_retired_turns WHEN NEW.request_id='second' BEGIN SELECT RAISE(ABORT,'storage conflict'); END"); err != nil {
		t.Fatal(err)
	}
	report, err := e.ApplyRetention(plan.Cutoff, plan.PlanHash)
	if err == nil || report.Applied {
		t.Fatal("expected rollback", report, err)
	}
	rows, err := e.db.Query("SELECT request_id FROM cmp_turns")
	if err != nil || len(rows) != 3 {
		t.Fatal(rows, err)
	}
	rows, err = e.db.Query("SELECT request_id FROM cmp_retired_turns")
	if err != nil || len(rows) != 0 {
		t.Fatal("leaked tombstone", rows, err)
	}
}

// TestRetiredUpdateGuardIsAddedToExistingDatabases covers the upgrade path for
// the UPDATE guard. journal.sql runs on every Open with CREATE TRIGGER IF NOT
// EXISTS, so a database that predates the guard gains it on reopen and a
// second reopen is harmless rather than accumulating triggers.
func TestRetiredUpdateGuardIsAddedToExistingDatabases(t *testing.T) {
	path := filepath.Join(t.TempDir(), "memory.db")
	e, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	task, err := e.CreateTask(CreateTask{Title: "Invoice review"})
	if err != nil {
		t.Fatal(err)
	}
	retireTurn(t, e, task, "retired-id")
	if _, err = e.Begin(request(task, "live-id")); err != nil {
		t.Fatal(err)
	}
	if _, err = e.Commit("live-id", 1, Reply{Text: "saved"}); err != nil {
		t.Fatal(err)
	}
	plan, err := e.RetentionPlan("2021-01-01T00:00:00Z")
	if err != nil {
		t.Fatal(err)
	}
	if _, err = e.ApplyRetention(plan.Cutoff, plan.PlanHash); err != nil {
		t.Fatal(err)
	}
	// Drop the guard to stand in for a database written before it existed, and
	// show the rename it blocks is otherwise accepted.
	if err = e.db.Exec("DROP TRIGGER cmp_turns_retired_update_guard"); err != nil {
		t.Fatal(err)
	}
	if err = e.db.Exec("UPDATE cmp_turns SET request_id='retired-id' WHERE request_id='live-id'"); err != nil {
		t.Fatalf("unguarded rename was refused: %v", err)
	}
	if err = e.db.Exec("UPDATE cmp_turns SET request_id='live-id' WHERE request_id='retired-id'"); err != nil {
		t.Fatal(err)
	}
	e.Close()

	reopened, err := Open(path)
	if err != nil {
		t.Fatal("reopening an existing database failed", err)
	}
	guards, err := reopened.db.Query("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'cmp_turns_retired%'")
	if err != nil || len(guards) != 2 {
		t.Fatal("guards not created on reopen", guards, err)
	}
	if err = reopened.db.Exec("UPDATE cmp_turns SET request_id='retired-id' WHERE request_id='live-id'"); err == nil || !strings.Contains(err.Error(), "retired request ID cannot be reused") {
		t.Fatal("reopened database still allows the rename", err)
	}
	reopened.Close()
	again, err := Open(path)
	if err != nil {
		t.Fatal("second reopen failed", err)
	}
	defer again.Close()
	guards, err = again.db.Query("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'cmp_turns_retired%'")
	if err != nil || len(guards) != 2 {
		t.Fatal("repeated open accumulated triggers", guards, err)
	}
}
