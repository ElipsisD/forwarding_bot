from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from excel_parser import Order


WIDTH = 1080
MAX_HEIGHT = 2160
MAX_ADDRESSES_PER_PAGE = 6
PADDING = 32
CARD_GAP = 16
CARD_RADIUS = 16

BACKGROUND = "#f4f6f8"
CARD = "#ffffff"
TEXT = "#16202a"
MUTED = "#667085"
LINE = "#d9e0e7"
ACCENT = "#1457d9"
PAYMENT_BG = "#e8f5ec"
PAYMENT_TEXT = "#116032"
WARNING_BG = "#fff4df"
WARNING_TEXT = "#8a4b00"


@dataclass
class OrderGroup:
    address: str
    orders: list[Order]


def _group_orders_by_address(orders: list[Order]) -> list[OrderGroup]:
    groups: list[OrderGroup] = []
    groups_by_address: dict[str, OrderGroup] = {}

    for order in orders:
        key = " ".join(order.address.casefold().split())
        if not key:
            groups.append(OrderGroup(address="", orders=[order]))
            continue

        group = groups_by_address.get(key)
        if group is None:
            group = OrderGroup(address=order.address, orders=[])
            groups_by_address[key] = group
            groups.append(group)
        group.orders.append(order)

    return groups


def render_orders(orders: list[Order], output_dir: Path) -> list[Path]:
    groups = _group_orders_by_address(orders)
    pages: list[list[OrderGroup]] = []
    current: list[OrderGroup] = []
    current_height = _header_height()

    for group in groups:
        card_height = _measure_card(group)
        if current and (
            len(current) >= MAX_ADDRESSES_PER_PAGE
            or current_height + card_height + CARD_GAP + PADDING > MAX_HEIGHT
        ):
            pages.append(current)
            current = []
            current_height = _header_height()
        current.append(group)
        current_height += card_height + CARD_GAP

    if current:
        pages.append(current)

    result: list[Path] = []
    for page_index, page_orders in enumerate(pages, start=1):
        path = output_dir / f"orders_page_{page_index}.png"
        _render_page(page_orders, path, page_index, len(pages), len(orders), len(groups))
        result.append(path)

    return result


def _render_page(
    groups: list[OrderGroup],
    path: Path,
    page: int,
    total_pages: int,
    total_orders: int,
    total_addresses: int,
) -> None:
    height = _header_height() + sum(_measure_card(group) + CARD_GAP for group in groups) + PADDING
    image = Image.new("RGB", (WIDTH, height), BACKGROUND)
    draw = ImageDraw.Draw(image)

    y = PADDING
    title_font = _font(46, bold=True)
    meta_font = _font(26)
    draw.text((PADDING, y), "Маршрутный лист", fill=TEXT, font=title_font)
    meta = f"{total_orders} заказов · {total_addresses} адресов"
    if total_pages > 1:
        meta += f" · страница {page}/{total_pages}"
    draw.text((PADDING, y + 56), meta, fill=MUTED, font=meta_font)
    y += _header_height() - PADDING

    for group in groups:
        card_height = _measure_card(group)
        _draw_card(draw, group, PADDING, y, WIDTH - PADDING * 2, card_height)
        y += card_height + CARD_GAP

    image.save(path, "PNG", optimize=True)


def _draw_card(draw: ImageDraw.ImageDraw, group: OrderGroup, x: int, y: int, width: int, height: int) -> None:
    draw.rounded_rectangle((x, y, x + width, y + height), radius=CARD_RADIUS, fill=CARD, outline=LINE, width=2)

    inner_x = x + 24
    inner_y = y + 20
    content_width = width - 48

    number_font = _font(42, bold=True)
    label_font = _font(20, bold=True)
    body_font = _font(30)
    small_font = _font(24)

    inner_y = _draw_section(
        draw,
        "АДРЕС",
        group.address or "Адрес не указан",
        inner_x,
        inner_y,
        content_width,
        label_font,
        _font(34, bold=True),
    )

    for index, order in enumerate(group.orders):
        if index:
            _draw_separator(draw, inner_x, inner_y, content_width)
            inner_y += 14
        inner_y = _draw_order(
            draw,
            order,
            x,
            width,
            inner_x,
            inner_y,
            content_width,
            number_font,
            small_font,
            label_font,
            body_font,
        )


def _draw_order(
    draw: ImageDraw.ImageDraw,
    order: Order,
    card_x: int,
    card_width: int,
    x: int,
    y: int,
    content_width: int,
    number_font: ImageFont.FreeTypeFont,
    small_font: ImageFont.FreeTypeFont,
    label_font: ImageFont.FreeTypeFont,
    body_font: ImageFont.FreeTypeFont,
) -> int:
    order_number = order.number or "без номера"
    draw.text((x, y), f"#{order_number}", fill=ACCENT, font=number_font)

    places_text = f"{order.places or '-'} мест"
    badge_width = _text_width(draw, places_text, small_font) + 34
    badge_x = card_x + card_width - 30 - badge_width
    draw.rounded_rectangle((badge_x, y + 2, badge_x + badge_width, y + 40), radius=10, fill="#edf2ff")
    draw.text((badge_x + 17, y + 7), places_text, fill=ACCENT, font=small_font)

    y += 60
    y = _draw_wrapped(draw, order.recipient or "Получатель не указан", x, y, content_width, body_font, TEXT)
    y += 10

    payment = _payment_text(order)
    if payment:
        payment_needs_attention = _payment_needs_attention(payment, order.amount)
        y = _draw_highlight(
            draw,
            payment,
            x,
            y,
            content_width,
            WARNING_BG if payment_needs_attention else PAYMENT_BG,
            WARNING_TEXT if payment_needs_attention else PAYMENT_TEXT,
        )

    if order.hours:
        y = _draw_section(draw, "ВРЕМЯ РАБОТЫ", order.hours, x, y, content_width, label_font, body_font)
    if order.extra:
        y = _draw_section(draw, "ДОП. ИНФОРМАЦИЯ", order.extra, x, y, content_width, label_font, body_font)
    return y


