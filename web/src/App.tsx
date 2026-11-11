import { useEffect, useId, useRef, useState } from "react";
import {
  Bell,
  CaretDown,
  CaretRight,
  ChartLine,
  Clock,
  House,
  SignOut,
  Thermometer,
  User,
  Devices,
  Pulse,
  AppleLogo,
  Stack,
  ArrowClockwise,
  Plus,
  X,
} from "@phosphor-icons/react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, ApiError, setCsrf, setAccount, amsName, when, age } from "./api";
import type {
  AlertEvent,
  EventPage,
  EventFilters,
  History,
  Rule,
  Snapshot,
} from "./api";
import { AccountLogin, AccountSettings, beginDashboardSignIn, startOIDC } from "./AccountAuth";
import { clearLegacyBrowserCredentials, finishDashboardAuthorization, type Account } from "./auth";
import { HostedAccountFlow, hostedAccountPaths } from "./HostedAccounts";
import { HostedAdmin, hostedAdminPath } from "./HostedAdmin";
import { HostedSetup } from "./HostedSetup";
import { DevicePairing } from "./DevicePairing";
import "@fontsource/quicksand/latin-700.css";
import "./style.css";

type Page =
  | "overview"
  | "history"
  | "alerts"
  | "relay"
  | "devices"
  | "activities"
  | "push"
  | "queue"
  | "accounts"
  | "password";
type Session = { authenticated: boolean; configured: boolean; csrf?: string; mode?: "oauth"; account?: Account; oidc_label?: string; hosted?: boolean; public_signup?: boolean; passkeys_available?: boolean };
type RecordRow = Record<string, unknown>;
const userPages = [
  ["overview", "Overview", House],
  ["history", "History", ChartLine],
  ["alerts", "Humidity alerts", Bell],
] as const;
const adminPages = [
  ["relay", "Relay", Pulse],
  ["devices", "Registered devices", Devices],
  ["activities", "Live Activities", Stack],
  ["push", "Apple push", AppleLogo],
  ["queue", "Pending queue", Clock],
] as const;

