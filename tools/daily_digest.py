"""個人化每日摘要：把雲端產出的盤前／盤後簡報，對照你自己的持股，濃縮成幾行。

  morning：美股收盤後（台北早上）——美股持股昨夜漲跌、對台股的影響（TSM ADR）、近期財報
  evening：美股盤前（台北晚上）——只在「例外」時才有內容：持股盤前漲跌 ≥ 門檻、或今明有財報

輸入：private/portfolio/holdings.toml
輸出：stdout 印出 JSON（給排程任務的 Claude 讀）；morning 另外產出 private/reports/digest_latest.html

用法：python -X utf8 tools/daily_digest.py morning|evening
"""
from __future__ import annotations

import html
import json
import sys
import tomllib
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

from report_style import CSS

ROOT = Path(__file__).resolve().parent.parent
HOLDINGS = ROOT / "private" / "portfolio" / "holdings.toml"
REPORT_DIR = ROOT / "private" / "reports"
SITE = "https://sophieku840814.github.io/US_stock_daily"
CARDS = Path(r"C:\Users\Andy\Documents\obsidian\obsidian\00 投資系統\交易")

PREMARKET_ALERT = 3.0      # 盤前漲跌 ≥ 3% 才通知
MIN_WEIGHT = 0.003         # 低於美股部位 0.3% 的小部位不單獨通知
EARNINGS_DAYS = 3          # 未來幾天內的財報要提醒


def load_us_positions() -> dict[str, float]:
    """回傳 {代號: 股數}，只取美股／海外（非 .TW）部位，跨帳戶合併"""
    h = tomllib.loads(HOLDINGS.read_text(encoding="utf-8"))
    qty: dict[str, float] = defaultdict(float)
    for acc in h["accounts"]:
        for p in acc["positions"]:
            if not p["ticker"].endswith((".TW", ".TWO")):
                qty[p["ticker"]] += p["qty"]
    return dict(qty)


def daily_change(ticker: str) -> dict | None:
    df = yf.Ticker(ticker).history(period="7d", interval="1d", auto_adjust=False)
    df = df.dropna(subset=["Close"])
    if len(df) < 2:
        return None
    last, prev = df.iloc[-1], df.iloc[-2]
    return {"date": df.index[-1].date().isoformat(), "close": float(last["Close"]),
            "prev": float(prev["Close"]), "pct": (float(last["Close"]) / float(prev["Close"]) - 1) * 100}


def premarket_change(ticker: str) -> dict | None:
    """只認「今天（美東）09:30 以前」的盤前成交；週末、假日或還沒開始盤前就回傳 None"""
    t = yf.Ticker(ticker)
    today_et = datetime.now(ZoneInfo("America/New_York")).date()
    df = t.history(period="5d", interval="5m", prepost=True).dropna(subset=["Close"])
    if df.empty:
        return None
    idx = df.index.tz_convert("America/New_York")
    pre = df[(idx.date == today_et) & ((idx.hour * 60 + idx.minute) < 9 * 60 + 30)]
    daily = t.history(period="7d", interval="1d", auto_adjust=False).dropna(subset=["Close"])
    daily = daily[daily.index.tz_convert("America/New_York").date < today_et]
    if pre.empty or daily.empty:
        return None
    px, prev = float(pre["Close"].iloc[-1]), float(daily["Close"].iloc[-1])
    return {"time": pre.index[-1].tz_convert("America/New_York").strftime("%H:%M ET"),
            "price": px, "prev": prev, "pct": (px / prev - 1) * 100}


def next_earnings(ticker: str) -> str | None:
    try:
        cal = yf.Ticker(ticker).calendar or {}
    except Exception:  # noqa: BLE001
        return None
    dates = cal.get("Earnings Date") or []
    today = date.today()
    for d in dates:
        d = d if isinstance(d, date) else None
        if d and today <= d <= today + timedelta(days=EARNINGS_DAYS):
            return d.isoformat()
    return None


