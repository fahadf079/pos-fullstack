import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { Cancelled, PinError, hooks, req } from "./api";
import type { Auth } from "./api";
import "./security.css";

export type Role = "guest" | "employee" | "owner" | "developer";
export const RANK: Record<Role, number> = { guest: 0, employee: 1, owner: 2, developer: 3 };
export interface Me { id: number; username: string; full_name: string; role: Role; two_factor: boolean }
interface Status { needs_setup: boolean; user: Me | null; locked: boolean; guest_enabled: boolean; lock_seconds: number }

interface AuthApi {
  me: Me;
  logout: () => Promise<void>;
  /** Lock the screen right now (it also locks by itself after the inactivity time). */
  lock: () => void;
  /** Bumps every time the screen is unlocked, so pages refetch what they could not read while locked. */
  epoch: number;
  /** Run an action that may need the employee's own PIN: tries it, and if the server asks, shows the PIN box and retries. */
  withPin: <T>(label: string, fn: (auth?: Auth) => Promise<T>) => Promise<T>;
}
const AuthCtx = createContext<AuthApi | null>(null);
export function useAuth(): AuthApi {
  const c = useContext(AuthCtx);
  if (!c) throw new Error("useAuth must be used inside <AuthGate>");
  return c;
}

interface Dlg { id: number; label: string; message: string; resolve: (a: Auth | null) => void }

/** Number keys for a touch screen; the physical keyboard works too. */
function Keypad({ onKey }: { onKey: (k: string) => void }) {
  return (
    <div className="sx-keypad">
      {["1", "2", "3", "4", "5", "6", "7", "8", "9", "⌫", "0", "OK"].map((k) => (
        <button key={k} type="button" className={k === "OK" ? "primary" : ""} onClick={() => onKey(k)}>{k}</button>
      ))}
    </div>
  );
}

/** A masked PIN field + keypad. Calls onSubmit with the digits on OK / Enter. */
function PinEntry({ onSubmit, disabled, autoFocus = true }: { onSubmit: (pin: string) => void; disabled?: boolean; autoFocus?: boolean }) {
  const [pin, setPin] = useState("");
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => { if (autoFocus) ref.current?.focus(); }, [autoFocus]);
  const key = (k: string) => {
    if (k === "⌫") setPin((p) => p.slice(0, -1));
    else if (k === "OK") { if (/^\d{4,8}$/.test(pin)) { onSubmit(pin); setPin(""); } }
    else setPin((p) => (p.length < 8 ? p + k : p));
  };
  return (
    <form onSubmit={(e) => { e.preventDefault(); key("OK"); }}>
      <input ref={ref} className="sx-pin" type="password" inputMode="numeric" autoComplete="off" maxLength={8} value={pin} placeholder="PIN" disabled={disabled}
        onChange={(e) => setPin(e.target.value.replace(/\D/g, ""))} />
      <Keypad onKey={key} />
    </form>
  );
}

/** The confirm box: asks for the employee's own PIN. */
function PinDialog({ dlg }: { dlg: Dlg }) {
  return (
    <div className="sx-overlay" role="dialog" aria-modal="true" onKeyDown={(e) => e.key === "Escape" && dlg.resolve(null)}>
      <div className="sx-dialog">
        <h3>{dlg.label}</h3>
        <PinEntry onSubmit={(pin) => dlg.resolve({ pin })} />
        {dlg.message && <div className="err">{dlg.message}</div>}
        <button type="button" onClick={() => dlg.resolve(null)}>Cancel</button>
      </div>
    </div>
  );
}

function Card({ title, children, wide }: { title: string; children: ReactNode; wide?: boolean }) {
  return <div className="sx-screen"><div className={"sx-card" + (wide ? " wide" : "")}><div className="sx-brand">POS</div><h2>{title}</h2>{children}</div></div>;
}

interface Person { username: string; name: string }

