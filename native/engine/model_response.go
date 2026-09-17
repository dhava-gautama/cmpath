package engine

// ModelCall holds the exact provider request and, when confirmed, response.
// A missing response is an unknown remote outcome, not permission to retry.
type ModelCall struct {
	CallID            string  `json:"call_id"`
	PayloadJSON       string  `json:"payload_json"`
	Units             int64   `json:"units"`
	Counting          string  `json:"counting"`
	ResponseJSON      *string `json:"response_json"`
	ResponseCreatedAt *string `json:"response_created_at"`
}

// ModelCalls returns model records in the order requests were first journaled.
func (e *Engine) ModelCalls(id string) (calls []ModelCall, err error) {
	err = e.locked(func() error {
		if _, er := e.turn(id); er != nil {
			return er
		}
		rows, er := e.db.Query(`SELECT m.call_id,m.payload_json,m.units,m.counting,
		 r.response_json,r.created_at AS response_created_at
		 FROM cmp_model_calls m LEFT JOIN cmp_model_responses r
		 ON m.request_id=r.request_id AND m.call_id=r.call_id
		 WHERE m.request_id=? ORDER BY m.rowid`, id)
		if er != nil {
			return er
		}
		calls = []ModelCall{}
		for _, row := range rows {
			call := ModelCall{CallID: row["call_id"].(string), PayloadJSON: row["payload_json"].(string),
				Units: row["units"].(int64), Counting: row["counting"].(string)}
			if row["response_json"] != nil {
				raw := row["response_json"].(string)
				created := row["response_created_at"].(string)
				call.ResponseJSON = &raw
				call.ResponseCreatedAt = &created
			}
			calls = append(calls, call)
		}
		return nil
	})
	return
}

// RecordModelResponse records an immutable confirmed response without inference.
func (e *Engine) RecordModelResponse(id string, generation int64, callID, response string) error {
	if !textOK(callID) {
		return problem("invalid", "model call ID is required")
	}
	if err := checkedJSON(response); err != nil {
		return err
	}
	var object map[string]any
	if err := decodeJSON([]byte(response), &object); err != nil || object == nil {
		return problem("invalid", "provider response must be a JSON object")
	}
	return e.transaction(func() error {
		if _, er := e.pending(id, generation); er != nil {
			return er
		}
		requests, er := e.db.Query("SELECT call_id FROM cmp_model_calls WHERE request_id=? AND call_id=?", id, callID)
		if er != nil {
			return er
		}
		if len(requests) == 0 {
			return problem("not_found", "model request must be journaled before its response")
		}
		rows, er := e.db.Query("SELECT response_json FROM cmp_model_responses WHERE request_id=? AND call_id=?", id, callID)
		if er != nil {
			return er
		}
		if len(rows) > 0 {
			if rows[0]["response_json"] != response {
				return problem("conflict", "model response is already recorded with different bytes")
			}
			return nil
		}
		return e.db.Exec("INSERT INTO cmp_model_responses(request_id,call_id,response_json,created_at) VALUES(?,?,?,?)", id, callID, response, now())
	})
}
