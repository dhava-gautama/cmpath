/**
 * Dependency-free CMP managed-turn client.
 *
 * This is a transport client, not an agent wrapper. The host must explicitly
 * install before-model, after-model, before-tool, after-tool, and commit hooks.
 * A fetch failure is an unknown outcome and is never retried here.
 */

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type FetchLike = (input: string | URL, init?: RequestInit) => Promise<Response>;

export interface CMPClientOptions {
  baseUrl: string;
  apiKey?: string;
  timeoutMs?: number;
  path?: string;
  /** Injectable for contract tests; production uses globalThis.fetch. */
  fetchImpl?: FetchLike;
}

export class CMPError extends Error {
  readonly code: string;
  readonly status?: number;
  /** True when the server may have applied a mutating request. */
  readonly uncertain: boolean;

  constructor(code: string, message: string, status?: number, uncertain = false) {
    super(`${code}: ${message}`);
    this.name = "CMPError";
    this.code = code;
    this.status = status;
    this.uncertain = uncertain;
  }
}

function text(value: unknown, name: string): string {
  if (typeof value !== "string" || value.trim() === "") throw new TypeError(`${name} must be nonempty text`);
  return value;
}

function wire(value: JsonValue | string): string {
  if (typeof value === "string") {
    JSON.parse(value); // preserve caller whitespace and key order after validation
    return value;
  }
  return JSON.stringify(value);
}

function integer(value: number, name: string, positive = false): void {
  if (!Number.isInteger(value) || (positive ? value < 1 : value < 0)) {
    throw new TypeError(`${name} must be a ${positive ? "positive" : "nonnegative"} integer`);
  }
}

export class CMPClient {
  private readonly url: string;
  private readonly apiKey?: string;
  private readonly timeoutMs: number;
  private readonly fetchImpl: FetchLike;

  constructor(options: CMPClientOptions) {
    const path = options.path ?? "/v1/managed-turn";
    if (!path.startsWith("/") || path.includes("?") || path.includes("#")) {
      throw new TypeError("path must be an absolute path without query or fragment");
    }
    const parsed = new URL(text(options.baseUrl, "baseUrl"));
    const loopback = parsed.protocol === "http:" && ["localhost", "127.0.0.1", "::1"].includes(parsed.hostname);
    if (!parsed.hostname || (parsed.protocol !== "https:" && !loopback)) {
      throw new TypeError("baseUrl must use HTTPS or loopback HTTP");
    }
    if (parsed.username || parsed.password || parsed.search || parsed.hash) {
      throw new TypeError("baseUrl cannot contain credentials, query, or fragment");
    }
    parsed.pathname = parsed.pathname.replace(/\/+$/, "") + path;
    this.url = parsed.toString();
    const runtime = globalThis as typeof globalThis & { process?: { env?: Record<string, string | undefined> } };
    this.apiKey = options.apiKey ?? runtime.process?.env?.CMP_API_KEY;
    this.timeoutMs = options.timeoutMs ?? 30_000;
    if (!Number.isFinite(this.timeoutMs) || this.timeoutMs <= 0) throw new TypeError("timeoutMs must be positive");
    this.fetchImpl = options.fetchImpl ?? globalThis.fetch?.bind(globalThis);
    if (!this.fetchImpl) throw new TypeError("global fetch is unavailable; pass fetchImpl");
  }

