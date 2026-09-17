package engine

import (
	"context"
	"fmt"
	"time"
)

type Reply struct {
	Text       string          `json:"text"`
	Snapshot   map[string]any  `json:"snapshot,omitempty"`
	Facts      []FactUpdate    `json:"facts,omitempty"`
	Provenance []ProvenanceRef `json:"provenance,omitempty"`
}

// ProvenanceRef identifies immutable journal evidence used to derive a
// persisted snapshot. The reference is checked at commit time and recorded in
// the turn journal; copied text is deliberately not accepted as provenance.
type ProvenanceRef struct {
	EvidenceID int64  `json:"evidence_id"`
	Kind       string `json:"kind"`
}
type Turn struct {
	RequestID    string  `json:"request_id"`
	TaskID       int64   `json:"task_id"`
	Status       string  `json:"status"`
	Generation   int64   `json:"generation"`
	TaskRevision int64   `json:"task_revision"`
	Package      Package `json:"package"`
	Reply        *Reply  `json:"reply,omitempty"`
	Created      bool    `json:"created"`
	Replayed     bool    `json:"replayed"`
}

func (e *Engine) turn(id string) (Turn, error) {
	rows, err := e.db.Query("SELECT * FROM cmp_turns WHERE request_id=?", id)
	if err != nil {
		return Turn{}, err
	}
	if len(rows) == 0 {
		retired, er := e.db.Query("SELECT request_id FROM cmp_retired_turns WHERE request_id=?", id)
		if er != nil {
			return Turn{}, er
		}
		if len(retired) != 0 {
			return Turn{}, problem("retired", "turn journal was retired; this request ID cannot execute again")
		}
		return Turn{}, problem("not_found", "turn does not exist")
	}
	r := rows[0]
	out := Turn{RequestID: id, TaskID: r["task_id"].(int64), Status: r["status"].(string), Generation: r["generation"].(int64), TaskRevision: r["task_revision"].(int64)}
	if err = decodeJSON([]byte(r["package_json"].(string)), &out.Package); err != nil {
		return Turn{}, err
	}
	if r["reply_json"] != nil {
		var reply Reply
		if err = decodeJSON([]byte(r["reply_json"].(string)), &reply); err != nil {
			return Turn{}, err
		}
		out.Reply = &reply
	}
	return out, nil
}
func (e *Engine) Turn(id string) (out Turn, err error) {
	err = e.locked(func() error { out, err = e.turn(id); return err })
	return
}
func (e *Engine) pending(id string, generation int64) (Turn, error) {
	turn, err := e.turn(id)
	if err != nil {
		return turn, err
	}
	if turn.Status != "pending" {
		return turn, problem("state", "turn is not pending")
	}
	if turn.Generation != generation {
		return turn, problem("fenced", "worker generation is stale")
	}
	return turn, nil
}

// Begin records the user message and exact first-call context once. A duplicate
// pending request is observable but must not automatically execute another model call.
func (e *Engine) Begin(input Request, custom ...Counter) (out Turn, err error) {
	r, c, err := normalizeRequest(input, custom)
	if err != nil {
		return out, err
	}
	hash, err := fingerprint(r)
	if err != nil {
		return out, err
	}
	raw, err := encode(r)
	if err != nil {
		return out, err
	}
	err = e.transaction(func() error {
		retired, er := e.db.Query("SELECT fingerprint FROM cmp_retired_turns WHERE request_id=?", r.RequestID)
		if er != nil {
			return er
		}
		if len(retired) != 0 {
			if retired[0]["fingerprint"] != hash {
				return problem("conflict", "retired request ID was used with different input")
			}
			return problem("retired", "turn journal was retired; use a new ID only for genuinely new work")
		}
		old, er := e.db.Query("SELECT fingerprint FROM cmp_turns WHERE request_id=?", r.RequestID)
		if er != nil {
			return er
		}
		if len(old) > 0 {
			if old[0]["fingerprint"] != hash {
				return problem("conflict", "request ID was used with different input")
			}
			out, er = e.turn(r.RequestID)
			out.Replayed = true
			return er
		}
		busy, er := e.db.Query("SELECT request_id FROM cmp_turns WHERE task_id=? AND status='pending'", r.TaskID)
		if er != nil {
			return er
		}
		if len(busy) > 0 {
			return problem("busy", "task already has an unfinished turn")
		}
		task, er := e.task(r.TaskID)
		if er != nil {
			return er
		}
		if task.Status == "archived" {
			if er = e.db.Exec("UPDATE tasks SET status='open',revision=revision+1 WHERE id=?", r.TaskID); er != nil {
				return er
			}
			task.Revision++
		}
		payload, er := e.pack(r, c)
		if er != nil {
			return er
		}
		encoded, er := encode(payload)
		if er != nil {
			return er
		}
		if _, er = e.append(Append{TaskID: r.TaskID, Role: "user", Content: r.Query, Source: map[string]any{"request_id": r.RequestID}}); er != nil {
			return er
		}
		if er = e.db.Exec("UPDATE runtime_state SET active_task=?,version=version+1 WHERE singleton=1", r.TaskID); er != nil {
			return er
		}
		if er = e.db.Exec("INSERT INTO events(kind,task_id,created_at) VALUES('harness_begin',?,?)", r.TaskID, now()); er != nil {
			return er
		}
		if er = e.db.Exec("INSERT INTO cmp_turns(request_id,task_id,fingerprint,request_json,status,generation,task_revision,package_json,created_at,updated_at) VALUES(?,?,?,?,'pending',1,?,?,?,?)", r.RequestID, r.TaskID, hash, raw, task.Revision, encoded, now(), now()); er != nil {
			return er
		}
		if er = e.captureBasis(r, task); er != nil {
			return er
		}
		out, er = e.turn(r.RequestID)
		out.Created = true
		return er
	})
	return
}

