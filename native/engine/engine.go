// Package engine embeds durable memory in an agent harness. It owns no model,
// network transport or external tool execution. Slow callbacks run outside SQL transactions.
package engine

import (
	"bytes"
	"crypto/sha256"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"
	"unicode/utf8"

	"cmpath.local/native/internal/sqlite"
	"golang.org/x/text/cases"
	"golang.org/x/text/unicode/norm"
)

//go:embed base.sql
var baseSchema string

//go:embed journal.sql
var journalSchema string

const Version = "0.4.0a5"

type Error struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func (e *Error) Error() string           { return e.Code + ": " + e.Message }
func problem(code, message string) error { return &Error{code, message} }

// Code classifies a storage or operation failure for the wire protocol.
func Code(err error) string {
	var e *Error
	if errors.As(err, &e) {
		return e.Code
	}
	// A locked database is contention, not damage: the caller can retry after
	// the competing writer commits. It must not be reported as storage_error,
	// which a caller reads as corruption or an unusable file.
	var busy *sqlite.Error
	if errors.As(err, &busy) && busy.Busy() {
		return "busy"
	}
	// A column that cannot be represented faithfully means the stored row does
	// not match the schema the engine depends on.
	var mismatch *sqlite.TypeError
	if errors.As(err, &mismatch) {
		return "integrity"
	}
	return "storage_error"
}

// Option adjusts an Engine at Open time.
type Option func(*config)

type config struct{ busyTimeoutMS int }

// WithBusyTimeoutMS raises the SQLite lock wait above the driver floor. A
// request below the floor is ignored; the floor covers the measured write
// horizon of one export, batch append or retention transaction.
func WithBusyTimeoutMS(ms int) Option { return func(c *config) { c.busyTimeoutMS = ms } }

type Engine struct {
	mu     sync.Mutex
	db     *sqlite.DB
	closed bool
}

func Open(path string, opts ...Option) (*Engine, error) {
	if strings.ContainsRune(path, 0) {
		return nil, problem("invalid", "invalid database path")
	}
	cfg := config{}
	for _, opt := range opts {
		opt(&cfg)
	}
	db, err := sqlite.Open(path, sqlite.WithBusyTimeoutMS(cfg.busyTimeoutMS))
	if err != nil {
		return nil, err
	}
	fail := func(err error) (*Engine, error) { db.Close(); return nil, err }
	if err = db.Script("PRAGMA foreign_keys=ON;"); err != nil {
		return fail(err)
	}
	v, err := db.Query("PRAGMA user_version")
	if err != nil {
		return fail(err)
	}
	tables, err := db.Query("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
	if err != nil {
		return fail(err)
	}
	version := v[0]["user_version"].(int64)
	if (version == 0 && len(tables) > 0) || (version != 0 && version != 1) {
		return fail(problem("schema", "unsupported database schema"))
	}
	if version == 0 {
		if err = db.Script(baseSchema); err != nil {
			return fail(err)
		}
	}
	// Validate the base schema before adding any harness tables.
	if _, err = db.Query("SELECT t.id,m.id,f.id,d.child,a.alias,s.version,e.id FROM tasks t,messages m,facts f,dependencies d,aliases a,runtime_state s,events e LIMIT 0"); err != nil {
		return fail(err)
	}
	if _, err = db.Query("SELECT rowid FROM evidence_index LIMIT 0"); err != nil {
		return fail(err)
	}
	meta, err := db.Query("SELECT name FROM sqlite_master WHERE name='cmp_harness_meta'")
	if err != nil {
		return fail(err)
	}
	if len(meta) > 0 {
		r, er := db.Query("SELECT version FROM cmp_harness_meta WHERE singleton=1")
		if er != nil {
			return fail(er)
		}
		if len(r) != 1 || (r[0]["version"] != int64(1) && r[0]["version"] != int64(2) && r[0]["version"] != int64(3) && r[0]["version"] != int64(4)) {
			return fail(problem("schema", "unsupported harness schema"))
		}
	}
	if err = db.Script("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;"); err != nil {
		return fail(err)
	}
	if err = db.Script(journalSchema); err != nil {
		return fail(err)
	}
	return &Engine{db: db}, nil
}
func (e *Engine) Close() {
	e.mu.Lock()
	defer e.mu.Unlock()
	if !e.closed {
		e.db.Close()
		e.closed = true
	}
}
func (e *Engine) locked(fn func() error) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.closed {
		return problem("closed", "engine is closed")
	}
	return fn()
}
func (e *Engine) transaction(fn func() error) (err error) {
	return e.locked(func() (err error) {
		if err = e.db.Exec("BEGIN IMMEDIATE"); err != nil {
			return err
		}
		defer func() {
			if p := recover(); p != nil {
				_ = e.db.Exec("ROLLBACK")
				panic(p)
			}
			if err != nil {
				_ = e.db.Exec("ROLLBACK")
			}
		}()
		if err = fn(); err != nil {
			return err
		}
		return e.db.Exec("COMMIT")
	})
}
func now() string                      { return time.Now().UTC().Format(time.RFC3339Nano) }
func encode(value any) (string, error) { b, err := json.Marshal(value); return string(b), err }
func fingerprint(value any) (string, error) {
	b, err := json.Marshal(value)
	if err != nil {
		return "", err
	}
	return fmt.Sprintf("%x", sha256.Sum256(b)), nil
}
func decodeJSON(raw []byte, value any) error {
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	return d.Decode(value)
}
func object(text string) map[string]any {
	var v map[string]any
	_ = decodeJSON([]byte(text), &v)
	if v == nil {
		v = map[string]any{}
	}
	return v
}
func textOK(text string) bool { return strings.TrimSpace(text) != "" && utf8.ValidString(text) }

