"""非 AI 股票池篩選：依投資政策書「AI 主題高於目標時，新資金優先投入其他主題」，找台美股的非 AI 候選。

漏斗：
  1. 全市場初篩（基本面）
     - 美股：Yahoo 篩選器，市值 ≥ 100 億美元、近 12 個月 EPS 成長 ≥ 10%、排除科技類股
     - 台股：證交所／櫃買資料（twdata），排除電子／半導體等 AI 供應鏈產業，營收年增與累計年增 ≥ 10%、本益比 ≤ 25、有成交量
  2. 排除 AI 相關公司（手動清單：雲端巨頭、AI 電力／散熱等）與既有持股
  3. 計分（各項以排名百分位計，越高越好）
     - 基本面：美股＝下年度 EPS 預估 30 天修正；台股＝累計營收年增
     - 趨勢：股價在 200 日均線之上、50 日均線在 200 日之上
     - 相對強弱：近 3 個月報酬 vs 大盤（SPY／0050）
     - 估值：前瞻（或目前）本益比越低越好
輸出：Obsidian「00 投資系統/股票池/非 AI 股票池 YYYY-MM.md」

用法：python -X utf8 tools/non_ai_pool.py [--top 15]
"""
from __future__ import annotations

import argparse
import sys
import tomllib
from datetime import date
from pathlib import Path

import yfinance as yf
from yfinance import EquityQuery as Q

from twdata.api import market_screen

ROOT = Path(__file__).resolve().parent.parent
HOLDINGS = ROOT / "private" / "portfolio" / "holdings.toml"
OUT_DIR = Path(r"C:\Users\Andy\Documents\obsidian\obsidian\00 投資系統\股票池")

US_SECTORS = ["Healthcare", "Financial Services", "Consumer Defensive", "Industrials", "Utilities",
              "Consumer Cyclical", "Communication Services", "Energy", "Basic Materials", "Real Estate"]
# 屬於 AI 資本支出循環的公司：雲端／AI 平台、AI 電力與資料中心設備
US_AI_EXCLUDE = {"GOOGL", "GOOG", "AMZN", "META", "MSFT", "AAPL", "TSLA", "NFLX",
                 "VRT", "ETN", "GEV", "PWR", "CEG", "VST", "NRG", "TLN", "OKLO", "SMR", "MOD", "JCI",
                 "EQIX", "DLR", "IRM", "CCJ", "BE", "FIX", "EME", "STRL", "APP"}
US_EXCHANGES = {"NYQ", "NMS", "NGM", "NCM", "ASE", "PCX", "BTS"}  # 只收紐約證交所／Nasdaq，排除場外 OTC
MAX_PER_GROUP = 3  # 清單裡同一產業最多幾檔，避免全被同一個循環佔走
TW_AI_INDUSTRIES = ("半導體業", "電子零組件業", "電腦及週邊設備業", "光電業", "通信網路業",
                    "電子通路業", "其他電子業", "資訊服務業", "數位雲端")


def held_tickers() -> set[str]:
    h = tomllib.loads(HOLDINGS.read_text(encoding="utf-8"))
    out = set()
    for a in h["accounts"]:
        for p in a["positions"]:
            out.add(p["ticker"].split(".")[0])
    return out


def pct_rank(values: list[float | None]) -> list[float]:
    idx = [i for i, v in enumerate(values) if v is not None]
    order = sorted(idx, key=lambda i: values[i])
    out = [0.5] * len(values)
    for r, i in enumerate(order):
        out[i] = r / max(1, len(order) - 1)
    return out


def trend_and_rs(symbol: str, bench_ret: float) -> dict:
    h = yf.Ticker(symbol).history(period="1y", auto_adjust=True)["Close"].dropna()
    if len(h) < 200:
        return {}
    px, ma50, ma200 = float(h.iloc[-1]), float(h.tail(50).mean()), float(h.tail(200).mean())
    ret3m = float(h.iloc[-1] / h.iloc[-63] - 1)
    return {"price": px, "above200": px > ma200, "ma50_gt_200": ma50 > ma200,
            "ret3m": ret3m, "rs3m": ret3m - bench_ret, "off_high": px / float(h.max()) - 1}


def bench(symbol: str) -> float:
    h = yf.Ticker(symbol).history(period="6mo", auto_adjust=True)["Close"].dropna()
    return float(h.iloc[-1] / h.iloc[-63] - 1)


def score(rows: list[dict], fund_key: str, pe_key: str) -> None:
    f = pct_rank([r.get(fund_key) for r in rows])
    rs = pct_rank([r.get("rs3m") for r in rows])
    pe = pct_rank([(-r[pe_key]) if r.get(pe_key) and r[pe_key] > 0 else None for r in rows])
    for r, a, b, c in zip(rows, f, rs, pe):
        trend = (0.5 if r.get("above200") else 0) + (0.5 if r.get("ma50_gt_200") else 0)
        r["score"] = round(100 * (0.35 * a + 0.25 * trend + 0.25 * b + 0.15 * c))


