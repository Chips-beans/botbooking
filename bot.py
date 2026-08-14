import json
import os
import threading
import asyncio
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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import libsql

load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
LOCAL_TZ = ZoneInfo("Asia/Phnom_Penh")
TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

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
DEFAULT_GROUP_ID = -1003850589682


# # --- Dummy HTTP Server for Render Health Checks ---
# # --- Dummy HTTP Server for Render Health Checks ---
# class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
#     def do_GET(self):
#         self.send_response(200)
#         self.send_header("Content-type", "text/plain")
#         self.end_headers()
#         self.wfile.write(b"Bot is running 24/7!")

#     def do_HEAD(self):
#         # Render sends HEAD requests for health checks!
#         self.send_response(200)
#         self.send_header("Content-type", "text/plain")
#         self.end_headers()

#     # Silence logs so health pings don't flood your console
#     def log_message(self, format, *args):
#         return


# def run_dummy_server():
#     port = int(os.environ.get("PORT", 10000))
#     server = HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
#     print(f"Dummy HTTP server listening on port {port}")
#     server.serve_forever()


# threading.Thread(target=run_dummy_server, daemon=True).start()

# # --- turso Setup-----
# def get_turso_conn():
#     """Returns a connection to Turso database with safety checks."""
#     if not TURSO_URL or not TURSO_TOKEN:
#         raise ValueError("❌ TURSO_DATABASE_URL or TURSO_AUTH_TOKEN is missing from environment variables!")
    
#     return libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)

# def init_turso_db():
#     """Initializes the bookings table in Turso if it doesn't exist."""
#     conn = get_turso_conn()
#     conn.execute("""
#         CREATE TABLE IF NOT EXISTS bookings (
#             id INTEGER PRIMARY KEY AUTOINCREMENT,
#             room TEXT NOT NULL,
#             booking_date TEXT NOT NULL,
#             time_slot TEXT NOT NULL,
#             user_handle TEXT NOT NULL,
#             team TEXT NOT NULL,
#             created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
#             UNIQUE(room, booking_date, time_slot)
#         )
#     """)
#     conn.commit()
#     conn.close()


# def is_slot_booked(room: str, date_str: str, time_slot: str) -> bool:
#     """Checks room availability directly against Turso DB."""
#     try:
#         conn = get_turso_conn()
#         cursor = conn.cursor()
#         cursor.execute(
#             """
#             SELECT 1 FROM bookings 
#             WHERE room = ? AND booking_date = ? AND time_slot = ?
#             """,
#             (room, date_str, time_slot)
#         )
#         row = cursor.fetchone()
#         conn.close()
#         return row is not None
#     except Exception as e:
#         print(f"Error checking Turso slot: {e}")
#         return False

# # --- Google Sheets Setup ---
# def get_sheet():
#     scope = [
#         "https://spreadsheets.google.com/feeds",
#         "https://www.googleapis.com/auth/drive",
#     ]
#     google_creds_json = os.getenv("GOOGLE_CREDENTIALS")
#     if google_creds_json:
#         creds_dict = json.loads(google_creds_json)
#         creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
#     else:
#         creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)

#     client = gspread.authorize(creds)
#     return client.open("Room Bookings").sheet1


# def save_booking_to_sheet(user_id, tg_user, team, room, time_slot, booking_date):
#     """Saves: User ID | Telegram User | Team | Room | Time Slot | Booking Date"""
#     sheet = get_sheet()
#     sheet.append_row([str(user_id), tg_user, team, room, time_slot, booking_date])




# def cleanup_expired_bookings():
#     """Deletes rows from Google Sheets whose booking date/time has passed."""
#     try:
#         sheet = get_sheet()
#         records = sheet.get_all_records()
#         now = datetime.now()

#         # Reverse loop so row deletions don't mess up shifting indices
#         for idx, row in reversed(list(enumerate(records, start=2))):
#             date_str = str(row.get("Booking Date"))
#             time_slot = str(row.get("Time Slot"))

