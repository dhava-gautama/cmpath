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
	// Force a storage conflict after the first deletion, testing atomic rollback.
	if err = e.db.Exec("INSERT INTO cmp_retired_turns SELECT request_id,fingerprint,status,updated_at FROM cmp_turns WHERE request_id='second'"); err != nil {
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
	rows, err = e.db.Query("SELECT request_id FROM cmp_retired_turns WHERE request_id='first'")
	if err != nil || len(rows) != 0 {
		t.Fatal("leaked tombstone", rows, err)
	}
}
