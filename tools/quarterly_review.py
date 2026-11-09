"""季檢討報告：把持股總覽、投資政策書、論點筆記、交易卡彙整成一則 Obsidian 筆記。

使用者只要讀這一則、在「決策區」打勾，就完成季檢討（約 1 小時）。

輸入：
  - 最新與最早的持股快照 private/portfolio/snapshots/*.json（請先跑 portfolio_exposure.py）
  - 投資政策書 private/portfolio/policy.toml
  - Obsidian：10 公司/*/*投資論點.md、00 投資系統/交易/*.md
輸出：Obsidian「00 投資系統/檢討/YYYY-MM 季檢討.md」（已存在時另存為 …（重產 HHMM）.md，不覆蓋你已經填寫的版本）

用法：python -X utf8 tools/quarterly_review.py
"""
from __future__ import annotations

import json
import re
import tomllib
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAP_DIR = ROOT / "private" / "portfolio" / "snapshots"
POLICY = ROOT / "private" / "portfolio" / "policy.toml"
VAULT = Path(r"C:\Users\Andy\Documents\obsidian\obsidian")
COMPANIES = VAULT / "10 公司"
CARDS = VAULT / "00 投資系統" / "交易"
OUT_DIR = VAULT / "00 投資系統" / "檢討"
LOOKBACK_DAYS = 92


def frontmatter(text: str) -> dict:
    if not text.startswith("---\n"):
        return {}
    fm = {}
    for line in text.split("\n---\n", 1)[0].split("\n")[1:]:
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def link(p: Path, table: bool = False) -> str:
    """Obsidian 連結；放在表格裡時別名的 | 要加反斜線跳脫"""
    sep = "\\|" if table else "|"
    return f"[[{p.relative_to(VAULT).with_suffix('').as_posix()}{sep}{p.stem.replace(' 投資論點', '')}]]"


def load_theses() -> list[dict]:
    out = []
    since = date.today() - timedelta(days=LOOKBACK_DAYS)
    for p in sorted(COMPANIES.glob("*/*投資論點.md")):
        text = p.read_text(encoding="utf-8")
        fm = frontmatter(text)
        if fm.get("held") != "true":
            continue
        events = []
        for m in re.finditer(r"^### (\d{4}-\d{2}-\d{2})｜([^｜\n]+)｜論點：([^\n]+)", text, re.M):
            try:
                d = date.fromisoformat(m.group(1))
            except ValueError:
                continue
            if d >= since:
                events.append({"date": m.group(1), "event": m.group(2).strip(), "verdict": m.group(3).strip()})
        out.append({"path": p, "fm": fm, "events": events,
                    "single_source_flags": text.count("⚠️")})
    return out


def load_cards() -> list[dict]:
    cards = []
    if CARDS.exists():
        for p in sorted(CARDS.glob("*.md")):
            fm = frontmatter(p.read_text(encoding="utf-8"))
            if "交易卡" in p.read_text(encoding="utf-8").split("\n---\n", 1)[0]:
                cards.append({"path": p, "fm": fm})
    return cards


def bench_return(symbol: str, start: str, end: str) -> float | None:
    """指數 ETF 在 start～end（含）之間的報酬；抓不到回傳 None"""
    try:
        import yfinance as yf
        s = date.fromisoformat(start)
        h = yf.Ticker(symbol).history(start=(s - timedelta(days=7)).isoformat(),
                                      end=(date.fromisoformat(end) + timedelta(days=1)).isoformat())["Close"].dropna()
        h.index = h.index.date
        before = h[h.index <= s]
        base = float(before.iloc[-1]) if len(before) else float(h.iloc[0])
        return float(h.iloc[-1]) / base - 1
    except Exception:  # noqa: BLE001
        return None


def status_icon(ok: bool | None) -> str:
    return "—" if ok is None else ("✅" if ok else "⚠️")


