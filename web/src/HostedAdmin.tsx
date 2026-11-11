import { useEffect, useState, type ReactNode } from "react";
import { api } from "./api";

export const hostedAdminPath = "/v2/admin/hosted";

type HostedAdminAccount = {
  id: string;
  email: string;
  role: "admin" | "member";
  disabled: boolean;
};

type Directory = { accounts: HostedAdminAccount[]; nextCursor: string | null };

export function HostedAdmin({
  directory,
  children,
}: {
  directory: boolean;
  children: (accountId: string | null) => ReactNode;
}) {
  const [accounts, setAccounts] = useState<HostedAdminAccount[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [selection, setSelection] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const selected = accounts.find((account) => account.id === selection);

  useEffect(() => {
    const controller = new AbortController();
    api<Directory>(`${hostedAdminPath}/accounts`, { signal: controller.signal })
      .then(({ accounts, nextCursor }) => {
        if (controller.signal.aborted) return;
        setAccounts(accounts);
        setCursor(nextCursor);
      })
      .catch((error: Error) => {
        if (!controller.signal.aborted) setError(error.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, []);

  async function loadMore() {
    if (cursor === null || loading) return;
    setLoading(true);
    setError("");
    try {
      const next = await api<Directory>(
        `${hostedAdminPath}/accounts?cursor=${encodeURIComponent(cursor)}`,
      );
      setAccounts((current) => {
        const byId = new Map(current.map((account) => [account.id, account]));
        for (const account of next.accounts) byId.set(account.id, account);
        return Array.from(byId.values());
      });
      setCursor(next.nextCursor);
    } catch (error) {
      setError((error as Error).message);
    } finally {
      setLoading(false);
    }
  }

  return <>
    {directory && <div className="page-heading"><div>
      <span className="eyebrow">Admin</span>
      <h1>Accounts</h1>
    </div></div>}
    <section className="data-section hosted-admin-scope" aria-label="Account scope">
      <div>
        <label htmlFor="hosted-admin-account">Select account</label>
        <select id="hosted-admin-account" value={selection} onChange={(event) => setSelection(event.target.value)}>
          <option value="">{loading && !accounts.length ? "Loading accounts…" : "Choose an account"}</option>
          {accounts.map((account) => <option key={account.id} value={account.id}>
            {account.email}{account.disabled ? " (suspended)" : ""}
          </option>)}
        </select>
      </div>
      {cursor !== null && <button className="load-more" onClick={loadMore} disabled={loading}>
        Load more accounts
      </button>}
      {error && <p role="alert" className="notice">{error}</p>}
    </section>
    {directory ? <>
      {selected && <AccountActions
        key={selected.id}
        account={selected}
        setDisabled={(disabled) => setAccounts((current) => current.map(
          (account) => account.id === selected.id ? { ...account, disabled } : account,
        ))}
      />}
      <InviteAccount />
    </> : children(selected?.id ?? null)}
  </>;
}

function AccountActions({ account, setDisabled }: {
  account: HostedAdminAccount;
  setDisabled: (disabled: boolean) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  async function act(action: "suspend" | "reactivate" | "reset") {
    const confirmation = action === "suspend"
      ? `Suspend ${account.email} and revoke their connected devices and relay?`
      : action === "reactivate"
        ? `Reactivate ${account.email}? They will need to sign in and pair again.`
        : `Send a password reset link to ${account.email}?`;
    if (!window.confirm(confirmation)) return;
    setBusy(true);
    setMessage("");
    setError("");
    try {
      await api(`${hostedAdminPath}/accounts/${encodeURIComponent(account.id)}/${action}`, {
        method: "POST",
      });
      if (action !== "reset") setDisabled(action === "suspend");
      setMessage(action === "reset" ? "Password reset email queued." : action === "suspend" ? "Account suspended." : "Account reactivated.");
    } catch (error) {
      setError((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return <section className="data-section hosted-admin-card">
    <h2>{account.email}</h2>
    <p className="muted">{account.role === "admin" ? "Administrator" : "Member"} · {account.disabled ? "Suspended" : "Active"}</p>
    {account.role === "member" && <div className="hosted-admin-actions">
      <button className="load-more" disabled={busy} onClick={() => void act(account.disabled ? "reactivate" : "suspend")}>
        {account.disabled ? "Reactivate account" : "Suspend account"}
      </button>
      {!account.disabled && <button className="load-more" disabled={busy} onClick={() => void act("reset")}>
        Send password reset
      </button>}
    </div>}
    {message && <p role="status">{message}</p>}
    {error && <p className="notice" role="alert">{error}</p>}
  </section>;
}

function InviteAccount() {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  return <section className="data-section hosted-admin-card">
    <h2>Invite a member</h2>
    <form className="hosted-admin-actions" onSubmit={async (event) => {
      event.preventDefault();
      setBusy(true);
      setMessage("");
      setError("");
      try {
        await api(`${hostedAdminPath}/invitations`, { method: "POST", body: JSON.stringify({ email }) });
        setMessage("Invitation email queued.");
        setEmail("");
      } catch (error) {
        setError((error as Error).message);
      } finally {
        setBusy(false);
      }
    }}>
      <div>
        <label htmlFor="hosted-admin-invite">Email address</label>
        <input id="hosted-admin-invite" value={email} onChange={(event) => setEmail(event.target.value)}
          type="email" autoComplete="off" required maxLength={254} />
      </div>
      <button className="primary" disabled={busy}>Send invitation</button>
    </form>
    {message && <p role="status">{message}</p>}
    {error && <p className="notice" role="alert">{error}</p>}
  </section>;
}
