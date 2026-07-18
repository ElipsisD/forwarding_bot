import asyncio
from contextlib import ExitStack
import logging
import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from telegram import InputMediaPhoto, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from excel_parser import parse_orders
from image_renderer import render_orders


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Пришлите Excel-файл .xlsx, а я соберу из него удобные карточки заказов для водителя."
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

        try:
            image_paths = render_orders(orders, tmp_path)
        except Exception:
            logger.exception("Failed to render %s", file_name)
            await update.message.reply_text("Таблицу прочитал, но не смог собрать изображение.")
            return

        caption = f"Готово: {len(orders)} заказов."
        if len(image_paths) == 1:
            with image_paths[0].open("rb") as image_file:
                await update.message.reply_photo(photo=image_file, caption=caption)
            return

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


async def handle_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Отправьте Excel-файл .xlsx документом.")


def main() -> None:
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Set BOT_TOKEN environment variable before starting the bot.")

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.ALL, handle_other))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