export function App() {
  const [editor, setEditor] = useState<{
    rule: Rule;
    creating: boolean;
  } | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [mode, setMode] = useState<"user" | "admin">("user");
  const [page, setPage] = useState<Page>("overview");
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [rules, setRules] = useState<Rule[]>([]);
  const [ams, setAms] = useState("0");
  const [connectionError, setConnectionError] = useState("");
  const [rulesError, setRulesError] = useState("");
  const [rulesSynced, setRulesSynced] = useState<number | null>(null);
  const [tick, setTick] = useState(Date.now() / 1000);
  const [sessionError, setSessionError] = useState("");
  const loggedOut = () => {
    setSession((current) => ({
      authenticated: false, configured: true, mode: current?.mode, oidc_label: current?.oidc_label,
      hosted: current?.hosted, public_signup: current?.public_signup, passkeys_available: current?.passkeys_available,
    }));
    setMode("user");
    setPage("overview");
    if (location.pathname !== "/device") history.replaceState(null, "", "/");
    setCsrf("");
    setAccount("");
    setAms("0");
    setConnectionError("");
    setRulesError("");
    setSnapshot(null);
    setRules([]);
    setRulesSynced(null);
    setEditor(null);
  };
  useEffect(() => {
    let mounted = true;
    clearLegacyBrowserCredentials();
    const accountChanged = () => { loggedOut(); location.reload(); };
    api<Session>("/auth/session")
      .then(async (initial) => {
        if (!mounted) return;
        const launchSSO = location.pathname === "/login/sso";
        if (launchSSO) history.replaceState(null, "", "/");
        let next = initial;
        if (initial.mode === "oauth") {
          try {
            if (initial.configured && await finishDashboardAuthorization()) {
              next = await api<Session>("/auth/session");
              if (mounted && location.pathname === "/account") setPage("password");
            } else if (mounted && launchSSO && initial.configured && initial.oidc_label && !initial.authenticated) {
              await startOIDC(await beginDashboardSignIn());
            }
          } catch (error) {
            if (mounted) setSessionError((error as Error).message);
          }
        }
        if (mounted) {
          setAccount(next.account?.id ?? "");
          setSession(next);
          setCsrf(next.csrf || "");
        }
      })
      .catch((error: Error) => {
        if (mounted) {
          setSessionError(error.message);
        }
      });
    window.addEventListener("airkeep:session-ended", loggedOut);
    window.addEventListener("airkeep:account-changed", accountChanged);
    return () => {
      mounted = false;
      window.removeEventListener("airkeep:session-ended", loggedOut);
      window.removeEventListener("airkeep:account-changed", accountChanged);
    };
  }, []);
  useEffect(() => {
    const t = setInterval(() => setTick(Date.now() / 1000), 1000);
    return () => clearInterval(t);
  }, []);
  const refreshRules = async () => {
    try {
      setRules(await api<Rule[]>("/v2/alerts"));
      setRulesSynced(Date.now() / 1000);
      setRulesError("");
    } catch (e) {
      setRulesError((e as Error).message);
    }
  };
  const publicAccountFlow = session?.hosted && hostedAccountPaths.includes(location.pathname);
  useEffect(() => {
    if (!session?.authenticated || publicAccountFlow) return;
    let active = true;
    const load = async () => {
      if (document.hidden) return;
      if (session.mode === "oauth") {
        try {
          const latest = await api<Session>("/auth/session");
          if (!active) return;
          if (!latest.authenticated || latest.account?.id !== session.account?.id) {
            window.dispatchEvent(new Event("airkeep:account-changed"));
            return;
          }
          if (latest.account?.role !== session.account?.role) setSession(latest);
        } catch { return; }
      }
      try {
        const value = await api<Snapshot>("/v1/snapshot");
        if (active) {
          setSnapshot(value);
          setConnectionError("");
        }
      } catch {
        if (active)
          setConnectionError("Relay unavailable. Showing the last reading.");
      }
      try {
        const value = await api<Rule[]>("/v2/alerts");
        if (active) {
          setRules(value);
          setRulesSynced(Date.now() / 1000);
          setRulesError("");
        }
      } catch (e) {
        if (active) setRulesError((e as Error).message);
      }
    };
    void load();
    const t = setInterval(load, 5000);
    document.addEventListener("visibilitychange", load);
    window.addEventListener("focus", load);
    return () => {
      active = false;
      clearInterval(t);
      document.removeEventListener("visibilitychange", load);
      window.removeEventListener("focus", load);
    };
  }, [session?.authenticated, session?.account?.id, session?.account?.role, publicAccountFlow]);
  const enabledAlerts = rules.filter((rule) => rule.enabled).length;
  const reading = snapshot?.readings.find((r) => r.amsID === ams);
  const stale =
    !reading || tick - reading.observedAt > (snapshot?.staleAfter || 90);
  const units = [
    ...new Set([
      ams,
      ...(snapshot?.readings.map((r) => r.amsID) || []),
      ...rules.map((r) => r.amsID),
    ]),
  ].sort();
  const go = (target: Page) => {
    setPage(target);
    window.location.hash = target;
  };
  useEffect(() => {
    const sync = () => {
      const value = (location.hash.slice(1) || (location.pathname === "/account" ? "password" : "")) as Page;
      if (value === "password" || value === "accounts" || [...userPages, ...adminPages].some((p) => p[0] === value)) {
        setPage(value);
        setMode(value === "accounts" || adminPages.some((p) => p[0] === value) ? "admin" : "user");
      }
    };
    sync();
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);
  const oauth = session?.mode === "oauth";
  const canAdmin = !!session?.authenticated && (session.hosted
    ? session.account?.role === "admin"
    : !oauth || session.account?.role === "admin");
  const effectiveMode = canAdmin ? mode : "user";
  const cloudAdmin = session?.hosted && effectiveMode === "admin";
  const adminPage = adminPages.some(([key]) => key === page);
  const effectivePage = !canAdmin && (adminPage || page === "accounts") ? "overview" : page;
  if (publicAccountFlow) return <HostedAccountFlow publicSignup={session?.public_signup} />;
  if (!session?.authenticated && oauth)
    return <AccountLogin configured={session.configured} error={sessionError} oidcLabel={session.oidc_label}
      hosted={session.hosted} publicSignup={session.public_signup} passkeysAvailable={session.passkeys_available} />;
  if (!session?.authenticated)
    return (
      <Login
        session={session}
        error={sessionError}
        onLogin={(s) => {
          setSession(s);
          setCsrf(s.csrf || "");
          setSessionError("");
        }}
      />
    );
  if (location.pathname === "/device") {
    return oauth ? <DevicePairing /> :
      <main className="login"><p>Set up an Airkeep account to pair your display. <a href="/">Back to Airkeep</a></p></main>;
  }
  return (
    <div className="shell" key={session.account?.id ?? "local"}>
      <aside className="sidebar">
        <a
          className="brand"
          href="#overview"
          onClick={() => {
            setMode("user");
            go("overview");
          }}
        >
          <img src="/brand/mark.svg" alt="" />
          <span>Airkeep</span>
        </a>
        <nav aria-label="Main navigation">
          {(effectiveMode === "user" ? userPages : adminPages).map(
            ([id, label, Icon]) => (
              <button
                className={page === id ? "nav-item selected" : "nav-item"}
                key={id}
                onClick={() => go(id)}
              >
                <Icon size={25} weight="light" />
                <span>{label}</span>
              </button>
            ),
          )}
          {oauth && <button className={page === "accounts" || page === "password" ? "nav-item selected" : "nav-item"}
            onClick={() => go(effectiveMode === "admin" ? "accounts" : "password")}>
            <User size={25} weight="light" />
            <span>{effectiveMode === "admin" ? "Accounts" : "My account"}</span>
          </button>}
        </nav>
        <div className="sidebar-bottom">
          <span className="eyebrow">View</span>
          <div className="view-row">
            <User size={25} weight="light" />
            <div className="segmented modes">
              {(canAdmin ? ["user", "admin"] as const : ["user"] as const).map((m) => (
                <button
                  key={m}
                  className={effectiveMode === m ? "chosen" : ""}
                  onClick={() => {
                    setMode(m);
                    go(m === "user" ? "overview" : "relay");
                  }}
                >
                  {m === "user" ? "User" : "Admin"}
                </button>
              ))}
            </div>
          </div>
          <button
            className="logout"
            onClick={async () => {
              try {
                await api(oauth ? "/auth/revoke" : "/auth/logout", { method: "POST" });
                loggedOut();
              } catch (e) {
                setSessionError((e as Error).message);
              }
            }}
          >
            <SignOut size={18} />
            Sign out
          </button>
          {sessionError && <p className="error">{sessionError}</p>}
        </div>
      </aside>
      <main>
        <header className="topbar">
          {cloudAdmin ? <div className="topbar-left"><span>Cloud administration</span></div> : <div className="topbar-left">
            <div className="ams-select">
              <select
                aria-label="AMS"
                value={ams}
                onChange={(e) => setAms(e.target.value)}
              >
                {units.map((id) => (
                  <option key={id} value={id}>
                    {amsName(id)}
                  </option>
                ))}
              </select>
              <CaretDown size={20} />
            </div>
            <span
              className={
                "connection " + (stale || connectionError ? "waiting" : "")
              }
            >
              <span className="status-dot" />
              {connectionError
                ? "Relay unavailable"
                : stale
                  ? "Waiting for fresh data"
                  : snapshot?.printerConnected
                    ? session.hosted ? "Connected through relay" : "Connected locally"
                    : "Printer reconnecting"}
            </span>
          </div>}
          <div className="date">
            <span>
              {new Date(tick * 1000).toLocaleDateString([], {
                weekday: "long",
                day: "numeric",
                month: "long",
                year: "numeric",
              })}
            </span>
            <span>
              {new Date(tick * 1000).toLocaleTimeString([], {
                hour: "2-digit",
                minute: "2-digit",
                timeZoneName: "short",
              })}
            </span>
          </div>
        </header>
        {!cloudAdmin && connectionError && (
          <p className="notice" role="status">
            {connectionError}
          </p>
        )}
        {session.hosted && effectivePage === "overview" && <HostedSetup />}
        {cloudAdmin ? (
          <HostedAdmin directory={effectivePage === "accounts"}>
            {(accountId) => <Admin key={JSON.stringify([effectivePage, accountId])}
              page={effectivePage} hostedAccountId={accountId} />}
          </HostedAdmin>
        ) : oauth && (effectivePage === "password" || effectivePage === "accounts") ? (
          <AccountSettings key={effectivePage} admin={effectivePage === "accounts"} signedOut={loggedOut}
            oidcLabel={session.oidc_label} localPassword={session.account?.local_password} hosted={session.hosted} />
        ) : effectiveMode === "user" ? (
          <>
            {effectivePage === "overview" && (
              <>
                <section className="hero">
                  <h1 className="eyebrow">Relative humidity</h1>
                  <div className={"humidity " + (stale ? "stale" : "")}>
                    <strong>{reading?.humidity.toFixed(0) ?? "—"}</strong>
                    <span>% RH</span>
                  </div>
                  <div className="reading-details">
                    <div className="temperature">
                      <Thermometer size={32} weight="light" />
                      <div>
                        <strong>
                          {reading?.temperature != null
                            ? `${reading.temperature.toFixed(1)} °C`
                            : "—"}
                        </strong>
                        <span>Temperature</span>
                      </div>
                    </div>
                    <div className="updated">
                      <Clock size={25} weight="light" />
                      {reading
                        ? `Updated ${age(reading.observedAt, tick)}`
                        : "Waiting for a reading"}
                    </div>
                  </div>
                </section>
                <HistoryChart ams={ams} />
                <button className="alert-summary" onClick={() => go("alerts")}>
                  <Bell size={29} weight="light" />
                  <span>
                    {enabledAlerts} {enabledAlerts === 1 ? "alert" : "alerts"}{" "}
                    enabled
                  </span>
                  <span className="text-action">
                    View alerts <CaretRight size={20} />
                  </span>
                </button>
              </>
            )}
            {effectivePage === "history" && (
              <>
                <div className="page-heading">
                  <h1>History</h1>
                  <p>Humidity over the last seven days.</p>
                </div>
                <HistoryChart ams={ams} expanded />
              </>
            )}
            {effectivePage === "alerts" && (
              <Alerts
                rules={rules}
                units={units}
                ams={ams}
                refresh={refreshRules}
                error={rulesError}
                synced={rulesSynced}
                edit={setEditor}
              />
            )}
          </>
        ) : (
          <Admin key={effectivePage} page={effectivePage} />
        )}
        <footer>
          Airkeep <span>Readings from your relay</span>
        </footer>
      </main>
      {editor && (
        <RuleEditor
          key={editor.rule.id}
          initial={editor.rule}
          creating={editor.creating}
          rules={rules}
          units={units}
          offline={!!rulesError}
          close={() => setEditor(null)}
          saved={refreshRules}
        />
      )}
    </div>
  );
}

function Login({
  session,
  error,
  onLogin,
}: {
  session: Session | null;
  error: string;
  onLogin: (s: Session) => void;
}) {
  const [password, setPassword] = useState(""),
    [message, setMessage] = useState(""),
    [busy, setBusy] = useState(false);
  return (
    <div className="login">
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          try {
            onLogin(
              await api<Session>("/auth/login", {
                method: "POST",
                body: JSON.stringify({ password }),
              }),
            );
            setPassword("");
          } catch (e) {
            setMessage((e as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        <img src="/brand/mark.svg" alt="" />
        <h1>Airkeep</h1>
        <p>Sign in to your relay.</p>
        {session?.configured === false ? (
          <div className="notice">
            Set up a browser password on your relay to get started.
          </div>
        ) : (
          <>
            <label htmlFor="password">Password</label>
            <input
              autoFocus
              id="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
            <button className="primary" disabled={busy || !session}>
              {busy ? "Signing in…" : "Sign in"}
            </button>
          </>
        )}
        {(message || error) && (
          <p className="error" role="alert">
            {message || error}
          </p>
        )}
      </form>
    </div>
  );
}

function HistoryChart({
  ams,
  expanded = false,
}: {
  ams: string;
  expanded?: boolean;
}) {
  const summaryID = useId();
  const [hours, setHours] = useState(168),
    [data, setData] = useState<History | null>(null),
    [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    const load = async () => {
      if (document.hidden) return;
      try {
        const h = await api<History>(
          `/v2/history?ams=${encodeURIComponent(ams)}&hours=${hours}`,
        );
        if (active) {
          setData(h);
          setError("");
        }
      } catch (e) {
        if (active) setError((e as Error).message);
      }
    };
    setData(null);
    void load();
    const t = setInterval(load, 30000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, [ams, hours]);
  const points =
    data?.series.flatMap((s, i) =>
      s.points.map((p) => ({ ...p, [`segment${i}`]: p.humidityMean })),
    ) || [];
  const low = points.length ? Math.min(...points.map((p) => p.humidityMin)) : 0;
  const high = points.length ? Math.max(...points.map((p) => p.humidityMax)) : 0;
  const padding = Math.max(2, (10 - (high - low)) / 2);
  const span = high - low + 2 * padding;
  const step = [2, 5, 10, 20].find((value) => span <= value * 6) ?? 20;
  const lower = Math.min(
    90, Math.max(0, Math.floor((low - padding) / step) * step),
  );
  const upper = Math.max(
    10, Math.min(100, Math.ceil((high + padding) / step) * step),
  );
  const ticks = Array.from(
    { length: Math.floor((upper - lower) / step) + 1 },
    (_, index) => lower + index * step,
  );
  const formatHumidity = new Intl.NumberFormat(undefined, {
    maximumFractionDigits: 1,
  });
  const observedRange = low === high
    ? formatHumidity.format(low)
    : `${formatHumidity.format(low)}–${formatHumidity.format(high)}`;
  const historySummary = points.length
    ? `${amsName(ams)} humidity over the past ${hours === 168 ? "seven days" : `${hours} ${hours === 1 ? "hour" : "hours"}`}. Minimum ${formatHumidity.format(low)} percent, maximum ${formatHumidity.format(high)} percent. Latest source reading ${when(points[points.length - 1].lastObservedAt)}. ${data?.series.length && data.series.length > 1 ? "Gaps indicate interruptions in readings." : ""}`
    : "Humidity history is loading or empty.";
  return (
    <section className={"history " + (expanded ? "expanded" : "")}>
      <div className="section-heading">
        <h2 className="eyebrow">Humidity history</h2>
        <div className="segmented ranges" aria-label="History range">
          {[
            [1, "1h"],
            [6, "6h"],
            [24, "24h"],
            [168, "7d"],
          ].map(([value, label]) => (
            <button
              key={value}
              className={hours === value ? "chosen" : ""}
              onClick={() => setHours(Number(value))}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
      {error && <p className="error">{error}</p>}
      {points.length > 0 && (
        <p className="history-range">Range · {observedRange}% RH</p>
      )}
      <p className="sr-only" id={summaryID} aria-live="polite">
        {historySummary}
      </p>
      <div
        className="chart"
        role="region"
        aria-label={`${amsName(ams)} humidity history`}
        aria-describedby={summaryID}
      >
        {points.length > 0 ? (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart
              data={points}
              margin={{ top: 12, right: 10, left: 0, bottom: 8 }}
            >
              <CartesianGrid stroke="#30312f" vertical={false} />
              <XAxis
                type="number"
                dataKey="lastObservedAt"
                domain={[data?.from || 0, data?.to || 0]}
                ticks={undefined}
                minTickGap={65}
                tickFormatter={(v) =>
                  new Date(v * 1000).toLocaleString([], {
                    ...(hours > 24
                      ? { day: "numeric", month: "short" }
                      : { hour: "2-digit", minute: "2-digit" }),
                  })
                }
                stroke="#767772"
                tickLine={false}
              />
              <YAxis
                domain={[lower, upper]}
                ticks={ticks}
                width={45}
                tickLine={false}
                axisLine={false}
                stroke="#a2a39d"
              />
              <Tooltip
                content={({ active, payload }) => {
                  const p = payload?.[0]?.payload;
                  return active && p ? (
                    <div className="chart-tooltip">
                      <strong>{p.humidityMean.toFixed(1)}% RH</strong>
                      <span>{when(p.lastObservedAt)}</span>
                      {hours > 24 && (
                        <span>
                          {p.humidityMin.toFixed(1)}–{p.humidityMax.toFixed(1)}%
                          range · {p.sampleCount} readings
                        </span>
                      )}
                    </div>
                  ) : null;
                }}
              />
              {data?.series.map((_, i) => (
                <Line
                  key={i}
                  type="linear"
                  dataKey={`segment${i}`}
                  stroke="#ffb82e"
                  strokeWidth={1.5}
                  dot={false}
                  isAnimationActive={false}
                  connectNulls={false}
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <div className="empty">
            <ChartLine size={34} weight="light" />
            <p>
              {data
                ? "History appears as readings arrive."
                : "Loading history…"}
            </p>
          </div>
        )}
      </div>
    </section>
  );
}

function Alerts({
  rules,
  units,
  ams,
  refresh,
  error,
  synced,
  edit,
}: {
  rules: Rule[];
  units: string[];
  ams: string;
  refresh: () => Promise<void>;
  error: string;
  synced: number | null;
  edit: (editor: { rule: Rule; creating: boolean }) => void;
}) {
  const [message, setMessage] = useState(""),
    [busy, setBusy] = useState(""),
    [events, setEvents] = useState<AlertEvent[]>([]),
    [cursor, setCursor] = useState<string | null>(null),
    [eventsError, setEventsError] = useState("");
  const [filterAMS, setFilterAMS] = useState("");
  const [filterRule, setFilterRule] = useState("");
  const [filters, setFilters] = useState<EventFilters>({
    amsIDs: units,
    rules: rules.map((rule) => ({
      id: rule.id,
      name: rule.name,
      deleted: false,
    })),
  });
  const [eventsLoading, setEventsLoading] = useState(false);
  const requestVersion = useRef(0);
  const requestActive = useRef(false);
  const olderPagesLoaded = useRef(false);
  const loadEvents = async (more = false) => {
    if (more && !cursor) return;
    const version = ++requestVersion.current;
    requestActive.current = true;
    setEventsLoading(true);
    const query = new URLSearchParams();
    if (filterAMS) query.set("ams", filterAMS);
    if (filterRule) query.set("rule_id", filterRule);
    if (more && cursor) query.set("cursor", cursor);
    try {
      const page = await api<EventPage>("/v2/alert-events?" + query.toString());
      if (version !== requestVersion.current) return;
      if (more) {
        olderPagesLoaded.current = true;
        setEvents((existing) =>
          Array.from(
            new Map(
              [...existing, ...page.events].map((event) => [event.id, event]),
            ).values(),
          ),
        );
      } else {
        olderPagesLoaded.current = false;
        setEvents(page.events);
      }
      if (page.filters) setFilters(page.filters);
      setCursor(page.nextCursor);
      setEventsError("");
    } catch (error) {
      if (version === requestVersion.current)
        setEventsError((error as Error).message);
    } finally {
      if (version === requestVersion.current) {
        requestActive.current = false;
        setEventsLoading(false);
      }
    }
  };
  useEffect(() => {
    ++requestVersion.current;
    olderPagesLoaded.current = false;
    setCursor(null);
    setEvents([]);
    void loadEvents();
    const timer = setInterval(() => {
      if (
        !document.hidden &&
        !requestActive.current &&
        !olderPagesLoaded.current
      )
        void loadEvents();
    }, 10000);
    return () => {
      clearInterval(timer);
      ++requestVersion.current;
      requestActive.current = false;
    };
  }, [filterAMS, filterRule]);
  const save = async (rule: Rule) => {
    setBusy(rule.id);
    try {
      await api(`/v2/alerts/${rule.id}`, {
        method: "PUT",
        headers: { "If-Match": String(rule.revision) },
        body: JSON.stringify(rule),
      });
      await refresh();
    } catch (e) {
      setMessage((e as Error).message);
    } finally {
      setBusy("");
    }
  };
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Humidity alerts</h1>
          <p>Your alerts sync across devices through this relay.</p>
        </div>
        <button
          className="primary"
          disabled={!!error || synced === null}
          onClick={() => {
            let n = 1;
            while (rules.some((r) => r.name === `Alert ${n}`)) n++;
            edit({
              creating: true,
              rule: {
                id: crypto.randomUUID(),
                revision: 1,
                name: `Alert ${n}`,
                enabled: false,
                amsID: ams,
                threshold: 30,
                holdSeconds: 300,
                hysteresis: 3,
                active: false,
              },
            });
          }}
        >
          <Plus size={18} />
          Add alert
        </button>
      </div>
      {(message || error) && (
        <p className="notice" role="alert">
          {message || error}
          {error && " Showing cached rules."}
          <button
            onClick={() => {
              setMessage("");
              void refresh();
            }}
          >
            Refresh
          </button>
        </p>
      )}
      {synced && <p className="sync">Last synced {when(synced)}</p>}
      <div className="rule-list">
        {rules.length === 0 && (
          <p className="empty">
            Add your first alert to choose a humidity limit and hold time.
          </p>
        )}
        {rules.map((rule) => (
          <article className="rule" key={rule.id}>
            <div>
              <h2>{rule.name}</h2>
              <p>
                {amsName(rule.amsID)} · Above {rule.threshold}% RH{" "}
                {rule.holdSeconds
                  ? `for ${rule.holdSeconds / 60} min`
                  : "immediately"}
              </p>
              <span className={rule.active ? "amber" : ""}>
                {!rule.enabled
                  ? "Off"
                  : rule.active
                    ? "Humidity above limit"
                    : "Watching humidity"}
              </span>
            </div>
            <div className="rule-actions">
              <label className="switch">
                <input
                  aria-label={`Enable ${rule.name}`}
                  type="checkbox"
                  checked={rule.enabled}
                  disabled={!!error || busy === rule.id}
                  onChange={(e) =>
                    void save({ ...rule, enabled: e.target.checked })
                  }
                />
                <span />
              </label>
              <div>
                <button onClick={() => edit({ rule, creating: false })}>
                  Edit
                </button>
                <button
                  className="danger"
                  disabled={!!error || busy === rule.id}
                  onClick={async () => {
                    if (!confirm(`Delete ${rule.name} from all devices?`))
                      return;
                    setBusy(rule.id);
                    try {
                      await api(`/v2/alerts/${rule.id}`, {
                        method: "DELETE",
                        headers: { "If-Match": String(rule.revision) },
                      });
                      await refresh();
                    } catch (e) {
                      setMessage((e as Error).message);
                    } finally {
                      setBusy("");
                    }
                  }}
                >
                  Delete
                </button>
              </div>
            </div>
          </article>
        ))}
      </div>
      <div className="section-heading timeline-heading">
        <h2>
          Past alerts <small>Last seven days</small>
        </h2>
        <button onClick={() => void loadEvents()}>
          <ArrowClockwise size={18} />
          Refresh
        </button>
      </div>
      <div className="event-filters">
        <label>
          AMS
          <select
            aria-label="Filter past alerts by AMS"
            value={filterAMS}
            onChange={(event) => setFilterAMS(event.target.value)}
          >
            <option value="">All AMS</option>
            {filters.amsIDs.map((id) => (
              <option key={id} value={id}>
                {amsName(id)}
              </option>
            ))}
          </select>
        </label>
        <label>
          Alert
          <select
            aria-label="Filter past alerts by rule"
            value={filterRule}
            onChange={(event) => setFilterRule(event.target.value)}
          >
            <option value="">All rules</option>
            {filters.rules.map((rule) => (
              <option key={rule.id} value={rule.id}>
                {rule.name}
                {rule.deleted ? " (past)" : ""}
              </option>
            ))}
          </select>
        </label>
      </div>
      {eventsError && <p className="error">{eventsError}</p>}
      <div className="timeline">
        {events.length === 0 ? (
          <p className="empty">
            {eventsLoading
              ? "Loading past alerts…"
              : filterAMS || filterRule
                ? "No past alerts match these filters."
                : "Your next humidity alert will appear here."}
          </p>
        ) : (
          events.map((event) => (
            <article key={event.id}>
              <Bell
                size={22}
                weight="light"
                className={event.type === "high" ? "amber" : ""}
              />
              <div>
                <strong>{event.name}</strong>
                <span>
                  {amsName(event.amsID)} ·{" "}
                  {event.type === "high" ? "Triggered" : "Recovered"} at{" "}
                  {event.reading.humidity}% RH
                </span>
              </div>
              <time>{when(event.occurredAt)}</time>
            </article>
          ))
        )}
        {cursor && (
          <button
            className="load-more"
            disabled={eventsLoading}
            onClick={() => void loadEvents(true)}
          >
            {eventsLoading ? "Loading…" : "Load earlier alerts"}
          </button>
        )}
      </div>
    </>
  );
}

function RuleEditor({
  initial,
  creating,
  rules,
  units,
  offline,
  close,
  saved,
}: {
  initial: Rule;
  creating: boolean;
  rules: Rule[];
  units: string[];
  offline: boolean;
  close: () => void;
  saved: () => Promise<void>;
}) {
  const [draft, setDraft] = useState(initial),
    [baseline, setBaseline] = useState(initial),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [conflict, setConflict] = useState(false),
    [removed, setRemoved] = useState(false);
  const modal = useRef<HTMLElement>(null);
  const dirty = JSON.stringify(draft) !== JSON.stringify(baseline);
  const cancel = () => {
    if (busy) return;
    if (removed || !dirty || confirm("Discard your changes?")) close();
  };
  useEffect(() => {
    if (creating || busy || offline) return;
    const latest = rules.find((r) => r.id === initial.id);
    if (!latest) {
      setRemoved(true);
      setError("This alert was removed on another device.");
    } else if (latest.revision !== baseline.revision) {
      setConflict(true);
      setError(
        "This alert changed on another device. Reload the latest rule before saving.",
      );
    }
  }, [rules, creating, busy, offline, initial.id, baseline.revision]);
  useEffect(() => {
    const leave = (e: BeforeUnloadEvent) => {
      if (dirty) {
        e.preventDefault();
      }
    };
    window.addEventListener("beforeunload", leave);
    return () => window.removeEventListener("beforeunload", leave);
  }, [dirty]);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = overflow;
      previous?.focus();
    };
  }, []);
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        cancel();
      }
      if (event.key === "Tab") {
        const items = Array.from(
          modal.current?.querySelectorAll<HTMLElement>(
            "button:not(:disabled), input:not(:disabled), select:not(:disabled), a[href]",
          ) || [],
        );
        const first = items[0],
          last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
    };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, [dirty, busy, removed]);
  return (
    <div
      className="modal-shade"
      onClick={(e) => {
        if (e.target === e.currentTarget) cancel();
      }}
    >
      <section
        className="modal"
        ref={modal}
        role="dialog"
        aria-modal="true"
        aria-labelledby="editor-title"
      >
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            try {
              await api(creating ? "/v2/alerts" : `/v2/alerts/${draft.id}`, {
                method: creating ? "POST" : "PUT",
                headers: creating ? {} : { "If-Match": String(draft.revision) },
                body: JSON.stringify({ ...draft, name: draft.name.trim() }),
              });
              await saved();
              close();
            } catch (e) {
              setError((e as Error).message);
              setConflict(e instanceof ApiError && e.status === 412);
              setRemoved(
                !creating && e instanceof ApiError && e.status === 404,
              );
            } finally {
              setBusy(false);
            }
          }}
        >
          <div className="modal-header">
            <h2 id="editor-title">
              {creating ? "New humidity alert" : "Edit humidity alert"}
            </h2>
            <button
              type="button"
              aria-label="Close editor"
              onClick={cancel}
              disabled={busy}
            >
              <X size={23} />
            </button>
          </div>
          <label>
            Name
            <input
              autoFocus
              value={draft.name}
              maxLength={80}
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            />
          </label>
          <label className="inline-label">
            Enabled
            <input
              type="checkbox"
              checked={draft.enabled}
              onChange={(e) =>
                setDraft({ ...draft, enabled: e.target.checked })
              }
            />
          </label>
          <label>
            AMS
            <select
              value={draft.amsID}
              onChange={(e) => setDraft({ ...draft, amsID: e.target.value })}
            >
              {[...new Set([...units, draft.amsID])].map((id) => (
                <option key={id} value={id}>
                  {amsName(id)}
                </option>
              ))}
            </select>
          </label>
          <label>
            Alert above (% RH)
            <input
              type="number"
              min={5}
              max={95}
              step={1}
              required
              value={draft.threshold}
              onChange={(e) => {
                const threshold = Number(e.target.value);
                setDraft({
                  ...draft,
                  threshold,
                  hysteresis: Math.min(draft.hysteresis, threshold - 1),
                });
              }}
            />
          </label>
          <label>
            Stay above limit for
            <select
              value={draft.holdSeconds}
              onChange={(e) =>
                setDraft({ ...draft, holdSeconds: Number(e.target.value) })
              }
            >
              {[...new Set([0, 60, 300, 900, draft.holdSeconds])]
                .sort((a, b) => a - b)
                .map((v) => (
                  <option key={v} value={v}>
                    {v === 0
                      ? "Immediately"
                      : `${v / 60} ${v === 60 ? "minute" : "minutes"}`}
                  </option>
                ))}
            </select>
          </label>
          <label>
            Recovery margin (% RH)
            <input
              type="number"
              min={1}
              max={Math.min(20, draft.threshold - 1)}
              step={1}
              required
              value={draft.hysteresis}
              onChange={(e) =>
                setDraft({ ...draft, hysteresis: Number(e.target.value) })
              }
            />
          </label>
          <p className="muted small">
            Get an alert when humidity stays above your limit, then another when
            it recovers. Changing the condition restarts the hold time.
          </p>
          {offline && (
            <p className="notice">
              Reconnect to save. Your draft will stay here.
            </p>
          )}
          {error && (
            <div className="notice" role="alert">
              {error}
              {conflict && (
                <button
                  type="button"
                  onClick={async () => {
                    try {
                      const value = await api<Rule>(`/v2/alerts/${draft.id}`);
                      setDraft(value);
                      setBaseline(value);
                      setConflict(false);
                      setError("");
                    } catch (e) {
                      setError((e as Error).message);
                      if (e instanceof ApiError && e.status === 404)
                        setRemoved(true);
                    }
                  }}
                >
                  Reload latest
                </button>
              )}
            </div>
          )}
          <div className="modal-footer">
            <button type="button" disabled={busy} onClick={cancel}>
              {removed ? "Close" : "Cancel"}
            </button>
            <button
              className="primary"
              disabled={
                busy || offline || removed || conflict || !draft.name.trim()
              }
            >
              {busy ? "Saving…" : "Save"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}

type HostedHealth = {
  version: string;
  uptimeSeconds: number;
  database: { status: string; bytes: number };
  pushSetup: RecordRow[];
  email: { configured: boolean; counts: Record<string, number> };
};

function Admin({ page, hostedAccountId }: { page: Page; hostedAccountId?: string | null }) {
  const [data, setData] = useState<RecordRow | null>(null),
    [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const hosted = hostedAccountId !== undefined;
  const base = hostedAccountId
    ? `${hostedAdminPath}/accounts/${encodeURIComponent(hostedAccountId)}`
    : "/v2/admin";
  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    setData(null);
    setError("");
    const load = async () => {
      if (!hosted && document.hidden) return;
      try {
        const sections =
          page === "relay"
            ? ["overview"]
            : page === "devices"
              ? ["devices", "widgets"]
              : page === "activities"
                ? ["activities"]
                : page === "queue"
                  ? ["queue", "widgets"]
                  : hosted ? ["push-attempts"] : ["overview", "push-attempts"];
        const paths = hosted && !hostedAccountId ? [] : sections.map((section) => [section, `${base}/${section}`]);
        if (hosted && (page === "relay" || page === "push")) {
          paths.push(["health", `${hostedAdminPath}/health`]);
        }
        const values = await Promise.all(paths.map(async ([section, path]) =>
          [section, await api(path, { signal: controller.signal })] as const,
        ));
        if (active) {
          setData(Object.fromEntries(values));
          setError("");
        }
      } catch (e) {
        if (active) setError((e as Error).message);
      }
    };
    void load();
    const timer = hosted ? undefined : setInterval(load, 10000);
    return () => {
      active = false;
      controller.abort();
      if (timer !== undefined) clearInterval(timer);
    };
  }, [base, hosted, hostedAccountId, page, refresh]);
  const overview = data?.overview as RecordRow | undefined;
  const health = data?.health as HostedHealth | undefined;
  const relayState = {
    unpaired: "Not paired",
    relay_offline: "Relay offline",
    printer_disconnected: "Printer disconnected",
    waiting_for_ams: "Waiting for AMS readings",
    stale_readings: "AMS readings are stale",
    connected: "Connected",
  }[String(overview?.relayStatus)];
  const title = {
    overview: "Overview",
    history: "History",
    alerts: "Alerts",
    relay: "Relay",
    devices: "Registered devices",
    activities: "Live Activities",
    push: "Apple push",
    queue: "Pending queue",
    accounts: "Accounts",
    password: "Change password",
  }[page];
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">Admin</span>
          <h1>{title}</h1>
        </div>
        {hosted && <button className="load-more" onClick={() => setRefresh((value) => value + 1)}>Refresh</button>}
      </div>
      {error && <p className="notice" role="alert">{error}</p>}
      {!data && !error && <p className="empty">Loading…</p>}
      {page === "relay" && health && <>
        <div className="stat-grid">
          {[
            ["Service", health.database.status === "ok" ? "Healthy" : "Needs attention"],
            ["API uptime", `${Math.floor(health.uptimeSeconds / 3600)} hr ${Math.floor((health.uptimeSeconds % 3600) / 60)} min`],
            ["PostgreSQL", `${(health.database.bytes / 1024 / 1024).toFixed(1)} MB`],
            ["Email", health.email.configured ? "Configured" : "Needs setup"],
          ].map(([name, value]) => <section className="stat" key={name}>
            <span className="eyebrow">{name}</span><strong>{value}</strong>
          </section>)}
        </div>
        <DataTable title="Email queue" rows={Object.entries(health.email.counts).map(([state, count]) => ({ state, count }))} />
      </>}
      {hosted && !hostedAccountId && data && <p className="empty">Choose an account to view its diagnostics.</p>}
      {page === "relay" && overview && (
        <>
          <div className="stat-grid">
            {[
              [
                "Printer",
                relayState ?? (overview.printerConnected ? "Connected" : "Reconnecting"),
              ],
              ...(hosted ? [] : [[
                "Uptime",
                `${Math.floor(Number(overview.uptimeSeconds) / 3600)} hr ${Math.floor((Number(overview.uptimeSeconds) % 3600) / 60)} min`,
              ],
              [
                "Database",
                `${(Number(overview.databaseBytes) / 1024 / 1024).toFixed(1)} MB`,
              ],
              ["History", `${overview.historyDays} days`]]),
            ].map(([name, value]) => (
              <section className="stat" key={String(name)}>
                <span className="eyebrow">{name}</span>
                <strong>{value}</strong>
              </section>
            ))}
          </div>
          <DataTable
            title="Storage"
            rows={Object.entries(overview.counts as RecordRow).map(
              ([name, count]) => ({
                name:
                  {
                    samples: "Stored readings",
                    devices: "Apple devices",
                    activities: "Live Activities",
                    alert_rules: "Humidity rules",
                    outbox: "Pending notifications",
                  }[name] || name,
                count,
              }),
            )}
          />
          <p className="muted small">
            Oldest AMS reading:{" "}
            {typeof overview.readingAgeSeconds === "number"
              ? age(Date.now() / 1000 - overview.readingAgeSeconds)
              : "Unknown"}
          </p>
          <p className="muted small">{hosted
            ? `Airkeep ${health?.version ?? ""}`
            : `Relay ${String(overview.version)} · Alert history started ${when(Number(overview.timelineStartedAt))}`}
          </p>
        </>
      )}
      {page === "devices" && data && (!hosted || hostedAccountId) && (
        <>
          <DataTable title="Apple devices" rows={data.devices as RecordRow[]} />
          <DataTable title="Watch widgets" rows={data.widgets as RecordRow[]} />
        </>
      )}
      {page === "activities" && data && (!hosted || hostedAccountId) && (
        <DataTable
          title="Registered sessions"
          rows={data.activities as RecordRow[]}
        />
      )}{" "}
      {page === "queue" && data && (!hosted || hostedAccountId) && (
        <>
          <DataTable
            title="Humidity notifications"
            rows={data.queue as RecordRow[]}
          />
          <DataTable
            title="Widget refreshes"
            rows={(Array.isArray(data.widgets) ? data.widgets : []).filter(
              (r: RecordRow) => r.pending,
            )}
          />
        </>
      )}
      {page === "push" && data && (
        <>
          <DataTable
            title="Push configuration"
            rows={(health?.pushSetup ?? overview?.pushSetup ?? []) as RecordRow[]}
          />
          {(!hosted || hostedAccountId) && <PushAttempts
            endpoint={`${base}/push-attempts`}
            initial={
              data["push-attempts"] as {
                attempts: RecordRow[];
                nextCursor: number | null;
              }
            }
          />}
        </>
      )}
    </>
  );
}
const labels: Record<string, string> = {
  id: "ID",
  platform: "Platform",
  topic: "App topic",
  environment: "Environment",
  receiveAlerts: "Receive alerts",
  tokenFingerprint: "Token fingerprint",
  createdAt: "Registered",
  lastRegisteredAt: "Last registered",
  generation: "Generation",
  active: "Active",
  amsIDs: "AMS",
  updatedAt: "Updated",
  lastAcceptedAt: "Last accepted",
  lastStatus: "Status",
  lastReason: "Reason",
  pending: "Pending",
  nextAttemptAt: "Next attempt",
  expiresAt: "Ends",
  amsID: "AMS",
  configuration: "Configuration",
  reason: "Details",
  at: "Time",
  kind: "Type",
  destination: "Destination",
  fingerprint: "Token fingerprint",
  status: "Status",
  apns_id: "APNs request ID",
  ruleID: "Rule",
  deviceID: "Device",
  attempts: "Attempts",
  name: "Name",
  count: "Count",
};
function cell(key: string, value: unknown): string {
  if (value === null || value === undefined) return "Unknown";
  if (
    [
      "at",
      "createdAt",
      "lastRegisteredAt",
      "updatedAt",
      "lastAcceptedAt",
      "nextAttemptAt",
      "expiresAt",
    ].includes(key)
  )
    return Number(value) > 0 ? when(Number(value)) : "—";
  if (key === "platform")
    return (
      { ios: "iOS", mac: "macOS", watch: "watchOS" }[String(value)] ||
      String(value)
    );
  if (key === "environment")
    return value === "sandbox" ? "Development" : "Production";
  if (key === "amsID") return amsName(String(value));
  if (key === "amsIDs")
    return (value as string[]).map(amsName).join(", ") || "None";
  if (key === "status" || key === "lastStatus")
    return value === 200 ? "Accepted by Apple" : String(value);
  if (typeof value === "boolean") return value ? "On" : "Off";
  if (key === "configuration")
    return (
      {
        available: "Configured",
        missing: "Needs setup",
        invalid: "Needs attention",
      }[String(value)] || String(value)
    );
  if (key === "reason" || key === "lastReason")
    return (
      {
        local_checks_passed: "Ready",
        credentials_missing: "Add push credentials",
        key_unreadable: "Check key permissions",
        key_invalid: "Check signing key",
        topic_mismatch: "Check app topic",
        connection_failed: "Connection failed",
      }[String(value)] || String(value)
    );
  return String(value);
}
function DataTable({ title, rows }: { title: string; rows: RecordRow[] }) {
  const safeRows = Array.isArray(rows) ? rows : [];
  const columns = safeRows.length ? Object.keys(safeRows[0]) : [];
  return (
    <section className="data-section">
      <h2>{title}</h2>
      {!safeRows.length ? (
        <p className="empty">Nothing to show yet.</p>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                {columns.map((key) => (
                  <th key={key}>{labels[key] || key}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {safeRows.map((row, i) => (
                <tr key={String(row.id ?? i)}>
                  {columns.map((key) => (
                    <td key={key} title={cell(key, row[key])}>
                      {cell(key, row[key])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
function PushAttempts({
  initial,
  endpoint,
}: {
  initial?: { attempts?: RecordRow[]; nextCursor?: number | null };
  endpoint: string;
}) {
  const incoming = Array.isArray(initial?.attempts) ? initial.attempts : [];
  const [rows, setRows] = useState(incoming),
    [cursor, setCursor] = useState(initial?.nextCursor ?? null),
    [error, setError] = useState("");
  const paged = useRef(false);
  useEffect(() => {
    setRows((existing) =>
      paged.current
        ? Array.from(
            new Map(
              [...incoming, ...existing].map((row) => [row.id, row]),
            ).values(),
          ).sort((a, b) => Number(b.id) - Number(a.id))
        : incoming,
    );
    if (!paged.current) setCursor(initial?.nextCursor ?? null);
  }, [initial]);
  return (
    <>
      <DataTable title="Recent push requests" rows={rows} />
      {error && <p className="error">{error}</p>}
      {cursor && (
        <button
          className="load-more"
          onClick={async () => {
            try {
              const page = await api<{
                attempts: RecordRow[];
                nextCursor: number | null;
              }>(`${endpoint}?before=${cursor}`);
              paged.current = true;
              setRows((v) => [...v, ...page.attempts]);
              setCursor(page.nextCursor);
            } catch (e) {
              setError((e as Error).message);
            }
          }}
        >
          Load earlier requests
        </button>
      )}
    </>
  );
}
