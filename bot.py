import json
import os
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
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
TEAM, ROOM, DATE, TIME, CONFIRM = range(5)

TEAMS = [f"Team {i}" for i in range(1, 17)]
ROOMS = ["Conference Room A", "Conference Room B", "Meeting Pod 1"]
TIME_SLOTS = ["09:00 - 10:00", "10:00 - 11:00", "14:00 - 15:00", "15:00 - 16:00"]

# ==========================================
# 📌 CONFIGURATION: ROOM BOOKING INFO TOPIC
# ==========================================
# Replace 888 with your actual central Room Booking Info Topic ID
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
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    else:
        creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)

    client = gspread.authorize(creds)
    return client.open("Room Bookings").sheet1


def save_booking_to_sheet(user_id, tg_user, team, room, time_slot, booking_date):
    """Saves: User ID | Telegram User | Team | Room | Time Slot | Booking Date"""
    sheet = get_sheet()
    sheet.append_row([str(user_id), tg_user, team, room, time_slot, booking_date])


def is_slot_booked(room, date_str, time_slot):
    sheet = get_sheet()
    records = sheet.get_all_records()
    for row in records:
        if (
            str(row.get("Room")) == room
            and str(row.get("Booking Date")) == date_str
            and str(row.get("Time Slot")) == time_slot
        ):
            return True
    return False


def cleanup_expired_bookings():
    """Deletes rows from Google Sheets whose booking date/time has passed."""
    try:
        sheet = get_sheet()
        records = sheet.get_all_records()
        now = datetime.now()

        # Reverse loop so row deletions don't mess up shifting indices
        for idx, row in reversed(list(enumerate(records, start=2))):
            date_str = str(row.get("Booking Date"))
            time_slot = str(row.get("Time Slot"))

            if date_str and time_slot and "-" in time_slot:
                end_time_str = time_slot.split("-")[-1].strip()
                full_dt_str = f"{date_str} {end_time_str}"
                try:
                    booking_end_dt = datetime.strptime(full_dt_str, "%Y-%m-%d %H:%M")
                    if now > booking_end_dt:
                        sheet.delete_row(idx)
                        print(f"Purged expired reservation row {idx}: {row}")
                except ValueError:
                    continue
    except Exception as e:
        print(f"Error during automatic sheet cleanup: {e}")


# --- Helper: Build Dynamic Date Selector Keyboard ---
def build_date_keyboard(offset_days=0):
    """Generates 7 days starting from today + offset_days with week navigation."""
    today = datetime.now() + timedelta(days=offset_days)
    keyboard = []

    for i in range(7):
        day_date = today + timedelta(days=i)
        label = day_date.strftime("%a, %b %d")
        val = day_date.strftime("%Y-%m-%d")
        keyboard.append([InlineKeyboardButton(f"📅 {label}", callback_data=f"DATE_{val}")])

    nav_row = []
    if offset_days >= 7:
        nav_row.append(
            InlineKeyboardButton("⬅️ Prev Week", callback_data=f"PAGE_{offset_days - 7}")
        )
    nav_row.append(
        InlineKeyboardButton("Next Week ➡️", callback_data=f"PAGE_{offset_days + 7}")
    )
    keyboard.append(nav_row)

    return InlineKeyboardMarkup(keyboard)


# --- Bot Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_type = update.effective_chat.type
    bot_username = (await context.bot.get_me()).username

    # Group/Supergroup Command Trigger
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

    # 4x4 Team Selection Grid
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
        text=f"Selected Team: *{selected_team}*\n\nNow, please select a room:",
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )
    return ROOM


