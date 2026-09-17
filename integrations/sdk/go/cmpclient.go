// Package cmpclient is a dependency-free client for CMP's managed-turn JSON
// HTTP contract. It exposes explicit lifecycle calls; it does not intercept
// model or tool SDKs and it never retries a request after a transport error.
package cmpclient

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

const maxResponseBytes = 16 * 1024 * 1024

// ErrUnknownOutcome marks a transport failure after a mutating request may
// have reached CMP. The caller should inspect or reconcile before retrying.
var ErrUnknownOutcome = errors.New("CMP request outcome is unknown")

// Error is a protocol or server error. Unknown is true only for transport
// failures, where the server may have applied a mutating operation.
type Error struct {
	Code    string
	Message string
	Status  int
	Unknown bool
	Cause   error
}

func (e *Error) Error() string {
	if e.Cause != nil {
		return fmt.Sprintf("%s: %s: %v", e.Code, e.Message, e.Cause)
	}
	return fmt.Sprintf("%s: %s", e.Code, e.Message)
}

func (e *Error) Unwrap() error { return e.Cause }

// Options controls NewWithOptions. All fields are optional.
type Options struct {
	APIKey     string
	Path       string
	Timeout    time.Duration
	HTTPClient *http.Client
}

// Client sends one request per explicit managed-turn hook.
type Client struct {
	endpoint string
	apiKey   string
	timeout  time.Duration
	http     *http.Client
}

// New creates a client for /v1/managed-turn.
func New(baseURL string) (*Client, error) { return NewWithOptions(baseURL, Options{}) }

// NewWithOptions creates a client. HTTPS is required except for loopback HTTP,
// and credentials/query/fragment components are rejected from the URL.
func NewWithOptions(baseURL string, options Options) (*Client, error) {
	parsed, err := url.Parse(baseURL)
	if err != nil || parsed.Scheme == "" || parsed.Host == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return nil, fmt.Errorf("baseURL must be an HTTPS or loopback HTTP URL without credentials, query, or fragment")
	}
	loopback := parsed.Scheme == "http" && (parsed.Hostname() == "localhost" || parsed.Hostname() == "127.0.0.1" || parsed.Hostname() == "::1")
	if parsed.Scheme != "https" && !loopback {
		return nil, fmt.Errorf("baseURL must use HTTPS or loopback HTTP")
	}
	path := options.Path
	if path == "" {
		path = "/v1/managed-turn"
	}
	if !strings.HasPrefix(path, "/") || strings.ContainsAny(path, "?#") {
		return nil, fmt.Errorf("path must be absolute and contain no query or fragment")
	}
	parsed.Path = strings.TrimRight(parsed.Path, "/") + path
	parsed.RawPath = ""
	timeout := options.Timeout
	if timeout == 0 {
		timeout = 30 * time.Second
	}
	if timeout < 0 {
		return nil, fmt.Errorf("timeout must be positive")
	}
	httpClient := options.HTTPClient
	if httpClient == nil {
		httpClient = &http.Client{}
	}
	apiKey := options.APIKey
	if apiKey == "" {
		apiKey = os.Getenv("CMP_API_KEY")
	}
	return &Client{endpoint: parsed.String(), apiKey: apiKey, timeout: timeout, http: httpClient}, nil
}

// Endpoint returns the resolved POST URL, useful for diagnostics and tests.
func (c *Client) Endpoint() string { return c.endpoint }

type envelope struct {
	V          int            `json:"v"`
	Op         string         `json:"op"`
	RequestID  string         `json:"request_id"`
	Generation *int64         `json:"generation,omitempty"`
	CallID     string         `json:"call_id,omitempty"`
	Args       map[string]any `json:"args"`
}

type wireResponse struct {
	V      int             `json:"v"`
	OK     *bool           `json:"ok"`
	Result json.RawMessage `json:"result"`
	Error  *struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	} `json:"error"`
}

