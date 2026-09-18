// Package sqlite owns the small SQLite C boundary used by the memory engine.
// A connection is serialized by its Engine; no C pointer is exposed to callers.
package sqlite

/*
#cgo CFLAGS: -DSQLITE_ENABLE_FTS5 -DSQLITE_THREADSAFE=1
#cgo LDFLAGS: -lm
#include <sqlite3.h>
#include <stdlib.h>
static int bind_text_copy(sqlite3_stmt *s, int n, const char *v, int len) {
    return sqlite3_bind_text(s, n, v, len, SQLITE_TRANSIENT);
}
static int bind_blob_copy(sqlite3_stmt *s, int n, const void *v, int len) {
    // sqlite3_bind_blob() maps a NULL pointer to SQL NULL regardless of the
    // length, which would silently turn an empty blob into NULL. zeroblob is
    // a real zero-length BLOB, so the storage class survives the round trip.
    if (len == 0) return sqlite3_bind_zeroblob(s, n, 0);
    return sqlite3_bind_blob(s, n, v, len, SQLITE_TRANSIENT);
}
*/
import "C"

import (
	"fmt"
	"unsafe"
)

// BusyTimeoutMS is the floor for a connection's SQLite busy timeout. A caller
// may ask for longer, never less.
//
// WAL readers never block, but two writers serialize on one database file, and
// the measured single-transaction write horizon already exceeds the 5 s
// default this wrapper used before: Export of 160k messages is about 5.2 s,
// AppendBatch of 50k rows about 12.0 s, RetentionPlan of 20k turns about
// 8.4 s and ApplyRetention of 20k turns about 10.8 s, while a throttled Export
// held the write lock for 311 s. Below the floor a collided writer gives up
// immediately with "database is locked" instead of waiting. The Python
// sibling applies the same 10000 ms floor over its `timeout` argument.
const BusyTimeoutMS = 10_000

// ValueKind is a SQLite storage class, as reported by sqlite3_column_type.
type ValueKind string

const (
	Null    ValueKind = "null"
	Integer ValueKind = "integer"
	Real    ValueKind = "real"
	Text    ValueKind = "text"
	Blob    ValueKind = "blob"
)

// Error is a SQLite failure carrying the C result code.
type Error struct {
	Code    int
	Message string
}

func (e *Error) Error() string { return fmt.Sprintf("sqlite %d: %s", e.Code, e.Message) }

// Busy reports whether the failure is lock contention rather than corruption
// or a permanent schema problem. The extended SQLITE_BUSY variants
// (SQLITE_BUSY_SNAPSHOT, SQLITE_BUSY_RECOVERY, SQLITE_BUSY_TIMEOUT) differ
// from the primary code only in the high bits, so testing the low byte
// recognises all of them.
func (e *Error) Busy() bool { return e.Code&0xff == int(C.SQLITE_BUSY) }

// TypeError reports a column whose storage class cannot be represented by the
// requested Go type, including SQL NULL and an absent column. A caller that
// reads a column the schema does not guarantee the type of gets a coded
// failure instead of a panic or a silent coercion.
type TypeError struct {
	Column string
	Got    ValueKind
	Want   ValueKind
	Absent bool
}

func (e *TypeError) Error() string {
	if e.Absent {
		return fmt.Sprintf("sqlite: column %q is absent, want %s", e.Column, e.Want)
	}
	return fmt.Sprintf("sqlite: column %q has storage class %s, want %s", e.Column, e.Got, e.Want)
}

type Row map[string]any
type DB struct{ handle *C.sqlite3 }

// Option adjusts a connection at Open time.
type Option func(*config)

type config struct{ busyTimeoutMS int }

// WithBusyTimeoutMS raises the lock wait above the BusyTimeoutMS floor.
// A smaller request is ignored: the floor is a correctness minimum, not a
// preference, because the write horizon of a single maintenance transaction
// routinely exceeds it.
func WithBusyTimeoutMS(ms int) Option { return func(c *config) { c.busyTimeoutMS = ms } }

// effectiveBusyTimeout applies the documented floor over the requested value.
func effectiveBusyTimeout(ms int) int {
	if ms < BusyTimeoutMS {
		return BusyTimeoutMS
	}
	return ms
}

