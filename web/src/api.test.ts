import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, setCsrf, amsName, age } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
  setCsrf("");
});
describe("browser API boundary", () => {
  it("keeps credentials in cookies and sends CSRF only in its header", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ ok: true }), { status: 200 }),
      );
    vi.stubGlobal("fetch", fetch);
    setCsrf("csrf-secret");
    await api("/v2/alerts", { method: "POST", body: "{}" });
    const [path, options] = fetch.mock.calls[0];
    expect(path).toBe("/v2/alerts");
    expect(options.credentials).toBe("same-origin");
    expect(options.cache).toBe("no-store");
    expect(options.headers["X-Airkeep-CSRF"]).toBe("csrf-secret");
    expect(options.headers.Authorization).toBeUndefined();
  });
  it("handles empty deletes and preserves useful conflict errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(null, { status: 204 })),
    );
    expect(await api("/v2/alerts/rule", { method: "DELETE" })).toBeUndefined();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("{}", { status: 412 })),
    );
    await expect(api("/v2/alerts/rule")).rejects.toMatchObject({
      status: 412,
      message: expect.stringContaining("Reload"),
    });
  });
  it("expires authenticated UI state without expiring it for a wrong login password", async () => {
    const dispatchEvent = vi.fn();
    vi.stubGlobal("window", { dispatchEvent });
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockImplementation(() =>
          Promise.resolve(new Response("{}", { status: 401 })),
        ),
    );
    await expect(api("/auth/login")).rejects.toBeInstanceOf(ApiError);
    expect(dispatchEvent).not.toHaveBeenCalled();
    await expect(api("/v2/alerts")).rejects.toBeInstanceOf(ApiError);
    expect(dispatchEvent).toHaveBeenCalledOnce();
  });
  it("ignores an old session failure after a successful re-login", async () => {
    const dispatchEvent = vi.fn();
    vi.stubGlobal("window", { dispatchEvent });
    let finish!: (response: Response) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            finish = resolve;
          }),
      ),
    );
    setCsrf("old-session");
    const pending = api("/v2/alerts");
    setCsrf("new-session");
    finish(new Response("{}", { status: 401 }));
    await expect(pending).rejects.toBeInstanceOf(ApiError);
    expect(dispatchEvent).not.toHaveBeenCalled();
  });
  it("keeps network failures separate from session expiry", async () => {
    const dispatchEvent = vi.fn();
    vi.stubGlobal("window", { dispatchEvent });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new TypeError("Failed to fetch")),
    );
    await expect(api("/v2/alerts")).rejects.toMatchObject({
      status: 0,
      message: expect.stringContaining("Reconnect"),
    });
    expect(dispatchEvent).not.toHaveBeenCalled();
  });
  it("uses source observation age and one-based AMS labels", () => {
    expect(amsName("0")).toBe("AMS 1");
    expect(amsName("2")).toBe("AMS 3");
    expect(age(1000, 1120)).toBe("2 min. ago");
    expect(age(1000, 5000)).toBe("1 hr. ago");
  });
});
