import { useEffect, useState } from "react";
import { api, setOAuth } from "./api";
import { dashboardAuthorization, type Account } from "./auth";

export function AccountLogin({ native = false, configured = true, error = "" }: {
  native?: boolean; configured?: boolean; error?: string;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [transaction, setTransaction] = useState(() => native ? new URLSearchParams(location.search).get("transaction") ?? "" : "");
  const [change, setChange] = useState(false);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (native && location.search) history.replaceState(null, "", "/connect"); }, [native]);
  return <div className="login"><form onSubmit={async (event) => {
    event.preventDefault();
    setBusy(true);
    setMessage("");
    try {
      setOAuth(true);
      let id = transaction;
      if (!id) {
        if (native) throw new Error("Start sign-in again from the Airkeep app.");
        const fields = await dashboardAuthorization();
        id = (await api<{ transaction: string }>("/auth/authorize", { method: "POST", body: JSON.stringify(fields) })).transaction;
      }
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
    <h1>{native ? "Connect to Airkeep" : "Airkeep"}</h1>
    <p>{change ? "Choose your own password." : "Sign in to your relay."}</p>
    {configured ? <>
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
    {(message || error) && <p className="error" role="alert">{message || error}</p>}
  </form></div>;
}

type Connection = { id: string; client: string; expires: number; current: boolean };
const clientLabels: Record<string, string> = {
  dashboard: "Web dashboard", "apple-ios": "iPhone or iPad", "apple-mac": "Mac", watch: "Apple Watch",
};

export function AccountSettings({ admin, signedOut }: { admin: boolean; signedOut: () => void }) {
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
    <h2>{admin ? "Accounts" : "Change password"}</h2>
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
        <button disabled={busy} onClick={() => void run(async () => {
          const result = await api<{ temporaryPassword: string }>("/v2/admin/accounts/" + account.id + "/reset", { method: "POST" });
          setTemporary(result.temporaryPassword);
        })}>Reset password</button>
      </p>)}
      {temporary && <p role="status">Temporary password (expires in 24 hours) <code>{temporary}</code></p>}
    </> : <>
    <form onSubmit={(e) => {
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
    </form>
    <h2>Connected apps</h2>
    {connections.map((connection) => <p key={connection.id}>
      {clientLabels[connection.client] ?? connection.client}{connection.current ? " · This tab" : ""}{" "}
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
