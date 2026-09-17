// A real local file-checksum workflow with memory embedded in this Go process.
// No inference service, Python process, RPC server or mock model is involved.
package main

import (
	"cmpath.local/native/engine"
	"context"
	"crypto/sha256"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
)

func main() {
	db := flag.String("db", "checksum-memory.db", "database path")
	input := flag.String("input", "", "file to hash")
	requestID := flag.String("request-id", "", "unique ID for this logical request")
	flag.Parse()
	if *input == "" || *requestID == "" {
		fmt.Fprintln(os.Stderr, "--input and --request-id are required")
		os.Exit(2)
	}
	path, err := filepath.Abs(*input)
	must(err)
	memory, err := engine.Open(*db)
	must(err)
	defer memory.Close()
	route, err := memory.Resolve("File integrity workflow")
	must(err)
	var task engine.Task
	if route.Status == "resolved" {
		task, err = memory.Task(route.TaskID)
	} else if route.Status == "not_found" {
		task, err = memory.CreateTask(engine.CreateTask{Title: "File integrity workflow", Snapshot: map[string]any{"next_action": "compute checksum"}})
	} else {
		fmt.Fprintln(os.Stderr, "The file integrity task is ambiguous")
		os.Exit(1)
	}
	must(err)
	turn, err := memory.Run(context.Background(), engine.Request{RequestID: *requestID, TaskID: task.ID, Query: "Calculate the SHA-256 of " + path, Budget: 2000, Recent: 4}, func(ctx context.Context, session *engine.Session) (engine.Reply, error) {
		value, err := session.Tool("file-hash", "sha256_file", map[string]any{"path": path}, func() (any, error) {
			data, err := os.ReadFile(path)
			if err != nil {
				return nil, err
			}
			return map[string]any{"path": path, "sha256": fmt.Sprintf("%x", sha256.Sum256(data)), "bytes": len(data)}, nil
		})
		if err != nil {
			return engine.Reply{}, err
		}
		encoded, err := json.Marshal(value)
		if err != nil {
			return engine.Reply{}, err
		}
		return engine.Reply{Text: string(encoded), Snapshot: map[string]any{"next_action": "review checksum", "file": path}}, nil
	})
	must(err)
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	must(encoder.Encode(turn))
}
func must(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
