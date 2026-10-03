import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { Cancelled, PinError, hooks, req } from "./api";
import type { Auth } from "./api";
import "./security.css";

export type Role = "cashier" | "manager" | "owner";
export const RANK: Record<Role, number> = { cashier: 1, manager: 2, owner: 3 };
export interface Me { id: number; username: string; full_name: string; role: Role; must_change: boolean }

interface AuthApi {
  me: Me;
  logout: () => Promise<void>;
  /** Run an action that may need a PIN: tries it, and if the server asks for a PIN (or a manager's approval) shows the box and retries. */
  withPin: <T>(label: string, fn: (auth?: Auth) => Promise<T>) => Promise<T>;
}
const AuthCtx = createContext<AuthApi | null>(null);
export function useAuth(): AuthApi {
  const c = useContext(AuthCtx);
  if (!c) throw new Error("useAuth must be used inside <AuthGate>");
  return c;
}

interface Dlg { id: number; label: string; approver: boolean; who: string; message: string; resolve: (a: Auth | null) => void }

/** The PIN box. For a cashier whose action needs approval it also asks for the manager's username. */
function PinDialog({ dlg }: { dlg: Dlg }) {
  const [pin, setPin] = useState(""), [who, setWho] = useState(dlg.who);        // after a wrong PIN the username is kept
  const pinRef = useRef<HTMLInputElement>(null), whoRef = useRef<HTMLInputElement>(null);
  useEffect(() => { (dlg.approver && !who ? whoRef : pinRef).current?.focus(); }, []); // eslint-disable-line
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (dlg.approver && !who.trim()) { whoRef.current?.focus(); return; }
    if (!/^\d{4,8}$/.test(pin)) { pinRef.current?.focus(); return; }
    dlg.resolve({ pin, approver: dlg.approver ? who.trim() : undefined });
  };
  return (
    <div className="sx-overlay" role="dialog" aria-modal="true">
      <form className="sx-dialog" onSubmit={submit} onKeyDown={(e) => e.key === "Escape" && dlg.resolve(null)}>
        <h3>{dlg.label}</h3>
        <p className="hint">{dlg.approver ? "This needs a manager's or owner's approval. Ask them to type their username and PIN." : "Enter your PIN to confirm."}</p>
        {dlg.approver && <input ref={whoRef} value={who} placeholder="Manager / owner username" autoComplete="off" onChange={(e) => setWho(e.target.value)} />}
        <input ref={pinRef} type="password" inputMode="numeric" autoComplete="off" maxLength={8} value={pin} placeholder="PIN"
          onChange={(e) => setPin(e.target.value.replace(/\D/g, ""))} />
        {dlg.message && <div className="err">{dlg.message}</div>}
        <div><button type="submit" className="primary">Confirm</button><button type="button" onClick={() => dlg.resolve(null)}>Cancel</button></div>
      </form>
    </div>
  );
}

function Card({ title, children }: { title: string; children: ReactNode }) {
  return <div className="sx-screen"><div className="sx-card"><div className="sx-brand">POS</div><h2>{title}</h2>{children}</div></div>;
}

function LoginScreen({ notice, onDone }: { notice: string; onDone: (m: Me) => void }) {
  const [u, setU] = useState(""), [p, setP] = useState(""), [err, setErr] = useState(""), [busy, setBusy] = useState(false);
  const go = async (e: FormEvent) => {
    e.preventDefault(); setErr(""); setBusy(true);
    try { onDone((await req<{ user: Me }>("/auth/login", { username: u.trim(), password: p })).user); }
    catch (x) { setErr((x as Error).message); setP(""); }
    setBusy(false);
  };
  return (
    <Card title="Log in">
      <form onSubmit={go}>
        {notice && <div className="sx-note">{notice}</div>}
        <input autoFocus value={u} placeholder="Username" autoComplete="username" onChange={(e) => setU(e.target.value)} />
        <input type="password" value={p} placeholder="Password" autoComplete="current-password" onChange={(e) => setP(e.target.value)} />
        {err && <div className="err">{err}</div>}
        <button className="primary" disabled={busy || !u.trim() || !p}>Log in</button>
      </form>
    </Card>
  );
}

function SetupScreen({ onDone }: { onDone: (m: Me) => void }) {
  const [f, setF] = useState({ username: "", full_name: "", password: "", password2: "", pin: "", pin2: "" });
  const [err, setErr] = useState(""), [busy, setBusy] = useState(false);
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: k.startsWith("pin") ? e.target.value.replace(/\D/g, "") : e.target.value });
  const go = async (e: FormEvent) => {
    e.preventDefault(); setErr("");
    if (f.password !== f.password2) return setErr("The two passwords don't match.");
    if (f.pin !== f.pin2) return setErr("The two PINs don't match.");
    setBusy(true);
    try { onDone((await req<{ user: Me }>("/auth/setup", { username: f.username.trim(), full_name: f.full_name.trim(), password: f.password, pin: f.pin })).user); }
    catch (x) { setErr((x as Error).message); }
    setBusy(false);
  };
  return (
    <Card title="First-time setup: create the owner account">
      <form onSubmit={go}>
        <p className="hint">The owner can add managers and cashiers, and sees everything. Use a password only you know, and a PIN (4–8 digits) you will type for refunds, price changes and receiving stock.</p>
        <input autoFocus value={f.username} placeholder="Username (e.g. fahad)" autoComplete="username" onChange={set("username")} />
        <input value={f.full_name} placeholder="Full name" onChange={set("full_name")} />
        <input type="password" value={f.password} placeholder="Password (8+ characters)" autoComplete="new-password" onChange={set("password")} />
        <input type="password" value={f.password2} placeholder="Password again" autoComplete="new-password" onChange={set("password2")} />
        <input type="password" inputMode="numeric" maxLength={8} value={f.pin} placeholder="PIN (4–8 digits)" autoComplete="off" onChange={set("pin")} />
        <input type="password" inputMode="numeric" maxLength={8} value={f.pin2} placeholder="PIN again" autoComplete="off" onChange={set("pin2")} />
        {err && <div className="err">{err}</div>}
        <button className="primary" disabled={busy}>Create owner account</button>
      </form>
    </Card>
  );
}

