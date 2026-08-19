import os
import pytz
from datetime import time
from dotenv import load_dotenv

from telegram import BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
)
from telegram.request import HTTPXRequest

import database as db
import handlers

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
LOCAL_TZ = pytz.timezone("Asia/Phnom_Penh")


async def post_init(application: Application):
    commands = [
        BotCommand("book", "Book a room slot"),
        BotCommand("cancel_booking", "Cancel an existing reservation"),
        BotCommand("restart", "Restart the current booking process"),
        BotCommand("cancel", "Cancel current step"),
    ]
    await application.bot.set_my_commands(commands)


def main():
    # Start HTTP Dummy Server for 24/7 keep-alive
    db.start_dummy_server()

    # Initialize SQLite schema
    db.sync_init_db()

    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)

    app = (
        Application.builder()
        .token(TOKEN)
        .request(request)
        .post_init(post_init)
        .build()
    )

    app.add_error_handler(handlers.error_handler)

    if app.job_queue:
        app.job_queue.run_repeating(handlers.run_cleanup_job, interval=600, first=10)

        # Schedule weekly quota reset every Monday at 00:00 (Asia/Phnom_Penh)
        app.job_queue.run_daily(
            handlers.weekly_quota_reset_job,
            time=time(hour=0, minute=0, second=0, tzinfo=LOCAL_TZ),
            days=(1,),
            name="weekly_quota_reset",
        )

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("book", handlers.start),
            CommandHandler("start", handlers.start),
            CommandHandler("restart", handlers.restart_command),
        ],
        states={
            handlers.TEAM: [CallbackQueryHandler(handlers.team_choice, pattern="^TEAM_")],
            handlers.ROOM: [
                CallbackQueryHandler(handlers.back_to_team, pattern="^BACK_TO_TEAM$"),
                CallbackQueryHandler(handlers.cancel, pattern="^CANCEL$"),
                CallbackQueryHandler(handlers.room_choice),
            ],
            handlers.DATE: [
                CallbackQueryHandler(handlers.back_to_room, pattern="^BACK_TO_ROOM$"),
                CallbackQueryHandler(handlers.cancel, pattern="^CANCEL$"),
                CallbackQueryHandler(handlers.date_choice, pattern="^(DATE_|PAGE_|IGNORE)"),
            ],
            handlers.START_TIME: [
                CallbackQueryHandler(handlers.back_to_date, pattern="^BACK_TO_DATE$"),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, handlers.receive_start_time
                ),
            ],
            handlers.ENTER_MINUTES: [
                CallbackQueryHandler(handlers.back_to_start_time, pattern="^BACK_TO_START_TIME$"),
                CallbackQueryHandler(handlers.cancel, pattern="^CANCEL$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.receive_minutes),
            ],
            handlers.CONFIRM: [
                CallbackQueryHandler(
                    handlers.confirm_booking, pattern="^(CONFIRM|CANCEL)$"
                )
            ],
        },
        fallbacks=[
            CommandHandler("cancel", handlers.cancel),
            CommandHandler("restart", handlers.restart_command),
            CallbackQueryHandler(handlers.cancel, pattern="^CANCEL$"),
        ],
        per_chat=True,
        per_user=True,
        per_message=False,
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("cancel_booking", handlers.list_bookings_to_cancel))
    app.add_handler(CallbackQueryHandler(handlers.handle_cancel_booking_callback, pattern=r"^cancel_db_\d+$"))

    print("⚡ Room booking bot running with SQLite...")
    app.run_polling()


if __name__ == "__main__":
    main()