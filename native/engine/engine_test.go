package engine

import (
	"context"
	"encoding/json"
	"errors"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"cmpath.local/native/internal/sqlite"
)

func fresh(t *testing.T) (*Engine, Task) {
	t.Helper()
	e, err := Open(filepath.Join(t.TempDir(), "memory.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(e.Close)
	task, err := e.CreateTask(CreateTask{Title: "Invoice review", Aliases: []string{"monthly bill"}, Snapshot: map[string]any{"next": "verify"}})
	if err != nil {
		t.Fatal(err)
	}
	return e, task
}
func request(task Task, id string) Request {
	return Request{RequestID: id, TaskID: task.ID, Query: "What is the cobalt budget?", Budget: 2000, Scope: "task", Recent: 4}
}
func mustCode(t *testing.T, err error, code string) {
	t.Helper()
	if err == nil || Code(err) != code {
		t.Fatalf("want %s, got %v", code, err)
	}
}

func TestTaskGraphAndResolution(t *testing.T) {
	e, parent := fresh(t)
	child, err := e.CreateTask(CreateTask{Title: "Dependent invoice", Parents: []int64{parent.ID}})
	if err != nil {
		t.Fatal(err)
	}
	if len(child.Parents) != 1 || child.Parents[0] != parent.ID {
		t.Fatal(child)
	}
	r, err := e.Resolve("return to the monthly bill")
	if err != nil || r.TaskID != parent.ID {
		t.Fatal(r, err)
	}
	_, err = e.CreateTask(CreateTask{Title: "Other task", Aliases: []string{"monthly bill"}})
	if err != nil {
		t.Fatal(err)
	}
	r, err = e.Resolve("monthly bill")
	if err != nil || r.Status != "ambiguous" {
		t.Fatal(r, err)
	}
	r, err = e.Resolve("T9999 monthly bill")
	if err != nil || r.Status != "not_found" {
		t.Fatal(r, err)
	}
	_, err = e.CreateTask(CreateTask{Title: "Bad parent", Parents: []int64{9999}})
	mustCode(t, err, "not_found")
}
func TestAppendBatchRollbackAndFullContent(t *testing.T) {
	e, task := fresh(t)
	_, err := e.AppendBatch([]Append{{TaskID: task.ID, Role: "user", Content: "must roll back"}, {TaskID: task.ID, Role: "system", Content: "invalid role"}})
	mustCode(t, err, "invalid")
	info, _ := e.Info()
	if info["messages"] != int64(0) {
		t.Fatal(info)
	}
	original := "cobalt αβ café\x00 tail"
	rows, err := e.AppendBatch([]Append{{TaskID: task.ID, Role: "document", Content: original, Source: map[string]any{"large": json.Number("900719925474099312345")}}})
	if err != nil {
		t.Fatal(err)
	}
	hits, err := e.Search("cobalt", nil, 8)
	if err != nil || len(hits) != 1 || hits[0].Content != original || hits[0].ID != rows[0].ID {
		t.Fatal(hits, err)
	}
	if hits[0].Source["large"].(json.Number).String() != "900719925474099312345" {
		t.Fatal(hits[0].Source)
	}
}
func TestScopeSourcePairAndBudget(t *testing.T) {
	e, task := fresh(t)
	other, err := e.CreateTask(CreateTask{Title: "Unrelated"})
	if err != nil {
		t.Fatal(err)
	}
	rows, err := e.AppendBatch([]Append{{TaskID: task.ID, Role: "user", Content: "cobalt budget is 3400"}, {TaskID: other.ID, Role: "user", Content: "cobalt budget is 9900"}})
	if err != nil {
		t.Fatal(err)
	}
	if err = e.SetFact(task.ID, FactUpdate{Key: "budget", Value: 3400, EvidenceID: rows[0].ID}); err != nil {
		t.Fatal(err)
	}
	mustCode(t, e.SetFact(task.ID, FactUpdate{Key: "budget", Value: 9900, EvidenceID: rows[1].ID}), "invalid")
	r := request(task, "preview")
	r.Counting = "utf8-json-bytes"
	r.Budget = 2000
	r.Reserve = 100
	p, err := e.Preview(r)
	if err != nil {
		t.Fatal(err)
	}
	if p.UsedUnits != len([]byte(p.MessagesJSON)) || p.UsedUnits > 1900 {
		t.Fatal(p)
	}
	if !strings.Contains(p.MessagesJSON, "3400") || strings.Contains(p.MessagesJSON, "9900") || len(p.Citations) != 1 {
		t.Fatal(p)
	}
	r.Budget = 80
	r.Reserve = 0
	_, err = e.Begin(r)
	mustCode(t, err, "budget")
	info, _ := e.Info()
	if info["cmp_turns"] != int64(0) || info["messages"] != int64(2) {
		t.Fatal(info)
	}
}
func TestCustomCounterAndMandatorySnapshot(t *testing.T) {
	e, task := fresh(t)
	r := request(task, "custom")
	r.Budget = 5000
	c := Counter{"exact-test", func(m []Message) (int, error) { b, err := json.Marshal(m); return len(b), err }}
	p, err := e.Preview(r, c)
	if err != nil || p.Counting != "exact-test" || p.UsedUnits != len(p.MessagesJSON) {
		t.Fatal(p, err)
	}
	n := 0
	bad := Counter{"unstable", func([]Message) (int, error) { n++; return n, nil }}
	_, err = e.Preview(r, bad)
	mustCode(t, err, "invalid")
	huge, err := e.CreateTask(CreateTask{Title: "Large state", Snapshot: map[string]any{"plan": strings.Repeat("x", 12000)}})
	if err != nil {
		t.Fatal(err)
	}
	_, err = e.Begin(request(huge, "huge"))
	mustCode(t, err, "budget")
}
func TestRunReplayAndAtomicSnapshot(t *testing.T) {
	e, task := fresh(t)
	called := 0
	complete := func(context.Context, *Session) (Reply, error) {
		called++
		return Reply{Text: "Budget checked", Snapshot: map[string]any{"next": "invoice"}}, nil
	}
	first, err := e.Run(context.Background(), request(task, "once"), complete)
	if err != nil {
		t.Fatal(err)
	}
	second, err := e.Run(context.Background(), request(task, "once"), complete)
	if err != nil || called != 1 || !second.Replayed || first.Reply.Text != second.Reply.Text {
		t.Fatal(second, err, called)
	}
	saved, _ := e.Task(task.ID)
	if saved.Snapshot["next"] != "invoice" || saved.Revision != 2 {
		t.Fatal(saved)
	}
	info, _ := e.Info()
	if info["messages"] != int64(2) {
		t.Fatal(info)
	}
	changed := request(task, "once")
	changed.Query = "different"
	_, err = e.Begin(changed)
	mustCode(t, err, "conflict")
}
func TestPendingReplayRequiresRecovery(t *testing.T) {
	e, task := fresh(t)
	first, err := e.Begin(request(task, "pending"))
	if err != nil {
		t.Fatal(err)
	}
	duplicate, err := e.Begin(request(task, "pending"))
	if err != nil || duplicate.Created || !duplicate.Replayed || duplicate.Package.MessagesJSON != first.Package.MessagesJSON {
		t.Fatal(duplicate, err)
	}
	_, err = e.Begin(request(task, "other-request"))
	mustCode(t, err, "busy")
	_, err = e.Run(context.Background(), request(task, "pending"), func(context.Context, *Session) (Reply, error) {
		t.Fatal("callback must not execute")
		return Reply{}, nil
	})
	mustCode(t, err, "in_progress")
	recovered, err := e.Recover("pending", 1)
	if err != nil || recovered.Generation != 2 {
		t.Fatal(recovered, err)
	}
	_, err = e.Commit("pending", 1, Reply{Text: "stale"})
	mustCode(t, err, "fenced")
	_, err = e.Commit("pending", 2, Reply{Text: "recovered"})
	if err != nil {
		t.Fatal(err)
	}
}
func TestIndeterminateToolAndReconciliation(t *testing.T) {
	e, task := fresh(t)
	turn, err := e.Begin(request(task, "tool"))
	if err != nil {
		t.Fatal(err)
	}
	_, err = e.StartTool("tool", 1, "send-1", "external_action", map[string]any{"value": 1})
	if err != nil {
		t.Fatal(err)
	}
	s := &Session{Engine: e, Turn: turn}
	_, err = s.Tool("send-1", "external_action", map[string]any{"value": 1}, func() (any, error) { t.Fatal("unknown effect must not repeat"); return nil, nil })
	mustCode(t, err, "indeterminate_tool")
	_, err = e.Commit("tool", 1, Reply{Text: "done"})
	mustCode(t, err, "indeterminate_tool")
	recovered, err := e.Recover("tool", 1)
	if err != nil {
		t.Fatal(err)
	}
	_, err = e.FinishTool("tool", 1, "send-1", map[string]any{"confirmed": true})
	mustCode(t, err, "fenced")
	_, err = e.FinishTool("tool", recovered.Generation, "send-1", map[string]any{"confirmed": true})
	if err != nil {
		t.Fatal(err)
	}
	s.Turn = recovered
	out, err := s.Tool("send-1", "external_action", map[string]any{"value": 1}, func() (any, error) { t.Fatal("completed tool must not repeat"); return nil, nil })
	if err != nil || out.(map[string]any)["confirmed"] != true {
		t.Fatal(out, err)
	}
	_, err = e.Commit("tool", 2, Reply{Text: "done"})
	if err != nil {
		t.Fatal(err)
	}
}
func TestToolFailurePreservesIntent(t *testing.T) {
	e, task := fresh(t)
	turn, _ := e.Begin(request(task, "failure"))
	s := Session{e, turn}
	expected := errors.New("interrupted action")
	_, err := s.Tool("call", "action", nil, func() (any, error) { return nil, expected })
	if !errors.Is(err, expected) {
		t.Fatal(err)
	}
	calls, _ := e.Tools("failure")
	if len(calls) != 1 || calls[0].Status != "started" {
		t.Fatal(calls)
	}
}
func TestCommitRollbackOnInvalidFact(t *testing.T) {
	e, task := fresh(t)
	_, err := e.Begin(request(task, "rollback"))
	if err != nil {
		t.Fatal(err)
	}
	_, err = e.Commit("rollback", 1, Reply{Text: "must roll back", Snapshot: map[string]any{"next": "wrong"}, Facts: []FactUpdate{{Key: "missing", EvidenceID: 9999, Value: 1}}})
	mustCode(t, err, "invalid")
	saved, _ := e.Task(task.ID)
	turn, _ := e.Turn("rollback")
	info, _ := e.Info()
	if saved.Revision != 1 || saved.Snapshot["next"] != "verify" || turn.Status != "pending" || info["messages"] != int64(1) {
		t.Fatal(saved, turn.Status, info)
	}
}
func TestStaleSnapshotConflict(t *testing.T) {
	e, task := fresh(t)
	_, err := e.Begin(request(task, "stale"))
	if err != nil {
		t.Fatal(err)
	}
	err = e.transaction(func() error {
		return e.db.Exec("UPDATE tasks SET snapshot='{}',revision=revision+1 WHERE id=?", task.ID)
	})
	if err != nil {
		t.Fatal(err)
	}
	_, err = e.Commit("stale", 1, Reply{Text: "stale"})
	mustCode(t, err, "conflict")
	_, err = e.Recover("stale", 1)
	mustCode(t, err, "conflict")
}
func TestModelPayloadAuditAndBudget(t *testing.T) {
	e, task := fresh(t)
	_, err := e.Begin(request(task, "model"))
	if err != nil {
		t.Fatal(err)
	}
	body := `{"messages":[],"tools":[{"type":"function"}]}`
	if err = e.RecordModelRequest("model", 1, "model-1", body, 100, "caller-tokenizer-v1"); err != nil {
		t.Fatal(err)
	}
	if err = e.RecordModelRequest("model", 1, "model-1", body, 100, "caller-tokenizer-v1"); err != nil {
		t.Fatal(err)
	}
	mustCode(t, e.RecordModelRequest("model", 1, "model-1", `{}`, 100, "caller-tokenizer-v1"), "conflict")
	mustCode(t, e.RecordModelRequest("model", 1, "model-2", body, 2001, "caller-tokenizer-v1"), "budget")
	info, _ := e.Info()
	if info["cmp_model_calls"] != int64(1) {
		t.Fatal(info)
	}
}
func TestAbortPreservesHistory(t *testing.T) {
	e, task := fresh(t)
	_, err := e.Begin(request(task, "abort"))
	if err != nil {
		t.Fatal(err)
	}
	if err = e.Abort("abort", 1); err != nil {
		t.Fatal(err)
	}
	turn, _ := e.Turn("abort")
	if turn.Status != "aborted" {
		t.Fatal(turn)
	}
	_, err = e.Begin(request(task, "next"))
	if err != nil {
		t.Fatal(err)
	}
	info, _ := e.Info()
	if info["messages"] != int64(2) {
		t.Fatal(info)
	}
}
func TestReopenKeepsExactContextAndLargeInteger(t *testing.T) {
	path := filepath.Join(t.TempDir(), "reopen.db")
	e, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	task, err := e.CreateTask(CreateTask{Title: "Large identity", Snapshot: map[string]any{"id": json.Number("900719925474099312345")}})
	if err != nil {
		t.Fatal(err)
	}
	one, err := e.Begin(request(task, "persist"))
	if err != nil {
		t.Fatal(err)
	}
	e.Close()
	e, err = Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()
	two, err := e.Turn("persist")
	if err != nil {
		t.Fatal(err)
	}
	if one.Package.MessagesJSON != two.Package.MessagesJSON || two.Package.Snapshot["id"].(json.Number).String() != "900719925474099312345" {
		t.Fatal(two)
	}
}
func TestConcurrentBeginAcrossConnections(t *testing.T) {
	path := filepath.Join(t.TempDir(), "shared.db")
	a, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer a.Close()
	task, err := a.CreateTask(CreateTask{Title: "shared"})
	if err != nil {
		t.Fatal(err)
	}
	b, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer b.Close()
	var created atomic.Int64
	var wg sync.WaitGroup
	for i := 0; i < 12; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			e := a
			if i%2 == 1 {
				e = b
			}
			turn, er := e.Begin(request(task, "concurrent"))
			if er != nil {
				t.Error(er)
				return
			}
			if turn.Created {
				created.Add(1)
			}
		}(i)
	}
	wg.Wait()
	if created.Load() != 1 {
		t.Fatal(created.Load())
	}
	info, _ := a.Info()
	if info["messages"] != int64(1) {
		t.Fatal(info)
	}
}
func TestSlowCallbackDoesNotHoldTransaction(t *testing.T) {
	e, task := fresh(t)
	other, err := e.CreateTask(CreateTask{Title: "Parallel task"})
	if err != nil {
		t.Fatal(err)
	}
	entered := make(chan struct{})
	release := make(chan struct{})
	done := make(chan error, 1)
	go func() {
		_, err := e.Run(context.Background(), request(task, "slow"), func(context.Context, *Session) (Reply, error) {
			close(entered)
			<-release
			return Reply{Text: "done"}, nil
		})
		done <- err
	}()
	<-entered
	independent := make(chan error, 1)
	go func() { _, err := e.Begin(request(other, "parallel")); independent <- err }()
	select {
	case err := <-independent:
		if err != nil {
			t.Error(err)
		}
	case <-time.After(2 * time.Second):
		t.Error("slow callback held the storage lock")
	}
	close(release)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}
