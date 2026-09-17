package engine

import (
	"cmpath.local/native/internal/sqlite"
	"path/filepath"
	"testing"
)

func TestModelResponseOrderPreservationAndFencing(t *testing.T) {
	e, task := fresh(t)
	turn, err := e.Begin(request(task, "response-journal"))
	if err != nil {
		t.Fatal(err)
	}
	raw := `{"choices":[{"message":{"role":"assistant","content":"café \\u0000 preserved"}}],"large":900719925474099312345}`
	mustCode(t, e.RecordModelResponse(turn.RequestID, 1, "model-2", raw), "not_found")
	for _, id := range []string{"model-2", "model-10", "model-1"} {
		if err = e.RecordModelRequest(turn.RequestID, 1, id, `{"messages":[],"tools":[]}`, 8, "test-units"); err != nil {
			t.Fatal(err)
		}
	}
	if err = e.RecordModelResponse(turn.RequestID, 1, "model-2", raw); err != nil {
		t.Fatal(err)
	}
	if err = e.RecordModelResponse(turn.RequestID, 1, "model-2", raw); err != nil {
		t.Fatal(err)
	}
	mustCode(t, e.RecordModelResponse(turn.RequestID, 1, "model-2", `{"different":true}`), "conflict")
	calls, err := e.ModelCalls(turn.RequestID)
	if err != nil || len(calls) != 3 || calls[0].CallID != "model-2" || calls[1].CallID != "model-10" || calls[2].CallID != "model-1" {
		t.Fatal(calls, err)
	}
	if calls[0].ResponseJSON == nil || *calls[0].ResponseJSON != raw || calls[1].ResponseJSON != nil {
		t.Fatal(calls)
	}
	recovered, err := e.Recover(turn.RequestID, 1)
	if err != nil {
		t.Fatal(err)
	}
	mustCode(t, e.RecordModelResponse(turn.RequestID, 1, "model-1", raw), "fenced")
	if err = e.RecordModelResponse(turn.RequestID, recovered.Generation, "model-1", raw); err != nil {
		t.Fatal(err)
	}
}

func TestModelResponseValidationAndTerminalState(t *testing.T) {
	e, task := fresh(t)
	turn, err := e.Begin(request(task, "validate-model"))
	if err != nil {
		t.Fatal(err)
	}
	if err = e.RecordModelRequest(turn.RequestID, 1, "model", `{}`, 1, "test"); err != nil {
		t.Fatal(err)
	}
	for _, raw := range []string{`null`, `[]`, `"text"`, `{"n":NaN}`, "{\xff}"} {
		mustCode(t, e.RecordModelResponse(turn.RequestID, 1, "model", raw), "invalid")
	}
	if _, err = e.Commit(turn.RequestID, 1, Reply{Text: "confirmed"}); err != nil {
		t.Fatal(err)
	}
	mustCode(t, e.RecordModelResponse(turn.RequestID, 1, "model", `{}`), "state")
	calls, err := e.ModelCalls(turn.RequestID)
	if err != nil || len(calls) != 1 || calls[0].ResponseJSON != nil {
		t.Fatal(calls, err)
	}
}

func TestHarnessSchemaUpgradePreservesPendingRecords(t *testing.T) {
	path := filepath.Join(t.TempDir(), "old.db")
	db, err := sqlite.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if err = db.Script(baseSchema); err != nil {
		t.Fatal(err)
	}
	// Construct the original v1 journal exactly up to its new additive tables.
	legacy := `BEGIN IMMEDIATE;
 CREATE TABLE cmp_harness_meta(singleton INTEGER PRIMARY KEY,version INTEGER NOT NULL);
 INSERT INTO cmp_harness_meta VALUES(1,1);COMMIT;`
	if err = db.Script(legacy); err != nil {
		t.Fatal(err)
	}
	db.Close()
	e, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()
	info, err := e.Info()
	if err != nil || info["harness_schema"] != 4 {
		t.Fatal(info, err)
	}
	rows, err := e.db.Query("SELECT version FROM cmp_harness_meta")
	if err != nil || rows[0]["version"] != int64(4) {
		t.Fatal(rows, err)
	}
	task, err := e.CreateTask(CreateTask{Title: "upgraded"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = e.Begin(request(task, "migrated")); err != nil {
		t.Fatal(err)
	}
}
