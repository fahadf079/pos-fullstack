import { JSDOM } from "jsdom";
import fs from "fs";
const DIST = new URL("../frontend/dist", import.meta.url).pathname;
const js = fs.readFileSync(`${DIST}/assets/` + fs.readdirSync(`${DIST}/assets`).find((f) => f.endsWith(".js")), "utf8");

// cookie-aware fetch (Node's fetch has no cookie jar) + a streaming EventSource, like a real browser
const jar = new Map(); const real = globalThis.fetch;
async function jfetch(url, o = {}) {
  const h = { ...(o.headers || {}) }; if (jar.size) h.Cookie = [...jar].map(([k, v]) => `${k}=${v}`).join("; ");
  const r = await real(url, { ...o, headers: h });
  for (const sc of r.headers.getSetCookie()) { const [kv] = sc.split(";"); const i = kv.indexOf("="), k = kv.slice(0, i), v = kv.slice(i + 1);
    if (/max-age=0/i.test(sc) || v === "" || v === '""') jar.delete(k); else jar.set(k, v); }
  return r;
}
let esCount = 0;
class ES { constructor(u) { esCount++; this.ctl = new AbortController(); (async () => { try { const r = await jfetch(u, { signal: this.ctl.signal }); if (!r.ok) throw 0; this.onopen?.();
  const rd = r.body.getReader(), dec = new TextDecoder(); let b = ""; for (;;) { const { done, value } = await rd.read(); if (done) break; b += dec.decode(value); let i; while ((i = b.indexOf("\n\n")) >= 0) { const ev = b.slice(0, i); b = b.slice(i + 2); if (ev.includes("data:")) this.onmessage?.({}); } } } catch { /* closed */ } this.onerror?.(); })(); } close() { this.ctl.abort(); } }

const dom = new JSDOM(`<!doctype html><div id="root"></div>`, { url: "http://localhost:5173/", runScripts: "outside-only", pretendToBeVisual: true });
const w = dom.window; const doc = w.document;
w.fetch = jfetch; w.EventSource = ES; w.confirm = () => true; w.prompt = (_m, d) => d ?? "x"; globalThis.EventSource = ES;
w.eval(js);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const text = () => doc.body.textContent.replace(/\s+/g, " ");
async function until(fn, label, ms = 6000) { const t0 = Date.now(); for (;;) { let v; try { v = fn(); } catch { v = false; } if (v) return v; if (Date.now() - t0 > ms) throw new Error("TIMEOUT waiting for: " + label + "\n--- screen: " + text().slice(0, 600)); await sleep(30); } }
const all = (sel) => [...doc.querySelectorAll(sel)];
const btn = (t) => all("button").find((b) => b.textContent.trim() === t || b.textContent.trim().startsWith(t));
const input = (ph) => all("input").find((i) => (i.placeholder || "").startsWith(ph));
const click = (el) => el.dispatchEvent(new w.MouseEvent("click", { bubbles: true, cancelable: true }));
const setter = Object.getOwnPropertyDescriptor(w.HTMLInputElement.prototype, "value").set;
const type = (ph, v) => { const el = typeof ph === "string" ? input(ph) : ph; if (!el) throw new Error("no input " + ph); setter.call(el, v); el.dispatchEvent(new w.Event("input", { bubbles: true })); };
const select = (el, v) => { Object.getOwnPropertyDescriptor(w.HTMLSelectElement.prototype, "value").set.call(el, v); el.dispatchEvent(new w.Event("change", { bubbles: true })); };
let n = 0; const check = (name, cond) => { if (!cond) throw new Error("CHECK FAILED: " + name + "\n--- screen: " + text().slice(0, 600)); n++; console.log("  ok", name); };
const nav = () => all("nav button").map((b) => b.textContent.trim());
const dialog = () => doc.querySelector(".sx-dialog");

