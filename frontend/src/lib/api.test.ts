import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, AUTH_EXPIRED_EVENT, setCsrfToken } from "./api";
import { sessionFor } from "../test/fixtures";

beforeEach(() => setCsrfToken(null));
afterEach(() => { vi.unstubAllGlobals(); setCsrfToken(null); });

describe("HTTP account and job contracts", () => {
  it("sends the CSRF token and stable intent key without an engine field", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(sessionFor([])), { status: 202 }));
    vi.stubGlobal("fetch", fetch);
    setCsrfToken("csrf-token");
    await api.match("Review code", "claude-code", "intent-key", "session-id");
    const [url, options] = fetch.mock.calls[0];
    expect(url).toBe("/api/matches");
    expect(options.credentials).toBe("same-origin");
    expect(new Headers(options.headers).get("X-CSRF-Token")).toBe("csrf-token");
    expect(new Headers(options.headers).get("Idempotency-Key")).toBe("intent-key");
    expect(JSON.parse(options.body)).toEqual({ task: "Review code", agent: "claude-code", session_id: "session-id" });
  });

  it("does not send an unauthenticated mutation and still allows public reads", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ skills: [], categories: [], total: 0, has_more: false })));
    vi.stubGlobal("fetch", fetch);
    await expect(api.match("Review code", "codex", "key")).rejects.toMatchObject({ status: 401 });
    expect(fetch).not.toHaveBeenCalled();
    expect(await api.catalog()).toMatchObject({ total: 0 });
  });

  it("accepts an empty logout response and addresses cancellations by job", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(sessionFor([], { status: "cancelled" }))));
    vi.stubGlobal("fetch", fetch);
    setCsrfToken("csrf-token");
    await expect(api.logout()).resolves.toBeUndefined();
    await api.cancel("job-123");
    expect(fetch.mock.calls.map(([url]) => url)).toEqual(["/api/auth/logout", "/api/jobs/job-123/cancel"]);
  });

  it("notifies the UI on 401 and clears credentials before another write", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "Sign in." }), { status: 401 }));
    vi.stubGlobal("fetch", fetch);
    setCsrfToken("expired-token");
    const expired = vi.fn();
    window.addEventListener(AUTH_EXPIRED_EVENT, expired);
    try {
      await expect(api.profile()).rejects.toBeInstanceOf(ApiError);
      expect(expired).toHaveBeenCalledOnce();
      await expect(api.save("skill")).rejects.toMatchObject({ status: 401 });
      expect(fetch).toHaveBeenCalledOnce();
    } finally {
      window.removeEventListener(AUTH_EXPIRED_EVENT, expired);
    }
  });
});
