package engine

import (
	"bytes"
	"fmt"
	"path/filepath"
	"strings"
	"testing"

	"cmpath.local/native/internal/sqlite"
)

func scoped(task Task, id, scope string) Request {
	r := request(task, id)
	r.Consistency, r.Scope = "scope", scope
	return r
}

func mustExec(t *testing.T, db *sqlite.DB, sql string, args ...any) {
	t.Helper()
	if err := db.Exec(sql, args...); err != nil {
		t.Fatal(err)
	}
}

func TestScopeFreshnessMutationCoverage(t *testing.T) {
	for _, mutation := range []string{"fact_revision", "fact_update", "fact_delete", "parent_append", "empty_result", "snapshot", "alias", "ancestry", "aba"} {
		t.Run(mutation, func(t *testing.T) {
			e, parent := fresh(t)
			child, err := e.CreateTask(CreateTask{Title: "child", Parents: []int64{parent.ID}})
			if err != nil {
				t.Fatal(err)
			}
			ev, err := e.AppendBatch([]Append{{TaskID: child.ID, Role: "document", Content: "record"}})
			if err != nil {
				t.Fatal(err)
			}
			if err = e.SetFact(child.ID, FactUpdate{Key: "budget", Value: 3, EvidenceID: ev[0].ID}); err != nil {
				t.Fatal(err)
			}
			if mutation == "empty_result" {
				mustExec(t, e.db, "DELETE FROM facts WHERE task_id=?", child.ID)
			}
			r := scoped(child, "guard", "lineage")
			r.Query, r.Recent = "unobtainium", 0
			turn, err := e.Begin(r)
			if err != nil {
				t.Fatal(err)
			}
			// A separate raw SQLite connection represents a legacy Python writer.
			paths, err := e.db.Query("PRAGMA database_list")
			if err != nil {
				t.Fatal(err)
			}
			writer, err := sqlite.Open(paths[0]["file"].(string))
			if err != nil {
				t.Fatal(err)
			}
			defer writer.Close()
			switch mutation {
			case "fact_revision":
				mustExec(t, writer, "INSERT INTO facts(task_id,key,revision,value,evidence_id,retracted,created_at) VALUES(?,'budget',2,'4',?,0,?)", child.ID, ev[0].ID, now())
			case "fact_update":
				mustExec(t, writer, "UPDATE facts SET value='4' WHERE task_id=?", child.ID)
			case "fact_delete":
				mustExec(t, writer, "DELETE FROM facts WHERE task_id=?", child.ID)
			case "parent_append":
				mustExec(t, writer, "INSERT INTO messages(task_id,role,content,source,created_at) VALUES(?,'document','parent change','{}',?)", parent.ID, now())
			case "empty_result":
				hits, er := e.Search("unobtainium", []int64{child.ID}, 10)
				// Begin's user evidence itself matches; no preexisting matching document was retrieved.
				if er != nil || len(hits) != 1 || len(turn.Package.Citations) != 0 {
					t.Fatal(hits, er)
				}
				mustExec(t, writer, "INSERT INTO messages(task_id,role,content,source,created_at) VALUES(?,'document','unobtainium discovered','{}',?)", child.ID, now())
			case "snapshot":
				mustExec(t, writer, "UPDATE tasks SET snapshot='{}',revision=revision+1 WHERE id=?", child.ID)
			case "alias":
				mustExec(t, writer, "INSERT INTO aliases VALUES(?,'new alias')", parent.ID)
			case "ancestry":
				mustExec(t, writer, "DELETE FROM dependencies WHERE child=?", child.ID)
			case "aba":
				mustExec(t, writer, "UPDATE facts SET value='4' WHERE task_id=?", child.ID)
				mustExec(t, writer, "UPDATE facts SET value='3' WHERE task_id=?", child.ID)
			}
			if mutation != "snapshot" {
				current, er := e.Task(child.ID)
				if er != nil || current.Revision != turn.TaskRevision {
					t.Fatal(current, er)
				}
			}
			_, err = e.StartTool("guard", 1, "new", "write", nil)
			mustCode(t, err, "stale_context")
			mustCode(t, e.RecordModelRequest("guard", 1, "new", "{}", 1, "test"), "stale_context")
			_, err = e.Commit("guard", 1, Reply{Text: "stale"})
			mustCode(t, err, "stale_context")
			calls, er := e.Tools("guard")
			if er != nil || len(calls) != 0 {
				t.Fatal(calls, er)
			}
		})
	}
}

