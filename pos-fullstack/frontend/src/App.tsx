import { Fragment, useEffect, useRef, useState } from "react";
import { api, BASE, fq, r3, type CartItem, type Dash, type Movement, type Product, type SaleDetail } from "./api";
import { AuthGate, RANK, useAuth } from "./auth";
import type { Role } from "./auth";
import { Catalog } from "./CatalogPage";
import { History } from "./HistoryPage";
import { Purchasing, Reports } from "./PurchasingPages";
import { Account, Security } from "./SecurityPage";
import "./App.css";
import "./purchasing.css";

// [page, lowest role that sees it]. The server enforces the same walls: hiding a tab is only convenience.
const NAV = [["Dashboard", "cashier"], ["Inventory", "cashier"], ["Catalog", "manager"], ["Purchasing", "manager"], ["Invoices", "cashier"], ["Reports", "manager"],
  ["History", "manager"], ["Settings", "owner"], ["Security", "owner"], ["Account", "cashier"]] as const satisfies readonly (readonly [string, Role])[];
type Page = (typeof NAV)[number][0];
const rs = (n: number) => `Rs ${(Math.round(n * 100) / 100).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;

// One live connection: backend pushes a tick on ANY stock/sale change, every screen refetches.
function useLive() {
  const [tick, setTick] = useState(0), [live, setLive] = useState(false);
  useEffect(() => {
    const es = new EventSource(BASE + "/events", { withCredentials: true });
    es.onmessage = () => setTick((t) => t + 1);
    es.onopen = () => setLive(true);
    es.onerror = () => setLive(false);
    return () => es.close();
  }, []);
  return { tick, live };
}
function useData<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [d, setD] = useState<T | null>(null);
  useEffect(() => { let on = true; fn().then((x) => on && setD(x)).catch(() => {}); return () => { on = false; }; }, deps); // eslint-disable-line  (ignores stale responses)
  return d;
}

function Dashboard({ tick }: { tick: number }) {
  const { withPin } = useAuth();
  const [sku, setSku] = useState(""), [code, setCode] = useState(""), [pay, setPay] = useState("");
  const [err, setErr] = useState(""), [receipt, setReceipt] = useState<string>("");
  const [weigh, setWeigh] = useState<Product | null>(null), [wt, setWt] = useState("");   // weighed item waiting for its weight
  const scanRef = useRef<HTMLInputElement>(null), wtRef = useRef<HTMLInputElement>(null);
  const dash = useData<Dash>(api.dashboard, [tick]);
  const cart = useData(() => api.cart(code), [tick, code]);
  const inv = useData(api.inventory, [tick]);
  const sellable = (inv?.products ?? []).filter((p) => p.active);   // deactivated products can't be sold
  const matches = sku.trim()
    ? sellable.filter((p) => (p.name + p.sku).toLowerCase().includes(sku.trim().toLowerCase())).slice(0, 6)
    : [];
  // pieces are scanned straight into the cart; kg / litre items first ask for the weight
  const choose = async (p: Product) => {
    if (p.unit !== "pc") { setWeigh(p); setWt(""); setSku(""); setTimeout(() => wtRef.current?.focus(), 0); return; }
    await api.scan(p.sku); setSku("");
  };
  const addFromBox = () => {
    const v = sku.trim(); if (!v) return;
    const target = sellable.find((p) => p.sku === v) ?? (matches.length === 1 ? matches[0] : undefined);
    run(async () => {
      if (!inv) throw new Error("Products are still loading — scan again in a moment.");
      if (!target) throw new Error(matches.length ? "Several products match — click one below." : (inv?.products ?? []).some((p) => p.sku === v && !p.active) ? "That product is deactivated." : `No product matches "${v}"`);
      await choose(target);
    });
  };
  const addWeighed = async () => {
    if (!weigh) return;
    setErr("");
    try {
      const n = Number(wt);
      if (!wt.trim() || !Number.isFinite(n) || n <= 0 || Math.abs(n * 1000 - Math.round(n * 1000)) > 1e-6) throw new Error("Enter a weight above 0 (up to 3 decimals).");
      await api.scan(weigh.sku, n); setWeigh(null); setWt(""); scanRef.current?.focus();
    } catch (e) { setErr((e as Error).message); wtRef.current?.focus(); }
  };
  const run = async (fn: () => Promise<unknown>) => { setErr(""); try { await fn(); } catch (e) { setErr((e as Error).message); } scanRef.current?.focus(); };
  const t = cart?.totals;

  return (<>
    <h1>Dashboard</h1>
    <div className="kpis">
      <div><b>{dash ? rs(dash.revenue) : "…"}</b>Today's revenue</div>
      <div><b>{dash?.transactions ?? "…"}</b>Transactions</div>
      <div><b>{dash?.items_sold ?? "…"}</b>Items sold</div>
      <div className={dash?.low_stock.length ? "warn" : ""}><b>{dash?.low_stock.length ?? "…"}</b>Low-stock items</div>
    </div>
    {err && <div className="err">{err}</div>}
    <div className="cols">
      <section>
        <h2>Scan</h2>
        <input ref={scanRef} autoFocus value={sku} placeholder="Scan barcode, or type a product name, press Enter" onChange={(e) => setSku(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && addFromBox()} />
        <p className="hint">Stock is deducted the instant you scan or add. Use − to return an item.</p>
        {matches.map((p) => (
          <div key={p.sku} className="row">
            <span>{p.name} ({fq(p.stock, p.unit)} left){p.unit !== "pc" ? ` · ${rs(p.price)}/${p.unit}` : ""}</span>
            <button disabled={p.stock <= 0} onClick={() => run(() => choose(p))}>{p.unit !== "pc" ? "Weigh…" : "Add"}</button>
          </div>
        ))}
        {weigh && (
          <div className="row weigh">
            <span>Weight of <b>{weigh.name}</b> ({rs(weigh.price)}/{weigh.unit}, {fq(weigh.stock, weigh.unit)} in stock)</span>
            <span>
              <input ref={wtRef} type="number" min="0.001" step="0.001" style={{ width: 90 }} value={wt} placeholder={weigh.unit}
                onChange={(e) => setWt(e.target.value)} onKeyDown={(e) => e.key === "Enter" && addWeighed()} />
              <button className="primary" onClick={addWeighed}>Add</button>
              <button onClick={() => { setWeigh(null); setWt(""); scanRef.current?.focus(); }}>Cancel</button>
            </span>
          </div>)}
        <h2>Low stock</h2>
        {dash?.low_stock.length ? dash.low_stock.map((p) => <div key={p.sku} className="row"><span>{p.name}</span><b className="bad">{fq(p.stock, p.unit)} left</b></div>) : <p className="hint">Nothing running low.</p>}
        <h2>Top items today</h2>
        {dash?.top_items.length ? dash.top_items.map((i) => <div key={i.name} className="row"><span>{i.name}</span><b>{i.qty}</b></div>) : <p className="hint">No sales yet.</p>}
      </section>
      <section>
        <h2>Cart</h2>
        {!cart?.items.length && <p className="hint">Cart is empty — scan an item.</p>}
        {cart?.items.map((i) => {
          const byWeight = (i.unit ?? "pc") !== "pc";
          return (
            <div key={i.id} className="row"><span>{i.name}{byWeight && <small> @ {rs(i.price)}/{i.unit}</small>}</span>
              <span>
                {byWeight ? (<>
                  <b>{fq(i.qty, i.unit)}</b>{" "}
                  <button onClick={() => {
                    const v = prompt(`New weight for ${i.name} (${i.unit})?`, String(i.qty)); if (v === null) return;
                    const n = Number(v);
                    run(async () => { if (!v.trim() || !Number.isFinite(n) || n < 0) throw new Error("Enter a valid weight."); await api.cartSet(i.id, n); });
                  }}>Weight</button>
                  <button onClick={() => run(() => api.remove(i.id, i.qty))}>Remove</button>
                </>) : (<>
                  <button onClick={() => run(() => api.remove(i.id))}>−</button>
                  <b> {i.qty} </b>
                  <button onClick={() => run(() => api.scan(i.sku))}>+</button>
                </>)}
                {" "}{rs(i.price * i.qty)}
              </span></div>);
        })}
        <input value={code} placeholder="Discount code (SAVE10 / SAVE20)" onChange={(e) => setCode(e.target.value)} />
        {t && <>
          <div className="row"><span>Subtotal</span><span>{rs(t.subtotal)}</span></div>
          <div className="row"><span>Discount</span><span>-{rs(t.discount)}</span></div>
          <div className="row"><span>Tax (8%)</span><span>{rs(t.tax)}</span></div>
          <div className="row total"><span>Total</span><span>{rs(t.total)}</span></div></>}
        <div className="pay">{["Cash", "Card", "Wallet"].map((m) => <button key={m} className={pay === m ? "on" : ""} onClick={() => setPay(m)}>{m}</button>)}</div>
        <button className="primary" disabled={!cart?.items.length} onClick={() => run(async () => {
          if (!pay) throw new Error("Select a payment method first.");
          const { receipt: r } = await withPin(`Discount ${code.trim().toUpperCase()} needs approval`, (a) => api.checkout(pay, code || undefined, a));
          setReceipt(`${r.items.map((i) => `${i.name} ×${fq(i.qty, i.unit)}  ${rs(i.price * i.qty)}`).join("\n")}\n—\nTOTAL ${rs(r.totals.total)} (${r.payment_method})\n${new Date(r.timestamp).toLocaleString()}`);
          setPay(""); setCode("");
        })}>Checkout</button>
        <button onClick={() => run(async () => { await api.clear(); setReceipt(""); })}>Clear cart (returns stock)</button>
        {receipt && <pre className="receipt">{receipt}</pre>}
      </section>
    </div>
  </>);
}

function Inventory({ tick }: { tick: number }) {
  const { withPin } = useAuth();
  const inv = useData(api.inventory, [tick]);
  const mv = useData(() => api.movements(30), [tick]);
  const [q, setQ] = useState(""), [err, setErr] = useState("");
  const low = inv?.low_threshold ?? 5;
  const list = (inv?.products ?? []).filter((p: Product) => (p.name + p.sku + p.cat).toLowerCase().includes(q.toLowerCase()));
  const run = async (fn: () => Promise<unknown>) => { setErr(""); try { await fn(); } catch (e) { setErr((e as Error).message); } };
  return (<>
    <h1>Inventory <small>(live)</small></h1>
    {err && <div className="err">{err}</div>}
    <p className="hint">To add stock, create a purchase order and receive it under <b>Purchasing</b>. Use Adjust below only to correct a physical count.</p>
    <section>
      <input value={q} placeholder="Search name / SKU / category" onChange={(e) => setQ(e.target.value)} />
      <table><thead><tr><th>SKU</th><th>Name</th><th>Category</th><th>Price</th><th>Stock</th><th></th></tr></thead><tbody>
        {list.map((p) => (
          <tr key={p.id} className={!p.active ? "inactive" : p.stock === 0 ? "out" : p.stock <= low ? "low" : ""}>
            <td>{p.sku}</td><td>{p.name}{!p.active && <b className="bad"> INACTIVE</b>}</td><td>{p.cat}</td><td>{rs(p.price)}{p.unit !== "pc" ? `/${p.unit}` : ""}</td>
            <td><b>{fq(p.stock, p.unit)}</b>{p.stock === 0 ? " OUT" : p.stock <= low ? " LOW" : ""}</td>
            <td><button onClick={() => {
              const v = prompt(`Counted stock for ${p.name}${p.unit !== "pc" ? ` (${p.unit})` : ""}?`, String(p.stock)); if (v === null || v.trim() === "") return;
              const n = Number(v); if (!Number.isFinite(n) || n < 0) { setErr("Enter the counted stock as a number (0 or more)."); return; }
              run(() => withPin(`Stock count: ${p.name} → ${fq(n, p.unit)}`, (a) => api.adjust(p.sku, n, a)));
            }}>Adjust</button></td>
          </tr>))}
      </tbody></table>
    </section>
    <section>
      <h2>Stock movements (every change, live)</h2>
      <table><thead><tr><th>Time</th><th>Type</th><th>Product</th><th>Δ</th><th>Before → After</th><th>By</th></tr></thead><tbody>
        {(mv?.entries ?? []).map((m: Movement) => (
          <tr key={m.id}><td>{m.ts.slice(11)}</td><td>{m.type}</td><td>{m.name}</td>
            <td className={m.delta < 0 ? "bad" : "good"}>{m.delta > 0 ? "+" : ""}{r3(m.delta)}</td><td>{r3(m.before)} → {r3(m.after)}</td><td className="hint">{m.actor === "system" ? "—" : m.actor}</td></tr>))}
      </tbody></table>
    </section>
  </>);
}


/** Pick which items (and how many) to take back from one invoice. Money preview uses the same share-of-total rule as the server. */
function RefundPanel({ d, onRefund }: { d: SaleDetail; onRefund: (d: SaleDetail, picks: { id: number; qty: number }[] | undefined, amount: number) => void }) {
  const [ret, setRet] = useState<Record<number, string>>({});
  const [msg, setMsg] = useState("");
  const left = (i: CartItem) => r3(i.qty - (i.refunded_qty ?? 0));
  const share = d.subtotal > 0 ? d.total / d.subtotal : 0;
  const picks: { id: number; qty: number }[] = [];
  let bad = "";
  for (const i of d.items) {
    const raw = (ret[i.id] ?? "").trim();
    if (!raw) continue;
    const q = Number(raw);
    if (!Number.isFinite(q) || q <= 0) { bad = `"${i.name}": enter a quantity above 0`; break; }
    if ((i.unit ?? "pc") === "pc" && !Number.isInteger(q)) { bad = `"${i.name}" is sold by the piece: enter a whole number`; break; }
    if (q > left(i) + 1e-9) { bad = `"${i.name}": only ${fq(left(i), i.unit)} can still be returned`; break; }
    picks.push({ id: i.id, qty: r3(q) });
  }
  const amt = (list: { id: number; qty: number }[]) => Math.round(list.reduce((t, p) => t + Math.round((d.items.find((i) => i.id === p.id)?.price ?? 0) * p.qty * 100) / 100, 0) * share * 100) / 100;
  return (<div style={{ marginTop: 8 }}>
    <table><thead><tr><th>Item</th><th>Sold</th><th>Already returned</th><th>Return now</th></tr></thead><tbody>
      {d.items.map((i) => (<tr key={i.id}>
        <td>{i.name}</td><td>{fq(i.qty, i.unit)}</td><td>{fq(i.refunded_qty ?? 0, i.unit)}</td>
        <td>{left(i) > 0
          ? <span><input type="number" min="0" step={(i.unit ?? "pc") === "pc" ? 1 : 0.001} max={left(i)} placeholder={`0 – ${fq(left(i), i.unit)}`} value={ret[i.id] ?? ""} style={{ width: 130 }}
              onChange={(e) => { setMsg(""); setRet({ ...ret, [i.id]: e.target.value }); }} />{" "}
              <button type="button" onClick={() => setRet({ ...ret, [i.id]: String(left(i)) })}>All</button></span>
          : <span className="hint">fully returned</span>}</td>
      </tr>))}
    </tbody></table>
    {(bad || msg) && <div className="err">{bad || msg}</div>}
    <p className="hint">{picks.length && !bad ? `Refund for the selected items: about ${rs(amt(picks))} (invoice discount and tax are shared out proportionally).` : "Enter how many of each item the customer is bringing back."}</p>
    <button disabled={!picks.length || !!bad} onClick={() => onRefund(d, picks, amt(picks))}>Refund selected items</button>{" "}
    <button onClick={() => onRefund(d, undefined, Math.round((d.total - d.refunded_amount) * 100) / 100)}>Refund everything left ({rs(d.total - d.refunded_amount)})</button>
  </div>);
}

function Invoices({ tick }: { tick: number }) {
  const { withPin } = useAuth();
  const data = useData(() => api.sales(100), [tick]);
  const [openId, setOpenId] = useState<number | null>(null);
  const [detail, setDetail] = useState<SaleDetail | null>(null);
  const [err, setErr] = useState("");

  const open = (id: number) => {
    setErr("");
    if (openId === id) { setOpenId(null); setDetail(null); return; }
    setOpenId(id);
    api.sale(id).then(setDetail).catch((e) => setErr((e as Error).message));
  };
  const doRefund = (d: SaleDetail, picks: { id: number; qty: number }[] | undefined, amount: number) => {
    const what = picks ? `the ${picks.length} selected item${picks.length > 1 ? "s" : ""}` : "everything still refundable";
    if (!window.confirm(`Refund ${what} on invoice #${d.id} (about ${rs(amount)} back to the customer)? The items go back into stock. This can't be undone.`)) return;
    const note = prompt("Reason for refund?", "customer return");
    if (note === null) return;
    if (!note.trim()) { setErr("A refund needs a reason."); return; }
    setErr("");
    withPin(`Refund on invoice #${d.id}`, (a) => api.refund(d.id, note.trim(), picks, a)).then(() => api.sale(d.id).then(setDetail)).catch((e) => setErr((e as Error).message));
  };

  return (<>
    <h1>Invoices</h1>
    {err && <div className="err">{err}</div>}
    <section>
      <table><thead><tr><th>#</th><th>Time</th><th>Items</th><th>Payment</th><th>Code</th><th>Total</th><th>Served by</th><th></th></tr></thead><tbody>
        {(data?.sales ?? []).map((s) => (<Fragment key={s.id}>
          <tr className={s.refunded ? "out" : ""} style={{ cursor: "pointer" }} onClick={() => open(s.id)}>
            <td>#{s.id}</td><td>{new Date(s.ts).toLocaleString()}</td><td>{s.item_count}</td><td>{s.payment}</td>
            <td>{s.discount_code || "—"}</td><td>{rs(s.total)}</td><td className="hint">{s.actor === "system" ? "—" : s.actor}</td>
            <td>{s.refunded ? <b className="bad">REFUNDED</b> : s.refunded_amount > 0 ? <b className="bad">PART-REFUNDED {rs(s.refunded_amount)}</b> : ""}</td>
          </tr>
          {openId === s.id && detail && (
            <tr><td colSpan={8}>
              <div className="receipt">
                {detail.items.map((i) => `${i.name} ×${fq(i.qty, i.unit)}  ${rs(i.price * i.qty)}${i.refunded_qty ? `   (returned ×${fq(i.refunded_qty, i.unit)})` : ""}`).join("\n")}
                {"\n—\n"}Subtotal {rs(detail.subtotal)}{"\n"}Discount -{rs(detail.discount)}{detail.discount_code ? ` (${detail.discount_code})` : ""}{"\n"}
                Tax {rs(detail.tax)}{"\nTOTAL "}{rs(detail.total)}{" ("}{detail.payment}{")\n"}
                {new Date(detail.ts).toLocaleString()}{detail.actor !== "system" ? `  ·  served by ${detail.actor}` : ""}
                {detail.refunds.length ? "\n\n" + detail.refunds.map((r) => `REFUND ${new Date(r.ts).toLocaleString()} — ${rs(r.amount)} — ${r.note}`).join("\n") : ""}
                {detail.refunded && !detail.refunds.length ? `\n\nREFUNDED ${new Date(detail.refund_ts!).toLocaleString()} — ${detail.refund_note}` : ""}
              </div>
              {!detail.refunded && <RefundPanel key={`${detail.id}-${detail.refunds.length}`} d={detail} onRefund={doRefund} />}
            </td></tr>
          )}
        </Fragment>))}
      </tbody></table>
      {!data?.sales.length && <p className="hint">No sales yet.</p>}
    </section>
  </>);
}

