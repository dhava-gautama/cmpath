package engine

import (
	"errors"
	"fmt"
	"path/filepath"
	"testing"
	"time"

	"cmpath.local/native/internal/sqlite"
)

func TestCodeClassifiesBusyAndUnrepresentableColumns(t *testing.T) {
	wrapped := fmt.Errorf("begin: %w", &sqlite.Error{Code: 5, Message: "database is locked"})
	cases := []struct {
		name string
		err  error
		want string
	}{
		{name: "busy", err: &sqlite.Error{Code: 5, Message: "database is locked"}, want: "busy"},
		{name: "busy snapshot", err: &sqlite.Error{Code: 5 | (2 << 8), Message: "database is locked"}, want: "busy"},
		{name: "busy recovery", err: &sqlite.Error{Code: 5 | (1 << 8), Message: "database is locked"}, want: "busy"},
		{name: "corrupt", err: &sqlite.Error{Code: 11, Message: "database disk image is malformed"}, want: "storage_error"},
		{name: "wrapped busy", err: wrapped, want: "busy"},
		{name: "unrepresentable column", err: &sqlite.TypeError{Column: "request_id", Got: sqlite.Blob, Want: sqlite.Text}, want: "integrity"},
		{name: "engine problem", err: problem("busy", "task already has an unfinished turn"), want: "busy"},
		{name: "unknown", err: errors.New("disk on fire"), want: "storage_error"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := Code(tc.err); got != tc.want {
				t.Fatalf("Code(%v) = %s, want %s", tc.err, got, tc.want)
			}
		})
	}
}

func TestBusyTimeoutFloorIsAppliedToTheConnection(t *testing.T) {
	busyTimeout := func(t *testing.T, e *Engine) int64 {
		t.Helper()
		rows, err := e.db.Query("PRAGMA busy_timeout")
		if err != nil {
			t.Fatal(err)
		}
		ms, err := rows[0].Int("timeout")
		if err != nil {
			t.Fatal(err)
		}
		return ms
	}
	open := func(t *testing.T, opts ...Option) *Engine {
		t.Helper()
		e, err := Open(filepath.Join(t.TempDir(), "timeout.db"), opts...)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(e.Close)
		return e
	}
	if ms := busyTimeout(t, open(t)); ms != sqlite.BusyTimeoutMS {
		t.Fatalf("default busy timeout %d ms, want the %d ms floor", ms, sqlite.BusyTimeoutMS)
	}
	if ms := busyTimeout(t, open(t, WithBusyTimeoutMS(30_000))); ms != 30_000 {
		t.Fatalf("a raised busy timeout was not respected: %d ms", ms)
	}
	if ms := busyTimeout(t, open(t, WithBusyTimeoutMS(100))); ms != sqlite.BusyTimeoutMS {
		t.Fatalf("busy timeout %d ms fell below the floor", ms)
	}
}

// A maintenance transaction can outlive the 5 s busy timeout this engine used
// to install, so a writer that collides with one must wait rather than fail
// immediately with "database is locked".
const contendedLockHold = 6500 * time.Millisecond

func sharedDatabase(t *testing.T) (string, *Engine, Task) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "contended.db")
	first, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(first.Close)
	task, err := first.CreateTask(CreateTask{Title: "contended writer"})
	if err != nil {
		t.Fatal(err)
	}
	return path, first, task
}

func secondConnection(t *testing.T, path string) *Engine {
	t.Helper()
	second, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(second.Close)
	return second
}

// holdWriteLock holds an exclusive write lock on path for hold and runs fn
// while the lock is held.
func holdWriteLock(t *testing.T, path string, hold time.Duration, fn func() error) (time.Duration, error) {
	t.Helper()
	holder, err := sqlite.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer holder.Close()
	if err = holder.Exec("BEGIN IMMEDIATE"); err != nil {
		t.Fatal(err)
	}
	released := make(chan struct{})
	go func() {
		time.Sleep(hold)
		_ = holder.Exec("ROLLBACK")
		close(released)
	}()
	started := time.Now()
	err = fn()
	elapsed := time.Since(started)
	<-released
	return elapsed, err
}

func batch(task Task, count int) []Append {
	out := make([]Append, count)
	for i := range out {
		out[i] = Append{TaskID: task.ID, Role: "user", Content: fmt.Sprintf("contended %d", i)}
	}
	return out
}

func TestConcurrentWriterAndHeldLock(t *testing.T) {
	t.Run("waits out a lock held past the old five second default", func(t *testing.T) {
		t.Parallel()
		path, first, task := sharedDatabase(t)
		second := secondConnection(t, path)
		elapsed, err := holdWriteLock(t, path, contendedLockHold, func() error {
			appended, er := second.AppendBatch(batch(task, 3))
			if er != nil {
				return er
			}
			if len(appended) != 3 {
				return fmt.Errorf("append batch returned %d of 3 rows", len(appended))
			}
			return nil
		})
		if err != nil {
			t.Fatalf("a lock held %s must be waited out, got %s: %v", contendedLockHold, Code(err), err)
		}
		if elapsed < contendedLockHold {
			t.Fatalf("write returned after %s, before the lock was released", elapsed)
		}
		info, err := first.Info()
		if err != nil {
			t.Fatal(err)
		}
		if info["messages"] != int64(3) {
			t.Fatalf("contended batch was not applied exactly once: %v", info["messages"])
		}
	})
	t.Run("reports a coded busy error when the floor expires", func(t *testing.T) {
		t.Parallel()
		path, first, task := sharedDatabase(t)
		second := secondConnection(t, path)
		hold := time.Duration(sqlite.BusyTimeoutMS)*time.Millisecond + 2*time.Second
		elapsed, err := holdWriteLock(t, path, hold, func() error {
			_, er := second.AppendBatch(batch(task, 3))
			return er
		})
		if err == nil {
			t.Fatalf("a lock held %s outlived the %d ms floor yet the write succeeded", hold, sqlite.BusyTimeoutMS)
		}
		if code := Code(err); code != "busy" {
			t.Fatalf("lock contention classified as %s, want busy: %v", code, err)
		}
		if elapsed < time.Duration(sqlite.BusyTimeoutMS)*time.Millisecond || elapsed >= hold {
			t.Fatalf("waited %s, want at least the %d ms floor and less than the %s hold", elapsed, sqlite.BusyTimeoutMS, hold)
		}
		info, err := first.Info()
		if err != nil {
			t.Fatal(err)
		}
		if info["messages"] != int64(0) {
			t.Fatalf("a refused write left %v rows behind", info["messages"])
		}
	})
}
