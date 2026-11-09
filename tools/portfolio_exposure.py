"""持股總覽：整合多券商持股、換算幣別、拆解 ETF 成分股，算出真正的曝險。

輸入：private/portfolio/holdings.toml（個人持股，不進 git）
      tools/portfolio_config.toml（名稱、主題、ETF、門檻設定）
輸出：private/reports/portfolio_YYYY-MM-DD.html、private/reports/portfolio_latest.html
      private/portfolio/snapshots/YYYY-MM-DD.json（每日快照，之後可做歷史追蹤）

用法：python -X utf8 tools/portfolio_exposure.py
"""
from __future__ import annotations

import html
import json
import sys
import tomllib
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import yfinance as yf

from report_style import CSS

ROOT = Path(__file__).resolve().parent.parent
HOLDINGS = ROOT / "private" / "portfolio" / "holdings.toml"
CONFIG = ROOT / "tools" / "portfolio_config.toml"
REPORT_DIR = ROOT / "private" / "reports"
SNAP_DIR = ROOT / "private" / "portfolio" / "snapshots"


POLICY = ROOT / "private" / "portfolio" / "policy.toml"


def load_policy() -> dict:
    """投資政策書（private/portfolio/policy.toml）；沒有就回傳空設定，改用 portfolio_config 的門檻"""
    return tomllib.loads(POLICY.read_text(encoding="utf-8")) if POLICY.exists() else {}


# ---------------------------------------------------------------- 資料抓取
def fetch_quote(ticker: str) -> dict:
    t = yf.Ticker(ticker)
    fi = t.fast_info
    try:
        info = t.info or {}
    except Exception:
        info = {}
    price = float(fi.last_price)
    prev = fi.previous_close
    return {
        "price": price,
        "prev": float(prev) if prev else None,
        "currency": (fi.currency or info.get("currency") or "USD").upper(),
        "name": info.get("shortName") or ticker,
        "quote_type": info.get("quoteType"),
        "sector": info.get("sector"),
    }


def fetch_top_holdings(ticker: str) -> list[tuple[str, str, float]]:
    try:
        df = yf.Ticker(ticker).funds_data.top_holdings
    except Exception as e:  # noqa: BLE001
        print(f"  ! {ticker} 成分股抓取失敗：{e}", file=sys.stderr)
        return []
    return [(str(sym), str(row["Name"]), float(row["Holding Percent"])) for sym, row in df.iterrows()]


def fx_to_usd(currency: str, cache: dict) -> float:
    """回傳 1 單位該幣別 = 多少 USD"""
    if currency == "USD":
        return 1.0
    if currency not in cache:
        per_usd = float(yf.Ticker(f"{currency}=X").fast_info.last_price)
        cache[currency] = 1.0 / per_usd
    return cache[currency]


