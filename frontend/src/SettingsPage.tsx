import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { req } from "./api";

interface Setting { key: string; label: string; value: boolean | number | string | Record<string, number> }
interface Net { enforce: boolean; approved_ips: string[]; missing: string[]; bind_host: string; detected: { ip: string; kind: string; approved: boolean; type?: string | null; name?: string | null }[] }
interface Card { ref: string; configured: boolean; updated_ts: string | null; updated_by: string | null }
type Note = { ok: boolean; text: string } | null;

function useGet<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [d, setD] = useState<T | null>(null), [err, setErr] = useState("");
  useEffect(() => { let on = true; fn().then((x) => { if (on) { setD(x); setErr(""); } }).catch((e: Error) => { if (on) setErr(e.message); }); return () => { on = false; }; }, deps); // eslint-disable-line
  return { d, err };
}

/** Owner: switches, the approved network connection, and the card machine (write-only). */
export function Settings({ tick }: { tick: number }) {
  const [n, setN] = useState(0), [note, setNote] = useState<Note>(null);
  const st = useGet(() => req<{ settings: Setting[]; lock_seconds: number }>("/settings"), [n]);
  const net = useGet(() => req<Net>("/network"), [n, tick]);
  const card = useGet(() => req<Card>("/integrations/card"), [n]);
  const [pick, setPick] = useState<string[] | null>(null), [pw, setPw] = useState("");
  const [cf, setCf] = useState({ merchant_id: "", api_key: "", terminal_id: "" });
  const [num, setNum] = useState<Record<string, string>>({}), [codesTxt, setCodesTxt] = useState<string | null>(null);
  const parseCodes = (t: string) => Object.fromEntries(t.split(/[\n,]+/).map((x) => x.trim()).filter(Boolean).map((x) => { const [k, v] = x.split("="); return [(k ?? "").trim(), Number((v ?? "").trim())]; }));
  const run = async (fn: () => Promise<unknown>, okText: string) => { setNote(null); try { await fn(); setNote({ ok: true, text: okText }); setN((x) => x + 1); return true; } catch (e) { setNote({ ok: false, text: (e as Error).message }); return false; } };
  const approved = pick ?? net.d?.approved_ips ?? [];

  const saveNet = async (e: FormEvent) => {
    e.preventDefault();
    if (await run(() => req("/network/policy", { approved_ips: approved, password: pw }), approved.length ? "Approved connection saved." : "Any connection on the shop network is accepted.")) { setPw(""); setPick(null); }
  };
  const saveCard = async (e: FormEvent) => {
    e.preventDefault();
    if (await run(() => req("/integrations/card/credentials", { merchant_id: cf.merchant_id, api_key: cf.api_key, terminal_id: cf.terminal_id || undefined }), "Card machine credentials stored. They can never be shown again.")) setCf({ merchant_id: "", api_key: "", terminal_id: "" });
  };
  return (
    <div className="sx">
      <h1>Settings</h1>
      {note && <div className={note.ok ? "sx-ok" : "err"}>{note.text}</div>}
      {(st.err || net.err) && <div className="err">{st.err || net.err}</div>}

      <section>
        <h2>Switches</h2>
        <table><tbody>
          {(st.d?.settings ?? []).map((s) => (
            <tr key={s.key}>
              <td>{s.label}</td>
              <td>{typeof s.value === "boolean"
                ? <label className="sx-check"><input type="checkbox" checked={s.value} onChange={(e) => void run(() => req("/settings", { key: s.key, value: e.target.checked }), "Saved.")} /> {s.value ? "on" : "off"}</label>
                : typeof s.value === "number"
                ? <span className="sx-inline"><input type="number" style={{ width: 110 }} value={num[s.key] ?? String(s.value)} onChange={(e) => setNum({ ...num, [s.key]: e.target.value })} />
                  <button onClick={() => void run(() => req("/settings", { key: s.key, value: Number(num[s.key] ?? s.value) }), "Saved.")}>Save</button></span>
                : typeof s.value === "string"
                ? <span className="sx-inline"><input style={{ width: 280 }} maxLength={200} value={num[s.key] ?? s.value} onChange={(e) => setNum({ ...num, [s.key]: e.target.value })} />
                  <button onClick={() => void run(() => req("/settings", { key: s.key, value: num[s.key] ?? s.value }), "Saved.")}>Save</button></span>
                : <span className="sx-stack"><textarea rows={3} style={{ width: 280 }} value={codesTxt ?? Object.entries(s.value).map(([k, v]) => `${k}=${v}`).join("\n")} onChange={(e) => setCodesTxt(e.target.value)} />
                  <span className="hint">CODE=percent</span>
                  <button onClick={() => void run(() => req("/settings", { key: s.key, value: parseCodes(codesTxt ?? Object.entries(s.value).map(([k, v]) => `${k}=${v}`).join("\n")) }), "Saved.").then((ok) => ok && setCodesTxt(null))}>Save codes</button></span>}</td>
            </tr>
          ))}
        </tbody></table>
      </section>

      <section>
        <h2>Network <small>{net.d && (net.d.enforce ? <b className="good">protection on</b> : <b className="bad">protection OFF (developer override)</b>)}</small></h2>
        {net.d && net.d.missing.length > 0 && <div className="err">The approved connection ({net.d.missing.join(", ")}) is not present on this computer right now.</div>}
        <form className="sx-ips" onSubmit={(e) => void saveNet(e)}>
          {(net.d?.detected ?? []).filter((d) => d.kind !== "public").map((d) => (
            <label key={d.ip}><input type="checkbox" checked={approved.includes(d.ip)} disabled={d.kind === "loopback"}
              onChange={(e) => setPick(e.target.checked ? [...approved, d.ip] : approved.filter((x) => x !== d.ip))} />
              {" "}<code>{d.ip}</code> <span className="hint">{d.kind === "loopback" ? "this computer" : `${d.type ?? ""}${d.name ? ` “${d.name}”` : ""}`}</span></label>
          ))}
          {approved.filter((ip) => !(net.d?.detected ?? []).some((d) => d.ip === ip)).map((ip) => <label key={ip}><input type="checkbox" checked onChange={() => setPick(approved.filter((x) => x !== ip))} /> <code>{ip}</code> <span className="hint bad">not on this computer now</span></label>)}
          <input type="password" value={pw} autoComplete="current-password" placeholder="Your password, to confirm" onChange={(e) => setPw(e.target.value)} />
          <button className="primary" disabled={!pw || pick === null}>Save approved connection</button>
        </form>
      </section>

      <section>
        <h2>Card machine <small>{card.d?.configured ? <b className="good">credentials stored</b> : <b className="hint">not set up</b>}</small></h2>
        {card.d?.configured && <p className="hint">{`Last stored by ${card.d.updated_by} on ${card.d.updated_ts ? new Date(card.d.updated_ts).toLocaleString() : ""}.`}</p>}
        <form className="sx-stack" onSubmit={(e) => void saveCard(e)}>
          <input type="password" autoComplete="off" value={cf.merchant_id} placeholder="Merchant ID" onChange={(e) => setCf({ ...cf, merchant_id: e.target.value })} />
          <input type="password" autoComplete="off" value={cf.api_key} placeholder="API key / secret" onChange={(e) => setCf({ ...cf, api_key: e.target.value })} />
          <input type="password" autoComplete="off" value={cf.terminal_id} placeholder="Terminal ID (optional)" onChange={(e) => setCf({ ...cf, terminal_id: e.target.value })} />
          <button className="primary" disabled={!cf.merchant_id || !cf.api_key}>{card.d?.configured ? "Replace credentials" : "Store credentials"}</button>
          {card.d?.configured && <button type="button" onClick={() => window.confirm("Remove the stored card machine credentials?") && void run(() => req("/integrations/card/clear", {}), "Credentials removed.")}>Remove</button>}
        </form>
      </section>
    </div>
  );
}

