import { useEffect, useState } from "react";
import { api } from "./api";

type Relay = { status: "unpaired" | "relay_offline" | "printer_disconnected" | "waiting_for_ams" | "connected"; printerID: string | null };
const descriptions: Record<Relay["status"], string> = {
  unpaired: "Add your printer to start receiving AMS readings.",
  relay_offline: "Your relay is offline. Check its power and internet connection.",
  printer_disconnected: "Your relay is online and waiting for your printer. Check the printer address and LAN access code in relay setup.",
  waiting_for_ams: "Your printer is connected. Waiting for AMS readings.",
  connected: "Your printer and AMS are connected.",
};

export function HostedSetup() {
  const [relay, setRelay] = useState<Relay>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<"disconnect" | "replace">();
  const [expanded, setExpanded] = useState(location.pathname === "/setup");
  useEffect(() => {
    let active = true;
    async function load() {
      if (document.hidden) return;
      try { const value = await api<Relay>("/v1/relay"); if (active) { setRelay(value); setError(""); } }
      catch (e) { if (active) setError((e as Error).message); }
    }
    void load();
    const timer = setInterval(load, 5000);
    return () => { active = false; clearInterval(timer); };
  }, []);
  return <section className="hosted-setup">
    <div className="hosted-setup-heading"><h2>{relay?.status === "unpaired" ? "Add printer" : "Your printer"}</h2>
      <button aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? "Hide setup" : "Printer setup"}</button></div>
    {relay && <p role="status">{descriptions[relay.status]}</p>}
    {relay?.printerID && <p>Printer <code>{relay.printerID}</code></p>}
    {(expanded || relay?.status === "unpaired") && <>
      <h3>Connect your local relay</h3>
      <p>Run the Airkeep relay on a computer or device on your printer’s network. In relay setup, select Airkeep hosted, then enter your printer’s address, serial number, and LAN access code.</p>
      <p>Open the pairing link shown by your relay and sign in to this account. Check that its code matches before approving.</p>
      <a className="primary" href="/device">Enter relay pairing code</a>
      <p><a href="https://github.com/danohn/airkeep/blob/main/docs/docker-deployment.md" target="_blank" rel="noreferrer">Relay installation instructions</a></p>
      {relay?.status !== "unpaired" && <div className="hosted-setup-actions">
        <button disabled={busy} onClick={() => setConfirm("disconnect")}>Disconnect relay</button>
        <button disabled={busy} onClick={() => setConfirm("replace")}>Replace printer</button>
      </div>}
    </>}
    {confirm && <div className="notice">
      <p>{confirm === "replace" ? "Replacing your printer removes its source and stored history. Pair the new printer after confirming." : "Disconnecting stops new readings. Your stored history is kept."}</p>
      <button disabled={busy} onClick={async () => {
        setBusy(true); setError("");
        try {
          await api("/v1/relay" + (confirm === "replace" ? "?replace=true" : ""), { method: "DELETE" });
          setRelay(await api<Relay>("/v1/relay")); setConfirm(undefined);
          window.dispatchEvent(new Event("airkeep:relay-changed"));
        } catch (e) { setError((e as Error).message); }
        finally { setBusy(false); }
      }}>{confirm === "replace" ? "Remove printer and history" : "Disconnect relay"}</button>
      <button disabled={busy} onClick={() => setConfirm(undefined)}>Cancel</button>
    </div>}
    {error && <p className="error" role="alert">{error}</p>}
  </section>;
}
