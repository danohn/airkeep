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
