"""每週預估修正掃描＋催化劑行事曆（美股版的月營收掃描）。

每週日晚上由排程執行，產出一頁手機版週報：
  1. 預估修正：美股持股（排除 ETF）＋觀察名單 us_stocks，逐檔看
     - eps_trend：本年度（0y）與下年度（+1y）EPS 預估，current 對 7 天前／30 天前的變化 %
     - eps_revisions：近 30 天上修／下修家數
     - upgrades_downgrades：近 7 天評等變動（券商、動作、舊→新評等）
     - analyst_price_targets：分析師平均目標價與現價差距
     訊號：下年度 EPS 30 天上修 ≥ 3% → 正面；下修 ≥ 3% → 負面；近 7 天有降評 → 注意
  2. 催化劑行事曆（未來 14 天，依日期排序）
     - 美股：持股＋觀察名單的財報日（yfinance calendar）
     - 台股：持股＋觀察名單 stocks 的除權息日（twdata.dividends → FinMind）
     - 每月 10 日月營收公布截止日（落在區間內才列）
  3. 總經事件（FOMC、CPI 等）不由程式抓，報告留區塊、JSON 標 macro="由排程補充"，
     由排程裡的 Claude 用網路搜尋補上。

資料來源：Yahoo Finance（yfinance）、FinMind（透過 tools/twdata 共用資料層與快取）
輸入：private/portfolio/holdings.toml（持股，只讀）、private/portfolio/watchlist.toml（stocks／us_stocks）
輸出：private/reports/weekly_YYYY-MM-DD.html、private/reports/weekly_latest.html
      stdout：JSON（ensure_ascii=False），給排程裡的 Claude 讀；進度訊息走 stderr
單檔抓取失敗時該檔標 error，不會讓整支程式失敗。

用法：python -X utf8 tools/weekly_catalysts.py
"""
from __future__ import annotations

import html
import json
import sys
import tomllib
from datetime import date, datetime, timedelta
from pathlib import Path

import yfinance as yf

from report_style import CSS
from twdata.api import dividends, stock_info

ROOT = Path(__file__).resolve().parent.parent
HOLDINGS = ROOT / "private" / "portfolio" / "holdings.toml"
WATCHLIST = ROOT / "private" / "portfolio" / "watchlist.toml"
REPORT_DIR = ROOT / "private" / "reports"

HORIZON_DAYS = 14        # 行事曆往後看幾天
REV_PCT = 3.0            # 下年度 EPS 30 天變化 ±3% 視為上修／下修訊號
RATING_DAYS = 7          # 評等變動看近幾天
REVENUE_DEADLINE_DAY = 10  # 台股月營收申報截止日
KNOWN_ETF = {"VOO", "VGT", "DRAM", "SPY", "QQQ"}  # 已知 ETF，省一次 quoteType 查詢


# ---------------------------------------------------------------- 名單
def load_lists() -> tuple[list[tuple[str, str]], list[tuple[str, str, str]]]:
    """回傳 (美股 [(代號, 分類)], 台股 [(代號, 名稱, 分類)])，持股優先"""
    us, tw, seen_us, seen_tw = [], [], set(), set()
    if HOLDINGS.exists():
        h = tomllib.loads(HOLDINGS.read_text(encoding="utf-8"))
        for acc in h["accounts"]:
            for p in acc["positions"]:
                tk = p["ticker"]
                if tk.endswith((".TW", ".TWO")):
                    sid = tk.split(".")[0]
                    if sid not in seen_tw:
                        tw.append((sid, "", "持股"))
                        seen_tw.add(sid)
                elif "." not in tk and tk not in seen_us:  # 排除倫敦掛牌等（CSPX.L）
                    us.append((tk, "持股"))
                    seen_us.add(tk)
    if WATCHLIST.exists():
        w = tomllib.loads(WATCHLIST.read_text(encoding="utf-8"))
        for x in w.get("stocks", []):
            sid = str(x["id"])
            if sid not in seen_tw:
                tw.append((sid, x.get("name", ""), "觀察"))
                seen_tw.add(sid)
        for tk in w.get("us_stocks", []):
            tk = str(tk).upper()
            if tk not in seen_us:
                us.append((tk, "觀察"))
                seen_us.add(tk)
    return us, tw