async def room_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    # Route back if returning from a fully booked error screen
    if query.data == "CHOOSE_OTHER_ROOM":
        keyboard = [[InlineKeyboardButton(room, callback_data=room)] for room in ROOMS]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text=f"Selected Team: *{context.user_data['team']}*\n\nPlease select another room:",
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
        return ROOM

    # Handle Next/Prev week navigation
    if query.data.startswith("PAGE_"):
        offset = int(query.data.replace("PAGE_", ""))
        reply_markup = build_date_keyboard(offset)
        await query.edit_message_text(
            text=f"Selected Room: *{context.user_data['room']}*\n\nSelect a date to book:",
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
        return DATE

    selected_room = query.data
    context.user_data["room"] = selected_room

    reply_markup = build_date_keyboard(0)
    await query.edit_message_text(
        text=f"Selected Room: *{selected_room}*\n\nSelect a date to book:",
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )
    return DATE


async def date_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data.startswith("PAGE_"):
        offset = int(query.data.replace("PAGE_", ""))
        reply_markup = build_date_keyboard(offset)
        await query.edit_message_text(
            text=f"Selected Room: *{context.user_data['room']}*\n\nSelect a date to book:",
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
        return DATE

    selected_date = query.data.replace("DATE_", "")
    context.user_data["booking_date"] = selected_date
    selected_room = context.user_data["room"]

    # Filter out time slots that have already passed for today
    valid_slots = [
        slot for slot in TIME_SLOTS if not is_slot_past(selected_date, slot)
    ]

    # If all remaining slots for today are passed or booked
    all_booked = not valid_slots or all(
        is_slot_booked(selected_room, selected_date, slot) for slot in valid_slots
    )

    if all_booked:
        keyboard = [
            [InlineKeyboardButton("🚪 Choose Another Room", callback_data="CHOOSE_OTHER_ROOM")],
            [InlineKeyboardButton("❌ Cancel Booking", callback_data="CANCEL")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text=(
                f"⚠️ *No Available Slots*\n\n"
                f"There are no remaining available time slots for *{selected_room}* on *{selected_date}*.\n"
                f"Please choose another room or date."
            ),
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
        return ROOM

    # Display only future time slots
    keyboard = []
    for slot in valid_slots:
        if is_slot_booked(selected_room, selected_date, slot):
            keyboard.append(
                [InlineKeyboardButton(f"❌ {slot} (Booked)", callback_data="DISABLED")]
            )
        else:
            keyboard.append([InlineKeyboardButton(f"⏰ {slot}", callback_data=slot)])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        text=f"Selected Date: *{selected_date}*\nNow pick an available time slot:",
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )
    return TIME

def is_slot_past(date_str, time_slot):
    """Checks if a given slot's START time has already passed today."""
    try:
        # Extract start time (e.g., "16:00" from "16:00 - 17:00")
        start_time_str = time_slot.split("-")[0].strip()
        full_dt_str = f"{date_str} {start_time_str}"
        slot_start_dt = datetime.strptime(full_dt_str, "%Y-%m-%d %H:%M")

        # Returns True if current time is ahead of the slot start time
        return datetime.now() > slot_start_dt
    except Exception:
        return False

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
            f"• *Date:* {context.user_data['booking_date']}\n"
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
        booking_date = context.user_data["booking_date"]

        # Save to Google Sheets
        save_booking_to_sheet(user_id, tg_user, team, room, time_slot, booking_date)

        # Private Chat Confirmation
        await query.edit_message_text(
            text=(
                f"🎉 *Booking Confirmed!*\n"
                f"• *Team:* {team} ({tg_user})\n"
                f"• *Room:* {room}\n"
                f"• *Date:* {booking_date}\n"
                f"• *Time:* {time_slot}"
            ),
            parse_mode="Markdown",
        )

        # Post Alert to Central Booking Info Topic
        origin_group_id = context.user_data.get("origin_group_id")
        if origin_group_id:
            try:
                booking_alert_message = (
                    f"📢 *New Room Reservation*\n\n"
                    f"👥 *Team:* {team} ({tg_user})\n"
                    f"🚪 *Room:* {room}\n"
                    f"📅 *Date:* {booking_date}\n"
                    f"⏰ *Time Slot:* {time_slot}"
                )
                await context.bot.send_message(
                    chat_id=origin_group_id,
                    message_thread_id=BOOKING_TOPIC_ID,
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


async def run_cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    """Periodic job to clean expired sheet entries."""
    cleanup_expired_bookings()


def main():
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
    )

    app = Application.builder().token(TOKEN).request(request).build()

    # Periodic task: Purges expired bookings every 10 minutes (600 seconds)
    if app.job_queue:
        app.job_queue.run_repeating(run_cleanup_job, interval=600, first=10)

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("book", start), CommandHandler("start", start)],
        states={
            TEAM: [CallbackQueryHandler(team_choice, pattern="^TEAM_")],
            ROOM: [
                CallbackQueryHandler(cancel, pattern="^CANCEL$"),
                CallbackQueryHandler(room_choice),
            ],
            DATE: [CallbackQueryHandler(date_choice, pattern="^(DATE_|PAGE_)")],
            TIME: [CallbackQueryHandler(time_choice)],
            CONFIRM: [CallbackQueryHandler(confirm_booking)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_user=True,
        per_message=False,
    )

    app.add_handler(conv_handler)
    print("Bot running with full feature set...")
    app.run_polling()


if __name__ == "__main__":
    main()