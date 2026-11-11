import { useState } from "react";
import {
  browserSupportsWebAuthn, startRegistration, type PublicKeyCredentialCreationOptionsJSON,
} from "@simplewebauthn/browser";
import { api, when } from "./api";

type Passkey = { credential_id: string; label: string; created_at: number; last_used_at: number | null };
export type SecurityMethods = { passkeys: Passkey[]; totp_enabled: boolean; passkeys_available: boolean };
type Change = { kind: "remove-passkey"; passkey: Passkey } |
  { kind: "add-passkey" | "enable-totp" | "disable-totp" | "replace-codes" };
type Setup = { secret: string; qr_code: string };
type Recovery = { recovery_codes: string[] };

export function canUsePasskeys() {
  return window.isSecureContext && browserSupportsWebAuthn();
}

export function VerificationCodeField({
  id, value, onChange, label = "Authenticator or recovery code", digits = false, autoFocus = false,
}: {
  id: string; value: string; onChange: (value: string) => void; label?: string; digits?: boolean; autoFocus?: boolean;
}) {
  return <>
    <label htmlFor={id}>{label}</label>
    <input id={id} value={value} onChange={(event) => onChange(event.target.value)}
      inputMode={digits ? "numeric" : undefined} pattern={digits ? "[0-9]{6}" : undefined}
      maxLength={digits ? 6 : 40} autoComplete={digits ? "one-time-code" : "off"}
      autoCapitalize="off" spellCheck={false} autoFocus={autoFocus} required />
  </>;
}

function changeLabel(change: Change) {
  switch (change.kind) {
    case "add-passkey": return "Add passkey";
    case "remove-passkey": return "Remove passkey";
    case "enable-totp": return "Set up authenticator";
    case "disable-totp": return "Turn off authenticator";
    case "replace-codes": return "Replace recovery codes";
  }
}