function LoginScreen({ notice, guest, onDone }: { notice: string; guest: boolean; onDone: (m: Me) => void }) {
  const [tab, setTab] = useState<"employee" | "owner">("employee");
  const [people, setPeople] = useState<Person[] | null>(null), [who, setWho] = useState<Person | null>(null);
  const [u, setU] = useState(""), [p, setP] = useState(""), [code, setCode] = useState(""), [needCode, setNeedCode] = useState(false);
  const [err, setErr] = useState(""), [busy, setBusy] = useState(false);
  useEffect(() => { req<{ people: Person[] }>("/auth/people").then((r) => setPeople(r.people)).catch(() => setPeople([])); }, []);

  const pinGo = async (pin: string) => {
    if (!who) return;
    setErr(""); setBusy(true);
    try { onDone((await req<{ user: Me }>("/auth/pin-login", { username: who.username, pin })).user); }
    catch (x) { setErr((x as Error).message); }
    setBusy(false);
  };
  const ownerGo = async (e: FormEvent) => {
    e.preventDefault(); setErr(""); setBusy(true);
    try { onDone((await req<{ user: Me }>("/auth/login", { username: u.trim(), password: p, code: code.trim() || undefined })).user); }
    catch (x) {
      const m = (x as Error).message;
      if (m.startsWith("Enter the 6-digit code")) { setNeedCode(true); setErr(""); }          // password was right: now the 2FA code
      else { setErr(m); setP(""); setCode(""); setNeedCode(false); }
    }
    setBusy(false);
  };
  const guestGo = async () => { try { onDone((await req<{ user: Me }>("/auth/guest", {})).user); } catch (x) { setErr((x as Error).message); } };

  return (
    <Card title="Sign in" wide={tab === "employee"}>
      {notice && <div className="sx-note">{notice}</div>}
      <div className="sx-tabs">
        <button type="button" className={tab === "employee" ? "on" : ""} onClick={() => { setTab("employee"); setErr(""); }}>Employee</button>
        <button type="button" className={tab === "owner" ? "on" : ""} onClick={() => { setTab("owner"); setErr(""); setWho(null); }}>Owner</button>
      </div>
      {tab === "employee" && !who && (
        <div>
          {people === null && <p className="hint">Loading…</p>}
          {people?.length === 0 && <p className="hint">No employees yet.</p>}
          <div className="sx-tiles">{(people ?? []).map((x) => <button key={x.username} type="button" className="sx-tile" onClick={() => { setWho(x); setErr(""); }}>{x.name}</button>)}</div>
        </div>
      )}
      {tab === "employee" && who && (
        <div>
          <p><b>{who.name}</b>, enter your PIN</p>
          <PinEntry onSubmit={(pin) => void pinGo(pin)} disabled={busy} />
          {err && <div className="err">{err}</div>}
          <button type="button" onClick={() => { setWho(null); setErr(""); }}>Not you? Back</button>
        </div>
      )}
      {tab === "owner" && (
        <form onSubmit={(e) => void ownerGo(e)}>
          <input autoFocus value={u} placeholder="Username" autoComplete="username" onChange={(e) => setU(e.target.value)} />
          <input type="password" value={p} placeholder="Password" autoComplete="current-password" onChange={(e) => setP(e.target.value)} />
          {needCode && <>
            <input autoFocus value={code} placeholder="6-digit code" autoComplete="one-time-code" onChange={(e) => setCode(e.target.value)} />
          </>}
          {err && <div className="err">{err}</div>}
          <button className="primary" disabled={busy || !u.trim() || !p || (needCode && !code.trim())}>Sign in</button>
        </form>
      )}
      {guest && <button type="button" className="sx-guest" onClick={() => void guestGo()}>View as guest (read-only)</button>}
    </Card>
  );
}

