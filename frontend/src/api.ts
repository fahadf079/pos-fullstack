// The backend runs on the same computer as the screen (or, for a till on the shop network, the same host name).
export const BASE = `http://${window.location.hostname || "localhost"}:8000`;
/** quantities: whole numbers for pieces ("pc"), up to 3 decimals for weighed items ("kg" / "l") */
export const r3 = (n: number) => Math.round(n * 1000) / 1000;
export const fq = (n: number, unit = "pc") => `${r3(n).toLocaleString(undefined, { maximumFractionDigits: 3 })}${unit === "pc" ? "" : " " + unit}`;
export interface Product { id: number; sku: string; name: string; cat: string; price: number; stock: number; cost: number; unit: string; active: number }
export interface CartItem { id: number; sku: string; name: string; price: number; qty: number; unit?: string; refunded_qty?: number }  // unit missing on invoices from before weighed items existed = pieces
export interface Totals { subtotal: number; discount: number; tax: number; total: number }
export interface Movement { id: number; ts: string; sku: string; name: string; type: string; delta: number; before: number; after: number; note: string; actor: string }
export interface Dash { transactions: number; revenue: number; items_sold: number; top_items: { name: string; qty: number }[]; low_stock: { sku: string; name: string; stock: number; unit?: string }[]; stock_value: number }
export interface SaleSummary { id: number; ts: string; total: number; payment: string; discount_code: string | null; refunded: number; refunded_amount: number; item_count: number; actor: string }
export interface SaleDetail { id: number; ts: string; subtotal: number; discount: number; tax: number; total: number; discount_code: string | null; payment: string; items: CartItem[]; refunded: number; refunded_amount: number; refund_note: string | null; refund_ts: string | null; actor: string; refunds: { id: number; ts: string; note: string; amount: number }[] }

/** What the PIN box collects: your own PIN, or (for a cashier) a manager's username + PIN to approve. */
export interface Auth { pin: string; approver?: string }
/** The server wants a PIN (or a manager's approval) for this action. `invalid` = one was sent but was wrong. */
export class PinError extends Error {
  needsApprover: boolean; invalid: boolean;
  constructor(message: string, needsApprover: boolean, invalid: boolean) { super(message); this.name = "PinError"; this.needsApprover = needsApprover; this.invalid = invalid; }
}
/** The person closed the PIN box. Its message is empty, so error banners simply stay hidden. */
export class Cancelled extends Error { constructor() { super(""); this.name = "Cancelled"; } }
/** Set by AuthGate: what to do when the session ends / a password change is demanded. */
export const hooks = { unauthorized: () => {}, mustChange: () => {} };

export async function req<T>(path: string, body?: unknown, auth?: Auth): Promise<T> {
  const post = body !== undefined;
  const headers: Record<string, string> = {};
  if (post) { headers["Content-Type"] = "application/json"; headers["X-POS"] = "1"; }     // X-POS: a cross-site form can't send this header
  if (auth) { headers["X-POS-PIN"] = auth.pin; if (auth.approver) headers["X-POS-Approver"] = auth.approver; }
  const res = await fetch(BASE + path, { method: post ? "POST" : "GET", headers, credentials: "include", body: post ? JSON.stringify(body) : undefined });
  if (!res.ok) {
    const d = (await res.json().catch(() => ({}))).detail;  // string for most errors, object for PIN / password rules, array for validation errors
    if (res.status === 401 && !path.startsWith("/auth/")) hooks.unauthorized();
    if (d && typeof d === "object" && !Array.isArray(d)) {
      const o = d as { code?: string; message?: string; override?: boolean };
      if (o.code === "pin_required" || o.code === "pin_invalid") throw new PinError(o.message ?? "PIN needed", !!o.override, o.code === "pin_invalid");
      if (o.code === "must_change_password") hooks.mustChange();
      throw new Error(o.message ?? "Not allowed");
    }
    throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map((x: { msg?: string }) => x.msg).join("; ") : `Request failed (${res.status})`);
  }
  return res.json();
}
export const api = {
  inventory: () => req<{ products: Product[]; low_threshold: number }>("/inventory"),
  movements: (n = 50) => req<{ entries: Movement[] }>(`/movements?limit=${n}`),
  dashboard: () => req<Dash>("/dashboard"),
  cart: (code = "") => req<{ items: CartItem[]; totals: Totals }>(`/cart?discount_code=${encodeURIComponent(code)}`),
  scan: (sku: string, qty = 1) => req("/scan", { sku, qty }),
  remove: (product_id: number, qty = 1) => req("/cart/remove", { product_id, qty }),
  cartSet: (product_id: number, qty: number) => req("/cart/set", { product_id, qty }),
  clear: () => req("/cart/clear", {}),
  checkout: (payment_method: string, discount_code?: string, auth?: Auth) => req<{ receipt: { items: CartItem[]; totals: Totals; id: number; payment_method: string; timestamp: string } }>("/checkout", { payment_method, discount_code }, auth),
  sales: (n = 100) => req<{ sales: SaleSummary[] }>(`/sales?limit=${n}`),
  sale: (id: number) => req<SaleDetail>(`/sales/${id}`),
  refund: (id: number, note: string, items?: { id: number; qty: number }[], auth?: Auth) => req<{ ok: boolean; refunded_amount: number; complete: boolean }>(`/sales/${id}/refund`, { note, items }, auth),
  adjust: (sku: string, new_stock: number, auth?: Auth) => req("/inventory/adjust", { sku, new_stock }, auth),
};
