"""Bounded CSV, XLSX and vCard parsing with Python's standard library.

No workbook macros, formulas, external links or files are executed or fetched.
Only the user's selected file is processed. Raw files are never persisted.
"""
import csv
import hashlib
import io
import json
import posixpath
import re
import zipfile
from xml.etree import ElementTree as ET

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 2000
MAX_COLUMNS = 80
MAX_CELL = 500
FIELDS = {"name", "first_name", "last_name", "phone", "email", "ministry"}
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def bounded_rows(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS + 1:
        raise ValueError("Use a header and 1–2,000 contact rows per import.")
    for row in rows:
        if not isinstance(row, list) or len(row) > MAX_COLUMNS:
            raise ValueError("Files support up to 80 columns.")
        if any(not isinstance(v, str) or len(v) > MAX_CELL for v in row):
            raise ValueError("Each cell must be text with at most 500 characters.")
    return rows


def xml(data):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("Unsupported XML declarations in workbook.")
    return ET.fromstring(data)


def parse_xlsx(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > 400 or sum(i.file_size for i in infos) > 20 * 1024 * 1024:
                raise ValueError("Workbook expands beyond the 20 MB safety limit.")
            if any(i.flag_bits & 1 for i in infos):
                raise ValueError("Unlock the workbook before importing.")
            shared = []
            if "xl/sharedStrings.xml" in archive.namelist():
                root = xml(archive.read("xl/sharedStrings.xml"))
                shared = ["".join(n.itertext()) for n in root.findall("s:si", NS)]
            rels = xml(archive.read("xl/_rels/workbook.xml.rels"))
            targets = {r.attrib["Id"]: r.attrib["Target"] for r in rels if r.attrib.get("TargetMode") != "External"}
            sheets = []
            for sheet in xml(archive.read("xl/workbook.xml")).findall("s:sheets/s:sheet", NS):
                rid = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                target = targets.get(rid, "")
                path = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/" + target)
                if not path.startswith("xl/worksheets/") or not path.endswith(".xml"):
                    raise ValueError("Unsupported worksheet location.")
                rows = []
                for row in xml(archive.read(path)).findall("s:sheetData/s:row", NS):
                    cells = []
                    for cell in row.findall("s:c", NS):
                        ref = re.match(r"^([A-Z]+)[1-9][0-9]*$", cell.attrib.get("r", ""))
                        if not ref:
                            raise ValueError("Workbook cells need valid references.")
                        column = 0
                        for letter in ref[1]:
                            column = column * 26 + ord(letter) - 64
                        if column > MAX_COLUMNS:
                            raise ValueError("Files support up to 80 columns.")
                        while len(cells) < column:
                            cells.append("")
                        value = cell.findtext("s:v", "", NS)
                        kind = cell.attrib.get("t")
                        if cell.find("s:f", NS) is not None or kind == "e":
                            value = "[Unsupported formula/error]"
                        elif kind == "s":
                            value = shared[int(value)]
                        elif kind == "inlineStr":
                            value = "".join(cell.find("s:is", NS).itertext())
                        elif re.fullmatch(r"[0-9]{1,16}\.0+", value):
                            # Excel can store integer phones with a decimal suffix.
                            value = value.split(".", 1)[0]
                        cells[column - 1] = value
                    if any(cells):
                        rows.append(cells)
                    if len(rows) > MAX_ROWS + 1:
                        raise ValueError("Use at most 2,000 contacts per worksheet.")
                if rows:
                    # XLSX omits trailing blank cells. Keep optional blank fields.
                    width = len(rows[0])
                    rows = [r + [""] * max(0, width - len(r)) for r in rows]
                    sheets.append({"name": sheet.attrib.get("name", "Sheet"), "rows": bounded_rows(rows)})
            if not sheets or sum(len(s["rows"]) for s in sheets) > MAX_ROWS + 20:
                raise ValueError("Use a nonempty workbook with at most 2,000 contact rows total.")
            return sheets
    except (KeyError, IndexError, TypeError, ET.ParseError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError("Unable to read this XLSX file. Export it as CSV and try again.") from exc


def unescape_vcard(value):
    return re.sub(r"\\([nN,;\\])", lambda m: " " if m[1].lower() == "n" else m[1], value)


def parse_vcard(text):
    text = re.sub(r"\r?\n[ \t]", "", text)
    rows, card = [["Name", "Phone", "Email", "Ministry"]], None
    for line in text.splitlines():
        if line.upper() == "BEGIN:VCARD":
            card = {"phones": []}
        elif line.upper() == "END:VCARD" and card is not None:
            # Choose each exported number explicitly in the preview; no hidden selection.
            for phone in card["phones"] or [""]:
                rows.append([card.get("name", ""), phone, card.get("email", ""), ""])
            card = None
        elif card is not None and ":" in line:
            key, value = line.split(":", 1)
            if "ENCODING=" in key.upper():
                raise ValueError("Use a UTF-8 vCard export without encoded contact fields.")
            key = key.split(";", 1)[0].split(".")[-1].upper()
            if key == "FN":
                card["name"] = unescape_vcard(value)
            elif key == "N" and not card.get("name"):
                parts = value.split(";")
                card["name"] = unescape_vcard(" ".join(parts[1:2] + parts[:1]))
            elif key == "TEL":
                card["phones"].append(value.removeprefix("tel:"))
            elif key == "EMAIL" and not card.get("email"):
                card["email"] = unescape_vcard(value)
    if card is not None or len(rows) == 1:
        raise ValueError("Select a complete vCard file containing contacts.")
    return bounded_rows(rows)


def parse_file(filename, data):
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Select a nonempty file up to 5 MB.")
    extension = filename.rsplit(".", 1)[-1].lower()
    if extension == "xlsx":
        return parse_xlsx(data)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Export your file as UTF-8 CSV or vCard.") from exc
    if extension == "vcf":
        rows = parse_vcard(text)
    elif extension == "csv":
        try:
            sample = text[:8192]
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        try:
            rows = []
            for row in csv.reader(io.StringIO(text, newline=""), dialect=dialect, strict=True):
                if any(v.strip() for v in row):
                    rows.append(row)
                if len(rows) > MAX_ROWS + 1:
                    raise ValueError("Use at most 2,000 contact rows per import.")
            bounded_rows(rows)
        except csv.Error as exc:
            raise ValueError("Check the CSV quoting and delimiters.") from exc
    else:
        raise ValueError("Choose a CSV, XLSX or vCard (.vcf) file. Export older XLS files as CSV.")
    return [{"name": "Contacts", "rows": rows}]


def normalize_phone(value, country="US"):
    value = value.strip()
    if not re.fullmatch(r"\+?[0-9\s().-]+", value):
        raise ValueError("Use a phone number without extensions or letters.")
    digits = re.sub(r"\D", "", value)
    if value.startswith("+"):
        if not re.fullmatch(r"[1-9][0-9]{7,14}", digits):
            raise ValueError("Use +country code and 8–15 digits.")
    elif country in {"US", "CA"} and len(digits) == 10:
        digits = "1" + digits
    elif country in {"US", "CA"} and len(digits) == 11 and digits.startswith("1"):
        pass
    else:
        raise ValueError("Include +country code; local numbers support US/Canada only.")
    if digits.startswith("1") and (len(digits) != 11 or digits[1] not in "23456789" or digits[4] not in "23456789"):
        raise ValueError("Check the US/Canada area code and exchange.")
    return "+" + digits


def preview(rows, mapping, country, source, existing=()):
    bounded_rows(rows)
    if len(rows) < 2:
        raise ValueError("Add contact rows beneath the header.")
    if not isinstance(mapping, dict) or set(mapping) - FIELDS:
        raise ValueError("Choose supported contact fields.")
    if "phone" not in mapping or not ({"name", "first_name"} & mapping.keys()):
        raise ValueError("Map a phone column and a name or first-name column.")
    if any(type(v) is not int or not 0 <= v < len(rows[0]) for v in mapping.values()) or len(set(mapping.values())) != len(mapping):
        raise ValueError("Map each field to a different valid column.")
    if country not in {"US", "CA", "international"}:
        raise ValueError("Choose a supported default phone region.")
    if not isinstance(source, str) or not 1 <= len(source.strip()) <= 240:
        raise ValueError("Describe the contact source (up to 240 characters).")
    seen, results, ready = set(existing), [], []
    counts = {"ready": 0, "duplicate": 0, "invalid": 0}
    for number, row in enumerate(rows[1:], 2):
        def get(key):
            index = mapping.get(key)
            return row[index].strip() if index is not None and index < len(row) else ""
        contact = {"name": get("name") or " ".join(filter(None, [get("first_name"), get("last_name")])),
                   "phone": get("phone"), "email": get("email"), "ministry": get("ministry"), "source": source.strip()}
        status, reason = "ready", "Awaiting volunteer consent; no text will be sent."
        try:
            if len(row) != len(rows[0]):
                raise ValueError("Row has a different number of columns than the header.")
            if any(v.startswith(("=", "[Unsupported formula/error]")) for v in contact.values()):
                raise ValueError("Replace formulas/errors with plain text values.")
            if not 1 <= len(contact["name"]) <= 160:
                raise ValueError("Provide a name of 1–160 characters.")
            if len(contact["ministry"]) > 160:
                raise ValueError("Ministry must be at most 160 characters.")
            if contact["email"] and (len(contact["email"]) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", contact["email"])):
                raise ValueError("Check the email address or leave it blank.")
            contact["phone"] = normalize_phone(contact["phone"], country)
            if contact["phone"] in seen:
                status, reason = "duplicate", "Phone already staged or repeated in this file; existing record preserved."
            else:
                seen.add(contact["phone"])
                ready.append(contact)
        except ValueError as exc:
            status, reason = "invalid", str(exc)
        counts[status] += 1
        results.append({"row": number, **contact, "status": status, "reason": reason, "consent": "not_recorded"})
    digest = hashlib.sha256(json.dumps({"ready": ready, "results": results}, sort_keys=True).encode()).hexdigest()
    return {"rows": results, "counts": counts, "preview_hash": digest}, ready
