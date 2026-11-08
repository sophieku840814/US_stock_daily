"""Recalculate an .xlsx with the local Microsoft Excel (COM) and scan for formula errors.

Stand-in for the skill's LibreOffice-based recalc.py on Windows machines with Excel.
    python tools/recalc_excel.py models/2330_DCF_Model_YYYY-MM-DD.xlsx
"""
import json
import subprocess
import sys
from pathlib import Path

from openpyxl import load_workbook

path = Path(sys.argv[1]).resolve()
ps = f"""
$x = New-Object -ComObject Excel.Application
$x.Visible = $false; $x.DisplayAlerts = $false
$wb = $x.Workbooks.Open('{path}')
$x.CalculateFull()
$wb.Save(); $wb.Close($false); $x.Quit()
[System.Runtime.InteropServices.Marshal]::ReleaseComObject($x) | Out-Null
"""
subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)

ERRS = ("#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#NULL!", "#NUM!", "#N/A")
vals = load_workbook(path, data_only=True)
fmls = load_workbook(path, data_only=False)
summary, n_formulas, empty = {}, 0, []
for ws in fmls.worksheets:
    for row in ws.iter_rows():
        for c in row:
            if isinstance(c.value, str) and c.value.startswith("="):
                n_formulas += 1
                v = vals[ws.title][c.coordinate].value
                if v is None:
                    empty.append(f"{ws.title}!{c.coordinate}")
                elif isinstance(v, str) and v in ERRS:
                    summary.setdefault(v, []).append(f"{ws.title}!{c.coordinate}")
print(json.dumps({
    "status": "success" if not summary and not empty else "errors_found",
    "total_formulas": n_formulas,
    "total_errors": sum(len(v) for v in summary.values()),
    "error_summary": summary,
    "uncalculated": empty[:20],
}, indent=2))
