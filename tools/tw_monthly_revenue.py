"""台股月營收掃描：持股／觀察名單逐檔檢查 + 全市場篩選股票池候選。

資料來源（皆透過 tools/twdata 共用資料層與快取）：
  - 全市場最新一個月：TWSE OpenAPI（上市）＋ TPEx OpenAPI（上櫃），每月 10 日申報截止後彙整
  - 個股歷史（最近 25 個月）：twdata.monthly_revenue → FinMind（公司申報後幾小時內就有）
輸入：private/portfolio/holdings.toml（台股持股）、private/portfolio/watchlist.toml（觀察名單）
輸出：private/reports/tw_revenue_YYYY-MM.html、private/reports/tw_revenue_latest.html
      data/tw_revenue/YYYY-MM.json（全市場月營收快照，公開資料）

用法：python -X utf8 tools/tw_monthly_revenue.py
"""
from __future__ import annotations

import html
import json
import sys
import tomllib
from datetime import datetime
from pathlib import Path

from report_style import CSS
from twdata.api import monthly_revenue
from twdata.sources import tpex, twse

ROOT = Path(__file__).resolve().parent.parent
HOLDINGS = ROOT / "private" / "portfolio" / "holdings.toml"
WATCHLIST = ROOT / "private" / "portfolio" / "watchlist.toml"
REPORT_DIR = ROOT / "private" / "reports"
SNAP_DIR = ROOT / "data" / "tw_revenue"

# 篩選與警示門檻
SCREEN_MIN_REVENUE_K = 1_000_000  # 當月營收 ≥ 10 億（單位：仟元），排除太小的公司
SCREEN_MIN_YOY = 30.0            # 當月年增 ≥ 30%
SCREEN_MIN_CUM_YOY = 20.0        # 累計年增 ≥ 20%
SCREEN_EXCLUDE_INDUSTRY = ("金融保險業",)
ACCEL_PP = 5.0                   # 近 3 月平均年增比前 3 月高 5 個百分點 → 加速
MOM_BIG = 20.0                   # 月增 ±20% 視為大幅變動