# ---------------------------------------------------------------- 美股
def us_candidates(exclude: set[str], limit: int = 80) -> list[dict]:
    q = Q("and", [Q("eq", ["region", "us"]), Q("gt", ["intradaymarketcap", 10e9]),
                  Q("is-in", ["sector", *US_SECTORS]), Q("gt", ["epsgrowth.lasttwelvemonths", 10])])
    quotes = yf.screen(q, size=250, sortField="intradaymarketcap", sortAsc=False).get("quotes", [])
    rows = [x for x in quotes if x.get("quoteType") == "EQUITY" and x["symbol"] not in exclude
            and x.get("exchange") in US_EXCHANGES][:limit]
    spy = bench("SPY")
    out = []
    for x in rows:
        sym = x["symbol"]
        print(f"  US {sym}", file=sys.stderr)
        try:
            t = yf.Ticker(sym)
            info = t.info
            et = t.eps_trend
            rev = None
            if et is not None and "+1y" in et.index and et.loc["+1y", "30daysAgo"]:
                rev = float(et.loc["+1y", "current"] / et.loc["+1y", "30daysAgo"] - 1)
            tr = trend_and_rs(sym, spy)
        except Exception as e:  # noqa: BLE001
            print(f"    ! {sym}: {e}", file=sys.stderr)
            continue
        if not tr:
            continue
        out.append({"ticker": sym, "name": x.get("shortName", sym), "sector": info.get("sector"),
                    "industry": info.get("industry"), "mcap_b": (x.get("marketCap") or 0) / 1e9,
                    "fwd_pe": x.get("forwardPE"), "eps_rev_30d": rev,
                    "div_yield": x.get("dividendYield"), **tr})
    score(out, "eps_rev_30d", "fwd_pe")
    return sorted(out, key=lambda r: -r["score"])


# ---------------------------------------------------------------- 台股
def tw_candidates(exclude: set[str], limit: int = 50) -> list[dict]:
    res = market_screen(min_revenue_yoy=10, min_ytd_revenue_yoy=10, max_pe=25, min_volume_lots=300,
                        sort_by="ytd_revenue_yoy_pct", limit=400)
    rows = [r for r in res["results"]
            if r.get("industry") and not any(k in r["industry"] for k in TW_AI_INDUSTRIES)
            and not r["stock_id"].startswith("00") and r["stock_id"] not in exclude][:limit]
    tw50 = bench("0050.TW")
    out = []
    for r in rows:
        sym = f"{r['stock_id']}.{'TW' if r['market'] == 'twse' else 'TWO'}"
        print(f"  TW {sym}", file=sys.stderr)
        try:
            tr = trend_and_rs(sym, tw50)
        except Exception as e:  # noqa: BLE001
            print(f"    ! {sym}: {e}", file=sys.stderr)
            continue
        if not tr:
            continue
        industry = "金融保險業" if r["industry"] == "金融業" else r["industry"]
        ytd = r.get("ytd_revenue_yoy_pct")
        out.append({"ticker": r["stock_id"], "name": r["name"], "industry": industry,
                    "ytd_capped": min(ytd, 100) if ytd is not None else None,  # 計分用：避免營建交屋、證券成交量等極端值壟斷
                    "pe": r.get("pe"), "pb": r.get("pb"), "div_yield": r.get("dividend_yield_pct"),
                    "rev_yoy": r.get("revenue_yoy_pct"), "ytd_rev_yoy": r.get("ytd_revenue_yoy_pct"),
                    "revenue_month": r.get("revenue_month"), **tr})
    score(out, "ytd_capped", "pe")
    return sorted(out, key=lambda r: -r["score"])


# ---------------------------------------------------------------- 輸出
def _p(v, signed=True):
    return "—" if v is None else (f"{v:+.0%}" if signed else f"{v:.0%}")


def trend_mark(r):
    return ("✅" if r.get("above200") else "❌") + ("✅" if r.get("ma50_gt_200") else "❌")


def diversify(rows: list[dict], key: str, top: int) -> list[dict]:
    """依分數挑前 top 名，但同一產業最多 MAX_PER_GROUP 檔"""
    out, cnt = [], {}
    for r in rows:
        g = r.get(key) or "其他"
        if cnt.get(g, 0) >= MAX_PER_GROUP:
            continue
        cnt[g] = cnt.get(g, 0) + 1
        out.append(r)
        if len(out) >= top:
            break
    return out