// Recover explicitly fences prior workers. It does not redo model or tool calls.
// Started tools require reconciliation by the host before final commit.
func (e *Engine) Recover(id string, expectedGeneration int64) (out Turn, err error) {
	err = e.transaction(func() error {
		old, er := e.pending(id, expectedGeneration)
		if er != nil {
			return er
		}
		task, er := e.task(old.TaskID)
		if er != nil {
			return er
		}
		if task.Revision != old.TaskRevision {
			return problem("conflict", "task snapshot changed; abort and prepare a new turn")
		}
		if er = e.db.Exec("UPDATE cmp_turns SET generation=generation+1,updated_at=? WHERE request_id=?", now(), id); er != nil {
			return er
		}
		out, er = e.turn(id)
		return er
	})
	return
}
func (e *Engine) Commit(id string, generation int64, reply Reply) (out Turn, err error) {
	if !textOK(reply.Text) {
		return out, problem("invalid", "final reply must be nonempty UTF-8")
	}
	hash, err := fingerprint(reply)
	if err != nil {
		return out, err
	}
	raw, err := encode(reply)
	if err != nil {
		return out, err
	}
	err = e.transaction(func() error {
		current, er := e.turn(id)
		if er != nil {
			return er
		}
		if current.Status == "committed" {
			rows, er := e.db.Query("SELECT commit_hash FROM cmp_turns WHERE request_id=?", id)
			if er != nil {
				return er
			}
			if current.Generation != generation {
				return problem("fenced", "worker generation is stale")
			}
			if rows[0]["commit_hash"] != hash {
				return problem("conflict", "turn was committed with a different reply")
			}
			out = current
			out.Replayed = true
			return nil
		}
		current, er = e.pending(id, generation)
		if er != nil {
			return er
		}
		if er = e.validateBasis(id); er != nil {
			return er
		}
		request, er := e.turnRequest(id)
		if er != nil {
			return er
		}
		task, er := e.task(current.TaskID)
		if er != nil {
			return er
		}
		// A scoped turn that publishes a replacement snapshot must identify the
		// evidence it derived that state from. Re-emitting the unchanged snapshot
		// is not a derived write. Legacy snapshot turns remain replay-compatible;
		// new scoped writes fail closed until annotated.
		snapshotChanged := false
		if reply.Snapshot != nil {
			before, encErr := encode(current.Package.Snapshot)
			if encErr != nil {
				return encErr
			}
			after, encErr := encode(reply.Snapshot)
			if encErr != nil {
				return encErr
			}
			snapshotChanged = before != after
		}
		if request.Consistency == "scope" && snapshotChanged && len(reply.Provenance) == 0 {
			return problem("provenance_required", "scoped snapshot writes must declare evidence provenance")
		}
		if er = e.validateProvenance(request, task, reply.Provenance); er != nil {
			return er
		}
		if task.Revision != current.TaskRevision {
			return problem("conflict", "task snapshot changed during execution")
		}
		tools, er := e.db.Query("SELECT call_id FROM cmp_tool_calls WHERE request_id=? AND status='started'", id)
		if er != nil {
			return er
		}
		if len(tools) > 0 {
			return problem("indeterminate_tool", "tool intent has no confirmed result")
		}
		if _, er = e.append(Append{TaskID: task.ID, Role: "assistant", Content: reply.Text, Source: map[string]any{"request_id": id}}); er != nil {
			return er
		}
		for _, fact := range reply.Facts {
			if er = e.setFact(task.ID, fact); er != nil {
				return er
			}
		}
		snapshot := task.Snapshot
		if reply.Snapshot != nil {
			snapshot = reply.Snapshot
		}
		encoded, er := encode(snapshot)
		if er != nil {
			return er
		}
		if er = e.db.Exec("UPDATE tasks SET snapshot=?,revision=revision+1 WHERE id=?", encoded, task.ID); er != nil {
			return er
		}
		for _, ref := range reply.Provenance {
			if er = e.db.Exec("INSERT INTO cmp_turn_provenance(request_id,evidence_id,kind,created_at) VALUES(?,?,?,?)", id, ref.EvidenceID, ref.Kind, now()); er != nil {
				return er
			}
		}
		if er = e.db.Exec("UPDATE cmp_turns SET status='committed',reply_json=?,commit_hash=?,updated_at=? WHERE request_id=?", raw, hash, now(), id); er != nil {
			return er
		}
		if er = e.db.Exec("INSERT INTO events(kind,task_id,created_at) VALUES('harness_commit',?,?)", task.ID, now()); er != nil {
			return er
		}
		out, er = e.turn(id)
		return er
	})
	return
}
func (e *Engine) Abort(id string, generation int64) error {
	return e.transaction(func() error {
		if _, er := e.pending(id, generation); er != nil {
			return er
		}
		// Aborting work does not erase either the user message or external-tool audit.
		return e.db.Exec("UPDATE cmp_turns SET status='aborted',updated_at=? WHERE request_id=?", now(), id)
	})
}

