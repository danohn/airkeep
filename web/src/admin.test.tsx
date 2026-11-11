// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { App } from "./App";
import { api } from "./api";
vi.mock("./api", async () => ({
  ...(await vi.importActual("./api")),
  api: vi.fn(),
}));
const request = vi.mocked(api);
const overview = {
  version: "0.3.0",
  uptimeSeconds: 1200,
  printerConnected: true,
  databaseBytes: 2000,
  historyDays: 7,
  counts: { devices: 1 },
  pushSetup: [],
  timelineStartedAt: 1,
};
function reply(path: string) {
  if (path === "/auth/session")
    return { authenticated: true, configured: true, csrf: "test" };
  if (path === "/v1/snapshot")
    return {
      readings: [
        {
          amsID: "0",
          humidity: 19,
          temperature: 24,
          observedAt: Date.now() / 1000,
        },
      ],
      staleAfter: 90,
      printerConnected: true,
    };
  if (path === "/v2/alerts") return [];
  if (path.startsWith("/v2/history")) return { series: [] };
  if (path === "/v2/admin/overview") return overview;
  if (path === "/v2/admin/devices") return [{ id: "phone", platform: "ios" }];
  if (path === "/v2/admin/widgets") return [];
  if (path === "/v2/admin/activities") return [{ id: "activity", amsID: "0" }];
  if (path === "/v2/admin/queue") return [{ id: 1, deviceID: "phone" }];
  if (path === "/v2/admin/push-attempts")
    return { attempts: [], nextCursor: null };
  return {};
}
beforeEach(() => {
  location.hash = "";
  request.mockImplementation(async (path) => reply(path) as never);
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
async function openAdmin() {
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "Admin" }));
  await screen.findByRole("heading", { name: "Storage" });
}
describe("Admin navigation", () => {
  it.each([
    { seconds: 600, label: "10 min. ago" },
    { seconds: null, label: "Unknown" },
  ])(
    "shows the oldest AMS reading age as $label",
    async ({ seconds, label }) => {
      request.mockImplementation(async (path) =>
        path === "/v2/admin/overview"
          ? ({ ...overview, readingAgeSeconds: seconds } as never)
          : (reply(path) as never),
      );
      await openAdmin();
      expect(screen.getByText(`Oldest AMS reading: ${label}`)).toBeTruthy();
    },
  );
  it("changes between every loaded page without rendering another page’s payload", async () => {
    await openAdmin();
    for (const [label, heading] of [
      ["Registered devices", "Apple devices"],
      ["Apple push", "Recent push requests"],
      ["Live Activities", "Registered sessions"],
      ["Pending queue", "Humidity notifications"],
      ["Relay", "Storage"],
    ]) {
      fireEvent.click(screen.getByRole("button", { name: label }));
      expect(
        await screen.findByRole("heading", { name: heading }),
      ).toBeTruthy();
    }
  });
  it("ignores responses from a page left while its request was still loading", async () => {
    await openAdmin();
    let finish!: (value: unknown) => void;
    request.mockImplementation(async (path) =>
      path === "/v2/admin/devices"
        ? new Promise((resolve) => {
            finish = resolve;
          })
        : (reply(path) as never),
    );
    fireEvent.click(screen.getByRole("button", { name: "Registered devices" }));
    await screen.findByText("Loading…");
    fireEvent.click(screen.getByRole("button", { name: "Apple push" }));
    await screen.findByRole("heading", {
      name: "Recent push requests",
    });
    await act(async () => finish([{ id: "late-device" }]));
    expect(
      screen.getByRole("heading", {
        name: "Recent push requests",
      }),
    ).toBeTruthy();
    expect(screen.queryByText("late-device")).toBeNull();
  });
});

describe("Hosted admin", () => {
  const prefix = "/v2/admin/hosted";
  const accounts = [
    { id: "first", email: "first@example.test", role: "member", disabled: false },
    { id: "second", email: "second@example.test", role: "member", disabled: false },
  ];
  function hostedReply(path: string, role: "member" | "admin" = "admin") {
    if (path === "/auth/session") return { authenticated: true, configured: true, mode: "oauth", hosted: true,
      account: { id: "actor", username: "operator@example.test", role } };
    if (path === `${prefix}/accounts`) return { accounts, nextCursor: null };
    if (path === `${prefix}/health`) return { version: "0.5.0", uptimeSeconds: 30,
      database: { status: "ok", bytes: 1048576 }, pushSetup: [], email: { configured: true, counts: {} } };
    return reply(path);
  }
  it("keeps a member on their user dashboard when opening an admin link", async () => {
    history.replaceState(null, "", "/#devices");
    request.mockImplementation(async (path) => hostedReply(path, "member") as never);
    render(<App />);
    await screen.findByText("Relative humidity");
    expect(screen.queryByRole("button", { name: "Admin" })).toBeNull();
    expect(request.mock.calls.some(([path]) => path.startsWith("/v2/admin"))).toBe(false);
  });
  it("starts with no selected tenant and ignores a response from a previous selection", async () => {
    let finish!: (value: never) => void;
    let previousSignal: AbortSignal | null | undefined;
    request.mockImplementation(async (path, init) => {
      if (path === `${prefix}/accounts/first/devices`) {
        previousSignal = init?.signal;
        return new Promise((resolve) => { finish = resolve; });
      }
      if (path === `${prefix}/accounts/second/devices`) return [{ id: "second-device" }] as never;
      if (path.endsWith("/widgets")) return [] as never;
      return hostedReply(path) as never;
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Admin" }));
    await screen.findByText("Choose an account to view its diagnostics.");
    expect(request.mock.calls.some(([path]) => path.includes("/accounts/first/"))).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Registered devices" }));
    await screen.findByRole("option", { name: "first@example.test" });
    const select = screen.getByLabelText("Select account");
    fireEvent.change(select, { target: { value: "first" } });
    await screen.findByText("Loading…");
    fireEvent.change(select, { target: { value: "second" } });
    await screen.findByText("second-device");
    expect(previousSignal?.aborted).toBe(true);
    await act(async () => finish([{ id: "late-first-device" }] as never));
    expect(screen.queryByText("late-first-device")).toBeNull();
  });
});

describe("Overview copy", () => {
  it.each([0, 1, 2])(
    "uses the correct alert noun for %i enabled rules",
    async (count) => {
      const rules = Array.from({ length: count }, (_, index) => ({
        id: "rule" + index,
        name: "Alert " + index,
        amsID: "0",
        enabled: true,
        revision: 1,
        threshold: 30,
        holdSeconds: 300,
        hysteresis: 3,
        active: false,
      }));
      request.mockImplementation(async (path) =>
        path === "/v2/alerts" ? (rules as never) : (reply(path) as never),
      );
      render(<App />);
      expect(
        await screen.findByRole("button", {
          name: new RegExp(
            `${count} ${count === 1 ? "alert" : "alerts"} enabled`,
          ),
        }),
      ).toBeTruthy();
    },
  );
});