function SetupScreen({ onDone }: { onDone: (m: Me) => void }) {
  const [f, setF] = useState({ username: "", full_name: "", password: "", password2: "" });
  const [err, setErr] = useState(""), [busy, setBusy] = useState(false);
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const go = async (e: FormEvent) => {
    e.preventDefault(); setErr("");
    if (f.password !== f.password2) return setErr("The two passwords don't match.");
    setBusy(true);
    try { onDone((await req<{ user: Me }>("/auth/setup", { username: f.username.trim(), full_name: f.full_name.trim(), password: f.password })).user); }
    catch (x) { setErr((x as Error).message); }
    setBusy(false);
  };
  return (
    <Card title="First-time setup: create the owner account">
      <form onSubmit={(e) => void go(e)}>
        <input autoFocus value={f.username} placeholder="Username" autoComplete="username" onChange={set("username")} />
        <input value={f.full_name} placeholder="Full name" onChange={set("full_name")} />
        <input type="password" value={f.password} placeholder="Password" autoComplete="new-password" onChange={set("password")} />
        <input type="password" value={f.password2} placeholder="Password again" autoComplete="new-password" onChange={set("password2")} />
        {err && <div className="err">{err}</div>}
        <button className="primary" disabled={busy}>Create owner account</button>
      </form>
    </Card>
  );
}

/** Covers the whole screen. Same person, same credential as their sign-in: employee PIN, owner password. */
function LockScreen({ me, onUnlocked, onSignOut }: { me: Me; onUnlocked: () => void; onSignOut: () => void }) {
  const [pw, setPw] = useState(""), [err, setErr] = useState(""), [busy, setBusy] = useState(false), [needCode, setNeedCode] = useState(false), [code, setCode] = useState("");
  const unlock = async (body: { pin?: string; password?: string; code?: string }) => {
    setErr(""); setBusy(true);
    try { await req("/auth/unlock", body); onUnlocked(); } catch (x) { if ((x as Error).name === "CodeRequired") { setNeedCode(true); setErr(""); } else { setErr((x as Error).message); setPw(""); setCode(""); } }
    setBusy(false);
  };
  return (
    <div className="sx-lock" role="dialog" aria-modal="true" aria-label="Screen locked">
      <div className="sx-card">
        <div className="sx-brand">POS · locked</div>
        <h2>{me.full_name || me.username}</h2>
        {me.role === "employee"
          ? <PinEntry onSubmit={(pin) => void unlock({ pin })} disabled={busy} />
          : <form onSubmit={(e) => { e.preventDefault(); void unlock({ password: pw, code: code || undefined }); }}>
            <input autoFocus type="password" value={pw} placeholder="Password" autoComplete="current-password" onChange={(e) => setPw(e.target.value)} />
            {needCode && <input autoFocus inputMode="numeric" maxLength={32} value={code} placeholder="6-digit code" onChange={(e) => setCode(e.target.value)} />}
            <button className="primary" disabled={busy || !pw || (needCode && !code)}>Unlock</button>
          </form>}
        {err && <div className="err">{err}</div>}
        <button type="button" onClick={onSignOut}>Sign out (shift change)</button>
      </div>
    </div>
  );
}

type Stage = "loading" | "setup" | "login" | "ready" | "locked" | "down" | "blocked";