interface Diag { stages: { order: number; name: string; priority: string; question: string }[]; monitor: { last_run: string | null; last_error: string | null; interval_seconds: number; debounce: number };
  settings: Record<string, unknown>; lock: { seconds: number; server_grace_seconds: number }; routes_with_rules: number }

/** Developer only: how requests flow, the checker's state, and the network protection override. */
export function Developer({ tick }: { tick: number }) {
  const [n, setN] = useState(0), [note, setNote] = useState<Note>(null);
  const dg = useGet(() => req<Diag>("/developer/diagnostics"), [n, tick]);
  const enforce = (dg.d?.settings.network as { enforce?: boolean } | undefined)?.enforce ?? true;
  const flip = async () => { setNote(null); try { await req("/network/override", { enforce: !enforce }); setN((x) => x + 1); } catch (e) { setNote({ ok: false, text: (e as Error).message }); } };
  return (
    <div className="sx">
      <h1>Developer</h1>
      {note && <div className="err">{note.text}</div>}{dg.err && <div className="err">{dg.err}</div>}
      <section>
        <h2>Request pipeline (fixed order)</h2>
        <table><thead><tr><th>#</th><th>Stage</th><th>Priority</th><th>Question</th></tr></thead>
          <tbody>{(dg.d?.stages ?? []).map((s) => <tr key={s.name}><td>{s.order}</td><td><b>{s.name}</b></td><td className={s.priority === "high" ? "bad" : ""}>{s.priority}</td><td>{s.question}</td></tr>)}</tbody></table>
        <p className="hint">{dg.d?.routes_with_rules} routes have an explicit permission rule. Lock: {dg.d?.lock.seconds}s + {dg.d?.lock.server_grace_seconds}s server grace. Checker: every {dg.d?.monitor.interval_seconds}s, last run {dg.d?.monitor.last_run ?? "never"}{dg.d?.monitor.last_error ? ` — error: ${dg.d.monitor.last_error}` : ""}.</p>
      </section>
      <section>
        <h2>Network protection</h2>
        <p>Currently <b className={enforce ? "good" : "bad"}>{enforce ? "ON" : "OFF"}</b>.</p>
        <button onClick={() => void flip()}>{enforce ? "Switch protection OFF" : "Switch protection ON"}</button>
      </section>
    </div>
  );
}
