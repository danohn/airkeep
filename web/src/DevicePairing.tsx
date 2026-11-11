import { useState } from "react";
import { api } from "./api";
import { devicePairingCode, devicePairingPath } from "./auth";

type Pairing = { user_code: string; display: string; client_id?: string };

export function DevicePairing() {
  const [code, setCode] = useState(devicePairingCode);
  const [pairing, setPairing] = useState<Pairing>();
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(approve?: boolean) {
    setBusy(true);
    setError("");
    try {
      if (approve === undefined) {
        history.replaceState(null, "", devicePairingPath(code));
        setPairing(await api<Pairing>("/auth/device/verify", {
          method: "POST", body: JSON.stringify({ user_code: code }),
        }));
      } else if (pairing) {
        await api("/auth/device/decision", {
          method: "POST", body: JSON.stringify({ user_code: pairing.user_code, approve }),
        });
        setPairing(undefined);
        setMessage(approve ? pairing.client_id === "airkeep-relay" ? "Relay approved. Return to relay setup." : "Display approved. Return to your ESP32." : "Pairing denied.");
        history.replaceState(null, "", "/device");
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return <main className="login"><form onSubmit={(event) => { event.preventDefault(); void submit(); }}>
    <img src="/brand/mark.svg" alt="" />
    <h1>{pairing?.client_id === "airkeep-relay" ? "Pair relay" : "Pair display or relay"}</h1>
    {message ? <p role="status">{message}</p> : pairing ? <>
      <p>Check that <strong>{pairing.user_code}</strong> matches the code on your {pairing.display}.</p>
      <button type="button" className="primary" disabled={busy} onClick={() => void submit(true)}>Approve</button>
      <button type="button" disabled={busy} onClick={() => void submit(false)}>Deny</button>
    </> : <>
      <label htmlFor="pairing-code">Code on your display or relay</label>
      <input id="pairing-code" autoComplete="off" autoCapitalize="characters" spellCheck={false}
        maxLength={16} required value={code} onChange={(event) => setCode(event.target.value)} />
      <button className="primary" disabled={busy}>Continue</button>
    </>}
    {error && <p className="error" role="alert">{error}</p>}
    <p><a href="/">Back to Airkeep</a></p>
  </form></main>;
}
