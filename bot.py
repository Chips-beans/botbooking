import os
from dotenv import load_dotenv
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")

# Conversation States (Added NAME as step 0)
NAME, ROOM, TIME, CONFIRM = range(4)

ROOMS = ["Conference Room A", "Conference Room B", "Meeting Pod 1"]
TIME_SLOTS = ["09:00 - 10:00", "10:00 - 11:00", "14:00 - 15:00", "15:00 - 16:00"]


# Simple HTTP handler for Render health checks
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running 24/7!")

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
    print(f"Dummy HTTP server listening on port {port}")
    server.serve_forever()

# Start the dummy server in a background thread
threading.Thread(target=run_dummy_server, daemon=True).start()

def get_sheet():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        "credentials.json", scope
    )
    client = gspread.authorize(creds)
    return client.open("Room Bookings").sheet1


def save_booking_to_sheet(user_id, custom_name, room, time_slot):
    sheet = get_sheet()
    sheet.append_row([str(user_id), custom_name, room, time_slot])


def is_slot_booked(room, time_slot):
    sheet = get_sheet()
    records = sheet.get_all_records()
    for row in records:
        if row.get("Room") == room and row.get("Time Slot") == time_slot:
            return True
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_type = update.effective_chat.type
    bot_username = (await context.bot.get_me()).username

    # Case 1: Command called in a Group
    if chat_type in ["group", "supergroup"]:
        group_id = update.effective_chat.id
        pm_url = f"https://t.me/{bot_username}?start={group_id}"
        keyboard = [[InlineKeyboardButton("🚪 Book a Room (Private Chat)", url=pm_url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "Click below to reserve a room in a private chat:",
            reply_markup=reply_markup,
        )
        return ConversationHandler.END

    # Case 2: Command called in Private Chat
    if context.args and context.args[0] != "book":
        context.user_data["origin_group_id"] = context.args[0]
    else:
        context.user_data["origin_group_id"] = None

    # Step 1: Prompt for user's full name
    await update.message.reply_text(
        "Welcome to the Room Booking Bot! 🚪\n\nFirst, please type your *Full Name* for the reservation:",
        parse_mode="Markdown",
    )
    return NAME


async def capture_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    entered_name = update.message.text.strip()
    context.user_data["user_name"] = entered_name

    # Step 2: Display room choices after receiving the name
    keyboard = [[InlineKeyboardButton(room, callback_data=room)] for room in ROOMS]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"Thank you, *{entered_name}*!\n\nNow, please select a room:",
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )
    return ROOM


async def room_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    selected_room = query.data
    context.user_data["room"] = selected_room

    keyboard = []
    for slot in TIME_SLOTS:
        if is_slot_booked(selected_room, slot):
            keyboard.append(
                [InlineKeyboardButton(f"❌ {slot} (Booked)", callback_data="DISABLED")]
            )
        else:
            keyboard.append([InlineKeyboardButton(f"⏰ {slot}", callback_data=slot)])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        text=f"Selected Room: *{selected_room}*\nNow pick an available time slot:",
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )
    return TIME


async def time_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "DISABLED":
        await query.answer("This slot is already booked. Pick another.", show_alert=True)
        return TIME

    selected_time = query.data
    context.user_data["time_slot"] = selected_time

    keyboard = [
        [
            InlineKeyboardButton("✅ Confirm", callback_data="CONFIRM"),
            InlineKeyboardButton("❌ Cancel", callback_data="CANCEL"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Show summary including the custom entered name
    await query.edit_message_text(
        text=(
            f"📋 *Reservation Summary*\n"
            f"• *Name:* {context.user_data['user_name']}\n"
            f"• *Room:* {context.user_data['room']}\n"
            f"• *Time:* {selected_time}\n\n"
            f"Confirm booking?"
        ),
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )
    return CONFIRM


async def confirm_booking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "CONFIRM":
        user_id = query.from_user.id
        custom_name = context.user_data["user_name"]
        room = context.user_data["room"]
        time_slot = context.user_data["time_slot"]

        # Save to Google Sheets with custom name
        save_booking_to_sheet(user_id, custom_name, room, time_slot)

        # 1. Private message confirmation
        await query.edit_message_text(
            text=(
                f"🎉 *Booking Confirmed!*\n"
                f"• *Name:* {custom_name}\n"
                f"• *Room:* {room}\n"
                f"• *Time:* {time_slot}"
            ),
            parse_mode="Markdown",
        )

        # 2. Group chat announcement with custom name
        origin_group_id = context.user_data.get("origin_group_id")
        if origin_group_id:
            try:
                group_message = (
                    f"📢 *Room Reservation Alert*\n\n"
                    f"👤 *Booked by:* {custom_name}\n"
                    f"🚪 *Room:* {room}\n"
                    f"⏰ *Time Slot:* {time_slot}"
                )
                await context.bot.send_message(
                    chat_id=origin_group_id,
                    text=group_message,
                    parse_mode="Markdown",
                )
            except Exception as e:
                print(f"Could not send message to group {origin_group_id}: {e}")

    else:
        await query.edit_message_text(text="Booking cancelled.")

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END


def main():
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
    )

    app = Application.builder().token(TOKEN).request(request).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("book", start), CommandHandler("start", start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, capture_name)],
            ROOM: [CallbackQueryHandler(room_choice)],
            TIME: [CallbackQueryHandler(time_choice)],
            CONFIRM: [CallbackQueryHandler(confirm_booking)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_user=True,
        per_message=False,
    )

    app.add_handler(conv_handler)
    print("Bot running with custom name input step...")
    app.run_polling()


if __name__ == "__main__":
    main()