def build() -> str:
    snaps = sorted(SNAP_DIR.glob("*.json"))
    if not snaps:
        raise SystemExit("找不到持股快照，請先執行 python -X utf8 tools/portfolio_exposure.py")
    now = json.loads(snaps[-1].read_text(encoding="utf-8"))
    first = json.loads(snaps[0].read_text(encoding="utf-8"))
    pol = tomllib.loads(POLICY.read_text(encoding="utf-8")) if POLICY.exists() else {}
    conc, alloc, swing = pol.get("concentration", {}), pol.get("allocation", {}), pol.get("swing", {})
    transition = set(pol.get("transition", {}).get("tickers", []))
    rate = now["fx"]["TWD_per_USD"]
    price = {r["ticker"]: r["price"] for r in now["rows"]}
    theses, cards = load_theses(), load_cards()
    today = date.today()

    L = []
    L.append("---\ntags:\n  - 季檢討\n"
             f"date: {today.isoformat()}\ngenerated: {datetime.now():%Y-%m-%d %H:%M}\n---")
    L.append(f"# {today:%Y-%m} 季檢討")
    L.append("> 由 `tools/quarterly_review.py` 自動產生。上半部是數據，**最下面的「決策區」由你填**。"
             f"依據：[[00 投資系統/投資政策書]]。資料：持股快照 {now['as_of']}（最早一份 {first['as_of'][:10]}）。\n")

    # 1. 摘要
    top = next(e for e in now["exposures"] if not e["residual"])
    ai = dict(now["themes"]).get("AI／半導體", 0)
    ai0 = dict(first["themes"]).get("AI／半導體", 0)
    L.append("## 1. 摘要")
    L.append("| 項目 | 現在 | 期初 | 變化 |\n|---|---|---|---|")
    L.append(f"| 總資產 | NT${now['total_twd'] / 1e4:,.0f} 萬 | NT${first['total_twd'] / 1e4:,.0f} 萬 | "
             f"{now['total_twd'] / first['total_twd'] - 1:+.1%} |")
    L.append(f"| 台股／美股 | {now['by_market'].get('台股', 0):.0%}／{now['by_market'].get('美股／海外', 0):.0%} | "
             f"{first['by_market'].get('台股', 0):.0%}／{first['by_market'].get('美股／海外', 0):.0%} | |")
    L.append(f"| 核心／衛星 | {now['by_sleeve'].get('core', 0):.1%}／{now['by_sleeve'].get('satellite', 0):.1%} | "
             f"{first['by_sleeve'].get('core', 0):.1%}／{first['by_sleeve'].get('satellite', 0):.1%} | |")
    L.append(f"| 最大單一曝險 | {top['name']} {top['pct']:.1%} | | |")
    L.append(f"| AI／半導體主題 | {ai:.1%} | {ai0:.1%} | {(ai - ai0) * 100:+.1f} 個百分點 |\n")

    # 2. 政策書檢查
    L.append("## 2. 投資政策書檢查")
    L.append("| 規則 | 上限／目標 | 現在 | 狀態 |\n|---|---|---|---|")
    exc = conc.get("exceptions", {})
    single_max = conc.get("single_name_max", 0.15)
    worst = [e for e in now["exposures"] if not e["residual"] and e["key"] not in exc]
    if worst:
        w = worst[0]
        L.append(f"| 單一公司（一般） | {single_max:.0%} | {w['name']} {w['pct']:.1%} | {status_icon(w['pct'] <= single_max)} |")
    for k, lim in exc.items():
        e = next((x for x in now["exposures"] if x["key"] == k), None)
        if e:
            L.append(f"| 單一公司（{e['name']} 例外） | {lim:.0%} | {e['pct']:.1%} | {status_icon(e['pct'] <= lim)} |")
    t_max, t_target = conc.get("theme_max", 0.7), conc.get("theme_target", 0.6)
    L.append(f"| AI／半導體主題 | 目標 {t_target:.0%}／上限 {t_max:.0%} | {ai:.1%} | "
             f"{'✅' if ai <= t_target else ('⚠️ 高於目標' if ai <= t_max else '❌ 超過上限')} |")
    sat = now["by_sleeve"].get("satellite", 0)
    L.append(f"| 衛星部位 | 目標 {alloc.get('satellite_target', 0.15):.0%}／上限 {alloc.get('satellite_max', 0.18):.0%} | "
             f"{sat:.1%} | {status_icon(sat <= alloc.get('satellite_max', 0.18))} |")
    open_cards = [c for c in cards if c["fm"].get("status") == "持有中"]
    formal = [c for c in open_cards if c["fm"].get("ticker") not in transition]
    L.append(f"| 正式波段筆數（不含過渡） | {swing.get('max_open_trades', 5)} 筆 | {len(formal)} 筆 | "
             f"{status_icon(len(formal) <= swing.get('max_open_trades', 5))} |")
    sat_tickers = {r["ticker"] for r in now["rows"] if r["sleeve"] == "satellite"}
    covered = {c["fm"].get("ticker") for c in open_cards}
    missing = sorted(sat_tickers - covered)
    L.append(f"| 衛星持股都有交易卡 | 100% | {len(sat_tickers) - len(missing)}/{len(sat_tickers)} | "
             f"{status_icon(not missing)}{'：缺 ' + '、'.join(missing) if missing else ''} |")
    below_rr = [c for c in open_cards if (num(c["fm"].get("reward_risk")) or 0) < swing.get("min_reward_risk", 2)]
    L.append(f"| 交易卡風險報酬比 ≥ {swing.get('min_reward_risk', 2):g}:1 | 全部 | "
             f"{len(open_cards) - len(below_rr)}/{len(open_cards)} | {status_icon(not below_rr)} |\n")

    # 3. 論點總覽
    L.append("## 3. 論點總覽（依實際曝險排序）")
    L.append("| 持股 | 分類 | 狀態 | 信心 | 實際曝險 | 下次檢查 | 本季最後一筆紀錄 |\n|---|---|---|---|---|---|---|")
    for t in sorted(theses, key=lambda t: -(num(t["fm"].get("lookthrough_pct")) or num(t["fm"].get("weight_pct")) or 0)):
        fm = t["fm"]
        nr = fm.get("next_review", "")
        overdue = nr and nr < today.isoformat()
        last = t["events"][0] if t["events"] else None
        last_s = f"{last['date']} {last['event']}（{last['verdict']}）" if last else "—"
        expo = num(fm.get("lookthrough_pct")) or num(fm.get("weight_pct")) or 0
        L.append(f"| {link(t['path'], table=True)} | {fm.get('sleeve', '')} | {fm.get('status', '')} | {fm.get('conviction', '')} | "
                 f"{expo:.2f}% | {nr}{' ⚠️逾期' if overdue else ''} | {last_s} |")
    L.append("")

    # 4. 需要你注意
    L.append("## 4. 需要你注意")
    watch = [t for t in theses if t["fm"].get("status") in ("觀察中", "失效")]
    todo = [t for t in theses if t["fm"].get("status") == "待補論點"]
    overdue = [t for t in theses if t["fm"].get("next_review", "9") < today.isoformat()]
    if watch:
        L.append("**論點觀察中／失效：** " + "、".join(f"{link(t['path'])}（{t['fm'].get('status')}）" for t in watch))
    if todo:
        L.append("**還沒寫論點：** " + "、".join(link(t["path"]) for t in todo))
    if overdue:
        L.append("**檢查日已過：** " + "、".join(link(t["path"]) for t in overdue))
    alerts = [a for a in now.get("alerts", []) if a[0] in ("serious", "warning")]
    if alerts:
        L.append("**持股總覽警示：**")
        L += [f"- {msg}" for _, msg in alerts]
    flags = sorted(theses, key=lambda t: -t["single_source_flags"])[:5]
    flags = [t for t in flags if t["single_source_flags"]]
    if flags:
        L.append("**論點裡待查證的 ⚠️ 最多的筆記：** " + "、".join(f"{link(t['path'])}（{t['single_source_flags']}）" for t in flags))
    L.append("")

    # 5. 交易卡
    L.append("## 5. 交易卡（持有中）")
    L.append("| 卡片 | 類型 | 現價 | 停損 | 目標 | 距停損 | 風險報酬比 | 期限動作 |\n|---|---|---|---|---|---|---|---|")
    for c in open_cards:
        fm = c["fm"]
        tk = fm.get("ticker", "")
        px, stop, target = price.get(tk), num(fm.get("stop")), num(fm.get("target"))
        dist = f"{px / stop - 1:+.0%}" if px and stop else "—"
        act = f"{fm.get('action_date')} {fm.get('action')}" if fm.get("action_date") else "—"
        kind = "過渡" if tk in transition else fm.get("setup", "")
        L.append(f"| [[{c['path'].relative_to(VAULT).with_suffix('').as_posix()}\\|{c['path'].stem}]] | {kind} | "
                 f"{px:,.2f} | {stop:g} | {target:g} | {dist} | {fm.get('reward_risk', '')} | {act} |"
                 if px and stop and target else
                 f"| {c['path'].stem} | {kind} | — | {fm.get('stop')} | {fm.get('target')} | — | {fm.get('reward_risk', '')} | {act} |")
    closed = [c for c in cards if c["fm"].get("status") == "已出場"]
    if closed:
        rs = [num(c["fm"].get("result_r")) for c in closed if num(c["fm"].get("result_r")) is not None]
        win = sum(1 for r in rs if r > 0)
        L.append(f"\n**本期已出場 {len(closed)} 筆**：勝率 {win}/{len(rs) or 1}，平均 {sum(rs) / len(rs):+.2f}R" if rs
                 else f"\n**本期已出場 {len(closed)} 筆**（尚未填結果）")
    L.append("")

    # 5.5 選股 vs 指數（政策書 4.2）
    bench = pol.get("new_money", {}).get("benchmark")
    if bench:
        L.append(f"## 5.5 選股 vs {bench}（政策書 4.2）")
        rows_sv = []
        for c in cards:
            fm = c["fm"]
            tk, od = fm.get("ticker"), fm.get("open_date")
            entry = num(fm.get("entry"))
            if not (tk and od and entry):
                continue
            exit_px = num(fm.get("exit_price")) if fm.get("status") == "已出場" else price.get(tk)
            end = fm.get("close_date") or today.isoformat()
            if not exit_px:
                continue
            b = bench_return(bench, od, end)
            w = num(fm.get("size_twd")) or 0
            rows_sv.append((c["path"].stem, exit_px / entry - 1, b, w))
        if rows_sv:
            L.append("| 交易卡 | 選股報酬 | 同期 " + bench + " | 差距 |\n|---|---|---|---|")
            for name, r, b, _ in rows_sv:
                L.append(f"| {name} | {r:+.1%} | {b:+.1%} | {r - b:+.1%} |" if b is not None else f"| {name} | {r:+.1%} | — | — |")
            tw_ = sum(w for *_, w in rows_sv if w) or 1
            port = sum(r * w for _, r, _, w in rows_sv) / tw_
            bm = [(b, w) for _, _, b, w in rows_sv if b is not None]
            bport = sum(b * w for b, w in bm) / (sum(w for _, w in bm) or 1)
            L.append(f"\n**加權合計：選股 {port:+.1%} vs {bench} {bport:+.1%}（差距 {port - bport:+.1%}）**。"
                     "連續 4 季輸給指數 → 依政策書衛星降到 5% 或停止選股。過渡部位的起算日是 2026-11-09 補卡日。")
        L.append("")

    # 6. 本季事件
    L.append(f"## 6. 本季追蹤紀錄（近 {LOOKBACK_DAYS} 天）")
    evs = sorted(((e, t) for t in theses for e in t["events"]), key=lambda x: x[0]["date"], reverse=True)
    for e, t in evs[:40]:
        L.append(f"- {e['date']}｜{link(t['path'])}｜{e['event']}｜論點：{e['verdict']}")
    if len(evs) > 40:
        L.append(f"- …另有 {len(evs) - 40} 筆")
    L.append("")

    # 7. 決策區
    L.append("## 7. 決策區（你填）")
    L.append("### 再平衡")
    L.append("- [ ] 維持現狀\n- [ ] 新資金投向：____（政策書：AI 主題高於 60% 目標時，優先非 AI）\n- [ ] 減碼：____（原因：超過上限／論點失效）")
    L.append("### 觀察中／失效的論點")
    for t in watch:
        L.append(f"- {link(t['path'])}：[ ] 續抱　[ ] 降為觀察名單　[ ] 出場　理由：____")
    L.append("### 交易卡與過渡期")
    L.append(f"- 過渡期部位 12/21 統一檢查：[ ] 已排入行事曆\n- 交易卡風險報酬比未達標的 {len(below_rr)} 張：[ ] 維持例外　[ ] 提前出場：____")
    L.append("### 投資政策書")
    L.append("- [ ] 不修改\n- [ ] 修改：____（修改後同步 Obsidian 與 private/portfolio/policy.toml）")
    L.append("### 下一季的重點")
    L.append("- \n")
    L.append("> 本報告為個人紀錄與研究用途，不構成投資建議。")
    return "\n".join(L)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{date.today():%Y-%m} 季檢討.md"
    if out.exists():
        out = OUT_DIR / f"{date.today():%Y-%m} 季檢討（重產 {datetime.now():%H%M}）.md"
    out.write_text(build(), encoding="utf-8")
    print(f"已產生：{out}")


if __name__ == "__main__":
    main()