type ToolCall struct {
	CallID     string `json:"call_id"`
	Name       string `json:"name"`
	Arguments  any    `json:"arguments"`
	Status     string `json:"status"`
	Result     any    `json:"result,omitempty"`
	EvidenceID int64  `json:"evidence_id,omitempty"`
	Created    bool   `json:"created"`
}

type ActionLease struct {
	Token      string `json:"token"`
	RequestID  string `json:"request_id"`
	CallID     string `json:"call_id"`
	Generation int64  `json:"generation"`
	ArgsHash   string `json:"args_hash"`
	ExpiresAt  int64  `json:"expires_at"`
}

func (e *Engine) tool(id, callID string) (ToolCall, error) {
	rows, err := e.db.Query("SELECT * FROM cmp_tool_calls WHERE request_id=? AND call_id=?", id, callID)
	if err != nil {
		return ToolCall{}, err
	}
	if len(rows) == 0 {
		return ToolCall{}, problem("not_found", "tool call does not exist")
	}
	r := rows[0]
	out := ToolCall{CallID: callID, Name: r["name"].(string), Status: r["status"].(string)}
	if err = decodeJSON([]byte(r["args_json"].(string)), &out.Arguments); err != nil {
		return out, err
	}
	if r["result_json"] != nil {
		if err = decodeJSON([]byte(r["result_json"].(string)), &out.Result); err != nil {
			return out, err
		}
	}
	if r["evidence_id"] != nil {
		out.EvidenceID = r["evidence_id"].(int64)
	}
	return out, nil
}
func (e *Engine) Tools(id string) (out []ToolCall, err error) {
	out = []ToolCall{}
	err = e.locked(func() error {
		rows, er := e.db.Query("SELECT call_id FROM cmp_tool_calls WHERE request_id=? ORDER BY rowid", id)
		if er != nil {
			return er
		}
		for _, row := range rows {
			call, er := e.tool(id, row["call_id"].(string))
			if er != nil {
				return er
			}
			out = append(out, call)
		}
		return nil
	})
	return
}

