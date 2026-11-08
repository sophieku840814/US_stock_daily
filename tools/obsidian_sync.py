"""把持股同步到 Obsidian：每檔個股一則「投資論點」筆記。

- 沒有筆記：用範本建立骨架，自動帶入部位資料，並連結該公司資料夾裡既有的研究筆記
- 已有筆記：只更新 frontmatter 裡的部位欄位（held、qty、cost、weight_pct、lookthrough_pct、price、synced），
  不動內文，你或 Claude 寫的論點不會被覆蓋
- 已經不持有的：held 改成 false（筆記保留，方便日後檢討）
- 產生／更新「00 投資系統/持股論點總表.base」與「交易紀錄總表.base」（Obsidian Bases 資料庫檢視）

資料：private/portfolio/holdings.toml、policy.toml、最新的 private/portfolio/snapshots/*.json（先跑 portfolio_exposure.py）
用法：python -X utf8 tools/obsidian_sync.py
"""
from __future__ import annotations

import json
import os
import re
import tomllib
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VAULT = Path(os.environ.get("OBSIDIAN_VAULT", r"C:\Users\Andy\Documents\obsidian\obsidian"))
SYS = VAULT / "00 投資系統"
COMPANIES = VAULT / "10 公司"
TEMPLATE = SYS / "範本" / "投資論點範本.md"
HOLDINGS = ROOT / "private" / "portfolio" / "holdings.toml"
POLICY = ROOT / "private" / "portfolio" / "policy.toml"
SNAP_DIR = ROOT / "private" / "portfolio" / "snapshots"
CONFIG = ROOT / "tools" / "portfolio_config.toml"

MANAGED = ["held", "qty", "cost", "weight_pct", "lookthrough_pct", "price", "synced"]

BASE_THESIS = """filters:
  and:
    - file.hasTag("投資論點")
views:
  - type: table
    name: 目前持股
    filters:
      and:
        - 'held == true'
    order:
      - file.name
      - sleeve
      - status
      - conviction
      - weight_pct
      - lookthrough_pct
      - next_review
  - type: table
    name: 待補論點
    filters:
      and:
        - 'held == true'
        - 'status == "待補論點"'
    order:
      - file.name
      - sleeve
      - weight_pct
  - type: table
    name: 全部（含已出場）
    order:
      - file.name
      - held
      - status
      - updated
"""

BASE_TRADES = """filters:
  and:
    - file.hasTag("交易卡")
views:
  - type: table
    name: 進行中
    filters:
      or:
        - 'status == "計畫中"'
        - 'status == "持有中"'
    order:
      - file.name
      - ticker
      - setup
      - status
      - entry
      - stop
      - target
      - reward_risk
  - type: table
    name: 已出場
    filters:
      and:
        - 'status == "已出場"'
    order:
      - file.name
      - ticker
      - setup
      - close_date
      - exit_reason
      - result_r
"""


def latest_snapshot() -> dict:
    files = sorted(SNAP_DIR.glob("*.json"))
    if not files:
        raise SystemExit("找不到持股快照，請先執行 python -X utf8 tools/portfolio_exposure.py")
    return json.loads(files[-1].read_text(encoding="utf-8"))


def next_quarterly_review(months: list[int]) -> str:
    t = date.today()
    for y in (t.year, t.year + 1):
        for m in sorted(months):
            d = date(y, m, 15)
            if d > t:
                return d.isoformat()
    return (t + timedelta(days=90)).isoformat()


def note_path(ticker: str, name: str) -> Path:
    if ticker.endswith((".TW", ".TWO")):
        sid = ticker.split(".")[0]
        return COMPANIES / f"{sid} {name}" / f"{name} 投資論點.md"
    return COMPANIES / ticker / f"{ticker} 投資論點.md"