# ---------------------------------------------------------------- 美股
def _f(v):
    try:
        v = float(v)
        return None if v != v else v  # NaN → None
    except (TypeError, ValueError):
        return None


def _chg(cur, old):
    cur, old = _f(cur), _f(old)
    return (cur / old - 1) * 100 if cur is not None and old not in (None, 0) else None


def _is_etf(t: yf.Ticker, tk: str) -> tuple[bool, str]:
    if tk in KNOWN_ETF:
        return True, tk
    try:
        info = t.info or {}
    except Exception:  # noqa: BLE001
        return False, tk
    return info.get("quoteType") == "ETF", info.get("shortName") or tk


def analyse_us(tk: str, tag: str, today: date) -> dict | None:
    """回傳單檔預估修正結果；ETF 回傳 None。網路錯誤時填 error。"""
    res = {"ticker": tk, "name": tk, "tag": tag, "flags": [], "eps": {}, "ratings": [], "earnings_date": None}
    t = yf.Ticker(tk)
    try:
        etf, res["name"] = _is_etf(t, tk)
        if etf:
            return None
    except Exception as e:  # noqa: BLE001
        res["error"] = f"info：{e}"

    errs = []
    # EPS 預估趨勢
    try:
        tr = t.eps_trend
        for per, key in (("0y", "this_year"), ("+1y", "next_year")):
            if tr is not None and per in tr.index:
                r = tr.loc[per]
                res["eps"][key] = {"current": _f(r.get("current")),
                                   "chg_7d_pct": _chg(r.get("current"), r.get("7daysAgo")),
                                   "chg_30d_pct": _chg(r.get("current"), r.get("30daysAgo"))}
    except Exception as e:  # noqa: BLE001
        errs.append(f"eps_trend：{e}")
    # 上修／下修家數
    try:
        rv = t.eps_revisions
        for per, key in (("0y", "this_year"), ("+1y", "next_year")):
            if rv is not None and per in rv.index:
                r = rv.loc[per]
                res["eps"].setdefault(key, {}).update(
                    up_30d=int(_f(r.get("upLast30days")) or 0), down_30d=int(_f(r.get("downLast30days")) or 0))
    except Exception as e:  # noqa: BLE001
        errs.append(f"eps_revisions：{e}")
    # 近 7 天評等變動
    try:
        ud = t.upgrades_downgrades
        if ud is not None and not ud.empty:
            cutoff = datetime.combine(today - timedelta(days=RATING_DAYS), datetime.min.time())
            idx = ud.index.tz_localize(None) if getattr(ud.index, "tz", None) else ud.index
            for when, row in ud[idx >= cutoff].iterrows():
                res["ratings"].append({"date": str(when)[:10], "firm": row.get("Firm"), "action": row.get("Action"),
                                       "from": row.get("FromGrade") or "", "to": row.get("ToGrade") or "",
                                       "pt": _f(row.get("currentPriceTarget")),
                                       "prior_pt": _f(row.get("priorPriceTarget"))})
    except Exception as e:  # noqa: BLE001
        errs.append(f"upgrades_downgrades：{e}")
    # 目標價
    try:
        pt = t.analyst_price_targets or {}
        res["price"], res["target_mean"] = _f(pt.get("current")), _f(pt.get("mean"))
        res["upside_pct"] = _chg(res["target_mean"], res["price"])
    except Exception as e:  # noqa: BLE001
        errs.append(f"price_targets：{e}")
    # 財報日
    try:
        cal = t.calendar or {}
        for d in cal.get("Earnings Date") or []:
            if isinstance(d, date) and today <= d <= today + timedelta(days=HORIZON_DAYS):
                res["earnings_date"] = d.isoformat()
                break
    except Exception as e:  # noqa: BLE001
        errs.append(f"calendar：{e}")
    if errs:
        res["error"] = "；".join(([res["error"]] if res.get("error") else []) + errs)

    # 訊號
    ny = res["eps"].get("next_year", {})
    c30 = ny.get("chg_30d_pct")
    if c30 is not None and c30 >= REV_PCT:
        res["flags"].append(("good", f"下年度 EPS 預估 30 天上修 {c30:+.1f}%"))
    elif c30 is not None and c30 <= -REV_PCT:
        res["flags"].append(("serious", f"下年度 EPS 預估 30 天下修 {c30:+.1f}%"))
    downs = [r for r in res["ratings"] if str(r["action"]).lower() == "down"]
    if downs:
        txt = "、".join(f"{r['firm']}（{r['from']}→{r['to']}）" for r in downs)
        res["flags"].append(("warning", f"近 {RATING_DAYS} 天降評：{txt}"))
    return res