const PLANNED: Record<string, string> = {
  Settings: "Shop details, tax rate, receipt footer, low-stock threshold, discount codes, printer.",
};

function Shell() {
  const { me, logout } = useAuth();
  const [page, setPage] = useState<Page>("Dashboard");
  const { tick, live } = useLive();
  const nav = NAV.filter(([, role]) => RANK[me.role] >= RANK[role]).map(([name]) => name);
  return (
    <div className="shell">
      <nav>
        <div className="brand">POS</div>
        {nav.map((n) => <button key={n} className={n === page ? "on" : ""} onClick={() => setPage(n)}>{n}</button>)}
        <div className="who">{me.full_name || me.username}<br />{me.role}</div>
        <button onClick={() => void logout()}>Log out</button>
        <div className={`live ${live ? "ok" : ""}`}>{live ? "● live" : "○ offline"}</div>
      </nav>
      <main>
        {page === "Dashboard" && <Dashboard tick={tick} />}
        {page === "Inventory" && <Inventory tick={tick} />}
        {page === "Catalog" && <Catalog tick={tick} />}
        {page === "Purchasing" && <Purchasing tick={tick} />}
        {page === "Invoices" && <Invoices tick={tick} />}
        {page === "Reports" && <Reports tick={tick} />}
        {page === "History" && <History tick={tick} />}
        {page === "Security" && <Security tick={tick} />}
        {page === "Account" && <Account />}
        {PLANNED[page] && <><h1>{page}</h1><p className="hint">Not built yet — planned scope: {PLANNED[page]}</p></>}
      </main>
    </div>
  );
}

export default function App() {
  return <AuthGate><Shell /></AuthGate>;
}
