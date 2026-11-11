import { useEffect, useState } from "react";
import { startAuthentication, type PublicKeyCredentialRequestOptionsJSON } from "@simplewebauthn/browser";
import { api } from "./api";
import { dashboardAuthorization, type Account } from "./auth";
import { SecuritySettings, VerificationCodeField, canUsePasskeys, type SecurityMethods } from "./AccountSecurity";
import { OIDCSettings } from "./OIDCSettings";
import { DeleteHostedAccount } from "./HostedAccounts";

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

export function AccountLogin({ native = false, invite = false, configured = true, error = "", oidcLabel, hosted = false, publicSignup = false, passkeysAvailable = false }: {
  native?: boolean; invite?: boolean; configured?: boolean; error?: string; oidcLabel?: string; hosted?: boolean; publicSignup?: boolean; passkeysAvailable?: boolean;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [transaction, setTransaction] = useState(() => native ? new URLSearchParams(location.search).get("transaction") ?? "" : "");
  const [change, setChange] = useState(false);
  const [needsCode, setNeedsCode] = useState(false);
  const [verificationCode, setVerificationCode] = useState("");
  const [useRecovery, setUseRecovery] = useState(false);
  const [invitation] = useState(() => invite ? location.hash.slice(1) : "");
  const [provider, setProvider] = useState("");
  const [hostedMode, setHostedMode] = useState(hosted);
  const [signupOpen, setSignupOpen] = useState(publicSignup);
  const [passkeys, setPasskeys] = useState(false);
  const [message, setMessage] = useState(() => new URLSearchParams(location.search).get("sso_error") ?? "");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (native || invite || new URLSearchParams(location.search).has("sso_error")) {
      history.replaceState(null, "", location.pathname + (location.pathname === "/device" ? location.hash : ""));
    }
    if (!native && !invite) return;
    let active = true;
    api<{ oidc_label?: string; hosted?: boolean; public_signup?: boolean; passkeys_available?: boolean }>("/auth/session").then((status) => {
      if (active) {
        setProvider(status.oidc_label ?? "");
        setHostedMode(status.hosted ?? false);
        setSignupOpen(status.public_signup ?? false);
        setPasskeys(status.passkeys_available ?? false);
      }
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
  function redirectToCallback(redirect: string) {
    const callback = new URL(redirect);
    const valid = native
      ? callback.protocol === "io.airkeep.oauth:" && callback.pathname === "/callback" && !callback.host
      : callback.origin === location.origin && callback.pathname === "/auth/callback";
    if (!valid) throw new Error("Invalid sign-in callback.");
    location.assign(redirect);
  }
  async function signInWithPasskey() {
    setBusy(true);
    setMessage("");
    try {
      const id = await authorization();
      const optionsJSON = await api<PublicKeyCredentialRequestOptionsJSON>("/auth/passkey/options", {
        method: "POST", body: JSON.stringify({ transaction: id }),
      });
      const credential = await startAuthentication({ optionsJSON });
      const { redirect } = await api<{ redirect: string }>("/auth/passkey/verify", {
        method: "POST", body: JSON.stringify({ transaction: id, credential }),
      });
      redirectToCallback(redirect);
    } catch (e) {
      setMessage(e instanceof DOMException ? "Passkey sign-in didn’t complete. Try again or use your password." : (e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return <div className="login"><form onSubmit={async (event) => {
    event.preventDefault();
    setBusy(true);
    setMessage("");
    try {
      const id = await authorization();
      const result = await api<{ passwordChangeRequired?: boolean; totpRequired?: boolean; redirect?: string }>("/auth/credentials", {
        method: "POST", body: JSON.stringify({
          transaction: id, username, password,
          ...(change ? { new_password: newPassword } : {}),
          ...(needsCode ? { verification_code: verificationCode.trim() } : {}),
        }),
      });
      if (result.passwordChangeRequired) {
        setTransaction(id);
        setChange(true);
      } else if (result.totpRequired) {
        setTransaction(id);
        setNeedsCode(true);
      } else if (result.redirect) {
        redirectToCallback(result.redirect);
      }
    } catch (e) {
      setMessage((e as Error).message);
    } finally {
      setBusy(false);
    }
  }}>
    <img src="/brand/mark.svg" alt="" />
    <h1>{needsCode ? "Verify your sign-in" : invite ? "Airkeep invitation" : native ? "Connect to Airkeep" : "Airkeep"}</h1>
    <p>{needsCode ? (useRecovery ? "Enter one of your saved recovery codes." : "Enter the six-digit code from your authenticator app.") : invite ? "Use single sign-on to accept your invitation." : change ? "Choose your own password." : hostedMode ? "Sign in to your Airkeep account." : "Sign in to your relay."}</p>
    {invite ? <a href="/">Go to Airkeep sign-in</a> : configured ? <>
      {needsCode ? <>
        <VerificationCodeField key={String(useRecovery)} id="sign-in-code" value={verificationCode} onChange={setVerificationCode}
          label={useRecovery ? "Recovery code" : "Authenticator code"} digits={!useRecovery} autoFocus />
        <button className="primary" disabled={busy}>Verify and sign in</button>
        <button type="button" disabled={busy} onClick={() => {
          setUseRecovery(!useRecovery);
          setVerificationCode("");
          setMessage("");
        }}>{useRecovery ? "Use an authenticator code" : "Use a recovery code"}</button>
        <button type="button" disabled={busy} onClick={() => {
          setNeedsCode(false);
          setUseRecovery(false);
          setVerificationCode("");
          setChange(false);
          setPassword("");
          setNewPassword("");
          setMessage("");
          if (!native) setTransaction("");
        }}>Back to sign-in</button>
      </> : <>
        <label htmlFor="username">{hostedMode ? "Email address" : "Username"}</label>
        <input id="username" type={hostedMode ? "email" : "text"} maxLength={hostedMode ? 254 : 64} autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required />
        <label htmlFor="password">{change ? "Temporary password" : "Password"}</label>
        <input id="password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        {change && <>
          <label htmlFor="new-password">New password</label>
          <input id="new-password" type="password" autoComplete="new-password" minLength={12} value={newPassword} onChange={(e) => setNewPassword(e.target.value)} required />
        </>}
        <button className="primary" disabled={busy}>{change ? "Save password and sign in" : "Sign in"}</button>
        {!change && (passkeysAvailable || passkeys) && canUsePasskeys() &&
          <button type="button" disabled={busy} onClick={() => void signInWithPasskey()}>Sign in with a passkey</button>}
      </>}
    </> : <div className="notice">Set up an administrator on your relay to get started.</div>}
    {(oidcLabel || provider) && !change && !needsCode && (!invite || invitation) &&
      <button type="button" disabled={busy} onClick={() => void signInWithOIDC()}>
        Sign in with {oidcLabel || provider}
      </button>}
    {hostedMode && !invite && !needsCode && <>{signupOpen && <a href="/signup">Create an account</a>}<a href="/forgot-password">Forgot password?</a></>}
    {invite && !invitation && <p>Open the original invitation link to try again.</p>}
    {(message || error) && <p className="error" role="alert">{message || error}</p>}
  </form></div>;
}

type Connection = { id: string; client: string; expires: number; current: boolean; display_suffix?: string };
const clientLabels: Record<string, string> = {
  dashboard: "Web dashboard", "apple-ios": "iPhone or iPad", "apple-mac": "Mac", watch: "Apple Watch", esp32: "ESP32 display", "airkeep-relay": "Printer relay",
};

export function AccountSettings({ admin, signedOut, oidcLabel, localPassword = true, hosted = false }: {
  admin: boolean; signedOut: () => void; oidcLabel?: string; localPassword?: boolean; hosted?: boolean;
}) {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [replacement, setReplacement] = useState("");
  const [verificationCode, setVerificationCode] = useState("");
  const [security, setSecurity] = useState<SecurityMethods | null>(null);
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
    if (!admin && localPassword) {
      api<SecurityMethods>("/auth/security").then((methods) => {
        if (active) setSecurity(methods);
      }).catch((error: Error) => { if (active) setError(error.message); });
    }
    return () => { active = false; };
  }, [admin, localPassword]);
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
        await api("/auth/password", {
          method: "POST", body: JSON.stringify({
            password, new_password: replacement,
            ...(security?.totp_enabled ? { verification_code: verificationCode.trim() } : {}),
          }),
        });
        signedOut();
      });
    }}>
      <label htmlFor="current-account-password">Current password</label>
      <input id="current-account-password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
      <label htmlFor="next-account-password">New password</label>
      <input id="next-account-password" type="password" autoComplete="new-password" minLength={12} value={replacement} onChange={(e) => setReplacement(e.target.value)} required />
      {security?.totp_enabled && <VerificationCodeField id="password-change-code" value={verificationCode} onChange={setVerificationCode} />}
      <button className="primary" disabled={busy}>Save password</button>
    </form>}
    {security && <SecuritySettings methods={security} onChange={setSecurity} />}
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
    {hosted && <DeleteHostedAccount signedOut={signedOut} totpEnabled={security?.totp_enabled} />}
    {error && <p className="error" role="alert">{error}</p>}
  </section>;
}