# ---------------------------------------------------------------- 台股
def tw_events(tw: list[tuple[str, str, str]], today: date) -> tuple[list[dict], list[dict]]:
    """回傳 (除權息事件, 錯誤)"""
    end = today + timedelta(days=HORIZON_DAYS)
    events, errors = [], []
    for sid, name, tag in tw:
        print(f"  {sid} 除權息…", file=sys.stderr)
        if not name:
            try:
                name = stock_info(sid).get("name") or sid
            except Exception:  # noqa: BLE001
                name = sid
        try:
            rows = dividends(sid, years=2).get("dividends", [])
        except Exception as e:  # noqa: BLE001
            errors.append({"sid": sid, "name": name, "error": str(e)})
            continue
        for r in rows:
            d = (r.get("CashExDividendTradingDate") or "")[:10]
            try:
                dd = date.fromisoformat(d)
            except ValueError:
                continue
            if today <= dd <= end:
                parts = []
                if r.get("cash_dividend"):
                    parts.append(f"現金 {r['cash_dividend']:g} 元")
                if r.get("stock_dividend"):
                    parts.append(f"股票 {r['stock_dividend']:g} 元")
                events.append({"date": d, "market": "台股", "type": "除權息", "ticker": sid, "name": name,
                               "tag": tag, "detail": "、".join(parts) or "除權息"})
    return events, errors


def revenue_deadlines(today: date) -> list[dict]:
    end, out = today + timedelta(days=HORIZON_DAYS), []
    y, m = today.year, today.month
    for _ in range(2):
        d = date(y, m, REVENUE_DEADLINE_DAY)
        if today <= d <= end:
            prev = m - 1 or 12
            out.append({"date": d.isoformat(), "market": "台股", "type": "月營收", "ticker": "", "name": "全市場",
                        "tag": "", "detail": f"{prev} 月營收公布截止日"})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# ---------------------------------------------------------------- HTML
def _pp(v) -> str:
    return "—" if v is None else f"{v:+.1f}%"


def _cls(v) -> str:
    return "" if v is None else ("pos" if v > 0 else "neg" if v < 0 else "")


ACTION_ZH = {"up": "升評", "down": "降評", "main": "維持", "init": "首評", "reit": "重申"}
WEEKDAY = "一二三四五六日"

EXTRA_CSS = """
.alert.good .ic{color:var(--good)}
.ev{display:grid;grid-template-columns:76px 1fr;gap:8px;padding:7px 0;border-bottom:1px solid var(--grid);font-size:14px}
.ev:last-child{border:0}.ev .d{color:var(--text2);font-variant-numeric:tabular-nums;font-size:13px}
.note-sm{color:var(--muted);font-size:12px}
"""