func Open(path string, opts ...Option) (*DB, error) {
	cfg := config{}
	for _, opt := range opts {
		opt(&cfg)
	}
	name := C.CString(path)
	defer C.free(unsafe.Pointer(name))
	db := &DB{}
	code := C.sqlite3_open_v2(name, &db.handle, C.SQLITE_OPEN_READWRITE|C.SQLITE_OPEN_CREATE|C.SQLITE_OPEN_FULLMUTEX, nil)
	if code != C.SQLITE_OK {
		err := db.err(code)
		db.Close()
		return nil, err
	}
	C.sqlite3_busy_timeout(db.handle, C.int(effectiveBusyTimeout(cfg.busyTimeoutMS)))
	return db, nil
}
func (d *DB) err(code C.int) error {
	return &Error{Code: int(code), Message: C.GoString(C.sqlite3_errmsg(d.handle))}
}
func (d *DB) Close() {
	if d.handle != nil {
		C.sqlite3_close_v2(d.handle)
		d.handle = nil
	}
}
func (d *DB) Version() string { return C.GoString(C.sqlite3_libversion()) }
func (d *DB) LastID() int64   { return int64(C.sqlite3_last_insert_rowid(d.handle)) }

// Changes is the number of rows modified by the most recent INSERT, UPDATE or
// DELETE on this connection. Exec discards it, so a statement that matches no
// rows still reports success; a caller that must distinguish a real effect
// from an empty match reads this immediately after Exec. The engine serializes
// statements on a connection, so the reading is unambiguous.
func (d *DB) Changes() int64 { return int64(C.sqlite3_changes(d.handle)) }

func (d *DB) Script(sql string) error {
	s := C.CString(sql)
	defer C.free(unsafe.Pointer(s))
	if code := C.sqlite3_exec(d.handle, s, nil, nil, nil); code != C.SQLITE_OK {
		return d.err(code)
	}
	return nil
}
func (d *DB) prepare(sql string, args []any) (*C.sqlite3_stmt, error) {
	s := C.CString(sql)
	defer C.free(unsafe.Pointer(s))
	var statement *C.sqlite3_stmt
	if code := C.sqlite3_prepare_v2(d.handle, s, -1, &statement, nil); code != C.SQLITE_OK {
		return nil, d.err(code)
	}
	if int(C.sqlite3_bind_parameter_count(statement)) != len(args) {
		C.sqlite3_finalize(statement)
		return nil, fmt.Errorf("wrong SQL argument count")
	}
	for i, arg := range args {
		var code C.int
		switch v := arg.(type) {
		case nil:
			code = C.sqlite3_bind_null(statement, C.int(i+1))
		case int:
			code = C.sqlite3_bind_int64(statement, C.int(i+1), C.sqlite3_int64(v))
		case int64:
			code = C.sqlite3_bind_int64(statement, C.int(i+1), C.sqlite3_int64(v))
		case bool:
			var n int
			if v {
				n = 1
			}
			code = C.sqlite3_bind_int(statement, C.int(i+1), C.int(n))
		case float64:
			code = C.sqlite3_bind_double(statement, C.int(i+1), C.double(v))
		case string:
			text := C.CString(v)
			code = C.bind_text_copy(statement, C.int(i+1), text, C.int(len(v)))
			C.free(unsafe.Pointer(text))
		case []byte:
			// A nil slice binds as NULL, matching the nil interface case; an
			// empty non-nil slice binds as a real zero-length blob.
			var blob unsafe.Pointer
			if len(v) > 0 {
				blob = unsafe.Pointer(&v[0])
			}
			code = C.bind_blob_copy(statement, C.int(i+1), blob, C.int(len(v)))
		default:
			C.sqlite3_finalize(statement)
			return nil, fmt.Errorf("unsupported SQL parameter %T", arg)
		}
		if code != C.SQLITE_OK {
			C.sqlite3_finalize(statement)
			return nil, d.err(code)
		}
	}
	return statement, nil
}
func (d *DB) Exec(sql string, args ...any) error {
	s, err := d.prepare(sql, args)
	if err != nil {
		return err
	}
	defer C.sqlite3_finalize(s)
	code := C.sqlite3_step(s)
	if code != C.SQLITE_DONE {
		return d.err(code)
	}
	return nil
}

