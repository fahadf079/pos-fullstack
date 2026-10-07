import { Fragment, useEffect, useState } from "react";
import { fq, req } from "./api";
import { useAuth } from "./auth";
import "./catalog.css";

interface CProduct { id: number; sku: string; name: string; cat: string; price: number; cost: number; stock: number; unit: string; active: boolean; in_cart: number }
interface Meta { categories: string[]; units: { code: string; label: string }[] }
interface Hist { ts: string; field: string; old: string | null; new: string | null }
interface Saved { product: CProduct; warning: string; changed?: string[] }

const rs = (n: number) => "Rs " + (Math.round(n * 100) / 100).toLocaleString(undefined, { maximumFractionDigits: 2 });
const UNIT_LABEL: Record<string, string> = { pc: "piece", kg: "kg", l: "litre" };
const FIELD_LABEL: Record<string, string> = { created: "created", name: "name", cat: "category", price: "price", cost: "cost", unit: "unit", active: "active" };

// fetch with a stale-response guard (a slow older answer must not overwrite a newer one)
function useGet<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [d, setD] = useState<T | null>(null);
  useEffect(() => { let on = true; fn().then((x) => { if (on) setD(x); }).catch(() => {}); return () => { on = false; }; }, deps); // eslint-disable-line
  return d;
}

interface Draft { name: string; cat: string; unit: string; price: string; cost: string }
const toDraft = (p: CProduct): Draft => ({ name: p.name, cat: p.cat, unit: p.unit, price: String(p.price), cost: p.cost ? String(p.cost) : "" });
const blankNew = { sku: "", name: "", cat: "", unit: "pc", price: "", cost: "" };