function MustChangeScreen({ me, onDone, onLogout }: { me: Me; onDone: () => void; onLogout: () => void }) {
  const [cur, setCur] = useState(""), [n1, setN1] = useState(""), [n2, setN2] = useState(""), [err, setErr] = useState(""), [busy, setBusy] = useState(false);
  const go = async (e: FormEvent) => {
    e.preventDefault(); setErr("");
    if (n1 !== n2) return setErr("The two new passwords don't match.");
    setBusy(true);
    try { await req("/auth/password", { current_password: cur, new_password: n1 }); onDone(); }
    catch (x) { setErr((x as Error).message); }
    setBusy(false);
  };
  return (
    <Card title="Choose your own password">
      <form onSubmit={go}>
        <p className="hint">Hello {me.full_name || me.username}. The owner set a starting password for you. Replace it with one only you know; from then on you log in with your new password (the starting one stops working).</p>
        <input type="password" autoFocus value={cur} placeholder="Password you just logged in with" autoComplete="current-password" onChange={(e) => setCur(e.target.value)} />
        <input type="password" value={n1} placeholder="New password (8+ characters)" autoComplete="new-password" onChange={(e) => setN1(e.target.value)} />
        <input type="password" value={n2} placeholder="New password again" autoComplete="new-password" onChange={(e) => setN2(e.target.value)} />
        {err && <div className="err">{err}</div>}
        <button className="primary" disabled={busy}>Save and continue</button>
        <button type="button" onClick={onLogout}>Log out</button>
      </form>
    </Card>
  );
}

type Stage = "loading" | "setup" | "login" | "change" | "ready" | "down";

/** Wraps the whole app: shows setup / login / forced password change, otherwise the app itself. */
export function AuthGate({ children }: { children: ReactNode }) {
  const [stage, setStage] = useState<Stage>("loading");
  const [me, setMe] = useState<Me | null>(null), [notice, setNotice] = useState("");
  const [dlg, setDlg] = useState<Dlg | null>(null);
  const dlgId = useRef(0);

  const load = useCallback(async () => {
    try {
      const s = await req<{ needs_setup: boolean; user: Me | null }>("/auth/status");
      if (s.needs_setup) setStage("setup");
      else if (!s.user) setStage("login");
      else { setMe(s.user); setStage(s.user.must_change ? "change" : "ready"); }
    } catch { setStage("down"); }
  }, []);
  useEffect(() => {
    hooks.unauthorized = () => { setMe(null); setNotice("Your session ended. Please log in again."); setStage("login"); };
    hooks.mustChange = () => setStage("change");
    void load();
  }, [load]);

  const entered = (m: Me) => { setMe(m); setNotice(""); setStage(m.must_change ? "change" : "ready"); };
  const logout = useCallback(async () => {
    try { await req("/auth/logout", {}); } catch { /* already logged out */ }
    setMe(null); setNotice(""); setStage("login");
  }, []);
  const ask = useCallback((label: string, approver: boolean, who: string, message: string) =>
    new Promise<Auth | null>((resolve) => setDlg({ id: ++dlgId.current, label, approver, who, message, resolve: (a) => { setDlg(null); resolve(a); } })), []);
  const withPin = useCallback(async <T,>(label: string, fn: (auth?: Auth) => Promise<T>): Promise<T> => {
    let auth: Auth | undefined;
    for (;;) {
      try { return await fn(auth); }
      catch (e) {
        if (!(e instanceof PinError)) throw e;
        const got = await ask(label, e.needsApprover, auth?.approver ?? "", e.invalid ? e.message : "");
        if (!got) throw new Cancelled();
        auth = got;
      }
    }
  }, [ask]);

  if (stage === "loading") return <Card title="Starting…"><p className="hint">Connecting to the POS server.</p></Card>;
  if (stage === "down") return <Card title="Can't reach the POS server"><p className="hint">Start the backend (see start.bat), then try again.</p><button className="primary" onClick={() => { setStage("loading"); void load(); }}>Try again</button></Card>;
  if (stage === "setup") return <SetupScreen onDone={entered} />;
  if (stage === "login" || !me) return <LoginScreen notice={notice} onDone={entered} />;
  if (stage === "change") return <MustChangeScreen me={me} onDone={() => void load()} onLogout={() => void logout()} />;
  return (
    <AuthCtx.Provider value={{ me, logout, withPin }}>
      {children}
      {dlg && <PinDialog key={dlg.id} dlg={dlg} />}
    </AuthCtx.Provider>
  );
}