// Query decodes each column by its SQLite storage class: NULL to nil, INTEGER
// to int64, FLOAT to float64, TEXT to string and BLOB to []byte. A blob is
// never presented as a string: that coercion loses the distinction, and
// SQLite never compares a blob equal to text, so a rebinding of the coerced
// value silently matches no row. Any other class fails closed with a coded
// error rather than being guessed.
func (d *DB) Query(sql string, args ...any) ([]Row, error) {
	s, err := d.prepare(sql, args)
	if err != nil {
		return nil, err
	}
	defer C.sqlite3_finalize(s)
	rows := []Row{}
	for {
		code := C.sqlite3_step(s)
		if code == C.SQLITE_DONE {
			return rows, nil
		}
		if code != C.SQLITE_ROW {
			return nil, d.err(code)
		}
		row := Row{}
		for i := C.int(0); i < C.sqlite3_column_count(s); i++ {
			key := C.GoString(C.sqlite3_column_name(s, i))
			switch kind := C.sqlite3_column_type(s, i); kind {
			case C.SQLITE_NULL:
				row[key] = nil
			case C.SQLITE_INTEGER:
				row[key] = int64(C.sqlite3_column_int64(s, i))
			case C.SQLITE_FLOAT:
				row[key] = float64(C.sqlite3_column_double(s, i))
			case C.SQLITE_TEXT:
				// Read the text pointer before its length: sqlite3_column_bytes
				// may otherwise convert the value first, and the copy owns the
				// bytes independently of the statement.
				text := C.sqlite3_column_text(s, i)
				row[key] = C.GoStringN((*C.char)(unsafe.Pointer(text)), C.sqlite3_column_bytes(s, i))
			case C.SQLITE_BLOB:
				row[key] = C.GoBytes(unsafe.Pointer(C.sqlite3_column_blob(s, i)), C.sqlite3_column_bytes(s, i))
			default:
				return nil, &Error{Code: int(C.SQLITE_MISMATCH), Message: fmt.Sprintf("column %q has unsupported storage class %d", key, int(kind))}
			}
		}
		rows = append(rows, row)
	}
}

// Text returns a TEXT column. A NULL, absent or non-TEXT column is a coded
// *TypeError. Use it for any column the schema does not guarantee to be
// non-NULL text; a bare type assertion on the Row map panics on those.
func (r Row) Text(column string) (string, error) {
	value, ok := r[column]
	if !ok {
		return "", &TypeError{Column: column, Want: Text, Absent: true}
	}
	text, ok := value.(string)
	if !ok {
		return "", typeError(column, value, Text)
	}
	return text, nil
}

// Int returns an INTEGER column as int64.
func (r Row) Int(column string) (int64, error) {
	value, ok := r[column]
	if !ok {
		return 0, &TypeError{Column: column, Want: Integer, Absent: true}
	}
	number, ok := value.(int64)
	if !ok {
		return 0, typeError(column, value, Integer)
	}
	return number, nil
}

// Float returns a FLOAT column as float64. It does not widen an INTEGER: a
// caller that accepts either class must say so with Int first.
func (r Row) Float(column string) (float64, error) {
	value, ok := r[column]
	if !ok {
		return 0, &TypeError{Column: column, Want: Real, Absent: true}
	}
	number, ok := value.(float64)
	if !ok {
		return 0, typeError(column, value, Real)
	}
	return number, nil
}

// Blob returns a BLOB column. An empty blob decodes to an empty, non-nil
// slice; a NULL or non-blob column is a coded *TypeError.
func (r Row) Blob(column string) ([]byte, error) {
	value, ok := r[column]
	if !ok {
		return nil, &TypeError{Column: column, Want: Blob, Absent: true}
	}
	blob, ok := value.([]byte)
	if !ok {
		return nil, typeError(column, value, Blob)
	}
	return blob, nil
}

// typeError names the storage class a decoded value actually carries.
func typeError(column string, value any, want ValueKind) *TypeError {
	got := Null
	switch value.(type) {
	case nil:
		got = Null
	case int64:
		got = Integer
	case float64:
		got = Real
	case string:
		got = Text
	case []byte:
		got = Blob
	}
	return &TypeError{Column: column, Got: got, Want: want}
}
