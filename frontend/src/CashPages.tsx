import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { req } from "./api";
import { useAuth } from "./auth";

interface Shift { id: number; opened_ts: string; opening_float: number }
interface Move { id: number; ts: string; actor: string; kind: string; amount: number; reason: string }
interface ShiftRow { drops?: number; payouts?: number; added?: number; id: number; actor: string; opened_ts: string; closed_ts: string | null; opening_float: number; counted_cash: number | null; expected_cash: number | null; variance: number | null; note: string; open: boolean }
interface DrawerEv { id: number; ts: string; actor: string; kind: string; sale_id: number | null; reason: string; ok: boolean; detail: string }
const rs = (n: number | null) => (n === null ? "—" : "Rs " + (Math.round(n * 100) / 100).toLocaleString(undefined, { maximumFractionDigits: 2 }));
type Note = { ok: boolean; text: string } | null;

function useGet<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [d, setD] = useState<T | null>(null);
  useEffect(() => { let on = true; fn().then((x) => on && setD(x)).catch(() => {}); return () => { on = false; }; }, deps); // eslint-disable-line
  return d;
}

/** Cash-up: an employee opens a shift with the float and closes it by typing the cash they COUNTED (never shown the expected amount).
 *  The owner sees expected cash and differences, and every drawer opening. */