#             if date_str and time_slot and "-" in time_slot:
#                 end_time_str = time_slot.split("-")[-1].strip()
#                 full_dt_str = f"{date_str} {end_time_str}"
#                 try:
#                     booking_end_dt = datetime.strptime(full_dt_str, "%Y-%m-%d %H:%M")
#                     if now > booking_end_dt:
#                         sheet.delete_row(idx)
#                         print(f"Purged expired reservation row {idx}: {row}")
#                 except ValueError:
#                     continue
#     except Exception as e:
#         print(f"Error during automatic sheet cleanup: {e}")


# # --- Helper: Build Dynamic Date Selector Keyboard ---
# def build_date_keyboard(offset_days=0):
#     """Generates 7 days starting from today + offset_days with week navigation."""
#     today = datetime.now() + timedelta(days=offset_days)
#     keyboard = []

#     for i in range(7):
#         day_date = today + timedelta(days=i)
#         label = day_date.strftime("%a, %b %d")
#         val = day_date.strftime("%Y-%m-%d")
#         keyboard.append([InlineKeyboardButton(f"📅 {label}", callback_data=f"DATE_{val}")])

#     nav_row = []
#     if offset_days >= 7:
#         nav_row.append(
#             InlineKeyboardButton("⬅️ Prev Week", callback_data=f"PAGE_{offset_days - 7}")
#         )
#     nav_row.append(
#         InlineKeyboardButton("Next Week ➡️", callback_data=f"PAGE_{offset_days + 7}")
#     )
#     keyboard.append(nav_row)

#     return InlineKeyboardMarkup(keyboard)


# # --- Bot Command Handlers ---
# async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
#     chat_type = update.effective_chat.type
#     bot_username = (await context.bot.get_me()).username

#     # 1. If triggered inside a Group / Supergroup -> Send PM Link
#     if chat_type in ["group", "supergroup"]:
#         group_id = update.effective_chat.id
#         pm_url = f"https://t.me/{bot_username}?start={group_id}"
#         keyboard = [[InlineKeyboardButton("🚪 Book a Room (Private Chat)", url=pm_url)]]
#         reply_markup = InlineKeyboardMarkup(keyboard)

#         await update.message.reply_text(
#             "Click below to reserve a room in a private chat:",
#             reply_markup=reply_markup,
#         )
#         return ConversationHandler.END

#     # 2. If triggered in Private Chat (PM) -> Start Conversation
#     user = update.effective_user
#     tg_handle = f"@{user.username}" if user.username else user.first_name
#     context.user_data["telegram_user"] = tg_handle

#     # Extract group ID from deep link parameter, or fallback to DEFAULT_GROUP_ID
#     if context.args and context.args[0] != "book":
#         try:
#             context.user_data["origin_group_id"] = int(context.args[0])
#         except ValueError:
#             context.user_data["origin_group_id"] = DEFAULT_GROUP_ID
#     else:
#         # Fallback: User started the bot directly in PM without a deep link
#         context.user_data["origin_group_id"] = DEFAULT_GROUP_ID

#     # 3. Build & Display 4x4 Team Selection Grid
#     keyboard = []
#     row = []
#     for team in TEAMS:
#         row.append(InlineKeyboardButton(team, callback_data=f"TEAM_{team}"))
#         if len(row) == 4:
#             keyboard.append(row)
#             row = []
#     if row:
#         keyboard.append(row)

#     reply_markup = InlineKeyboardMarkup(keyboard)
#     await update.message.reply_text(
#         "Welcome to the Room Booking Bot! 🚪\n\nPlease select your *Team*:",
#         parse_mode="Markdown",
#         reply_markup=reply_markup,
#     )
#     return TEAM


# async def team_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
#     query = update.callback_query
#     await query.answer()

#     selected_team = query.data.replace("TEAM_", "")
#     context.user_data["team"] = selected_team

#     keyboard = [[InlineKeyboardButton(room, callback_data=room)] for room in ROOMS]
#     reply_markup = InlineKeyboardMarkup(keyboard)

#     await query.edit_message_text(
#         text=f"Selected Team: *{selected_team}*\n\nNow, please select a room:",
#         parse_mode="Markdown",
#         reply_markup=reply_markup,
#     )
#     return ROOM


# async def room_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
#     query = update.callback_query
#     await query.answer()

