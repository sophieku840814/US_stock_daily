"""通用 DCF 模型產生器：輸入代號 → 產出 Excel 模型（全部公式）＋摘要 JSON。

用法：
    python -X utf8 tools/dcf_build.py 2330.TW [--rf 0.016] [--erp 0.055] [--tg 0.025] [--gcap 0.40] [--refresh]
    python -X utf8 tools/dcf_build.py AMD

輸入：data/dcf_inputs/{TICKER}.json（不存在或加 --refresh 時，自動呼叫 tools/dcf_inputs.py 產生）。
輸出：
    models/{代號去掉 .TW/.TWO}_DCF_{評價日}.xlsx
    models/{代號去掉 .TW/.TWO}_DCF_summary.json

設計（沿用 tools/build_dcf_2330.py 的版面與 Claude Finance dcf-model skill 規範）：
- 所有預測、折現、終值、敏感度都是 Excel 公式；藍字＝輸入、黑字＝公式、綠字＝跨表連結；每個輸入都有來源註解。
- 歷史：最近 3–4 個完整會計年度；另有 TTM（近四季）備忘欄。
- 「當年度橋接年」（評價日所在會計年度，例如 FY2026E）：不分情境，只計入資產負債表日之後的部分。
- 預測 5 年：從評價日的下一個會計年度開始（例如 FY2027E–FY2031E）。
- 折現：年中折現（mid-year convention），折現期以實際評價日計算：
      期間 = MAX(0, (會計年度結束日 − 評價日)/365.25 − 計入比例/2)
      計入比例 = MAX(0, MIN(1, (會計年度結束日 − 資產負債表日)/365.25))
  （淨負債取資產負債表日數字，所以只計入資產負債表日之後產生的現金流；終值以最後一年的年中期間折現，與原版一致）
- 三情境自動假設：
  營收成長＝今年（橋接年）用分析師 0y、第一個預測年用 +1y（皆上限 --gcap，預設 60%；無預估用 YTD 月營收／歷史 CAGR），
  之後線性收斂到永續成長率；悲觀／樂觀＝基本 ∓ 30%×|基本|。
  利益率＝TTM，悲觀 −3pp、樂觀 +2pp（限制 0%–70%）；若 TTM 攤銷 > 1% 營收（併購無形資產攤銷，如 AMD），
  改用 EBITA 利益率且 D&A 只取折舊。D&A、資本支出用近 3 年平均；資本支出第 2–5 年以公式線性收斂到
  永續水準（折舊% × 1.25），避免終值年 FCF 跳升。營運資金＝資產負債表（應收＋存貨−應付）年增加 / 營收年增加，
  近 3 年中位數，限制 0–20%。終值佔 EV > 75% 時在摘要標示警告。
- WACC：rf（美股 ^TNX 自動；台股參數，預設 1.6% 待查證）、beta（yfinance 原始值 → Blume 0.67β+0.33 → 限制 0.8–1.6）、
  ERP（美股 5.0%、台股 5.5%）、舉債成本（利息費用／平均負債，下限 rf；抓不到用 rf+1%）、稅率、市值權重。
- 摘要 JSON 的三情境合理價由 Python 依同一套公式重算（compute_scenario），並與 Excel 內「情境摘要」公式同步。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
BN = 1e9
TERM_CAPEX_RATIO = 1.25
GROWTH_SPREAD = 0.30      # 悲觀／樂觀成長率 = 基本 ∓ 30%×|基本|
MARGIN_BEAR, MARGIN_BULL = -0.03, 0.02
MARGIN_MIN, MARGIN_MAX = 0.0, 0.70
BETA_MIN, BETA_MAX = 0.8, 1.6
TV_WARN = 0.75
STATUTORY_TAX = {"TW": 0.20, "US": 0.21}
TW_RF_DEFAULT = 0.016

# ---------- styles（與 build_dcf_2330.py 相同） ----------
BLUE, GREEN = Font(color="0000FF"), Font(color="008000")
F_HDR = PatternFill("solid", fgColor="1F4E79")
F_SUB = PatternFill("solid", fgColor="D9E1F2")
F_INP = PatternFill("solid", fgColor="F2F2F2")
F_OUT = PatternFill("solid", fgColor="BDD7EE")
NUM = '#,##0;(#,##0);"-"'
NUM1 = '#,##0.0;(#,##0.0);"-"'
PCT = '0.0%;(0.0%);"-"'
DATE = "yyyy-mm-dd"


def note(c, src):
    c.comment = Comment(src, "Claude")
    c.comment.width, c.comment.height = 360, 110


def inp(ws, ref, val, fmt, src):
    c = ws[ref]
    c.value, c.font, c.fill, c.number_format = val, BLUE, F_INP, fmt
    note(c, src)
    return c


def calc(ws, ref, f, fmt, bold=False, out=False):
    c = ws[ref]
    c.value, c.number_format = f, fmt
    c.font = Font(color="000000", bold=bold or out)
    if out:
        c.fill = F_OUT
    return c


def link(ws, ref, f, fmt):
    c = ws[ref]
    c.value, c.font, c.number_format = f, GREEN, fmt
    return c


def header(ws, row, text, c1="A", c2="J"):
    ws[f"{c1}{row}"] = text
    ws[f"{c1}{row}"].font = Font(bold=True, color="FFFFFF")
    for col in range(ord(c1), ord(c2) + 1):
        ws[f"{chr(col)}{row}"].fill = F_HDR
    ws.merge_cells(f"{c1}{row}:{c2}{row}")


def subhdr(ws, row, labels, start="A"):
    for i, t in enumerate(labels):
        c = ws[f"{chr(ord(start) + i)}{row}"]
        c.value, c.font, c.fill = t, Font(bold=True), F_SUB
        c.alignment = Alignment(horizontal="center" if i else "left")


def box(ws, rng, weight="medium"):
    s = Side(style=weight)
    (c1, r1), (c2, r2) = [(x.rstrip("0123456789"), int(x.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))) for x in rng.split(":")]
    for r in range(r1, r2 + 1):
        for col in range(ord(c1), ord(c2) + 1):
            cell = ws[f"{chr(col)}{r}"]
            b = cell.border
            cell.border = Border(left=s if col == ord(c1) else b.left, right=s if col == ord(c2) else b.right,
                                 top=s if r == r1 else b.top, bottom=s if r == r2 else b.bottom)


# ====================================================================== data
def load_inputs(ticker: str, refresh: bool) -> dict:
    path = ROOT / "data" / "dcf_inputs" / f"{ticker}.json"
    if refresh or not path.exists():
        subprocess.run([sys.executable, "-X", "utf8", str(TOOLS / "dcf_inputs.py"), ticker], check=True)
    return json.loads(path.read_text(encoding="utf-8"))


def _d(s: str) -> date:
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def _add_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:  # 2/29
        return d.replace(year=d.year + n, day=28)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _pct(v):
    return f"{v * 100:.1f}%"


def assumptions(D: dict, tg: float, gcap: float) -> dict:
    """自動產生橋接年＋三情境假設。回傳數值與每個假設的註解（算法＋來源）。"""
    A = D["annual"]
    ys = sorted(A)
    T = D.get("ttm") or {}
    est = (D.get("estimates") or {}).get("revenue") or {}
    src = D["source"]
    last3 = ys[-3:]
    rev = lambda y: A[y]["revenue"]  # noqa: E731
    out: dict = {"notes": {}}

    # ---- 營收成長 ----
    n_cagr = len(ys) - 1
    cagr = (rev(ys[-1]) / rev(ys[0])) ** (1 / n_cagr) - 1 if n_cagr >= 1 and rev(ys[0]) > 0 else None
    cagr_note = f"近 {n_cagr} 年營收 CAGR（FY{ys[0][:4]}→FY{ys[-1][:4]}）= {_pct(cagr)}" if cagr is not None else "CAGR 無法計算"

    # 橋接年（評價日所在會計年度）：分析師 0y 預估優先（上限 gcap），其次台股 YTD 月營收、TTM
    ytd = D.get("current_year_ytd")
    e0 = est.get("0y") or {}
    ytd_txt = (f"；對照 {ytd['source']} 累計至 {ytd['through_month']} 實際營收年增 {_pct(ytd['ytd_yoy'])}"
               if ytd and ytd.get("ytd_yoy") is not None else "")
    if e0.get("growth") is not None:
        bg = min(e0["growth"], gcap)
        bg_note = (f"Source: Yahoo Finance via yfinance revenue_estimate '0y'（本會計年度）分析師平均預估成長 {_pct(e0['growth'])}，"
                   f"{int(e0.get('numberOfAnalysts') or 0)} 位分析師，抓取日 {D['as_of']}"
                   + (f"，超過上限 {_pct(gcap)}，以上限計" if bg < e0["growth"] else "") + ytd_txt)
    elif ytd and ytd.get("ytd_yoy") is not None:
        bg = min(ytd["ytd_yoy"], gcap)
        bg_note = f"Source: 無分析師預估，用 {ytd['source']} 累計至 {ytd['through_month']} 營收年增率 {_pct(bg)}（實際數，套用全年）"
    elif T.get("revenue") and rev(ys[-1]):
        bg = T["revenue"] / rev(ys[-1]) - 1
        bg_note = f"無分析師預估：以 TTM 營收 / FY{ys[-1][:4]} 營收 − 1 = {_pct(bg)} 近似（{src}）"
    else:
        bg = cagr or 0.0
        bg_note = f"無分析師預估與 TTM：用 {cagr_note}"
    out["bridge_g"] = bg
    out["notes"]["bridge_g"] = bg_note

    # 預測第一年
    e1 = est.get("+1y") or {}
    if e1.get("growth") is not None:
        g1_raw = e1["growth"]
        g1_src = (f"Yahoo Finance via yfinance revenue_estimate '+1y'（下一會計年度）分析師平均預估成長 {_pct(g1_raw)}，"
                  f"{int(e1.get('numberOfAnalysts') or 0)} 位分析師，抓取日 {D['as_of']}")
    else:
        g1_raw = cagr if cagr is not None else tg
        g1_src = f"無分析師預估，改用 {cagr_note}"
    g1 = min(g1_raw, gcap)
    capped = g1 < g1_raw
    base_g = [g1 + (tg - g1) * t / 5 for t in range(5)]
    g_note = (f"Source: 自動假設。第一年 = {g1_src}"
              + (f"，超過上限 {_pct(gcap)}，以上限計" if capped else "")
              + f"；之後線性收斂到永續成長率 {_pct(tg)}（g_t = g1 + (tg − g1)×(t−1)/5，第 6 年＝tg）。")
    spread = lambda g, s: g + s * GROWTH_SPREAD * abs(g)  # noqa: E731
    out["g"] = {"BEAR": [spread(g, -1) for g in base_g], "BASE": base_g, "BULL": [spread(g, 1) for g in base_g]}
    out["notes"]["g"] = {"BASE": g_note,
                         "BEAR": f"Source: 自動假設。基本情境各年成長率 − 30%×|基本|。{g_note}",
                         "BULL": f"Source: 自動假設。基本情境各年成長率 + 30%×|基本|。{g_note}"}
    out["g1_raw"], out["g1_capped"], out["cagr"] = g1_raw, capped, cagr

    # ---- 攤銷是否重大（併購無形資產攤銷）：TTM 攤銷 > 1% 營收 → 改用 EBITA 利益率＋僅折舊 ----
    base_p = T if T.get("revenue") else A[ys[-1]]
    amort_pct = (base_p.get("amortization") or 0) / base_p["revenue"]
    dep_ok = all(A[y].get("depreciation") is not None for y in last3)
    ebita = dep_ok and amort_pct > 0.01
    out["ebita_mode"], out["amort_pct"] = ebita, amort_pct

    # ---- EBIT 利益率 ----
    lab_p = f"TTM（{T.get('period', '')}）" if T.get("revenue") else f"FY{ys[-1][:4]}"
    if ebita:
        m0 = (base_p["operating_income"] + base_p["amortization"]) / base_p["revenue"]
        m_src = (f"{lab_p}（營業利益＋無形資產攤銷）/ 營收 = EBITA 利益率 {_pct(m0)}"
                 f"（攤銷占營收 {_pct(amort_pct)} > 1%，屬併購無形資產攤銷，排除於利益率與 D&A；"
                 f"未計攤銷之稅盾，略為保守）（{src}）")
    else:
        m0 = base_p["operating_income"] / base_p["revenue"]
        m_src = f"{lab_p} 營業利益 / 營收 = {_pct(m0)}（{src}）"
    out["m"] = {k: _clamp(m0 + d, MARGIN_MIN, MARGIN_MAX) for k, d in
                (("BEAR", MARGIN_BEAR), ("BASE", 0.0), ("BULL", MARGIN_BULL))}
    out["bridge_m"] = _clamp(m0, MARGIN_MIN, MARGIN_MAX)
    lim = f"限制在 {_pct(MARGIN_MIN)}–{_pct(MARGIN_MAX)}"
    out["notes"]["m"] = {"BASE": f"Source: 自動假設。{m_src}，5 年持平，{lim}",
                         "BEAR": f"Source: 自動假設。{m_src} − 3 個百分點，{lim}",
                         "BULL": f"Source: 自動假設。{m_src} + 2 個百分點，{lim}"}
    out["notes"]["bridge_m"] = f"Source: 自動假設。{m_src}"

    # ---- 稅率 ----
    tx = [A[y]["tax"] for y in last3]
    pbt = [A[y]["pretax_income"] for y in last3]
    if all(v is not None for v in tx + pbt) and sum(pbt) > 0:
        tr = sum(tx) / sum(pbt)
        t_note = f"近 3 年有效稅率 Σ所得稅 / Σ稅前淨利（FY{last3[0][:4]}–FY{last3[-1][:4]}）= {_pct(tr)}（{src}）"
    else:
        tr = None
        t_note = "近 3 年稅額資料不全"
    stat = STATUTORY_TAX.get(D["market"], 0.21)
    if tr is None or not 0.05 <= tr <= 0.35:
        t_note += f"；超出合理範圍 5%–35% 或缺資料，改用法定稅率 {_pct(stat)}"
        tr = stat
    out["tax"] = tr
    out["notes"]["tax"] = f"Source: 自動假設。{t_note}"

    # ---- D&A、資本支出（% 營收，近 3 年平均） ----
    def avg_ratio(key, sign=1):
        vals = [sign * A[y][key] / rev(y) for y in last3 if A[y].get(key) is not None]
        return sum(vals) / len(vals) if vals else None

    yr = f"FY{last3[0][:4]}–FY{last3[-1][:4]}"
    if ebita:
        da = avg_ratio("depreciation") or 0.0
        ttm_da = T["depreciation"] / T["revenue"] if T.get("depreciation") and T.get("revenue") else None
        da_note = (f"Source: 自動假設。近 3 年（{yr}）折舊（不含併購無形資產攤銷）/ 營收 簡單平均 = {_pct(da)}"
                   + (f"（TTM 為 {_pct(ttm_da)}）" if ttm_da else "")
                   + f"；攤銷已在 EBITA 利益率中排除。此列同時是永續資本支出的基礎（{src}）")
    else:
        da = avg_ratio("d_and_a") or 0.0
        ttm_da = T["d_and_a"] / T["revenue"] if T.get("d_and_a") and T.get("revenue") else None
        da_note = (f"Source: 自動假設。近 3 年（{yr}）D&A / 營收 簡單平均 = {_pct(da)}"
                   + (f"（TTM 為 {_pct(ttm_da)}）" if ttm_da else "")
                   + f"；攤銷占營收 {_pct(amort_pct)} ≤ 1%（非重大），D&A 視同折舊作為永續資本支出基礎（{src}）")
    cx = avg_ratio("capex", -1) or 0.0
    out["da"], out["capex"] = da, cx
    ttm_cx = -T["capex"] / T["revenue"] if T.get("capex") and T.get("revenue") else None
    out["notes"]["da"] = da_note
    out["capex_terminal"] = da * TERM_CAPEX_RATIO
    out["capex_path"] = [cx + (out["capex_terminal"] - cx) * j / 4 for j in range(5)]
    out["notes"]["capex"] = (f"Source: 自動假設。第 1 年 = 近 3 年（{yr}）資本支出 / 營收 簡單平均 {_pct(cx)}"
                             + (f"（TTM 為 {_pct(ttm_cx)}）" if ttm_cx else "")
                             + f"；第 2–5 年為公式，線性收斂到永續水準（折舊% × 永續倍數 = {_pct(out['capex_terminal'])}），"
                               f"避免終值年 FCF 跳升（{src}）")

    # ---- Δ營運資金（% of Δ營收）：資產負債表（應收＋存貨−應付）年增加 / 營收年增加，近 3 年中位數，限制 0–20% ----
    ratios, parts = [], []
    for i, y in enumerate(ys):
        if y not in last3 or i == 0:
            continue
        dr = rev(y) - rev(ys[i - 1])
        dn = A[y].get("delta_op_nwc")
        if dn is None:
            parts.append(f"FY{y[:4]} 缺資料")
        elif dr <= 0:
            parts.append(f"FY{y[:4]} 營收衰退，略過")
        else:
            ratios.append(dn / dr)
            parts.append(f"FY{y[:4]} {_pct(dn / dr)}")
    if ratios:
        ratios.sort()
        k = len(ratios)
        nwc_raw = ratios[k // 2] if k % 2 else (ratios[k // 2 - 1] + ratios[k // 2]) / 2
        nwc = _clamp(nwc_raw, 0.0, 0.20)
        n_note = (f"資產負債表營業營運資金（應收＋存貨−應付）年增加 / 營收年增加，近 3 年中位數 = {_pct(nwc_raw)}"
                  f"（{'；'.join(parts)}）" + ("" if nwc == nwc_raw else f"，限制在 0%–20%，以 {_pct(nwc)} 計"))
    else:
        nwc = 0.10
        n_note = f"近 3 年無可用年度（{'；'.join(parts)}），用預設 10%"
    out["nwc"] = nwc
    out["notes"]["nwc"] = f"Source: 自動假設。{n_note}（{src}）"
    return out


def wacc_inputs(D: dict, rf_arg, erp_arg) -> dict:
    tw = D["market"] == "TW"
    if rf_arg is not None:
        rf, rf_note = rf_arg, f"Source: 使用者以 --rf 指定 {_pct(rf_arg)}（{D['as_of']}）"
    elif not tw and D.get("risk_free"):
        r = D["risk_free"]
        rf, rf_note = r["value"], f"Source: {r['source']}，{r['as_of']} 收盤 {r['value'] * 100:.3f}%"
    elif tw:
        rf, rf_note = TW_RF_DEFAULT, ("Source: 參數預設值 1.6%（台灣 10 年期公債殖利率）——待查證！"
                                      "尚未接上可靠的免費資料源，使用前請至央行／櫃買中心查最新殖利率，並用 --rf 覆寫")
    else:
        rf, rf_note = 0.04, "Source: ^TNX 抓取失敗，暫用 4.0% 預設值——待查證，請用 --rf 覆寫"
    beta_raw = D.get("beta")
    if beta_raw is None:
        beta, b_note = 1.0, "Source: yfinance 無 beta，用 1.0 預設值——待查證"
    else:
        blume = 0.67 * beta_raw + 0.33
        beta = _clamp(blume, BETA_MIN, BETA_MAX)
        b_note = (f"Source: {D.get('beta_source', 'yfinance beta')} 原始值 {beta_raw:.3f}；"
                  f"Blume 調整 0.67×原始＋0.33 = {blume:.3f}"
                  + (f"；限制在 {BETA_MIN}–{BETA_MAX} 區間後為 {beta:.3f}" if beta != blume else
                     f"（在 {BETA_MIN}–{BETA_MAX} 區間內）") + f"，抓取日 {D['as_of']}")
    erp = erp_arg if erp_arg is not None else (0.055 if tw else 0.050)
    e_note = (f"Source: 使用者以 --erp 指定 {_pct(erp)}" if erp_arg is not None else
              ("Source: 預設值 5.5%（成熟市場約 4.5–5% ＋ 台灣國家／地緣風險溢酬約 0.5–1%），可用 --erp 覆寫" if tw else
               "Source: 預設值 5.0%（美國市場常用 ERP，Damodaran 隱含 ERP 約 4–5%），可用 --erp 覆寫"))
    k = D.get("cost_of_debt_estimate")
    if k and k.get("value"):
        kd = _clamp(k["value"], rf, rf + 0.06)
        k_method = "利息費用 / 平均負債"
        k_note = (f"Source: 估算 = FY{k['fiscal_year']} {k['method']}："
                  f"{k['interest_expense'] / BN:,.2f} / {k['avg_debt'] / BN:,.1f}（十億）= {_pct(k['value'])}"
                  + ("" if kd == k["value"] else f"；限制在 rf ~ rf+6% 區間（優質發行人實際殖利率不會低於公債），以 {kd * 100:.2f}% 計"))
    else:
        kd = rf + 0.01
        k_method = "rf + 1%"
        k_note = "Source: 抓不到利息費用或負債，用 rf + 1% 預設——待查證"
    return {"rf": rf, "rf_note": rf_note, "beta": beta, "beta_raw": beta_raw, "beta_note": b_note,
            "beta_blume": None if beta_raw is None else 0.67 * beta_raw + 0.33,
            "erp": erp, "erp_note": e_note, "kd": kd, "kd_method": k_method, "kd_note": k_note}


# ====================================================================== Python 版 DCF（與 Excel 公式一致）
def compute_scenario(P: dict, case: str, wacc_base: float) -> dict:
    """依 Excel 同一套公式計算單一情境的每股價值（給摘要 JSON 使用，並用來核對 Excel）。"""
    S = P["scen"][case]
    W = wacc_base + S["wadj"]
    tg, tcx = S["tg"], S["tcx"]
    br = P["bridge"]
    rev0 = P["last_rev"]
    rev_b = rev0 * (1 + br["g"])
    fcf_b = rev_b * (br["m"] * (1 - br["tax"]) + br["da"] - br["capex"]) - (rev_b - rev0) * br["nwc"]
    revs, fcfs = [], []
    prev = rev_b
    for t in range(5):
        r = prev * (1 + S["g"][t])
        fcfs.append(r * (S["m"][t] * (1 - S["tax"][t]) + S["da"][t] - S["capex"][t]) - (r - prev) * S["nwc"][t])
        revs.append(r)
        prev = r
    frac, per = P["frac"], P["per"]  # index 0 = bridge
    pv = fcf_b * frac[0] / (1 + W) ** per[0] + sum(fcfs[t] * frac[t + 1] / (1 + W) ** per[t + 1] for t in range(5))
    rt = revs[-1] * (1 + tg)
    tfcf = rt * (S["m"][4] * (1 - S["tax"][4]) + S["da"][4] * (1 - tcx)) - (rt - revs[-1]) * S["nwc"][4]
    tv = tfcf / (W - tg)
    pv_tv = tv / (1 + W) ** per[5]
    ev = pv + pv_tv
    eq = ev - P["net_debt"]
    return {"wacc": W, "tg": tg, "ev": ev, "pv_fcf": pv, "pv_tv": pv_tv, "tv_pct_ev": pv_tv / ev,
            "equity": eq, "price": eq / P["shares"], "revenue_fy5": revs[-1], "fcf": fcfs}


def wacc_python(P: dict, W: dict, tax_fy1: float) -> float:
    mcap = P["price"] * P["shares"]
    debt = P["debt"]
    ke = W["rf"] + W["beta"] * W["erp"]
    return mcap / (mcap + debt) * ke + debt / (mcap + debt) * W["kd"] * (1 - tax_fy1)


# ====================================================================== Excel
def build(ticker: str, rf_arg, erp_arg, tg: float, gcap: float, refresh: bool) -> tuple[Path, Path, dict]:
    D = load_inputs(ticker, refresh)
    A = D["annual"]
    ys = sorted(A)
    nh = len(ys)
    if nh < 3:
        raise SystemExit("完整年度不足 3 年")
    T = D.get("ttm") or {}
    base = ticker.upper().replace(".TWO", "").replace(".TW", "")
    vdate = _d(D["valuation_date"])
    bsdate = _d(D["balance_sheet_date"])
    last_fy = _d(ys[-1])
    # 橋接年 = 最後完整年度的下一個會計年度；預測 = 其後 5 年
    fy_ends = [_add_years(last_fy, k) for k in range(1, 7)]  # bridge + 5 proj
    cur = D["currency"]
    sym = "NT$" if cur == "TWD" else ("$" if cur == "USD" else f"{cur} ")
    PX = f'"{sym}"#,##0.00;("{sym}"#,##0.00);"-"'
    UNIT = f"{cur} bn"
    SRC = D["source"]
    ASOF = D["as_of"]
    name = D.get("name_en") or D.get("name") or ticker
    if D.get("name") and D.get("name") != name:
        name = f"{D['name']} {name}"

    AS = assumptions(D, tg, gcap)
    WI = wacc_inputs(D, rf_arg, erp_arg)

    OUT = ROOT / "models" / f"{base}_DCF_{vdate.isoformat()}.xlsx"
    SUMMARY = ROOT / "models" / f"{base}_DCF_summary.json"

    wb = Workbook()
    ws = wb.active
    ws.title = "DCF"
    wc = wb.create_sheet("WACC")

    # ---------------- locked layout ----------------
    COLS = "BCDEFGHIJKLMNOP"
    HCOL = list(COLS[:nh])
    BCOL = COLS[nh]
    PCOL = list(COLS[nh + 1:nh + 6])
    LASTC = PCOL[-1]
    TTMC = COLS[nh + 7]
    ACOL = ["B", "C", "D", "E", "F"]
    YEARS_H = [f"{y[:4]}A" for y in ys]
    Y_B = f"FY{fy_ends[0].year}E"
    YEARS_P = [f"FY{d.year}E" for d in fy_ends[1:]]

    R_SEL, R_CASE = 4, 5
    R_MKT = 7   # 8 price 9 shares 10 mcap 11 debt 12 cash 13 net debt 14 val date 15 BS date 16 last FY end
    R_BR = 18   # 19 col hdr, 20.. bridge assumptions
    BR = {k: R_BR + 2 + i for i, k in enumerate(["g", "m", "tax", "da", "capex", "nwc"])}
    BLOCKS = {"BEAR": 27, "BASE": 39, "BULL": 51, "SELECTED": 63}
    ASSUMP = ["Revenue Growth (%)", "EBIT Margin (%)", "Tax Rate (%)", "D&A (% of Revenue)",
              "CapEx (% of Revenue)", "Δ NWC (% of Δ Revenue)", "Terminal Growth Rate",
              "WACC Adjustment vs. WACC sheet", "Terminal CapEx (× terminal D&A)"]
    OFF = {k: i + 2 for i, k in enumerate(["g", "m", "tax", "da", "capex", "nwc", "tg", "wadj", "tcx"])}
    SINGLE = ("tg", "wadj", "tcx")
    SEL = BLOCKS["SELECTED"]
    R_WACC_SEL = SEL + 11  # 74
    R_FIN = 76
    R = dict(rev=78, g=79, gp=80, gm=81, ebit=82, em=83, tax=84, tr=85, nopat=86,
             da=88, dap=89, capex=90, cxp=91, nwc=92, nwcp=93, fcf=94, fcfm=95)
    R_DCF = 97   # 98 hdr, 99 FY end, 100 FCF, 101 frac, 102 period, 103 DF, 104 PV
    RD = dict(fye=99, fcf=100, frac=101, per=102, df=103, pv=104)
    R_TV = 106
    R_VAL = 118
    R_SCN = 131
    R_S1, R_S2, R_S3 = 138, 148, 158

    ws.column_dimensions["A"].width = 44
    for col in COLS:
        ws.column_dimensions[col].width = 13
    ws.column_dimensions[TTMC].width = 15
    wc.column_dimensions["A"].width = 44
    for col in "BCD":
        wc.column_dimensions[col].width = 16

    # ---------------- header ----------------
    ws["A1"] = f"{name} — DCF Model"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = (f"Ticker: {ticker} | Valuation date: {vdate} | Fiscal year end: {last_fy.strftime('%m-%d')} | "
                f"Units: {UNIT} (per-share in {cur}) | Generated by tools/dcf_build.py")
    ws["A3"] = ("Mid-year convention with actual valuation date: period = MAX(0,(FY end − valuation date)/365.25 − counted "
                "fraction/2); only cash flows after the balance-sheet date are counted (net debt is as of that date).")
    ws["A3"].font = Font(italic=True, size=9)
    ws[f"A{R_SEL}"] = "Case Selector (1=Bear, 2=Base, 3=Bull)"
    inp(ws, f"B{R_SEL}", 2, "0", f"Source: 預設基本情境（{ASOF} 自動產生），可改 1/2/3 切換情境")
    ws[f"A{R_CASE}"] = "Active Case"
    calc(ws, f"B{R_CASE}", f'=CHOOSE($B${R_SEL},"Bear","Base","Bull")', "@", bold=True)

    # ---------------- market data ----------------
    header(ws, R_MKT, "MARKET DATA & KEY INPUTS", "A", "C")
    labels = ["Current Stock Price", "Shares Outstanding (bn)", f"Market Cap ({UNIT})", f"Total Debt ({UNIT})",
              f"Cash & ST Investments ({UNIT})", f"Net Debt / (Net Cash) ({UNIT})", "Valuation Date",
              "Balance Sheet Date (debt & cash as of)", "Last Reported Fiscal Year End"]
    for i, t in enumerate(labels):
        ws[f"A{R_MKT + 1 + i}"] = t
    inp(ws, "B8", D["price"], PX, f"Source: {D['price_source']}，價格日期 {D['price_date']}")
    inp(ws, "B9", D["shares_outstanding"] / BN, "0.0000", f"Source: {D['shares_source']}，抓取日 {ASOF}")
    calc(ws, "B10", "=B8*B9", NUM1)
    inp(ws, "B11", (D["total_debt"] or 0) / BN, NUM1,
        f"Source: {SRC}，{D['balance_sheet_date']}；定義：{D['debt_definition']}"
        + ("" if D["total_debt"] is not None else "——抓不到，暫以 0 計，待查證"))
    inp(ws, "B12", (D["total_cash"] or 0) / BN, NUM1,
        f"Source: {SRC}，{D['balance_sheet_date']}；定義：{D['cash_definition']}"
        + ("" if D["total_cash"] is not None else "——抓不到，暫以 0 計，待查證"))
    calc(ws, "B13", "=B11-B12", NUM1)
    inp(ws, "B14", vdate, DATE, "Source: 評價日＝模型產生日（data/dcf_inputs 的 valuation_date）")
    inp(ws, "B15", bsdate, DATE, f"Source: 最近一期財報資產負債表日（{SRC}）")
    inp(ws, "B16", last_fy, DATE, f"Source: 最近一個完整會計年度結束日（{SRC}）")
    box(ws, f"A{R_MKT}:C16", "medium")

    # ---------------- bridge-year assumptions ----------------
    header(ws, R_BR, f"CURRENT-YEAR BRIDGE ({Y_B}) — not scenario-dependent; only post-balance-sheet portion is valued",
           "A", LASTC)
    subhdr(ws, R_BR + 1, ["Assumption", Y_B, "Basis"])
    br_vals = {"g": AS["bridge_g"], "m": AS["bridge_m"], "tax": AS["tax"], "da": AS["da"], "capex": AS["capex"],
               "nwc": AS["nwc"]}
    br_notes = {"g": None, "m": AS["notes"]["bridge_m"], "tax": AS["notes"]["tax"], "da": AS["notes"]["da"],
                "capex": AS["notes"]["capex"], "nwc": AS["notes"]["nwc"]}
    ytd = D.get("current_year_ytd")
    e0 = ((D.get("estimates") or {}).get("revenue") or {}).get("0y") or {}
    br_notes["g"] = AS["notes"]["bridge_g"]
    basis = {"g": "analyst '0y' estimate" if e0.get("growth") is not None else ("YTD monthly revenue YoY" if ytd else "TTM vs last FY"),
             "m": "TTM EBITA margin" if AS["ebita_mode"] else "TTM EBIT margin", "tax": "3Y effective tax rate",
             "da": "3Y avg depreciation % rev" if AS["ebita_mode"] else "3Y avg D&A % of revenue",
             "capex": "3Y avg % of revenue", "nwc": "3Y median BS ΔNWC / ΔRevenue"}
    for i, (k, lab) in enumerate(zip(BR, ASSUMP[:6])):
        r = BR[k]
        ws[f"A{r}"] = lab
        inp(ws, f"B{r}", br_vals[k], PCT, br_notes[k])
        ws[f"C{r}"] = basis[k]
        ws[f"C{r}"].font = Font(italic=True, size=9)
    box(ws, f"A{R_BR}:{LASTC}{BR['nwc']}", "thin")

    # ---------------- scenario blocks ----------------
    SCEN = {}
    for case in ("BEAR", "BASE", "BULL"):
        SCEN[case] = dict(g=AS["g"][case], m=[AS["m"][case]] * 5, tax=[AS["tax"]] * 5, da=[AS["da"]] * 5,
                          capex=list(AS["capex_path"]), nwc=[AS["nwc"]] * 5, tg=tg, wadj=0.0, tcx=TERM_CAPEX_RATIO)
    common_notes = {
        "tax": AS["notes"]["tax"], "da": AS["notes"]["da"], "capex": AS["notes"]["capex"], "nwc": AS["notes"]["nwc"],
        "tg": f"Source: 永續成長率參數 --tg（預設 2.5%，本次 {_pct(tg)}），三情境相同；須低於 WACC",
        "wadj": "Source: 三情境共用 WACC sheet 的 WACC（調整 0）；如需情境別風險溢酬可手動修改",
        "tcx": ("Source: 沿用 build_dcf_2330.py 原設計——永續期資本支出 = 終值年 D&A × 1.25"
                "（成長所需的淨再投資；可對照下方 reinvestment rate／return on new capital 檢查）"),
    }
    for nm, top in BLOCKS.items():
        title = f"{nm} CASE ASSUMPTIONS" if nm != "SELECTED" else "SELECTED CASE (drives model) — CHOOSE on case selector"
        header(ws, top, title, "A", "F")
        subhdr(ws, top + 1, ["Assumption"] + YEARS_P)
        for i, lab in enumerate(ASSUMP):
            ws[f"A{top + 2 + i}"] = lab
        for key, off in OFF.items():
            r = top + off
            cols = ACOL if key not in SINGLE else ["B"]
            fmt = "0.00\"x\"" if key == "tcx" else PCT
            for j, col in enumerate(cols):
                if nm == "SELECTED":
                    refs = ",".join(f"{col}{BLOCKS[b] + off}" for b in ("BEAR", "BASE", "BULL"))
                    calc(ws, f"{col}{r}", f"=CHOOSE($B${R_SEL},{refs})", fmt)
                elif key == "capex" and j > 0:
                    c0, da5, tcx = f"$B${r}", f"$F${top + OFF['da']}", f"$B${top + OFF['tcx']}"
                    calc(ws, f"{col}{r}", f"={c0}+({da5}*{tcx}-{c0})*{j}/4", fmt)
                else:
                    v = SCEN[nm][key]
                    srcn = AS["notes"][key][nm] if key in ("g", "m") else common_notes[key]
                    inp(ws, f"{col}{r}", v[j] if isinstance(v, list) else v, fmt, srcn)
        box(ws, f"A{top}:F{top + 10}", "thin")
    ws[f"A{R_WACC_SEL}"] = "WACC (selected) = WACC sheet + adjustment"
    calc(ws, f"B{R_WACC_SEL}", f"=WACC!$B$29+B{SEL + OFF['wadj']}", PCT, out=True)
    box(ws, f"A{BLOCKS['BEAR']}:F{R_WACC_SEL}", "medium")

    def aref(block, key, j=0):
        col = "B" if key in SINGLE else ACOL[j]
        return f"${col}${BLOCKS[block] + OFF[key]}"

    sel = lambda key, j=0: aref("SELECTED", key, j)  # noqa: E731
    TG, W = f"$B${SEL + OFF['tg']}", f"$B${R_WACC_SEL}"
    bref = lambda k: f"$B${BR[k]}"  # noqa: E731

    # ---------------- historical & projected financials ----------------
    header(ws, R_FIN, f"HISTORICAL & PROJECTED FINANCIALS ({UNIT})", "A", TTMC)
    subhdr(ws, R_FIN + 1, ["Income Statement / FCF"] + YEARS_H + [f"{Y_B} (bridge)"] + YEARS_P + ["", "TTM (memo)"])
    flabels = {R["rev"]: "Revenue", R["g"]: "  % growth", R["gp"]: "Gross Profit (memo)", R["gm"]: "  % gross margin",
               R["ebit"]: "EBIT (Operating Income)" + ("; forecast = EBITA (ex acquired amortization)" if AS["ebita_mode"] else ""), R["em"]: "  % EBIT margin", R["tax"]: "(-) Taxes on EBIT",
               R["tr"]: "  tax rate", R["nopat"]: "NOPAT", R["da"]: "(+) D&A" + ("; forecast = depreciation only" if AS["ebita_mode"] else ""), R["dap"]: "  % of revenue",
               R["capex"]: "(-) CapEx", R["cxp"]: "  % of revenue", R["nwc"]: "(-) Δ NWC (increase = use of cash)",
               R["nwcp"]: "  % of Δ revenue", R["fcf"]: "Unlevered Free Cash Flow", R["fcfm"]: "  % FCF margin"}
    for r, t in flabels.items():
        ws[f"A{r}"] = t
        if t.startswith("  "):
            ws[f"A{r}"].font = Font(italic=True)
    ws[f"A{R['fcf']}"].font = Font(bold=True)

    def hist_col(col, a, label):
        def put(row, key, sign=1, what=""):
            v = a.get(key)
            if v is None:
                inp(ws, f"{col}{row}", 0, NUM1, f"Source: {SRC} {label} {what} — 資料缺漏，以 0 計（待查證）")
            else:
                inp(ws, f"{col}{row}", sign * v / BN, NUM1, f"Source: {SRC}, {label} {what}")
        put(R["rev"], "revenue", what="營收 Revenue")
        put(R["gp"], "gross_profit", what="毛利 Gross Profit")
        put(R["ebit"], "operating_income", what="營業利益 Operating Income")
        tr = a.get("tax_rate")
        inp(ws, f"{col}{R['tr']}", tr if tr is not None else 0, PCT,
            f"Source: {SRC}, {label} 有效稅率 = 所得稅 / 稅前淨利" + ("" if tr is not None else "——缺漏以 0 計"))
        put(R["da"], "d_and_a", what="折舊＋攤銷 D&A（現金流量表）")
        put(R["capex"], "capex", -1, "資本支出 CapEx（購置不動產廠房設備，正負號已反轉）")
        if a.get("delta_op_nwc") is not None:
            put(R["nwc"], "delta_op_nwc", 1, "營業營運資金（應收＋存貨−應付，資產負債表）年增加（+ = 資金流出）")
        else:
            put(R["nwc"], "change_in_working_capital", -1,
                "營運資金變動（現金流量表；無資產負債表數字時的替代；正負號已反轉：+ = 資金流出）")
        calc(ws, f"{col}{R['gm']}", f"=IFERROR({col}{R['gp']}/{col}{R['rev']},0)", PCT)
        calc(ws, f"{col}{R['em']}", f"={col}{R['ebit']}/{col}{R['rev']}", PCT)
        calc(ws, f"{col}{R['tax']}", f"={col}{R['ebit']}*{col}{R['tr']}", NUM1)
        calc(ws, f"{col}{R['nopat']}", f"={col}{R['ebit']}-{col}{R['tax']}", NUM1)
        calc(ws, f"{col}{R['dap']}", f"={col}{R['da']}/{col}{R['rev']}", PCT)
        calc(ws, f"{col}{R['cxp']}", f"={col}{R['capex']}/{col}{R['rev']}", PCT)
        calc(ws, f"{col}{R['fcf']}", f"={col}{R['nopat']}+{col}{R['da']}-{col}{R['capex']}-{col}{R['nwc']}", NUM1, bold=True)
        calc(ws, f"{col}{R['fcfm']}", f"={col}{R['fcf']}/{col}{R['rev']}", PCT)

    for col, y in zip(HCOL, ys):
        hist_col(col, A[y], f"FY{y[:4]}（年度，期末 {y}）")
    for i in range(1, nh):
        c, p = HCOL[i], HCOL[i - 1]
        calc(ws, f"{c}{R['g']}", f"={c}{R['rev']}/{p}{R['rev']}-1", PCT)
        calc(ws, f"{c}{R['nwcp']}", f"=IFERROR({c}{R['nwc']}/({c}{R['rev']}-{p}{R['rev']}),0)", PCT)
    if T.get("revenue"):
        hist_col(TTMC, T, f"TTM {T.get('period', '')}")
        ws[f"{TTMC}{R['g']}"] = "memo"
        ws[f"{TTMC}{R['g']}"].font = Font(italic=True, size=9)

    # bridge column
    lastH = HCOL[-1]
    bc = BCOL
    calc(ws, f"{bc}{R['rev']}", f"={lastH}{R['rev']}*(1+{bref('g')})", NUM1)
    calc(ws, f"{bc}{R['ebit']}", f"={bc}{R['rev']}*{bref('m')}", NUM1)
    calc(ws, f"{bc}{R['tax']}", f"={bc}{R['ebit']}*{bref('tax')}", NUM1)
    calc(ws, f"{bc}{R['da']}", f"={bc}{R['rev']}*{bref('da')}", NUM1)
    calc(ws, f"{bc}{R['capex']}", f"={bc}{R['rev']}*{bref('capex')}", NUM1)
    calc(ws, f"{bc}{R['nwc']}", f"=({bc}{R['rev']}-{lastH}{R['rev']})*{bref('nwc')}", NUM1)
    prev = lastH
    for pc, j in zip(PCOL, range(5)):
        calc(ws, f"{pc}{R['rev']}", f"={prev if pc != PCOL[0] else bc}{R['rev']}*(1+{sel('g', j)})", NUM1)
        calc(ws, f"{pc}{R['ebit']}", f"={pc}{R['rev']}*{sel('m', j)}", NUM1)
        calc(ws, f"{pc}{R['tax']}", f"={pc}{R['ebit']}*{sel('tax', j)}", NUM1)
        calc(ws, f"{pc}{R['da']}", f"={pc}{R['rev']}*{sel('da', j)}", NUM1)
        calc(ws, f"{pc}{R['capex']}", f"={pc}{R['rev']}*{sel('capex', j)}", NUM1)
        p_ = bc if pc == PCOL[0] else prev
        calc(ws, f"{pc}{R['nwc']}", f"=({pc}{R['rev']}-{p_}{R['rev']})*{sel('nwc', j)}", NUM1)
        prev = pc
    prev = lastH
    for pc in [bc] + PCOL:
        calc(ws, f"{pc}{R['g']}", f"={pc}{R['rev']}/{prev}{R['rev']}-1", PCT)
        calc(ws, f"{pc}{R['em']}", f"={pc}{R['ebit']}/{pc}{R['rev']}", PCT)
        calc(ws, f"{pc}{R['tr']}", f"=IFERROR({pc}{R['tax']}/{pc}{R['ebit']},0)", PCT)
        calc(ws, f"{pc}{R['nopat']}", f"={pc}{R['ebit']}-{pc}{R['tax']}", NUM1)
        calc(ws, f"{pc}{R['dap']}", f"={pc}{R['da']}/{pc}{R['rev']}", PCT)
        calc(ws, f"{pc}{R['cxp']}", f"={pc}{R['capex']}/{pc}{R['rev']}", PCT)
        calc(ws, f"{pc}{R['nwcp']}", f"=IFERROR({pc}{R['nwc']}/({pc}{R['rev']}-{prev}{R['rev']}),0)", PCT)
        calc(ws, f"{pc}{R['fcf']}", f"={pc}{R['nopat']}+{pc}{R['da']}-{pc}{R['capex']}-{pc}{R['nwc']}", NUM1, bold=True)
        calc(ws, f"{pc}{R['fcfm']}", f"={pc}{R['fcf']}/{pc}{R['rev']}", PCT)
        prev = pc
    box(ws, f"A{R_FIN}:{TTMC}{R['fcfm']}", "medium")
    box(ws, f"B{R_FIN + 1}:{lastH}{R['fcfm']}", "thin")

    # ---------------- discounting ----------------
    DC = [bc] + PCOL
    header(ws, R_DCF, "DCF VALUATION — mid-year convention, discounted to the actual valuation date", "A", LASTC)
    subhdr(ws, R_DCF + 1, [""] + [""] * (nh) + [f"{Y_B} (bridge)"] + YEARS_P)
    ws[f"A{RD['fye']}"], ws[f"A{RD['fcf']}"], ws[f"A{RD['frac']}"] = (
        "Fiscal Year End", f"Unlevered FCF ({UNIT})", "Fraction of year counted (after balance-sheet date)")
    ws[f"A{RD['per']}"], ws[f"A{RD['df']}"], ws[f"A{RD['pv']}"] = (
        "Discount Period (years from valuation date, mid-point)", "Discount Factor", f"PV of counted FCF ({UNIT})")
    for i, pc in enumerate(DC):
        prevd = "$B$16" if i == 0 else f"{DC[i - 1]}{RD['fye']}"
        calc(ws, f"{pc}{RD['fye']}", f"=EDATE({prevd},12)", DATE)
        calc(ws, f"{pc}{RD['fcf']}", f"={pc}{R['fcf']}", NUM1)
        calc(ws, f"{pc}{RD['frac']}", f"=MAX(0,MIN(1,({pc}{RD['fye']}-$B$15)/365.25))", "0.000")
        calc(ws, f"{pc}{RD['per']}", f"=MAX(0,({pc}{RD['fye']}-$B$14)/365.25-{pc}{RD['frac']}/2)", "0.000")
        calc(ws, f"{pc}{RD['df']}", f"=1/(1+{W})^{pc}{RD['per']}", "0.0000")
        calc(ws, f"{pc}{RD['pv']}", f"={pc}{RD['fcf']}*{pc}{RD['frac']}*{pc}{RD['df']}", NUM1)
    box(ws, f"A{R_DCF}:{LASTC}{RD['pv']}", "medium")

    header(ws, R_TV, "TERMINAL VALUE — perpetuity growth on a normalised steady-state year", "A", "C")
    t = R_TV
    L = LASTC
    tv_rows = [
        (f"Terminal-year Revenue ({YEARS_P[-1]} × (1+g))", f"={L}{R['rev']}*(1+{TG})", NUM1, False),
        (f"NOPAT ({YEARS_P[-1]} margin & tax)", f"=B{t + 1}*{sel('m', 4)}*(1-{sel('tax', 4)})", NUM1, False),
        (f"(+) D&A ({YEARS_P[-1]} % of revenue)", f"=B{t + 1}*{sel('da', 4)}", NUM1, False),
        ("(-) Normalised CapEx (= D&A × terminal ratio)", f"=B{t + 3}*{sel('tcx')}", NUM1, False),
        ("(-) Δ NWC", f"=(B{t + 1}-{L}{R['rev']})*{sel('nwc', 4)}", NUM1, False),
        (f"Terminal FCF ({UNIT})", f"=B{t + 2}+B{t + 3}-B{t + 4}-B{t + 5}", NUM1, True),
        (f"Terminal Value at {YEARS_P[-1]} ({UNIT})", f"=B{t + 6}/({W}-{TG})", NUM1, False),
        (f"PV of Terminal Value ({UNIT}) — discounted at last mid-year period", f"=B{t + 7}/(1+{W})^{L}{RD['per']}", NUM1, False),
        ("Check: reinvestment rate (net capex + ΔNWC) / NOPAT", f"=(B{t + 4}-B{t + 3}+B{t + 5})/B{t + 2}", PCT, False),
        ("Check: implied return on new capital (g ÷ reinvestment)", f"=IFERROR({TG}/B{t + 9},0)", PCT, False),
    ]
    for i, (lab, f, fmt, bold) in enumerate(tv_rows, start=1):
        ws[f"A{t + i}"] = lab
        calc(ws, f"B{t + i}", f, fmt, bold=bold)
    box(ws, f"A{R_TV}:C{R_TV + len(tv_rows)}", "medium")

    header(ws, R_VAL, "VALUATION SUMMARY (selected case)", "A", "C")
    v = R_VAL
    rows = [(f"Sum of PV of FCFs ({UNIT})", f"=SUM({bc}{RD['pv']}:{L}{RD['pv']})", NUM1, False),
            (f"PV of Terminal Value ({UNIT})", f"=B{R_TV + 8}", NUM1, False),
            (f"Enterprise Value ({UNIT})", f"=B{v + 1}+B{v + 2}", NUM1, True),
            (f"(-) Net Debt / (+) Net Cash ({UNIT})", "=B13", NUM1, False),
            (f"Equity Value ({UNIT})", f"=B{v + 3}-B{v + 4}", NUM1, True),
            ("Shares Outstanding (bn)", "=B9", "0.0000", False),
            (f"IMPLIED PRICE PER SHARE ({cur})", f"=B{v + 5}/B{v + 6}", PX, True),
            (f"Current Stock Price ({cur})", "=B8", PX, False),
            ("Implied Upside / (Downside)", f"=B{v + 7}/B{v + 8}-1", PCT, True),
            ("Terminal Value as % of EV", f"=B{v + 2}/B{v + 3}", PCT, False),
            (f"Implied EV / {YEARS_H[-1]} EBITDA", f"=B{v + 3}/({lastH}{R['ebit']}+{lastH}{R['da']})", '0.0"x"', False)]
    for i, (lab, f, fmt, out) in enumerate(rows, start=1):
        ws[f"A{v + i}"] = lab
        calc(ws, f"B{v + i}", f, fmt, out=out)
    R_PRICE = v + 7
    box(ws, f"A{R_VAL}:C{v + len(rows)}", "medium")

    # ---------------- full-DCF formula builders ----------------
    FR = {pc: f"${pc}${RD['frac']}" for pc in DC}
    PR = {pc: f"${pc}${RD['per']}" for pc in DC}
    REV_B, FCF_B = f"${bc}${R['rev']}", f"${bc}${R['fcf']}"

    def term_fcf(block, rev_t, g, dm="0"):
        m, tax, da, nwc = (aref(block, k, 4) for k in ("m", "tax", "da", "nwc"))
        return (f"({rev_t}*(1+{g})*(({m}+{dm})*(1-{tax})+{da}*(1-{aref(block, 'tcx')}))"
                f"-{rev_t}*{g}*{nwc})")

    def price_block(block, w, g, dg="0", dm="0"):
        """完整重算：橋接年 FCF 取模型列；5 年預測依該情境區塊假設（dg/dm 疊加在每一年）。"""
        rev, fcfs = [], []
        for j in range(5):
            growth = "*".join(f"(1+{aref(block, 'g', a)}+{dg})" for a in range(j + 1))
            rev.append(f"({REV_B}*{growth})")
            prev_rev = REV_B if j == 0 else rev[j - 1]
            fcfs.append(f"({rev[j]}*(({aref(block, 'm', j)}+{dm})*(1-{aref(block, 'tax', j)})+{aref(block, 'da', j)}"
                        f"-{aref(block, 'capex', j)})-({rev[j]}-{prev_rev})*{aref(block, 'nwc', j)})")
        pv = f"{FCF_B}*{FR[bc]}/(1+{w})^{PR[bc]}+" + "+".join(
            f"{fcfs[j]}*{FR[PCOL[j]]}/(1+{w})^{PR[PCOL[j]]}" for j in range(5))
        tv = f"{term_fcf(block, rev[4], g, dm)}/({w}-{g})/(1+{w})^{PR[L]}"
        return f"=({pv}+{tv}-$B$13)/$B$9"

    FCF_RNG = f"${bc}${RD['fcf']}:${L}${RD['fcf']}"
    FRAC_RNG = f"${bc}${RD['frac']}:${L}${RD['frac']}"
    PER_RNG = f"${bc}${RD['per']}:${L}${RD['per']}"

    def price_from_wacc(w, g):
        return (f"=(SUMPRODUCT({FCF_RNG}*{FRAC_RNG}/(1+{w})^{PER_RNG})"
                f"+{term_fcf('SELECTED', f'${L}${R['rev']}', g)}/({w}-{g})/(1+{w})^{PR[L]}-$B$13)/$B$9")

    # ---------------- scenario summary (live formulas for all three cases) ----------------
    header(ws, R_SCN, "SCENARIO SUMMARY — full DCF for each case (independent of selector)", "A", "E")
    subhdr(ws, R_SCN + 1, ["Case", "Implied Price", "Upside", "WACC used", "Terminal g"])
    for i, case in enumerate(("BEAR", "BASE", "BULL")):
        r = R_SCN + 2 + i
        ws[f"A{r}"] = case.title()
        w_expr = f"(WACC!$B$29+{aref(case, 'wadj')})"
        calc(ws, f"B{r}", price_block(case, w_expr, aref(case, "tg")), PX, out=(case == "BASE"))
        calc(ws, f"C{r}", f"=B{r}/$B$8-1", PCT)
        calc(ws, f"D{r}", f"={w_expr}", PCT)
        calc(ws, f"E{r}", f"={aref(case, 'tg')}", PCT)
    ws[f"A{R_SCN + 5}"] = "Check: selected case price − scenario table (should be 0)"
    calc(ws, f"B{R_SCN + 5}", f"=B{R_PRICE}-INDEX(B{R_SCN + 2}:B{R_SCN + 4},$B${R_SEL})", PX)
    box(ws, f"A{R_SCN}:E{R_SCN + 5}", "medium")

    # ---------------- sensitivity tables ----------------
    def sens_table(top, title, row_lab, col_lab, row_center, col_center, row_step, col_step, cell_formula,
                   fmt_r, fmt_c, step_src):
        header(ws, top, title, "A", "G")
        ws[f"A{top + 1}"] = f"Rows: {row_lab} | Columns: {col_lab} | Output: implied price per share ({cur})"
        ws[f"A{top + 1}"].font = Font(italic=True)
        ws[f"I{top + 1}"], ws[f"I{top + 2}"] = "Row step", "Col step"
        inp(ws, f"J{top + 1}", row_step, fmt_r, step_src)
        inp(ws, f"J{top + 2}", col_step, fmt_c, step_src)
        hr = top + 2
        ws[f"B{hr}"] = f"{row_lab} ↓ / {col_lab} →"
        ws[f"B{hr}"].font, ws[f"B{hr}"].fill = Font(bold=True, size=8), F_SUB
        cols = ["C", "D", "E", "F", "G"]
        for j, cc in enumerate(cols):
            calc(ws, f"{cc}{hr}", f"={col_center}+({j - 2})*$J${top + 2}", fmt_c, bold=True).fill = F_SUB
        for i in range(5):
            r = hr + 1 + i
            calc(ws, f"B{r}", f"={row_center}+({i - 2})*$J${top + 1}", fmt_r, bold=True).fill = F_SUB
            for cc in cols:
                calc(ws, f"{cc}{r}", cell_formula(f"$B{r}", f"{cc}${hr}"), PX)
        center = ws[f"E{hr + 3}"]
        center.fill, center.font = F_OUT, Font(bold=True)
        ws.conditional_formatting.add(f"C{hr + 1}:G{hr + 5}", ColorScaleRule(
            start_type="min", start_color="F8696B", mid_type="percentile", mid_value=50,
            mid_color="FFFFFF", end_type="max", end_color="63BE7B"))
        box(ws, f"A{top}:G{hr + 5}", "medium")

    STEP_SRC = "Source: Sensitivity step size (analyst choice)"
    sens_table(R_S1, "SENSITIVITY 1 — WACC vs Terminal Growth", "WACC", "Terminal g",
               W, TG, 0.005, 0.005, lambda r, c: price_from_wacc(r, c), PCT, PCT, STEP_SRC)
    sens_table(R_S2, "SENSITIVITY 2 — Revenue Growth vs EBIT Margin (shift applied to all 5 forecast years)",
               "Δ growth (pp)", "Δ EBIT margin (pp)", "0", "0", 0.02, 0.02,
               lambda r, c: price_block("SELECTED", W, TG, r, c), PCT, PCT, STEP_SRC)

    def price_beta_rf(beta, rf):
        w = f"(WACC!$B$26*({rf}+{beta}*WACC!$B$6)+WACC!$B$27*WACC!$C$27+{sel('wadj')})"
        return price_from_wacc(w, TG)

    sens_table(R_S3, "SENSITIVITY 3 — Beta vs Risk-Free Rate", "Beta", "Risk-free rate",
               "WACC!$B$5", "WACC!$B$4", 0.10, 0.0025, price_beta_rf, '0.00', '0.00%', STEP_SRC)

    # ---------------- WACC sheet ----------------
    wc["A1"] = f"{name} ({ticker}) — Cost of Capital ({cur})"
    wc["A1"].font = Font(bold=True, size=14)
    header(wc, 3, "COST OF EQUITY (CAPM)", "A", "D")
    rf_lab = "Risk-Free Rate (Taiwan 10Y govt bond) — 待查證" if D["market"] == "TW" else "Risk-Free Rate (US 10Y Treasury, ^TNX)"
    wc["A4"], wc["A5"], wc["A6"], wc["A7"] = (rf_lab, f"Beta (Blume-adjusted, clamped {BETA_MIN}–{BETA_MAX})",
                                              "Equity Risk Premium", "Cost of Equity")
    inp(wc, "B4", WI["rf"], '0.00%', WI["rf_note"])
    inp(wc, "B5", WI["beta"], '0.000', WI["beta_note"])
    inp(wc, "B6", WI["erp"], '0.00%', WI["erp_note"])
    calc(wc, "B7", "=B4+B5*B6", '0.00%', bold=True)
    if WI["beta_raw"] is not None:
        wc["C5"] = f"raw {WI['beta_raw']:.3f} → Blume {WI['beta_blume']:.3f}"
        wc["C5"].font = Font(italic=True, size=9)
    box(wc, "A3:D7", "medium")

    header(wc, 9, "COST OF DEBT", "A", "D")
    wc["A10"], wc["A11"], wc["A12"], wc["A13"] = ("Method", "Pre-Tax Cost of Debt", "Tax Rate (selected case, FY1)",
                                                  "After-Tax Cost of Debt")
    inp(wc, "B10", WI["kd_method"], "@", "Source: 舉債成本估算方法（利息費用 / 平均負債；抓不到用 rf + 1%）")
    inp(wc, "B11", WI["kd"], '0.00%', WI["kd_note"])
    link(wc, "B12", f"=DCF!$B${SEL + OFF['tax']}", '0.0%')
    calc(wc, "B13", "=B11*(1-B12)", '0.00%', bold=True)
    box(wc, "A9:D13", "medium")

    header(wc, 15, f"CAPITAL STRUCTURE (market values, {UNIT})", "A", "D")
    for r, lab, f, fmt in [(16, f"Current Stock Price ({cur})", "=DCF!$B$8", PX), (17, "Shares Outstanding (bn)", "=DCF!$B$9", '0.0000'),
                           (19, "Total Debt", "=DCF!$B$11", NUM1), (20, "Cash & ST Investments", "=DCF!$B$12", NUM1)]:
        wc[f"A{r}"] = lab
        link(wc, f"B{r}", f, fmt)
    wc["A18"], wc["A21"], wc["A22"] = "Market Capitalization", "Net Debt / (Net Cash)", "Enterprise Value"
    calc(wc, "B18", "=B16*B17", NUM1)
    calc(wc, "B21", "=B19-B20", NUM1)
    calc(wc, "B22", "=B18+B21", NUM1)
    box(wc, "A15:D22", "medium")

    header(wc, 24, "WACC CALCULATION (market-value weights on gross debt)", "A", "D")
    subhdr(wc, 25, ["Component", "Weight", "Cost", "Contribution"])
    wc["A26"], wc["A27"] = "Equity", "Debt"
    calc(wc, "B26", "=B18/(B18+B19)", PCT)
    calc(wc, "C26", "=B7", '0.00%')
    calc(wc, "D26", "=B26*C26", '0.00%')
    calc(wc, "B27", "=B19/(B18+B19)", PCT)
    calc(wc, "C27", "=B13", '0.00%')
    calc(wc, "D27", "=B27*C27", '0.00%')
    wc["A29"] = "WEIGHTED AVERAGE COST OF CAPITAL"
    wc["A29"].font = Font(bold=True)
    calc(wc, "B29", "=D26+D27", '0.00%', out=True)
    box(wc, "A24:D29", "medium")

    ws.freeze_panes = "B4"
    OUT.parent.mkdir(exist_ok=True)
    wb.save(OUT)

    # ---------------- Python 重算 → 摘要 ----------------
    def yfrac(d: date) -> float:
        return (d - bsdate).days / 365.25

    frac = [_clamp(yfrac(d), 0, 1) for d in fy_ends]
    per = [max(0.0, (d - vdate).days / 365.25 - f / 2) for d, f in zip(fy_ends, frac)]
    P = {"scen": SCEN, "bridge": br_vals, "last_rev": A[ys[-1]]["revenue"] / BN, "frac": frac, "per": per,
         "net_debt": ((D["total_debt"] or 0) - (D["total_cash"] or 0)) / BN, "shares": D["shares_outstanding"] / BN,
         "price": D["price"], "debt": (D["total_debt"] or 0) / BN}
    wacc = wacc_python(P, WI, AS["tax"])
    res = {c: compute_scenario(P, c, wacc) for c in ("BEAR", "BASE", "BULL")}
    summary = {
        "ticker": ticker, "name": name, "currency": cur, "valuation_date": str(vdate),
        "price": D["price"], "price_date": D["price_date"], "price_source": D["price_source"], "model_file": str(OUT.relative_to(ROOT)).replace("\\", "/"),
        "fair_value_per_share": {c.lower(): round(res[c]["price"], 2) for c in res},
        "upside": {c.lower(): round(res[c]["price"] / D["price"] - 1, 4) for c in res},
        "terminal_value_pct_of_ev": {c.lower(): round(res[c]["tv_pct_ev"], 4) for c in res},
        "terminal_value_warning": (f"終值佔 EV 超過 {TV_WARN:.0%}（" + "、".join(
            f"{c.lower()} {res[c]['tv_pct_ev']:.1%}" for c in res if res[c]["tv_pct_ev"] > TV_WARN) + "），結果高度依賴永續假設")
        if any(res[c]["tv_pct_ev"] > TV_WARN for c in res) else None,
        "enterprise_value_bn": {c.lower(): round(res[c]["ev"], 2) for c in res},
        "wacc": round(wacc, 5),
        "wacc_inputs": {"rf": WI["rf"], "rf_source": WI["rf_note"], "beta": WI["beta"], "beta_raw": WI["beta_raw"],
                        "beta_blume": None if WI["beta_blume"] is None else round(WI["beta_blume"], 4),
                        "beta_method": f"Blume 0.67×raw+0.33, clamped {BETA_MIN}–{BETA_MAX}",
                        "erp": WI["erp"], "cost_of_debt_pre_tax": round(WI["kd"], 5), "cost_of_debt_method": WI["kd_method"],
                        "tax_rate": round(AS["tax"], 4)},
        "terminal_growth": tg,
        "assumptions": {
            "bridge_year": Y_B, "forecast_years": YEARS_P,
            "bridge_revenue_growth": round(AS["bridge_g"], 4),
            "revenue_growth": {c.lower(): [round(x, 4) for x in AS["g"][c]] for c in ("BEAR", "BASE", "BULL")},
            "first_year_growth_raw": round(AS["g1_raw"], 4), "first_year_growth_capped": AS["g1_capped"],
            "growth_cap": gcap, "hist_revenue_cagr": None if AS["cagr"] is None else round(AS["cagr"], 4),
            "ebit_margin": {c.lower(): round(AS["m"][c], 4) for c in ("BEAR", "BASE", "BULL")},
            "tax_rate": round(AS["tax"], 4), "d_and_a_pct_rev": round(AS["da"], 4),
            "margin_basis": "EBITA (acquired-intangible amortization excluded; D&A = depreciation only)" if AS["ebita_mode"]
            else "EBIT (GAAP/IFRS operating income)", "amortization_pct_rev_ttm": round(AS["amort_pct"], 4),
            "capex_pct_rev_path": [round(x, 4) for x in AS["capex_path"]], "capex_pct_rev_3y_avg": round(AS["capex"], 4),
            "nwc_pct_delta_rev": round(AS["nwc"], 4), "terminal_capex_x_da": TERM_CAPEX_RATIO,
            "discounting": "mid-year convention from actual valuation date; cash flows counted only after balance-sheet date "
                           f"({D['balance_sheet_date']}); TV discounted at final mid-year period",
        },
        "net_debt_bn": round(P["net_debt"], 2), "shares_bn": round(P["shares"], 4),
        "data_source": SRC,
        "calc_method": "Python replica of the Excel formulas (compute_scenario); verify against the Excel SCENARIO SUMMARY after recalc",
    }
    SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return OUT, SUMMARY, summary


def main() -> None:
    ap = argparse.ArgumentParser(description="通用 DCF 模型產生器")
    ap.add_argument("ticker")
    ap.add_argument("--rf", type=float, default=None, help="無風險利率（小數）；美股預設 ^TNX，台股預設 0.016（待查證）")
    ap.add_argument("--erp", type=float, default=None, help="股權風險溢酬；預設美股 0.05、台股 0.055")
    ap.add_argument("--tg", type=float, default=0.025, help="永續成長率，預設 0.025")
    ap.add_argument("--gcap", type=float, default=0.60, help="分析師預估營收成長上限（今年與第一個預測年），預設 0.60")
    ap.add_argument("--refresh", action="store_true", help="重新抓取 data/dcf_inputs/{TICKER}.json")
    a = ap.parse_args()
    out, summ, s = build(a.ticker.upper(), a.rf, a.erp, a.tg, a.gcap, a.refresh)
    print(out)
    print(summ)
    print(json.dumps({k: s[k] for k in ("price", "fair_value_per_share", "upside", "terminal_value_pct_of_ev", "wacc")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