def _f(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 全市場
def fetch_market() -> tuple[str, list[dict]]:
    rows = []
    for src, market in ((twse("opendata/t187ap05_L"), "上市"), (tpex("mopsfin_t187ap05_O"), "上櫃")):
        for x in src:
            rows.append({
                "sid": x["公司代號"], "name": x["公司名稱"], "industry": x["產業別"], "market": market,
                "ym": x["資料年月"],
                "rev": _f(x["營業收入-當月營收"]), "rev_prev": _f(x["營業收入-上月營收"]),
                "mom": _f(x["營業收入-上月比較增減(%)"]), "yoy": _f(x["營業收入-去年同月增減(%)"]),
                "cum_yoy": _f(x["累計營業收入-前期比較增減(%)"]),
                "note": (x.get("備註") or "").strip(" -"),
            })
    # 民國年月 → 西元
    ym = max(r["ym"] for r in rows)
    period = f"{int(ym[:-2]) + 1911}-{ym[-2:]}"
    return period, [r for r in rows if r["ym"] == ym]


def screen(market: list[dict], exclude: set[str]) -> list[dict]:
    out = [r for r in market
           if r["rev"] and r["rev"] >= SCREEN_MIN_REVENUE_K
           and (r["yoy"] or -999) >= SCREEN_MIN_YOY
           and (r["cum_yoy"] or -999) >= SCREEN_MIN_CUM_YOY
           and r["yoy"] >= r["cum_yoy"]          # 當月不輸今年以來，成長沒有在放緩
           and r["industry"] not in SCREEN_EXCLUDE_INDUSTRY
           and r["sid"] not in exclude]
    return sorted(out, key=lambda r: -r["cum_yoy"])


# ---------------------------------------------------------------- 個股歷史
def fetch_history(sid: str) -> list[dict]:
    """近 25 個月營收（元），透過 twdata（FinMind，含 6 小時快取）"""
    try:
        d = monthly_revenue(sid, months=25)
    except Exception as e:  # noqa: BLE001
        print(f"  ! {sid} 歷史月營收抓取失敗：{e}", file=sys.stderr)
        return []
    out = []
    for x in d.get("months", []):
        y, m = x["month"].split("-")
        if x.get("revenue_mn") is not None:
            out.append({"y": int(y), "m": int(m), "rev": x["revenue_mn"] * 1e6})
    return out


def analyse(sid: str, name: str, hist: list[dict], tag: str, industry: str = "") -> dict:
    by = {(h["y"], h["m"]): h["rev"] for h in hist}
    for h in hist:
        prev = by.get((h["y"] - 1, h["m"]))
        h["yoy"] = (h["rev"] / prev - 1) * 100 if prev else None
        pm = (h["y"], h["m"] - 1) if h["m"] > 1 else (h["y"] - 1, 12)
        h["mom"] = (h["rev"] / by[pm] - 1) * 100 if by.get(pm) else None
    res = {"sid": sid, "name": name, "tag": tag, "hist": hist[-13:], "flags": []}
    if not hist:
        res["flags"].append(("info", "抓不到歷史資料"))
        return res
    last = hist[-1]
    res.update(period=f"{last['y']}-{last['m']:02d}", rev=last["rev"], yoy=last["yoy"], mom=last["mom"])
    ytd = [h for h in hist if h["y"] == last["y"]]
    ytd_prev = sum(by.get((last["y"] - 1, h["m"]), 0) for h in ytd)
    res["cum_yoy"] = (sum(h["rev"] for h in ytd) / ytd_prev - 1) * 100 if ytd_prev else None
    if industry in SCREEN_EXCLUDE_INDUSTRY:
        # 金控的「營收」含投資損益，月與月之間波動大，不適合用成長訊號判讀
        res["flags"].append(("info", "金融股月營收波動大、參考性低，不做成長判讀；請看月自結獲利"))
        return res

    yoys = [h["yoy"] for h in hist if h["yoy"] is not None]
    if len(hist) >= 13 and last["rev"] >= max(h["rev"] for h in hist[-25:]):
        res["flags"].append(("good", "營收創近兩年新高"))
    if len(yoys) >= 6:
        recent, before = sum(yoys[-3:]) / 3, sum(yoys[-6:-3]) / 3
        res["accel"] = recent - before
        if recent - before >= ACCEL_PP:
            res["flags"].append(("good", f"年增率加速（近 3 月平均 {recent:.0f}%，前 3 月 {before:.0f}%）"))
        elif before - recent >= ACCEL_PP:
            res["flags"].append(("warning", f"年增率放緩（近 3 月平均 {recent:.0f}%，前 3 月 {before:.0f}%）"))
    if len(yoys) >= 2 and yoys[-1] < 0 and yoys[-2] < 0:
        res["flags"].append(("serious", "連續 2 個月年減，檢查論點是否失效"))
    elif yoys and yoys[-1] < 0 and len(yoys) >= 2 and yoys[-2] >= 0:
        res["flags"].append(("warning", "年增率轉負"))
    if last["mom"] is not None and abs(last["mom"]) >= MOM_BIG:
        res["flags"].append(("info", f"月增 {last['mom']:+.0f}%，變動較大"))
    return res


# ---------------------------------------------------------------- 名單
def load_lists() -> list[tuple[str, str, str]]:
    """回傳 [(代號, 名稱, 分類)]，持股優先"""
    items, seen = [], set()
    if HOLDINGS.exists():
        h = tomllib.loads(HOLDINGS.read_text(encoding="utf-8"))
        for acc in h["accounts"]:
            for p in acc["positions"]:
                tk = p["ticker"]
                if tk.endswith((".TW", ".TWO")):
                    sid = tk.split(".")[0]
                    if sid.startswith("00"):   # ETF 沒有月營收
                        continue
                    if sid not in seen:
                        items.append((sid, "", "持股"))
                        seen.add(sid)
    if WATCHLIST.exists():
        w = tomllib.loads(WATCHLIST.read_text(encoding="utf-8"))
        for x in w.get("stocks", []):
            sid = str(x["id"])
            if sid not in seen:
                items.append((sid, x.get("name", ""), "觀察"))
                seen.add(sid)
    return items


# ---------------------------------------------------------------- HTML
def _yi(v_yuan: float | None) -> str:
    return "—" if v_yuan is None else f"{v_yuan / 1e8:,.1f} 億"


def _pp(v: float | None) -> str:
    return "—" if v is None else f"{v:+.1f}%"


def _cls(v: float | None) -> str:
    return "" if v is None else ("pos" if v > 0 else "neg" if v < 0 else "")


def _spark(hist: list[dict]) -> str:
    if not hist:
        return ""
    vals = [h["rev"] for h in hist]
    mx = max(vals) or 1
    w, gap, H = 7, 2, 28
    bars = []
    for i, h in enumerate(hist):
        bh = max(2, h["rev"] / mx * H)
        last = i == len(hist) - 1
        tip = f"{h['y']}-{h['m']:02d}：{h['rev'] / 1e8:,.1f} 億（年增 {_pp(h.get('yoy'))}）"
        bars.append(f"<rect x='{i * (w + gap)}' y='{H - bh:.1f}' width='{w}' height='{bh:.1f}' rx='2' "
                    f"fill='var({'--series' if last else '--series-dim'})'><title>{html.escape(tip)}</title></rect>")
    return (f"<svg width='{len(hist) * (w + gap)}' height='{H}' role='img' "
            f"aria-label='近 13 個月營收'>{''.join(bars)}</svg>")


EXTRA_CSS = """
.stock{padding:12px 0;border-bottom:1px solid var(--grid)}.stock:last-child{border:0}
.stock .hd{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.stock .nm{font-weight:650}.stock .nums{display:flex;gap:14px;flex-wrap:wrap;font-size:13px;color:var(--text2);margin:4px 0;font-variant-numeric:tabular-nums}
.flag{font-size:13px;margin-top:3px}.flag .ic{display:inline-block;width:1.2em;font-weight:700}
.flag.good .ic{color:var(--good)}.flag.warning .ic{color:var(--warning)}.flag.serious .ic{color:var(--serious)}.flag.info .ic{color:var(--muted)}
.note-sm{color:var(--muted);font-size:12px}
.alert.good .ic{color:var(--good)}
"""


def render(period_mkt: str, analysed: list[dict], cands: list[dict], n_market: int) -> str:
    e = html.escape
    icon = {"good": "▲", "warning": "!", "serious": "▼", "info": "i"}
    label = {"good": "正面", "warning": "注意", "serious": "警示", "info": "資訊"}
    out = [f"<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>台股月營收</title><style>{CSS}{EXTRA_CSS}</style></head><body><main>"]
    out.append(f"<h1>台股月營收掃描</h1><div class='sub'>產出時間 {datetime.now():%Y-%m-%d %H:%M}"
               f"・全市場資料月份 {e(period_mkt)}（{n_market} 家）</div>")

    # 重點
    # 例外原則：持股的正負訊號＋觀察名單的負面訊號；觀察名單的正面訊號只顯示數量
    flagged = [(a, f) for a in analysed for f in a["flags"]
               if f[0] in ("serious", "warning") or (f[0] == "good" and a["tag"] == "持股")]
    good_watch = sorted({a["name"] for a in analysed for f in a["flags"] if f[0] == "good" and a["tag"] == "觀察"})
    out.append("<h2>重點</h2><div class='card'>")
    if not flagged:
        out.append("<div class='alert info'><span class='ic'>i</span><span>持股與觀察名單本月沒有特別訊號</span></div>")
    order = {"serious": 0, "warning": 1, "good": 2}
    for a, (lvl, msg) in sorted(flagged, key=lambda x: (order[x[1][0]], x[0]["tag"] != "持股")):
        out.append(f"<div class='alert {lvl}'><span class='ic' aria-hidden='true'>{icon[lvl]}</span>"
                   f"<span><b>{e(a['name'])}</b>（{e(a['tag'])}）{e(msg)}</span></div>")
    if good_watch:
        out.append(f"<div class='alert good'><span class='ic' aria-hidden='true'>▲</span>"
                   f"<span>觀察名單有 {len(good_watch)} 檔出現正面訊號（創新高或成長加速）："
                   f"{e('、'.join(good_watch))}，細節見下方</span></div>")
    out.append("</div>")

    # 逐檔
    for tag in ("持股", "觀察"):
        group = [a for a in analysed if a["tag"] == tag]
        if not group:
            continue
        out.append(f"<h2>{tag}（{len(group)} 檔）</h2><div class='card'>")
        for a in group:
            out.append(f"<div class='stock'><div class='hd'><span class='nm'>{e(a['name'])} "
                       f"<span class='sub'>{e(a['sid'])}</span></span>{_spark(a['hist'])}</div>")
            if a.get("period"):
                out.append(f"<div class='nums'><span>{e(a['period'])}</span><span>營收 {_yi(a['rev'])}</span>"
                           f"<span class='{_cls(a['yoy'])}'>年增 {_pp(a['yoy'])}</span>"
                           f"<span class='{_cls(a['mom'])}'>月增 {_pp(a['mom'])}</span>"
                           f"<span class='{_cls(a.get('cum_yoy'))}'>累計 {_pp(a.get('cum_yoy'))}</span></div>")
            for lvl, msg in a["flags"]:
                out.append(f"<div class='flag {lvl}'><span class='ic' aria-hidden='true'>{icon[lvl]}</span>"
                           f"<span class='sr'>{label[lvl]}：</span>{e(msg)}</div>")
            out.append("</div>")
        out.append("</div>")

    # 篩選
    out.append(f"<h2>股票池候選（{len(cands)} 檔）</h2>"
               f"<div class='sub' style='margin-bottom:8px'>條件：當月營收 ≥ 10 億、年增 ≥ {SCREEN_MIN_YOY:.0f}%、"
               f"累計年增 ≥ {SCREEN_MIN_CUM_YOY:.0f}%、當月年增不低於累計年增（成長沒有放緩），"
               f"排除金融股與已在名單內的公司，依累計年增排序。"
               f"這只是基本面初篩，不代表值得買進。</div>")
    out.append("<div class='card scroll'><table><tr><th>公司</th><th>產業</th><th>營收</th><th>年增</th>"
               "<th>月增</th><th>累計年增</th></tr>")
    for r in cands[:40]:
        note = f"<br><span class='note-sm'>{e(r['note'][:40])}</span>" if r["note"] else ""
        out.append(f"<tr><td>{e(r['name'])} <span class='sub'>{e(r['sid'])}</span>{note}</td>"
                   f"<td><span class='sub'>{e(r['industry'])}</span></td>"
                   f"<td>{r['rev'] / 1e5:,.1f} 億</td><td class='{_cls(r['yoy'])}'>{_pp(r['yoy'])}</td>"
                   f"<td class='{_cls(r['mom'])}'>{_pp(r['mom'])}</td><td class='{_cls(r['cum_yoy'])}'>{_pp(r['cum_yoy'])}</td></tr>")
    if len(cands) > 40:
        out.append(f"<tr><td colspan='6' class='sub'>另有 {len(cands) - 40} 檔，完整清單在 data/tw_revenue 快照裡</td></tr>")
    out.append("</table></div>")
    out.append("<p class='note'>資料來源：臺灣證券交易所／證券櫃買中心 OpenAPI（全市場彙總，每月 10 日申報截止後更新）、"
               "FinMind（個股歷史）。逐檔區塊用的是公司最新申報的月份，可能比全市場資料新一個月。"
               "本報告為個人研究用途，不構成投資建議。</p></main></body></html>")
    return "".join(out).replace("<span class='sr'>", "<span style='position:absolute;left:-9999px'>")


# ---------------------------------------------------------------- main
def main() -> None:
    print("抓取全市場月營收…", file=sys.stderr)
    period, market = fetch_market()
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    (SNAP_DIR / f"{period}.json").write_text(json.dumps(market, ensure_ascii=False, indent=1), encoding="utf-8")
    names = {r["sid"]: r["name"] for r in market}
    industries = {r["sid"]: r["industry"] for r in market}

    items = load_lists()
    analysed = []
    for sid, nm, tag in items:
        print(f"  {sid} 歷史月營收…", file=sys.stderr)
        analysed.append(analyse(sid, nm or names.get(sid, sid), fetch_history(sid), tag, industries.get(sid, "")))
    cands = screen(market, {sid for sid, _, _ in items})

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    page = render(period, analysed, cands, len(market))
    latest_period = max((a.get("period", "") for a in analysed), default=period) or period
    (REPORT_DIR / f"tw_revenue_{latest_period}.html").write_text(page, encoding="utf-8")
    (REPORT_DIR / "tw_revenue_latest.html").write_text(page, encoding="utf-8")

    print(f"全市場 {period}：{len(market)} 家；候選 {len(cands)} 檔")
    for a in analysed:
        flags = "；".join(m for _, m in a["flags"]) or "—"
        print(f"  [{a['tag']}] {a['name']}({a['sid']}) {a.get('period', '')} 年增 {_pp(a.get('yoy'))} 累計 {_pp(a.get('cum_yoy'))} | {flags}")
    print(f"報告：{REPORT_DIR / 'tw_revenue_latest.html'}")


if __name__ == "__main__":
    main()