// ActionEligible revalidates a started tool intent immediately before the host
// invokes its external callback. The returned lease is an auditable local
// certificate, not a promise that an uncooperating remote service is atomic.
func (e *Engine) ActionEligible(id string, generation int64, callID string, ttlMS int64) (out ActionLease, err error) {
	if !textOK(callID) || len(callID) > 128 {
		return out, problem("invalid", "tool call ID is required")
	}
	if ttlMS == 0 {
		ttlMS = 5000
	}
	if ttlMS < 1 || ttlMS > 600000 {
		return out, problem("invalid", "lease TTL must be between 1 and 600000 milliseconds")
	}
	err = e.transaction(func() error {
		_, er := e.pending(id, generation)
		if er != nil {
			return er
		}
		call, er := e.tool(id, callID)
		if er != nil {
			return er
		}
		if call.Status != "started" {
			return problem("state", "only a started tool intent can receive an action lease")
		}
		if er = e.validateBasis(id); er != nil {
			return er
		}
		hash, er := fingerprint([]any{call.Name, call.Arguments})
		if er != nil {
			return er
		}
		expires := time.Now().UTC().UnixMilli() + ttlMS
		token, er := fingerprint([]any{id, generation, callID, hash, expires})
		if er != nil {
			return er
		}
		if er = e.db.Exec("INSERT INTO cmp_action_leases(token,request_id,generation,call_id,args_hash,expires_at,created_at) VALUES(?,?,?,?,?,?,?)", token, id, generation, callID, hash, expires, now()); er != nil {
			return er
		}
		out = ActionLease{Token: token, RequestID: id, CallID: callID, Generation: generation, ArgsHash: hash, ExpiresAt: expires}
		return nil
	})
	return
}
func (e *Engine) StartTool(id string, generation int64, callID, name string, args any) (out ToolCall, err error) {
	if !textOK(callID) || !textOK(name) || len(callID) > 128 {
		return out, problem("invalid", "tool call ID and name are required")
	}
	raw, err := encode(args)
	if err != nil {
		return out, err
	}
	hash, err := fingerprint([]any{name, args})
	if err != nil {
		return out, err
	}
	err = e.transaction(func() error {
		if _, er := e.pending(id, generation); er != nil {
			return er
		}
		rows, er := e.db.Query("SELECT fingerprint FROM cmp_tool_calls WHERE request_id=? AND call_id=?", id, callID)
		if er != nil {
			return er
		}
		if len(rows) > 0 {
			if rows[0]["fingerprint"] != hash {
				return problem("conflict", "tool call ID has different input")
			}
			out, er = e.tool(id, callID)
			return er
		}
		if er = e.validateBasis(id); er != nil {
			return er
		}
		if er = e.db.Exec("INSERT INTO cmp_tool_calls(request_id,call_id,name,args_json,fingerprint,status) VALUES(?,?,?,?,?,'started')", id, callID, name, raw, hash); er != nil {
			return er
		}
		out, er = e.tool(id, callID)
		out.Created = true
		return er
	})
	return
}
func (e *Engine) FinishTool(id string, generation int64, callID string, result any, leaseToken ...string) (out ToolCall, err error) {
	raw, err := encode(result)
	if err != nil {
		return out, err
	}
	err = e.transaction(func() error {
		turn, er := e.pending(id, generation)
		if er != nil {
			return er
		}
		call, er := e.tool(id, callID)
		if er != nil {
			return er
		}
		if call.Status == "completed" {
			rows, er := e.db.Query("SELECT result_json FROM cmp_tool_calls WHERE request_id=? AND call_id=?", id, callID)
			if er != nil {
				return er
			}
			if rows[0]["result_json"] != raw {
				return problem("conflict", "tool result was already recorded differently")
			}
			out = call
			return nil
		}
		if len(leaseToken) > 1 {
			return problem("invalid", "at most one action lease token is allowed")
		}
		if len(leaseToken) == 1 && leaseToken[0] != "" {
			if er = e.validateActionLease(id, generation, callID, call, leaseToken[0]); er != nil {
				return er
			}
		}
		taskBefore, er := e.scopeEpoch(turn.TaskID)
		if er != nil {
			return er
		}
		globalBefore, er := e.scopeEpoch(0)
		if er != nil {
			return er
		}
		ev, er := e.append(Append{TaskID: turn.TaskID, Role: "tool", Content: raw, Source: map[string]any{"request_id": id, "call_id": callID, "tool": call.Name}})
		if er != nil {
			return er
		}
		if er = e.advanceBasis(id, turn.TaskID, taskBefore); er != nil {
			return er
		}
		if er = e.advanceBasis(id, 0, globalBefore); er != nil {
			return er
		}
		if er = e.db.Exec("UPDATE cmp_tool_calls SET status='completed',result_json=?,evidence_id=? WHERE request_id=? AND call_id=?", raw, ev.ID, id, callID); er != nil {
			return er
		}
		out, er = e.tool(id, callID)
		return er
	})
	return
}

