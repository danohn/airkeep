export type Account = {
  id: string;
  username: string;
  role: "admin" | "member";
  disabled?: boolean;
  temporary_until?: number | null;
  local_password?: boolean;
};

type Pending = { verifier: string; state: string; redirect: string; deviceCode?: string; returnPath?: "/account" | "/setup" };

const pendingKey = "airkeep.oauth.pending";

export function clearLegacyBrowserCredentials() {
  sessionStorage.removeItem("airkeep.oauth.grant");
}

function base64url(data: Uint8Array) {
  return btoa(String.fromCharCode(...data)).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

export function devicePairingCode() {
  return (new URLSearchParams(location.hash.slice(1)).get("user_code") ?? "").slice(0, 16);
}

export function devicePairingPath(code: string, search = "") {
  return "/device" + search + (code ? "#" + new URLSearchParams({ user_code: code }) : "");
}

export function restoreDevicePairingAfterSSOError() {
  if (location.pathname !== "/" || !new URLSearchParams(location.search).has("sso_error")) return;
  const raw = sessionStorage.getItem(pendingKey);
  if (!raw) return;
  const pending: Pending = JSON.parse(raw);
  if (pending.deviceCode === undefined) return;
  sessionStorage.removeItem(pendingKey);
  history.replaceState(null, "", devicePairingPath(pending.deviceCode, location.search));
}

export async function dashboardAuthorization() {
  const verifier = base64url(crypto.getRandomValues(new Uint8Array(32)));
  const state = base64url(crypto.getRandomValues(new Uint8Array(32)));
  const challenge = base64url(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))));
  const redirect = location.origin + "/auth/callback";
  const pending: Pending = { verifier, state, redirect };
  if (location.pathname === "/device") pending.deviceCode = devicePairingCode();
  if (location.pathname === "/account" || location.pathname === "/setup") pending.returnPath = location.pathname;
  sessionStorage.setItem(pendingKey, JSON.stringify(pending));
  return {
    client_id: "dashboard", redirect_uri: redirect, state, code_challenge: challenge,
    code_challenge_method: "S256", response_type: "code",
  };
}

let callback: Promise<boolean> | undefined;
export function finishDashboardAuthorization() {
  if (callback) return callback;
  if (location.pathname !== "/auth/callback") return Promise.resolve(false);
  callback = (async () => {
    const query = new URLSearchParams(location.search);
    const raw = sessionStorage.getItem(pendingKey);
    sessionStorage.removeItem(pendingKey);
    if (!raw) {
      history.replaceState(null, "", "/");
      throw new Error("Start sign-in again.");
    }
    const pending = JSON.parse(raw) as Pending;
    const returnPath = pending.returnPath === "/account" || pending.returnPath === "/setup" ? pending.returnPath : "/";
    history.replaceState(null, "", pending.deviceCode === undefined ? returnPath : devicePairingPath(pending.deviceCode));
    const code = query.get("code");
    if (!code || pending.state !== query.get("state")) throw new Error("Start sign-in again.");
    const response = await fetch("/auth/token", {
      method: "POST", credentials: "same-origin", cache: "no-store",
      body: new URLSearchParams({
        client_id: "dashboard", grant_type: "authorization_code", code,
        redirect_uri: pending.redirect, code_verifier: pending.verifier,
      }),
    });
    if (!response.ok) throw new Error("Start sign-in again.");
    return true;
  })().finally(() => { callback = undefined; });
  return callback;
}
