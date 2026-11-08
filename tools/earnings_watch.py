"""美股持股財報偵測：找出「快要公布」與「剛公布」財報的持股，並追蹤是否已處理過。

  preview_due：下次財報日在接下來 1–3 個日曆天內，且還沒寫過該次財報預覽 → 該寫財報預覽
  review_due ：最近一次財報日是 1–3 天前，且還沒寫過該次財報判讀     → 該寫財報判讀
  upcoming   ：接下來 --days 天內有財報的全部持股（含日期、權重）

「今天」以台北時間為準；財報日一律是美東日期（美股多在美東盤後公布，台北隔天早上才看得到，
所以美東 11/17 盤後的財報，在台北 11/18 早上是 days_since = 1）。

範圍：美股／海外（非 .TW／.TWO）、非 ETF 的持股，跨帳戶合併；權重 < 總資產 0.2% 的忽略。
權重用最新持股快照（private/portfolio/snapshots/*.json 的 value_usd / total_usd）。

資料來源（Yahoo Finance，可能是預估日期）：
  下次財報日：Ticker.info 的 earningsTimestampStart/End + isEarningsDateEstimate（有時間、能判斷是否確認）；
              拿不到才用 Ticker.calendar["Earnings Date"]（兩個日期＝區間 → confirmed false，一個 → unknown）
  上次財報日：Ticker.get_earnings_dates() 中 Reported EPS 非空的最近一筆
  consensus  ：Ticker.calendar 的 Earnings Average／Revenue Average

狀態檔：private/portfolio/earnings_state.json
  {ticker: {"previewed": "YYYY-MM-DD", "reviewed": "YYYY-MM-DD"}}（值為財報日）
  Yahoo 的日期偶爾會差一天，所以標記日期與財報日相差 ±3 天內都視為同一次財報。

用法：
  python -X utf8 tools/earnings_watch.py [--days 14]          偵測，stdout 印出 JSON（給排程任務的 Claude 讀）
  python -X utf8 tools/earnings_watch.py --mark previewed|reviewed TICKER YYYY-MM-DD
                                                              處理完後標記，避免重複
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
SNAP_DIR = ROOT / "private" / "portfolio" / "snapshots"
STATE = ROOT / "private" / "portfolio" / "earnings_state.json"
THESIS_DIR = Path(r"C:\Users\Andy\Documents\obsidian\obsidian\10 公司")

TPE = ZoneInfo("Asia/Taipei")
ET = ZoneInfo("America/New_York")
MIN_WEIGHT = 0.002         # 低於總資產 0.2% 的部位忽略
DUE_WINDOW = (1, 3)        # preview／review 的天數區間（日曆天）
SAME_EVENT_TOLERANCE = 3   # 狀態檔日期與財報日相差幾天內視為同一次財報


# ---------------------------------------------------------------- 持股
def load_positions() -> tuple[dict[str, float], str]:
    """回傳 ({代號: 權重}, 快照檔名)；美股／海外、非 ETF，跨帳戶合併"""
    snaps = sorted(SNAP_DIR.glob("*.json"))
    if not snaps:
        sys.exit(f"找不到持股快照：{SNAP_DIR}（先跑 tools/portfolio_exposure.py）")
    snap = json.loads(snaps[-1].read_text(encoding="utf-8"))
    total = snap["total_usd"]
    val: dict[str, float] = defaultdict(float)
    for r in snap["rows"]:
        tk = r["ticker"]
        if tk.endswith((".TW", ".TWO")) or r.get("is_etf"):
            continue
        val[tk] += r.get("value_usd") or 0.0
    return {tk: v / total for tk, v in val.items()}, snaps[-1].name


# ---------------------------------------------------------------- 狀態檔
def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            return {}
    return {}


def already(state: dict, ticker: str, kind: str, earnings_date: str) -> bool:
    marked = state.get(ticker, {}).get(kind)
    if not marked:
        return False
    try:
        gap = abs((date.fromisoformat(marked) - date.fromisoformat(earnings_date)).days)
    except ValueError:
        return False
    return gap <= SAME_EVENT_TOLERANCE


def mark(kind: str, ticker: str, d: str) -> None:
    date.fromisoformat(d)  # 格式檢查
    state = load_state()
    state.setdefault(ticker.upper(), {})[kind] = d
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"ok": True, "marked": {ticker.upper(): {kind: d}}, "state_file": str(STATE)},
                     ensure_ascii=False))


# ---------------------------------------------------------------- yfinance
def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _timing(dt_et: datetime) -> str:
    m = dt_et.hour * 60 + dt_et.minute
    if m == 0:
        return "unknown"          # 只有日期沒有時間
    if m >= 16 * 60:
        return "after_close"
    if m < 9 * 60 + 30:
        return "before_open"
    return "during_market"


def earnings_info(ticker: str) -> dict:
    """回傳下次／上次財報資訊；各欄位拿不到就是 None，並在 notes 說明"""
    t = yf.Ticker(ticker)
    now = datetime.now(ET)
    out: dict = {"next": None, "last": None, "consensus": None, "notes": []}

    # calendar：備援日期 + consensus
    cal_dates: list[date] = []
    try:
        cal = t.calendar or {}
        cal_dates = [d for d in (cal.get("Earnings Date") or []) if isinstance(d, date)]
        cons = {k: _num(cal.get(src)) for k, src in
                (("eps_avg", "Earnings Average"), ("eps_low", "Earnings Low"), ("eps_high", "Earnings High"),
                 ("revenue_avg", "Revenue Average"), ("revenue_low", "Revenue Low"), ("revenue_high", "Revenue High"))}
        cons = {k: v for k, v in cons.items() if v is not None}
        if cons:
            out["consensus"] = cons
    except Exception as ex:  # noqa: BLE001
        out["notes"].append(f"calendar 失敗：{type(ex).__name__}")

    # info：有時間戳與是否為預估
    info = {}
    try:
        info = t.info or {}
    except Exception as ex:  # noqa: BLE001
        out["notes"].append(f"info 失敗：{type(ex).__name__}")
    ts_s, ts_e = info.get("earningsTimestampStart"), info.get("earningsTimestampEnd")
    if ts_s and datetime.fromtimestamp(ts_s, ET) >= now - timedelta(hours=12):
        s = datetime.fromtimestamp(ts_s, ET)
        e = datetime.fromtimestamp(ts_e, ET) if ts_e else s
        est = info.get("isEarningsDateEstimate")
        if e.date() != s.date():
            confirmed = False
        else:
            confirmed = (not est) if isinstance(est, bool) else "unknown"
        out["next"] = {"date": s.date().isoformat(), "date_end": e.date().isoformat() if e.date() != s.date() else None,
                       "time_et": s.strftime("%H:%M") if _timing(s) != "unknown" else None,
                       "timing": _timing(s), "confirmed": confirmed, "source": "info"}
        if cal_dates and cal_dates[0] != s.date():
            out["next"]["calendar_date"] = cal_dates[0].isoformat()
            out["notes"].append(f"calendar 日期（{cal_dates[0]}）與 info（{s.date()}）不同，以 info 為準")
    elif cal_dates and cal_dates[-1] >= now.date():
        out["next"] = {"date": cal_dates[0].isoformat(),
                       "date_end": cal_dates[-1].isoformat() if len(cal_dates) > 1 else None,
                       "time_et": None, "timing": "unknown",
                       "confirmed": False if len(cal_dates) > 1 else "unknown", "source": "calendar"}

    # get_earnings_dates：上次已公布
    try:
        df = t.get_earnings_dates(limit=8)
        if df is not None and not df.empty and "Reported EPS" in df.columns:
            rep = df[df["Reported EPS"].notna()].sort_index(ascending=False)
            rep = rep[rep.index.tz_convert(ET) <= now]
            if not rep.empty:
                dt = rep.index[0].tz_convert(ET).to_pydatetime()
                row = rep.iloc[0]
                out["last"] = {"date": dt.date().isoformat(),
                               "time_et": dt.strftime("%H:%M") if _timing(dt) != "unknown" else None,
                               "timing": _timing(dt),
                               "eps_estimate": _num(row.get("EPS Estimate")),
                               "eps_reported": _num(row.get("Reported EPS")),
                               "surprise_pct": _num(row.get("Surprise(%)"))}
            if out["next"] is None:   # info／calendar 都沒有 → 用 earnings_dates 的未來列（多半是預估）
                fut = df[df.index.tz_convert(ET) > now].sort_index()
                if not fut.empty:
                    dt = fut.index[0].tz_convert(ET).to_pydatetime()
                    out["next"] = {"date": dt.date().isoformat(), "date_end": None,
                                   "time_et": dt.strftime("%H:%M") if _timing(dt) != "unknown" else None,
                                   "timing": _timing(dt), "confirmed": "unknown", "source": "earnings_dates"}
    except Exception as ex:  # noqa: BLE001
        out["notes"].append(f"get_earnings_dates 失敗：{type(ex).__name__}")
    if out["last"] is None:
        out["notes"].append("拿不到最近一次已公布財報日，review 偵測略過")
    if out["next"] is None:
        out["notes"].append("拿不到下次財報日")
    return out


def thesis_note(ticker: str) -> str | None:
    p = THESIS_DIR / ticker / f"{ticker} 投資論點.md"
    return str(p) if p.exists() else None


# ---------------------------------------------------------------- 偵測
def detect(days: int) -> dict:
    today = datetime.now(TPE).date()
    weights, snap_name = load_positions()
    state = load_state()
    lo, hi = DUE_WINDOW
    preview, review, upcoming, errors, skipped, no_next = [], [], [], [], [], []

    for tk, w in sorted(weights.items(), key=lambda kv: -kv[1]):
        if w < MIN_WEIGHT:
            skipped.append(tk)
            continue
        base = {"ticker": tk, "weight_pct": round(w * 100, 2)}
        try:
            info = earnings_info(tk)
        except Exception as ex:  # noqa: BLE001
            errors.append({**base, "error": f"{type(ex).__name__}: {ex}"})
            continue
        note = thesis_note(tk)
        common = {"thesis_note": note, "notes": info["notes"] or None}

        nx = info["next"]
        if not nx:
            no_next.append(tk)
        if nx:
            du = (date.fromisoformat(nx["date"]) - today).days
            item = {**base, "earnings_date": nx["date"], "earnings_date_end": nx["date_end"],
                    "time_et": nx["time_et"], "timing": nx["timing"], "confirmed": nx["confirmed"],
                    "days_until": du, "consensus": info["consensus"], **common}
            if 0 <= du <= days:
                upcoming.append(item)
            if lo <= du <= hi and not already(state, tk, "previewed", nx["date"]):
                preview.append(item)

        ls = info["last"]
        if ls:
            ds = (today - date.fromisoformat(ls["date"])).days
            if lo <= ds <= hi and not already(state, tk, "reviewed", ls["date"]):
                review.append({**base, "earnings_date": ls["date"], "time_et": ls["time_et"],
                               "timing": ls["timing"], "confirmed": True, "days_since": ds,
                               "eps_estimate": ls["eps_estimate"], "eps_reported": ls["eps_reported"],
                               "surprise_pct": ls["surprise_pct"], "consensus": None, **common})
        if not nx and not ls:
            errors.append({**base, "error": "; ".join(info["notes"]) or "無財報資料"})

    upcoming.sort(key=lambda r: r["earnings_date"])
    return {
        "today_taipei": today.isoformat(), "window_days": days, "snapshot": snap_name,
        "preview_due": preview, "review_due": review, "upcoming": upcoming,
        "errors": errors, "skipped_small": skipped, "no_next_date": no_next,
        "state_file": str(STATE),
        "mark_usage": "python -X utf8 tools/earnings_watch.py --mark previewed|reviewed TICKER YYYY-MM-DD",
        "disclaimer": "財報日來自 Yahoo Finance，confirmed=false/unknown 表示可能是預估；日期為美東時間。",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="美股持股財報偵測")
    ap.add_argument("--days", type=int, default=14, help="upcoming 往後看幾天（預設 14）")
    ap.add_argument("--mark", nargs=3, metavar=("KIND", "TICKER", "DATE"),
                    help="標記已處理：KIND = previewed|reviewed，DATE = 財報日 YYYY-MM-DD")
    a = ap.parse_args()
    if a.mark:
        kind, tk, d = a.mark
        if kind not in ("previewed", "reviewed"):
            sys.exit("KIND 必須是 previewed 或 reviewed")
        try:
            mark(kind, tk, d)
        except ValueError:
            sys.exit("DATE 格式必須是 YYYY-MM-DD")
        return
    print(json.dumps(detect(a.days), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