type Task struct {
	ID       int64          `json:"id"`
	Title    string         `json:"title"`
	Status   string         `json:"status"`
	Parents  []int64        `json:"parents"`
	Aliases  []string       `json:"aliases"`
	Snapshot map[string]any `json:"snapshot"`
	Revision int64          `json:"revision"`
}
type CreateTask struct {
	Title    string         `json:"title"`
	Parents  []int64        `json:"parents,omitempty"`
	Aliases  []string       `json:"aliases,omitempty"`
	Snapshot map[string]any `json:"snapshot,omitempty"`
}

func (e *Engine) task(id int64) (Task, error) {
	rows, err := e.db.Query("SELECT * FROM tasks WHERE id=?", id)
	if err != nil {
		return Task{}, err
	}
	if len(rows) == 0 {
		return Task{}, problem("not_found", "task does not exist")
	}
	r := rows[0]
	task := Task{ID: id, Title: r["title"].(string), Status: r["status"].(string), Snapshot: object(r["snapshot"].(string)), Revision: r["revision"].(int64), Parents: []int64{}, Aliases: []string{}}
	rows, err = e.db.Query("SELECT parent FROM dependencies WHERE child=? ORDER BY parent", id)
	if err != nil {
		return Task{}, err
	}
	for _, r = range rows {
		task.Parents = append(task.Parents, r["parent"].(int64))
	}
	rows, err = e.db.Query("SELECT alias FROM aliases WHERE task_id=? ORDER BY alias", id)
	if err != nil {
		return Task{}, err
	}
	for _, r = range rows {
		task.Aliases = append(task.Aliases, r["alias"].(string))
	}
	return task, nil
}
func (e *Engine) Task(id int64) (task Task, err error) {
	err = e.locked(func() error { task, err = e.task(id); return err })
	return
}
func (e *Engine) CreateTask(input CreateTask) (task Task, err error) {
	if !textOK(input.Title) {
		return task, problem("invalid", "task title must be nonempty UTF-8")
	}
	if input.Snapshot == nil {
		input.Snapshot = map[string]any{}
	}
	snapshot, err := encode(input.Snapshot)
	if err != nil {
		return task, err
	}
	err = e.transaction(func() error {
		for _, id := range input.Parents {
			if _, er := e.task(id); er != nil {
				return er
			}
		}
		if er := e.db.Exec("INSERT INTO tasks(title,status,snapshot,created_at) VALUES(?,'open',?,?)", input.Title, snapshot, now()); er != nil {
			return er
		}
		id := e.db.LastID()
		for _, parent := range input.Parents {
			if er := e.db.Exec("INSERT OR IGNORE INTO dependencies(child,parent) VALUES(?,?)", id, parent); er != nil {
				return er
			}
		}
		for _, alias := range input.Aliases {
			alias = normalize(alias)
			if alias == "" {
				return problem("invalid", "empty alias")
			}
			if er := e.db.Exec("INSERT OR IGNORE INTO aliases VALUES(?,?)", id, alias); er != nil {
				return er
			}
		}
		task, err = e.task(id)
		return err
	})
	return
}