#     # Route back if returning from a fully booked error screen
#     if query.data == "CHOOSE_OTHER_ROOM":
#         keyboard = [[InlineKeyboardButton(room, callback_data=room)] for room in ROOMS]
#         reply_markup = InlineKeyboardMarkup(keyboard)
#         await query.edit_message_text(
#             text=f"Selected Team: *{context.user_data['team']}*\n\nPlease select another room:",
#             parse_mode="Markdown",
#             reply_markup=reply_markup,
#         )
#         return ROOM

#     # Handle Next/Prev week navigation
#     if query.data.startswith("PAGE_"):
#         offset = int(query.data.replace("PAGE_", ""))
#         reply_markup = build_date_keyboard(offset)
#         await query.edit_message_text(
#             text=f"Selected Room: *{context.user_data['room']}*\n\nSelect a date to book:",
#             parse_mode="Markdown",
#             reply_markup=reply_markup,
#         )
#         return DATE

#     selected_room = query.data
#     context.user_data["room"] = selected_room

#     reply_markup = build_date_keyboard(0)
#     await query.edit_message_text(
#         text=f"Selected Room: *{selected_room}*\n\nSelect a date to book:",
#         parse_mode="Markdown",
#         reply_markup=reply_markup,
#     )
#     return DATE


# async def date_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
#     query = update.callback_query
#     await query.answer()

#     if query.data.startswith("PAGE_"):
#         offset = int(query.data.replace("PAGE_", ""))
#         reply_markup = build_date_keyboard(offset)
#         await query.edit_message_text(
#             text=f"Selected Room: *{context.user_data['room']}*\n\nSelect a date to book:",
#             parse_mode="Markdown",
#             reply_markup=reply_markup,
#         )
#         return DATE

#     selected_date = query.data.replace("DATE_", "")
#     context.user_data["booking_date"] = selected_date
#     selected_room = context.user_data["room"]

#     # Filter out time slots that have already passed for today
#     valid_slots = [
#         slot for slot in TIME_SLOTS if not is_slot_past(selected_date, slot)
#     ]

#     # If all remaining slots for today are passed or booked
#     all_booked = not valid_slots or all(
#         is_slot_booked(selected_room, selected_date, slot) for slot in valid_slots
#     )

#     if all_booked:
#         keyboard = [
#             [InlineKeyboardButton("🚪 Choose Another Room", callback_data="CHOOSE_OTHER_ROOM")],
#             [InlineKeyboardButton("❌ Cancel Booking", callback_data="CANCEL")],
#         ]
#         reply_markup = InlineKeyboardMarkup(keyboard)
#         await query.edit_message_text(
#             text=(
#                 f"⚠️ *No Available Slots*\n\n"
#                 f"There are no remaining available time slots for *{selected_room}* on *{selected_date}*.\n"
#                 f"Please choose another room or date."
#             ),
#             parse_mode="Markdown",
#             reply_markup=reply_markup,
#         )
#         return ROOM

#     # Display only future time slots
#     keyboard = []
#     for slot in valid_slots:
#         if is_slot_booked(selected_room, selected_date, slot):
#             keyboard.append(
#                 [InlineKeyboardButton(f"❌ {slot} (Booked)", callback_data="DISABLED")]
#             )
#         else:
#             keyboard.append([InlineKeyboardButton(f"⏰ {slot}", callback_data=slot)])

#     reply_markup = InlineKeyboardMarkup(keyboard)
#     await query.edit_message_text(
#         text=f"Selected Date: *{selected_date}*\nNow pick an available time slot:",
#         parse_mode="Markdown",
#         reply_markup=reply_markup,
#     )
#     return TIME

# def is_slot_past(date_str, time_slot):
#     """Checks if a given slot's START time has already passed in Asia/Phnom_Penh."""
#     try:
#         start_time_str = time_slot.split("-")[0].strip()
#         full_dt_str = f"{date_str} {start_time_str}"

#         # Parse and localize the target slot time
#         slot_start_dt = datetime.strptime(
#             full_dt_str, "%Y-%m-%d %H:%M"
#         ).replace(tzinfo=LOCAL_TZ)

#         # Compare against current local time in Phnom Penh
#         now = datetime.now(LOCAL_TZ)

#         return now > slot_start_dt
#     except Exception:
#         return False

# async def time_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
#     query = update.callback_query
#     await query.answer()

