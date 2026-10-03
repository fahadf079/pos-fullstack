import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { req as baseReq } from "./api";
import type { Auth } from "./api";
import { useAuth } from "./auth";

/* Uses the app's own fetch layer (api.ts): GET when there is no body, POST otherwise. */
async function req<T>(path: string, method = "GET", body?: unknown, auth?: Auth): Promise<T> {
  return method === "GET" ? baseReq<T>(path, undefined, auth) : baseReq<T>(path, body ?? {}, auth);
}

export interface PProduct { id: number; sku: string; name: string; cat: string; price: number; stock: number; cost: number; unit: string }
export interface Supplier { id: number; name: string; phone: string; note: string; active: boolean; open_pos: number; owed: number }
export interface POItem { id: number; product_id: number | null; sku: string; name: string; cat: string; sale_price: number; unit: string; ordered: number; received: number; remaining: number; unit_cost: number; is_new: boolean }
export interface PO { id: number; po_no: string; supplier_id: number; supplier: string; ts: string; expected: string; status: string; note: string; line_count: number; ordered_value: number; ordered_units: number; received_units: number; items?: POItem[]; receipts?: { id: number; pur_no: string; ts: string; total: number; paid: boolean }[] }
export interface Purchase { id: number; pur_no: string; po_no: string; supplier: string; ts: string; total: number; units: number; lines: number; paid: boolean; paid_ts: string | null; pay_method: string }
export interface PurchaseDetail extends Purchase { note: string; items: { name: string; qty: number; unit_cost: number; line_total: number }[] }
export interface Expense { id: number; exp_no: string; ts: string; category: string; amount: number; payee: string; note: string; voided: boolean; voided_ts: string | null; void_reason: string }
export interface Report {
  from: string; to: string; sales_net: number; sales_count: number; refunded_net: number; refunded_count: number;
  purchases_total: number; purchases_count: number; expenses_total: number; expenses_count: number; cashflow: number;
  unpaid_to_suppliers: number; open_po_count: number; open_po_value: number;
  by_supplier: { name: string; deliveries: number; total: number; unpaid: number }[];
  top_products: { name: string; qty: number; total: number }[];
  expenses_by_category: { category: string; total: number }[];
}

/* ───────────── helpers ───────────── */
const rs = (n: number) => "Rs " + (Math.round(n * 100) / 100).toLocaleString("en-PK", { maximumFractionDigits: 2 });
/* quantities: whole numbers for pieces, up to 3 decimals for kg / litre items */
const r3 = (n: number) => Math.round(n * 1000) / 1000;
const fq = (n: number, unit = "pc") => `${r3(n).toLocaleString("en-PK", { maximumFractionDigits: 3 })}${unit === "pc" ? "" : " " + unit}`;
const qtyOk = (n: number, unit: string, allowZero = false) =>
  Number.isFinite(n) && (allowZero ? n >= 0 : n > 0) && (unit === "pc" ? Number.isInteger(n) : Math.abs(n * 1000 - Math.round(n * 1000)) < 1e-6);
const when = (ts: string | null) => (ts ? new Date(ts).toLocaleString() : "—");
const day = (d: Date) => {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
};
const daysAgo = (n: number) => { const d = new Date(); d.setDate(d.getDate() - n); return day(d); };

function useFetch<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [err, setErr] = useState("");
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(fn, deps);
  useEffect(() => {
    let live = true;
    run().then((d) => { if (live) { setData(d); setErr(""); } }).catch((e) => { if (live) setErr(String(e.message || e)); });
    return () => { live = false; };
  }, [run]);
  return { data, err };
}

function Msg({ ok, text }: { ok?: boolean; text: string }) {
  if (!text) return null;
  return <div className={ok ? "pm-msg pm-ok" : "pm-msg pm-bad"}>{text}</div>;
}

const STATUS_LABEL: Record<string, string> = {
  ORDERED: "🟡 Ordered", PARTIAL: "🟠 Partly received", RECEIVED: "🟢 Received", CLOSED: "⚪ Closed short", CANCELLED: "🔴 Cancelled",
};

/* ═════════════════════════ PURCHASING PAGE ═════════════════════════ */
type Tab = "orders" | "purchases" | "suppliers";

export function Purchasing({ tick }: { tick: number }) {
  const [tab, setTab] = useState<Tab>("orders");
  return (
    <div className="pm">
      <h1>Purchasing</h1>
      <div className="pm-tabs">
        {(["orders", "purchases", "suppliers"] as Tab[]).map((t) => (
          <button key={t} className={tab === t ? "pm-tab on" : "pm-tab"} onClick={() => setTab(t)}>
            {t === "orders" ? "Purchase Orders" : t === "purchases" ? "Purchases (received)" : "Suppliers"}
          </button>
        ))}
      </div>
      {tab === "orders" && <Orders tick={tick} />}
      {tab === "purchases" && <Purchases tick={tick} />}
      {tab === "suppliers" && <Suppliers tick={tick} />}
    </div>
  );
}