func TestClosedEngine(t *testing.T) {
	e, task := fresh(t)
	e.Close()
	_, err := e.Task(task.ID)
	mustCode(t, err, "closed")
}

// TestLineEndingsToLFNormalizesStatements covers the normalization the embedded
// schemas go through. A checkout may hand the file over with any line ending, and
// the schema a database is created with must not depend on that, so CRLF and
// lone CR are reduced to LF while a carriage return inside a string literal --
// data the caller asked to store -- is left alone.
func TestLineEndingsToLFNormalizesStatements(t *testing.T) {
	for _, tc := range []struct {
		name, in, want string
	}{
		{"already LF", "a\nb\n", "a\nb\n"},
		{"CRLF", "a\r\nb\r\n", "a\nb\n"},
		{"lone CR", "a\rb\r", "a\nb\n"},
		{"mixed", "a\r\nb\nc\rd", "a\nb\nc\nd"},
		{"literal kept", "SELECT 'a\r\nb'\r\n", "SELECT 'a\r\nb'\n"},
		{"escaped quote in literal kept", "SELECT 'it''s\r\nhere'\r\n", "SELECT 'it''s\r\nhere'\n"},
		{"apostrophe in a comment does not open a literal", "-- task's history\r\nSELECT 'a\r\nb'\r\n", "-- task's history\nSELECT 'a\r\nb'\n"},
		{"comment normalized", "-- one\r\n-- two\r\nSELECT 1", "-- one\n-- two\nSELECT 1"},
		{"block comment normalized", "/* one\r\ntwo */\r\nSELECT 1", "/* one\ntwo */\nSELECT 1"},
		{"unterminated literal", "SELECT 'a\r\nb", "SELECT 'a\r\nb"},
	} {
		if got := lineEndingsToLF(tc.in); got != tc.want {
			t.Errorf("%s: lineEndingsToLF(%q) = %q, want %q", tc.name, tc.in, got, tc.want)
		}
	}
}