def render(us: list[dict], tw: list[dict], top: int) -> str:
    today = date.today()
    us_all, tw_all = us, tw
    us, tw = diversify(us, "sector", top), diversify(tw, "industry", top)
    L = ["---\ntags:\n  - 股票池\n  - 非AI\n"
         f"date: {today.isoformat()}\n---",
         f"# 非 AI 股票池 {today:%Y-%m}",
         "> 由 `tools/non_ai_pool.py` 產生。依據 [[00 投資系統/投資政策書]]：AI／半導體主題高於 60% 目標時，新資金優先投入其他主題。",
         "> **這是基本面＋趨勢的初篩，不是買進建議。** 有興趣的先做「快速評估」，進場前一定要先填交易卡。",
         "> 分數＝基本面 35%＋趨勢 25%＋相對強弱 25%＋估值 15%（各項以排名百分位計）。趨勢欄：第一個符號＝股價在 200 日線上，第二個＝50 日線在 200 日線上。\n",
         f"## 美股（前 {len(us)} 名，共評分 {len(us_all)} 檔；同一類股最多 {MAX_PER_GROUP} 檔）",
         "條件：市值 ≥ 100 億美元、近 12 個月 EPS 成長 ≥ 10%、非科技類股，排除雲端巨頭與 AI 電力／資料中心設備股、既有持股。\n",
         "| # | 代號 | 公司 | 產業 | 分數 | 前瞻本益比 | EPS 預估 30 天 | 趨勢 | 3 個月 vs SPY | 離高點 |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(us, 1):
        L.append(f"| {i} | {r['ticker']} | {r['name']} | {r.get('industry') or r.get('sector') or ''} | **{r['score']}** | "
                 f"{r['fwd_pe']:.1f} | {_p(r['eps_rev_30d'])} | {trend_mark(r)} | {_p(r['rs3m'])} | {_p(r['off_high'])} |"
                 if r.get("fwd_pe") else
                 f"| {i} | {r['ticker']} | {r['name']} | {r.get('industry') or ''} | **{r['score']}** | — | {_p(r['eps_rev_30d'])} | "
                 f"{trend_mark(r)} | {_p(r['rs3m'])} | {_p(r['off_high'])} |")
    L += ["", f"## 台股（前 {len(tw)} 名，共評分 {len(tw_all)} 檔；同一產業最多 {MAX_PER_GROUP} 檔）",
          "條件：排除半導體、電子零組件、電腦週邊、光電、網通等 AI 供應鏈產業；月營收年增與累計年增 ≥ 10%、本益比 ≤ 25、日成交量 ≥ 300 張，排除既有持股。\n",
          "| # | 代號 | 公司 | 產業 | 分數 | 本益比 | 殖利率 | 單月營收年增 | 累計年增 | 趨勢 | 3 個月 vs 0050 |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(tw, 1):
        L.append(f"| {i} | {r['ticker']} | {r['name']} | {r['industry']} | **{r['score']}** | {r['pe'] or '—'} | "
                 f"{r['div_yield'] if r['div_yield'] is not None else '—'}% | {r['rev_yoy']:+.0f}% | {r['ytd_rev_yoy']:+.0f}% | "
                 f"{trend_mark(r)} | {_p(r['rs3m'])} |")
    L += ["", "## 下一步",
          "- 挑 3–5 檔有興趣的，跟 Claude 說「快速評估 XXX」（15 分鐘版：估值、論點、風險）",
          "- 通過評估的加入觀察名單（`private/portfolio/watchlist.toml`），週報與月營收掃描會自動追蹤",
          "- 12/21 過渡期檢查後若有衛星額度，進場前先填交易卡（風險報酬比 ≥ 2:1）",
          "", "## 方法限制",
          "- 美股的 AI 排除清單是手動維護的，可能漏掉間接受惠 AI 的公司（例如部分工業、公用事業）",
          "- 台股用產業分類排除電子業，部分非電子產業（例如電機機械的重電股）也可能是 AI 電力題材",
          "- 台股證券股的營收跟大盤成交量連動（間接受惠 AI 多頭）；營建股營收依交屋時點認列，單月年增波動很大，要看全年",
          "- 能源股的 EPS 成長跟油價循環有關，不是長期結構性成長",
          "- 資料來源：Yahoo Finance（yfinance）、證交所／櫃買中心 OpenAPI（twdata）。分數只是排序工具。",
          "", "> 個人研究用途，不構成投資建議。"]
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=15)
    a = ap.parse_args()
    held = held_tickers()
    print("美股篩選…", file=sys.stderr)
    us = us_candidates(US_AI_EXCLUDE | held)
    print("台股篩選…", file=sys.stderr)
    tw = tw_candidates(held)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"非 AI 股票池 {date.today():%Y-%m}.md"
    out.write_text(render(us, tw, a.top), encoding="utf-8")
    print(f"美股評分 {len(us)} 檔、台股評分 {len(tw)} 檔")
    print("美股前 10：" + "、".join(f"{r['ticker']}({r['score']})" for r in us[:10]))
    print("台股前 10：" + "、".join(f"{r['ticker']} {r['name']}({r['score']})" for r in tw[:10]))
    print(f"輸出：{out}")


if __name__ == "__main__":
    main()