type Evidence struct {
	ID       int64          `json:"id"`
	TaskID   int64          `json:"task_id"`
	Role     string         `json:"role"`
	Content  string         `json:"content"`
	Source   map[string]any `json:"source"`
	Citation string         `json:"citation"`
	Score    float64        `json:"score"`
}
type Append struct {
	TaskID  int64          `json:"task_id"`
	Role    string         `json:"role"`
	Content string         `json:"content"`
	Source  map[string]any `json:"source,omitempty"`
}

func evidence(r sqlite.Row) Evidence {
	id, task := r["id"].(int64), r["task_id"].(int64)
	score, _ := r["score"].(float64)
	return Evidence{id, task, r["role"].(string), r["content"].(string), object(r["source"].(string)), fmt.Sprintf("T%d:M%d", task, id), score}
}
func (e *Engine) append(input Append) (Evidence, error) {
	if input.Role != "user" && input.Role != "assistant" && input.Role != "tool" && input.Role != "document" {
		return Evidence{}, problem("invalid", "invalid evidence role")
	}
	if !textOK(input.Content) {
		return Evidence{}, problem("invalid", "evidence must be nonempty UTF-8")
	}
	if input.Source == nil {
		input.Source = map[string]any{}
	}
	source, err := encode(input.Source)
	if err != nil {
		return Evidence{}, err
	}
	if err = e.db.Exec("INSERT INTO messages(task_id,role,content,source,created_at) VALUES(?,?,?,?,?)", input.TaskID, input.Role, input.Content, source, now()); err != nil {
		return Evidence{}, err
	}
	id := e.db.LastID()
	return Evidence{id, input.TaskID, input.Role, input.Content, input.Source, fmt.Sprintf("T%d:M%d", input.TaskID, id), 0}, nil
}
func (e *Engine) AppendBatch(inputs []Append) (out []Evidence, err error) {
	out = []Evidence{}
	err = e.transaction(func() error {
		for _, input := range inputs {
			v, er := e.append(input)
			if er != nil {
				return er
			}
			out = append(out, v)
		}
		return nil
	})
	if err != nil {
		out = nil
	}
	return
}

var stops = func() map[string]bool {
	m := map[string]bool{}
	for _, w := range strings.Fields("a an the and or to of in on at for with from is are was were be i me my you your it its this that what which who how when where why do does did can could would should please") {
		m[w] = true
	}
	return m
}()

