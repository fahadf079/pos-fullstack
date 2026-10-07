import { Fragment, useEffect, useState } from "react";
import { req } from "./api";
import "./history.css";

interface Entry { id: number; ts: string; user_id: number | null; actor: string; approved_by: string | null; action: string; entity: string; entity_id: string; summary: string; details: unknown }
interface HistoryResp { total: number; entries: Entry[] }
interface Meta { categories: { category: string; count: number }[] }

const PAGE = 50;
const LABEL: Record<string, string> = { sale: "Sale / refund", stock: "Stock count", catalog: "Product change", po: "Purchase order", purchase: "Delivery / payment", supplier: "Supplier", expense: "Expense" };
const label = (c: string) => LABEL[c] ?? c;
const catOf = (action: string) => action.split(".")[0];
const verb = (action: string) => action.split(".").slice(1).join(".");

// fetch with a stale-response guard (a slow older answer must not overwrite a newer one)
function useGet<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [d, setD] = useState<T | null>(null), [err, setErr] = useState("");
  useEffect(() => { let on = true; fn().then((x) => { if (on) { setD(x); setErr(""); } }).catch((e: Error) => { if (on) setErr(e.message); }); return () => { on = false; }; }, deps); // eslint-disable-line
  return { d, err };
}

export function History({ tick }: { tick: number }) {
  const [cat, setCat] = useState(""), [q, setQ] = useState(""), [from, setFrom] = useState(""), [to, setTo] = useState("");
  const [page, setPage] = useState(0), [open, setOpen] = useState<number | null>(null);
  const [qLive, setQLive] = useState("");                                   // search applies shortly after typing stops
  useEffect(() => { const t = setTimeout(() => { setQ(qLive); setPage(0); }, 300); return () => clearTimeout(t); }, [qLive]);

  const meta = useGet(() => req<Meta>("/history/meta"), [tick]);
  const badRange = from !== "" && to !== "" && from > to;
  const qs = new URLSearchParams({ limit: String(PAGE), offset: String(page * PAGE) });
  if (cat) qs.set("category", cat); if (q.trim()) qs.set("q", q.trim()); if (from) qs.set("from", from); if (to) qs.set("to", to);
  const { d, err } = useGet(() => (badRange ? Promise.resolve(null) : req<HistoryResp>(`/history?${qs}`)), [tick, cat, q, from, to, page, badRange]);

  const total = d?.total ?? 0, last = Math.min(total, page * PAGE + PAGE);
  const filtered = cat || q.trim() || from || to;
  const reset = () => { setCat(""); setQLive(""); setQ(""); setFrom(""); setTo(""); setPage(0); };

  return (
    <div className="hs">
      <h1>History</h1>
      <div className="hs-bar">
        <select value={cat} onChange={(e) => { setCat(e.target.value); setPage(0); }}>
          <option value="">All events</option>
          {(meta.d?.categories ?? []).map((c) => <option key={c.category} value={c.category}>{label(c.category)} ({c.count})</option>)}
        </select>
        <input className="hs-search" placeholder="Search (product, invoice #, PO-00001, reason…)" value={qLive} onChange={(e) => setQLive(e.target.value)} />
        <label>From <input type="date" value={from} max={to || undefined} onChange={(e) => { setFrom(e.target.value); setPage(0); }} /></label>
        <label>To <input type="date" value={to} min={from || undefined} onChange={(e) => { setTo(e.target.value); setPage(0); }} /></label>
        {filtered && <button onClick={reset}>Clear filters</button>}
      </div>
      {badRange && <div className="err">“From” is after “To”.</div>}
      {err && <div className="err">{err}</div>}
      <table>
        <thead><tr><th>When</th><th>Who</th><th>Event</th><th>What happened</th><th /></tr></thead>
        <tbody>
          {(d?.entries ?? []).map((e) => (
            <Fragment key={e.id}>
              <tr className="hs-row" onClick={() => setOpen(open === e.id ? null : e.id)}>
                <td className="hs-when">{new Date(e.ts).toLocaleString()}</td>
                <td className="hs-who">{e.actor === "system" ? <span className="hint">before login</span> : e.actor}{e.approved_by && <div className="hint">approved by {e.approved_by}</div>}</td>
                <td><span className={`hs-tag hs-${catOf(e.action)}`}>{label(catOf(e.action))}</span> <span className="hint">{verb(e.action)}</span></td>
                <td>{e.summary}</td>
                <td className="hs-more">{e.details != null ? (open === e.id ? "▾" : "▸") : ""}</td>
              </tr>
              {open === e.id && e.details != null && <tr className="hs-detail"><td colSpan={5}><pre>{JSON.stringify(e.details, null, 2)}</pre></td></tr>}
            </Fragment>
          ))}
          {d && d.entries.length === 0 && <tr><td colSpan={5} className="hint">{filtered ? "Nothing matches these filters." : "No history yet."}</td></tr>}
        </tbody>
      </table>
      <div className="hs-pager">
        <button disabled={page === 0} onClick={() => setPage(page - 1)}>← Newer</button>
        <span className="hint">{total === 0 ? "0 entries" : `${page * PAGE + 1}–${last} of ${total}`}</span>
        <button disabled={last >= total} onClick={() => setPage(page + 1)}>Older →</button>
      </div>
    </div>
  );
}
