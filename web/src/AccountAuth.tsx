import { useEffect, useState } from "react";
import { api } from "./api";
import { dashboardAuthorization, type Account } from "./auth";
import { OIDCSettings } from "./OIDCSettings";

export async function beginDashboardSignIn() {
  const fields = await dashboardAuthorization();
  const result = await api<{ transaction: string }>("/auth/authorize", {
    method: "POST", body: JSON.stringify(fields),
  });
  return result.transaction;
}

export async function startOIDC(transaction: string, invitation?: string) {
  const result = await api<{ redirect: string }>("/auth/oidc/start", {
    method: "POST", body: JSON.stringify({ transaction, invitation }),
  });
  const destination = new URL(result.redirect);
  if (destination.protocol !== "https:") throw new Error("Invalid sign-in destination.");
  location.assign(destination.href);
}

export function AccountLogin({ native = false, invite = false, configured = true, error = "", oidcLabel }: {
  native?: boolean; invite?: boolean; configured?: boolean; error?: string; oidcLabel?: string;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [transaction, setTransaction] = useState(() => native ? new URLSearchParams(location.search).get("transaction") ?? "" : "");
  const [change, setChange] = useState(false);
  const [invitation] = useState(() => invite ? location.hash.slice(1) : "");
  const [provider, setProvider] = useState("");
  const [message, setMessage] = useState(() => new URLSearchParams(location.search).get("sso_error") ?? "");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (native || invite || new URLSearchParams(location.search).has("sso_error")) {
      history.replaceState(null, "", location.pathname + (location.pathname === "/device" ? location.hash : ""));
    }
    if (!native && !invite) return;
    let active = true;
    api<{ oidc_label?: string }>("/auth/session").then((status) => {
      if (active) setProvider(status.oidc_label ?? "");
    }).catch(() => { if (active) setMessage("Could not check single sign-on availability."); });
    return () => { active = false; };
  }, [native, invite]);
  async function authorization() {
    if (transaction) return transaction;
    if (native) throw new Error("Start sign-in again from the Airkeep app.");
    return beginDashboardSignIn();
  }
  async function signInWithOIDC() {
    setBusy(true);
    setMessage("");
    try {
      await startOIDC(await authorization(), invite ? invitation : undefined);
    } catch (e) {
      setMessage((e as Error).message);
      setBusy(false);
    }
  }
  return <div className="login"><form onSubmit={async (event) => {
    event.preventDefault();
    setBusy(true);
    setMessage("");
    try {
      const id = await authorization();
      const result = await api<{ passwordChangeRequired?: boolean; redirect?: string }>("/auth/credentials", {
        method: "POST", body: JSON.stringify({ transaction: id, username, password, ...(change ? { new_password: newPassword } : {}) }),
      });
      if (result.passwordChangeRequired) {
        setTransaction(id);
        setChange(true);
      } else if (result.redirect) {
        const callback = new URL(result.redirect);
        const valid = native
          ? callback.protocol === "io.airkeep.oauth:" && callback.pathname === "/callback" && !callback.host
          : callback.origin === location.origin && callback.pathname === "/auth/callback";
        if (!valid) throw new Error("Invalid sign-in callback.");
        location.assign(result.redirect);
      }
    } catch (e) {
      setMessage((e as Error).message);
    } finally {
      setBusy(false);
    }
  }}>
    <img src="/brand/mark.svg" alt="" />
    <h1>{invite ? "Airkeep invitation" : native ? "Connect to Airkeep" : "Airkeep"}</h1>
    <p>{invite ? "Use single sign-on to accept your invitation." : change ? "Choose your own password." : "Sign in to your relay."}</p>
    {invite ? <a href="/">Go to Airkeep sign-in</a> : configured ? <>
      <label htmlFor="username">Username</label>
      <input id="username" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required />
      <label htmlFor="password">{change ? "Temporary password" : "Password"}</label>
      <input id="password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
      {change && <>
        <label htmlFor="new-password">New password</label>
        <input id="new-password" type="password" autoComplete="new-password" minLength={12} value={newPassword} onChange={(e) => setNewPassword(e.target.value)} required />
      </>}
      <button className="primary" disabled={busy}>{change ? "Save password and sign in" : "Sign in"}</button>
    </> : <div className="notice">Set up an administrator on your relay to get started.</div>}
    {(oidcLabel || provider) && !change && (!invite || invitation) &&
      <button type="button" disabled={busy} onClick={() => void signInWithOIDC()}>
        Sign in with {oidcLabel || provider}
      </button>}
    {invite && !invitation && <p>Open the original invitation link to try again.</p>}
    {(message || error) && <p className="error" role="alert">{message || error}</p>}
  </form></div>;
}