  private async call(op: string, requestId: string, args: Record<string, unknown> = {}, generation?: number,
                     callId?: string): Promise<unknown> {
    text(op, "op");
    text(requestId, "requestId");
    if (generation !== undefined) integer(generation, "generation", true);
    if (callId !== undefined) text(callId, "callId");
    const envelope: Record<string, unknown> = { v: 1, op, request_id: requestId, args };
    if (generation !== undefined) envelope.generation = generation;
    if (callId !== undefined) envelope.call_id = callId;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    const headers: Record<string, string> = { "content-type": "application/json", accept: "application/json" };
    if (this.apiKey) headers.authorization = `Bearer ${this.apiKey}`;
    let response: Response;
    let bodyText: string;
    try {
      response = await this.fetchImpl(this.url, {
        method: "POST", headers, body: JSON.stringify(envelope), signal: controller.signal,
      });
      bodyText = await response.text();
    } catch (error) {
      throw new CMPError("transport", error instanceof Error ? error.message : String(error), undefined, true);
    } finally {
      clearTimeout(timer);
    }
    let body: any;
    try {
      body = JSON.parse(bodyText);
    } catch (error) {
      throw new CMPError("protocol", `response is not JSON: ${error instanceof Error ? error.message : String(error)}`,
                         response.status);
    }
    if (!body || body.v !== 1) throw new CMPError("protocol", "response must be a v=1 object", response.status);
    if (!response.ok || body.ok === false) {
      const remote = body.error && typeof body.error === "object" ? body.error : {};
      throw new CMPError(String(remote.code ?? "server_error"), String(remote.message ?? `HTTP ${response.status}`), response.status);
    }
    if (body.ok !== true) throw new CMPError("protocol", "success response must set ok=true", response.status);
    return body.result;
  }

  begin(requestId: string, taskId: number, query: string, options: {
    system?: string; modelKey?: string; budget?: number; reserve?: number; scope?: string;
    retrievalLimit?: number; recent?: number; counting?: string;
  } = {}): Promise<unknown> {
    integer(taskId, "taskId", true); text(query, "query");
    const budget = options.budget ?? 2000, reserve = options.reserve ?? 0;
    const retrievalLimit = options.retrievalLimit ?? 24, recent = options.recent ?? 4;
    integer(budget, "budget"); integer(reserve, "reserve"); integer(retrievalLimit, "retrievalLimit"); integer(recent, "recent");
    if (budget <= reserve) throw new TypeError("budget must exceed reserve");
    const args = {
      task_id: taskId, query, system: options.system ?? "", model_key: options.modelKey ?? "",
      budget, reserve, scope: options.scope ?? "lineage", retrieval_limit: retrievalLimit,
      recent, counting: options.counting ?? "estimated-json",
    };
    return this.call("begin", requestId, args);
  }

  inspect(requestId: string): Promise<unknown> { return this.call("inspect", requestId); }

  recover(requestId: string, generation: number): Promise<unknown> {
    return this.call("recover", requestId, {}, generation);
  }

  beforeModel(requestId: string, generation: number, callId: string, payload: JsonValue | string,
             units: number, counting: string): Promise<unknown> {
    integer(units, "units");
    return this.call("before_model", requestId, { payload_json: wire(payload), units, counting: text(counting, "counting") }, generation, callId);
  }

  afterModel(requestId: string, generation: number, callId: string,
            outcome: { response: JsonValue | string } | { error: JsonValue }): Promise<unknown> {
    const args = "response" in outcome ? { response_json: wire(outcome.response) } : { error: outcome.error };
    return this.call("after_model", requestId, args, generation, callId);
  }

  beforeTool(requestId: string, generation: number, callId: string, name: string,
            argumentsValue: JsonValue, ttlMs = 5000): Promise<unknown> {
    integer(ttlMs, "ttlMs", true);
    return this.call("before_tool", requestId, { name: text(name, "name"), arguments: argumentsValue, ttl_ms: ttlMs }, generation, callId);
  }

  afterTool(requestId: string, generation: number, callId: string,
           outcome: { result: JsonValue } | { error: JsonValue }, leaseToken?: string): Promise<unknown> {
    const args: Record<string, unknown> = "result" in outcome ? { result: outcome.result } : { error: outcome.error };
    if (leaseToken !== undefined) args.lease_token = text(leaseToken, "leaseToken");
    return this.call("after_tool", requestId, args, generation, callId);
  }

  commit(requestId: string, generation: number, reply: Record<string, JsonValue>): Promise<unknown> {
    return this.call("commit", requestId, { reply }, generation);
  }

  abort(requestId: string, generation: number): Promise<unknown> {
    return this.call("abort", requestId, {}, generation);
  }
}

export default CMPClient;
