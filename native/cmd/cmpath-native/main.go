// cmpath-native exposes the embedded engine over a private, persistent stdio channel.
package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"

	"cmpath.local/native/engine"
)

type request struct {
	Version   int             `json:"v"`
	ID        string          `json:"id"`
	Operation string          `json:"op"`
	Args      json.RawMessage `json:"args"`
}
type response struct {
	Version int           `json:"v"`
	ID      string        `json:"id"`
	Result  any           `json:"result,omitempty"`
	Error   *engine.Error `json:"error,omitempty"`
}

func decode[T any](raw []byte) (value T, err error) {
	if len(raw) == 0 {
		raw = []byte("{}")
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.DisallowUnknownFields()
	d.UseNumber()
	if err = d.Decode(&value); err != nil {
		return value, &engine.Error{Code: "invalid", Message: err.Error()}
	}
	var extra any
	if err = d.Decode(&extra); err != io.EOF {
		return value, &engine.Error{Code: "invalid", Message: "exactly one JSON value is required"}
	}
	return value, nil
}
func dispatch(e *engine.Engine, r request) (any, error) {
	if r.Version != 1 || r.ID == "" || len(r.ID) > 128 {
		return nil, &engine.Error{Code: "protocol", Message: "v=1 and a nonempty ID are required"}
	}
	switch r.Operation {
	case "info":
		if _, err := decode[struct{}](r.Args); err != nil {
			return nil, err
		}
		return e.Info()
	case "create_task":
		a, err := decode[engine.CreateTask](r.Args)
		if err != nil {
			return nil, err
		}
		return e.CreateTask(a)
	case "task":
		a, err := decode[struct {
			ID int64 `json:"task_id"`
		}](r.Args)
		if err != nil {
			return nil, err
		}
		return e.Task(a.ID)
	case "append_batch":
		a, err := decode[struct {
			Messages []engine.Append `json:"messages"`
		}](r.Args)
		if err != nil {
			return nil, err
		}
		return e.AppendBatch(a.Messages)
	case "search":
		a, err := decode[struct {
			Query   string  `json:"query"`
			TaskIDs []int64 `json:"task_ids"`
			Limit   int     `json:"limit"`
		}](r.Args)
		if err != nil {
			return nil, err
		}
		return e.Search(a.Query, a.TaskIDs, a.Limit)
	case "resolve":
		a, err := decode[struct {
			Query string `json:"query"`
		}](r.Args)
		if err != nil {
			return nil, err
		}
		return e.Resolve(a.Query)
	case "begin", "preview":
		a, err := decode[engine.Request](r.Args)
		if err != nil {
			return nil, err
		}
		if r.Operation == "begin" {
			return e.Begin(a)
		}
		return e.Preview(a)
	case "turn", "tools", "model_calls":
		a, err := decode[struct {
			ID string `json:"request_id"`
		}](r.Args)
		if err != nil {
			return nil, err
		}
		if r.Operation == "turn" {
			return e.Turn(a.ID)
		}
		if r.Operation == "model_calls" {
			return e.ModelCalls(a.ID)
		}
		return e.Tools(a.ID)
	case "recover", "abort":
		a, err := decode[struct {
			ID         string `json:"request_id"`
			Generation int64  `json:"generation"`
		}](r.Args)
		if err != nil {
			return nil, err
		}
		if r.Operation == "recover" {
			return e.Recover(a.ID, a.Generation)
		}
		return map[string]bool{"aborted": true}, e.Abort(a.ID, a.Generation)
	case "commit":
		a, err := decode[struct {
			ID         string       `json:"request_id"`
			Generation int64        `json:"generation"`
			Reply      engine.Reply `json:"reply"`
		}](r.Args)
		if err != nil {
			return nil, err
		}
		return e.Commit(a.ID, a.Generation, a.Reply)
	case "tool_start":
		a, err := decode[struct {
			ID         string `json:"request_id"`
			Generation int64  `json:"generation"`
			CallID     string `json:"call_id"`
			Name       string `json:"name"`
			Arguments  any    `json:"arguments"`
		}](r.Args)
		if err != nil {
			return nil, err
		}
		return e.StartTool(a.ID, a.Generation, a.CallID, a.Name, a.Arguments)
	case "tool_finish":
		a, err := decode[struct {
			ID         string `json:"request_id"`
			Generation int64  `json:"generation"`
			CallID     string `json:"call_id"`
			Result     any    `json:"result"`
			LeaseToken string `json:"lease_token,omitempty"`
		}](r.Args)
		if err != nil {
			return nil, err
		}
		return e.FinishTool(a.ID, a.Generation, a.CallID, a.Result, a.LeaseToken)
	case "action_eligible":
		a, err := decode[struct {
			ID         string `json:"request_id"`
			Generation int64  `json:"generation"`
			CallID     string `json:"call_id"`
			TTLMS      int64  `json:"ttl_ms"`
		}](r.Args)
		if err != nil {
			return nil, err
		}
		return e.ActionEligible(a.ID, a.Generation, a.CallID, a.TTLMS)
	case "model_request":
		a, err := decode[struct {
			ID         string `json:"request_id"`
			Generation int64  `json:"generation"`
			CallID     string `json:"call_id"`
			Payload    string `json:"payload_json"`
			Units      int    `json:"units"`
			Counting   string `json:"counting"`
		}](r.Args)
		if err != nil {
			return nil, err
		}
		return map[string]bool{"recorded": true}, e.RecordModelRequest(a.ID, a.Generation, a.CallID, a.Payload, a.Units, a.Counting)
	case "model_response":
		a, err := decode[struct {
			ID         string `json:"request_id"`
			Generation int64  `json:"generation"`
			CallID     string `json:"call_id"`
			Response   string `json:"response_json"`
		}](r.Args)
		if err != nil {
			return nil, err
		}
		return map[string]bool{"recorded": true}, e.RecordModelResponse(a.ID, a.Generation, a.CallID, a.Response)
	case "export_file":
		a, err := decode[struct {
			Path string `json:"path"`
		}](r.Args)
		if err != nil {
			return nil, err
		}
		return e.ExportFile(a.Path)
	case "retention_plan", "retention_apply":
		a, err := decode[struct {
			Cutoff   string `json:"cutoff"`
			PlanHash string `json:"plan_hash,omitempty"`
		}](r.Args)
		if err != nil {
			return nil, err
		}
		if r.Operation == "retention_plan" {
			return e.RetentionPlan(a.Cutoff)
		}
		return e.ApplyRetention(a.Cutoff, a.PlanHash)
	case "set_fact":
		a, err := decode[struct {
			TaskID int64             `json:"task_id"`
			Fact   engine.FactUpdate `json:"fact"`
		}](r.Args)
		if err != nil {
			return nil, err
		}
		return map[string]bool{"recorded": true}, e.SetFact(a.TaskID, a.Fact)
	default:
		return nil, &engine.Error{Code: "operation", Message: "unknown operation"}
	}
}

// dispatchRecovered converts a panic from a single operation into a coded
// response. A panic raised inside Engine.transaction has already rolled that
// transaction back before it propagated, so the connection is consistent; the
// panic text travels in the error message so the failure is visible rather
// than silently converted into a retryable one. Without this boundary one
// malformed row would terminate the process and the host's persistent channel
// with it.
func dispatchRecovered(e *engine.Engine, r request) (result any, err error) {
	defer func() {
		if p := recover(); p != nil {
			result = nil
			err = &engine.Error{Code: "internal_error", Message: fmt.Sprintf("operation %s panicked: %v", r.Operation, p)}
		}
	}()
	return dispatch(e, r)
}

func serve(e *engine.Engine, input io.Reader, output io.Writer) error {
	scanner := bufio.NewScanner(input)
	scanner.Buffer(make([]byte, 64*1024), 16*1024*1024)
	writer := bufio.NewWriter(output)
	encoder := json.NewEncoder(writer)
	encoder.SetEscapeHTML(false)
	for scanner.Scan() {
		r, err := decode[request](scanner.Bytes())
		out := response{Version: 1, ID: r.ID}
		if err == nil {
			out.Result, err = dispatchRecovered(e, r)
		}
		if err != nil {
			out.Result = nil
			out.Error = &engine.Error{Code: engine.Code(err), Message: err.Error()}
		}
		if err = encoder.Encode(out); err != nil {
			return err
		}
		if err = writer.Flush(); err != nil {
			return err
		}
	}
	return scanner.Err()
}
func main() {
	path := flag.String("db", "", "SQLite database path, or :memory:")
	create := flag.Bool("create", false, "Allow creation of a new database")
	busyTimeout := flag.Int("busy-timeout-ms", 0, "Milliseconds to wait for a competing writer (raised to the 10000 ms floor)")
	version := flag.Bool("version", false, "Print the engine version")
	flag.Parse()
	if *version {
		fmt.Println(engine.Version)
		return
	}
	if *path == "" {
		fmt.Fprintln(os.Stderr, "--db is required")
		os.Exit(2)
	}
	if *path != ":memory:" && !*create {
		if info, err := os.Stat(*path); err != nil || !info.Mode().IsRegular() {
			fmt.Fprintln(os.Stderr, "database does not exist; use --create for a new database")
			os.Exit(2)
		}
	}
	e, err := engine.Open(*path, engine.WithBusyTimeoutMS(*busyTimeout))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	defer e.Close()
	if err = serve(e, os.Stdin, os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
}
