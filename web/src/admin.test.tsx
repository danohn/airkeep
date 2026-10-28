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