type Connection = { id: string; client: string; expires: number; current: boolean; display_suffix?: string };
const clientLabels: Record<string, string> = {
  dashboard: "Web dashboard", "apple-ios": "iPhone or iPad", "apple-mac": "Mac", watch: "Apple Watch", esp32: "ESP32 display",
};

export function AccountSettings({ admin, signedOut, oidcLabel, localPassword = true }: {
  admin: boolean; signedOut: () => void; oidcLabel?: string; localPassword?: boolean;
}) {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [replacement, setReplacement] = useState("");
  const [temporary, setTemporary] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    setTemporary("");
    try { await action(); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  async function load() { setAccounts(await api<Account[]>("/v2/admin/accounts")); }
  useEffect(() => {
    let active = true;
    const request = admin
      ? api<Account[]>("/v2/admin/accounts").then((items) => { if (active) setAccounts(items); })
      : api<Connection[]>("/auth/grants").then((items) => { if (active) setConnections(items); });
    request.catch((error: Error) => { if (active) setError(error.message); });
    return () => { active = false; };
  }, [admin]);
  return <section className="account-settings">
    <h2>{admin ? "Accounts" : localPassword ? "Change password" : "My account"}</h2>
    {admin ? <>
      <form onSubmit={(e) => {
        e.preventDefault();
        void run(async () => {
          const result = await api<{ temporaryPassword: string }>("/v2/admin/accounts", {
            method: "POST", body: JSON.stringify({ username }),
          });
          setUsername("");
          setTemporary(result.temporaryPassword);
          await load();
        });
      }}>
        <label htmlFor="new-user">Username</label>
        <input id="new-user" value={username} onChange={(e) => setUsername(e.target.value)} required />
        <button className="primary" disabled={busy}>Create account</button>
      </form>
      <button disabled={busy} onClick={() => void run(load)}>Refresh accounts</button>
      {accounts.map((account) => <p key={account.id}>
        {account.username} · {account.role}{account.disabled ? " · Disabled" : ""}{" "}
        <button disabled={busy} onClick={() => void run(async () => {
          await api("/v2/admin/accounts/" + account.id, {
            method: "PATCH", body: JSON.stringify({ disabled: !account.disabled }),
          });
          await load();
        })}>{account.disabled ? "Enable" : "Disable"}</button>{" "}
        <button disabled={busy} onClick={() => void run(async () => {
          await api("/v2/admin/accounts/" + account.id, {
            method: "PATCH", body: JSON.stringify({ role: account.role === "admin" ? "member" : "admin" }),
          });
          await load();
        })}>{account.role === "admin" ? "Make member" : "Make admin"}</button>{" "}
        {account.local_password !== false && <button disabled={busy} onClick={() => void run(async () => {
          const result = await api<{ temporaryPassword: string }>("/v2/admin/accounts/" + account.id + "/reset", { method: "POST" });
          setTemporary(result.temporaryPassword);
        })}>Reset password</button>}
      </p>)}
      {temporary && <p role="status">Temporary password (expires in 24 hours) <code>{temporary}</code></p>}
      {oidcLabel && <OIDCSettings accounts={accounts} />}
    </> : <>
    {localPassword && <form onSubmit={(e) => {
      e.preventDefault();
      void run(async () => {
        await api("/auth/password", { method: "POST", body: JSON.stringify({ password, new_password: replacement }) });
        signedOut();
      });
    }}>
      <label htmlFor="current-account-password">Current password</label>
      <input id="current-account-password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
      <label htmlFor="next-account-password">New password</label>
      <input id="next-account-password" type="password" autoComplete="new-password" minLength={12} value={replacement} onChange={(e) => setReplacement(e.target.value)} required />
      <button className="primary" disabled={busy}>Save password</button>
    </form>}
    <h2>Connected apps</h2>
    {connections.map((connection) => <p key={connection.id}>
      {clientLabels[connection.client] ?? connection.client}{connection.display_suffix ? " · " + connection.display_suffix : ""}{connection.current ? " · This tab" : ""}{" "}
      <button disabled={busy} onClick={() => void run(async () => {
        await api("/auth/grants/" + connection.id, { method: "DELETE" });
        if (connection.current) signedOut();
        else setConnections(await api<Connection[]>("/auth/grants"));
      })}>Sign out</button>
    </p>)}
    </>}
    {error && <p className="error" role="alert">{error}</p>}
  </section>;
}