def render(today: date, us: list[dict], events: list[dict], errors: list[str]) -> str:
    e = html.escape
    icon = {"good": "▲", "warning": "!", "serious": "▼"}
    out = [f"<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>每週催化劑</title><style>{CSS}{EXTRA_CSS}</style></head><body><main>"]
    out.append(f"<h1>每週預估修正＋催化劑</h1><div class='sub'>產出時間 {datetime.now():%Y-%m-%d %H:%M}"
               f"・行事曆區間 {today:%m/%d}–{today + timedelta(days=HORIZON_DAYS):%m/%d}"
               f"・美股 {len(us)} 檔</div>")

    # 重點：負面訊號（全部）＋持股正面訊號＋本週財報（7 天內）
    out.append("<h2>重點</h2><div class='card'>")
    hl = []
    order = {"serious": 0, "warning": 1, "good": 2}
    flagged = [(a, f) for a in us for f in a["flags"] if f[0] != "good" or a["tag"] == "持股"]
    for a, (lvl, msg) in sorted(flagged, key=lambda x: (order[x[1][0]], x[0]["tag"] != "持股")):
        hl.append(f"<div class='alert {lvl}'><span class='ic' aria-hidden='true'>{icon[lvl]}</span>"
                  f"<span><b>{e(a['ticker'])}</b>（{e(a['tag'])}）{e(msg)}</span></div>")
    week_end = (today + timedelta(days=7)).isoformat()
    for ev in events:
        if ev["type"] == "財報" and ev["date"] <= week_end:
            d = date.fromisoformat(ev["date"])
            hl.append(f"<div class='alert info'><span class='ic' aria-hidden='true'>i</span>"
                      f"<span><b>{e(ev['ticker'])}</b>（{e(ev['tag'])}）{d:%m/%d}（{WEEKDAY[d.weekday()]}）財報</span></div>")
    out.extend(hl or ["<div class='alert info'><span class='ic'>i</span><span>本週無特別訊號</span></div>"])
    out.append("</div>")

    # 預估修正表
    out.append("<h2>預估修正</h2><div class='sub' style='margin-bottom:8px'>EPS 為分析師共識；"
               "變化 % 為現在對 7 天／30 天前；修正家數為近 30 天上修／下修；目標價差距為平均目標價對現價。</div>")
    out.append("<div class='card scroll'><table><tr><th>代號</th><th>本年 EPS</th><th>30天</th>"
               "<th>下年 EPS</th><th>7天</th><th>30天</th><th>上／下修</th><th>目標價差距</th></tr>")
    for a in us:
        ty, ny = a["eps"].get("this_year", {}), a["eps"].get("next_year", {})
        mark = "".join(icon[f[0]] for f in a["flags"])
        err = f"<br><span class='note-sm'>部分資料抓取失敗</span>" if a.get("error") else ""
        cur = lambda v: "—" if v is None else f"{v:.2f}"  # noqa: E731
        ud = f"{ny.get('up_30d', '—')}／{ny.get('down_30d', '—')}" if ny else "—"
        out.append(f"<tr><td><b>{e(a['ticker'])}</b> <span class='tag'>{e(a['tag'])}</span> {mark}{err}</td>"
                   f"<td>{cur(ty.get('current'))}</td><td class='{_cls(ty.get('chg_30d_pct'))}'>{_pp(ty.get('chg_30d_pct'))}</td>"
                   f"<td>{cur(ny.get('current'))}</td><td class='{_cls(ny.get('chg_7d_pct'))}'>{_pp(ny.get('chg_7d_pct'))}</td>"
                   f"<td class='{_cls(ny.get('chg_30d_pct'))}'>{_pp(ny.get('chg_30d_pct'))}</td><td>{ud}</td>"
                   f"<td class='{_cls(a.get('upside_pct'))}'>{_pp(a.get('upside_pct'))}</td></tr>")
    out.append("</table></div>")

    # 只列升評／降評／首評；「維持、重申」多半只是調目標價，只計數（JSON 保留全部）
    all_rated = [(a, r) for a in us for r in a["ratings"]]
    rated = [(a, r) for a, r in all_rated if str(r["action"]).lower() in ("up", "down", "init")]
    out.append(f"<h2>近 {RATING_DAYS} 天評等變動（{len(rated)} 筆）</h2><div class='card'>")
    if not rated:
        out.append("<div class='sub'>沒有升評、降評或首評</div>")
    for a, r in sorted(rated, key=lambda x: x[1]["date"], reverse=True):
        act = ACTION_ZH.get(str(r["action"]).lower(), r["action"])
        grade = f"{r['from']} → {r['to']}" if r["from"] and r["from"] != r["to"] else r["to"]
        pt = f"・目標價 {r['prior_pt']:g} → {r['pt']:g}" if r.get("pt") and r.get("prior_pt") else (
            f"・目標價 {r['pt']:g}" if r.get("pt") else "")
        lvl = "warning" if act == "降評" else "info"
        out.append(f"<div class='alert {lvl}'><span class='ic'>{'!' if lvl == 'warning' else 'i'}</span>"
                   f"<span><b>{e(a['ticker'])}</b> {e(r['date'][5:])} {e(str(r['firm']))} {e(str(act))}：{e(grade)}{e(pt)}</span></div>")
    if len(all_rated) > len(rated):
        out.append(f"<div class='note-sm' style='margin-top:6px'>另有 {len(all_rated) - len(rated)} 筆維持／重申評等"
                   f"（多為調整目標價），未逐筆列出</div>")
    out.append("</div>")

    # 行事曆
    out.append(f"<h2>未來 {HORIZON_DAYS} 天行事曆（{len(events)} 件）</h2><div class='card'>")
    if not events:
        out.append("<div class='sub'>區間內沒有已知事件</div>")
    for ev in events:
        d = date.fromisoformat(ev["date"])
        who = f"<b>{e(ev['ticker'])}</b> {e(ev['name'])}" if ev["ticker"] else f"<b>{e(ev['name'])}</b>"
        tag = f" <span class='tag'>{e(ev['tag'])}</span>" if ev["tag"] else ""
        out.append(f"<div class='ev'><span class='d'>{d:%m/%d}（{WEEKDAY[d.weekday()]}）</span>"
                   f"<span>{e(ev['market'])}・{e(ev['type'])}：{who}{tag} <span class='sub'>{e(ev['detail'])}</span></span></div>")
    out.append("</div>")

    # 總經
    out.append("<h2>總經事件</h2><div class='card'><div class='sub'>由排程補充</div></div>")

    if errors:
        out.append("<h2>抓取失敗</h2><div class='card'><ul>"
                   + "".join(f"<li class='note-sm'>{e(x)}</li>" for x in errors) + "</ul></div>")
    out.append("<p class='note'>資料來源：Yahoo Finance（yfinance：EPS 預估趨勢、修正家數、評等變動、目標價、財報日）、"
               "FinMind（台股除權息，透過 twdata）。財報日可能為預估值，以公司公告為準；"
               "台股除權息僅列已公告且資料源已收錄者。本報告為個人研究用途，不構成投資建議。</p></main></body></html>")
    return "".join(out)


