package cmpclient

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestLifecycleContractAndExactPayload(t *testing.T) {
	var operations []string
	var payload string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer r.Body.Close()
		var request struct {
			Op   string         `json:"op"`
			Args map[string]any `json:"args"`
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Fatalf("decode request: %v", err)
		}
		operations = append(operations, request.Op)
		if request.Op == "before_model" {
			payload, _ = request.Args["payload_json"].(string)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"v":1,"ok":true,"result":{"accepted":true}}`))
	}))
	defer server.Close()
	client, err := NewWithOptions(server.URL, Options{APIKey: "secret"})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if _, err = client.Begin(ctx, "r1", 1, "hello", BeginOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, err = client.BeforeModel(ctx, "r1", 1, "m1", ` { "messages": [] } `, 4, "bytes"); err != nil {
		t.Fatal(err)
	}
	if _, err = client.AfterModel(ctx, "r1", 1, "m1", map[string]any{"id": "r"}, nil); err != nil {
		t.Fatal(err)
	}
	if _, err = client.BeforeTool(ctx, "r1", 1, "t1", "write", map[string]any{"x": 1}, 5000); err != nil {
		t.Fatal(err)
	}
	if _, err = client.AfterTool(ctx, "r1", 1, "t1", map[string]any{"ok": true}, nil, "lease"); err != nil {
		t.Fatal(err)
	}
	if _, err = client.Commit(ctx, "r1", 1, map[string]any{"text": "done"}); err != nil {
		t.Fatal(err)
	}
	want := []string{"begin", "before_model", "after_model", "before_tool", "after_tool", "commit"}
	if strings.Join(operations, ",") != strings.Join(want, ",") {
		t.Fatalf("operations = %v, want %v", operations, want)
	}
	if payload != ` { "messages": [] } ` {
		t.Fatalf("payload was not preserved: %q", payload)
	}
}

func TestServerErrorAndURLValidation(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"v":1,"ok":false,"error":{"code":"fenced","message":"stale"}}`))
	}))
	defer server.Close()
	client, err := New(server.URL)
	if err != nil {
		t.Fatal(err)
	}
	_, err = client.Inspect(context.Background(), "r1")
	if err == nil || !strings.Contains(err.Error(), "fenced") {
		t.Fatalf("error = %v, want fenced", err)
	}
	if _, err = New("http://example.com"); err == nil {
		t.Fatal("non-loopback HTTP URL was accepted")
	}
}
