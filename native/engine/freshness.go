package engine

import "fmt"

// captureBasis runs after Begin's own writes, in the same transaction as pack.
// The scope includes candidate tasks even when retrieval returned no evidence.
func (e *Engine) captureBasis(r Request, task Task) error {
	if r.Consistency != "scope" {
		return nil
	}
	ids, err := e.scope(task, r.Scope)
	if err != nil {
		return err
	}
	if r.Scope == "all" {
		ids = []int64{0}
	}
	for _, id := range ids {
		epoch, er := e.scopeEpoch(id)
		if er != nil {
			return er
		}
		if err = e.db.Exec("INSERT INTO cmp_turn_basis(request_id,task_id,epoch) VALUES(?,?,?)", r.RequestID, id, epoch); err != nil {
			return err
		}
	}
	return nil
}

// validateBasis is called only for new intents and pending commits. Historical
// replay and recording known remote outcomes must remain possible when stale.
func (e *Engine) validateBasis(id string) error {
	r, err := e.turnRequest(id)
	if err != nil {
		return err
	}
	if r.Consistency != "scope" {
		return nil
	}
	rows, err := e.db.Query(`SELECT b.epoch AS expected,c.epoch AS actual
	 FROM cmp_turn_basis b LEFT JOIN cmp_scope_epochs c ON c.task_id=b.task_id
	 WHERE b.request_id=?`, id)
	if err != nil {
		return err
	}
	if len(rows) == 0 {
		return problem("stale_context", "turn scope basis is missing; abort and prepare a new turn")
	}
	for _, row := range rows {
		if row["expected"] != row["actual"] {
			return problem("stale_context", "memory in the turn scope changed; abort and prepare a new turn")
		}
	}
	return nil
}

func (e *Engine) turnRequest(id string) (Request, error) {
	rows, err := e.db.Query("SELECT request_json FROM cmp_turns WHERE request_id=?", id)
	if err != nil {
		return Request{}, err
	}
	if len(rows) != 1 {
		return Request{}, problem("not_found", "turn does not exist")
	}
	var r Request
	if err = decodeJSON([]byte(rows[0]["request_json"].(string)), &r); err != nil {
		return Request{}, err
	}
	return r, nil
}

// validateProvenance checks source identity and admissibility at the same
// transaction boundary as the derived write. It is intentionally conservative:
// a source implicated in a retracted fact is treated as invalid even when the
// caller cannot tell CMP which claim was derived from it.
func (e *Engine) validateProvenance(r Request, task Task, refs []ProvenanceRef) error {
	if len(refs) == 0 {
		return nil
	}
	ids, err := e.scope(task, r.Scope)
	if err != nil {
		return err
	}
	allowed := map[int64]bool{}
	if r.Scope == "all" {
		allowed = nil
	} else {
		for _, id := range ids {
			allowed[id] = true
		}
	}
	seen := map[string]bool{}
	for _, ref := range refs {
		if ref.EvidenceID < 1 || (ref.Kind != "supports" && ref.Kind != "contradicts") {
			return problem("invalid", "provenance requires a positive evidence ID and kind supports or contradicts")
		}
		key := fmt.Sprintf("%d/%s", ref.EvidenceID, ref.Kind)
		if seen[key] {
			return problem("invalid", "duplicate provenance edge")
		}
		seen[key] = true
		rows, er := e.db.Query("SELECT task_id FROM messages WHERE id=?", ref.EvidenceID)
		if er != nil {
			return er
		}
		if len(rows) != 1 {
			return problem("stale_context", "provenance evidence no longer exists")
		}
		if allowed != nil && !allowed[rows[0]["task_id"].(int64)] {
			return problem("invalid", "provenance evidence is outside the turn scope")
		}
		if ref.Kind == "contradicts" {
			return problem("conflict", "contradictory provenance must be resolved before publishing derived state")
		}
		facts, er := e.db.Query("SELECT retracted FROM facts WHERE evidence_id=?", ref.EvidenceID)
		if er != nil {
			return er
		}
		for _, fact := range facts {
			if fact["retracted"] == int64(1) {
				return problem("stale_context", "provenance source has been retracted")
			}
		}
	}
	return nil
}

func (e *Engine) scopeEpoch(id int64) (int64, error) {
	rows, err := e.db.Query("SELECT epoch FROM cmp_scope_epochs WHERE task_id=?", id)
	if err != nil {
		return 0, err
	}
	if len(rows) != 1 {
		return 0, problem("integrity", "scope clock is missing")
	}
	return rows[0]["epoch"].(int64), nil
}

// advanceBasis exempts only this transaction's own evidence write. Adding the
// measured delta, rather than refreshing to the current clock, preserves any
// intervening change that happened while the external tool was executing.
func (e *Engine) advanceBasis(requestID string, taskID, before int64) error {
	after, err := e.scopeEpoch(taskID)
	if err != nil {
		return err
	}
	return e.db.Exec("UPDATE cmp_turn_basis SET epoch=epoch+? WHERE request_id=? AND task_id=?", after-before, requestID, taskID)
}
