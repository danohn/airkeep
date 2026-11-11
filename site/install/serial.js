const encoder = new TextEncoder();

export function validateSettings({ ssid, password, relayUrl, amsId }) {
  const size = (value) => encoder.encode(value).length;
  if (!ssid || size(ssid) > 32 || ssid.includes("\0")) {
    throw new Error("Enter a Wi-Fi name of 1–32 bytes.");
  }
  const passwordSize = size(password);
  if (password.includes("\0") || (passwordSize && (passwordSize < 8 || passwordSize > 63) &&
      !/^[a-f0-9]{64}$/i.test(password))) {
    throw new Error("Enter a Wi-Fi password of 8–63 bytes or a 64-digit hexadecimal key.");
  }
  const origin = relayUrl.trim().replace(/\/$/, "");
  if (!/^https:\/\/[^\s/?#@\\]+$/.test(origin) || /[^\x21-\x7e]/.test(origin) || size(origin) > 480 ||
      !URL.canParse(origin) || new URL(origin).port === "0") {
    throw new Error("Enter the relay’s HTTPS address, with a port if needed.");
  }
  if (!/^[0-9]{1,32}$/.test(amsId)) throw new Error("Enter a numeric AMS ID.");
  return { ssid, password, relayUrl: origin, amsId };
}

export async function openDisplay(port, disconnected, timeout = 5000) {
  await port.open({ baudRate: 115200 });
  const reader = port.readable.getReader();
  const writer = port.writable.getWriter();
  let pending;
  let closing;

  const read = (async () => {
    const decoder = new TextDecoder();
    let line = "";
    let discard = false;
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        for (const char of decoder.decode(value, { stream: true })) {
          if (char === "\n") {
            if (!discard && pending?.matches(line.trimEnd())) pending.resolve(line.trimEnd());
            line = "";
            discard = false;
          } else if (line.length >= 1024) {
            discard = true;
          } else if (!discard) {
            line += char;
          }
        }
      }
    } catch {
      // Read errors and unplugging follow the same recovery path.
    } finally {
      reader.releaseLock();
      if (!closing) {
        pending?.reject(new Error("The USB connection closed. Reconnect the display and try again."));
        disconnected();
      }
    }
  })();

  function close() {
    closing ??= (async () => {
      pending?.reject(new Error("The USB connection closed."));
      await reader.cancel().catch(() => {});
      await read;
      writer.releaseLock();
      await port.close().catch(() => {});
    })();
    return closing;
  }

  function request(command, matches, retry = false) {
    return new Promise((resolve, reject) => {
      const finish = (callback) => (value) => {
        clearTimeout(timer);
        clearInterval(resend);
        pending = undefined;
        callback(value);
      };
      const timer = setTimeout(() => {
        pending?.reject(new Error("No response from Airkeep. Reconnect the display and try again."));
      }, timeout);
      const reply = { matches, resolve: finish(resolve), reject: finish(reject) };
      const send = () => writer.write(encoder.encode(command + "\n")).catch((error) => {
        if (pending === reply) reply.reject(error);
      });
      const resend = retry ? setInterval(send, 350) : undefined;
      pending = reply;
      void send();
    });
  }

  try {
    const reply = await request("\nAIRKEEP_HELLO", (line) => line.startsWith("AIRKEEP_READY "), true);
    const match = /^AIRKEEP_READY 1 ([0-9]+\.[0-9]+\.[0-9]+)$/.exec(reply);
    if (!match) throw new Error("This Airkeep firmware does not support this setup page. Install the current release.");
    return {
      version: match[1],
      close,
      async save(settings) {
        const result = await request("AIRKEEP_CONFIG " + JSON.stringify(settings),
          (line) => line === "AIRKEEP_OK saved" || line.startsWith("AIRKEEP_ERROR "));
        if (result === "AIRKEEP_ERROR invalid-settings") throw new Error("The display rejected the settings. Check the fields and try again.");
        if (result !== "AIRKEEP_OK saved") throw new Error("The display could not save its settings. Reconnect and try again.");
      },
    };
  } catch (error) {
    await close();
    throw error;
  }
}
