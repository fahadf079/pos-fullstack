// The backend runs on the same computer as the screen (or, for a till on the shop network, the same host name).
export const BASE = `${window.location.protocol === "https:" ? "https" : "http"}://${window.location.hostname || "localhost"}:8000`;   // the screen and the server use the same protocol
/** quantities: whole numbers for pieces ("pc"), up to 3 decimals for weighed items ("kg" / "l") */
export const r3 = (n: number) => Math.round(n * 1000) / 1000;
export const fq = (n: number, unit = "pc") => `${r3(n).toLocaleString(undefined, { maximumFractionDigits: 3 })}${unit === "pc" ? "" : " " + unit}`;
export interface Product { id: number; sku: string; name: string; cat: string; price: number; stock: number; cost: number; unit: string; active: number }
export interface CartItem { id: number; sku: string; name: string; price: number; qty: number; unit?: string; refunded_qty?: number }  // unit missing on invoices from before v7 = pieces
export interface Totals { subtotal: number; discount: number; tax: number; total: number }
export interface Movement { id: number; ts: string; sku: string; name: string; type: string; delta: number; before: number; after: number; note: string; actor: string }
export interface Dash { transactions: number; revenue: number; items_sold: number; top_items: { name: string; qty: number }[]; low_stock: { sku: string; name: string; stock: number; unit?: string }[]; stock_value: number }
export interface Alert { id: number; key: string; severity: "critical" | "warning" | "info"; title: string; detail: string; raised_ts: string; resolved_ts: string | null; ack_ts: string | null; ack_by: string | null; times: number; still_active: boolean; needs_ack: boolean }
export interface Shop { shop_name: string; receipt_footer: string; tax_percent: number; scale_barcodes: boolean }
export interface SaleSummary { id: number; ts: string; total: number; payment: string; discount_code: string | null; refunded: number; refunded_amount: number; item_count: number; actor: string }
export interface SaleDetail { id: number; ts: string; subtotal: number; discount: number; tax: number; total: number; discount_code: string | null; payment: string; items: CartItem[]; refunded: number; refunded_amount: number; refund_note: string | null; refund_ts: string | null; actor: string; refunds: { id: number; ts: string; note: string; amount: number; paid_via?: string | null }[] }

/** What the PIN box collects: the employee's own PIN. (Owners and developers never need one: they signed in with a password.) */
export interface Auth { pin: string }
/** The server wants the employee's own PIN for this action. `invalid` = one was sent but was wrong. (needsApprover is always false since v11.) */
export class PinError extends Error {
  needsApprover: boolean; invalid: boolean;
  constructor(message: string, needsApprover: boolean, invalid: boolean) { super(message); this.name = "PinError"; this.needsApprover = needsApprover; this.invalid = invalid; }
}
/** The person closed the PIN box. Its message is empty, so error banners simply stay hidden. */
export class Cancelled extends Error { constructor() { super(""); this.name = "Cancelled"; } }
/** Set by AuthGate: what to do when the session ends, the screen is locked by the server, or this computer's network is refused. */
export const hooks = { unauthorized: () => {}, locked: () => {}, blocked: (_message: string) => {}, twofa: () => {} };

export async function req<T>(path: string, body?: unknown, auth?: Auth): Promise<T> {
  const post = body !== undefined;
  const headers: Record<string, string> = {};
  if (post) { headers["Content-Type"] = "application/json"; headers["X-POS"] = "1"; }     // X-POS: a cross-site form can't send this header
  if (auth) headers["X-POS-PIN"] = auth.pin;
  const res = await fetch(BASE + path, { method: post ? "POST" : "GET", headers, credentials: "include", body: post ? JSON.stringify(body) : undefined });
  if (!res.ok) {
    const d = (await res.json().catch(() => ({}))).detail;  // string for most errors, object for PIN / password rules, array for validation errors
    if (res.status === 401 && !path.startsWith("/auth/")) hooks.unauthorized();
    if (d && typeof d === "object" && !Array.isArray(d)) {
      const o = d as { code?: string; message?: string; override?: boolean };
      if (o.code === "pin_required" || o.code === "pin_invalid") throw new PinError(o.message ?? "PIN needed", !!o.override, o.code === "pin_invalid");
      if (o.code === "locked") hooks.locked();
      if (o.code === "2fa_required") hooks.twofa();
      if (o.code === "code_required") { const e = new Error(o.message ?? "Enter your 2FA code"); e.name = "CodeRequired"; throw e; }
      if (o.code === "network_blocked") hooks.blocked(o.message ?? "This computer is not allowed to connect.");
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
  remove: (product_id: number, qty = 1, auth?: Auth) => req("/cart/remove", { product_id, qty }, auth),
  cartSet: (product_id: number, qty: number, auth?: Auth) => req("/cart/set", { product_id, qty }, auth),
  shop: () => req<Shop>("/shop"),
  clear: (auth?: Auth) => req("/cart/clear", {}, auth),
  checkout: (payment_method: string, discount_code?: string, auth?: Auth) => req<{ receipt: { items: CartItem[]; totals: Totals; id: number; payment_method: string; timestamp: string } }>("/checkout", { payment_method, discount_code }, auth),
  sales: (n = 100) => req<{ sales: SaleSummary[] }>(`/sales?limit=${n}`),
  sale: (id: number) => req<SaleDetail>(`/sales/${id}`),
  refund: (id: number, note: string, items?: { id: number; qty: number }[], auth?: Auth, paid_via?: string) => req<{ ok: boolean; refunded_amount: number; complete: boolean }>(`/sales/${id}/refund`, { note, items, paid_via }, auth),
  adjust: (sku: string, new_stock: number, auth?: Auth) => req("/inventory/adjust", { sku, new_stock }, auth),
};