func TestScopeOutsideChangesAndOwnToolWrites(t *testing.T) {
	for _, scope := range []string{"task", "lineage", "all"} {
		t.Run(scope, func(t *testing.T) {
			e, task := fresh(t)
			outside, err := e.CreateTask(CreateTask{Title: "outside"})
			if err != nil {
				t.Fatal(err)
			}
			_, err = e.Begin(scoped(task, "own", scope))
			if err != nil {
				t.Fatal(err)
			}
			for i := 0; i < 2; i++ {
				id := fmt.Sprint(i)
				if _, err = e.StartTool("own", 1, id, "read", nil); err != nil {
					t.Fatal(err)
				}
				if _, err = e.FinishTool("own", 1, id, "result"); err != nil {
					t.Fatal(err)
				}
			}
			if err = e.RecordModelRequest("own", 1, "model", "{}", 1, "test"); err != nil {
				t.Fatal(err)
			}
			if _, err = e.Commit("own", 1, Reply{Text: "fresh"}); err != nil {
				t.Fatal(err)
			}
			_, err = e.Begin(scoped(task, "outside", scope))
			if err != nil {
				t.Fatal(err)
			}
			if _, err = e.AppendBatch([]Append{{TaskID: outside.ID, Role: "document", Content: "unrelated"}}); err != nil {
				t.Fatal(err)
			}
			_, err = e.Commit("outside", 1, Reply{Text: "allowed only for scoped tasks"})
			if scope == "all" {
				mustCode(t, err, "stale_context")
			} else if err != nil {
				t.Fatal(err)
			}
			// A terminal replay is its historical saved result, regardless of later changes.
			mustExec(t, e.db, "UPDATE tasks SET snapshot='{}',revision=revision+1 WHERE id=?", task.ID)
			replay, err := e.Commit("own", 1, Reply{Text: "fresh"})
			if err != nil || !replay.Replayed || replay.Reply.Text != "fresh" {
				t.Fatal(replay, err)
			}
		})
	}
}

func TestScopeStaleOutcomesAndRecovery(t *testing.T) {
	for _, scope := range []string{"task", "all"} {
		t.Run(scope, func(t *testing.T) {
			e, task := fresh(t)
			if _, err := e.Begin(scoped(task, "race", scope)); err != nil {
				t.Fatal(err)
			}
			if _, err := e.StartTool("race", 1, "tool", "write", nil); err != nil {
				t.Fatal(err)
			}
			if err := e.RecordModelRequest("race", 1, "model", "{}", 1, "test"); err != nil {
				t.Fatal(err)
			}
			if _, err := e.AppendBatch([]Append{{TaskID: task.ID, Role: "document", Content: "external update during remote execution"}}); err != nil {
				t.Fatal(err)
			}
			if _, err := e.Recover("race", 1); err != nil {
				t.Fatal(err)
			}
			call, err := e.FinishTool("race", 2, "tool", "confirmed")
			if err != nil || call.Status != "completed" || call.EvidenceID == 0 {
				t.Fatal(call, err)
			}
			if err = e.RecordModelResponse("race", 2, "model", `{"confirmed":true}`); err != nil {
				t.Fatal(err)
			}
			call, err = e.StartTool("race", 2, "tool", "write", nil)
			if err != nil || call.Created || call.Result != "confirmed" {
				t.Fatal(call, err)
			}
			if _, err = e.FinishTool("race", 2, "tool", "confirmed"); err != nil {
				t.Fatal(err)
			}
			if err = e.RecordModelRequest("race", 2, "model", "{}", 1, "test"); err != nil {
				t.Fatal(err)
			}
			_, err = e.StartTool("race", 2, "new", "write", nil)
			mustCode(t, err, "stale_context")
			_, err = e.Commit("race", 2, Reply{Text: "must not publish"})
			mustCode(t, err, "stale_context")
		})
	}
}

