/** Prints a receipt from plain text in a small window (works with a normal printer or a 80 mm receipt printer chosen in the browser's print box). */
export function printReceipt(shop: string, text: string, footer: string) {
  const w = window.open("", "_blank", "width=380,height=600");
  if (!w) { window.alert("The browser blocked the print window. Allow pop-ups for this page and try again."); return; }
  const d = w.document;
  d.title = "Receipt";
  const style = d.createElement("style");
  style.textContent = "@page{margin:4mm}body{font:13px/1.35 monospace;width:72mm;margin:0 auto}h1{font-size:16px;text-align:center;margin:4px 0}pre{white-space:pre-wrap;margin:6px 0}p{text-align:center}";
  d.head.appendChild(style);
  const h = d.createElement("h1"); h.textContent = shop;                      // textContent: shop text can never run as HTML
  const pre = d.createElement("pre"); pre.textContent = text;
  const p = d.createElement("p"); p.textContent = footer;
  d.body.append(h, pre, p);
  w.focus(); w.print();
}