#     if query.data == "DISABLED":
#         await query.answer("This slot is already booked. Pick another.", show_alert=True)
#         return TIME

#     selected_time = query.data
#     context.user_data["time_slot"] = selected_time

#     keyboard = [
#         [
#             InlineKeyboardButton("✅ Confirm", callback_data="CONFIRM"),
#             InlineKeyboardButton("❌ Cancel", callback_data="CANCEL"),
#         ]
#     ]
#     reply_markup = InlineKeyboardMarkup(keyboard)

#     await query.edit_message_text(
#         text=(
#             f"📋 *Reservation Summary*\n"
#             f"• *Telegram User:* {context.user_data['telegram_user']}\n"
#             f"• *Team:* {context.user_data['team']}\n"
#             f"• *Room:* {context.user_data['room']}\n"
#             f"• *Date:* {context.user_data['booking_date']}\n"
#             f"• *Time:* {selected_time}\n\n"
#             f"Confirm booking?"
#         ),
#         parse_mode="Markdown",
#         reply_markup=reply_markup,
#     )
#     return CONFIRM


# async def confirm_booking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
#     query = update.callback_query
#     await query.answer()

#     # Handle Cancel button
#     if query.data == "CANCEL":
#         await query.edit_message_text("Booking cancelled.")
#         return ConversationHandler.END

#     # 1. Extract session variables safely
#     tg_user = context.user_data.get("telegram_user", "Unknown User")
#     team = context.user_data.get("team", "N/A")
#     room = context.user_data.get("room")
#     booking_date = context.user_data.get("booking_date")
#     time_slot = context.user_data.get("time_slot")
#     user_id = update.effective_user.id

#     # Fallback guard check
#     if not all([room, booking_date, time_slot]):
#         await query.edit_message_text("⚠️ Missing booking details. Please start over with /start.")
#         return ConversationHandler.END

#     # 2. Validation & Atomic Write to Turso
#     try:
#         conn = get_turso_conn()
#         conn.execute(
#             """
#             INSERT INTO bookings (room, booking_date, time_slot, user_handle, team)
#             VALUES (?, ?, ?, ?, ?)
#             """,
#             (room, booking_date, time_slot, tg_user, team)
#         )
#         conn.commit()
#         conn.close()
#     except libsql.IntegrityError:
#         # Prevents race conditions if two users pick the same slot simultaneously
#         await query.edit_message_text(
#             "⚠️ *Booking Failed!* This slot was just reserved by someone else. Please try another time.",
#             parse_mode="Markdown"
#         )
#         return ConversationHandler.END
#     except Exception as e:
#         print(f"Turso write error: {e}")
#         await query.edit_message_text("⚠️ Database error occurred. Please try again.")
#         return ConversationHandler.END

#     # 3. Append to Google Sheets (History Log)
#     try:
#         save_booking_to_sheet(user_id, tg_user, team, room, time_slot, booking_date)
#     except Exception as e:
#         print(f"Google Sheet logging failed (Turso copy succeeded): {e}")

#     # 4. Post Alert to Telegram Group Topic 35
#     target_group_id = context.user_data.get("origin_group_id") or DEFAULT_GROUP_ID
#     if target_group_id:
#         try:
#             booking_alert_message = (
#                 f"📢 *New Room Reservation*\n\n"
#                 f"👥 *Team:* {team} ({tg_user})\n"
#                 f"🚪 *Room:* {room}\n"
#                 f"📅 *Date:* {booking_date}\n"
#                 f"⏰ *Time Slot:* {time_slot}"
#             )
#             await context.bot.send_message(
#                 chat_id=target_group_id,
#                 message_thread_id=BOOKING_TOPIC_ID,
#                 text=booking_alert_message,
#                 parse_mode="Markdown",
#             )
#         except Exception as e:
#             print(f"Group alert error: {e}")

#     await query.edit_message_text(f"✅ Reservation confirmed for *{room}* on *{booking_date}* ({time_slot})!", parse_mode="Markdown")
#     return ConversationHandler.END

# async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
#     if update.callback_query:
#         await update.callback_query.answer()
#         await update.callback_query.edit_message_text("Booking cancelled.")
#     else:
#         await update.message.reply_text("Operation cancelled.")
#     return ConversationHandler.END