# ---------------------------------------------------------------- 計算
def build(holdings: dict, cfg: dict) -> dict:
    names = cfg.get("names", {})
    aliases = cfg.get("aliases", {})
    etf_cfg = cfg.get("etfs", {})
    theme_of = {tk: th for th, tks in cfg.get("themes", {}).items() for tk in tks}
    th = cfg["thresholds"]

    def canon(sym: str) -> str:
        return aliases.get(sym, sym)

    def display(sym: str, fallback: str) -> str:
        # 美股直接用代號（比 Yahoo 的長英文名好讀），其他市場用設定檔或 Yahoo 名稱
        return names.get(sym) or (sym if "." not in sym else fallback)

    fx: dict[str, float] = {}
    quotes: dict[str, dict] = {}
    tickers = sorted({p["ticker"] for a in holdings["accounts"] for p in a["positions"]})
    print(f"抓取 {len(tickers)} 檔報價…", file=sys.stderr)
    for tk in tickers:
        quotes[tk] = fetch_quote(tk)

    rows, cash_usd_total = [], 0.0
    accounts_out = []
    for acc in holdings["accounts"]:
        acc_usd = 0.0
        for p in acc["positions"]:
            q = quotes[p["ticker"]]
            rate = fx_to_usd(q["currency"], fx)
            value = p["qty"] * q["price"]
            cost = p.get("cost")
            rows.append({
                "account": acc["label"], "ticker": p["ticker"],
                "name": display(p["ticker"], q["name"]),
                "qty": p["qty"], "price": q["price"], "currency": q["currency"],
                "value_native": value, "value_usd": value * rate,
                "cost": cost,
                "pnl_pct": (q["price"] / cost - 1) if cost else None,
                "day_pct": (q["price"] / q["prev"] - 1) if q["prev"] else None,
                "sleeve": p.get("sleeve", "core"),
                "is_etf": p["ticker"] in etf_cfg or q["quote_type"] == "ETF",
                "market": "台股" if p["ticker"].endswith((".TW", ".TWO")) else "美股／海外",
            })
            acc_usd += value * rate
        cash_usd = acc.get("cash", 0.0) * fx_to_usd(acc.get("cash_currency", "USD"), fx)
        cash_usd_total += cash_usd
        accounts_out.append({"label": acc["label"], "usd": acc_usd + cash_usd, "cash_usd": cash_usd})

    total_usd = sum(r["value_usd"] for r in rows) + cash_usd_total
    twd_per_usd = 1.0 / fx_to_usd("TWD", fx)

    # --- ETF 拆解
    expo: dict[str, dict] = defaultdict(lambda: {"usd": 0.0, "direct": 0.0, "via": defaultdict(float), "name": None})
    etf_value: dict[str, float] = defaultdict(float)
    for r in rows:
        if r["is_etf"]:
            etf_value[r["ticker"]] += r["value_usd"]
        else:
            k = canon(r["ticker"])
            expo[k]["usd"] += r["value_usd"]
            expo[k]["direct"] += r["value_usd"]
            expo[k]["name"] = display(k, r["name"])
    residual_theme: dict[str, str] = {}
    etf_holdings: dict[str, list] = {}
    for etf, val in etf_value.items():
        print(f"拆解 {etf} 成分股…", file=sys.stderr)
        hold = fetch_top_holdings(etf)
        etf_holdings[etf] = hold
        covered = 0.0
        for sym, nm, w in hold:
            k = canon(sym)
            expo[k]["usd"] += val * w
            expo[k]["via"][etf] += val * w
            expo[k]["name"] = expo[k]["name"] or display(k, nm)
            covered += w
        rk = f"__rest__{etf}"
        expo[rk]["usd"] += val * max(0.0, 1 - covered)
        expo[rk]["name"] = f"{names.get(etf, etf)} 其餘成分"
        residual_theme[rk] = etf_cfg.get(etf, {}).get("residual_theme", "廣泛市場（ETF 其餘成分）")
    if cash_usd_total:
        expo["__cash__"]["usd"] += cash_usd_total
        expo["__cash__"]["name"] = "帳戶現金"
        residual_theme["__cash__"] = "現金類"

    def theme(k: str) -> str:
        if k in residual_theme:
            return residual_theme[k]
        if k in theme_of:
            return theme_of[k]
        sec = quotes.get(k, {}).get("sector")
        return {"Technology": "科技平台／軟體", "Financial Services": "金融",
                "Consumer Defensive": "消費", "Consumer Cyclical": "消費"}.get(sec, "其他（未分類）")

    exposures = sorted(
        ({"key": k, "name": v["name"] or k, "usd": v["usd"], "pct": v["usd"] / total_usd,
          "direct_pct": v["direct"] / total_usd,
          "via": {e: u / total_usd for e, u in v["via"].items()},
          "theme": theme(k), "residual": k.startswith("__")}
         for k, v in expo.items()),
        key=lambda x: -x["usd"])

    themes: dict[str, float] = defaultdict(float)
    for e in exposures:
        themes[e["theme"]] += e["pct"]
    themes_sorted = sorted(themes.items(), key=lambda x: -x[1])

    # --- 分組合計
    def group(field):
        g = defaultdict(float)
        for r in rows:
            g[r[field]] += r["value_usd"] / total_usd
        return dict(g)
    by_market = group("market")
    by_sleeve = group("sleeve")
    if cash_usd_total:
        by_sleeve["現金"] = cash_usd_total / total_usd

    # --- 重複持有
    overlaps = []
    acc_by_tk = defaultdict(set)
    for r in rows:
        acc_by_tk[canon(r["ticker"])].add(r["account"])
    for tk, accs in acc_by_tk.items():
        if len(accs) > 1:
            overlaps.append(f"{names.get(tk, tk)} 分散在 {len(accs)} 個帳戶：{'、'.join(sorted(accs))}")
    by_index = defaultdict(list)
    for etf in etf_value:
        if etf in etf_cfg:
            by_index[etf_cfg[etf]["index"]].append(names.get(etf, etf))
    for idx, etfs in by_index.items():
        if len(etfs) > 1:
            overlaps.append(f"{'、'.join(etfs)} 都追蹤「{idx}」，本質上是同一筆持有")
    for e in exposures:
        if e["direct_pct"] > 0 and e["via"]:
            via_pct = sum(e["via"].values())
            overlaps.append(f"{e['name']}：直接持有 {e['direct_pct']:.1%}，另經由 "
                            f"{'、'.join(names.get(x, x) for x in e['via'])} 間接持有 {via_pct:.1%}")

    # --- 警示
    alerts = []
    pol = load_policy()
    conc = pol.get("concentration", {})
    single_max = conc.get("single_name_max", th["single_name_max"])
    exceptions = conc.get("exceptions", {})
    for e in exposures:
        if e["residual"]:
            continue
        limit = exceptions.get(e["key"], single_max)
        if e["pct"] > limit:
            alerts.append(("serious", f"{e['name']} 實際曝險 {e['pct']:.1%}，超過上限 {limit:.0%}，新資金不再投入"))
        elif e["key"] in exceptions and e["pct"] > limit * 0.9:
            alerts.append(("warning", f"{e['name']} 實際曝險 {e['pct']:.1%}，接近例外上限 {limit:.0%}，不再加碼"))
    watch = conc.get("theme_watch")
    t_max, t_target = conc.get("theme_max", th["theme_max"]), conc.get("theme_target")
    for t_name, p in themes_sorted:
        if "廣泛市場" in t_name or (watch is not None and t_name not in watch):
            continue
        if p > t_max:
            alerts.append(("serious", f"「{t_name}」主題佔 {p:.1%}，超過硬上限 {t_max:.0%}：衛星不得再開這個主題的部位"))
        elif t_target and p > t_target:
            alerts.append(("warning", f"「{t_name}」主題佔 {p:.1%}，高於目標 {t_target:.0%}（上限 {t_max:.0%}）：新資金優先投入其他主題"))
    alloc = pol.get("allocation", {})
    sat = by_sleeve.get("satellite", 0)
    if alloc.get("satellite_max") and sat > alloc["satellite_max"]:
        alerts.append(("serious", f"衛星部位 {sat:.1%}，超過上限 {alloc['satellite_max']:.0%}：暫停新的波段"))
    elif alloc.get("satellite_target") and sat < alloc["satellite_target"] * 0.7:
        alerts.append(("info", f"衛星部位 {sat:.1%}，低於目標 {alloc['satellite_target']:.0%}，還有約 "
                               f"NT${(alloc['satellite_target'] - sat) * total_usd / fx_to_usd('TWD', fx) / 1e4:,.0f} 萬的波段額度"))
    tk_usd = defaultdict(float)
    for r in rows:
        tk_usd[r["ticker"]] += r["value_usd"]
    small = [tk for tk, v in tk_usd.items() if v / total_usd < th["small_position"]]
    if small:
        alerts.append(("info", f"{len(small)} 檔持股低於總資產 {th['small_position']:.0%}："
                               f"{'、'.join(names.get(tk, tk) for tk in small)}"))
    losers = [r for r in rows if r["pnl_pct"] is not None and r["pnl_pct"] < -0.10]
    for r in losers:
        alerts.append(("warning", f"{r['name']} 帳面虧損 {r['pnl_pct']:.1%}，建議做論點檢查"))

    # --- B 方案：新資金買指數 ETF 多少，才能把 AI 主題稀釋到目標
    nm = pol.get("new_money", {})
    index_plan = None
    idx = nm.get("index_etf")
    if idx:
        hold = etf_holdings.get(idx) or fetch_top_holdings(idx)
        ai_w = sum(w for sym, _, w in hold if theme(canon(sym)) == "AI／半導體")  # 只算前十大，偏低估
        ai_now = dict(themes_sorted).get("AI／半導體", 0)
        target = conc.get("theme_target")
        if target and ai_now > target > ai_w:
            need_usd = (ai_now - target) * total_usd / (target - ai_w)
            index_plan = {"etf": idx, "etf_ai_weight": ai_w, "ai_now": ai_now, "target": target,
                          "need_usd": need_usd, "need_twd": need_usd * twd_per_usd}
            alerts.append(("info", f"依 B 方案：新資金再投入 {names.get(idx, idx)} 約 NT${need_usd * twd_per_usd / 1e4:,.0f} 萬"
                                   f"（且不加碼 AI 個股），AI 主題可從 {ai_now:.1%} 降到 {target:.0%}"
                                   f"（{names.get(idx, idx)} 前十大中 AI／半導體約 {ai_w:.0%}）"))

    return {
        "index_plan": index_plan,
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "holdings_updated": holdings.get("updated"),
        "fx": {"TWD_per_USD": twd_per_usd, **{f"{c}_to_USD": v for c, v in fx.items()}},
        "total_usd": total_usd, "total_twd": total_usd * twd_per_usd,
        "accounts": accounts_out, "rows": rows,
        "exposures": exposures, "themes": themes_sorted,
        "by_market": by_market, "by_sleeve": by_sleeve,
        "overlaps": overlaps, "alerts": alerts,
        "etf_coverage": {e: sum(w for _, _, w in h) for e, h in etf_holdings.items()},
    }


