import logging
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from bot.config import BOT_TOKEN
from bot.database.db import init_db
from bot.handlers.start import start, help_command
from bot.handlers.quiz import quiz
from bot.handlers.chapter import chapter_quiz
from bot.handlers.pdf import pdf_handler
from bot.handlers.leaderboard import leaderboard
from bot.handlers.profile import profile
from bot.handlers.resume import resume
from bot.handlers.admin import stats
from bot.handlers.chat import chat

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

def build_app():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing. Railway Variables में इसे जोड़ें.")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("quiz", quiz))
    app.add_handler(CommandHandler("chapter", chapter_quiz))
    app.add_handler(CommandHandler("pdf", pdf_handler))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("resume", resume))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    app.add_handler(MessageHandler(filters.Document.PDF, pdf_handler))
    return app

if __name__ == "__main__":
    app = build_app()
    logger.info("NEET AI Bot starting...")
    app.run_polling(allowed_updates=["message"])