/** Wraps the whole app: setup / sign-in / lock screen, otherwise the app itself. Also runs the inactivity timer. */
export function AuthGate({ children }: { children: ReactNode }) {
  const [stage, setStage] = useState<Stage>("loading");
  const [me, setMe] = useState<Me | null>(null), [notice, setNotice] = useState(""), [blockedMsg, setBlockedMsg] = useState("");
  const [guestOn, setGuestOn] = useState(false), [lockSecs, setLockSecs] = useState(60), [epoch, setEpoch] = useState(0);
  const [dlg, setDlg] = useState<Dlg | null>(null);
  const dlgId = useRef(0), dlgRef = useRef<Dlg | null>(null), last = useRef(Date.now());
  dlgRef.current = dlg;

  const load = useCallback(async () => {
    try {
      const s = await req<Status>("/auth/status");
      setGuestOn(s.guest_enabled); setLockSecs(s.lock_seconds);
      if (s.needs_setup) setStage("setup");
      else if (!s.user) setStage("login");
      else { setMe(s.user); setStage(s.locked ? "locked" : "ready"); }
    } catch (e) { if (!blockedMsg) setStage("down"); void e; }
  }, [blockedMsg]);
  useEffect(() => {
    hooks.unauthorized = () => { setMe(null); setNotice("Your session ended. Please sign in again."); setStage("login"); };
    hooks.locked = () => setStage((s) => (s === "ready" ? "locked" : s));
    hooks.blocked = (m) => { setBlockedMsg(m); setStage("blocked"); };
    void load();
  }, [load]);

  const entered = (m: Me) => { setMe(m); setNotice(""); last.current = Date.now(); setStage("ready"); void load(); };
  const logout = useCallback(async () => {
    try { await req("/auth/logout", {}); } catch { /* already signed out */ }
    dlgRef.current?.resolve(null);
    setMe(null); setNotice(""); setStage("login");
  }, []);
  const lock = useCallback(() => {
    dlgRef.current?.resolve(null);                              // a confirm box never outlives the lock
    req("/auth/lock", {}).catch(() => {});
    setStage("locked");
  }, []);

  // inactivity: 60 s (set by the server) without a key, tap or scan -> lock. A ping tells the server we are still here.
  useEffect(() => {
    if (stage !== "ready" || !me) return;
    const limit = lockSecs * 1000;
    last.current = Date.now();
    let pinged = Date.now(), dirty = false;
    const bump = () => { last.current = Date.now(); dirty = true; };
    const evs = ["keydown", "pointerdown", "pointermove", "wheel", "touchstart"] as const;
    evs.forEach((e) => window.addEventListener(e, bump, { passive: true }));
    const iv = window.setInterval(() => {
      const now = Date.now();
      if (now - last.current >= limit) { if (me.role === "guest") void logout(); else lock(); return; }
      if (dirty && me.role !== "guest" && now - pinged > 15000) { dirty = false; pinged = now; req("/auth/activity", {}).catch(() => {}); }
    }, 1000);
    return () => { evs.forEach((e) => window.removeEventListener(e, bump)); window.clearInterval(iv); };
  }, [stage, me, lockSecs, lock, logout]);

  const ask = useCallback((label: string, message: string) =>
    new Promise<Auth | null>((resolve) => setDlg({ id: ++dlgId.current, label, message, resolve: (a) => { setDlg(null); resolve(a); } })), []);
  const withPin = useCallback(async <T,>(label: string, fn: (auth?: Auth) => Promise<T>): Promise<T> => {
    let auth: Auth | undefined;
    for (;;) {
      try { return await fn(auth); }
      catch (e) {
        if (!(e instanceof PinError)) throw e;
        const got = await ask(label, e.invalid ? e.message : "");
        if (!got) throw new Cancelled();
        auth = got;
      }
    }
  }, [ask]);

  if (stage === "loading") return <Card title="Starting…"><p className="hint">Loading…</p></Card>;
  if (stage === "blocked") return <Card title="This computer can't connect"><div className="err">{blockedMsg}</div><button className="primary" onClick={() => { setBlockedMsg(""); setStage("loading"); void load(); }}>Try again</button></Card>;
  if (stage === "down") return <Card title="Can't reach the POS server"><button className="primary" onClick={() => { setStage("loading"); void load(); }}>Try again</button></Card>;
  if (stage === "setup") return <SetupScreen onDone={entered} />;
  if (stage === "login" || !me) return <LoginScreen notice={notice} guest={guestOn} onDone={entered} />;
  return (
    <AuthCtx.Provider value={{ me, logout, lock, epoch, withPin }}>
      <div {...(stage === "locked" ? { inert: true } : {})}>{children}</div>
      {dlg && stage === "ready" && <PinDialog key={dlg.id} dlg={dlg} />}
      {stage === "locked" && <LockScreen me={me} onUnlocked={() => { last.current = Date.now(); setEpoch((x) => x + 1); setStage("ready"); }} onSignOut={() => void logout()} />}
    </AuthCtx.Provider>
  );
}