// 1 ── first-time setup
await until(() => text().includes("First-time setup"), "setup screen"); check("fresh install shows first-time setup (no data visible)", !nav().length);
type("Username", "boss"); type("Full name", "The Boss"); type("Password (8+", "Boss-pass-1"); type("Password again", "Boss-pass-1"); type("PIN (4", "4829"); type("PIN again", "4829");
click(btn("Create owner account")); await until(() => nav().includes("Security"), "owner app");
check("owner sees every tab", ["Dashboard", "Inventory", "Catalog", "Purchasing", "Invoices", "Reports", "History", "Settings", "Security", "Account"].every((t) => nav().includes(t)));
await until(() => text().includes("The Boss"), "name in sidebar"); await until(() => text().includes("● live"), "live updates connected (SSE with the login cookie)");

// 2 ── owner adds a cashier; the owner's PIN is demanded; a wrong PIN is refused and counted
click(btn("Security")); await until(() => text().includes("Who can do what"), "security page");
type("Username (letters", "sara"); type("Full name", "Sara K"); type("Starting password", "Sara-temp-12"); type("PIN (4–8", "2864"); click(btn("Add"));
await until(dialog, "PIN box for adding a person"); check("adding a person asks the owner for a PIN", dialog().textContent.includes("Add a person") && dialog().textContent.includes("Enter your PIN"));
type(dialog().querySelector("input"), "1357"); click(btn("Confirm")); await until(() => dialog()?.textContent.includes("Wrong PIN (4 tries left)"), "wrong-PIN message");
check("wrong PIN is refused with the tries left", true);
type(dialog().querySelector("input"), "4829"); click(btn("Confirm")); await until(() => text().includes("Added sara"), "added note");
await until(() => all("tbody tr").some((r) => r.textContent.includes("sara") && r.textContent.includes("must set password")), "sara in list"); check("new person listed, flagged 'must set password'", true);
check("permission table is shown", text().includes("Cashier — sells") && text().includes("Manager — also"));

// 2b ── owner can also set a PERMANENT password (box unticked): that person logs straight in, no forced change
const cb = doc.querySelector('.sx-add input[type=checkbox]'); check("'must choose own password' box is ticked by default", cb.checked);
type("Username (letters", "noor"); type("Full name", "Noor A"); check("label reads 'Starting password' while ticked", !!input("Starting password"));
type("Starting password", "Noor-perm-pass-1"); type("PIN (4–8", "5731"); click(cb); await sleep(80); check("label becomes 'Password' when unticked", !!input("Password (8+"));
click(btn("Add")); await until(dialog, "PIN box"); type(dialog().querySelector("input"), "4829"); click(btn("Confirm")); await until(() => text().includes("Added noor. The password you typed is their password as it is"), "permanent note");
click(btn("Log out")); await until(() => doc.querySelector(".sx-card") && text().includes("Log in"), "login"); type("Username", "noor"); type("Password", "Noor-perm-pass-1"); click(btn("Log in")); await until(() => nav().includes("Dashboard"), "noor straight in");
check("permanent password: logs straight in, no forced change", !text().includes("Choose your own password"));
click(btn("Log out")); await until(() => doc.querySelector(".sx-card") && text().includes("Log in"), "login screen");
check("owner re-login for next steps", true); type("Username", "boss"); type("Password", "Boss-pass-1"); click(btn("Log in")); await until(() => nav().includes("Security"), "boss back 2");
// 3 ── cashier: forced password change, then a limited app
click(btn("Log out")); await until(() => text().includes("Log in") && input("Username"), "login screen");
type("Username", "sara"); type("Password", "wrong-wrong-1"); click(btn("Log in")); await until(() => text().includes("Wrong username or password."), "bad login message"); check("wrong password shows a generic message", true);
type("Password", "Sara-temp-12"); click(btn("Log in")); await until(() => text().includes("Choose your own password"), "forced change");
check("temporary password forces a change before anything else", !nav().length);
type("Password you just logged in with", "Sara-temp-12"); type("New password (8+", "Sara-new-pass-1"); type("New password again", "Sara-new-pass-1"); click(btn("Save and continue"));
await until(() => nav().includes("Dashboard"), "cashier app");
check("cashier sees only Dashboard / Inventory / Invoices / Account", JSON.stringify(nav().filter((t) => t !== "Log out")) === JSON.stringify(["Dashboard", "Inventory", "Invoices", "Account"]));

