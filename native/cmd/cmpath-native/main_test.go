package main

import (
	"bytes"
	"encoding/json"
	"path/filepath"
	"strings"
	"testing"

	"cmpath.local/native/engine"
	"cmpath.local/native/internal/sqlite"
)

// journalWithBlobRequestID builds a database whose cmp_turns row carries a
// BLOB request_id. A TEXT PRIMARY KEY on a rowid table is neither typed nor
// NOT NULL, so SQLite accepts it, but the retention path cannot read it as
// text.
func journalWithBlobRequestID(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "journal.db")
	e, err := engine.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	e.Close()
	db, err := sqlite.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err = db.Exec(`INSERT INTO cmp_turns(
		request_id,task_id,fingerprint,request_json,status,generation,task_revision,
		package_json,reply_json,commit_hash,created_at,updated_at)
		VALUES(?,?,?,?,?,?,?,?,NULL,NULL,?,?)`,
		[]byte{0xFF, 0xFE}, 1, "fingerprint", "{}", "committed", 1, 1, "{}",
		"2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z"); err != nil {
		t.Fatal(err)
	}
	return path
}

func responses(t *testing.T, raw string) []response {
	t.Helper()
	out := []response{}
	for _, line := range strings.Split(strings.TrimSpace(raw), "\n") {
		if line == "" {
			continue
		}
		var decoded response
		if err := json.Unmarshal([]byte(line), &decoded); err != nil {
			t.Fatalf("bad response line %q: %v", line, err)
		}
		out = append(out, decoded)
	}
	return out
}

// A row the engine cannot decode must produce one coded error response, not a
// process-wide panic: the host keeps one persistent channel per engine, so a
// panic here loses every later request.
func TestServeSurvivesAnUndecodableJournalRow(t *testing.T) {
	e, err := engine.Open(journalWithBlobRequestID(t))
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()
	input := strings.Join([]string{
		`{"v":1,"id":"1","op":"retention_plan","args":{"cutoff":"2100-01-01T00:00:00Z"}}`,
		`{"v":1,"id":"2","op":"retention_apply","args":{"cutoff":"2100-01-01T00:00:00Z","plan_hash":"` + strings.Repeat("0", 64) + `"}}`,
		`{"v":1,"id":"3","op":"info","args":{}}`,
	}, "\n") + "\n"
	var out bytes.Buffer
	if err = serve(e, strings.NewReader(input), &out); err != nil {
		t.Fatal(err)
	}
	got := responses(t, out.String())
	if len(got) != 3 {
		t.Fatalf("want three responses, got %d: %s", len(got), out.String())
	}
	for i, want := range []string{"1", "2", "3"} {
		if got[i].ID != want {
			t.Fatalf("response %d has id %q", i, got[i].ID)
		}
	}
	for i := 0; i < 2; i++ {
		if got[i].Error == nil {
			t.Fatalf("operation %s reported success for an undecodable row: %s", got[i].ID, out.String())
		}
		// internal_error while the retention path asserts the column type;
		// integrity once it reads the column through Row.Text.
		if code := got[i].Error.Code; code != "internal_error" && code != "integrity" {
			t.Fatalf("operation %s failed with %q: %s", got[i].ID, code, out.String())
		}
	}
	if got[2].Error != nil || got[2].Result == nil {
		t.Fatalf("the channel stopped serving after one undecodable row: %s", out.String())
	}
}

// The same path with a well-formed row must keep working: the recovery
// boundary and the type-faithful decoding must not reject readable journals.
func TestServeRetainsReadableJournals(t *testing.T) {
	path := filepath.Join(t.TempDir(), "journal.db")
	e, err := engine.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()
	task, err := e.CreateTask(engine.CreateTask{Title: "retained"})
	if err != nil {
		t.Fatal(err)
	}
	db, err := sqlite.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if err = db.Exec(`INSERT INTO cmp_turns(
		request_id,task_id,fingerprint,request_json,status,generation,task_revision,
		package_json,reply_json,commit_hash,created_at,updated_at)
		VALUES(?,?,?,?,?,?,?,?,NULL,NULL,?,?)`,
		"text-request", task.ID, "fingerprint", "{}", "committed", 1, 1, "{}",
		"2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z"); err != nil {
		db.Close()
		t.Fatal(err)
	}
	db.Close()
	input := `{"v":1,"id":"1","op":"retention_plan","args":{"cutoff":"2100-01-01T00:00:00Z"}}` + "\n"
	var out bytes.Buffer
	if err = serve(e, strings.NewReader(input), &out); err != nil {
		t.Fatal(err)
	}
	got := responses(t, out.String())
	if len(got) != 1 || got[0].Error != nil {
		t.Fatalf("a readable journal row was refused: %s", out.String())
	}
	plan, ok := got[0].Result.(map[string]any)
	if !ok {
		t.Fatalf("unexpected result %T", got[0].Result)
	}
	rows, ok := plan["rows"].(map[string]any)
	if !ok || rows["cmp_turns"] != float64(1) {
		t.Fatalf("retention plan did not count the candidate: %v", plan)
	}
	if plan["plan_hash"] == "" {
		t.Fatalf("retention plan carried no fingerprint: %v", plan)
	}
}
