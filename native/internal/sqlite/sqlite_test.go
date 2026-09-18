package sqlite

import (
	"bytes"
	"errors"
	"path/filepath"
	"testing"
)

func openTest(t *testing.T) *DB {
	t.Helper()
	db, err := Open(filepath.Join(t.TempDir(), "driver.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	return db
}

// seedValues writes one row covering every storage class the driver claims to
// decode faithfully.
func seedValues(t *testing.T, db *DB) {
	t.Helper()
	if err := db.Script("CREATE TABLE samples(t TEXT, i INTEGER, r REAL, n, b BLOB, e BLOB)"); err != nil {
		t.Fatal(err)
	}
	if err := db.Exec("INSERT INTO samples VALUES(?,?,?,?,?,?)", "text", int64(7), 1.5, nil, []byte{0xFF, 0xFE}, []byte{}); err != nil {
		t.Fatal(err)
	}
}

func TestDecodeKeepsStorageClassesDistinct(t *testing.T) {
	db := openTest(t)
	seedValues(t, db)
	rows, err := db.Query("SELECT * FROM samples")
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 {
		t.Fatal(rows)
	}
	row := rows[0]
	if got, ok := row["t"].(string); !ok || got != "text" {
		t.Fatalf("text column decoded to %#v", row["t"])
	}
	if got, ok := row["i"].(int64); !ok || got != 7 {
		t.Fatalf("integer column decoded to %#v", row["i"])
	}
	if got, ok := row["r"].(float64); !ok || got != 1.5 {
		t.Fatalf("real column decoded to %#v", row["r"])
	}
	if value, present := row["n"]; !present || value != nil {
		t.Fatalf("NULL column decoded to %#v", row["n"])
	}
	// A blob must stay a blob. Decoding it through the text interface made it
	// indistinguishable from TEXT, so a caller rebinding it wrote text and
	// SQLite then compared no rows equal.
	if got, ok := row["b"].([]byte); !ok || !bytes.Equal(got, []byte{0xFF, 0xFE}) {
		t.Fatalf("blob column decoded to %#v", row["b"])
	}
	if got, ok := row["e"].([]byte); !ok || got == nil || len(got) != 0 {
		t.Fatalf("empty blob column decoded to %#v", row["e"])
	}
}

// A blob rebinding must still match its own row, and a text binding of the
// same bytes must not: this is the comparison SQLite actually performs and the
// reason the old text coercion turned every retention delete into a no-op.
func TestBlobRebindingMatchesOnlyBlobColumn(t *testing.T) {
	db := openTest(t)
	seedValues(t, db)
	rows, err := db.Query("SELECT b FROM samples")
	if err != nil {
		t.Fatal(err)
	}
	blob, err := rows[0].Blob("b")
	if err != nil {
		t.Fatal(err)
	}
	matched, err := db.Query("SELECT count(*) AS n FROM samples WHERE b=?", blob)
	if err != nil {
		t.Fatal(err)
	}
	if n, _ := matched[0].Int("n"); n != 1 {
		t.Fatalf("blob did not rebind to its own row: %d", n)
	}
	asText, err := db.Query("SELECT count(*) AS n FROM samples WHERE b=?", string(blob))
	if err != nil {
		t.Fatal(err)
	}
	if n, _ := asText[0].Int("n"); n != 0 {
		t.Fatalf("text compared equal to a blob: %d", n)
	}
}

func TestQueryKeepsDifferentBlobsDistinct(t *testing.T) {
	db := openTest(t)
	if err := db.Script("CREATE TABLE blobs(v BLOB)"); err != nil {
		t.Fatal(err)
	}
	for _, v := range [][]byte{{0xFF, 0xFE}, {0xFC, 0xFC}} {
		if err := db.Exec("INSERT INTO blobs VALUES(?)", v); err != nil {
			t.Fatal(err)
		}
	}
	rows, err := db.Query("SELECT v FROM blobs ORDER BY rowid")
	if err != nil {
		t.Fatal(err)
	}
	first, _ := rows[0].Blob("v")
	second, _ := rows[1].Blob("v")
	if bytes.Equal(first, second) {
		t.Fatalf("distinct blobs decoded identically: %x", first)
	}
}

func TestTypedAccessorsFailClosed(t *testing.T) {
	db := openTest(t)
	seedValues(t, db)
	rows, err := db.Query("SELECT * FROM samples")
	if err != nil {
		t.Fatal(err)
	}
	row := rows[0]
	if text, err := row.Text("t"); err != nil || text != "text" {
		t.Fatalf("Text(t) = %q, %v", text, err)
	}
	if number, err := row.Int("i"); err != nil || number != 7 {
		t.Fatalf("Int(i) = %d, %v", number, err)
	}
	if number, err := row.Float("r"); err != nil || number != 1.5 {
		t.Fatalf("Float(r) = %v, %v", number, err)
	}
	if blob, err := row.Blob("b"); err != nil || !bytes.Equal(blob, []byte{0xFF, 0xFE}) {
		t.Fatalf("Blob(b) = %x, %v", blob, err)
	}
	cases := []struct {
		name   string
		column string
		got    ValueKind
		want   ValueKind
		absent bool
		read   func(Row, string) error
	}{
		{name: "null text", column: "n", got: Null, want: Text, read: func(r Row, c string) error { _, err := r.Text(c); return err }},
		{name: "integer text", column: "i", got: Integer, want: Text, read: func(r Row, c string) error { _, err := r.Text(c); return err }},
		{name: "blob text", column: "b", got: Blob, want: Text, read: func(r Row, c string) error { _, err := r.Text(c); return err }},
		{name: "text integer", column: "t", got: Text, want: Integer, read: func(r Row, c string) error { _, err := r.Int(c); return err }},
		{name: "integer real", column: "i", got: Integer, want: Real, read: func(r Row, c string) error { _, err := r.Float(c); return err }},
		{name: "text blob", column: "t", got: Text, want: Blob, read: func(r Row, c string) error { _, err := r.Blob(c); return err }},
		{name: "absent", column: "missing", want: Text, absent: true, read: func(r Row, c string) error { _, err := r.Text(c); return err }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := tc.read(row, tc.column)
			var typeErr *TypeError
			if err == nil {
				t.Fatalf("want a coded type error, got nil")
			}
			if !errors.As(err, &typeErr) {
				t.Fatalf("want *TypeError, got %T (%v)", err, err)
			}
			if typeErr.Column != tc.column || typeErr.Want != tc.want || typeErr.Absent != tc.absent {
				t.Fatalf("unexpected classification: %+v", typeErr)
			}
			if !tc.absent && typeErr.Got != tc.got {
				t.Fatalf("got class %s, want %s (%+v)", typeErr.Got, tc.got, typeErr)
			}
		})
	}
}

func TestBusyTimeoutFloor(t *testing.T) {
	read := func(t *testing.T, db *DB) int64 {
		t.Helper()
		rows, err := db.Query("PRAGMA busy_timeout")
		if err != nil {
			t.Fatal(err)
		}
		value, err := rows[0].Int("timeout")
		if err != nil {
			t.Fatal(err)
		}
		return value
	}
	t.Run("default", func(t *testing.T) {
		if ms := read(t, openTest(t)); ms != BusyTimeoutMS {
			t.Fatalf("default busy timeout %d ms, want %d", ms, BusyTimeoutMS)
		}
	})
	t.Run("raised", func(t *testing.T) {
		db, err := Open(filepath.Join(t.TempDir(), "wide.db"), WithBusyTimeoutMS(30_000))
		if err != nil {
			t.Fatal(err)
		}
		defer db.Close()
		if ms := read(t, db); ms != 30_000 {
			t.Fatalf("raised busy timeout %d ms", ms)
		}
	})
	t.Run("never below the floor", func(t *testing.T) {
		db, err := Open(filepath.Join(t.TempDir(), "narrow.db"), WithBusyTimeoutMS(1))
		if err != nil {
			t.Fatal(err)
		}
		defer db.Close()
		if ms := read(t, db); ms != BusyTimeoutMS {
			t.Fatalf("busy timeout %d ms fell below the floor", ms)
		}
	})
}

// A deferred read transaction that lost its snapshot to a committed writer
// gets SQLITE_BUSY_SNAPSHOT, which the driver must report as a distinct busy
// condition rather than as storage damage.
func TestBusySnapshotIsClassifiedAsBusy(t *testing.T) {
	path := filepath.Join(t.TempDir(), "snapshot.db")
	reader := openTestAt(t, path)
	if err := reader.Script("PRAGMA journal_mode=WAL; CREATE TABLE t(x); INSERT INTO t VALUES(1);"); err != nil {
		t.Fatal(err)
	}
	if err := reader.Exec("BEGIN"); err != nil {
		t.Fatal(err)
	}
	if _, err := reader.Query("SELECT x FROM t"); err != nil {
		t.Fatal(err)
	}
	writer := openTestAt(t, path)
	if err := writer.Exec("BEGIN IMMEDIATE"); err != nil {
		t.Fatal(err)
	}
	if err := writer.Exec("UPDATE t SET x=2"); err != nil {
		t.Fatal(err)
	}
	if err := writer.Exec("COMMIT"); err != nil {
		t.Fatal(err)
	}
	err := reader.Exec("UPDATE t SET x=3")
	if err == nil {
		t.Fatal("a stale snapshot should not upgrade to a write")
	}
	var coded *Error
	if !errors.As(err, &coded) {
		t.Fatalf("want *Error, got %T (%v)", err, err)
	}
	if !coded.Busy() {
		t.Fatalf("stale snapshot classified as %d %q", coded.Code, coded.Message)
	}
	if coded.Code != 5 && coded.Code != 5|(2<<8) {
		t.Fatalf("unexpected busy code %d", coded.Code)
	}
}

func openTestAt(t *testing.T, path string) *DB {
	t.Helper()
	db, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	return db
}

// A statement that matches no rows still reports success; Changes is how a
// caller tells an effective statement from an empty match.
func TestChangesDistinguishesEmptyMatch(t *testing.T) {
	db := openTest(t)
	seedValues(t, db)
	if err := db.Exec("UPDATE samples SET i=8 WHERE t=?", "text"); err != nil {
		t.Fatal(err)
	}
	if n := db.Changes(); n != 1 {
		t.Fatalf("effective update reported %d changed rows", n)
	}
	// The comparison that made a retention apply a no-op: text never equals a
	// blob, so the statement succeeds and matches nothing.
	if err := db.Exec("UPDATE samples SET i=9 WHERE b=?", "text"); err != nil {
		t.Fatal(err)
	}
	if n := db.Changes(); n != 0 {
		t.Fatalf("a text-against-blob comparison matched %d rows", n)
	}
}
