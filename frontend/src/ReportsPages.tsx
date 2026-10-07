import { useEffect, useState } from "react";
import { req } from "./api";

const rs = (n: number | null | undefined) => (n === null || n === undefined ? "—" : "Rs " + (Math.round(n * 100) / 100).toLocaleString(undefined, { maximumFractionDigits: 2 }));
const iso = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

function useGet<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [d, setD] = useState<T | null>(null), [err, setErr] = useState("");
  useEffect(() => { let on = true; fn().then((x) => { if (on) { setD(x); setErr(""); } }).catch((e: Error) => { if (on) setErr(e.message); }); return () => { on = false; }; }, deps); // eslint-disable-line
  return { d, err };
}

interface Day { date: string; methods: { method: string; sales: number; sales_total: number; refunds: number; refunds_total: number; net: number }[]; sales_total: number; refunds_total: number; net_total: number;
  shifts: { id: number; actor: string; opening_float: number; counted_cash: number; expected_cash: number; variance: number }[]; shifts_open_now: number; counted_total: number; expected_total: number; variance_total: number;
  moves: Record<string, { count: number; total: number }>; manual_drawer_opens: number }

/** Owner: one page for the end of the day: takings by payment method, refunds by how they were paid back, every shift, cash moved. */
export function DayReport({ tick }: { tick: number }) {
  const [date, setDate] = useState(iso(new Date()));
  const r = useGet(() => req<Day>(`/reports/day?date=${date}`), [date, tick]);
  const d = r.d;
  return (
    <div className="sx">
      <h1>End of day</h1>
      <p><input type="date" value={date} max={iso(new Date())} onChange={(e) => e.target.value && setDate(e.target.value)} /> <button onClick={() => window.print()}>Print</button></p>
      {r.err && <div className="err">{r.err}</div>}
      {d && <>
        {d.shifts_open_now > 0 && <div className="err">{d.shifts_open_now} shift(s) are still open: their cash is not counted yet.</div>}
        <section>
          <h2>Takings by payment method</h2>
          <table><thead><tr><th>Method</th><th>Sales</th><th>Sold</th><th>Refunds paid back</th><th>Refunded</th><th>Net</th></tr></thead>
            <tbody>{d.methods.map((m) => <tr key={m.method}><td>{m.method}</td><td>{m.sales}</td><td>{rs(m.sales_total)}</td><td>{m.refunds}</td><td>{rs(m.refunds_total)}</td><td><b>{rs(m.net)}</b></td></tr>)}
              <tr><td><b>All</b></td><td></td><td><b>{rs(d.sales_total)}</b></td><td></td><td><b>{rs(d.refunds_total)}</b></td><td><b>{rs(d.net_total)}</b></td></tr></tbody></table>
        </section>
        <section>
          <h2>Cash drawers (shifts closed this day)</h2>
          <table><thead><tr><th>#</th><th>Employee</th><th>Float</th><th>Counted</th><th>Expected</th><th>Difference</th></tr></thead>
            <tbody>{d.shifts.map((s) => <tr key={s.id}><td>{s.id}</td><td>{s.actor}</td><td>{rs(s.opening_float)}</td><td>{rs(s.counted_cash)}</td><td>{rs(s.expected_cash)}</td><td><b className={s.variance < 0 ? "bad" : ""}>{rs(s.variance)}</b></td></tr>)}
              <tr><td></td><td><b>Total</b></td><td></td><td><b>{rs(d.counted_total)}</b></td><td><b>{rs(d.expected_total)}</b></td><td><b>{rs(d.variance_total)}</b></td></tr></tbody></table>
        </section>
      </>}
    </div>
  );
}

interface Profit { from: string; to: string; sales_count: number; revenue_net: number; cost_of_goods: number; gross_profit: number; margin_percent: number | null; expenses: number; profit_after_expenses: number;
  estimated_cost_lines: number; lines_without_cost: number; stock_value_at_cost: number; stock_value_at_price: number; best_items: { name: string; qty: number; revenue: number; profit: number }[] }

/** Owner: profit using what the shop paid for the goods. */
export function ProfitReport({ tick }: { tick: number }) {
  const today = new Date(), start = new Date(); start.setDate(today.getDate() - 29);
  const [from, setFrom] = useState(iso(start)), [to, setTo] = useState(iso(today));
  const r = useGet(() => req<Profit>(`/reports/profit?date_from=${from}&date_to=${to}`), [from, to, tick]);
  const d = r.d;
  return (
    <div className="sx">
      <h1>Profit</h1>
      <p><input type="date" value={from} onChange={(e) => e.target.value && setFrom(e.target.value)} /> to <input type="date" value={to} onChange={(e) => e.target.value && setTo(e.target.value)} /></p>
      {r.err && <div className="err">{r.err}</div>}
      {d && <>
        <section>
          <table><tbody>
            <tr><td>Sales (no tax, after refunds)</td><td><b>{rs(d.revenue_net)}</b> <span className="hint">{d.sales_count} sales</span></td></tr>
            <tr><td>Cost of the goods sold</td><td>{rs(d.cost_of_goods)}</td></tr>
            <tr><td><b>Gross profit</b></td><td><b className={d.gross_profit < 0 ? "bad" : "good"}>{rs(d.gross_profit)}</b> {d.margin_percent !== null && <span className="hint">{d.margin_percent}% margin</span>}</td></tr>
            <tr><td>Expenses (not voided)</td><td>{rs(d.expenses)}</td></tr>
            <tr><td><b>Profit after expenses</b></td><td><b className={d.profit_after_expenses < 0 ? "bad" : "good"}>{rs(d.profit_after_expenses)}</b></td></tr>
            <tr><td>Stock on the shelves</td><td>{rs(d.stock_value_at_cost)} at cost · {rs(d.stock_value_at_price)} at selling price</td></tr>
          </tbody></table>
          {d.lines_without_cost > 0 && <div className="err">{d.lines_without_cost} sold line(s) have no cost set, so their profit is overstated. Set the cost in Catalog (or receive stock through a purchase order).</div>}
        </section>
        <section>
          <h2>Best items by profit</h2>
          <table><thead><tr><th>Item</th><th>Qty</th><th>Sold for</th><th>Profit</th></tr></thead>
            <tbody>{d.best_items.map((b) => <tr key={b.name}><td>{b.name}</td><td>{b.qty}</td><td>{rs(b.revenue)}</td><td>{rs(b.profit)}</td></tr>)}</tbody></table>
        </section>
      </>}
    </div>
  );
}