# async def run_cleanup_job(context: ContextTypes.DEFAULT_TYPE):
#     """Periodic job to clean expired sheet entries."""
#     cleanup_expired_bookings()



# def main():
#     # Initialize Turso table on startup
#     init_turso_db()
#     request = HTTPXRequest(
#         connect_timeout=30.0,
#         read_timeout=30.0,
#     )

#     app = Application.builder().token(TOKEN).request(request).build()

#     # Periodic task: Purges expired bookings every 10 minutes (600 seconds)
#     if app.job_queue:
#         app.job_queue.run_repeating(run_cleanup_job, interval=600, first=10)

#     conv_handler = ConversationHandler(
#         entry_points=[CommandHandler("book", start), CommandHandler("start", start)],
#         states={
#             TEAM: [CallbackQueryHandler(team_choice, pattern="^TEAM_")],
#             ROOM: [
#                 CallbackQueryHandler(cancel, pattern="^CANCEL$"),
#                 CallbackQueryHandler(room_choice),
#             ],
#             DATE: [CallbackQueryHandler(date_choice, pattern="^(DATE_|PAGE_)")],
#             TIME: [CallbackQueryHandler(time_choice)],
#             CONFIRM: [CallbackQueryHandler(confirm_booking)],
#         },
#         fallbacks=[CommandHandler("cancel", cancel)],
#         per_chat=True,
#         per_user=True,
#         per_message=False,
#     )

#     app.add_handler(conv_handler)
#     print("Bot running with full feature set...")
#     app.run_polling()


# if __name__ == "__main__":
#     main()



# --- Dummy HTTP Server for Render Health Checks ---
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is running 24/7!")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()

    def log_message(self, format, *args):
        return


def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
    server.serve_forever()


threading.Thread(target=run_dummy_server, daemon=True).start()


# --- Turso Database Connection Management ---
DB_CONN = None

def get_turso_conn():
    """Reuses connection instance to avoid handshake latency."""
    global DB_CONN
    if not TURSO_URL or not TURSO_TOKEN:
        raise ValueError("❌ TURSO_DATABASE_URL or TURSO_AUTH_TOKEN missing!")
    if DB_CONN is None:
        DB_CONN = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
    return DB_CONN