# ---------------------------------------------------------------- main
def main() -> None:
    today = date.today()
    us_list, tw_list = load_lists()

    us, errors = [], []
    for tk, tag in us_list:
        print(f"  {tk} 預估修正…", file=sys.stderr)
        try:
            a = analyse_us(tk, tag, today)
        except Exception as ex:  # noqa: BLE001
            a = {"ticker": tk, "name": tk, "tag": tag, "flags": [], "eps": {}, "ratings": [],
                 "earnings_date": None, "error": str(ex)}
        if a is None:
            continue
        if a.get("error"):
            errors.append(f"{tk}：{a['error']}")
        us.append(a)

    events = [{"date": a["earnings_date"], "market": "美股", "type": "財報", "ticker": a["ticker"],
               "name": a["name"] if a["name"] != a["ticker"] else "", "tag": a["tag"], "detail": "財報（日期可能為預估）"}
              for a in us if a.get("earnings_date")]
    tw_ev, tw_err = tw_events(tw_list, today)
    errors += [f"{x['sid']} {x['name']}：{x['error']}" for x in tw_err]
    events = sorted(events + tw_ev + revenue_deadlines(today), key=lambda x: (x["date"], x["market"], x["ticker"]))

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    page = render(today, us, events, errors)
    path = REPORT_DIR / f"weekly_{today.isoformat()}.html"
    path.write_text(page, encoding="utf-8")
    (REPORT_DIR / "weekly_latest.html").write_text(page, encoding="utf-8")

    def sig(a):
        return [{"level": lvl, "message": msg} for lvl, msg in a["flags"]]

    result = {
        "date": today.isoformat(),
        "horizon_days": HORIZON_DAYS,
        "signals": [{"ticker": a["ticker"], "tag": a["tag"], **s} for a in us for s in sig(a)],
        "estimates": [{k: a.get(k) for k in ("ticker", "name", "tag", "eps", "ratings", "price", "target_mean",
                                             "upside_pct", "earnings_date", "error")} | {"signals": sig(a)} for a in us],
        "calendar": events,
        "macro": "由排程補充",
        "errors": errors,
        "report": str(path),
        "report_latest": str(REPORT_DIR / "weekly_latest.html"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
