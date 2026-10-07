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
class ES { addEventListener(n, f) { (this.ls ??= {})[n] = f; } constructor(u) { esCount++; this.ctl = new AbortController(); (async () => { try { const r = await jfetch(u, { signal: this.ctl.signal }); if (!r.ok) throw 0; this.onopen?.();
  const rd = r.body.getReader(), dec = new TextDecoder(); let b = ""; for (;;) { const { done, value } = await rd.read(); if (done) break; b += dec.decode(value); let i; while ((i = b.indexOf("\n\n")) >= 0) { const ev = b.slice(0, i); b = b.slice(i + 2); if (ev.startsWith("event: alert")) this.ls?.alert?.({}); else if (ev.includes("data:")) this.onmessage?.({}); } } } catch { /* closed */ } this.onerror?.(); })(); } close() { this.ctl.abort(); } }

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

const dialogText = () => doc.querySelector(".sx-dialog")?.textContent ?? "";
const lockScreen = () => doc.querySelector(".sx-lock");
const pad = async (digits) => { const inp = doc.querySelector(".sx-pin"); type(inp, digits); await sleep(50); const ok = all(".sx-keypad button").find((b) => b.textContent === "OK"); click(ok); };
const api = async (path, body, h = {}) => { const r = await jfetch("http://localhost:8000" + path, { method: body === undefined ? "GET" : "POST", headers: { "Content-Type": "application/json", "X-POS": "1", ...h }, body: body === undefined ? undefined : JSON.stringify(body) }); return r; };

// 1 ── first-time setup: ONE password for the owner, no PIN, no employee yet
await until(() => text().includes("First-time setup"), "setup screen"); check("fresh install shows first-time setup (no data visible)", !nav().length);
check("setup asks for a password only (no PIN field)", !input("PIN") && !!input("Password"));
type("Username", "boss"); type("Full name", "The Boss"); type("Password", "Boss-pass-1"); type("Password again", "Boss-pass-1");
click(btn("Create owner account")); await until(() => nav().includes("Security"), "owner app");
check("owner sees every owner tab but not Developer", ["Dashboard", "Inventory", "Catalog", "Purchasing", "Invoices", "Cash up", "Reports", "History", "Settings", "Security", "Account"].every((t) => nav().includes(t)) && !nav().includes("Developer"));
await until(() => text().includes("● live"), "live updates connected (SSE with the login cookie)");

// 2 ── owner adds an employee with a PIN (no password for employees)
click(btn("Security")); await until(() => text().includes("Who can do what"), "security page");
check("employee form asks for a PIN, not a password", !!input("PIN (4") && !input("Password"));
type("Username", "sara"); type("Name", "Sara K"); type("PIN (4", "2864"); click(btn("Add")); await until(() => text().includes("Added sara"), "added note");
check("no PIN box for the owner: the password at sign-in was enough", !doc.querySelector(".sx-dialog"));
check("permission table lists employee / owner / developer", text().includes("Employee") && text().includes("Owner") && text().includes("Developer"));
type("Username", "noor"); type("Name", "Noor A"); type("PIN (4", "5731"); click(btn("Add")); await until(() => text().includes("Added noor"), "second employee");