func (e *Engine) validateActionLease(id string, generation int64, callID string, call ToolCall, token string) error {
	rows, err := e.db.Query("SELECT generation,call_id,args_hash,expires_at FROM cmp_action_leases WHERE token=? AND request_id=?", token, id)
	if err != nil {
		return err
	}
	if len(rows) != 1 {
		return problem("stale_context", "action eligibility lease is missing")
	}
	if rows[0]["generation"] != generation || rows[0]["call_id"] != callID {
		return problem("fenced", "action eligibility lease belongs to another worker or tool")
	}
	if rows[0]["expires_at"].(int64) <= time.Now().UTC().UnixMilli() {
		return problem("stale_context", "action eligibility lease has expired")
	}
	hash, err := fingerprint([]any{call.Name, call.Arguments})
	if err != nil {
		return err
	}
	if rows[0]["args_hash"] != hash {
		return problem("conflict", "action eligibility lease arguments do not match the recorded intent")
	}
	return nil
}

// RecordModelRequest stores the exact serialized provider payload and the host's
// declared complete-payload count before dispatch. It never makes the request.
func (e *Engine) RecordModelRequest(id string, generation int64, callID, payload string, units int, counting string) error {
	if !textOK(callID) || !textOK(counting) || units < 0 {
		return problem("invalid", "model call ID, counting method and nonnegative units are required")
	}
	if err := checkedJSON(payload); err != nil {
		return err
	}
	return e.transaction(func() error {
		turn, er := e.pending(id, generation)
		if er != nil {
			return er
		}
		if units > turn.Package.InputAllowance {
			return problem("budget", "complete model payload exceeds the input allowance")
		}
		rows, er := e.db.Query("SELECT payload_json,units,counting FROM cmp_model_calls WHERE request_id=? AND call_id=?", id, callID)
		if er != nil {
			return er
		}
		if len(rows) > 0 {
			if rows[0]["payload_json"] != payload || rows[0]["units"] != int64(units) || rows[0]["counting"] != counting {
				return problem("conflict", "model call ID has a different payload or count")
			}
			return nil
		}
		if er = e.validateBasis(id); er != nil {
			return er
		}
		return e.db.Exec("INSERT INTO cmp_model_calls VALUES(?,?,?,?,?)", id, callID, payload, units, counting)
	})
}

// Run integrates a model/planner callback into the durable turn lifecycle.
// An already committed request returns its saved reply without invoking complete.
// A pending replay returns an error so a restart cannot silently repeat effects.
func (e *Engine) Run(ctx context.Context, r Request, complete func(context.Context, *Session) (Reply, error)) (Turn, error) {
	if err := ctx.Err(); err != nil {
		return Turn{}, err
	}
	if complete == nil {
		return Turn{}, problem("invalid", "completion callback is required")
	}
	turn, err := e.Begin(r)
	if err != nil {
		return turn, err
	}
	if turn.Status == "committed" {
		return turn, nil
	}
	if !turn.Created {
		return turn, problem("in_progress", "inspect and explicitly recover the pending turn")
	}
	reply, err := complete(ctx, &Session{Engine: e, Turn: turn})
	if err != nil {
		return turn, err
	}
	if err = ctx.Err(); err != nil {
		return turn, err
	}
	return e.Commit(turn.RequestID, turn.Generation, reply)
}

type Session struct {
	Engine *Engine
	Turn   Turn
}

// Tool executes only a newly journaled intent. A completed call returns the
// original result, while a previously started call requires reconciliation.
func (s *Session) Tool(callID, name string, args any, execute func() (any, error)) (any, error) {
	if execute == nil {
		return nil, problem("invalid", "tool callback is required")
	}
	call, err := s.Engine.StartTool(s.Turn.RequestID, s.Turn.Generation, callID, name, args)
	if err != nil {
		return nil, err
	}
	if call.Status == "completed" {
		return call.Result, nil
	}
	if !call.Created {
		return nil, problem("indeterminate_tool", fmt.Sprintf("reconcile tool %s before retry", callID))
	}
	lease, err := s.Engine.ActionEligible(s.Turn.RequestID, s.Turn.Generation, callID, 5000)
	if err != nil {
		return nil, err
	}
	result, err := execute()
	if err != nil {
		return nil, err
	}
	call, err = s.Engine.FinishTool(s.Turn.RequestID, s.Turn.Generation, callID, result, lease.Token)
	return call.Result, err
}
