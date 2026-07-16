# -*- coding: utf-8 -*-
"""يحوّل states.xlsx + districts.xlsx (بجذر المستودع) إلى app/rag/locations.json.

يُشغَّل مرة واحدة وقت التطوير (يحتاج openpyxl)؛ وقت التشغيل يقرأ
app/rag/locations.py ملف JSON الناتج فقط بدون أي اعتماد خارجي:

    python -m app.rag.prepare_locations

بنية الناتج:
{
  "states":    [{"code": "BGD", "name": "بغداد"}, ...],
  "districts": [{"name": "الحرية الثالثة", "state": "BGD"}, ...]
}
"""

import json
import os

import openpyxl

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(BASE_DIR, "..", ".."))
OUTPUT_PATH = os.path.join(BASE_DIR, "locations.json")


def _rows(path):
    ws = openpyxl.load_workbook(path, read_only=True).active
    rows = ws.iter_rows(values_only=True)
    next(rows)  # صف العناوين
    for row in rows:
        if row and row[0] and row[1]:
            yield str(row[0]).strip(), str(row[1]).strip()


def main():
    states = [
        {"code": code, "name": name}
        for code, name in _rows(os.path.join(REPO_ROOT, "states.xlsx"))
    ]
    districts = [
        {"name": name, "state": code}
        for name, code in _rows(os.path.join(REPO_ROOT, "districts.xlsx"))
    ]
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {"states": states, "districts": districts},
            f, ensure_ascii=False, indent=1,
        )
    print(f"كتبنا {len(states)} محافظة و{len(districts)} منطقة إلى {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
