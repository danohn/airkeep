import { useEffect, useState } from "react";
import { api, when } from "./api";
import type { Account } from "./auth";

type Policy = { automatic_creation: boolean; group_claim: string; admin_groups: string[] };
type Invitation = { id: string; username: string; role: Account["role"]; expires: number };

const invitationsPath = "/v2/admin/oidc/invitations";

export function OIDCSettings({ accounts }: { accounts: Account[] }) {
  const [policy, setPolicy] = useState<Policy>();
  const [groups, setGroups] = useState("");
  const [invites, setInvites] = useState<Invitation[]>([]);
  const [accountID, setAccountID] = useState("");
  const [username, setUsername] = useState("");
  const [role, setRole] = useState<Account["role"]>("member");
  const [link, setLink] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let active = true;
    Promise.all([api<Policy>("/v2/admin/oidc"), api<Invitation[]>(invitationsPath)]).then(([saved, pending]) => {
      if (!active) return;
      setPolicy(saved);
      setGroups(saved.admin_groups.join("\n"));
      setInvites(pending);
    }).catch((e: Error) => { if (active) setError(e.message); });
    return () => { active = false; };
  }, []);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setMessage("");
    setError("");
    try { await action(); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }

  return <section aria-label="Single sign-on">
    <h2>SSO</h2>
    {policy && <form onSubmit={(event) => {
      event.preventDefault();
      void run(async () => {
        const saved = await api<Policy>("/v2/admin/oidc", {
          method: "PUT",
          body: JSON.stringify({ ...policy, admin_groups: [...new Set(groups.split("\n").filter((name) => name.trim()))] }),
        });
        setPolicy(saved);
        setGroups(saved.admin_groups.join("\n"));
        setMessage("SSO settings saved.");
      });
    }}>
      <label><input type="checkbox" checked={policy.automatic_creation}
        onChange={(event) => setPolicy({ ...policy, automatic_creation: event.target.checked })} />
        Automatically create accounts
      </label>
      <p>New accounts start as members. A matching admin group grants the initial admin role.</p>
      <label htmlFor="oidc-group-claim">Group claim</label>
      <input id="oidc-group-claim" value={policy.group_claim} maxLength={100} required
        onChange={(event) => setPolicy({ ...policy, group_claim: event.target.value })} />
      <label htmlFor="oidc-admin-groups">Admin groups (one per line)</label>
      <textarea id="oidc-admin-groups" value={groups} onChange={(event) => setGroups(event.target.value)} />
      <p>Manage later role changes in Accounts.</p>
      <button className="primary" disabled={busy}>Save SSO settings</button>
    </form>}
    <h3>Invitations</h3>
    <p>Creating another invitation for the same account replaces its previous link.</p>
    <form onSubmit={(event) => {
      event.preventDefault();
      void run(async () => {
        const result = await api<{ url: string }>(invitationsPath, {
          method: "POST", body: JSON.stringify(accountID ? { account_id: accountID } : { username, role }),
        });
        setLink(result.url);
        setInvites(await api<Invitation[]>(invitationsPath));
      });
    }}>
      <label htmlFor="oidc-invite-account">Invite to</label>
      <select id="oidc-invite-account" value={accountID} onChange={(event) => setAccountID(event.target.value)}>
        <option value="">A new account</option>
        {accounts.filter((account) => !account.disabled).map((account) =>
          <option value={account.id} key={account.id}>{account.username}</option>)}
      </select>
      {!accountID && <>
        <label htmlFor="oidc-invite-name">Username</label>
        <input id="oidc-invite-name" value={username} required pattern="[A-Za-z0-9][A-Za-z0-9_.\x2d]{2,63}"
          onChange={(event) => setUsername(event.target.value)} />
        <label htmlFor="oidc-invite-role">Role</label>
        <select id="oidc-invite-role" value={role} onChange={(event) => setRole(event.target.value === "admin" ? "admin" : "member")}>
          <option value="member">Member</option><option value="admin">Admin</option>
        </select>
      </>}
      <button className="primary" disabled={busy}>Create invitation</button>
    </form>
    {link && <p role="status">Share this private link with the recipient. It expires in 24 hours.
      <input aria-label="Private invitation link" readOnly value={link} onFocus={(event) => event.target.select()} />
    </p>}
    {invites.map((invite) => <p key={invite.id}>{invite.username} · {invite.role} · Expires {when(invite.expires)}{" "}
      <button disabled={busy} onClick={() => void run(async () => {
        await api(invitationsPath + "/" + invite.id, { method: "DELETE" });
        setLink("");
        setInvites(await api<Invitation[]>(invitationsPath));
      })}>Revoke invitation</button>
    </p>)}
    {message && <p role="status">{message}</p>}
    {error && <p className="error" role="alert">{error}</p>}
  </section>;
}
