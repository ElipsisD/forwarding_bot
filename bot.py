import asyncio
from contextlib import ExitStack
from decimal import Decimal, InvalidOperation
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

from excel_parser import Order, parse_orders
from image_renderer import render_orders
from route_store import Route, RoutePoint, RouteStore
from yandex_geocoder import (
    coordinates_from_navigator_link,
    geocode_addresses,
    geocode_suggestion,
    is_navigator_link,
)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
ROUTE_STORE = RouteStore(Path("data/routes.json"))
POINTS_PER_PAGE = 8


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"Сегодня {_route_date()}. Пришлите Excel-файл .xlsx, а я соберу удобные карточки заказов для водителя."
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    document = update.message.document
    if not document:
        return

    file_name = document.file_name or "orders.xlsx"
    if not file_name.lower().endswith(".xlsx"):
        await update.message.reply_text("Нужен файл Excel в формате .xlsx.")
        return

    await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        xlsx_path = tmp_path / file_name
        telegram_file = await document.get_file()
        await telegram_file.download_to_drive(custom_path=xlsx_path)

        try:
            orders = parse_orders(xlsx_path)
        except Exception:
            logger.exception("Failed to parse %s", file_name)
            await update.message.reply_text(
                "Не получилось прочитать таблицу. Проверьте, что это обычный .xlsx с заголовками в первой строке."
            )
            return

        if not orders:
            await update.message.reply_text("В файле не нашел строк с заказами.")
            return

        await _send_orders(update, orders, tmp_path, file_name)
async def _send_orders(update: Update, orders: list[Order], tmp_path: Path, source_name: str) -> None:
    if not update.message or not update.effective_chat:
        return
    try:
        image_paths = render_orders(orders, tmp_path)
    except Exception:
        logger.exception("Failed to render %s", source_name)
        await update.message.reply_text("Таблицу прочитал, но не смог собрать изображение.")
        return

    caption = f"Готово: {len(orders)} заказов."
    if len(image_paths) == 1:
        with image_paths[0].open("rb") as image_file:
            await update.message.reply_photo(photo=image_file, caption=caption)
    else:
        for batch_start in range(0, len(image_paths), 10):
            image_batch = image_paths[batch_start : batch_start + 10]
            with ExitStack() as stack:
                media = [
                    InputMediaPhoto(
                        media=stack.enter_context(image_path.open("rb")),
                        caption=caption if batch_start == 0 and index == 0 else None,
                    )
                    for index, image_path in enumerate(image_batch)
                ]
                await update.message.reply_media_group(media)
            await asyncio.sleep(0.2)

    addresses_by_key: dict[str, str] = {}
    for order in orders:
        address = order.address or "Адрес не указан"
        addresses_by_key.setdefault(" ".join(address.casefold().split()), address)
    lookups = await geocode_addresses(
        list(addresses_by_key.values()),
        os.getenv("YANDEX_SUGGEST_API_KEY"),
        os.getenv("YANDEX_GEOCODER_API_KEY"),
    )
    route = await ROUTE_STORE.create_route(update.effective_chat.id, orders, lookups)
    if route.awaiting_suggestion is not None:
        await update.message.reply_text(
            _suggestion_prompt(route), reply_markup=_suggestion_keyboard(route, route.awaiting_suggestion)
        )
    else:
        await _notify_about_coordinate_refinement(update, route)
        await update.message.reply_text(_route_endpoint_prompt(route))


async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Отправьте Excel-файл .xlsx документом.")


