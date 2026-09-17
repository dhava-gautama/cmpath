// cmpath-bench times embedded engine calls; output encoding is outside timers.
package main

import (
	"cmpath.local/native/engine"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"runtime"
	"time"
)

type Query struct {
	ID   string `json:"query_id"`
	Kind string `json:"kind"`
	Text string `json:"query"`
}

func must(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
func main() {
	db := flag.String("db", "", "database path")
	mode := flag.String("mode", "search", "search or lifecycle")
	queries := flag.String("queries", "", "query JSON file")
	size := flag.Int("size", 0, "corpus size")
	warmup := flag.Int("warmup", 10, "untimed operations")
	count := flag.Int("count", 100, "lifecycle measured operations")
	flag.Parse()
	e, err := engine.Open(*db)
	must(err)
	defer e.Close()
	rows := []map[string]any{}
	if *mode == "search" {
		raw, err := os.ReadFile(*queries)
		must(err)
		var qs []Query
		must(json.Unmarshal(raw, &qs))
		if len(qs) == 0 {
			must(fmt.Errorf("empty queries"))
		}
		for i := 0; i < *warmup; i++ {
			_, err := e.Search(qs[i%len(qs)].Text, nil, 8)
			must(err)
		}
		for _, q := range qs {
			start := time.Now()
			hits, err := e.Search(q.Text, nil, 8)
			elapsed := time.Since(start)
			must(err)
			ids := []int64{}
			for _, h := range hits {
				ids = append(ids, h.ID)
			}
			rows = append(rows, map[string]any{"method": "go_embedded", "corpus_size": *size, "query_id": q.ID, "kind": q.Kind, "query": q.Text, "hit_ids": ids, "search_ms": float64(elapsed.Nanoseconds()) / 1e6})
		}
	} else if *mode == "lifecycle" {
		task, err := e.CreateTask(engine.CreateTask{Title: "Lifecycle benchmark"})
		must(err)
		for i := 0; i < *warmup+*count; i++ {
			id := fmt.Sprintf("operation-%03d", i)
			request := engine.Request{RequestID: id, TaskID: task.ID, Query: "Record a benchmark acknowledgement", Budget: 2000, Scope: "task", RetrievalLimit: 24, Recent: 4, Counting: "estimated"}
			start := time.Now()
			turn, err := e.Begin(request)
			must(err)
			result, err := e.Commit(id, turn.Generation, engine.Reply{Text: "Acknowledged."})
			elapsed := time.Since(start)
			must(err)
			if i >= *warmup {
				rows = append(rows, map[string]any{"method": "go_embedded", "operation_id": id, "lifecycle_ms": float64(elapsed.Nanoseconds()) / 1e6, "status": result.Status})
			}
		}
	} else {
		must(fmt.Errorf("unknown mode %q", *mode))
	}
	info, err := e.Info()
	must(err)
	must(json.NewEncoder(os.Stdout).Encode(map[string]any{"rows": rows, "info": info, "go_version": runtime.Version(), "gomaxprocs": runtime.GOMAXPROCS(0)}))
}