def _yaml_val(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def set_frontmatter(text: str, values: dict) -> str:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        fm_lines, body = [], text
    else:
        fm_lines, body = m.group(1).split("\n"), text[m.end():]
    for k, v in values.items():
        line = f"{k}: {_yaml_val(v)}"
        for i, ln in enumerate(fm_lines):
            if re.match(rf"^{re.escape(k)}:", ln):
                fm_lines[i] = line
                break
        else:
            fm_lines.append(line)
    return "---\n" + "\n".join(fm_lines) + "\n---\n" + body


def related_links(folder: Path, exclude: Path) -> str:
    if not folder.exists():
        return "- （尚無）"
    notes = sorted(p for p in folder.glob("*.md") if p != exclude)
    return "\n".join(f"- [[{p.relative_to(VAULT).with_suffix('').as_posix()}]]" for p in notes) or "- （尚無）"


def main() -> None:
    holdings = tomllib.loads(HOLDINGS.read_text(encoding="utf-8"))
    policy = tomllib.loads(POLICY.read_text(encoding="utf-8")) if POLICY.exists() else {}
    names = tomllib.loads(CONFIG.read_text(encoding="utf-8")).get("names", {})
    snap = latest_snapshot()
    total = snap["total_usd"]
    rows = {r["ticker"]: r for r in snap["rows"]}
    expo = {e["key"]: e for e in snap["exposures"]}

    agg: dict[str, dict] = defaultdict(lambda: {"qty": 0.0, "cost_x_qty": 0.0, "cost_qty": 0.0, "sleeve": "core"})
    for acc in holdings["accounts"]:
        for p in acc["positions"]:
            a = agg[p["ticker"]]
            a["qty"] += p["qty"]
            a["sleeve"] = p.get("sleeve", a["sleeve"])
            if p.get("cost"):
                a["cost_x_qty"] += p["cost"] * p["qty"]
                a["cost_qty"] += p["qty"]

    review_core = next_quarterly_review(policy.get("review", {}).get("quarterly_months", [2, 5, 8, 11]))
    review_sat = (date.today() + timedelta(days=14)).isoformat()
    template = TEMPLATE.read_text(encoding="utf-8")
    created, updated, held_paths = [], [], set()

    for tk, a in agg.items():
        r = rows.get(tk)
        if not r or r["is_etf"]:
            continue
        name = names.get(tk) or r["name"]
        path = note_path(tk, name)
        held_paths.add(path)
        value_usd = sum(x["value_usd"] for x in snap["rows"] if x["ticker"] == tk)
        key = "GOOGL" if tk in ("GOOG", "GOOGL") else tk
        vals = {
            "held": True,
            "qty": round(a["qty"], 4),
            "cost": round(a["cost_x_qty"] / a["cost_qty"], 2) if a["cost_qty"] else None,
            "weight_pct": round(value_usd / total * 100, 2),
            "lookthrough_pct": round(expo[key]["pct"] * 100, 2) if key in expo else None,
            "price": round(r["price"], 2),
            "synced": date.today().isoformat(),
        }
        if path.exists():
            path.write_text(set_frontmatter(path.read_text(encoding="utf-8"), vals), encoding="utf-8")
            updated.append(path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        sleeve = "核心" if a["sleeve"] == "core" else "衛星"
        text = template.replace("{{title}}", f"{name if '.' in tk else tk} 投資論點")
        text = set_frontmatter(text, {
            "ticker": tk, "name": name, "market": "台股" if tk.endswith((".TW", ".TWO")) else "美股",
            "sleeve": sleeve, "next_review": review_core if sleeve == "核心" else review_sat,
            "updated": date.today().isoformat(), **vals,
        })
        text = text.replace("## 相關研究\n", "## 相關研究\n" + related_links(path.parent, path) + "\n")
        path.write_text(text, encoding="utf-8")
        created.append(path)

    # 已經不持有的
    sold = []
    for p in COMPANIES.glob("*/* 投資論點.md"):
        if p not in held_paths and "held: true" in p.read_text(encoding="utf-8"):
            p.write_text(set_frontmatter(p.read_text(encoding="utf-8"), {"held": False, "synced": date.today().isoformat()}),
                         encoding="utf-8")
            sold.append(p)

    SYS.mkdir(parents=True, exist_ok=True)
    (SYS / "持股論點總表.base").write_text(BASE_THESIS, encoding="utf-8")
    (SYS / "交易紀錄總表.base").write_text(BASE_TRADES, encoding="utf-8")

    rel = lambda p: p.relative_to(VAULT).as_posix()  # noqa: E731
    print(f"新建 {len(created)} 則：" + "、".join(rel(p) for p in created))
    print(f"更新 {len(updated)} 則：" + "、".join(rel(p) for p in updated))
    if sold:
        print(f"標記為已出場 {len(sold)} 則：" + "、".join(rel(p) for p in sold))


if __name__ == "__main__":
    main()
