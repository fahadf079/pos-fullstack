import { Fragment, useEffect, useState } from "react";
import QRCode from "qrcode";
import type { FormEvent } from "react";
import { req } from "./api";
import { useAuth } from "./auth";
import type { Role } from "./auth";

interface UserRow { id: number; username: string; full_name: string; role: Role; active: boolean; last_login_ts: string | null; locked: boolean; pin_locked: boolean; sessions: number; two_factor: boolean }
interface Rule { method: string; path: string; role: Role; pin: "self" | null; label: string }
interface PolicyResp { rules: Rule[]; lockout: { password_tries: number; password_minutes: number; pin_tries: number; pin_minutes: number }; session_hours: number }
type Note = { ok: boolean; text: string } | null;

function useGet<T>(fn: () => Promise<T>, deps: unknown[]) {   // fetch with a stale-response guard
  const [d, setD] = useState<T | null>(null), [err, setErr] = useState("");
  useEffect(() => { let on = true; fn().then((x) => { if (on) { setD(x); setErr(""); } }).catch((e: Error) => { if (on) setErr(e.message); }); return () => { on = false; }; }, deps); // eslint-disable-line
  return { d, err };
}
const digits = (v: string) => v.replace(/\D/g, "");
const when = (t: string | null) => (t ? new Date(t).toLocaleString() : "never");