# ---------------------------------------------------------------- HTML


def _fmt_money(v: float, cur: str = "") -> str:
    return f"{cur}{v:,.0f}"


def _pct(v: float | None, signed: bool = False) -> str:
    if v is None:
        return "—"
    return f"{v:+.1%}" if signed else f"{v:.1%}"


def render(d: dict, cfg: dict) -> str:
    e = html.escape
    rate = d["fx"]["TWD_per_USD"]
    sleeve_label = {"core": "核心", "satellite": "衛星", "現金": "現金"}
    out = [f"<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>持股總覽</title><style>{CSS}</style></head><body><main>"]
    out.append(f"<h1>持股總覽</h1><div class='sub'>報價時間 {e(d['as_of'])}・持股更新於 {e(str(d['holdings_updated']))}"
               f"・匯率 1 USD = {rate:.2f} TWD</div>")

    # 摘要
    m, s = d["by_market"], d["by_sleeve"]
    top = next(x for x in d["exposures"] if not x["residual"])
    out.append("<h2>摘要</h2><div class='tiles'>")
    out.append(f"<div class='card tile'><div class='k'>總資產</div><div class='v'>NT${d['total_twd']/1e4:,.0f} 萬</div>"
               f"<div class='s'>≈ US${d['total_usd']:,.0f}</div></div>")
    out.append(f"<div class='card tile'><div class='k'>台股／美股</div><div class='v'>{m.get('台股',0):.0%}／{m.get('美股／海外',0):.0%}</div>"
               f"<div class='s'>依掛牌市場</div></div>")
    out.append(f"<div class='card tile'><div class='k'>核心／衛星</div><div class='v'>{s.get('core',0):.0%}／{s.get('satellite',0):.0%}</div>"
               f"<div class='s'>初步分類，可在 holdings.toml 調整</div></div>")
    out.append(f"<div class='card tile'><div class='k'>最大單一曝險</div><div class='v'>{e(top['name'])}</div>"
               f"<div class='s'>{top['pct']:.1%}（含 ETF 拆解）</div></div></div>")

    # 警示
    icon = {"serious": "▲", "warning": "!", "info": "i"}
    label = {"serious": "超標", "warning": "注意", "info": "資訊"}
    out.append("<h2>需要注意</h2><div class='card'>")
    for lvl, msg in d["alerts"] or [("info", "目前沒有警示")]:
        out.append(f"<div class='alert {lvl}'><span class='ic' aria-hidden='true'>{icon[lvl]}</span>"
                   f"<span><b>{label[lvl]}</b>　{e(msg)}</span></div>")
    out.append("</div>")

    # 實際曝險
    names_shown = [x for x in d["exposures"] if not x["residual"]][:15]
    mx = max(x["pct"] for x in names_shown)
    out.append("<h2>實際個股曝險（前 15，含 ETF 拆解）</h2><div class='card'>"
               "<div class='legend'><span><i style='background:var(--series)'></i>直接持有</span>"
               "<span><i style='background:var(--series-dim)'></i>經由 ETF</span></div>")
    for x in names_shown:
        via = sum(x["via"].values())
        tip = f"{x['name']}：合計 {x['pct']:.1%}（直接 {x['direct_pct']:.1%}，ETF {via:.1%}）"
        segs = ""
        if x["direct_pct"]:
            segs += f"<div class='seg direct' style='width:{x['direct_pct']/mx*100:.2f}%'></div>"
        if via:
            segs += f"<div class='seg via' style='width:{via/mx*100:.2f}%'></div>"
        out.append(f"<div class='bar-row' title='{e(tip)}'><span class='lbl'>{e(x['name'])}</span>"
                   f"<div class='track'>{segs}</div><span class='val'>{x['pct']:.1%}</span></div>")
    out.append("</div>")

    # 主題
    mx = max(p for _, p in d["themes"])
    out.append("<h2>主題曝險</h2><div class='card'>")
    for t_name, p in d["themes"]:
        out.append(f"<div class='bar-row' title='{e(t_name)}：{p:.1%}'><span class='lbl'>{e(t_name)}</span>"
                   f"<div class='track'><div class='seg' style='width:{p/mx*100:.2f}%'></div></div>"
                   f"<span class='val'>{p:.1%}</span></div>")
    out.append("</div>")

    # 重複持有
    if d["overlaps"]:
        out.append("<h2>重複持有</h2><div class='card'><ul>")
        out += [f"<li>{e(o)}</li>" for o in d["overlaps"]]
        out.append("</ul></div>")

    # 帳戶
    out.append("<h2>各帳戶</h2><div class='card scroll'><table><tr><th>帳戶</th><th>市值 (NT$)</th><th>市值 (US$)</th><th>佔比</th></tr>")
    for a in d["accounts"]:
        out.append(f"<tr><td>{e(a['label'])}</td><td>{a['usd']*rate:,.0f}</td><td>{a['usd']:,.0f}</td>"
                   f"<td>{a['usd']/d['total_usd']:.1%}</td></tr>")
    out.append("</table></div>")

    # 明細
    out.append("<h2>持股明細</h2><div class='card scroll'><table><tr><th>標的</th><th>帳戶</th><th>分類</th>"
               "<th>數量</th><th>現價</th><th>市值 (NT$)</th><th>佔比</th><th>今日</th><th>報酬</th></tr>")
    for r in sorted(d["rows"], key=lambda r: -r["value_usd"]):
        day_cls = "pos" if (r["day_pct"] or 0) > 0 else "neg" if (r["day_pct"] or 0) < 0 else ""
        pnl_cls = "pos" if (r["pnl_pct"] or 0) > 0 else "neg" if (r["pnl_pct"] or 0) < 0 else ""
        qty = f"{r['qty']:,.4f}".rstrip("0").rstrip(".")
        out.append(f"<tr><td>{e(r['name'])}<br><span class='sub'>{e(r['ticker'])}</span></td>"
                   f"<td><span class='sub'>{e(r['account'])}</span></td>"
                   f"<td><span class='tag'>{sleeve_label.get(r['sleeve'], r['sleeve'])}</span></td>"
                   f"<td>{qty}</td><td>{r['price']:,.2f}</td><td>{r['value_usd']*rate:,.0f}</td>"
                   f"<td>{r['value_usd']/d['total_usd']:.1%}</td>"
                   f"<td class='{day_cls}'>{_pct(r['day_pct'], True)}</td>"
                   f"<td class='{pnl_cls}'>{_pct(r['pnl_pct'], True)}</td></tr>")
    out.append("</table></div>")

    cov = "、".join(f"{k} {v:.0%}" for k, v in d["etf_coverage"].items())
    out.append(f"<p class='note'>資料來源：Yahoo Finance（yfinance），報價可能有延遲。ETF 拆解只用前十大成分股"
               f"（涵蓋比例：{e(cov)}），其餘成分歸到該 ETF 的代表主題。報酬欄只有提供成本的部位才會顯示。"
               f"本報告為個人紀錄與研究用途，不構成投資建議。</p>")
    out.append("</main></body></html>")
    return "".join(out)


# ---------------------------------------------------------------- main
def main() -> None:
    holdings = tomllib.loads(HOLDINGS.read_text(encoding="utf-8"))
    cfg = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    d = build(holdings, cfg)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    page = render(d, cfg)
    (REPORT_DIR / f"portfolio_{today}.html").write_text(page, encoding="utf-8")
    (REPORT_DIR / "portfolio_latest.html").write_text(page, encoding="utf-8")
    (SNAP_DIR / f"{today}.json").write_text(json.dumps(d, ensure_ascii=False, indent=2, default=dict), encoding="utf-8")

    print(f"總資產 NT${d['total_twd']:,.0f}（US${d['total_usd']:,.0f}）")
    print("前 8 大實際曝險：")
    for x in [x for x in d["exposures"] if not x["residual"]][:8]:
        print(f"  {x['name']:<24} {x['pct']:6.1%}")
    print("主題：")
    for t_name, p in d["themes"]:
        print(f"  {t_name:<24} {p:6.1%}")
    print("警示：")
    for lvl, msg in d["alerts"]:
        print(f"  [{lvl}] {msg}")
    print(f"報告：{REPORT_DIR / 'portfolio_latest.html'}")


if __name__ == "__main__":
    main()
