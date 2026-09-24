"""Read the supplied XLSX workbooks without an import-time Excel dependency."""

from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def workbook_sheets(path: Path):
    """Yield (sheet_name, rows) with cell values indexed by column letter."""
    if path.suffix.lower() != ".xlsx":
        raise ValueError(f"Unsupported Excel format: {path}")
    with ZipFile(path) as archive:
        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            xml = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            strings = ["".join(part.text or "" for part in item.iter(MAIN + "t"))
                       for item in xml.findall(MAIN + "si")]
        book = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {item.attrib["Id"]: item.attrib["Target"] for item in rels}
        for sheet in book.find(MAIN + "sheets"):
            target = targets[sheet.attrib[REL + "id"]].lstrip("/")
            target = target if target.startswith("xl/") else "xl/" + target
            xml = ET.fromstring(archive.read(target))
            rows = []
            for row in xml.iter(MAIN + "row"):
                values = {}
                for cell in row.findall(MAIN + "c"):
                    col = "".join(ch for ch in cell.attrib.get("r", "") if ch.isalpha())
                    raw = cell.find(MAIN + "v")
                    if cell.attrib.get("t") == "inlineStr":
                        value = "".join(part.text or "" for part in cell.iter(MAIN + "t"))
                    elif raw is None:
                        value = ""
                    elif cell.attrib.get("t") == "s":
                        value = strings[int(raw.text)]
                    else:
                        value = raw.text or ""
                    values[col] = value.strip()
                rows.append(values)
            yield sheet.attrib["name"], rows


def records(path: Path, required: dict[str, tuple[str, ...]]):
    """Yield validated header mappings and data rows for every workbook sheet."""
    for sheet_name, rows in workbook_sheets(path):
        if not rows:
            continue
        headings = {str(value).strip().casefold(): col for col, value in rows[0].items()}
        columns = {}
        for logical, aliases in required.items():
            matched = next((headings[name.casefold()] for name in aliases if name.casefold() in headings), None)
            if matched is None:
                raise ValueError(f"{path} / {sheet_name}: missing {logical}; headers={list(headings)}")
            columns[logical] = matched
        for row_number, row in enumerate(rows[1:], start=2):
            yield sheet_name, row_number, {key: row.get(col, "").strip() for key, col in columns.items()}
