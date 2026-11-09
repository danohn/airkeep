// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let held = false;
beforeEach(() => {
  vi.resetModules();
  sessionStorage.clear();
  held = false;
  Object.defineProperty(navigator, "locks", {
    configurable: true,
    value: {
      request: vi.fn(async (_name, _options, callback) => {
        if (held) return callback(null);
        held = true;
        try { return await callback({ name: "grant" }); }
        finally { held = false; }
      }),
    },
  });
});
afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

describe("cookie-free renewal", () => {
  it("serializes concurrent callers and persists the replacement refresh token", async () => {
    sessionStorage.setItem("airkeep.oauth.grant", JSON.stringify({ id: "one", refresh: "original" }));
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      access_token: "access", refresh_token: "replacement", grant_id: "one",
    }), { status: 200 }));
    vi.stubGlobal("fetch", fetcher);
    const auth = await import("./auth");
    expect(await Promise.all([auth.refresh(), auth.refresh()])).toEqual([true, true]);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0][1].credentials).toBe("omit");
    expect(auth.accessToken()).toBe("access");
    expect(JSON.parse(sessionStorage.getItem("airkeep.oauth.grant")!).refresh).toBe("replacement");
    auth.clearTokens();
  });

  it("clears a rejected grant", async () => {
    sessionStorage.setItem("airkeep.oauth.grant", JSON.stringify({ id: "one", refresh: "expired" }));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response('{"error":"invalid_grant"}', { status: 400 })));
    const auth = await import("./auth");
    expect(await auth.refresh()).toBe(false);
    expect(auth.accessToken()).toBe("");
    expect(sessionStorage.getItem("airkeep.oauth.grant")).toBeNull();
  });

  it("keeps stored credentials on a transport failure so retry can recover", async () => {
    sessionStorage.setItem("airkeep.oauth.grant", JSON.stringify({ id: "one", refresh: "original" }));
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Offline")));
    const auth = await import("./auth");
    await expect(auth.refresh()).rejects.toThrow("Offline");
    expect(JSON.parse(sessionStorage.getItem("airkeep.oauth.grant")!).refresh).toBe("original");
    auth.clearTokens();
  });
});

describe("display pairing sign-in return", () => {
  afterEach(() => history.replaceState(null, "", "/"));

  it("restores the QR code after the dashboard callback", async () => {
    history.replaceState(null, "", "/device#user_code=ABCD-EFGH");
    const auth = await import("./auth");
    const authorization = await auth.dashboardAuthorization();
    history.replaceState(null, "", "/auth/callback?code=example&state=" + authorization.state);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      access_token: "access", refresh_token: "refresh", grant_id: "display-owner",
    }))));
    expect(await auth.finishDashboardAuthorization()).toBe(true);
    expect(location.pathname).toBe("/device");
    expect(auth.devicePairingCode()).toBe("ABCD-EFGH");
    auth.clearTokens();
  });

  it("preserves manual entry on return from an OIDC error", async () => {
    history.replaceState(null, "", "/device");
    const auth = await import("./auth");
    await auth.dashboardAuthorization();
    history.replaceState(null, "", "/?sso_error=Denied");
    auth.restoreDevicePairingAfterSSOError();
    expect(location.pathname + location.search).toBe("/device?sso_error=Denied");
    expect(auth.devicePairingCode()).toBe("");
  });
});