/** Owner only: people, PINs, passwords of other owners, and the permission table. */
export function Security({ tick }: { tick: number }) {
  const { me } = useAuth();
  const [n, setN] = useState(0), [note, setNote] = useState<Note>(null);
  const users = useGet(() => req<{ users: UserRow[] }>("/users"), [tick, n]);
  const policy = useGet(() => req<PolicyResp>("/security/policy"), []);
  const [f, setF] = useState({ username: "", full_name: "", role: "employee" as "employee" | "owner", password: "", pin: "" });
  const [panel, setPanel] = useState<{ id: number; kind: "pw" | "pin" | "edit" } | null>(null), [val, setVal] = useState(""), [ed, setEd] = useState({ username: "", full_name: "" });

  const act = async (path: string, body: unknown, okText: string): Promise<boolean> => {
    setNote(null);
    try { await req(path, body); setNote({ ok: true, text: okText }); setN((x) => x + 1); return true; }
    catch (e) { setNote({ ok: false, text: (e as Error).message }); return false; }
  };
  const add = async (e: FormEvent) => {
    e.preventDefault();
    const body = { username: f.username.trim(), full_name: f.full_name.trim(), role: f.role, ...(f.role === "employee" ? { pin: f.pin } : { password: f.password }) };
    if (await act("/users", body, f.role === "employee" ? `Added ${f.username.trim()}.` : `Added owner ${f.username.trim()}.`))
      setF({ username: "", full_name: "", role: f.role, password: "", pin: "" });
  };
  const savePanel = async (u: UserRow) => {
    if (!panel) return;
    const ok = panel.kind === "edit"
      ? await act(`/users/${u.id}/update`, { username: ed.username.trim(), full_name: ed.full_name.trim() }, `${u.username} updated.`)
      : panel.kind === "pw"
        ? await act(`/users/${u.id}/password`, { new_password: val }, `Password of ${u.username} reset.`)
        : await act(`/users/${u.id}/pin`, { new_pin: val }, `PIN of ${u.username} set.`);
    if (ok) { setPanel(null); setVal(""); }
  };
  const roles: Role[] = ["employee", "owner", "developer"];
  const roleName: Record<string, string> = { guest: "Guest", employee: "Employee", owner: "Owner", developer: "Developer" };

  return (
    <div className="sx">
      <h1>Security</h1>
      {note && <div className={note.ok ? "sx-ok" : "err"}>{note.text}</div>}
      {users.err && <div className="err">{users.err}</div>}

      <section>
        <h2>People</h2>
        <table>
          <thead><tr><th>Name</th><th>Full name</th><th>Role</th><th>Status</th><th>Last sign-in</th><th></th></tr></thead>
          <tbody>
            {(users.d?.users ?? []).map((u) => (
              <Fragment key={u.id}>
                <tr className={u.active ? "" : "inactive"}>
                  <td><b>{u.username}</b>{u.id === me.id && <span className="hint"> (you)</span>}</td>
                  <td>{u.full_name || "—"}</td>
                  <td>{u.role}{u.role === "owner" && (u.two_factor ? <span className="good"> · 2FA</span> : <span className="hint"> · no 2FA</span>)}</td>
                  <td>
                    {!u.active && <b className="bad">DEACTIVATED </b>}{u.locked && <b className="bad">LOCKED </b>}{u.pin_locked && <b className="bad">PIN LOCKED </b>}
                    {u.active && !u.locked && !u.pin_locked && <span className="good">active</span>}
                    {u.sessions > 0 && <span className="hint"> · {u.sessions} sign-in{u.sessions > 1 ? "s" : ""} open</span>}
                  </td>
                  <td className="hint">{when(u.last_login_ts)}</td>
                  <td>
                    <button onClick={() => { setPanel(panel?.id === u.id && panel.kind === "edit" ? null : { id: u.id, kind: "edit" }); setEd({ username: u.username, full_name: u.full_name }); }}>Edit</button>
                    {u.role === "employee" && <button onClick={() => { setPanel(panel?.id === u.id && panel.kind === "pin" ? null : { id: u.id, kind: "pin" }); setVal(""); }}>Set PIN</button>}
                    {u.role === "owner" && u.id !== me.id && <button onClick={() => { setPanel(panel?.id === u.id && panel.kind === "pw" ? null : { id: u.id, kind: "pw" }); setVal(""); }}>Reset password</button>}
                    {(u.locked || u.pin_locked) && <button onClick={() => void act(`/users/${u.id}/unlock`, {}, `${u.username} unlocked.`)}>Unlock</button>}
                    {u.id !== me.id && (u.active
                      ? <button onClick={() => window.confirm(`Deactivate ${u.username}?`) && void act(`/users/${u.id}/active`, { active: false }, `${u.username} deactivated.`)}>Deactivate</button>
                      : <button onClick={() => void act(`/users/${u.id}/active`, { active: true }, `${u.username} reactivated.`)}>Reactivate</button>)}
                    {u.role === "employee" && <button onClick={() => window.confirm(`Remove ${u.username}?`) && void act(`/users/${u.id}/remove`, {}, `${u.username} removed.`)}>Remove</button>}
                  </td>
                </tr>
                {panel?.id === u.id && (
                  <tr><td colSpan={6}>
                    <form className="sx-inline" onSubmit={(e) => { e.preventDefault(); void savePanel(u); }}>
                      {panel.kind === "edit" ? <>
                        <input autoFocus value={ed.username} placeholder="Username" autoComplete="off" onChange={(e) => setEd({ ...ed, username: e.target.value })} />
                        <input value={ed.full_name} placeholder="Name" autoComplete="off" onChange={(e) => setEd({ ...ed, full_name: e.target.value })} />
                        <button className="primary" disabled={!ed.username.trim()}>Save</button><button type="button" onClick={() => setPanel(null)}>Cancel</button>
                      </> : <>
                      <input autoFocus type="password" autoComplete="new-password" value={val} maxLength={panel.kind === "pin" ? 8 : 200} inputMode={panel.kind === "pin" ? "numeric" : undefined}
                        placeholder={panel.kind === "pw" ? `New password for ${u.username} (8+ characters)` : `New PIN for ${u.username} (4–8 digits)`}
                        onChange={(e) => setVal(panel.kind === "pin" ? digits(e.target.value) : e.target.value)} />
                      <button className="primary" disabled={!val}>Save</button><button type="button" onClick={() => setPanel(null)}>Cancel</button>
                      </>}
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
          <select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value as "employee" | "owner" })}><option value="employee">Employee (PIN)</option><option value="owner">Owner (password)</option></select>
          <input value={f.username} placeholder="Username" autoComplete="off" onChange={(e) => setF({ ...f, username: e.target.value })} />
          <input value={f.full_name} placeholder="Name" onChange={(e) => setF({ ...f, full_name: e.target.value })} />
          {f.role === "employee"
            ? <input type="password" inputMode="numeric" maxLength={8} autoComplete="off" value={f.pin} placeholder="PIN (4–8 digits)" onChange={(e) => setF({ ...f, pin: digits(e.target.value) })} />
            : <input type="password" autoComplete="new-password" value={f.password} placeholder="Password (8+)" onChange={(e) => setF({ ...f, password: e.target.value })} />}
          <button className="primary" disabled={!f.username.trim() || (f.role === "employee" ? !f.pin : !f.password)}>Add</button>
        </form>
      </section>

      <section>
        <h2>Who can do what</h2>
        {roles.map((r) => (
          <details key={r}>
            <summary><b>{roleName[r]}</b></summary>
            <table><tbody>
              {(policy.d?.rules ?? []).filter((x) => x.role === r && x.path !== "/events").filter((x, i, a) => a.findIndex((y) => y.label === x.label) === i).map((x) => (
                <tr key={x.method + x.path}><td>{x.label}</td><td>{x.pin === "self" ? <b>PIN</b> : <span className="hint">—</span>}</td></tr>
              ))}
            </tbody></table>
          </details>
        ))}
      </section>
    </div>
  );
}

/** Everyone: lock now, sign out. Owners: password and two-step sign-in. */
export function Account() {
  const { me, logout, lock } = useAuth();
  const isOwner = me.role === "owner" || me.role === "developer";
  const [pw, setPw] = useState({ cur: "", n1: "", n2: "" }), [a, setA] = useState<Note>(null);
  const [tf, setTf] = useState<{ secret: string; uri: string } | null>(null), [code, setCode] = useState(""), [codes, setCodes] = useState<string[] | null>(null), [b, setB] = useState<Note>(null);
  const [off, setOff] = useState({ pw: "", code: "" }), [on2fa, setOn2fa] = useState(me.two_factor), [qr, setQr] = useState("");
  useEffect(() => { if (!tf) { setQr(""); return; } let on = true; void QRCode.toDataURL(tf.uri, { margin: 1, width: 200 }).then((u) => { if (on) setQr(u); }).catch(() => {}); return () => { on = false; }; }, [tf]);
  const savePw = async (e: FormEvent) => {
    e.preventDefault(); setA(null);
    if (pw.n1 !== pw.n2) return setA({ ok: false, text: "The two new passwords don't match." });
    try { await req("/auth/password", { current_password: pw.cur, new_password: pw.n1 }); setA({ ok: true, text: "Password changed." }); setPw({ cur: "", n1: "", n2: "" }); }
    catch (x) { setA({ ok: false, text: (x as Error).message }); }
  };
  const begin = async () => { setB(null); try { setTf(await req("/auth/2fa/begin", {})); } catch (x) { setB({ ok: false, text: (x as Error).message }); } };
  const confirm = async (e: FormEvent) => {
    e.preventDefault(); setB(null);
    try { const r = await req<{ recovery_codes: string[] }>("/auth/2fa/confirm", { code: code.trim() }); setCodes(r.recovery_codes); setTf(null); setCode(""); setOn2fa(true); }
    catch (x) { setB({ ok: false, text: (x as Error).message }); }
  };
  const disable = async (e: FormEvent) => {
    e.preventDefault(); setB(null);
    try { await req("/auth/2fa/disable", { password: off.pw, code: off.code.trim() }); setOn2fa(false); setCodes(null); setOff({ pw: "", code: "" }); setB({ ok: true, text: "Two-step sign-in is off." }); }
    catch (x) { setB({ ok: false, text: (x as Error).message }); }
  };
  return (
    <div className="sx">
      <h1>Account <small>{me.full_name || me.username} · {me.role}</small></h1>
      <section>
        <button onClick={lock}>Lock screen now</button><button onClick={() => void logout()}>Sign out</button>
      </section>
      {isOwner && <>
        <section>
          <h2>Change my password</h2>
          <form className="sx-stack" onSubmit={(e) => void savePw(e)}>
            <input type="password" autoComplete="current-password" value={pw.cur} placeholder="Current password" onChange={(e) => setPw({ ...pw, cur: e.target.value })} />
            <input type="password" autoComplete="new-password" value={pw.n1} placeholder="New password" onChange={(e) => setPw({ ...pw, n1: e.target.value })} />
            <input type="password" autoComplete="new-password" value={pw.n2} placeholder="New password again" onChange={(e) => setPw({ ...pw, n2: e.target.value })} />
            {a && <div className={a.ok ? "sx-ok" : "err"}>{a.text}</div>}
            <button className="primary" disabled={!pw.cur || !pw.n1}>Change password</button>
          </form>
        </section>
        <section>
          <h2>Two-step sign-in (authenticator app) <small>{on2fa ? <b className="good">ON</b> : <b className="bad">OFF</b>}</small></h2>
          {b && <div className={b.ok ? "sx-ok" : "err"}>{b.text}</div>}
          {!on2fa && !tf && <button className="primary" onClick={() => void begin()}>Set up two-step sign-in</button>}
          {tf && (
            <form className="sx-stack" onSubmit={(e) => void confirm(e)}>
              {qr && <img src={qr} width={200} height={200} alt="Setup QR code for your authenticator app" />}
              <p><code>{tf.secret}</code></p>
              <p><a href={tf.uri}>Setup link</a></p>
              <input value={code} inputMode="numeric" maxLength={6} placeholder="6-digit code" onChange={(e) => setCode(digits(e.target.value))} />
              <button className="primary" disabled={code.length !== 6}>Turn on</button>
            </form>
          )}
          {codes && <div><p><b>Recovery codes (shown once)</b></p><div className="sx-codes">{codes.map((c) => <div key={c}>{c}</div>)}</div></div>}
          {on2fa && me.role === "owner" && !codes && (
            <form className="sx-stack" onSubmit={(e) => void disable(e)}>
              <input type="password" value={off.pw} placeholder="Password" onChange={(e) => setOff({ ...off, pw: e.target.value })} />
              <input value={off.code} placeholder="Code" onChange={(e) => setOff({ ...off, code: e.target.value })} />
              <button disabled={!off.pw || !off.code}>Switch off</button>
            </form>
          )}
        </section>
      </>}
    </div>
  );
}
