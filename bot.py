import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
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
)
from telegram.request import HTTPXRequest

load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")

# Conversation States
TEAM, ROOM, TIME, CONFIRM = range(4)

TEAMS = [f"Team {i}" for i in range(1, 17)]
ROOMS = ["Conference Room A", "Conference Room B", "Meeting Pod 1"]
TIME_SLOTS = ["09:00 - 10:00", "10:00 - 11:00", "14:00 - 15:00", "15:00 - 16:00"]

# ==========================================
# 📌 CONFIGURATION: ROOM BOOKING INFO TOPIC
# ==========================================
# Replace 888 with your actual Room Booking Information Topic ID
BOOKING_TOPIC_ID = 35


# --- Dummy HTTP Server for Render Health Checks ---
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


threading.Thread(target=run_dummy_server, daemon=True).start()


# --- Google Sheets Setup ---
def get_sheet():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]

    google_creds_json = os.getenv("GOOGLE_CREDENTIALS")
    if google_creds_json:
        creds_dict = json.loads(google_creds_json)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(
            creds_dict, scope
        )
    else:
        creds = ServiceAccountCredentials.from_json_keyfile_name(
            "credentials.json", scope
        )

    client = gspread.authorize(creds)
    return client.open("Room Bookings").sheet1


def save_booking_to_sheet(user_id, tg_user, team, room, time_slot):
    sheet = get_sheet()
    sheet.append_row([str(user_id), tg_user, team, room, time_slot])


def is_slot_booked(room, time_slot):
    sheet = get_sheet()
    records = sheet.get_all_records()
    for row in records:
        if row.get("Room") == room and row.get("Time Slot") == time_slot:
            return True
    return False


# --- Bot Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_type = update.effective_chat.type
    bot_username = (await context.bot.get_me()).username

    # Triggered inside any Team Topic
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

    # Private Chat Flow
    user = update.effective_user
    tg_handle = f"@{user.username}" if user.username else user.first_name
    context.user_data["telegram_user"] = tg_handle

    if context.args and context.args[0] != "book":
        context.user_data["origin_group_id"] = int(context.args[0])
    else:
        context.user_data["origin_group_id"] = None

    # 4x4 Team selection grid
    keyboard = []
    row = []
    for team in TEAMS:
        row.append(InlineKeyboardButton(team, callback_data=f"TEAM_{team}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Welcome to the Room Booking Bot! 🚪\n\nPlease select your *Team*:",
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )
    return TEAM


async def team_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    selected_team = query.data.replace("TEAM_", "")
    context.user_data["team"] = selected_team

    keyboard = [[InlineKeyboardButton(room, callback_data=room)] for room in ROOMS]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text=f"Selected: *{selected_team}*\n\nNow, please select a room:",
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )
    return ROOM


# --- Callback Handler inside `room_choice` ---
async def room_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    # If the user clicked "Choose another room" after seeing an error
    if query.data == "CHOOSE_OTHER_ROOM":
        keyboard = [[InlineKeyboardButton(room, callback_data=room)] for room in ROOMS]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text=f"Selected: *{context.user_data['team']}*\n\nPlease select another room:",
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
        return ROOM

    selected_room = query.data
    context.user_data["room"] = selected_room

    # Check if ALL time slots for this room are booked
    all_booked = all(is_slot_booked(selected_room, slot) for slot in TIME_SLOTS)

    if all_booked:
        keyboard = [
            [InlineKeyboardButton("🚪 Choose Another Room", callback_data="CHOOSE_OTHER_ROOM")],
            [InlineKeyboardButton("❌ Cancel Booking", callback_data="CANCEL")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text=(
                f"⚠️ *Room Fully Booked*\n\n"
                f"All time slots for *{selected_room}* are currently booked.\n"
                f"Please choose another room or cancel."
            ),
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
        return ROOM  # Keep user in ROOM state to handle choice or cancellation

    # Build normal time slot keyboard
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

    await query.edit_message_text(
        text=(
            f"📋 *Reservation Summary*\n"
            f"• *Telegram User:* {context.user_data['telegram_user']}\n"
            f"• *Team:* {context.user_data['team']}\n"
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
        tg_user = context.user_data["telegram_user"]
        team = context.user_data["team"]
        room = context.user_data["room"]
        time_slot = context.user_data["time_slot"]

        # Save to Google Sheets
        save_booking_to_sheet(user_id, tg_user, team, room, time_slot)

        # Private confirmation message to user
        await query.edit_message_text(
            text=(
                f"🎉 *Booking Confirmed!*\n"
                f"• *Team:* {team} ({tg_user})\n"
                f"• *Room:* {room}\n"
                f"• *Time:* {time_slot}"
            ),
            parse_mode="Markdown",
        )

        # Send alert ONLY to the central Room Booking Information Topic
        origin_group_id = context.user_data.get("origin_group_id")
        if origin_group_id:
            try:
                booking_alert_message = (
                    f"📢 *New Room Reservation*\n\n"
                    f"👥 *Team:* {team} ({tg_user})\n"
                    f"🚪 *Room:* {room}\n"
                    f"⏰ *Time Slot:* {time_slot}"
                )
                await context.bot.send_message(
                    chat_id=origin_group_id,
                    message_thread_id=BOOKING_TOPIC_ID,  # Always routes to central topic
                    text=booking_alert_message,
                    parse_mode="Markdown",
                )
            except Exception as e:
                print(f"Could not send message to booking info topic: {e}")

    else:
        await query.edit_message_text(text="Booking cancelled.")

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Booking cancelled.")
    else:
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
            TEAM: [CallbackQueryHandler(team_choice, pattern="^TEAM_")],
            ROOM: [
                CallbackQueryHandler(cancel, pattern="^CANCEL$"),
                CallbackQueryHandler(room_choice),
            ],
            TIME: [CallbackQueryHandler(time_choice)],
            CONFIRM: [CallbackQueryHandler(confirm_booking)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_user=True,
        per_message=False,
    )

    app.add_handler(conv_handler)
    print("Bot running with dedicated Booking Topic routing...")
    app.run_polling()


if __name__ == "__main__":
    main()