export function CashUp({ tick }: { tick: number }) {
  const { me, withPin } = useAuth();
  const isOwner = me.role === "owner" || me.role === "developer";
  const [n, setN] = useState(0), [note, setNote] = useState<Note>(null);
  const cur = useGet(() => req<{ shift: Shift | null; required: boolean }>("/shifts/current"), [n, tick]);
  const shifts = useGet(() => (isOwner ? req<{ shifts: ShiftRow[]; threshold: number }>("/shifts") : Promise.resolve(null)), [n, tick, isOwner]);
  const events = useGet(() => (isOwner ? req<{ events: DrawerEv[] }>("/drawer/events?limit=30") : Promise.resolve(null)), [n, tick, isOwner]);
  const moves = useGet(() => (isOwner ? req<{ moves: Move[] }>("/shifts/moves?limit=40") : Promise.resolve(null)), [n, tick, isOwner]);
  const [mv, setMv] = useState({ kind: "drop", amount: "", reason: "" });
  const [fl, setFl] = useState(""), [counted, setCounted] = useState(""), [cnote, setCnote] = useState("");
  const run = async (fn: () => Promise<unknown>, okText: string) => { setNote(null); try { await fn(); setNote({ ok: true, text: okText }); setN((x) => x + 1); return true; } catch (e) { const m = (e as Error).message; if (m) setNote({ ok: false, text: m }); return false; } };

  const open = async (e: FormEvent) => { e.preventDefault(); if (await run(() => req("/shifts/open", { opening_float: Number(fl) }), "Shift opened.")) setFl(""); };
  const close = async (e: FormEvent) => {
    e.preventDefault();
    if (!window.confirm("Close your shift with this count? You can't change it afterwards.")) return;
    if (await run(() => req("/shifts/close", { counted_cash: Number(counted), note: cnote }), "Shift closed and your count was submitted to the owner.")) { setCounted(""); setCnote(""); }
  };
  const drawer = async () => {
    const reason = window.prompt("Why are you opening the cash drawer?");
    if (!reason) return;
    await run(() => withPin("Open the cash drawer", (a) => req("/drawer/open", { reason }, a)), "Drawer open recorded.");
  };
  return (
    <div className="sx">
      <h1>Cash up</h1>
      {note && <div className={note.ok ? "sx-ok" : "err"}>{note.text}</div>}
      <section>
        <h2>My shift</h2>
        {cur?.shift ? (
          <form className="sx-stack" onSubmit={(e) => void close(e)}>
            <p>Open since {new Date(cur.shift.opened_ts).toLocaleString()} · opening float {rs(cur.shift.opening_float)}</p>
            <span className="sx-inline">
              <select value={mv.kind} onChange={(e) => setMv({ ...mv, kind: e.target.value })}><option value="drop">Drop to the safe</option><option value="payout">Payout</option><option value="add">Cash added</option></select>
              <input type="number" min="0" step="0.01" style={{ width: 110 }} value={mv.amount} placeholder="Rs" onChange={(e) => setMv({ ...mv, amount: e.target.value })} />
              <input value={mv.reason} placeholder="Reason" maxLength={200} onChange={(e) => setMv({ ...mv, reason: e.target.value })} />
              <button type="button" disabled={!mv.amount || mv.reason.trim().length < 3} onClick={() => void run(() => withPin("Record this cash movement", (a) => req("/shifts/move", { kind: mv.kind, amount: Number(mv.amount), reason: mv.reason }, a)), "Recorded.").then((ok) => ok && setMv({ kind: "drop", amount: "", reason: "" }))}>Record</button>
            </span>
            <input type="number" min="0" step="0.01" value={counted} placeholder="Cash counted in the drawer (Rs)" onChange={(e) => setCounted(e.target.value)} />
            <input value={cnote} placeholder="Note (optional)" maxLength={200} onChange={(e) => setCnote(e.target.value)} />
            <button className="primary" disabled={counted === ""}>Submit count and close shift</button>
          </form>
        ) : (
          <form className="sx-stack" onSubmit={(e) => void open(e)}>
            <input type="number" min="0" step="0.01" value={fl} placeholder="Opening float in the drawer (Rs)" onChange={(e) => setFl(e.target.value)} />
            <button className="primary" disabled={fl === ""}>Open shift</button>
          </form>
        )}
        <button onClick={() => void drawer()}>Open cash drawer by hand…</button>
      </section>
      {isOwner && <>
        <section>
          <h2>Shifts</h2>
          <table><thead><tr><th>#</th><th>Employee</th><th>Opened</th><th>Float</th><th>Counted</th><th>Expected</th><th>Drops / payouts / added</th><th>Difference</th><th></th></tr></thead>
            <tbody>{(shifts?.shifts ?? []).map((s) => (
              <tr key={s.id} className={s.variance !== null && Math.abs(s.variance) > (shifts?.threshold ?? 0) ? "out" : ""}>
                <td>{s.id}</td><td>{s.actor}</td><td className="hint">{new Date(s.opened_ts).toLocaleString()}</td><td>{rs(s.opening_float)}</td>
                <td>{s.open ? <i className="hint">open</i> : rs(s.counted_cash)}</td><td>{rs(s.expected_cash)}</td><td className="hint">{rs(s.drops ?? 0)} / {rs(s.payouts ?? 0)} / {rs(s.added ?? 0)}</td><td><b className={s.variance && s.variance < 0 ? "bad" : ""}>{rs(s.variance)}</b></td>
                <td>{!s.open && <button onClick={() => { const nt = window.prompt("Review note (optional)") ; if (nt !== null) void run(() => req(`/shifts/${s.id}/review`, { note: nt }), "Reviewed."); }}>Mark reviewed</button>}</td>
              </tr>))}</tbody></table>
        </section>
        <section>
          <h2>Cash drops, payouts and additions</h2>
          <table><thead><tr><th>When</th><th>Who</th><th>What</th><th>Amount</th><th>Reason</th></tr></thead>
            <tbody>{(moves?.moves ?? []).map((m) => <tr key={m.id}><td className="hint">{new Date(m.ts).toLocaleString()}</td><td>{m.actor}</td><td>{m.kind}</td><td>{rs(m.amount)}</td><td>{m.reason}</td></tr>)}</tbody></table>
        </section>
        <section>
          <h2>Drawer openings</h2>
          <table><thead><tr><th>When</th><th>Who</th><th>Why</th><th>Result</th></tr></thead>
            <tbody>{(events?.events ?? []).map((e) => <tr key={e.id}><td className="hint">{new Date(e.ts).toLocaleString()}</td><td>{e.actor}</td>
              <td>{e.kind === "sale" ? `cash sale #${e.sale_id}` : `by hand: ${e.reason}`}</td><td className={e.ok ? "" : "bad"}>{e.ok ? "ok" : "failed"} <span className="hint">{e.detail}</span></td></tr>)}</tbody></table>
        </section>
      </>}
    </div>
  );
}