async def handle_mileage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text or not update.effective_chat:
        return
    route = await ROUTE_STORE.get_current_route(update.effective_chat.id)
    if route and route.awaiting_coordinate_point is not None:
        point_index = route.awaiting_coordinate_point
        value = update.message.text.strip()
        address = None
        card_uri = None
        if is_navigator_link(value):
            geocode = await coordinates_from_navigator_link(value, os.getenv("YANDEX_GEOCODER_API_KEY"))
        else:
            lookups = await geocode_addresses(
                [value],
                os.getenv("YANDEX_SUGGEST_API_KEY"),
                os.getenv("YANDEX_GEOCODER_API_KEY"),
            )
            lookup = lookups.get(" ".join(value.casefold().split()))
            geocode = lookup.geocode if lookup else None
            address = value
            card_uri = lookup.card_uri if lookup else None
        if geocode is None:
            await update.message.reply_text(
                "Не удалось определить координаты. Отправьте ссылку на точку из Яндекс Навигатора "
                "или укажите адрес точнее."
            )
            return
        if address is None:
            updated = await ROUTE_STORE.save_manual_coordinates(update.effective_chat.id, point_index, geocode)
        else:
            updated = await ROUTE_STORE.save_manual_address_geocode(
                update.effective_chat.id, point_index, address, geocode, card_uri
            )
        if not updated:
            return
        point = updated.points[point_index]
        if updated.awaiting_route_endpoint:
            await update.message.reply_text(_route_endpoint_prompt(updated))
            return
        if updated.awaiting_mileage == "start":
            await update.message.reply_text("Координаты уточнены. Введите стартовый пробег автомобиля в километрах.")
            return
        await update.message.reply_text(
            _point_details(point), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=_point_keyboard(updated, point_index)
        )
        return
    if route and route.awaiting_route_endpoint:
        value = update.message.text.strip()
        lookups = await geocode_addresses(
            [value],
            os.getenv("YANDEX_SUGGEST_API_KEY"),
            os.getenv("YANDEX_GEOCODER_API_KEY"),
        )
        lookup = lookups.get(" ".join(value.casefold().split()))
        geocode = lookup.geocode if lookup else None
        if not geocode or not geocode.latitude or not geocode.longitude:
            await update.message.reply_text("Не удалось найти точку. Укажите адрес точнее.")
            return
        updated = await ROUTE_STORE.save_route_endpoint(
            update.effective_chat.id, route.awaiting_route_endpoint, geocode
        )
        if not updated:
            return
        if updated.awaiting_route_endpoint:
            await update.message.reply_text(_route_endpoint_prompt(updated))
        else:
            await update.message.reply_text("Введите стартовый пробег автомобиля в километрах.")
        return
    if not route or not route.awaiting_mileage:
        await handle_other(update, context)
        return

    mileage = _parse_mileage(update.message.text)
    if mileage is None:
        await update.message.reply_text("Введите пробег числом в километрах, например: 125430.5")
        return
    if route.awaiting_mileage == "final" and route.start_mileage is not None:
        if mileage < Decimal(route.start_mileage):
            await update.message.reply_text("Финальный пробег не может быть меньше стартового. Введите значение еще раз.")
            return

    updated = await ROUTE_STORE.save_mileage(update.effective_chat.id, _format_mileage(mileage))
    if not updated:
        return
    if updated.final_mileage is not None and updated.start_mileage is not None:
        distance = Decimal(updated.final_mileage) - Decimal(updated.start_mileage)
        await update.message.reply_text(
            f"Маршрут от {_route_date()} завершен. Пройдено: {_format_mileage(distance)} км."
        )
        return
    await update.message.reply_text(
        _points_prompt(updated),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_points_keyboard(updated),
    )


async def handle_route_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not update.effective_chat:
        return
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 4 or parts[0] != "route":
        return
    _, route_id, action, value = parts
    route = await ROUTE_STORE.get_route(update.effective_chat.id, route_id)
    if route is None:
        await query.edit_message_text("Этот маршрут больше недоступен. Отправьте Excel-файл заново.")
        return

    if action == "page":
        await query.edit_message_text(
            _points_prompt(route), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=_points_keyboard(route, int(value))
        )
        return
    if action == "select":
        route = await ROUTE_STORE.select_point(update.effective_chat.id, route_id, int(value))
        if route is None:
            current_route = await ROUTE_STORE.get_route(update.effective_chat.id, route_id)
            if current_route:
                await query.edit_message_text(
                    _points_prompt(current_route, "Точка уже выполнена или недоступна."),
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=_points_keyboard(current_route),
                )
            return
        point = route.points[int(value)]
        await query.edit_message_text(
            _point_details(point),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_point_keyboard(route, int(value)),
        )
        return
    if action == "back":
        route = await ROUTE_STORE.clear_selection(update.effective_chat.id, route_id)
        if route:
            await query.edit_message_text(
                _points_prompt(route), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=_points_keyboard(route, int(value))
            )
        return
    if action == "refine":
        route = await ROUTE_STORE.request_coordinate_refinement(update.effective_chat.id, route_id, int(value))
        if route:
            await query.edit_message_text(
                "Отправьте ссылку на карточку точки из Яндекс Навигатора.\n"
                "Или напишите точный адрес — я найду его через API.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Назад к точкам", callback_data=f"route:{route.id}:refineback:0")]]
                ),
            )
        return
    if action == "refineback":
        route = await ROUTE_STORE.cancel_coordinate_refinement(update.effective_chat.id, route_id)
        if route:
            await query.edit_message_text(
                _coordinate_refinement_prompt(route), reply_markup=_coordinate_refinement_keyboard(route)
            )
        return
    if action == "complete":
        route = await ROUTE_STORE.complete_point(update.effective_chat.id, route_id, int(value))
        if route is None:
            await query.edit_message_text("Точка уже недоступна. Выберите другую.")
            return
        if all(point.delivered for point in route.points):
            await query.edit_message_text(
                f"Все точки маршрутного листа выполнены. Дата маршрута: {_route_date()}. "
                "Введите финальный пробег автомобиля в километрах."
            )
            return
        await query.edit_message_text(
            _points_prompt(route, "Заказ выполнен. Выберите следующую точку."),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_points_keyboard(route),
        )


