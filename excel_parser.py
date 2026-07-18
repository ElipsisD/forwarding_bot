from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import zipfile
import xml.etree.ElementTree as ET


SPREADSHEET_NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


@dataclass(frozen=True)
class Order:
    number: str
    recipient: str
    places: str
    amount: str
    payment: str
    address: str
    extra: str
    hours: str


FIELD_ALIASES = {
    "number": ("№", "номер", "номер заказа", "заказ"),
    "recipient": ("получатель", "клиент", "контрагент"),
    "places": ("мест", "места", "кол-во мест", "количество мест"),
    "amount": ("сумма", "сумма оплаты"),
    "payment": ("оплата", "тип оплаты", "способ оплаты"),
    "address": ("адрес", "адрес доставки"),
    "extra": ("дополнительная информация", "доп информация", "комментарий", "примечание"),
    "hours": ("время", "время работы", "режим работы"),
}


def parse_orders(path: str | Path) -> list[Order]:
    rows = _read_xlsx_rows(Path(path))
    if not rows:
        return []

    header_index, columns = _find_header(rows)
    orders: list[Order] = []

    for row in rows[header_index + 1 :]:
        values = {
            field: _clean(row.get(column_index, ""))
            for field, column_index in columns.items()
        }
        if not any(values.values()):
            continue

        order = Order(
            number=_normalize_order_number(values.get("number", "")),
            recipient=values.get("recipient", ""),
            places=values.get("places", ""),
            amount=values.get("amount", ""),
            payment=values.get("payment", ""),
            address=values.get("address", ""),
            extra=values.get("extra", ""),
            hours=values.get("hours", ""),
        )
        if order.number or order.recipient or order.address:
            orders.append(order)

    return orders


def _find_header(rows: list[dict[int, str]]) -> tuple[int, dict[str, int]]:
    best_index = 0
    best_columns: dict[str, int] = {}

    for index, row in enumerate(rows[:10]):
        columns = _match_columns(row)
        if len(columns) > len(best_columns):
            best_index = index
            best_columns = columns

    required = {"number", "recipient", "places", "payment", "address"}
    missing = required - set(best_columns)
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(sorted(missing))}")

    return best_index, best_columns


def _match_columns(row: dict[int, str]) -> dict[str, int]:
    result: dict[str, int] = {}
    normalized_cells = {index: _normalize(value) for index, value in row.items()}

    for field, aliases in FIELD_ALIASES.items():
        for index, value in normalized_cells.items():
            if not value:
                continue
            if any(value == _normalize(alias) for alias in aliases):
                result[field] = index
                break

    return result


def _read_xlsx_rows(path: Path) -> list[dict[int, str]]:
    with zipfile.ZipFile(path) as archive:
        shared_strings = _read_shared_strings(archive)
        sheet_name = _first_sheet_name(archive)
        root = ET.fromstring(archive.read(sheet_name))

    cells: dict[tuple[int, int], str] = {}
    for row in root.findall(".//a:sheetData/a:row", SPREADSHEET_NS):
        for cell in row.findall("a:c", SPREADSHEET_NS):
            reference = cell.attrib.get("r", "")
            column_index = _column_index(reference)
            row_index = _row_index(reference)
            if column_index is None or row_index is None:
                continue
            value = _cell_value(cell, shared_strings)
            if value:
                cells[row_index, column_index] = value

    for merge in root.findall(".//a:mergeCells/a:mergeCell", SPREADSHEET_NS):
        start, separator, end = merge.attrib.get("ref", "").partition(":")
        if not separator:
            continue
        start_column = _column_index(start)
        start_row = _row_index(start)
        end_column = _column_index(end)
        end_row = _row_index(end)
        if None in (start_column, start_row, end_column, end_row):
            continue

        value = cells.get((start_row, start_column), "")
        if not value:
            continue
        for row_index in range(start_row, end_row + 1):
            for column_index in range(start_column, end_column + 1):
                cells.setdefault((row_index, column_index), value)

    rows_by_index: dict[int, dict[int, str]] = {}
    for (row_index, column_index), value in cells.items():
        rows_by_index.setdefault(row_index, {})[column_index] = value
    return [rows_by_index[row_index] for row_index in sorted(rows_by_index)]


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []

    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    values: list[str] = []
    for item in root.findall("a:si", SPREADSHEET_NS):
        text_parts = [node.text or "" for node in item.findall(".//a:t", SPREADSHEET_NS)]
        values.append("".join(text_parts))
    return values


def _first_sheet_name(archive: zipfile.ZipFile) -> str:
    names = archive.namelist()
    for name in names:
        if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
            return name
    raise ValueError("No worksheet found in xlsx file.")


def _cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    value_node = cell.find("a:v", SPREADSHEET_NS)

    if cell_type == "s" and value_node is not None:
        index = int(value_node.text or "0")
        return shared_strings[index] if index < len(shared_strings) else ""

    if cell_type == "inlineStr":
        inline_node = cell.find("a:is", SPREADSHEET_NS)
        if inline_node is None:
            return ""
        return "".join(node.text or "" for node in inline_node.findall(".//a:t", SPREADSHEET_NS))

    return value_node.text if value_node is not None and value_node.text is not None else ""


def _column_index(reference: str) -> int | None:
    letters = "".join(character for character in reference if character.isalpha())
    if not letters:
        return None

    index = 0
    for letter in letters.upper():
        index = index * 26 + ord(letter) - ord("A") + 1
    return index


def _row_index(reference: str) -> int | None:
    digits = "".join(character for character in reference if character.isdigit())
    return int(digits) if digits else None


def _clean(value: str) -> str:
    value = value.replace("\u200b", "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in value.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def _normalize(value: str) -> str:
    value = _clean(value).lower()
    value = value.replace("ё", "е")
    value = re.sub(r"[^0-9a-zа-я№]+", " ", value)
    return " ".join(value.split())


def _normalize_order_number(value: str) -> str:
    if value.isdigit():
        return value.lstrip("0") or "0"
    return value