// TestEmbeddedSchemasCarryNoCarriageReturn pins the property the normalization
// exists to guarantee. The compiler hands a raw string literal over with LF, so
// the frozen cmpTurnsRebuildDDL is LF whatever the checkout did; the embedded
// scripts must be too, or a fresh database and a rebuilt one would store
// definitions that differ by line endings.
func TestEmbeddedSchemasCarryNoCarriageReturn(t *testing.T) {
	if strings.Contains(cmpTurnsRebuildDDL, "\r") {
		t.Fatal("the frozen rebuild DDL carries a carriage return")
	}
	for name, schema := range map[string]string{"base.sql": baseSchema, "journal.sql": journalSchema} {
		if strings.Contains(schema, "\r") {
			t.Fatalf("%s still carries a carriage return after normalization", name)
		}
		if strings.TrimSpace(schema) == "" {
			t.Fatalf("%s normalized to nothing", name)
		}
	}
	// The scripts have to stay loadable: normalization must not leave a
	// statement split in a way SQLite rejects. journal.sql references the base
	// tables, so the two go on in the order Open applies them.
	db := rawOpen(t, filepath.Join(t.TempDir(), "normalized.db"))
	legacyBase(t, db)
	if err := db.Script(journalSchema); err != nil {
		t.Fatalf("the normalized schemas no longer parse: %v", err)
	}
}

