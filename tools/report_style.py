"""共用的報告樣式（手機優先、自動深淺色）。"""

CSS = """
:root{color-scheme:light;--bg:#f4f4f1;--surface:#fcfcfb;--text:#0b0b0b;--text2:#52514e;--muted:#7a7974;
--grid:#e1e0d9;--ring:rgba(11,11,11,.10);--series:#2a78d6;--series-dim:#86b6ef;
--good:#0ca30c;--warning:#fab219;--serious:#ec835a;--critical:#d03b3b}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#121211;--surface:#1a1a19;
--text:#f0efec;--text2:#c3c2b7;--muted:#8f8e88;--grid:#2c2c2a;--ring:rgba(255,255,255,.10);--series:#3987e5;--series-dim:#184f95}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#121211;--surface:#1a1a19;--text:#f0efec;--text2:#c3c2b7;--muted:#8f8e88;
--grid:#2c2c2a;--ring:rgba(255,255,255,.10);--series:#3987e5;--series-dim:#184f95}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans TC","PingFang TC","Microsoft JhengHei",sans-serif}
main{max-width:860px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:22px;margin:0 0 2px}h2{font-size:17px;margin:28px 0 10px}
.sub{color:var(--text2);font-size:13px}
.card{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:14px 16px;margin-bottom:12px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.tile .k{color:var(--text2);font-size:12px}.tile .v{font-size:22px;font-weight:650;font-variant-numeric:tabular-nums}
.tile .s{color:var(--muted);font-size:12px}
.alert{display:flex;gap:8px;padding:8px 0;border-bottom:1px solid var(--grid);font-size:14px}.alert:last-child{border:0}
.alert .ic{flex:none;width:1.3em;text-align:center;font-weight:700}
.alert.serious .ic{color:var(--serious)}.alert.warning .ic{color:var(--warning)}.alert.info .ic{color:var(--muted)}
.bar-row{display:grid;grid-template-columns:minmax(90px,34%) 1fr 56px;gap:8px;align-items:center;padding:4px 0;font-size:13.5px}
.bar-row .lbl{line-height:1.3;overflow-wrap:anywhere}
.bar-row .track{height:14px;display:flex;gap:2px}
.bar-row .seg{height:100%;background:var(--series);border-radius:0 4px 4px 0}
.bar-row .seg.via{background:var(--series-dim);border-radius:0 4px 4px 0}
.bar-row .seg.direct+.seg.via{border-radius:0 4px 4px 0}.bar-row .seg.direct:not(:last-child){border-radius:0}
.bar-row .val{text-align:right;font-variant-numeric:tabular-nums;color:var(--text2)}
.bar-row:hover .lbl{color:var(--series)}
.legend{display:flex;gap:14px;font-size:12px;color:var(--text2);margin-bottom:6px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th,td{padding:6px 4px;border-bottom:1px solid var(--grid);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{color:var(--text2);font-weight:500;font-size:12px}
.scroll{overflow-x:auto}.pos{color:var(--good)}.neg{color:var(--critical)}
.tag{font-size:11px;padding:1px 6px;border-radius:99px;border:1px solid var(--ring);color:var(--text2)}
ul{margin:0;padding-left:18px}li{margin:4px 0;font-size:14px}
.note{color:var(--muted);font-size:12px;margin-top:24px}
"""