async def handle_suggestion_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not update.effective_chat:
        return
    await query.answer()
    parts = query.data.split(":")
    if len(parts) != 4 or parts[0] != "suggest":
        return
    _, route_id, point_value, suggestion_value = parts
    point_index = int(point_value)
    suggestion_index = int(suggestion_value)
    route = await ROUTE_STORE.get_route(update.effective_chat.id, route_id)
    if not route or route.awaiting_suggestion != point_index:
        await query.edit_message_text("Этот выбор больше недоступен. Отправьте Excel-файл заново.")
        return
    point = route.points[point_index]
    if not point.suggestions or suggestion_index < 0 or suggestion_index >= len(point.suggestions):
        await query.edit_message_text("Вариант адреса больше недоступен.")
        return
    geocode = await geocode_suggestion(
        point.suggestions[suggestion_index].uri, os.getenv("YANDEX_GEOCODER_API_KEY")
    )
    if geocode is None:
        await query.edit_message_text("Не удалось получить координаты. Выберите вариант еще раз.", reply_markup=_suggestion_keyboard(route, point_index))
        return
    route = await ROUTE_STORE.save_suggestion_geocode(
        update.effective_chat.id, route_id, point_index, suggestion_index, geocode
    )
    if not route:
        return
    if route.awaiting_suggestion is not None:
        await query.edit_message_text(
            _suggestion_prompt(route), reply_markup=_suggestion_keyboard(route, route.awaiting_suggestion)
        )
        return
    await query.edit_message_text("Адреса уточнены. Укажите стартовую точку маршрута текстом.")
    await _notify_about_coordinate_refinement(update, route)


def _points_keyboard(route: Route, page: int = 0) -> InlineKeyboardMarkup:
    pending = [(index, point) for index, point in enumerate(route.points) if not point.delivered]
    page_count = max(1, (len(pending) + POINTS_PER_PAGE - 1) // POINTS_PER_PAGE)
    page = min(max(page, 0), page_count - 1)
    chunk = pending[page * POINTS_PER_PAGE : (page + 1) * POINTS_PER_PAGE]
    buttons = [
        [InlineKeyboardButton(point.address, callback_data=f"route:{route.id}:select:{index}")]
        for index, point in chunk
    ]
    if page_count > 1:
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton("Назад", callback_data=f"route:{route.id}:page:{page - 1}"))
        navigation.append(InlineKeyboardButton(f"{page + 1}/{page_count}", callback_data=f"route:{route.id}:page:{page}"))
        if page < page_count - 1:
            navigation.append(InlineKeyboardButton("Далее", callback_data=f"route:{route.id}:page:{page + 1}"))
        buttons.append(navigation)
    return InlineKeyboardMarkup(buttons)


def _points_prompt(route: Route, message: str = "Выберите точку, в которую едет курьер.") -> str:
    coordinates = [
        f"{point.latitude},{point.longitude}"
        for point in route.points
        if not point.delivered and point.latitude and point.longitude
    ]
    if route.route_start_latitude and route.route_start_longitude:
        coordinates.insert(0, f"{route.route_start_latitude},{route.route_start_longitude}")
    if route.route_end_latitude and route.route_end_longitude:
        coordinates.append(f"{route.route_end_latitude},{route.route_end_longitude}")
    if len(coordinates) < 2:
        return _markdown(message)
    return f"[Построить маршрут в Яндекс Навигаторе]({_route_navigator_link(coordinates)})\n\n{_markdown(message)}"


def _point_keyboard(route: Route, point_index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("УТОЧНИТЬ КООРДИНАТЫ", callback_data=f"route:{route.id}:refine:{point_index}")],
            [InlineKeyboardButton("ЗАКАЗ ВЫПОЛНЕН", callback_data=f"route:{route.id}:complete:{point_index}")],
            [InlineKeyboardButton("Выбрать другую точку", callback_data=f"route:{route.id}:back:0")],
        ]
    )


def _suggestion_prompt(route: Route) -> str:
    point = route.points[route.awaiting_suggestion]
    return f"Уточните адрес для точки: {point.address}"


