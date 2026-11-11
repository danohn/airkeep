// @vitest-environment happy-dom
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { api } from "./api";
import { App } from "./App";
import { HostedAccountFlow } from "./HostedAccounts";
vi.mock("./api", async () => ({ ...(await vi.importActual("./api")), api: vi.fn() }));
const request = vi.mocked(api);
beforeEach(() => request.mockResolvedValue({}));
afterEach(() => { cleanup(); vi.clearAllMocks(); history.replaceState(null, "", "/"); });
describe("hosted account links", () => {
  it.each(["/signup/complete", "/reset-password"])("keeps the one-use token across StrictMode renders at %s", async (path) => {
    history.replaceState(null, "", path + "#token=" + "s".repeat(43));
    render(<StrictMode><HostedAccountFlow /></StrictMode>);
    expect(location.hash).toBe("");
    fireEvent.change(screen.getByLabelText("New password"), { target: { value: "a long new password" } });
    fireEvent.change(screen.getByLabelText("Confirm password"), { target: { value: "a long new password" } });
    fireEvent.click(screen.getByRole("button", { name: "Set password" }));
    await screen.findByRole("heading", { name: "Your password is set" });
    expect(request).toHaveBeenCalledWith(path === "/signup/complete" ? "/auth/signup/complete" : "/auth/password/reset", {
      method: "POST", body: JSON.stringify({ token: "s".repeat(43), password: "a long new password" }),
    });
  });
  it("shows a recovery link for a completion URL without a token", () => {
    history.replaceState(null, "", "/reset-password");
    render(<HostedAccountFlow />);
    expect(screen.getByRole("link", { name: "Request a fresh email link" }).getAttribute("href")).toBe("/forgot-password");
    expect(screen.queryByRole("button", { name: "Set password" })).toBeNull();
  });
});


it("shows hosted sign-in again when the OAuth callback has expired", async () => {
  history.replaceState(null, "", "/auth/callback?code=expired");
  request.mockResolvedValue({ authenticated: false, configured: true, mode: "oauth", hosted: true });
  render(<App />);
  await screen.findByText("Start sign-in again.");
  expect(screen.getByLabelText("Email address")).toBeTruthy();
});
