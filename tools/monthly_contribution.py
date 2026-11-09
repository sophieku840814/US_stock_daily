"""每月新資金分配試算（B 方案）：這個月 CSPX 買多少、選股留多少、離 AI 主題目標還差多少。

輸入：private/portfolio/policy.toml 的 [new_money]（monthly_twd、index_share、index_etf、start_month）
      最新持股快照 private/portfolio/snapshots/*.json（請先跑 portfolio_exposure.py，裡面有 index_plan）
輸出：stdout 印出 JSON，給排程裡的 Claude 讀

用法：python -X utf8 tools/monthly_contribution.py
"""
from __future__ import annotations

import json
import math
import tomllib
from datetime import date
from pathlib import Path

import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
POLICY = ROOT / "private" / "portfolio" / "policy.toml"
SNAP_DIR = ROOT / "private" / "portfolio" / "snapshots"


def main() -> None:
    pol = tomllib.loads(POLICY.read_text(encoding="utf-8"))
    nm = pol["new_money"]
    monthly = float(nm["monthly_twd"])
    idx_amt = monthly * nm.get("index_share", 0.85)
    sat_amt = monthly * nm.get("satellite_share", 0.15)
    etf = nm.get("index_etf", "CSPX.L")

    fx = float(yf.Ticker("TWD=X").fast_info.last_price)          # 1 USD = ? TWD
    px = float(yf.Ticker(etf).fast_info.last_price)               # CSPX 以美元計價
    usd = idx_amt / fx

    snap = json.loads(sorted(SNAP_DIR.glob("*.json"))[-1].read_text(encoding="utf-8"))
    plan = snap.get("index_plan")
    ai_now = dict(snap["themes"]).get("AI／半導體")
    months_left = math.ceil(plan["need_twd"] / idx_amt) if plan else 0

    start = nm.get("start_month")
    n_months = None
    if start:
        y, m = map(int, start.split("-"))
        n_months = (date.today().year - y) * 12 + date.today().month - m + 1

    sat_value = snap["by_sleeve"].get("satellite", 0) * snap["total_twd"]
    sat_target = pol.get("allocation", {}).get("satellite_target", 0.10) * snap["total_twd"]

    print(json.dumps({
        "month": f"{date.today():%Y-%m}",
        "monthly_twd": monthly,
        "index": {"etf": etf, "twd": round(idx_amt), "usd": round(usd, 2), "price_usd": round(px, 2),
                  "approx_shares": round(usd / px, 4), "note": "CSPX 一股約數百美元，用支援零股的券商（例如 IBKR）買"},
        "satellite": {"twd": round(sat_amt), "note": "沒有符合交易卡條件的標的時，這筆錢先留著（現金），不要硬買",
                      "current_twd": round(sat_value), "target_twd": round(sat_target)},
        "ai_theme": {"now": ai_now, "target": plan["target"] if plan else None,
                     "need_twd": round(plan["need_twd"]) if plan else 0, "months_left_at_this_pace": months_left},
        "months_since_start": n_months,
        "raise_reminder": bool(n_months and n_months >= 3 and monthly < 60000),
        "fx_usd_twd": round(fx, 2),
        "snapshot_as_of": snap["as_of"],
    }, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
