export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}
let csrf = "";
let sessionGeneration = 0;
export function setCsrf(value: string) {
  if (csrf !== value) sessionGeneration++;
  csrf = value;
}
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const requestGeneration = sessionGeneration;
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      credentials: "same-origin",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        ...(csrf ? { "X-Airkeep-CSRF": csrf } : {}),
        ...init.headers,
      },
    });
  } catch {
    throw new ApiError(
      0,
      "Could not reach your relay. Reconnect and try again.",
    );
  }
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    const message =
      typeof detail.detail === "string"
        ? detail.detail
        : response.status === 412
          ? "This alert changed on another device. Reload the latest rule before saving."
          : response.status === 404
            ? "This alert was removed on another device."
            : response.status === 401
              ? "Your session has ended. Sign in again."
              : "Please try again.";
    if (
      response.status === 401 &&
      path !== "/auth/login" &&
      requestGeneration === sessionGeneration
    )
      window.dispatchEvent(new Event("airkeep:session-ended"));
    throw new ApiError(response.status, message);
  }
  return response.status === 204 ? (undefined as T) : response.json();
}
export type Reading = {
  amsID: string;
  humidity: number;
  temperature: number | null;
  observedAt: number;
};
export type Snapshot = {
  readings: Reading[];
  staleAfter: number;
  printerConnected: boolean;
  serverTime: number;
};
export type Rule = {
  id: string;
  revision: number;
  name: string;
  enabled: boolean;
  amsID: string;
  threshold: number;
  holdSeconds: number;
  hysteresis: number;
  active: boolean;
};
export type Point = {
  bucketStart: number;
  humidityMean: number;
  humidityMin: number;
  humidityMax: number;
  sampleCount: number;
  lastObservedAt: number;
};
export type History = {
  from: number;
  to: number;
  bucketSeconds: number;
  series: { points: Point[] }[];
};
export type AlertEvent = {
  id: string;
  occurredAt: number;
  type: "high" | "recovered";
  ruleID: string;
  name: string;
  amsID: string;
  condition: Pick<
    Rule,
    "enabled" | "amsID" | "threshold" | "holdSeconds" | "hysteresis"
  >;
  reading: Reading;
};
export type EventFilters = {
  amsIDs: string[];
  rules: { id: string; name: string; deleted: boolean }[];
};
export type EventPage = {
  events: AlertEvent[];
  nextCursor: string | null;
  filters: EventFilters;
};
export function amsName(id: string) {
  return `AMS ${/^\d+$/.test(id) ? Number(id) + 1 : id}`;
}
export function when(value: number | null | undefined) {
  return value
    ? new Date(value * 1000).toLocaleString([], {
        dateStyle: "medium",
        timeStyle: "short",
      })
    : "Unknown";
}
export function age(value: number, now = Date.now() / 1000) {
  const elapsed = Math.max(0, Math.floor(now - value));
  return elapsed < 60
    ? `${elapsed} sec. ago`
    : elapsed < 3600
      ? `${Math.floor(elapsed / 60)} min. ago`
      : `${Math.floor(elapsed / 3600)} hr. ago`;
}