func TestScopePolicySerializationAndMigration(t *testing.T) {
	for _, version := range []int{1, 2, 3} {
		t.Run(fmt.Sprint(version), func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "migration.db")
			e, err := Open(path)
			if err != nil {
				t.Fatal(err)
			}
			task, err := e.CreateTask(CreateTask{Title: "legacy"})
			if err != nil {
				t.Fatal(err)
			}
			r := request(task, "pending")
			if _, err = e.Begin(r); err != nil {
				t.Fatal(err)
			}
			rows, err := e.db.Query("SELECT request_json,fingerprint FROM cmp_turns")
			if err != nil || strings.Contains(rows[0]["request_json"].(string), "consistency") {
				t.Fatal(rows, err)
			}
			original := rows[0]["request_json"].(string)
			r.Consistency = "snapshot"
			if replay, er := e.Begin(r); er != nil || !replay.Replayed {
				t.Fatal(replay, er)
			}
			r.Consistency = "unknown"
			_, err = e.Begin(r)
			mustCode(t, err, "invalid")
			if version < 3 {
				triggers, er := e.db.Query("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'cmp_epoch_%'")
				if er != nil {
					t.Fatal(er)
				}
				for _, row := range triggers {
					mustExec(t, e.db, "DROP TRIGGER "+row["name"].(string))
				}
				mustExec(t, e.db, "DROP TABLE cmp_turn_basis")
				mustExec(t, e.db, "DROP TABLE cmp_scope_epochs")
				mustExec(t, e.db, "UPDATE cmp_harness_meta SET version=?", version)
			}
			e.Close()
			e, err = Open(path)
			if err != nil {
				t.Fatal(err)
			}
			defer e.Close()
			rows, err = e.db.Query("SELECT request_json FROM cmp_turns")
			if err != nil || rows[0]["request_json"] != original {
				t.Fatal(rows, err)
			}
			// Old snapshot turns remain usable despite data changes without task revision changes.
			if _, err = e.AppendBatch([]Append{{TaskID: task.ID, Role: "document", Content: "legacy external change"}}); err != nil {
				t.Fatal(err)
			}
			if _, err = e.Commit("pending", 1, Reply{Text: "legacy policy"}); err != nil {
				t.Fatal(err)
			}
			if _, err = e.Begin(scoped(task, "new", "task")); err != nil {
				t.Fatal(err)
			}
			if _, err = e.Commit("new", 1, Reply{Text: "migrated fresh policy"}); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestScopeBasisExportRetention(t *testing.T) {
	e, task := fresh(t)
	if _, err := e.Begin(scoped(task, "retire", "task")); err != nil {
		t.Fatal(err)
	}
	if _, err := e.Commit("retire", 1, Reply{Text: "saved"}); err != nil {
		t.Fatal(err)
	}
	var archive bytes.Buffer
	report, err := e.Export(&archive)
	if err != nil || report.Rows["cmp_turn_basis"] != 1 || report.Rows["cmp_scope_epochs"] != 2 {
		t.Fatal(report, err)
	}
	mustExec(t, e.db, "UPDATE cmp_turns SET updated_at='2020-01-01T00:00:00Z'")
	plan, err := e.RetentionPlan("2021-01-01T00:00:00Z")
	if err != nil || plan.Rows["cmp_turn_basis"] != 1 {
		t.Fatal(plan, err)
	}
	before, err := e.scopeEpoch(task.ID)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = e.ApplyRetention(plan.Cutoff, plan.PlanHash); err != nil {
		t.Fatal(err)
	}
	rows, err := e.db.Query("SELECT * FROM cmp_turn_basis")
	if err != nil || len(rows) != 0 {
		t.Fatal(rows, err)
	}
	after, err := e.scopeEpoch(task.ID)
	if err != nil || before != after {
		t.Fatal(before, after, err)
	}
}

func TestScopeMissingClockFailsBegin(t *testing.T) {
	e, parent := fresh(t)
	child, err := e.CreateTask(CreateTask{Title: "child", Parents: []int64{parent.ID}})
	if err != nil {
		t.Fatal(err)
	}
	mustExec(t, e.db, "DELETE FROM cmp_scope_epochs WHERE task_id=?", parent.ID)
	_, err = e.Begin(scoped(child, "missing", "lineage"))
	mustCode(t, err, "integrity")
	_, err = e.Turn("missing")
	mustCode(t, err, "not_found")
}

func TestScopedDerivedProvenanceAndActionEligibility(t *testing.T) {
	e, task := fresh(t)
	ev, err := e.AppendBatch([]Append{{TaskID: task.ID, Role: "document", Content: "approved amount is 3"}})
	if err != nil {
		t.Fatal(err)
	}
	turn, err := e.Begin(scoped(task, "derived", "task"))
	if err != nil {
		t.Fatal(err)
	}
	_, err = e.Commit("derived", turn.Generation, Reply{Text: "derived", Snapshot: map[string]any{"amount": 3}})
	mustCode(t, err, "provenance_required")
	if _, err = e.Commit("derived", turn.Generation, Reply{
		Text:       "derived",
		Snapshot:   map[string]any{"amount": 3},
		Provenance: []ProvenanceRef{{EvidenceID: ev[0].ID, Kind: "supports"}},
	}); err != nil {
		t.Fatal(err)
	}
	rows, err := e.db.Query("SELECT evidence_id,kind FROM cmp_turn_provenance WHERE request_id='derived'")
	if err != nil || len(rows) != 1 || rows[0]["evidence_id"] != ev[0].ID || rows[0]["kind"] != "supports" {
		t.Fatal(rows, err)
	}

	e2, task2 := fresh(t)
	ev2, err := e2.AppendBatch([]Append{{TaskID: task2.ID, Role: "document", Content: "temporary evidence"}})
	if err != nil {
		t.Fatal(err)
	}
	if err = e2.SetFact(task2.ID, FactUpdate{Key: "amount", Value: 3, EvidenceID: ev2[0].ID, Retracted: true}); err != nil {
		t.Fatal(err)
	}
	if _, err = e2.Begin(scoped(task2, "retracted", "task")); err != nil {
		t.Fatal(err)
	}
	_, err = e2.Commit("retracted", 1, Reply{Text: "derived", Snapshot: map[string]any{"amount": 3}, Provenance: []ProvenanceRef{{EvidenceID: ev2[0].ID, Kind: "supports"}}})
	mustCode(t, err, "stale_context")

	e3, task3 := fresh(t)
	if _, err = e3.Begin(scoped(task3, "lease", "task")); err != nil {
		t.Fatal(err)
	}
	if _, err = e3.StartTool("lease", 1, "send", "external_write", map[string]any{"amount": 3}); err != nil {
		t.Fatal(err)
	}
	lease, err := e3.ActionEligible("lease", 1, "send", 1000)
	if err != nil || lease.Token == "" || lease.ExpiresAt <= 0 {
		t.Fatal(lease, err)
	}
	if _, err = e3.AppendBatch([]Append{{TaskID: task3.ID, Role: "document", Content: "changed after eligibility"}}); err != nil {
		t.Fatal(err)
	}
	_, err = e3.ActionEligible("lease", 1, "send", 1000)
	mustCode(t, err, "stale_context")
}

func TestScopeEpochAllMutationsAndOwnerMoves(t *testing.T) {
	for _, table := range []string{"tasks", "messages", "facts", "dependencies", "aliases"} {
		t.Run(table, func(t *testing.T) {
			e, first := fresh(t)
			second, err := e.CreateTask(CreateTask{Title: "second"})
			if err != nil {
				t.Fatal(err)
			}
			third, err := e.CreateTask(CreateTask{Title: "third"})
			if err != nil {
				t.Fatal(err)
			}
			ev, err := e.AppendBatch([]Append{{TaskID: third.ID, Role: "document", Content: "source"}})
			if err != nil {
				t.Fatal(err)
			}
			// These writes use raw SQL so the trigger behavior does not depend on engine methods.
			check := func(sql string, args []any, affected ...int64) {
				t.Helper()
				before := map[int64]int64{}
				ids := []int64{0, first.ID, second.ID, third.ID, 99}
				for _, id := range ids {
					epoch, _ := e.scopeEpoch(id)
					before[id] = epoch
				}
				mustExec(t, e.db, sql, args...)
				want := map[int64]int64{0: 1}
				for _, id := range affected {
					want[id] = 1
				}
				for _, id := range ids {
					after, _ := e.scopeEpoch(id)
					if after-before[id] != want[id] {
						t.Fatalf("%s clock %d advanced %d, want %d", sql, id, after-before[id], want[id])
					}
				}
			}
			switch table {
			case "tasks":
				check("INSERT INTO tasks(id,title,status,snapshot,created_at) VALUES(99,'new','open','{}',?)", []any{now()}, 99)
				check("UPDATE tasks SET title='changed' WHERE id=99", nil, 99)
				check("DELETE FROM tasks WHERE id=99", nil, 99)
				// Explicit reuse of a deleted ID continues the old epoch.
				check("INSERT INTO tasks(id,title,status,snapshot,created_at) VALUES(99,'reused','open','{}',?)", []any{now()}, 99)
			case "messages":
				check("INSERT INTO messages(id,task_id,role,content,source,created_at) VALUES(99,?,'document','test','{}',?)", []any{first.ID, now()}, first.ID)
				check("UPDATE messages SET task_id=? WHERE id=99", []any{second.ID}, first.ID, second.ID)
				check("UPDATE messages SET source='{}' WHERE id=99", nil, second.ID)
				check("DELETE FROM messages WHERE id=99", nil, second.ID)
			case "facts":
				check("INSERT INTO facts(id,task_id,key,revision,value,evidence_id,retracted,created_at) VALUES(99,?,'test',1,'1',?,0,?)", []any{first.ID, ev[0].ID, now()}, first.ID)
				check("UPDATE facts SET task_id=? WHERE id=99", []any{second.ID}, first.ID, second.ID)
				check("DELETE FROM facts WHERE id=99", nil, second.ID)
			case "dependencies":
				check("INSERT INTO dependencies VALUES(?,?)", []any{first.ID, third.ID}, first.ID)
				check("UPDATE dependencies SET child=? WHERE child=?", []any{second.ID, first.ID}, first.ID, second.ID)
				check("DELETE FROM dependencies WHERE child=?", []any{second.ID}, second.ID)
			case "aliases":
				check("INSERT INTO aliases VALUES(?,'move')", []any{first.ID}, first.ID)
				check("UPDATE aliases SET task_id=? WHERE alias='move'", []any{second.ID}, first.ID, second.ID)
				check("DELETE FROM aliases WHERE alias='move'", nil, second.ID)
			}
		})
	}
}
