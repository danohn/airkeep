import { useEffect, useState } from "react";
import { api } from "./api";

export const hostedAccountPaths = ["/signup", "/signup/complete", "/forgot-password", "/reset-password"];

export function HostedAccountFlow() {
  const reset = location.pathname === "/forgot-password" || location.pathname === "/reset-password";
  const completion = location.pathname === "/signup/complete" || location.pathname === "/reset-password";
  const [token] = useState(() => new URLSearchParams(location.hash.slice(1)).get("token") ?? "");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [sent, setSent] = useState(false);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // Keep one-use secrets out of the address bar and subsequent navigation.
  useEffect(() => {
    if (location.hash) history.replaceState(null, "", location.pathname);
  }, []);
  async function submit() {
    setBusy(true);
    setError("");
    try {
      if (completion) {
        if (password !== confirm) throw new Error("The passwords do not match.");
        await api(reset ? "/auth/password/reset" : "/auth/signup/complete", {
          method: "POST", body: JSON.stringify({ token, password }),
        });
        setPassword("");
        setConfirm("");
        setDone(true);
      } else {
        await api(reset ? "/auth/password/forgot" : sent ? "/auth/signup/resend" : "/auth/signup", {
          method: "POST", body: JSON.stringify({ email }),
        });
        setSent(true);
      }
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <main className="login"><form onSubmit={(event) => { event.preventDefault(); void submit(); }}>
    <img src="/brand/mark.svg" alt="" />
    <h1>{done ? "Your password is set" : completion ? "Set your password" : reset ? "Reset your password" : "Create your Airkeep account"}</h1>
    {done ? <><p>Sign in to connect your printer and view your AMS.</p><a className="primary" href="/">Sign in</a></> : completion ? <>
      {token ? <>
        <p>{reset ? "Choose a new password. Your connected apps will be signed out." : "Your email link verifies your address when you set your password."}</p>
        <label htmlFor="hosted-password">New password</label>
        <input id="hosted-password" type="password" autoComplete="new-password" minLength={12} maxLength={1024}
          required value={password} onChange={(e) => setPassword(e.target.value)} />
        <label htmlFor="hosted-confirm-password">Confirm password</label>
        <input id="hosted-confirm-password" type="password" autoComplete="new-password" minLength={12} maxLength={1024}
          required value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        <button className="primary" disabled={busy}>Set password</button>
      </> : <p>Open the link in your email to set your password.</p>}
      <a href={reset ? "/forgot-password" : "/signup"}>Request a fresh email link</a>
    </> : <>
      <p>{sent ? "If this email address can receive an Airkeep link, check your inbox shortly." : reset ? "We’ll email a link to set a new password." : "Verify your email address, then choose a password."}</p>
      <label htmlFor="hosted-email">Email address</label>
      <input id="hosted-email" type="email" autoComplete="email" required maxLength={254}
        value={email} onChange={(e) => setEmail(e.target.value)} />
      <button className="primary" disabled={busy}>{sent ? "Resend email" : reset ? "Email reset link" : "Email verification link"}</button>
    </>}
    {error && <p className="error" role="alert">{error}</p>}
    {!done && <a href="/">Back to sign-in</a>}
  </form></main>;
}

export function DeleteHostedAccount({ signedOut }: { signedOut: () => void }) {
  const [expanded, setExpanded] = useState(false);
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return <section className="account-settings">
    <h2>Delete account</h2>
    <p>Deleting your account removes your printer connection, history, alerts, and connected apps permanently.</p>
    {expanded ? <form onSubmit={async (event) => {
      event.preventDefault();
      setBusy(true);
      setError("");
      try {
        await api("/auth/account", { method: "DELETE", body: JSON.stringify({ password, confirmation }) });
        signedOut();
      } catch (e) { setError((e as Error).message); }
      finally { setBusy(false); }
    }}>
      <label htmlFor="delete-password">Current password</label>
      <input id="delete-password" type="password" autoComplete="current-password" required value={password}
        onChange={(e) => setPassword(e.target.value)} />
      <label htmlFor="delete-confirmation">Type DELETE to confirm</label>
      <input id="delete-confirmation" autoComplete="off" required pattern="DELETE" value={confirmation}
        onChange={(e) => setConfirmation(e.target.value)} />
      <button disabled={busy || confirmation !== "DELETE"}>Permanently delete account</button>
      <button type="button" disabled={busy} onClick={() => { setExpanded(false); setPassword(""); setConfirmation(""); }}>Cancel</button>
    </form> : <button onClick={() => setExpanded(true)}>Delete my account</button>}
    {error && <p className="error" role="alert">{error}</p>}
  </section>;
}