def usd_twd() -> float:
    return float(yf.Ticker("TWD=X").fast_info.last_price)


# ---------------------------------------------------------------- 交易卡（停損／目標／時間停損）
def _frontmatter(text: str) -> dict:
    if not text.startswith("---\n"):
        return {}
    fm = {}
    for line in text.split("\n---\n", 1)[0].split("\n")[1:]:
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm


def trade_card_alerts(price_of) -> list[dict]:
    """讀 Obsidian「00 投資系統/交易」裡 status＝持有中 的交易卡，用 price_of(ticker) 取價比對"""
    alerts = []
    if not CARDS.exists():
        return alerts
    for f in sorted(CARDS.glob("*.md")):
        fm = _frontmatter(f.read_text(encoding="utf-8"))
        if fm.get("status") != "持有中" or not fm.get("ticker"):
            continue
        tk, card = fm["ticker"], f.stem
        try:
            px = price_of(tk)
        except Exception:  # noqa: BLE001
            px = None
        stop = float(fm["stop"]) if fm.get("stop") else None
        target = float(fm["target"]) if fm.get("target") else None
        if px is not None and stop and px <= stop:
            alerts.append({"card": card, "ticker": tk, "type": "停損", "price": round(px, 2), "level": stop})
        if px is not None and target and px >= target:
            alerts.append({"card": card, "ticker": tk, "type": "目標", "price": round(px, 2), "level": target})
        ts = fm.get("time_stop")
        if ts:
            try:
                days = (date.fromisoformat(ts) - date.today()).days
            except ValueError:
                continue
            if days <= 0:
                alerts.append({"card": card, "ticker": tk, "type": "時間停損到期", "date": ts})
            elif days <= 3:
                alerts.append({"card": card, "ticker": tk, "type": f"時間停損 {days} 天後到期", "date": ts})
    return alerts


# ---------------------------------------------------------------- modes
def morning() -> dict:
    pos = load_us_positions()
    rows, total_usd, pnl_usd = [], 0.0, 0.0
    for tk, q in pos.items():
        c = daily_change(tk)
        if not c:
            continue
        val = q * c["close"]
        total_usd += val
        pnl_usd += q * (c["close"] - c["prev"])
        rows.append({"ticker": tk, "pct": c["pct"], "value_usd": val, "date": c["date"],
                     "earnings": next_earnings(tk)})
    for r in rows:
        r["weight"] = r["value_usd"] / total_usd if total_usd else 0
    rows.sort(key=lambda r: -abs(r["pct"]) * (r["weight"] >= MIN_WEIGHT))
    trade_date = max((r["date"] for r in rows), default=None)
    stale = trade_date is not None and (date.today() - date.fromisoformat(trade_date)).days > 3
    tsm = daily_change("TSM")
    idx = {name: daily_change(tk) for name, tk in (("S&P 500", "^GSPC"), ("Nasdaq", "^IXIC"), ("費半", "^SOX"))}
    rate = usd_twd()
    return {
        "mode": "morning", "trade_date": trade_date, "market_closed_or_stale": stale,
        "indices": {k: round(v["pct"], 2) for k, v in idx.items() if v},
        "us_portfolio_pnl_twd": round(pnl_usd * rate), "us_portfolio_pct": round(pnl_usd / (total_usd - pnl_usd) * 100, 2) if total_usd else None,
        "movers": [{"ticker": r["ticker"], "pct": round(r["pct"], 2), "weight_pct": round(r["weight"] * 100, 1)}
                   for r in rows if r["weight"] >= MIN_WEIGHT][:5],
        "tsm_adr_pct": round(tsm["pct"], 2) if tsm else None,
        "earnings_soon": [{"ticker": r["ticker"], "date": r["earnings"]} for r in rows if r["earnings"]],
        "full_report": f"{SITE}/reports/latest_aftermarket.html",
        "trade_card_alerts": trade_card_alerts(lambda tk: (daily_change(tk) or {}).get("close")),
    }


