import { useEffect, useState } from "react";
import { api } from "./api";
import type { Alert } from "./api";
import { req } from "./api";

/** Alerts at the very top of every page (high priority: shown before any page content). A critical alert needs an explicit
 *  acknowledgement (who and when are recorded) and stays on screen until the problem is fixed AND it has been acknowledged. */
export function AlertsBar({ tick, alertTick }: { tick: number; alertTick: number }) {
  const [list, setList] = useState<Alert[]>([]), [err, setErr] = useState("");
  useEffect(() => { let on = true; req<{ alerts: Alert[] }>("/alerts").then((r) => on && setList(r.alerts)).catch(() => {}); return () => { on = false; }; }, [tick, alertTick]);
  const ack = async (a: Alert) => { setErr(""); try { await req(`/alerts/${a.id}/ack`, {}); setList((await req<{ alerts: Alert[] }>("/alerts")).alerts); } catch (e) { setErr((e as Error).message); } };
  void api;
  if (!list.length) return null;
  return (
    <div className="sx-alerts" role="alert">
      {err && <div className="err">{err}</div>}
      {list.map((a) => (
        <div key={a.id} className={`sx-alert ${a.severity}`}>
          <div className="t">
            <b>{a.severity === "critical" ? "CRITICAL: " : ""}{a.title}</b>
            {a.detail}
            <small>{" "}{a.still_active ? `Raised ${new Date(a.raised_ts).toLocaleTimeString()}${a.times > 1 ? ` · seen ${a.times}×` : ""}` : "The problem is fixed."}
              {a.ack_by && ` · Acknowledged by ${a.ack_by} at ${new Date(a.ack_ts ?? "").toLocaleTimeString()}${a.still_active ? " — still active" : ""}`}</small>
          </div>
          {a.needs_ack && <button className="primary" onClick={() => void ack(a)}>Acknowledge</button>}
        </div>
      ))}
    </div>
  );
}