func tokens(text string) []string {
	return strings.FieldsFunc(cases.Fold().String(norm.NFKC.String(text)), func(r rune) bool { return !unicode.IsLetter(r) && !unicode.IsNumber(r) })
}
func normalize(text string) string { return strings.Join(tokens(text), " ") }
func terms(query string) []string {
	seen := map[string]bool{}
	out := []string{}
	for _, w := range tokens(query) {
		if !seen[w] && !stops[w] {
			seen[w] = true
			out = append(out, `"`+strings.ReplaceAll(w, `"`, `""`)+`"`)
			if len(out) == 64 {
				break
			}
		}
	}
	return out
}
func (e *Engine) search(query string, ids []int64, limit int) ([]Evidence, error) {
	if limit < 1 || limit > 1000 {
		return nil, problem("invalid", "search limit must be between 1 and 1000")
	}
	parts := terms(query)
	if len(parts) == 0 {
		return []Evidence{}, nil
	}
	sql := "SELECT m.*, -bm25(evidence_index) AS score FROM evidence_index JOIN messages m ON m.id=evidence_index.rowid WHERE evidence_index MATCH ?"
	args := []any{strings.Join(parts, " OR ")}
	if ids != nil {
		if len(ids) == 0 {
			return []Evidence{}, nil
		}
		marks := make([]string, len(ids))
		for i, id := range ids {
			marks[i] = "?"
			args = append(args, id)
		}
		sql += " AND m.task_id IN (" + strings.Join(marks, ",") + ")"
	}
	sql += " ORDER BY score DESC,m.id DESC LIMIT ?"
	args = append(args, limit)
	rows, err := e.db.Query(sql, args...)
	if err != nil {
		return nil, err
	}
	out := []Evidence{}
	for _, r := range rows {
		out = append(out, evidence(r))
	}
	return out, nil
}
func (e *Engine) Search(query string, ids []int64, limit int) (out []Evidence, err error) {
	err = e.locked(func() error { out, err = e.search(query, ids, limit); return err })
	return
}

type Resolution struct {
	Status     string  `json:"status"`
	TaskID     int64   `json:"task_id,omitempty"`
	Candidates []int64 `json:"candidates"`
}

var explicitID = regexp.MustCompile(`(?i)\bT([1-9][0-9]*)\b`)

func (e *Engine) Resolve(query string) (out Resolution, err error) {
	out = Resolution{Status: "not_found", Candidates: []int64{}}
	err = e.locked(func() error {
		matches := map[int64]bool{}
		explicit := explicitID.FindAllStringSubmatch(norm.NFKC.String(query), -1)
		for _, match := range explicit {
			id, er := strconv.ParseInt(match[1], 10, 64)
			if er != nil {
				return nil
			}
			if _, er = e.task(id); er != nil {
				if Code(er) == "not_found" {
					return nil
				}
				return er
			}
			matches[id] = true
		}
		{
			rows, er := e.db.Query("SELECT id,title AS phrase FROM tasks UNION ALL SELECT task_id,alias FROM aliases")
			if er != nil {
				return er
			}
			haystack := " " + normalize(query) + " "
			for _, r := range rows {
				phrase := normalize(r["phrase"].(string))
				if phrase != "" && strings.Contains(haystack, " "+phrase+" ") {
					matches[r["id"].(int64)] = true
				}
			}
		}
		for id := range matches {
			out.Candidates = append(out.Candidates, id)
		}
		sort.Slice(out.Candidates, func(i, j int) bool { return out.Candidates[i] < out.Candidates[j] })
		if len(out.Candidates) == 1 {
			out.Status = "resolved"
			out.TaskID = out.Candidates[0]
		} else if len(out.Candidates) > 1 {
			out.Status = "ambiguous"
		}
		return nil
	})
	return
}
func (e *Engine) Info() (info map[string]any, err error) {
	err = e.locked(func() error {
		info = map[string]any{"version": Version, "sqlite": e.db.Version(), "harness_schema": 4, "base_schema": 1}
		for _, name := range []string{"tasks", "messages", "cmp_turns", "cmp_tool_calls", "cmp_model_calls", "cmp_model_responses", "cmp_retired_turns"} {
			rows, er := e.db.Query("SELECT count(*) AS n FROM " + name)
			if er != nil {
				return er
			}
			info[name] = rows[0]["n"]
		}
		return nil
	})
	return
}
