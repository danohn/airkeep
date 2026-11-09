export type Account = {
  id: string;
  username: string;
  role: "admin" | "member";
  disabled?: boolean;
  temporary_until?: number | null;
  local_password?: boolean;
};

type Tokens = { access_token: string; refresh_token: string; grant_id: string };
type Pending = { verifier: string; state: string; redirect: string; deviceCode?: string };
type StoredGrant = { refresh: string; id: string };

const grantKey = "airkeep.oauth.grant";
const pendingKey = "airkeep.oauth.pending";
let access = "";
let renewal: Promise<boolean> | undefined;
let releaseTab: (() => void) | undefined;

export function accessToken() {
  return access;
}

function storedGrant(): StoredGrant | null {
  const value = sessionStorage.getItem(grantKey);
  if (!value) return null;
  try {
    return JSON.parse(value) as StoredGrant;
  } catch {
    sessionStorage.removeItem(grantKey);
    return null;
  }
}

export function clearTokens() {
  access = "";
  sessionStorage.removeItem(grantKey);
  releaseTab?.();
  releaseTab = undefined;
}

function save(tokens: Tokens) {
  sessionStorage.setItem(grantKey, JSON.stringify({ refresh: tokens.refresh_token, id: tokens.grant_id }));
  access = tokens.access_token;
}

async function ownGrant(id: string) {
  if (releaseTab) return true;
  if (!navigator.locks) throw new Error("Use a browser that supports secure tab coordination.");
  // A duplicated tab may inherit sessionStorage. Only the tab holding this
  // lock may exchange the refresh credential, even between refresh requests.
  return new Promise<boolean>((resolve, reject) => {
    void navigator.locks.request("airkeep.oauth." + id, { ifAvailable: true }, (lock) => {
      if (!lock) {
        resolve(false);
        return;
      }
      return new Promise<void>((release) => {
        releaseTab = release;
        resolve(true);
      });
    }).catch(reject);
  });
}

async function token(form: Record<string, string>) {
  const response = await fetch("/auth/token", {
    method: "POST", credentials: "omit", cache: "no-store",
    body: new URLSearchParams({ client_id: "dashboard", ...form }),
  });
  if (response.status === 400) return null;
  if (!response.ok) throw new Error("Could not renew your Airkeep sign-in.");
  return await response.json() as Tokens;
}

export function refresh() {
  renewal ??= (async () => {
    const grant = storedGrant();
    if (!grant) return false;
    if (!await ownGrant(grant.id)) {
      clearTokens();
      return false;
    }
    const tokens = await token({ grant_type: "refresh_token", refresh_token: grant.refresh });
    if (storedGrant()?.refresh !== grant.refresh) return false;
    if (!tokens) {
      clearTokens();
      return false;
    }
    save(tokens);
    return true;
  })().finally(() => { renewal = undefined; });
  return renewal;
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
  sessionStorage.setItem(pendingKey, JSON.stringify(pending));
  return {
    client_id: "dashboard", redirect_uri: redirect, state, code_challenge: challenge,
    code_challenge_method: "S256", response_type: "code",
  };
}

let callback: Promise<boolean> | undefined;
export function finishDashboardAuthorization() {
  if (callback) return callback;
  if (location.pathname !== "/auth/callback") return refresh();
  callback = (async () => {
    const query = new URLSearchParams(location.search);
    const raw = sessionStorage.getItem(pendingKey);
    sessionStorage.removeItem(pendingKey);
    if (!raw) {
      history.replaceState(null, "", "/");
      throw new Error("Start sign-in again.");
    }
    const pending = JSON.parse(raw) as Pending;
    history.replaceState(null, "", pending.deviceCode === undefined ? "/" : devicePairingPath(pending.deviceCode));
    const code = query.get("code");
    if (!code || pending.state !== query.get("state")) throw new Error("Start sign-in again.");
    const tokens = await token({
      grant_type: "authorization_code", code, redirect_uri: pending.redirect, code_verifier: pending.verifier,
    });
    if (!tokens || !await ownGrant(tokens.grant_id)) throw new Error("Start sign-in again.");
    save(tokens);
    return true;
  })().finally(() => { callback = undefined; });
  return callback;
}