def _draw_section(
    draw: ImageDraw.ImageDraw,
    label: str,
    value: str,
    x: int,
    y: int,
    width: int,
    label_font: ImageFont.FreeTypeFont,
    value_font: ImageFont.FreeTypeFont,
) -> int:
    draw.text((x, y), label, fill=MUTED, font=label_font)
    y += 26
    y = _draw_wrapped(draw, value, x, y, width, value_font, TEXT)
    return y + 12


def _draw_highlight(
    draw: ImageDraw.ImageDraw,
    value: str,
    x: int,
    y: int,
    width: int,
    fill: str,
    text_fill: str,
) -> int:
    font = _font(28, bold=True)
    lines = _wrap_lines(value, width - 32, font, draw)
    line_height = _line_height(font)
    box_height = len(lines) * line_height + 22
    draw.rounded_rectangle((x, y, x + width, y + box_height), radius=14, fill=fill)
    text_y = y + 11
    for line in lines:
        draw.text((x + 16, text_y), line, fill=text_fill, font=font)
        text_y += line_height
    return y + box_height + 14


def _draw_wrapped(
    draw: ImageDraw.ImageDraw,
    value: str,
    x: int,
    y: int,
    width: int,
    font: ImageFont.FreeTypeFont,
    fill: str,
) -> int:
    for line in _wrap_lines(value, width, font, draw):
        draw.text((x, y), line, fill=fill, font=font)
        y += _line_height(font)
    return y


def _draw_separator(draw: ImageDraw.ImageDraw, x: int, y: int, width: int) -> None:
    draw.line((x, y, x + width, y), fill=LINE, width=2)


def _payment_needs_attention(payment: str, amount: str) -> bool:
    normalized = payment.lower()
    attention_words = ("наличные", "cash", "карта", "перевод", "оплатить", "оплата при")
    return bool(amount.strip()) or any(word in normalized for word in attention_words)


def _payment_text(order: Order) -> str:
    payment = " ".join(order.payment.split())
    if payment.casefold() == "без оплаты":
        payment = ""

    parts = [payment] if payment else []
    if order.amount:
        parts.append(f"Сумма: {order.amount}")
    return "\n".join(parts)


def _measure_card(group: OrderGroup) -> int:
    probe = ImageDraw.Draw(Image.new("RGB", (WIDTH, 10)))
    content_width = WIDTH - PADDING * 2 - 48
    height = 20 + 26
    height += _measure_text(group.address or "Адрес не указан", content_width, _font(34, bold=True), probe) + 12
    for index, order in enumerate(group.orders):
        if index:
            height += 16
        height += _measure_order(order, content_width, probe)
    return max(height + 20, 210)


def _measure_order(order: Order, content_width: int, probe: ImageDraw.ImageDraw) -> int:
    height = 60
    height += _measure_text(order.recipient or "Получатель не указан", content_width, _font(30), probe) + 10

    payment = _payment_text(order)
    if payment:
        height += _measure_text(payment, content_width - 32, _font(28, bold=True), probe) + 22 + 14

    if order.hours:
        height += 26 + _measure_text(order.hours, content_width, _font(30), probe) + 12
    if order.extra:
        height += 26 + _measure_text(order.extra, content_width, _font(30), probe) + 12
    return height


def _measure_text(value: str, width: int, font: ImageFont.FreeTypeFont, draw: ImageDraw.ImageDraw) -> int:
    return len(_wrap_lines(value, width, font, draw)) * _line_height(font)


def _wrap_lines(value: str, width: int, font: ImageFont.FreeTypeFont, draw: ImageDraw.ImageDraw) -> list[str]:
    result: list[str] = []
    for paragraph in value.split("\n"):
        words = paragraph.split()
        if not words:
            result.append("")
            continue

        line = words[0]
        for word in words[1:]:
            candidate = f"{line} {word}"
            if _text_width(draw, candidate, font) <= width:
                line = candidate
            else:
                result.extend(_split_long_line(line, width, font, draw))
                line = word
        result.extend(_split_long_line(line, width, font, draw))
    return result


def _split_long_line(line: str, width: int, font: ImageFont.FreeTypeFont, draw: ImageDraw.ImageDraw) -> list[str]:
    if _text_width(draw, line, font) <= width:
        return [line]

    result: list[str] = []
    chunk = ""
    for character in line:
        candidate = chunk + character
        if chunk and _text_width(draw, candidate, font) > width:
            result.append(chunk)
            chunk = character
        else:
            chunk = candidate
    if chunk:
        result.append(chunk)
    return result


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    return int(draw.textlength(text, font=font))


def _line_height(font: ImageFont.FreeTypeFont) -> int:
    bbox = font.getbbox("АБВgqy")
    return bbox[3] - bbox[1] + 10


def _header_height() -> int:
    return 140


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        Path(os_font)
        for os_font in (
            r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
            r"C:\Windows\Fonts\seguisb.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        )
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()