export function Catalog({ tick }: { tick: number }) {
  const { withPin } = useAuth();
  const [n, setN] = useState(0);
  const products = useGet(() => req<CProduct[]>("/catalog/products?include_inactive=true"), [tick, n]);
  const meta = useGet(() => req<Meta>("/catalog/meta"), [tick, n]);
  const [q, setQ] = useState(""), [showOff, setShowOff] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "bad" | "warn"; text: string } | null>(null);
  const [editId, setEditId] = useState<number | null>(null), [draft, setDraft] = useState<Draft>(toDraft({ id: 0, sku: "", name: "", cat: "", price: 0, cost: 0, stock: 0, unit: "pc", active: true, in_cart: 0 }));
  const [adding, setAdding] = useState(false), [nf, setNf] = useState(blankNew);
  const [histId, setHistId] = useState<number | null>(null), [hist, setHist] = useState<Hist[]>([]);
  const [busy, setBusy] = useState(false);

  const all = products ?? [];
  const list = all.filter((p) => (showOff || p.active) && (p.name + p.sku + p.cat).toLowerCase().includes(q.trim().toLowerCase()));
  const noPrice = all.filter((p) => p.active && p.price <= 0).length;
  const below = all.filter((p) => p.active && p.cost > 0 && p.price > 0 && p.price < p.cost).length;

  const fail = (e: unknown) => setMsg({ kind: "bad", text: (e as Error).message });
  const belowCostAsk = (price: number, cost: number) =>
    cost > 0 && price < cost ? window.confirm(`The selling price ${rs(price)} is BELOW the purchase cost ${rs(cost)}: you lose ${rs(cost - price)} on every unit sold.\n\nSave anyway?`) : true;

  async function create() {
    setMsg(null);
    const price = Number(nf.price), cost = Number(nf.cost || 0);
    if (!nf.sku.trim() || !nf.name.trim()) return setMsg({ kind: "bad", text: "Barcode and name are required." });
    if (!Number.isFinite(price) || price <= 0) return setMsg({ kind: "bad", text: "The selling price must be above 0." });
    if (!Number.isFinite(cost) || cost < 0) return setMsg({ kind: "bad", text: "Cost must be 0 or more." });
    if (!belowCostAsk(price, cost)) return;
    setBusy(true);
    try {
      const r = await req<Saved>("/catalog/products", { sku: nf.sku.trim(), name: nf.name.trim(), cat: nf.cat.trim() || "Grocery", unit: nf.unit, price, cost });
      setMsg({ kind: r.warning ? "warn" : "ok", text: `"${r.product.name}" created with 0 stock. Add stock by receiving a purchase order (or Inventory → Adjust for opening stock).${r.warning ? " " + r.warning : ""}` });
      setNf(blankNew); setAdding(false); setN((x) => x + 1);
    } catch (e) { fail(e); }
    setBusy(false);
  }

  async function save(p: CProduct) {
    setMsg(null);
    const price = Number(draft.price), cost = Number(draft.cost || 0);
    if (!draft.name.trim()) return setMsg({ kind: "bad", text: "Name can't be empty." });
    if (!Number.isFinite(price) || price <= 0) return setMsg({ kind: "bad", text: "The selling price must be above 0." });
    if (!Number.isFinite(cost) || cost < 0) return setMsg({ kind: "bad", text: "Cost must be 0 or more." });
    if ((price !== p.price || cost !== p.cost) && !belowCostAsk(price, cost)) return;
    setBusy(true);
    try {
      const body = { name: draft.name.trim(), cat: draft.cat.trim() || "Grocery", unit: draft.unit, price, cost };
      const r = await withPin(`Change price/cost of ${p.name}`, (a) => req<Saved>(`/catalog/products/${p.id}/update`, body, a));   // only asks when price or cost really changed
      setMsg(r.changed && r.changed.length === 0 ? { kind: "ok", text: "Nothing changed." } : { kind: r.warning ? "warn" : "ok", text: `Saved "${r.product.name}".${r.warning ? " " + r.warning : ""}` });
      setEditId(null); setN((x) => x + 1);
      if (histId === p.id) openHistory(p.id, true);
    } catch (e) { fail(e); }
    setBusy(false);
  }

  async function toggle(p: CProduct) {
    setMsg(null);
    const off = p.active;
    const ask = off
      ? `Deactivate "${p.name}"?\n\nIt can no longer be scanned, sold or ordered. Its history stays, and refunds of old sales still work.${p.stock > 0 ? `\n\nNote: ${fq(p.stock, p.unit)} are still in stock.` : ""}`
      : `Reactivate "${p.name}"?`;
    if (!window.confirm(ask)) return;
    try { await req(`/catalog/products/${p.id}/active`, { active: !off }); setN((x) => x + 1); } catch (e) { fail(e); }
  }

  async function openHistory(id: number, force = false) {
    if (histId === id && !force) { setHistId(null); return; }
    try { setHist(await req<Hist[]>(`/catalog/products/${id}/history`)); setHistId(id); } catch (e) { fail(e); }
  }

  const margin = (p: CProduct) => (p.price > 0 && p.cost > 0 ? `${Math.round(((p.price - p.cost) / p.price) * 100)}%` : "—");

  return (
    <div className="cg">
      <h1>Catalog <small>(live)</small></h1>
      {msg && <div className={`cg-msg ${msg.kind}`}>{msg.text}</div>}
      {(noPrice > 0 || below > 0) && (
        <div className="cg-msg warn">
          {noPrice > 0 && <div><b>{noPrice}</b> active product(s) have no selling price and can't be sold until you set one.</div>}
          {below > 0 && <div><b>{below}</b> active product(s) are priced below their purchase cost.</div>}
        </div>
      )}
      <div className="cg-bar">
        <input value={q} placeholder="Search name / barcode / category" onChange={(e) => setQ(e.target.value)} />
        <label><input type="checkbox" checked={showOff} onChange={(e) => setShowOff(e.target.checked)} /> Show deactivated</label>
        <span className="cg-sub">{list.length} shown · {all.filter((p) => p.active).length} active</span>
        <button className="primary" onClick={() => { setAdding((x) => !x); setMsg(null); }}>{adding ? "Close" : "+ New product"}</button>
      </div>

      {adding && (
        <div className="cg-form">
          <label>Barcode / SKU<input value={nf.sku} maxLength={40} onChange={(e) => setNf({ ...nf, sku: e.target.value })} /></label>
          <button type="button" onClick={() => void req<{ sku: string }>("/catalog/next-barcode").then((r) => setNf((x) => ({ ...x, sku: r.sku }))).catch((e: Error) => setMsg({ kind: "bad", text: e.message }))}>Make a barcode</button>
          <label>Name<input value={nf.name} maxLength={120} onChange={(e) => setNf({ ...nf, name: e.target.value })} /></label>
          <label>Category<input list="cg-cats" value={nf.cat} placeholder="Grocery" maxLength={40} onChange={(e) => setNf({ ...nf, cat: e.target.value })} /></label>
          <label>Sold by
            <select value={nf.unit} onChange={(e) => setNf({ ...nf, unit: e.target.value })}>
              {(meta?.units ?? [{ code: "pc", label: "piece" }]).map((u) => <option key={u.code} value={u.code}>{u.code === "pc" ? "Piece (whole numbers)" : `Weight / volume (${u.code})`}</option>)}
            </select>
          </label>
          <label>Selling price{nf.unit !== "pc" ? ` (per ${nf.unit})` : ""}<input type="number" min="0.01" step="0.01" value={nf.price} onChange={(e) => setNf({ ...nf, price: e.target.value })} /></label>
          <label>Cost (optional)<input type="number" min="0" step="0.01" value={nf.cost} onChange={(e) => setNf({ ...nf, cost: e.target.value })} /></label>
          <div className="cg-actions"><button className="primary" disabled={busy} onClick={create}>{busy ? "Saving…" : "Create product"}</button></div>
        </div>
      )}
      <datalist id="cg-cats">{(meta?.categories ?? []).map((c) => <option key={c} value={c} />)}</datalist>

      <table>
        <thead><tr><th>Barcode</th><th>Name</th><th>Category</th><th>Sold by</th><th>Cost</th><th>Price</th><th>Margin</th><th>Stock</th><th></th></tr></thead>
        <tbody>
          {list.map((p) => {
            const editing = editId === p.id;
            return (
              <Fragment key={p.id}>
                <tr className={p.active ? "" : "cg-inactive"}>
                  <td>{p.sku}</td>
                  {editing ? (<>
                    <td><input value={draft.name} maxLength={120} onChange={(e) => setDraft({ ...draft, name: e.target.value })} /></td>
                    <td><input list="cg-cats" value={draft.cat} maxLength={40} onChange={(e) => setDraft({ ...draft, cat: e.target.value })} /></td>
                    <td><select value={draft.unit} onChange={(e) => setDraft({ ...draft, unit: e.target.value })}>{(meta?.units ?? []).map((u) => <option key={u.code} value={u.code}>{UNIT_LABEL[u.code] ?? u.code}</option>)}</select></td>
                    <td><input type="number" min="0" step="0.01" value={draft.cost} onChange={(e) => setDraft({ ...draft, cost: e.target.value })} /></td>
                    <td><input type="number" min="0.01" step="0.01" value={draft.price} onChange={(e) => setDraft({ ...draft, price: e.target.value })} /></td>
                    <td className="cg-num">{Number(draft.price) > 0 && Number(draft.cost) > 0 ? `${Math.round(((Number(draft.price) - Number(draft.cost)) / Number(draft.price)) * 100)}%` : "—"}</td>
                    <td className="cg-num">{fq(p.stock, p.unit)}</td>
                    <td><button className="primary" disabled={busy} onClick={() => save(p)}>Save</button><button onClick={() => { setEditId(null); setMsg(null); }}>Cancel</button></td>
                  </>) : (<>
                    <td>{p.name}
                      {!p.active && <span className="cg-tag cg-off">DEACTIVATED</span>}
                      {p.active && p.price <= 0 && <span className="cg-tag cg-noprice">NO PRICE</span>}
                      {p.active && p.cost > 0 && p.price > 0 && p.price < p.cost && <span className="cg-tag cg-below">BELOW COST</span>}
                    </td>
                    <td>{p.cat}</td><td>{UNIT_LABEL[p.unit] ?? p.unit}</td>
                    <td className="cg-num">{p.cost ? rs(p.cost) : "—"}</td>
                    <td className="cg-num">{p.price > 0 ? rs(p.price) : "—"}{p.unit !== "pc" ? `/${p.unit}` : ""}</td>
                    <td className="cg-num">{margin(p)}</td>
                    <td className="cg-num">{fq(p.stock, p.unit)}</td>
                    <td>
                      <button onClick={() => { setEditId(p.id); setDraft(toDraft(p)); setMsg(null); }}>Edit</button>
                      <button onClick={() => toggle(p)}>{p.active ? "Deactivate" : "Reactivate"}</button>
                      <button onClick={() => openHistory(p.id)}>{histId === p.id ? "Hide history" : "History"}</button>
                    </td>
                  </>)}
                </tr>
                {histId === p.id && (
                  <tr><td colSpan={9}><div className="cg-hist">
                    {hist.length === 0 ? "No changes recorded yet." : hist.map((h, i) => (
                      <div key={i}>{new Date(h.ts).toLocaleString()} — <b>{FIELD_LABEL[h.field] ?? h.field}</b>: {h.old === null ? "" : `${h.old} → `}{h.new}</div>
                    ))}
                  </div></td></tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
      {!list.length && <p className="hint">No products match.</p>}
    </div>
  );
}