export function SecuritySettings({ methods, onChange }: {
  methods: SecurityMethods; onChange: (methods: SecurityMethods) => void;
}) {
  const [change, setChange] = useState<Change | null>(null);
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [label, setLabel] = useState("");
  const [setup, setSetup] = useState<Setup | null>(null);
  const [recovery, setRecovery] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  function select(next: Change | null) {
    setChange(next);
    setPassword("");
    setCode("");
    setLabel("");
    setSetup(null);
    setError("");
  }

  async function refresh() {
    onChange(await api<SecurityMethods>("/auth/security"));
  }

  async function confirm(action: Change) {
    const body = JSON.stringify({
      password,
      ...(methods.totp_enabled ? { verification_code: code.trim() } : {}),
    });
    switch (action.kind) {
      case "add-passkey": {
        const optionsJSON = await api<PublicKeyCredentialCreationOptionsJSON>("/auth/security/passkeys/options", { method: "POST", body });
        const credential = await startRegistration({ optionsJSON });
        await api("/auth/security/passkeys", {
          method: "POST", body: JSON.stringify({ label: label.trim() || "Passkey", credential }),
        });
        break;
      }
      case "remove-passkey":
        await api("/auth/security/passkeys/" + encodeURIComponent(action.passkey.credential_id), { method: "DELETE", body });
        break;
      case "enable-totp": {
        const enrollment = await api<Setup>("/auth/security/totp/setup", { method: "POST", body });
        select(null);
        setSetup(enrollment);
        return;
      }
      case "disable-totp":
        await api("/auth/security/totp", { method: "DELETE", body });
        setRecovery([]);
        break;
      case "replace-codes": {
        const result = await api<Recovery>("/auth/security/totp/recovery", { method: "POST", body });
        setRecovery(result.recovery_codes);
        break;
      }
    }
    select(null);
    await refresh();
  }

  async function enableAuthenticator() {
    const result = await api<Recovery>("/auth/security/totp", { method: "POST", body: JSON.stringify({ code }) });
    select(null);
    setRecovery(result.recovery_codes);
    await refresh();
  }

  async function run(action: () => Promise<void>) {
    setError("");
    setBusy(true);
    try { await action(); }
    catch (e) {
      setError(e instanceof DOMException ? "Passkey setup didn’t complete. Try again." : (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return <section className="account-settings security-settings" aria-labelledby="sign-in-methods">
    <h2 id="sign-in-methods">Sign-in methods</h2>
    <div className="security-method">
      <h3>Passkeys</h3>
      <p>Sign in with your device or a security key.</p>
      {methods.passkeys.map((passkey) => <div className="security-passkey" key={passkey.credential_id}>
        <div><strong>{passkey.label}</strong>
          <small>{passkey.last_used_at ? "Last used " + when(passkey.last_used_at) : "Added " + when(passkey.created_at)}</small>
        </div>
        <button type="button" disabled={busy} aria-label={"Remove passkey " + passkey.label}
          onClick={() => select({ kind: "remove-passkey", passkey })}>Remove</button>
      </div>)}
      {methods.passkeys_available && canUsePasskeys()
        ? <button type="button" disabled={busy} onClick={() => select({ kind: "add-passkey" })}>Add passkey</button>
        : <p>To add a passkey, open Airkeep on its HTTPS hostname in a browser that supports passkeys.</p>}
    </div>
    <div className="security-method">
      <h3>Authenticator app</h3>
      <p>{methods.totp_enabled ? "On for password sign-in." : "Require a six-digit code when you sign in with your password."}</p>
      <div className="security-actions">
        {methods.totp_enabled ? <>
          <button type="button" disabled={busy} onClick={() => select({ kind: "replace-codes" })}>Replace recovery codes</button>
          <button type="button" disabled={busy} onClick={() => select({ kind: "disable-totp" })}>Turn off authenticator</button>
        </> : <button type="button" disabled={busy} onClick={() => select({ kind: "enable-totp" })}>Set up authenticator</button>}
      </div>
    </div>
    {change && <form className="security-form" onSubmit={(event) => { event.preventDefault(); void run(() => confirm(change)); }}>
      <h3>{changeLabel(change)}</h3>
      {change.kind === "remove-passkey" && <p>Remove <strong>{change.passkey.label}</strong> from your Airkeep account.</p>}
      {change.kind === "replace-codes" && <p>Your old recovery codes will stop working.</p>}
      {change.kind === "add-passkey" && <>
        <label htmlFor="passkey-label">Passkey name</label>
        <input id="passkey-label" value={label} onChange={(event) => setLabel(event.target.value)} maxLength={60} placeholder="Passkey" />
      </>}
      <label htmlFor="security-password">Current password</label>
      <input id="security-password" type="password" autoComplete="current-password" value={password} required autoFocus
        onChange={(event) => setPassword(event.target.value)} />
      {methods.totp_enabled && <VerificationCodeField id="security-verification" value={code} onChange={setCode} />}
      <div className="security-actions">
        <button className="primary" disabled={busy}>{changeLabel(change)}</button>
        <button type="button" disabled={busy} onClick={() => select(null)}>Cancel</button>
      </div>
    </form>}
    {setup && <form className="security-form" onSubmit={(event) => { event.preventDefault(); void run(enableAuthenticator); }}>
      <h3>Connect your authenticator app</h3>
      <p>Scan the QR code or enter the setup key in your authenticator app.</p>
      <img className="security-qr" src={setup.qr_code} alt="QR code for authenticator setup" width={192} height={192} />
      <p>Setup key <code>{setup.secret}</code></p>
      <VerificationCodeField id="setup-verification" label="Authenticator code" value={code} onChange={setCode} digits autoFocus />
      <div className="security-actions">
        <button className="primary" disabled={busy}>Turn on authenticator</button>
        <button type="button" disabled={busy} onClick={() => select(null)}>Cancel</button>
      </div>
    </form>}
    {recovery.length > 0 && <div className="security-form" aria-labelledby="recovery-codes">
      <h3 id="recovery-codes">Save your recovery codes</h3>
      <p>Keep these somewhere safe. Each code works once in place of an authenticator code. This is the only time you can view them.</p>
      <ul className="security-recovery">{recovery.map((item) => <li key={item}><code>{item}</code></li>)}</ul>
      <button type="button" onClick={() => setRecovery([])}>Done</button>
    </div>}
    {error && <p className="error" role="alert">{error}</p>}
  </section>;
}
