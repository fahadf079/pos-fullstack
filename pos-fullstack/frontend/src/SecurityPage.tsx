import { Fragment, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { req } from "./api";
import { useAuth } from "./auth";
import type { Role } from "./auth";

interface UserRow { id: number; username: string; full_name: string; role: Role; active: boolean; must_change: boolean; last_login_ts: string | null; created_ts: string; locked: boolean; pin_locked: boolean; sessions: number }
interface Rule { method: string; path: string; role: Role; pin: "self" | "approval" | null; label: string }
interface PolicyResp { rules: Rule[]; lockout: { password_tries: number; password_minutes: number; pin_tries: number; pin_minutes: number }; session_hours: number }
type Note = { ok: boolean; text: string } | null;

function useGet<T>(fn: () => Promise<T>, deps: unknown[]) {   // fetch with a stale-response guard
  const [d, setD] = useState<T | null>(null), [err, setErr] = useState("");
  useEffect(() => { let on = true; fn().then((x) => { if (on) { setD(x); setErr(""); } }).catch((e: Error) => { if (on) setErr(e.message); }); return () => { on = false; }; }, deps); // eslint-disable-line
  return { d, err };
}
const digits = (v: string) => v.replace(/\D/g, "");
const when = (t: string | null) => (t ? new Date(t).toLocaleString() : "never");

/** Owner only: people, roles, resets, and the permission table. */
export function Security({ tick }: { tick: number }) {
  const { me, withPin } = useAuth();
  const [n, setN] = useState(0), [note, setNote] = useState<Note>(null);
  const users = useGet(() => req<{ users: UserRow[] }>("/users"), [tick, n]);
  const policy = useGet(() => req<PolicyResp>("/security/policy"), []);
  const [f, setF] = useState({ username: "", full_name: "", role: "cashier" as Role, password: "", pin: "", must_change: true });
  const [panel, setPanel] = useState<{ id: number; kind: "pw" | "pin" } | null>(null), [val, setVal] = useState(""), [panelMust, setPanelMust] = useState(true);

  /** one action: asks for the owner's PIN where the server requires it, shows the outcome */
  const act = async (label: string, path: string, body: unknown, okText: string): Promise<boolean> => {
    setNote(null);
    try { await withPin(label, (a) => req(path, body, a)); setNote({ ok: true, text: okText }); setN((x) => x + 1); return true; }
    catch (e) { const m = (e as Error).message; if (m) setNote({ ok: false, text: m }); return false; }
  };
  const add = async (e: FormEvent) => {
    e.preventDefault();
    if (await act("Add a person", "/users", { ...f, username: f.username.trim(), full_name: f.full_name.trim() }, f.must_change ? `Added ${f.username.trim()}. They must choose their own password the first time they log in.` : `Added ${f.username.trim()}. The password you typed is their password as it is.`))
      setF({ username: "", full_name: "", role: "cashier", password: "", pin: "", must_change: true });
  };
  const savePanel = async (u: UserRow) => {
    if (!panel) return;
    const ok = panel.kind === "pw"
      ? await act(`Reset password of ${u.username}`, `/users/${u.id}/password`, { new_password: val, must_change: panelMust },
        panelMust ? `Password of ${u.username} reset. They must choose a new one at next login.` : `Password of ${u.username} reset. It is their password as it is.`)
      : await act(`Reset PIN of ${u.username}`, `/users/${u.id}/pin`, { new_pin: val }, `PIN of ${u.username} reset.`);
    if (ok) { setPanel(null); setVal(""); }
  };
  const rank: Role[] = ["cashier", "manager", "owner"];
  const roleName: Record<Role, string> = { cashier: "Cashier — sells", manager: "Manager — also catalog, purchasing, expenses, reports, History", owner: "Owner — also people & security" };

  return (
    <div className="sx">
      <h1>Security <small>people, roles, PINs — every action is recorded against a person</small></h1>
      {note && <div className={note.ok ? "sx-ok" : "err"}>{note.text}</div>}
      {users.err && <div className="err">{users.err}</div>}

      <section>
        <h2>People</h2>
        <table>
          <thead><tr><th>Username</th><th>Name</th><th>Role</th><th>Status</th><th>Last login</th><th></th></tr></thead>
          <tbody>
            {(users.d?.users ?? []).map((u) => (
              <Fragment key={u.id}>
                <tr className={u.active ? "" : "inactive"}>
                  <td><b>{u.username}</b>{u.id === me.id && <span className="hint"> (you)</span>}</td>
                  <td>{u.full_name || "—"}</td>
                  <td>
                    <select value={u.role} disabled={u.id === me.id || !u.active} title={u.id === me.id ? "You can't change your own role" : ""}
                      onChange={(e) => void act(`Change role of ${u.username} to ${e.target.value}`, `/users/${u.id}/update`, { role: e.target.value }, `${u.username} is now a ${e.target.value}.`)}>
                      {rank.map((r) => <option key={r} value={r}>{r}</option>)}
                    </select>
                  </td>
                  <td>
                    {!u.active && <b className="bad">DEACTIVATED </b>}{u.locked && <b className="bad">LOCKED </b>}{u.pin_locked && <b className="bad">PIN LOCKED </b>}
                    {u.must_change && <span className="hint">must set password </span>}{u.active && !u.locked && !u.pin_locked && !u.must_change && <span className="good">active</span>}
                    {u.sessions > 0 && <span className="hint"> · {u.sessions} login{u.sessions > 1 ? "s" : ""} open</span>}
                  </td>
                  <td className="hint">{when(u.last_login_ts)}</td>
                  <td>
                    <button onClick={() => { setPanel(panel?.id === u.id && panel.kind === "pw" ? null : { id: u.id, kind: "pw" }); setVal(""); setPanelMust(true); }} disabled={u.id === me.id} title={u.id === me.id ? "Use Account to change your own" : ""}>Reset password</button>
                    <button onClick={() => { setPanel(panel?.id === u.id && panel.kind === "pin" ? null : { id: u.id, kind: "pin" }); setVal(""); }}>Reset PIN</button>
                    {(u.locked || u.pin_locked) && <button onClick={() => void act(`Unlock ${u.username}`, `/users/${u.id}/unlock`, {}, `${u.username} unlocked.`)}>Unlock</button>}
                    {u.id !== me.id && (u.active
                      ? <button onClick={() => window.confirm(`Deactivate ${u.username}? They are logged out at once and can't log in. Their History stays.`) && void act(`Deactivate ${u.username}`, `/users/${u.id}/active`, { active: false }, `${u.username} deactivated.`)}>Deactivate</button>
                      : <button onClick={() => void act(`Reactivate ${u.username}`, `/users/${u.id}/active`, { active: true }, `${u.username} reactivated.`)}>Reactivate</button>)}
                  </td>
                </tr>
                {panel?.id === u.id && (
                  <tr><td colSpan={6}>
                    <form className="sx-inline" onSubmit={(e) => { e.preventDefault(); void savePanel(u); }}>
                      <input autoFocus type="password" autoComplete="new-password" value={val} maxLength={panel.kind === "pin" ? 8 : 200}
                        inputMode={panel.kind === "pin" ? "numeric" : undefined}
                        placeholder={panel.kind === "pw" ? `New password for ${u.username} (8+ characters)` : `New PIN for ${u.username} (4–8 digits)`}
                        onChange={(e) => setVal(panel.kind === "pin" ? digits(e.target.value) : e.target.value)} />
                      {panel.kind === "pw" && <label className="sx-check"><input type="checkbox" checked={panelMust} onChange={(e) => setPanelMust(e.target.checked)} /> They must choose their own at next login</label>}
                      <button className="primary" disabled={!val}>Save</button><button type="button" onClick={() => setPanel(null)}>Cancel</button>
                    </form>
                  </td></tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </section>

      <section>
        <h2>Add a person</h2>
        <form className="sx-add" onSubmit={(e) => void add(e)}>
          <input value={f.username} placeholder="Username (letters/numbers)" autoComplete="off" onChange={(e) => setF({ ...f, username: e.target.value })} />
          <input value={f.full_name} placeholder="Full name" onChange={(e) => setF({ ...f, full_name: e.target.value })} />
          <select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value as Role })}>{rank.map((r) => <option key={r} value={r}>{r}</option>)}</select>
          <input type="password" autoComplete="new-password" value={f.password} placeholder={f.must_change ? "Starting password (8+)" : "Password (8+)"} onChange={(e) => setF({ ...f, password: e.target.value })} />
          <input type="password" inputMode="numeric" maxLength={8} autoComplete="off" value={f.pin} placeholder="PIN (4–8 digits)" onChange={(e) => setF({ ...f, pin: digits(e.target.value) })} />
          <button className="primary" disabled={!f.username.trim() || !f.password || !f.pin}>Add</button>
          <label className="sx-check" style={{ flexBasis: "100%" }}><input type="checkbox" checked={f.must_change} onChange={(e) => setF({ ...f, must_change: e.target.checked })} /> They must choose their own password the first time they log in (recommended: only they will know it)</label>
        </form>
        <p className="hint">Tick the box: the password you type is only a starting one, and they replace it at first login. Untick it: the password you type stays as their real password. Either way, tell them their PIN privately; they can change both under Account. Resetting a password logs that person out at once, and their old password stops working.</p>
      </section>

      <section>
        <h2>Who can do what</h2>
        {policy.d && <p className="hint">
          Sessions last {policy.d.session_hours} hours. {policy.d.lockout.password_tries} wrong passwords lock an account for {policy.d.lockout.password_minutes} minutes; {policy.d.lockout.pin_tries} wrong PINs lock that PIN for {policy.d.lockout.pin_minutes} minutes.
          “PIN” = you type your own PIN. “Approval” = a manager/owner types their PIN (a cashier asks one to come and approve).
        </p>}
        {rank.map((r) => (
          <details key={r}>
            <summary><b>{roleName[r]}</b></summary>
            <table><tbody>
              {(policy.d?.rules ?? []).filter((x) => x.role === r && x.path !== "/events").filter((x, i, a) => a.findIndex((y) => y.label === x.label) === i).map((x) => (
                <tr key={x.method + x.path}><td>{x.label}</td><td>{x.pin === "self" ? <b>PIN</b> : x.pin === "approval" ? <b>PIN / approval</b> : <span className="hint">—</span>}</td></tr>
              ))}
            </tbody></table>
          </details>
        ))}
        <p className="hint">Also needing a PIN when they happen: giving a discount code at checkout (approval) and changing a product's price or cost (PIN).</p>
      </section>
    </div>
  );
}

/** Everyone: change my own password and PIN. */
export function Account() {
  const { me, logout } = useAuth();
  const [pw, setPw] = useState({ cur: "", n1: "", n2: "" }), [pn, setPn] = useState({ cur: "", n1: "", n2: "" });
  const [a, setA] = useState<Note>(null), [b, setB] = useState<Note>(null);
  const savePw = async (e: FormEvent) => {
    e.preventDefault(); setA(null);
    if (pw.n1 !== pw.n2) return setA({ ok: false, text: "The two new passwords don't match." });
    try { await req("/auth/password", { current_password: pw.cur, new_password: pw.n1 }); setA({ ok: true, text: "Password changed. Your other logins (other tills/browsers) were logged out." }); setPw({ cur: "", n1: "", n2: "" }); }
    catch (x) { setA({ ok: false, text: (x as Error).message }); }
  };
  const savePin = async (e: FormEvent) => {
    e.preventDefault(); setB(null);
    if (pn.n1 !== pn.n2) return setB({ ok: false, text: "The two new PINs don't match." });
    try { await req("/auth/pin", { current_password: pn.cur, new_pin: pn.n1 }); setB({ ok: true, text: "PIN changed." }); setPn({ cur: "", n1: "", n2: "" }); }
    catch (x) { setB({ ok: false, text: (x as Error).message }); }
  };
  return (
    <div className="sx">
      <h1>Account <small>{me.full_name || me.username} · {me.role}</small></h1>
      <section>
        <h2>Change my password</h2>
        <form className="sx-stack" onSubmit={(e) => void savePw(e)}>
          <input type="password" autoComplete="current-password" value={pw.cur} placeholder="Current password" onChange={(e) => setPw({ ...pw, cur: e.target.value })} />
          <input type="password" autoComplete="new-password" value={pw.n1} placeholder="New password (8+ characters)" onChange={(e) => setPw({ ...pw, n1: e.target.value })} />
          <input type="password" autoComplete="new-password" value={pw.n2} placeholder="New password again" onChange={(e) => setPw({ ...pw, n2: e.target.value })} />
          {a && <div className={a.ok ? "sx-ok" : "err"}>{a.text}</div>}
          <button className="primary" disabled={!pw.cur || !pw.n1}>Change password</button>
        </form>
      </section>
      <section>
        <h2>Change my PIN</h2>
        <form className="sx-stack" onSubmit={(e) => void savePin(e)}>
          <p className="hint">Your PIN is what you type to confirm refunds, stock counts, price changes and receiving stock. Don't share it. Changing it needs your password.</p>
          <input type="password" autoComplete="current-password" value={pn.cur} placeholder="Your password" onChange={(e) => setPn({ ...pn, cur: e.target.value })} />
          <input type="password" inputMode="numeric" maxLength={8} autoComplete="off" value={pn.n1} placeholder="New PIN (4–8 digits)" onChange={(e) => setPn({ ...pn, n1: digits(e.target.value) })} />
          <input type="password" inputMode="numeric" maxLength={8} autoComplete="off" value={pn.n2} placeholder="New PIN again" onChange={(e) => setPn({ ...pn, n2: digits(e.target.value) })} />
          {b && <div className={b.ok ? "sx-ok" : "err"}>{b.text}</div>}
          <button className="primary" disabled={!pn.cur || !pn.n1}>Change PIN</button>
        </form>
      </section>
      <section><button onClick={() => void logout()}>Log out</button></section>
    </div>
  );
}