// TestEmbeddedSchemasSurviveACRLFCheckout is the regression the Windows job
// caught: the schema a database is created with must be the same declaration
// whichever line endings the checkout used. Both spellings of journal.sql are
// applied here -- the checkout is simulated in the test so the property is
// exercised on every platform -- and the definitions they store must agree even
// though SQLite keeps the bytes it was given.
func TestEmbeddedSchemasSurviveACRLFCheckout(t *testing.T) {
	windows := strings.ReplaceAll(journalSchema, "\n", "\r\n")
	if windows == journalSchema {
		t.Fatal("the CRLF spelling is identical to the embedded schema; this test would prove nothing")
	}
	if got := lineEndingsToLF(windows); got != journalSchema {
		t.Fatal("a CRLF copy of journal.sql did not normalize back to the embedded schema")
	}
	// The engine executes the normalized form, so a checkout cannot reach it.
	unix, dos := rawOpen(t, filepath.Join(t.TempDir(), "unix.db")), rawOpen(t, filepath.Join(t.TempDir(), "windows.db"))
	for _, db := range []*sqlite.DB{unix, dos} {
		legacyBase(t, db)
	}
	if err := unix.Script(journalSchema); err != nil {
		t.Fatal(err)
	}
	if err := dos.Script(windows); err != nil {
		t.Fatal(err)
	}
	storedUnix, storedDos := objectSQL(t, unix, "table", "cmp_turns"), objectSQL(t, dos, "table", "cmp_turns")
	if storedUnix == storedDos {
		t.Fatal("SQLite did not store the CRLF script as written; this test no longer exercises the difference")
	}
	if ddlDefinition(storedUnix) != ddlDefinition(storedDos) {
		t.Fatalf("the two checkouts declare different tables:\nunix    %q\nwindows %q", storedUnix, storedDos)
	}
	for _, stored := range []string{storedUnix, storedDos} {
		if strings.Contains(ddlDefinition(stored), "\r") {
			t.Fatal("ddlDefinition left a carriage return behind")
		}
	}
}
