import { useState } from "react";
import type { ChangeEvent } from "react";
import { req } from "./api";

interface Row { sku: string; name: string; cat: string; unit: string; price: number; cost: number }
interface Result { ok: boolean; saved: number; would_add?: number; errors: { row: number; sku: string; error: string }[]; error_count?: number; checked: number }

/** Minimal CSV reader: commas, "quoted, fields", doubled quotes, CRLF. Header row required: barcode,name,category,unit,price,cost */
export function parseCsv(text: string): string[][] {
  const out: string[][] = []; let row: string[] = [], cur = "", q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) { if (c === '"') { if (text[i + 1] === '"') { cur += '"'; i++; } else q = false; } else cur += c; }
    else if (c === '"') q = true;
    else if (c === ",") { row.push(cur); cur = ""; }
    else if (c === "\n" || c === "\r") { if (c === "\r" && text[i + 1] === "\n") i++; row.push(cur); cur = ""; if (row.some((x) => x.trim())) out.push(row); row = []; }
    else cur += c;
  }
  row.push(cur); if (row.some((x) => x.trim())) out.push(row);
  return out;
}

/** Owner: add many products from a spreadsheet saved as CSV, and make in-shop barcodes. */
export function CatalogTools({ onDone }: { onDone: () => void }) {
  const [open, setOpen] = useState(false), [rows, setRows] = useState<Row[] | null>(null), [res, setRes] = useState<Result | null>(null), [msg, setMsg] = useState(""), [code, setCode] = useState("");
  const pick = (e: ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0]; setRes(null); setRows(null); setMsg("");
    if (!f) return;
    void f.text().then(async (t) => {
      const g = parseCsv(t.replace(/^\uFEFF/, ""));
      const head = (g[0] ?? []).map((h) => h.trim().toLowerCase());
      const col = (...names: string[]) => head.findIndex((h) => names.includes(h));
      const iS = col("barcode", "sku"), iN = col("name"), iP = col("price"), iC = col("category", "cat"), iU = col("unit"), iK = col("cost");
      if (iS < 0 || iN < 0 || iP < 0) { setMsg("The first line must have the column names. Required: barcode, name, price. Optional: category, unit, cost."); return; }
      const parsed = g.slice(1).map((r) => ({ sku: (r[iS] ?? "").trim(), name: (r[iN] ?? "").trim(), cat: iC >= 0 ? (r[iC] ?? "").trim() : "", unit: iU >= 0 ? (r[iU] ?? "").trim() || "pc" : "pc", price: Number(r[iP]), cost: iK >= 0 && (r[iK] ?? "").trim() ? Number(r[iK]) : 0 }));
      if (parsed.some((p) => !Number.isFinite(p.price) || !Number.isFinite(p.cost))) { setMsg("Some prices or costs are not numbers. Write them like 150 or 150.50, with no Rs or commas."); return; }
      setRows(parsed);
      try { setRes(await req<Result>("/catalog/import", { rows: parsed, dry_run: true })); } catch (x) { setMsg((x as Error).message); }
    });
  };
  const save = async () => {
    if (!rows) return;
    try { const r = await req<Result>("/catalog/import", { rows, dry_run: false }); setRes(r); if (r.ok) { setMsg(`${r.saved} products added. Stock starts at 0: receive it through a purchase order or count it in Inventory.`); setRows(null); onDone(); } } catch (x) { setMsg((x as Error).message); }
  };
  return (
    <div className="cg-form" style={{ marginBottom: 12 }}>
      <button onClick={() => setOpen((o) => !o)}>{open ? "Close tools" : "Import from file / make a barcode"}</button>
      {open && <div style={{ marginTop: 8 }}>
        <p className="hint">CSV: <code>barcode,name,category,unit,price,cost</code></p>
        <input type="file" accept=".csv,text/csv" onChange={pick} />
        {msg && <div className={res?.ok ? "sx-ok" : "err"}>{msg}</div>}
        {res && !res.ok && <table><thead><tr><th>Line</th><th>Barcode</th><th>Problem</th></tr></thead><tbody>{res.errors.map((e) => <tr key={e.row + e.error}><td>{e.row + 1}</td><td>{e.sku}</td><td>{e.error}</td></tr>)}</tbody></table>}
        {res?.ok && rows && <p>Checked {res.checked} lines: all good. <button className="primary" onClick={() => void save()}>Add {res.would_add} products</button></p>}
        <hr />
        <button onClick={() => void req<{ sku: string }>("/catalog/next-barcode").then((r) => setCode(r.sku)).catch((x: Error) => setMsg(x.message))}>Make a barcode</button> {code && <b><code>{code}</code></b>}
      </div>}
    </div>
  );
}