// 4 ── cashier sells; refund needs a manager's approval
const scan = input("Scan barcode"); type(scan, "8964001"); await until(() => text().includes("Basmati"), "products loaded"); scan.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
await until(() => btn("Checkout") && !btn("Checkout").disabled, "cart filled (Checkout enabled)"); click(btn("Cash")); await sleep(80); click(btn("Checkout")); await until(() => btn("Checkout")?.disabled, "checkout done (cart emptied)");
click(btn("Invoices")); await until(() => all("tbody tr").some((r) => r.textContent.includes("sara")), "invoice served by sara"); check("invoice lists who served the sale", true);
click(all("tbody tr").find((r) => r.textContent.includes("#1"))); await until(() => btn("Refund everything left"), "refund panel"); check("receipt says who served", text().includes("served by sara"));
click(btn("Refund everything left")); await until(dialog, "approval box");
check("cashier's refund asks for a MANAGER'S username + PIN", dialog().textContent.includes("manager's or owner's approval") && !!input("Manager / owner username"));
type("Manager / owner username", "boss"); type(dialog().querySelector('input[type=password]'), "1357"); click(btn("Confirm")); await until(() => dialog()?.textContent.includes("Wrong PIN"), "wrong approver PIN");
check("retry keeps the manager username typed", input("Manager / owner username").value === "boss"); type(dialog().querySelector('input[type=password]'), "4829"); click(btn("Confirm")); await until(() => text().includes("REFUNDED"), "refunded"); check("manager's PIN approves the refund", !dialog());

// 5 ── a discount needs approval; cancelling leaves the sale untouched
click(btn("Dashboard")); await until(() => input("Scan barcode"), "dashboard"); const s2 = input("Scan barcode"); type(s2, "8964002"); await until(() => !text().includes("still loading") && all("section button, .match button").length > 0, "products loaded 2"); await sleep(300); s2.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
await until(() => btn("Checkout") && !btn("Checkout").disabled, "cart 2 filled"); type("Discount code", "SAVE10"); click(btn("Cash")); await sleep(80); click(btn("Checkout")); await until(dialog, "discount approval box");
check("discount code asks for approval", dialog().textContent.includes("Discount SAVE10")); click(btn("Cancel")); await sleep(300);
check("cancelling the PIN box leaves the cart as it was, with no error banner", !dialog() && !doc.querySelector(".err") && !!btn("Clear cart"));
click(btn("Clear cart"));

// 6 ── manager-only pages are not offered; account page works; wrong current password is refused
click(btn("Account")); await until(() => text().includes("Change my password"), "account page");
type("Your password", "not-my-password"); type("New PIN (4", "6482"); type("New PIN again", "6482"); click(btn("Change PIN")); await until(() => text().includes("Your current password is wrong."), "wrong password on PIN change"); check("changing PIN needs the current password", true);

// 7 ── the boss sees both people in History
click(btn("Log out")); await until(() => input("Username"), "login again"); type("Username", "boss"); type("Password", "Boss-pass-1"); click(btn("Log in")); await until(() => nav().includes("History"), "boss back");
click(btn("History")); await until(() => text().includes("approved by boss"), "History shows approver");
const row = all("tbody tr").find((r) => r.textContent.includes("approved by boss")); check("History: refund by sara, 'approved by boss'", row.textContent.includes("sara") && row.textContent.toLowerCase().includes("refund"));
check("History also lists logins and people changes", text().includes("logged in") && text().includes("Person added"));

// 8 ── a dead session sends you to the login screen with an explanation
jar.clear(); click(btn("Reports")); await until(() => text().includes("Your session ended"), "session-ended notice"); check("expired session -> login screen with a message", !!input("Username"));
console.log(`\nUI CLICK-THROUGH: ${n} CHECKS PASSED (live-update streams opened: ${esCount})`); process.exit(0);