func strictJSON(value any) (string, error) {
	switch typed := value.(type) {
	case string:
		if !json.Valid([]byte(typed)) {
			return "", fmt.Errorf("value is not valid JSON")
		}
		return typed, nil
	case json.RawMessage:
		if !json.Valid(typed) {
			return "", fmt.Errorf("value is not valid JSON")
		}
		return string(typed), nil
	default:
		data, err := json.Marshal(value)
		return string(data), err
	}
}

func decodeResult(raw json.RawMessage) (any, error) {
	if len(raw) == 0 || bytes.Equal(raw, []byte("null")) {
		return nil, nil
	}
	var result any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&result); err != nil {
		return nil, fmt.Errorf("invalid result JSON: %w", err)
	}
	return result, nil
}

func (c *Client) call(ctx context.Context, op, requestID string, generation *int64, callID string,
	args map[string]any) (any, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	if strings.TrimSpace(op) == "" || strings.TrimSpace(requestID) == "" {
		return nil, fmt.Errorf("op and requestID must be nonempty")
	}
	if generation != nil && *generation < 1 {
		return nil, fmt.Errorf("generation must be positive")
	}
	if callID == "" && (op == "before_model" || op == "after_model" || op == "before_tool" || op == "after_tool") {
		return nil, fmt.Errorf("callID must be nonempty for %s", op)
	}
	body, err := json.Marshal(envelope{V: 1, Op: op, RequestID: requestID, Generation: generation, CallID: callID, Args: argsOrEmpty(args)})
	if err != nil {
		return nil, fmt.Errorf("request is not strict JSON: %w", err)
	}
	requestCtx, cancel := context.WithTimeout(ctx, c.timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(requestCtx, http.MethodPost, c.endpoint, bytes.NewReader(body))
	if err != nil {
		return nil, &Error{Code: "transport", Message: err.Error(), Unknown: true, Cause: ErrUnknownOutcome}
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	if c.apiKey != "" {
		req.Header.Set("Authorization", "Bearer "+c.apiKey)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, &Error{Code: "transport", Message: err.Error(), Unknown: true, Cause: ErrUnknownOutcome}
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxResponseBytes+1))
	if err != nil {
		return nil, &Error{Code: "transport", Message: err.Error(), Status: resp.StatusCode, Unknown: true, Cause: ErrUnknownOutcome}
	}
	if len(data) > maxResponseBytes {
		return nil, &Error{Code: "protocol", Message: "response exceeds 16 MiB", Status: resp.StatusCode}
	}
	var decoded wireResponse
	if err := json.Unmarshal(data, &decoded); err != nil {
		return nil, &Error{Code: "protocol", Message: fmt.Sprintf("response is not JSON: %v", err), Status: resp.StatusCode}
	}
	if decoded.V != 1 {
		return nil, &Error{Code: "protocol", Message: "response must be a v=1 object", Status: resp.StatusCode}
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 || decoded.OK == nil || !*decoded.OK {
		code, message := "server_error", resp.Status
		if decoded.Error != nil {
			if decoded.Error.Code != "" {
				code = decoded.Error.Code
			}
			if decoded.Error.Message != "" {
				message = decoded.Error.Message
			}
		}
		return nil, &Error{Code: code, Message: message, Status: resp.StatusCode}
	}
	return decodeResult(decoded.Result)
}

func argsOrEmpty(args map[string]any) map[string]any {
	if args == nil {
		return map[string]any{}
	}
	return args
}

// BeginOptions controls Begin. Zero values select the documented defaults.
type BeginOptions struct {
	System, ModelKey string
	Budget, Reserve  int
	Scope            string
	RetrievalLimit   int
	Recent           int
	Counting         string
}

func normalizeBegin(options BeginOptions) (BeginOptions, error) {
	if options.Budget == 0 {
		options.Budget = 2000
	}
	if options.RetrievalLimit == 0 {
		options.RetrievalLimit = 24
	}
	if options.Recent == 0 {
		options.Recent = 4
	}
	if options.Scope == "" {
		options.Scope = "lineage"
	}
	if options.Counting == "" {
		options.Counting = "estimated-json"
	}
	if options.Budget < 1 || options.Reserve < 0 || options.RetrievalLimit < 0 || options.Recent < 0 || options.Budget <= options.Reserve {
		return options, fmt.Errorf("invalid begin budget or limits")
	}
	return options, nil
}

func (c *Client) Begin(ctx context.Context, requestID string, taskID int64, query string, options BeginOptions) (any, error) {
	if taskID < 1 || strings.TrimSpace(query) == "" {
		return nil, fmt.Errorf("taskID must be positive and query nonempty")
	}
	options, err := normalizeBegin(options)
	if err != nil {
		return nil, err
	}
	return c.call(ctx, "begin", requestID, nil, "", map[string]any{
		"task_id": taskID, "query": query, "system": options.System, "model_key": options.ModelKey,
		"budget": options.Budget, "reserve": options.Reserve, "scope": options.Scope,
		"retrieval_limit": options.RetrievalLimit, "recent": options.Recent, "counting": options.Counting,
	})
}

func (c *Client) Inspect(ctx context.Context, requestID string) (any, error) {
	return c.call(ctx, "inspect", requestID, nil, "", nil)
}

func (c *Client) Recover(ctx context.Context, requestID string, generation int64) (any, error) {
	return c.call(ctx, "recover", requestID, &generation, "", nil)
}

func (c *Client) BeforeModel(ctx context.Context, requestID string, generation int64, callID string,
	payload any, units int, counting string) (any, error) {
	if units < 0 || strings.TrimSpace(counting) == "" {
		return nil, fmt.Errorf("units must be nonnegative and counting nonempty")
	}
	payloadJSON, err := strictJSON(payload)
	if err != nil {
		return nil, err
	}
	return c.call(ctx, "before_model", requestID, &generation, callID,
		map[string]any{"payload_json": payloadJSON, "units": units, "counting": counting})
}

// AfterModel records one confirmed response or one known provider error. Pass
// nil for the unused value; provider JSON null is not a useful response/error.
func (c *Client) AfterModel(ctx context.Context, requestID string, generation int64, callID string,
	response any, providerError any) (any, error) {
	if (response == nil) == (providerError == nil) {
		return nil, fmt.Errorf("exactly one of response or providerError is required")
	}
	args := map[string]any{}
	if response != nil {
		encoded, err := strictJSON(response)
		if err != nil {
			return nil, err
		}
		args["response_json"] = encoded
	} else {
		args["error"] = providerError
	}
	return c.call(ctx, "after_model", requestID, &generation, callID, args)
}

func (c *Client) BeforeTool(ctx context.Context, requestID string, generation int64, callID, name string,
	arguments any, ttlMS int64) (any, error) {
	if strings.TrimSpace(name) == "" {
		return nil, fmt.Errorf("name must be nonempty")
	}
	if ttlMS == 0 {
		ttlMS = 5000
	}
	if ttlMS < 1 {
		return nil, fmt.Errorf("ttlMS must be positive")
	}
	return c.call(ctx, "before_tool", requestID, &generation, callID,
		map[string]any{"name": name, "arguments": arguments, "ttl_ms": ttlMS})
}

// AfterTool records one confirmed result or one known tool error. A process
// crash/timeout before this call leaves the tool outcome indeterminate.
func (c *Client) AfterTool(ctx context.Context, requestID string, generation int64, callID string,
	result any, toolError any, leaseToken string) (any, error) {
	if (result == nil) == (toolError == nil) {
		return nil, fmt.Errorf("exactly one of result or toolError is required")
	}
	args := map[string]any{}
	if result != nil {
		args["result"] = result
	} else {
		args["error"] = toolError
	}
	if leaseToken != "" {
		args["lease_token"] = leaseToken
	}
	return c.call(ctx, "after_tool", requestID, &generation, callID, args)
}

func (c *Client) Commit(ctx context.Context, requestID string, generation int64, reply map[string]any) (any, error) {
	if reply == nil {
		return nil, fmt.Errorf("reply must be a JSON object")
	}
	return c.call(ctx, "commit", requestID, &generation, "", map[string]any{"reply": reply})
}

func (c *Client) Abort(ctx context.Context, requestID string, generation int64) (any, error) {
	return c.call(ctx, "abort", requestID, &generation, "", nil)
}