def _suggestion_keyboard(route: Route, point_index: int) -> InlineKeyboardMarkup:
    point = route.points[point_index]
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(option.title, callback_data=f"suggest:{route.id}:{point_index}:{index}")]
            for index, option in enumerate(point.suggestions or [])
        ]
    )


async def _notify_about_coordinate_refinement(update: Update, route: Route) -> None:
    points = [point.address for point in route.points if _needs_coordinate_refinement(point)]
    if not points:
        return
    addresses = "\n".join(f"• {address}" for address in points)
    await update.effective_message.reply_text(
        _coordinate_refinement_prompt(route, addresses),
        reply_markup=_coordinate_refinement_keyboard(route),
    )


def _needs_coordinate_refinement(point: RoutePoint) -> bool:
    return not point.address_confirmed and (point.geocode_quality or "").startswith("недостаточно данных")


def _coordinate_refinement_keyboard(route: Route) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(point.address, callback_data=f"route:{route.id}:refine:{index}")]
            for index, point in enumerate(route.points)
            if _needs_coordinate_refinement(point)
        ]
    )


def _coordinate_refinement_prompt(route: Route, addresses: str | None = None) -> str:
    if addresses is None:
        addresses = "\n".join(
            f"• {point.address}" for point in route.points if _needs_coordinate_refinement(point)
        )
    return (
        "Для следующих точек API вернул недостаточно данных для точных координат:\n"
        f"{addresses}\n\n"
        "Выберите точку и отправьте ссылку на неё из Яндекс Навигатора."
    )


def _route_endpoint_prompt(route: Route) -> str:
    if route.awaiting_route_endpoint == "start":
        return "Укажите стартовую точку маршрута текстом."
    return "Укажите конечную точку маршрута текстом."


def _point_details(point: RoutePoint) -> str:
    lines = ["*ТОЧКА ДОСТАВКИ*", _markdown(point.address)]
    if point.latitude and point.longitude:
        lines.append(f"*Координаты:* {_markdown(point.latitude)}, {_markdown(point.longitude)}")
        lines.append(
            f"[Открыть в Яндекс Навигаторе]({_navigator_link(point.latitude, point.longitude, point.card_uri)})"
        )
    if point.geocode_quality:
        lines.append(f"*Геокодирование:* {_markdown(point.geocode_quality)}")
    lines.append("")
    for position, order in enumerate(point.orders, start=1):
        lines.extend(
            [
                f"*ЗАКАЗ {position}*  \\#{_markdown(order.number or 'без номера')}",
                f"*Получатель:* {_markdown(order.recipient or 'не указан')}",
                f"*Мест:* {_markdown(order.places or 'не указано')}",
            ]
        )
        if order.payment or order.amount:
            lines.append(f"*ОПЛАТА:* *{_markdown(order.payment or 'не указан способ')}*")
            if order.amount:
                lines.append(f"*СУММА К ОПЛАТЕ:* *{_markdown(order.amount)}*")
        if order.hours:
            lines.append(f"*Время работы:* {_markdown(order.hours)}")
        if order.extra:
            lines.append(f"*Доп\\. информация:* {_markdown(order.extra)}")
        if position < len(point.orders):
            lines.append("")
    return "\n".join(lines)


def _markdown(value: str) -> str:
    return escape_markdown(value, version=2)


def _navigator_link(latitude: str, longitude: str, card_uri: str | None = None) -> str:
    if card_uri:
        return "https://yandex.ru/navi?" + urlencode({"uri": card_uri})
    return "https://yandex.ru/navi?" + urlencode(
        {
            "whatshere[zoom]": "14",
            "whatshere[point]": f"{longitude},{latitude}",
        }
    )


def _route_navigator_link(coordinates: list[str]) -> str:
    return f"https://yandex.ru/navi?rtext={'~'.join(coordinates)}&rtt=auto"


def _parse_mileage(value: str) -> Decimal | None:
    try:
        mileage = Decimal(value.strip().replace(",", "."))
    except InvalidOperation:
        return None
    return mileage if mileage.is_finite() and mileage >= 0 else None


def _format_mileage(value: Decimal) -> str:
    return format(value, "f").rstrip("0").rstrip(".") or "0"


def _route_date() -> str:
    return datetime.now(ZoneInfo("Asia/Krasnoyarsk")).strftime("%d.%m.%Y")


def main() -> None:
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Set BOT_TOKEN environment variable before starting the bot.")

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_suggestion_callback, pattern=r"^suggest:"))
    application.add_handler(CallbackQueryHandler(handle_route_callback, pattern=r"^route:"))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_mileage))
    application.add_handler(MessageHandler(filters.ALL, handle_other))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