def sync_init_turso_db():
    conn = get_turso_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room TEXT NOT NULL,
            booking_date TEXT NOT NULL,
            time_slot TEXT NOT NULL,
            user_handle TEXT NOT NULL,
            team TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(room, booking_date, time_slot)
        )
    """)
    conn.commit()


def sync_is_slot_booked(room: str, date_str: str, time_slot: str) -> bool:
    try:
        conn = get_turso_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM bookings WHERE room = ? AND booking_date = ? AND time_slot = ?",
            (room, date_str, time_slot)
        )
        return cursor.fetchone() is not None
    except Exception as e:
        print(f"Error checking Turso slot: {e}")
        return False


# Non-blocking async wrapper for room availability
async def is_slot_booked(room: str, date_str: str, time_slot: str) -> bool:
    return await asyncio.to_thread(sync_is_slot_booked, room, date_str, time_slot)


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


def sync_save_booking_to_sheet(user_id, tg_user, team, room, time_slot, booking_date):
    """Saves to Google Sheets (executed in background thread)."""
    try:
        sheet = get_sheet()
        sheet.append_row([str(user_id), tg_user, team, room, time_slot, booking_date])
    except Exception as e:
        print(f"Background Google Sheet write failed: {e}")


def cleanup_expired_bookings():
    try:
        sheet = get_sheet()
        records = sheet.get_all_records()
        now = datetime.now(LOCAL_TZ)

        for idx, row in reversed(list(enumerate(records, start=2))):
            date_str = str(row.get("Booking Date"))
            time_slot = str(row.get("Time Slot"))

            if date_str and time_slot and "-" in time_slot:
                end_time_str = time_slot.split("-")[-1].strip()
                full_dt_str = f"{date_str} {end_time_str}"
                try:
                    booking_end_dt = datetime.strptime(full_dt_str, "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL_TZ)
                    if now > booking_end_dt:
                        sheet.delete_row(idx)
                        print(f"Purged expired reservation row {idx}: {row}")
                except ValueError:
                    continue
    except Exception as e:
        print(f"Error during sheet cleanup: {e}")


# --- Helper Utilities ---
def is_slot_past(date_str: str, time_slot: str) -> bool:
    try:
        start_time_str = time_slot.split("-")[0].strip()
        full_dt_str = f"{date_str} {start_time_str}"
        slot_start_dt = datetime.strptime(full_dt_str, "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL_TZ)
        return datetime.now(LOCAL_TZ) > slot_start_dt
    except Exception:
        return False


def build_date_keyboard(offset_days=0):
    today = datetime.now(LOCAL_TZ) + timedelta(days=offset_days)
    keyboard = []

    for i in range(7):
        day_date = today + timedelta(days=i)
        label = day_date.strftime("%a, %b %d")
        val = day_date.strftime("%Y-%m-%d")
        keyboard.append([InlineKeyboardButton(f"📅 {label}", callback_data=f"DATE_{val}")])

    nav_row = []
    if offset_days >= 7:
        nav_row.append(InlineKeyboardButton("⬅️ Prev Week", callback_data=f"PAGE_{offset_days - 7}"))
    nav_row.append(InlineKeyboardButton("Next Week ➡️", callback_data=f"PAGE_{offset_days + 7}"))
    keyboard.append(nav_row)

    return InlineKeyboardMarkup(keyboard)


# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_type = update.effective_chat.type
    bot_username = (await context.bot.get_me()).username

    if chat_type in ["group", "supergroup"]:
        group_id = update.effective_chat.id
        pm_url = f"https://t.me/{bot_username}?start={group_id}"
        keyboard = [[InlineKeyboardButton("🚪 Book a Room (Private Chat)", url=pm_url)]]
        await update.message.reply_text("Click below to reserve a room in a private chat:", reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END

    user = update.effective_user
    context.user_data["telegram_user"] = f"@{user.username}" if user.username else user.first_name

    if context.args and context.args[0] != "book":
        try:
            context.user_data["origin_group_id"] = int(context.args[0])
        except ValueError:
            context.user_data["origin_group_id"] = DEFAULT_GROUP_ID
    else:
        context.user_data["origin_group_id"] = DEFAULT_GROUP_ID

    keyboard = []
    row = []
    for team in TEAMS:
        row.append(InlineKeyboardButton(team, callback_data=f"TEAM_{team}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    await update.message.reply_text(
        "Welcome to the Room Booking Bot! 🚪\n\nPlease select your *Team*:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return TEAM


async def team_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    selected_team = query.data.replace("TEAM_", "")
    context.user_data["team"] = selected_team

    keyboard = [[InlineKeyboardButton(room, callback_data=room)] for room in ROOMS]
    await query.edit_message_text(
        text=f"Selected Team: *{selected_team}*\n\nNow, please select a room:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ROOM


async def room_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "CHOOSE_OTHER_ROOM":
        keyboard = [[InlineKeyboardButton(room, callback_data=room)] for room in ROOMS]
        await query.edit_message_text(
            text=f"Selected Team: *{context.user_data['team']}*\n\nPlease select another room:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return ROOM

    if query.data.startswith("PAGE_"):
        offset = int(query.data.replace("PAGE_", ""))
        await query.edit_message_text(
            text=f"Selected Room: *{context.user_data['room']}*\n\nSelect a date to book:",
            parse_mode="Markdown",
            reply_markup=build_date_keyboard(offset),
        )
        return DATE

    selected_room = query.data
    context.user_data["room"] = selected_room

    await query.edit_message_text(
        text=f"Selected Room: *{selected_room}*\n\nSelect a date to book:",
        parse_mode="Markdown",
        reply_markup=build_date_keyboard(0),
    )
    return DATE


async def date_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data.startswith("PAGE_"):
        offset = int(query.data.replace("PAGE_", ""))
        await query.edit_message_text(
            text=f"Selected Room: *{context.user_data['room']}*\n\nSelect a date to book:",
            parse_mode="Markdown",
            reply_markup=build_date_keyboard(offset),
        )
        return DATE

    selected_date = query.data.replace("DATE_", "")
    context.user_data["booking_date"] = selected_date
    selected_room = context.user_data["room"]

    valid_slots = [slot for slot in TIME_SLOTS if not is_slot_past(selected_date, slot)]

    # Fetch status concurrently across valid slots using non-blocking calls
    slot_statuses = await asyncio.gather(
        *(is_slot_booked(selected_room, selected_date, slot) for slot in valid_slots)
    )

    all_booked = not valid_slots or all(slot_statuses)

    if all_booked:
        keyboard = [
            [InlineKeyboardButton("🚪 Choose Another Room", callback_data="CHOOSE_OTHER_ROOM")],
            [InlineKeyboardButton("❌ Cancel Booking", callback_data="CANCEL")],
        ]
        await query.edit_message_text(
            text=(
                f"⚠️ *No Available Slots*\n\n"
                f"There are no remaining available time slots for *{selected_room}* on *{selected_date}*.\n"
                f"Please choose another room or date."
            ),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return ROOM

    keyboard = []
    for slot, is_booked in zip(valid_slots, slot_statuses):
        if is_booked:
            keyboard.append([InlineKeyboardButton(f"❌ {slot} (Booked)", callback_data="DISABLED")])
        else:
            keyboard.append([InlineKeyboardButton(f"⏰ {slot}", callback_data=slot)])

    await query.edit_message_text(
        text=f"Selected Date: *{selected_date}*\nNow pick an available time slot:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
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
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CONFIRM


def sync_write_turso_booking(room, booking_date, time_slot, tg_user, team):
    conn = get_turso_conn()
    conn.execute(
        "INSERT INTO bookings (room, booking_date, time_slot, user_handle, team) VALUES (?, ?, ?, ?, ?)",
        (room, booking_date, time_slot, tg_user, team)
    )
    conn.commit()


async def confirm_booking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "CANCEL":
        await query.edit_message_text("Booking cancelled.")
        return ConversationHandler.END

    tg_user = context.user_data.get("telegram_user", "Unknown User")
    team = context.user_data.get("team", "N/A")
    room = context.user_data.get("room")
    booking_date = context.user_data.get("booking_date")
    time_slot = context.user_data.get("time_slot")
    user_id = update.effective_user.id

    if not all([room, booking_date, time_slot]):
        await query.edit_message_text("⚠️ Missing booking details. Please start over with /start.")
        return ConversationHandler.END

    # 1. Non-blocking Atomic Write to Turso
    try:
        await asyncio.to_thread(sync_write_turso_booking, room, booking_date, time_slot, tg_user, team)
    except libsql.IntegrityError:
        await query.edit_message_text("⚠️ *Booking Failed!* This slot was just reserved by someone else.", parse_mode="Markdown")
        return ConversationHandler.END
    except Exception as e:
        print(f"Turso write error: {e}")
        await query.edit_message_text("⚠️ Database error occurred. Please try again.")
        return ConversationHandler.END

    # 2. Fire-and-Forget Background Append to Google Sheets (Zero user wait time!)
    asyncio.create_task(
        asyncio.to_thread(sync_save_booking_to_sheet, user_id, tg_user, team, room, time_slot, booking_date)
    )

    # 3. Non-blocking Notification to Group Topic 35
    target_group_id = context.user_data.get("origin_group_id") or DEFAULT_GROUP_ID
    if target_group_id:
        booking_alert_message = (
            f"📢 *New Room Reservation*\n\n"
            f"👥 *Team:* {team} ({tg_user})\n"
            f"🚪 *Room:* {room}\n"
            f"📅 *Date:* {booking_date}\n"
            f"⏰ *Time Slot:* {time_slot}"
        )
        asyncio.create_task(
            context.bot.send_message(
                chat_id=target_group_id,
                message_thread_id=BOOKING_TOPIC_ID,
                text=booking_alert_message,
                parse_mode="Markdown",
            )
        )

    await query.edit_message_text(f"✅ Reservation confirmed for *{room}* on *{booking_date}* ({time_slot})!", parse_mode="Markdown")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Booking cancelled.")
    else:
        await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END


async def run_cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    await asyncio.to_thread(cleanup_expired_bookings)


def main():
    # Initialize DB synchronously once at startup
    sync_init_turso_db()

    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    app = Application.builder().token(TOKEN).request(request).build()

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
    print("⚡ Fast bot initialized and polling...")
    app.run_polling()


if __name__ == "__main__":
    main()