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
*/
import "C"

import (
	"fmt"
	"unsafe"
)

type Row map[string]any
type DB struct{ handle *C.sqlite3 }

func Open(path string) (*DB, error) {
	name := C.CString(path)
	defer C.free(unsafe.Pointer(name))
	db := &DB{}
	code := C.sqlite3_open_v2(name, &db.handle, C.SQLITE_OPEN_READWRITE|C.SQLITE_OPEN_CREATE|C.SQLITE_OPEN_FULLMUTEX, nil)
	if code != C.SQLITE_OK {
		err := db.err(code)
		db.Close()
		return nil, err
	}
	C.sqlite3_busy_timeout(db.handle, 5000)
	return db, nil
}
func (d *DB) err(code C.int) error {
	return fmt.Errorf("sqlite %d: %s", int(code), C.GoString(C.sqlite3_errmsg(d.handle)))
}
func (d *DB) Close() {
	if d.handle != nil {
		C.sqlite3_close_v2(d.handle)
		d.handle = nil
	}
}
func (d *DB) Version() string { return C.GoString(C.sqlite3_libversion()) }
func (d *DB) LastID() int64   { return int64(C.sqlite3_last_insert_rowid(d.handle)) }
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
			switch C.sqlite3_column_type(s, i) {
			case C.SQLITE_NULL:
				row[key] = nil
			case C.SQLITE_INTEGER:
				row[key] = int64(C.sqlite3_column_int64(s, i))
			case C.SQLITE_FLOAT:
				row[key] = float64(C.sqlite3_column_double(s, i))
			default:
				row[key] = C.GoStringN((*C.char)(unsafe.Pointer(C.sqlite3_column_text(s, i))), C.sqlite3_column_bytes(s, i))
			}
		}
		rows = append(rows, row)
	}
}