/* ───────────── Purchase orders list / create / receive ───────────── */
function Orders({ tick }: { tick: number }) {
  const [filter, setFilter] = useState("");
  const [mode, setMode] = useState<{ kind: "list" } | { kind: "new" } | { kind: "view" | "receive"; id: number }>({ kind: "list" });
  const [n, setN] = useState(0);
  const { data, err } = useFetch(() => req<PO[]>("/purchase-orders" + (filter ? `?status=${filter}` : "")), [filter, tick, n]);
  const back = () => { setMode({ kind: "list" }); setN((x) => x + 1); };

  if (mode.kind === "new") return <NewPO onDone={back} />;
  if (mode.kind === "receive") return <ReceivePO id={mode.id} onDone={back} />;
  if (mode.kind === "view") return <ViewPO id={mode.id} onBack={back} onReceive={() => setMode({ kind: "receive", id: mode.id })} />;

  return (
    <div>
      <div className="pm-row">
        <button className="pm-primary" onClick={() => setMode({ kind: "new" })}>+ New Purchase Order</button>
        <select value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="">All statuses</option>
          {Object.keys(STATUS_LABEL).map((s) => <option key={s} value={s}>{STATUS_LABEL[s]}</option>)}
        </select>
      </div>
      <Msg text={err} />
      <table className="pm-table">
        <thead><tr><th>PO</th><th>Supplier</th><th>Created</th><th>Expected</th><th>Units (recv/ord)</th><th>Value</th><th>Status</th><th /></tr></thead>
        <tbody>
          {(data ?? []).map((p) => (
            <tr key={p.id}>
              <td><b>{p.po_no}</b></td><td>{p.supplier}</td><td>{when(p.ts)}</td><td>{p.expected || "—"}</td>
              <td>{p.received_units} / {p.ordered_units}</td><td>{rs(p.ordered_value)}</td><td>{STATUS_LABEL[p.status] ?? p.status}</td>
              <td className="pm-actions">
                <button onClick={() => setMode({ kind: "view", id: p.id })}>View</button>
                {(p.status === "ORDERED" || p.status === "PARTIAL") && (
                  <button className="pm-primary" onClick={() => setMode({ kind: "receive", id: p.id })}>Receive stock</button>
                )}
              </td>
            </tr>
          ))}
          {data && data.length === 0 && <tr><td colSpan={8} className="pm-empty">No purchase orders yet.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

interface DraftLine { key: number; mode: "existing" | "new"; product_id: number | ""; sku: string; name: string; cat: string; sale_price: string; unit: string; qty: string; unit_cost: string }
let lineKey = 1;
const blankLine = (): DraftLine => ({ key: lineKey++, mode: "existing", product_id: "", sku: "", name: "", cat: "Grocery", sale_price: "", unit: "pc", qty: "", unit_cost: "" });

function NewPO({ onDone }: { onDone: () => void }) {
  const { data: suppliers } = useFetch(() => req<Supplier[]>("/suppliers?include_inactive=false"), []);
  const { data: products } = useFetch(() => req<PProduct[]>("/purchasing/products"), []);
  const [supplierId, setSupplierId] = useState<number | "">("");
  const [expected, setExpected] = useState("");
  const [note, setNote] = useState("");
  const [lines, setLines] = useState<DraftLine[]>([blankLine()]);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const upd = (key: number, patch: Partial<DraftLine>) => setLines((ls) => ls.map((l) => (l.key === key ? { ...l, ...patch } : l)));
  const pick = (key: number, text: string) => {
    // datalist gives "Name — SKU"; match on that label
    const p = (products ?? []).find((x) => `${x.name} — ${x.sku}` === text);
    upd(key, p ? { product_id: p.id, name: text, unit_cost: p.cost ? String(p.cost) : "" } : { product_id: "", name: text });
  };
  const total = lines.reduce((s, l) => s + (Number(l.qty) || 0) * (Number(l.unit_cost) || 0), 0);
  const unitOf = (l: DraftLine) => (l.mode === "existing" ? (products ?? []).find((p) => p.id === l.product_id)?.unit ?? "pc" : l.unit);

  async function submit() {
    setMsg("");
    if (supplierId === "") return setMsg("Choose a supplier (or add one in the Suppliers tab).");
    const payload = [];
    for (let i = 0; i < lines.length; i++) {
      const l = lines[i];
      const qty = Number(l.qty), cost = Number(l.unit_cost);
      if (!qtyOk(qty, unitOf(l))) return setMsg(unitOf(l) === "pc" ? `Line ${i + 1}: quantity must be a whole number above 0.` : `Line ${i + 1}: quantity must be above 0 (up to 3 decimals).`);
      if (l.unit_cost === "" || !(cost >= 0)) return setMsg(`Line ${i + 1}: enter the unit cost.`);
      if (l.mode === "existing") {
        if (l.product_id === "") return setMsg(`Line ${i + 1}: pick a product from the list.`);
        payload.push({ product_id: l.product_id, qty, unit_cost: cost });
      } else {
        if (!l.sku.trim() || !l.name.trim()) return setMsg(`Line ${i + 1}: new product needs SKU and name.`);
        if (l.sale_price === "" || !(Number(l.sale_price) > 0)) return setMsg(`Line ${i + 1}: the selling price must be above 0.`);
        payload.push({ sku: l.sku.trim(), name: l.name.trim(), cat: l.cat.trim() || "Grocery", unit: l.unit, sale_price: Number(l.sale_price), qty, unit_cost: cost });
      }
    }
    setBusy(true);
    try {
      await req("/purchase-orders", "POST", { supplier_id: supplierId, expected, note, lines: payload });
      onDone();
    } catch (e: any) { setMsg(e.message); setBusy(false); }
  }

  return (
    <div className="pm-card">
      <h3>New Purchase Order</h3>
      <p className="pm-hint">An order is only a request to the supplier — stock does <b>not</b> change until you receive the goods.</p>
      <div className="pm-row">
        <label>Supplier
          <select value={supplierId} onChange={(e) => setSupplierId(e.target.value ? Number(e.target.value) : "")}>
            <option value="">Select…</option>
            {(suppliers ?? []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </label>
        <label>Expected date<input type="date" value={expected} onChange={(e) => setExpected(e.target.value)} /></label>
        <label className="pm-grow">Note<input value={note} maxLength={300} onChange={(e) => setNote(e.target.value)} /></label>
      </div>

      <datalist id="pm-products">
        {(products ?? []).map((p) => <option key={p.id} value={`${p.name} — ${p.sku}`} />)}
      </datalist>

      <table className="pm-table">
        <thead><tr><th>Product</th><th>Qty</th><th>Unit cost</th><th>Line total</th><th /></tr></thead>
        <tbody>
          {lines.map((l) => (
            <tr key={l.key}>
              <td>
                {l.mode === "existing" ? (
                  <input list="pm-products" placeholder="Type product name or SKU…" value={l.name} onChange={(e) => pick(l.key, e.target.value)} />
                ) : (
                  <div className="pm-newprod">
                    <input placeholder="SKU / barcode" value={l.sku} onChange={(e) => upd(l.key, { sku: e.target.value })} />
                    <input placeholder="Product name" value={l.name} onChange={(e) => upd(l.key, { name: e.target.value })} />
                    <input placeholder="Category" value={l.cat} onChange={(e) => upd(l.key, { cat: e.target.value })} />
                    <input placeholder="Selling price (per unit)" type="number" min="0.01" step="0.01" value={l.sale_price} onChange={(e) => upd(l.key, { sale_price: e.target.value })} />
                    <select value={l.unit} onChange={(e) => upd(l.key, { unit: e.target.value })}>
                      <option value="pc">Sold by the piece</option><option value="kg">Sold by weight (kg)</option><option value="l">Sold by volume (litre)</option>
                    </select>
                  </div>
                )}
                {l.mode === "new" && Number(l.sale_price) > 0 && Number(l.unit_cost) > Number(l.sale_price) && <div className="pm-warn">Selling price is below the cost: every sale loses {rs(Number(l.unit_cost) - Number(l.sale_price))}.</div>}
                <button className="pm-link" onClick={() => upd(l.key, { mode: l.mode === "existing" ? "new" : "existing", product_id: "", name: "", sku: "" })}>
                  {l.mode === "existing" ? "Not in the catalog? Add new product" : "← Pick existing product"}
                </button>
              </td>
              <td><input type="number" min={unitOf(l) === "pc" ? "1" : "0.001"} step={unitOf(l) === "pc" ? "1" : "0.001"} value={l.qty} onChange={(e) => upd(l.key, { qty: e.target.value })} />{unitOf(l) !== "pc" && <small> {unitOf(l)}</small>}</td>
              <td><input type="number" min="0" step="0.01" value={l.unit_cost} onChange={(e) => upd(l.key, { unit_cost: e.target.value })} /></td>
              <td>{rs((Number(l.qty) || 0) * (Number(l.unit_cost) || 0))}</td>
              <td><button disabled={lines.length === 1} onClick={() => setLines((ls) => ls.filter((x) => x.key !== l.key))}>✕</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="pm-row">
        <button onClick={() => setLines((ls) => [...ls, blankLine()])}>+ Add line</button>
        <span className="pm-grow" />
        <b>Total: {rs(total)}</b>
      </div>
      <Msg text={msg} />
      <div className="pm-row">
        <button className="pm-primary" disabled={busy} onClick={submit}>{busy ? "Saving…" : "Create Purchase Order"}</button>
        <button disabled={busy} onClick={onDone}>Cancel</button>
      </div>
    </div>
  );
}

function ViewPO({ id, onBack, onReceive }: { id: number; onBack: () => void; onReceive: () => void }) {
  const { withPin } = useAuth();
  const [n, setN] = useState(0);
  const { data: po, err } = useFetch(() => req<PO>(`/purchase-orders/${id}`), [id, n]);
  const [msg, setMsg] = useState("");
  async function act(path: string, ask: string) {
    if (!window.confirm(ask)) return;
    try { await withPin(path.endsWith("cancel") ? "Cancel this order" : "Close this order", (a) => req(path, "POST", undefined, a)); setN((x) => x + 1); setMsg(""); } catch (e: any) { setMsg(e.message); }
  }
  if (!po) return <div><Msg text={err} /><button onClick={onBack}>← Back</button></div>;
  const open = po.status === "ORDERED" || po.status === "PARTIAL";
  return (
    <div className="pm-card">
      <button onClick={onBack}>← Back</button>
      <h3>{po.po_no} — {po.supplier} <small>{STATUS_LABEL[po.status] ?? po.status}</small></h3>
      <p className="pm-hint">Created {when(po.ts)}{po.expected ? ` · expected ${po.expected}` : ""}{po.note ? ` · ${po.note}` : ""}</p>
      <table className="pm-table">
        <thead><tr><th>Product</th><th>SKU</th><th>Ordered</th><th>Received</th><th>Remaining</th><th>Unit cost</th></tr></thead>
        <tbody>
          {(po.items ?? []).map((i) => (
            <tr key={i.id}><td>{i.name}{i.is_new && <span className="pm-tag">NEW PRODUCT</span>}</td><td>{i.sku}</td><td>{fq(i.ordered, i.unit)}</td><td>{fq(i.received, i.unit)}</td><td>{fq(i.remaining, i.unit)}</td><td>{rs(i.unit_cost)}</td></tr>
          ))}
        </tbody>
      </table>
      <b>Order value: {rs(po.ordered_value)}</b>
      {(po.receipts ?? []).length > 0 && (
        <p>Deliveries: {(po.receipts ?? []).map((r) => `${r.pur_no} (${rs(r.total)})`).join(", ")}</p>
      )}
      <Msg text={msg} />
      <div className="pm-row">
        {open && <button className="pm-primary" onClick={onReceive}>Receive stock</button>}
        {po.status === "ORDERED" && <button onClick={() => act(`/purchase-orders/${id}/cancel`, `Cancel ${po.po_no}?`)}>Cancel order</button>}
        {po.status === "PARTIAL" && <button onClick={() => act(`/purchase-orders/${id}/close`, "Close this order short? The remaining units will no longer be expected.")}>Close short</button>}
      </div>
    </div>
  );
}

function ReceivePO({ id, onDone }: { id: number; onDone: () => void }) {
  const { withPin } = useAuth();
  const { data: po, err } = useFetch(() => req<PO>(`/purchase-orders/${id}`), [id]);
  const [qty, setQty] = useState<Record<number, string>>({});
  const [cost, setCost] = useState<Record<number, string>>({});
  const [closeShort, setCloseShort] = useState(false);
  const [paid, setPaid] = useState(false);
  const [method, setMethod] = useState("Cash");
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [scan, setScan] = useState("");
  const [scanMsg, setScanMsg] = useState("");
  const scanRef = useRef<HTMLInputElement>(null);
  const [done, setDone] = useState<{ pur_no: string; total: number; po_status: string; units_short: number; new_products: string[] } | null>(null);

  if (done) {
    return (
      <div className="pm-card">
        <h3>✔ Stock received</h3>
        <p>{done.pur_no} recorded for {rs(done.total)}. Inventory has been updated.</p>
        {done.new_products.length > 0 && <p>New products created: {done.new_products.join(", ")}</p>}
        {done.units_short > 0 && <p>Order closed short by {fq(done.units_short)} unit(s).</p>}
        <button className="pm-primary" onClick={onDone}>Done</button>
      </div>
    );
  }
  if (!po) return <div><Msg text={err} /><button onClick={onDone}>← Back</button></div>;
  const items = (po.items ?? []).filter((i) => i.remaining > 0);
  const q = (i: POItem) => (qty[i.id] !== undefined ? qty[i.id] : String(i.remaining));
  const c = (i: POItem) => (cost[i.id] !== undefined ? cost[i.id] : String(i.unit_cost));
  const total = items.reduce((s, i) => s + (Number(q(i)) || 0) * (Number(c(i)) || 0), 0);
  const short = r3(items.reduce((s, i) => s + Math.max(0, i.remaining - (Number(q(i)) || 0)), 0));

  function onScan() {
    const v = scan.trim();
    if (!v) return;
    const it = items.find((i) => i.sku === v);
    if (!it) setScanMsg(`"${v}" is not on this order, or is already fully received.`);
    else {
      const cur = Number(q(it)) || 0;
      if (cur >= it.remaining) setScanMsg(`${it.name}: already at the ordered quantity (${fq(it.remaining, it.unit)}).`);
      else { const next = r3(Math.min(cur + 1, it.remaining)); setQty({ ...qty, [it.id]: String(next) }); setScanMsg(`${it.name} → ${fq(next, it.unit)} received${it.unit !== "pc" ? " (weighed items: type the exact weight in the box)" : ""}`); }
    }
    setScan("");
    scanRef.current?.focus();
  }

  async function confirm() {
    setMsg("");
    const lines: { po_item_id: number; qty: number; unit_cost: number }[] = [];
    for (const i of items) {
      const n = Number(q(i)), uc = Number(c(i));
      if (!qtyOk(n, i.unit, true)) return setMsg(i.unit === "pc" ? `${i.name}: received quantity must be a whole number (0 or more).` : `${i.name}: received quantity must be 0 or more (up to 3 decimals).`);
      if (n > i.remaining) return setMsg(`${i.name}: only ${fq(i.remaining, i.unit)} still expected.`);
      if (c(i) === "" || !(uc >= 0)) return setMsg(`${i.name}: enter a valid cost.`);
      lines.push({ po_item_id: i.id, qty: n, unit_cost: uc });
    }
    if (!lines.some((l) => l.qty > 0)) return setMsg("Enter a received quantity for at least one line.");
    if (short > 0 && closeShort && !window.confirm(`${short} unit(s) will be marked as never delivered and the order closed. Continue?`)) return;
    setBusy(true);
    try {
      const r = await withPin("Receive stock into the shop", (a) => req<NonNullable<typeof done>>(`/purchase-orders/${id}/receive`, "POST", { lines, close_short: closeShort, paid, pay_method: paid ? method : "", note }, a));
      setDone(r);
    } catch (e: any) { setMsg(e.message); setBusy(false); }
  }

  return (
    <div className="pm-card">
      <h3>Receive stock — {po.po_no}</h3>
      <p className="pm-hint">Supplier: <b>{po.supplier}</b>. Enter what <b>actually arrived</b> — the supplier may deliver less than ordered.</p>
      <div className="pm-row">
        <input ref={scanRef} autoFocus className="pm-grow" value={scan} placeholder="Scan each item's barcode as you unpack — Enter adds 1 to its received quantity"
          onChange={(e) => setScan(e.target.value)} onKeyDown={(e) => e.key === "Enter" && onScan()} />
        <button onClick={() => { setQty(Object.fromEntries(items.map((i) => [i.id, "0"]))); setScanMsg("Counting from 0 — scan items now."); scanRef.current?.focus(); }}>Start counting from 0</button>
        <button onClick={() => { setQty({}); setScanMsg(""); }}>Fill all with ordered qty</button>
      </div>
      {scanMsg && <p className="hint">{scanMsg}</p>}
      <table className="pm-table">
        <thead><tr><th>Product</th><th>Ordered</th><th>Already received</th><th>Received now</th><th>Cost / unit</th></tr></thead>
        <tbody>
          {items.map((i) => (
            <tr key={i.id}>
              <td>{i.name}{i.is_new && <span className="pm-tag">NEW PRODUCT</span>}</td>
              <td>{fq(i.ordered, i.unit)}</td><td>{fq(i.received, i.unit)}</td>
              <td><input type="number" min="0" max={i.remaining} step={i.unit === "pc" ? "1" : "0.001"} value={q(i)} onChange={(e) => setQty({ ...qty, [i.id]: e.target.value })} /></td>
              <td><input type="number" min="0" step="0.01" value={c(i)} onChange={(e) => setCost({ ...cost, [i.id]: e.target.value })} /></td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="pm-row"><b>Delivery value: {rs(total)}</b>{short > 0 && <span className="pm-warn">{short} unit(s) still missing</span>}</div>
      {short > 0 && (
        <label className="pm-check"><input type="checkbox" checked={closeShort} onChange={(e) => setCloseShort(e.target.checked)} />
          Supplier won't send the rest — close this order (otherwise it stays open for a later delivery)</label>
      )}
      <div className="pm-row">
        <label className="pm-check"><input type="checkbox" checked={paid} onChange={(e) => setPaid(e.target.checked)} /> Paid to supplier now</label>
        {paid && <select value={method} onChange={(e) => setMethod(e.target.value)}><option>Cash</option><option>Bank</option><option>Wallet</option></select>}
        <label className="pm-grow">Note<input value={note} maxLength={300} onChange={(e) => setNote(e.target.value)} /></label>
      </div>
      <Msg text={msg} />
      <div className="pm-row">
        <button className="pm-primary" disabled={busy} onClick={confirm}>{busy ? "Updating inventory…" : "CONFIRM RECEIVING"}</button>
        <button disabled={busy} onClick={onDone}>Cancel</button>
      </div>
    </div>
  );
}

/* ───────────── Purchases (received deliveries) ───────────── */
function Purchases({ tick }: { tick: number }) {
  const { withPin } = useAuth();
  const [unpaid, setUnpaid] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
  const [n, setN] = useState(0);
  const [msg, setMsg] = useState("");
  const { data, err } = useFetch(() => req<Purchase[]>(`/purchases${unpaid ? "?unpaid_only=true" : ""}`), [unpaid, tick, n]);
  const { data: detail } = useFetch(() => (open ? req<PurchaseDetail>(`/purchases/${open}`) : Promise.resolve(null)), [open, tick, n]);

  async function pay(p: Purchase) {
    const m = window.prompt(`Mark ${p.pur_no} (${rs(p.total)}) as paid. Method? (Cash / Bank / Wallet)`, "Cash");
    if (m === null) return;
    try { await withPin(`Mark ${p.pur_no} as paid`, (a) => req(`/purchases/${p.id}/pay`, "POST", { pay_method: m }, a)); setN((x) => x + 1); setMsg(""); } catch (e: any) { setMsg(e.message); }
  }
  return (
    <div>
      <label className="pm-check"><input type="checkbox" checked={unpaid} onChange={(e) => setUnpaid(e.target.checked)} /> Unpaid only</label>
      <Msg text={err || msg} />
      <table className="pm-table">
        <thead><tr><th>Purchase</th><th>PO</th><th>Supplier</th><th>Received</th><th>Units</th><th>Total</th><th>Payment</th><th /></tr></thead>
        <tbody>
          {(data ?? []).map((p) => (
            <Fragment key={p.id}>
              <tr className="pm-click" onClick={() => setOpen(open === p.id ? null : p.id)}>
                <td><b>{p.pur_no}</b></td><td>{p.po_no}</td><td>{p.supplier}</td><td>{when(p.ts)}</td><td>{p.units}</td><td>{rs(p.total)}</td>
                <td>{p.paid ? `✔ Paid (${p.pay_method || "—"})` : "⏳ Unpaid"}</td>
                <td>{!p.paid && <button onClick={(e) => { e.stopPropagation(); pay(p); }}>Mark paid</button>}</td>
              </tr>
              {open === p.id && detail && (
                <tr><td colSpan={8}>
                  <table className="pm-table pm-inner"><thead><tr><th>Product</th><th>Qty</th><th>Unit cost</th><th>Line total</th></tr></thead>
                    <tbody>{detail.items.map((i, k) => <tr key={k}><td>{i.name}</td><td>{i.qty}</td><td>{rs(i.unit_cost)}</td><td>{rs(i.line_total)}</td></tr>)}</tbody></table>
                  {detail.note && <p className="pm-hint">Note: {detail.note}</p>}
                </td></tr>
              )}
            </Fragment>
          ))}
          {data && data.length === 0 && <tr><td colSpan={8} className="pm-empty">No purchases recorded yet.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

/* ───────────── Suppliers ───────────── */
function Suppliers({ tick }: { tick: number }) {
  const [n, setN] = useState(0);
  const { data, err } = useFetch(() => req<Supplier[]>("/suppliers"), [tick, n]);
  const [name, setName] = useState(""); const [phone, setPhone] = useState(""); const [note, setNote] = useState("");
  const [msg, setMsg] = useState("");
  async function add() {
    if (!name.trim()) return setMsg("Supplier name is required.");
    try { await req("/suppliers", "POST", { name, phone, note }); setName(""); setPhone(""); setNote(""); setMsg(""); setN((x) => x + 1); } catch (e: any) { setMsg(e.message); }
  }
  async function toggle(s: Supplier) {
    try { await req(`/suppliers/${s.id}/update`, "POST", { active: !s.active }); setN((x) => x + 1); } catch (e: any) { setMsg(e.message); }
  }
  return (
    <div>
      <div className="pm-row">
        <input placeholder="Supplier name" value={name} onChange={(e) => setName(e.target.value)} />
        <input placeholder="Phone" value={phone} onChange={(e) => setPhone(e.target.value)} />
        <input className="pm-grow" placeholder="Note" value={note} onChange={(e) => setNote(e.target.value)} />
        <button className="pm-primary" onClick={add}>Add supplier</button>
      </div>
      <Msg text={err || msg} />
      <table className="pm-table">
        <thead><tr><th>Name</th><th>Phone</th><th>Open POs</th><th>We owe</th><th>Status</th><th /></tr></thead>
        <tbody>
          {(data ?? []).map((s) => (
            <tr key={s.id} className={s.active ? "" : "pm-dim"}>
              <td><b>{s.name}</b>{s.note && <div className="pm-hint">{s.note}</div>}</td><td>{s.phone || "—"}</td><td>{s.open_pos}</td>
              <td>{s.owed > 0 ? rs(s.owed) : "—"}</td><td>{s.active ? "Active" : "Inactive"}</td>
              <td><button onClick={() => toggle(s)}>{s.active ? "Deactivate" : "Reactivate"}</button></td>
            </tr>
          ))}
          {data && data.length === 0 && <tr><td colSpan={6} className="pm-empty">Add your first supplier above.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

/* ═════════════════════════ EXPENSES & REPORTS PAGE ═════════════════════════ */
function Kpi({ label, value, sub }: { label: string; value: string; sub?: ReactNode }) {
  return <div className="pm-kpi"><div className="pm-kpi-l">{label}</div><div className="pm-kpi-v">{value}</div>{sub && <div className="pm-hint">{sub}</div>}</div>;
}

export function Reports({ tick }: { tick: number }) {
  const { withPin } = useAuth();
  const [from, setFrom] = useState(() => daysAgo(29));
  const [to, setTo] = useState(() => day(new Date()));
  const [today] = useState(() => day(new Date()));
  const [n, setN] = useState(0);
  const valid = from !== "" && to !== "" && from <= to;
  const { data: rep, err } = useFetch(() => (valid ? req<Report>(`/reports/purchases?from=${from}&to=${to}`) : Promise.resolve(null)), [from, to, tick, n, valid]);
  const [showVoided, setShowVoided] = useState(false);
  const { data: exp, err: err2 } = useFetch(() => (valid ? req<{ total: number; items: Expense[] }>(`/expenses?from=${from}&to=${to}${showVoided ? "&include_voided=true" : ""}`) : Promise.resolve(null)), [from, to, tick, n, valid, showVoided]);
  const { data: meta } = useFetch(() => req<{ expense_categories: string[] }>("/purchasing/meta"), []);

  const [cat, setCat] = useState("Rent"); const [amount, setAmount] = useState("");
  const [payee, setPayee] = useState(""); const [enote, setEnote] = useState(""); const [edate, setEdate] = useState(() => day(new Date()));
  const [msg, setMsg] = useState("");

  async function addExpense() {
    const a = Number(amount);
    if (!(a > 0)) return setMsg("Enter an amount above 0.");
    try { await req("/expenses", "POST", { category: cat, amount: a, payee, note: enote, date: edate }); setAmount(""); setPayee(""); setEnote(""); setMsg(""); setN((x) => x + 1); } catch (e: any) { setMsg(e.message); }
  }
  async function voidIt(e: Expense) {   // expenses are never deleted: a voided one stays on record, with its reason
    const reason = window.prompt(`Void ${e.exp_no} (${rs(e.amount)})? It stays on record but stops counting in totals.\n\nReason (required):`);
    if (reason === null) return;
    if (!reason.trim()) return setMsg("A reason is required to void an expense.");
    try { await withPin(`Void ${e.exp_no}`, (a) => req(`/expenses/${e.id}/void`, "POST", { reason: reason.trim() }, a)); setMsg(""); setN((x) => x + 1); } catch (er: any) { setMsg(er.message); }
  }
  return (
    <div className="pm">
      <h1>Expenses &amp; Reports</h1>
      <div className="pm-row">
        <label>From<input type="date" value={from} max={to} onChange={(e) => setFrom(e.target.value)} /></label>
        <label>To<input type="date" value={to} min={from} onChange={(e) => setTo(e.target.value)} /></label>
        <button onClick={() => { setFrom(daysAgo(0)); setTo(daysAgo(0)); }}>Today</button>
        <button onClick={() => { setFrom(daysAgo(6)); setTo(daysAgo(0)); }}>7 days</button>
        <button onClick={() => { setFrom(daysAgo(29)); setTo(daysAgo(0)); }}>30 days</button>
      </div>
      {!valid && <Msg text="Pick a valid date range." />}
      <Msg text={err || err2} />

      {rep && (
        <>
          <div className="pm-kpis">
            <Kpi label="Sales (excl. tax)" value={rs(rep.sales_net)} sub={`${rep.sales_count} invoices · ${rep.refunded_count} refunded`} />
            <Kpi label="Purchases received" value={rs(rep.purchases_total)} sub={`${rep.purchases_count} deliveries`} />
            <Kpi label="Expenses" value={rs(rep.expenses_total)} sub={`${rep.expenses_count} entries`} />
            <Kpi label="Cash-flow view" value={rs(rep.cashflow)} sub="sales − purchases − expenses" />
            <Kpi label="Owed to suppliers" value={rs(rep.unpaid_to_suppliers)} sub="all unpaid deliveries" />
            <Kpi label="Open orders" value={rs(rep.open_po_value)} sub={`${rep.open_po_count} POs awaiting delivery`} />
          </div>
          <p className="pm-hint">Cash-flow view is not profit: stock bought but not yet sold counts against it. Profit needs cost-of-goods per sale, which can be added later since product cost is now recorded on every receipt.</p>

          <div className="pm-two">
            <div>
              <h4>Purchases by supplier</h4>
              <table className="pm-table"><thead><tr><th>Supplier</th><th>Deliveries</th><th>Total</th><th>Unpaid</th></tr></thead>
                <tbody>{rep.by_supplier.map((s) => <tr key={s.name}><td>{s.name}</td><td>{s.deliveries}</td><td>{rs(s.total)}</td><td>{s.unpaid ? rs(s.unpaid) : "—"}</td></tr>)}
                  {rep.by_supplier.length === 0 && <tr><td colSpan={4} className="pm-empty">None in this period.</td></tr>}</tbody></table>
            </div>
            <div>
              <h4>Most purchased products</h4>
              <table className="pm-table"><thead><tr><th>Product</th><th>Units</th><th>Spent</th></tr></thead>
                <tbody>{rep.top_products.map((p) => <tr key={p.name}><td>{p.name}</td><td>{p.qty}</td><td>{rs(p.total)}</td></tr>)}
                  {rep.top_products.length === 0 && <tr><td colSpan={3} className="pm-empty">None in this period.</td></tr>}</tbody></table>
            </div>
            <div>
              <h4>Expenses by category</h4>
              <table className="pm-table"><thead><tr><th>Category</th><th>Total</th></tr></thead>
                <tbody>{rep.expenses_by_category.map((e) => <tr key={e.category}><td>{e.category}</td><td>{rs(e.total)}</td></tr>)}
                  {rep.expenses_by_category.length === 0 && <tr><td colSpan={2} className="pm-empty">None in this period.</td></tr>}</tbody></table>
            </div>
          </div>
        </>
      )}

      <h3>Record an expense</h3>
      <div className="pm-row">
        <select value={cat} onChange={(e) => setCat(e.target.value)}>{(meta?.expense_categories ?? ["Rent", "Other"]).map((c) => <option key={c}>{c}</option>)}</select>
        <input type="number" min="0" step="0.01" placeholder="Amount" value={amount} onChange={(e) => setAmount(e.target.value)} />
        <input placeholder="Paid to" value={payee} onChange={(e) => setPayee(e.target.value)} />
        <input type="date" value={edate} max={today} onChange={(e) => setEdate(e.target.value)} />
        <input className="pm-grow" placeholder="Note" value={enote} onChange={(e) => setEnote(e.target.value)} />
        <button className="pm-primary" onClick={addExpense}>Add expense</button>
      </div>
      <Msg text={msg} />
      <label className="pm-check"><input type="checkbox" checked={showVoided} onChange={(e) => setShowVoided(e.target.checked)} /> Show voided expenses</label>
      <table className="pm-table">
        <thead><tr><th>#</th><th>Date</th><th>Category</th><th>Paid to</th><th>Note</th><th>Amount</th><th /></tr></thead>
        <tbody>
          {(exp?.items ?? []).map((e) => (
            <tr key={e.id} className={e.voided ? "pm-dim" : ""} style={e.voided ? { textDecoration: "line-through" } : undefined}><td>{e.exp_no}</td><td>{when(e.ts)}</td><td>{e.category}</td><td>{e.payee || "—"}</td><td>{e.note || "—"}</td><td>{rs(e.amount)}</td>
              <td style={{ textDecoration: "none" }}>{e.voided ? <span title={`${when(e.voided_ts)}`}>VOID: {e.void_reason}</span> : <button onClick={() => voidIt(e)}>Void</button>}</td></tr>
          ))}
          {exp && exp.items.length === 0 && <tr><td colSpan={7} className="pm-empty">No expenses in this period.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}