// 3 ── 2FA for the owner (authenticator seed shown, recovery codes shown once)
click(btn("Account")); await until(() => text().includes("Two-step sign-in"), "account page"); click(btn("Set up two-step sign-in")); await until(() => doc.querySelector("code"), "2FA key shown");
const seed = doc.querySelector("code").textContent.trim(); check("2FA setup key is shown for the authenticator app", /^[A-Z2-7]{32}$/.test(seed));
const { createHmac } = await import("crypto");
const b32 = (s) => { const A = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"; let bits = ""; for (const c of s) bits += A.indexOf(c).toString(2).padStart(5, "0"); const out = []; for (let i = 0; i + 8 <= bits.length; i += 8) out.push(parseInt(bits.slice(i, i + 8), 2)); return Buffer.from(out); };
const totp = (step) => { const key = b32(seed), msg = Buffer.alloc(8); msg.writeBigUInt64BE(BigInt(step)); const h = createHmac("sha1", key).update(msg).digest(); const o = h[19] & 15; return String((h.readUInt32BE(o) & 0x7fffffff) % 1000000).padStart(6, "0"); };
type("6-digit code", totp(Math.floor(Date.now() / 30000))); click(btn("Turn on")); await until(() => text().includes("Recovery codes"), "recovery codes");
check("recovery codes are shown once (8 of them)", all(".sx-codes div").length === 8);

// 4 ── employee signs in with name tile + PIN; limited app
click(btn("Sign out")); await until(() => text().includes("Sign in") && all(".sx-tile").length, "sign-in tiles");
check("sign-in shows name tiles for employees only (not the owner)", all(".sx-tile").map((t) => t.textContent).sort().join() === "Noor A,Sara K");
click(all(".sx-tile").find((t) => t.textContent === "Sara K")); await until(() => doc.querySelector(".sx-pin"), "pin pad");
await pad("0000"); await until(() => text().includes("Wrong PIN (4 tries left)"), "wrong PIN message"); check("wrong PIN is refused with the tries left", true);
await pad("2864"); await until(() => nav().includes("Dashboard"), "employee app");
check("employee sees only Dashboard / Inventory / Invoices / Cash up / Account", JSON.stringify(nav().filter((t) => !/Lock|Sign out/.test(t))) === JSON.stringify(["Dashboard", "Inventory", "Invoices", "Cash up", "Account"]));

// 5 ── employee sells; refund and discount use the employee's OWN PIN
const scan = input("Scan barcode"); type(scan, "8964001"); await until(() => text().includes("Basmati"), "products loaded"); scan.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
await until(() => btn("Checkout") && !btn("Checkout").disabled, "cart filled"); click(all(".pay button").find((b) => b.textContent.trim() === "Cash")); await sleep(80); click(btn("Checkout")); await until(() => btn("Checkout")?.disabled, "checkout done");
click(btn("Invoices")); await until(() => all("tbody tr").some((r) => r.textContent.includes("sara")), "invoice served by sara"); check("invoice lists who served the sale", true);
click(all("tbody tr").find((r) => r.textContent.includes("#1"))); await until(() => btn("Refund everything left"), "refund panel");
click(btn("Refund everything left")); await until(() => doc.querySelector(".sx-dialog"), "PIN box");
check("refund asks for the employee's OWN PIN (no manager username)", !!doc.querySelector(".sx-dialog input.sx-pin") && !input("Manager"));
await pad("1357"); await until(() => dialogText().includes("Wrong PIN"), "wrong PIN"); check("wrong PIN keeps the box open with a message", true);
await pad("2864"); await until(() => text().includes("REFUNDED"), "refund done"); check("refund done with the employee's own PIN", true);
click(btn("Dashboard")); await until(() => input("Scan barcode"), "dashboard"); const s2 = input("Scan barcode"); type(s2, "8964002"); await until(() => text().includes("Cooking Oil 1L ("), "product matched (list loaded)"); s2.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
await until(() => btn("Checkout") && !btn("Checkout").disabled, "cart 2 filled"); type("Discount code", "SAVE10"); click(all(".pay button").find((b) => b.textContent.trim() === "Cash")); await sleep(80); click(btn("Checkout")); await until(() => doc.querySelector(".sx-dialog"), "discount PIN box");
check("discount code asks for the employee's PIN", dialogText().includes("Discount SAVE10")); click(btn("Cancel")); await sleep(300);
check("cancelling leaves the cart as it was, with no error banner", !doc.querySelector(".sx-dialog") && !doc.querySelector(".err") && !!btn("Clear cart"));

// 6 ── the 1-minute lock: screen covered, sale kept, a PIN box never outlives it, only the same person can unlock
click(btn("Checkout")); await until(() => doc.querySelector(".sx-dialog"), "PIN box before lock");
click(btn("Lock screen")); await until(lockScreen, "lock screen");
check("a PIN box is dismissed when the screen locks", !doc.querySelector(".sx-dialog")); check("lock screen covers the app", !!lockScreen() && text().includes("locked"));
await sleep(300); const r1 = await api("/inventory"); check("server refuses everything while locked (423)", r1.status === 423);
await pad("9999"); await until(() => text().includes("Wrong PIN"), "wrong unlock PIN"); check("wrong PIN does not unlock", !!lockScreen());
await pad("5731"); await sleep(400); check("another employee's PIN does not unlock", !!lockScreen());
await pad("2864"); await until(() => !lockScreen(), "unlocked"); check("right PIN unlocks and the sale is still in the cart", !!btn("Clear cart"));
click(btn("Clear cart")); await sleep(200);

// 7 ── cash up: the employee never sees the expected cash
click(btn("Cash up")); await until(() => text().includes("My shift"), "cash up page"); type("Opening float", "500"); click(btn("Open shift")); await until(() => text().includes("Shift opened"), "shift opened");
type("Cash counted", "100"); click(btn("Submit count and close shift")); await until(() => text().includes("Shift closed"), "shift closed");
await until(() => !text().includes("Open since"), "shift view refreshed"); check("employee gets no expected amount or difference", !/xpected|ifference/.test(text()));


// 9 ── owner sees everything, including who did what
click(btn("Sign out")); await until(() => text().includes("Sign in"), "login screen"); click(btn("Owner")); await sleep(80);
type("Username", "boss"); type("Password", "Boss-pass-1"); click(btn("Sign in")); await until(() => input("6-digit code"), "2FA code asked after the right password");
check("owner with 2FA is asked for the code after the password", true);
type("6-digit code", "123456"); click(btn("Sign in")); await until(() => text().includes("Wrong name, password or code."), "bad code"); check("a wrong code is refused with a generic message", true);
type("Username", "boss"); type("Password", "Boss-pass-1"); click(btn("Sign in")); await until(() => input("6-digit code"), "code asked again");
type("6-digit code", totp(Math.floor(Date.now() / 30000) + 1)); click(btn("Sign in")); await until(() => nav().includes("History"), "boss in");
click(btn("Cash up")); await until(() => text().includes("Shifts"), "owner cash-up"); await until(() => text().includes("-Rs") || text().includes("Rs"), "figures"); check("owner sees expected cash and the difference", text().includes("Difference") && text().includes("Expected"));
click(btn("History")); await until(() => text().includes("boss signed in"), "History loaded with entries"); check("History lists the employee's refund, PIN lock-ups and sign-ins", text().toLowerCase().includes("refund") && text().includes("signed in"));
click(btn("Settings")); await until(() => text().includes("Card machine"), "settings"); check("Settings shows switches, network and card machine", text().includes("Network") && text().includes("Switches"));
check("Settings shows tax, discount codes and receipt text", text().includes("Sales tax") && text().includes("Discount codes") && !!all("textarea").find((t) => t.value.includes("SAVE10=10")) && text().includes("Shop name printed on receipts"));
click(btn("End of day")); await until(() => text().includes("Takings by payment method"), "end of day"); check("End of day shows takings, shifts and cash moved", text().includes("Cash drawers") && text().includes("Cash"));
click(btn("Profit")); await until(() => text().includes("Gross profit"), "profit"); check("Profit page shows gross profit and stock value", text().includes("Profit after expenses") && text().includes("Stock on the shelves"));
click(btn("Catalog")); await until(() => btn("Import from file"), "catalog tools"); click(btn("Import from file")); await until(() => btn("Make a barcode"), "barcode button");
click(btn("Make a barcode")); await until(() => /999\d{10}/.test(text()), "generated barcode"); check("Catalog can make an in-shop barcode", true);
click(btn("Settings")); await until(() => text().includes("Card machine"), "settings again");
type("Merchant ID", "MERCH-1"); type("API key", "SECRET-ABC-999"); click(btn("Store credentials")); await until(() => text().includes("credentials stored"), "card stored");
check("card credentials are never shown back", !text().includes("SECRET-ABC-999") && !text().includes("MERCH-1") && !doc.documentElement.outerHTML.includes("SECRET-ABC-999"));

// 10 ── a dead session sends you to the sign-in screen with an explanation
jar.clear(); click(btn("Reports")); await until(() => text().includes("Your session ended"), "session-ended notice"); check("expired session -> sign-in screen with a message", text().includes("Sign in"));
console.log(`\nUI CLICK-THROUGH: ${n} CHECKS PASSED (live-update streams opened: ${esCount})`); process.exit(0);