def evening() -> dict:
    pos = load_us_positions()
    vals, alerts, earnings = {}, [], []
    for tk, q in pos.items():
        p = premarket_change(tk)
        if p:
            vals[tk] = (q * p["prev"], p)
        e = next_earnings(tk)
        if e and e <= (date.today() + timedelta(days=1)).isoformat():
            earnings.append({"ticker": tk, "date": e})
    total = sum(v for v, _ in vals.values()) or 1
    for tk, (v, p) in vals.items():
        if abs(p["pct"]) >= PREMARKET_ALERT and v / total >= MIN_WEIGHT:
            alerts.append({"ticker": tk, "pct": round(p["pct"], 2), "weight_pct": round(v / total * 100, 1), "as_of": p["time"]})
    alerts.sort(key=lambda a: -abs(a["pct"]))
    cards = trade_card_alerts(lambda tk: vals[tk][1]["price"] if tk in vals else None)
    return {"mode": "evening", "notify": bool(alerts or earnings or cards), "premarket_alerts": alerts,
            "trade_card_alerts": cards,
            "earnings_today_or_tomorrow": earnings, "threshold_pct": PREMARKET_ALERT,
            "full_report": f"{SITE}/reports/latest_premarket.html"}


# ---------------------------------------------------------------- HTML（morning）
def render_morning(d: dict) -> str:
    e = html.escape

    def pct(v):
        return "—" if v is None else f"<span class='{'pos' if v > 0 else 'neg' if v < 0 else ''}'>{v:+.2f}%</span>"

    idx = "".join(f"<div class='card tile'><div class='k'>{e(k)}</div><div class='v'>{pct(v)}</div></div>"
                  for k, v in d["indices"].items())
    movers = "".join(f"<tr><td>{e(m['ticker'])}</td><td>{pct(m['pct'])}</td><td>{m['weight_pct']}%</td></tr>"
                     for m in d["movers"])
    earn = "".join(f"<li>{e(x['ticker'])}：{e(x['date'])}</li>" for x in d["earnings_soon"]) or "<li>近 3 天沒有持股財報</li>"
    return (f"<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'><title>每日摘要</title>"
            f"<style>{CSS}</style></head><body><main><h1>每日摘要</h1>"
            f"<div class='sub'>美股交易日 {e(str(d['trade_date']))}・產出 {datetime.now():%Y-%m-%d %H:%M}</div>"
            f"<h2>你的美股部位</h2><div class='tiles'>"
            f"<div class='card tile'><div class='k'>昨夜損益</div><div class='v'>NT${d['us_portfolio_pnl_twd']:+,}</div>"
            f"<div class='s'>{pct(d['us_portfolio_pct'])}</div></div>"
            f"<div class='card tile'><div class='k'>TSM ADR（台積電參考）</div><div class='v'>{pct(d['tsm_adr_pct'])}</div></div></div>"
            f"<h2>大盤</h2><div class='tiles'>{idx}</div>"
            f"<h2>持股漲跌前五</h2><div class='card'><table><tr><th>代號</th><th>漲跌</th><th>佔美股部位</th></tr>{movers}</table></div>"
            f"<h2>近期財報</h2><div class='card'><ul>{earn}</ul></div>"
            f"<p><a href='{e(d['full_report'])}'>看完整盤後簡報 →</a></p>"
            f"<p class='note'>資料來源：Yahoo Finance。個人研究用途，不構成投資建議。</p></main></body></html>")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "morning"
    if mode == "morning":
        d = morning()
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        (REPORT_DIR / "digest_latest.html").write_text(render_morning(d), encoding="utf-8")
        d["digest_file"] = str(REPORT_DIR / "digest_latest.html")
    elif mode == "evening":
        d = evening()
    else:
        sys.exit("mode must be morning or evening")
    print(json.dumps(d, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
