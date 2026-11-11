// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

beforeEach(() => {
  vi.resetModules();
  sessionStorage.clear();
});
afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

describe("browser session", () => {
  it("discards the old refresh token and loads the session with browser cookies", async () => {
    sessionStorage.setItem("airkeep.oauth.grant", JSON.stringify({ id: "old", refresh: "legacy-token" }));
    const fetcher = vi.fn().mockResolvedValue(new Response('{"authenticated":true}'));
    vi.stubGlobal("fetch", fetcher);
    const auth = await import("./auth");
    const { api } = await import("./api");

    auth.clearLegacyBrowserCredentials();
    expect(sessionStorage.getItem("airkeep.oauth.grant")).toBeNull();
    expect(await auth.finishDashboardAuthorization()).toBe(false);
    expect(await api("/auth/session")).toEqual({ authenticated: true });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0][1].credentials).toBe("same-origin");
    expect(fetcher.mock.calls[0][1].headers).not.toHaveProperty("Authorization");
  });
});

describe("display pairing sign-in return", () => {
  afterEach(() => history.replaceState(null, "", "/"));

  it("restores the QR code after the dashboard callback", async () => {
    history.replaceState(null, "", "/device#user_code=ABCD-EFGH");
    const auth = await import("./auth");
    const authorization = await auth.dashboardAuthorization();
    history.replaceState(null, "", "/auth/callback?code=example&state=" + authorization.state);
    const fetcher = vi.fn().mockResolvedValue(new Response('{"authenticated":true}'));
    vi.stubGlobal("fetch", fetcher);
    expect(await auth.finishDashboardAuthorization()).toBe(true);
    expect(location.pathname).toBe("/device");
    expect(auth.devicePairingCode()).toBe("ABCD-EFGH");
    expect(fetcher).toHaveBeenCalledWith("/auth/token", expect.objectContaining({ credentials: "same-origin" }));
    expect(sessionStorage.length).toBe(0);
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

describe("hosted account deep links", () => {
  afterEach(() => history.replaceState(null, "", "/"));

  it.each(["/account", "/setup"])("returns to %s after browser sign-in", async (path) => {
    history.replaceState(null, "", path);
    const auth = await import("./auth");
    const authorization = await auth.dashboardAuthorization();
    history.replaceState(null, "", "/auth/callback?code=example&state=" + authorization.state);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response('{}')));
    expect(await auth.finishDashboardAuthorization()).toBe(true);
    expect(location.pathname).toBe(path);
  });
});
