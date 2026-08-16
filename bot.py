import os
import re
import json
import asyncio
import threading
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import pytz
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from datetime import datetime, date, timedelta

# Telegram imports
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest

# Database & Sheets imports
import libsql
import gspread
from oauth2client.service_account import ServiceAccountCredentials

load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
LOCAL_TZ = ZoneInfo("Asia/Phnom_Penh")
# LOCAL_TZ = ZoneInfo("Pacific/Auckland")
TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")
DEFAULT_GROUP_ID = int(os.getenv("DEFAULT_GROUP_ID", "-1004469241236"))
BOOKING_TOPIC_ID = int(os.getenv("BOOKING_TOPIC_ID", "57"))
TEAMS = [f"Team {i}" for i in range(1, 17)]
ROOMS = ["A203", "A205"]
ALLOWED_DAYS = [0, 1, 3, 4, 5, 6]
# Conversation States
TEAM, ROOM, DATE, START_TIME, ENTER_MINUTES, CONFIRM = range(6)


# --- Helpers ---
def esc(text: str) -> str:
    """Escapes Telegram MarkdownV2 special characters."""
    if not isinstance(text, str):
        text = str(text)
    return re.sub(r"([_\[\]\(\)~`>#+\-=|{}.!])", r"\\\1", text)


# --- Dummy HTTP Server ---
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


# --- Database Connection Management ---
DB_CONN = None


def is_date_in_current_week(date_str: str) -> bool:
    """Returns True if the given YYYY-MM-DD date is within the current week (Mon-Sun)."""
    try:
        booking_dt = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return False

    today = datetime.now(LOCAL_TZ).date()
    start_of_week = today - timedelta(days=today.weekday())
    end_of_week = start_of_week + timedelta(days=6)

    return start_of_week <= booking_dt <= end_of_week


def get_turso_conn():
    global DB_CONN
    if not TURSO_URL or not TURSO_TOKEN:
        raise ValueError("❌ TURSO_DATABASE_URL or TURSO_AUTH_TOKEN missing!")
    if DB_CONN is None:
        DB_CONN = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
    return DB_CONN


def sync_init_turso_db():
    conn = get_turso_conn()
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room TEXT NOT NULL,
            booking_date TEXT NOT NULL,
            start_minutes INTEGER NOT NULL,
            duration_minutes INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            user_handle TEXT NOT NULL,
            team TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS team_quota (
            team TEXT PRIMARY KEY,
            remaining_minutes INTEGER DEFAULT 120
        )
    """
    )

    cursor.execute("PRAGMA table_info(bookings)")
    columns = [row[1] for row in cursor.fetchall()]

    if "start_minutes" not in columns:
        cursor.execute("ALTER TABLE bookings ADD COLUMN start_minutes INTEGER")
        if "start_hour" in columns:
            cursor.execute(
                """
                UPDATE bookings 
                SET start_minutes = CASE 
                    WHEN start_hour <= 24 THEN start_hour * 60 
                    ELSE start_hour 
                END
            """
            )

    conn.commit()


def sync_get_team_quota(team_name: str) -> int:
    conn = get_turso_conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT remaining_minutes FROM team_quota WHERE team = ?", (team_name,)
    )
    row = cursor.fetchone()
    if not row:
        cursor.execute(
            "INSERT INTO team_quota (team, remaining_minutes) VALUES (?, 120)",
            (team_name,),
        )
        conn.commit()
        return 120
    return row[0]


def sync_update_team_quota(team_name: str, minutes_to_subtract: int):
    conn = get_turso_conn()
    conn.execute(
        "UPDATE team_quota SET remaining_minutes = remaining_minutes - ? WHERE team = ?",
        (minutes_to_subtract, team_name),
    )
    conn.commit()


def sync_get_booked_slots_summary(room: str, date_str: str) -> list[str]:
    conn = get_turso_conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT start_minutes, duration_minutes, team, user_handle FROM bookings WHERE room = ? AND booking_date = ? ORDER BY start_minutes ASC",
        (room, date_str),
    )
    rows = cursor.fetchall()

    formatted_slots = []
    for start_val, duration, team, user_handle in rows:
        st_min = start_val if start_val > 24 else start_val * 60
        end_min = st_min + duration

        st_hr, st_m = st_min // 60, st_min % 60
        end_hr, end_m = end_min // 60, end_min % 60

        formatted_slots.append(
            f"• 🔴 `{st_hr:02d}:{st_m:02d} - {end_hr:02d}:{end_m:02d}` ({team} - {user_handle})"
        )

    return formatted_slots


def sync_is_slot_conflicting(
    room: str, date_str: str, new_start_min: int, new_duration: int
) -> bool:
    try:
        conn = get_turso_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT start_minutes, duration_minutes FROM bookings WHERE room = ? AND booking_date = ?",
            (room, date_str),
        )
        rows = cursor.fetchall()

        new_end_min = new_start_min + new_duration
        for start_val, duration in rows:
            existing_start_min = start_val if start_val > 24 else start_val * 60
            existing_end_min = existing_start_min + duration

            if (
                new_start_min < existing_end_min
                and new_end_min > existing_start_min
            ):
                return True
        return False
    except Exception as e:
        print(f"Error checking slot conflict: {e}")
        return True


async def is_slot_conflicting(
    room: str, date_str: str, new_start_min: int, new_duration: int
) -> bool:
    return await asyncio.to_thread(
        sync_is_slot_conflicting, room, date_str, new_start_min, new_duration
    )


# --- Google Sheets Integration ---
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


def sync_save_booking_to_sheet(
    user_id, tg_user, team, room, time_slot_str, booking_date
):
    try:
        sheet = get_sheet()
        sheet.append_row(
            [str(user_id), tg_user, team, room, time_slot_str, booking_date]
        )
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
                    booking_end_dt = datetime.strptime(
                        full_dt_str, "%Y-%m-%d %H:%M"
                    ).replace(tzinfo=LOCAL_TZ)
                    if now > booking_end_dt:
                        sheet.delete_row(idx)
                except ValueError:
                    continue
    except Exception as e:
        print(f"Error during sheet cleanup: {e}")


# --- UI & Keyboards ---
def build_team_keyboard():
    keyboard = []
    row = []
    for team in TEAMS:
        row.append(InlineKeyboardButton(team, callback_data=f"TEAM_{team}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return InlineKeyboardMarkup(keyboard)


def build_room_keyboard():
    keyboard = [[InlineKeyboardButton(room, callback_data=room)] for room in ROOMS]
    keyboard.append([InlineKeyboardButton("⬅️ Back to Teams", callback_data="BACK_TO_TEAM")])
    return InlineKeyboardMarkup(keyboard)


def build_date_keyboard(offset_days=0):
    now = datetime.now(LOCAL_TZ)
    today_date = now.date()
    current_time_mins = now.hour * 60 + now.minute
    CLOSE_MIN = 17 * 60  # 5:00 PM closing time

    # Calculate Monday of target week
    base_date = today_date + timedelta(days=offset_days)
    monday_of_week = base_date - timedelta(days=base_date.weekday())

    keyboard = []

    for i in range(7):
        day_date = monday_of_week + timedelta(days=i)
        if day_date.weekday() in ALLOWED_DAYS:
            label = day_date.strftime("%a, %b %d")
            val = day_date.strftime("%Y-%m-%d")

            # Mark as passed if it's a previous day OR if it's today and past 17:00
            is_past_day = day_date < today_date
            is_today_closed = (day_date == today_date and current_time_mins >= CLOSE_MIN)

            if is_past_day or is_today_closed:
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"❌ {label} (Passed)", callback_data="IGNORE"
                        )
                    ]
                )
            else:
                keyboard.append(
                    [InlineKeyboardButton(f"📅 {label}", callback_data=f"DATE_{val}")]
                )
    keyboard.append([InlineKeyboardButton("⬅️ Back to Rooms", callback_data="BACK_TO_ROOM")])

    return InlineKeyboardMarkup(keyboard)

def parse_time_to_minutes(time_str: str) -> int | None:
    time_str = time_str.strip()
    match = re.match(r"^(\d{1,2})[:.](\d{2})$", time_str)
    if not match:
        return None

    hr, mn = int(match.group(1)), int(match.group(2))

    if 1 <= hr <= 5:
        hr += 12

    if hr < 0 or hr > 23 or mn < 0 or mn > 59:
        return None

    return hr * 60 + mn


# --- Conversation Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_type = update.effective_chat.type
    bot_username = (await context.bot.get_me()).username

    if chat_type in ["group", "supergroup"]:
        group_id = update.effective_chat.id
        pm_url = f"https://t.me/{bot_username}?start={group_id}"
        keyboard = [
            [InlineKeyboardButton("🚪 Book a Room (Private Chat)", url=pm_url)]
        ]
        await update.message.reply_text(
            "Click below to reserve a room in a private chat:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return ConversationHandler.END

    user = update.effective_user
    context.user_data["telegram_user"] = (
        f"@{user.username}" if user.username else user.first_name
    )

    if context.args and context.args[0] != "book":
        try:
            context.user_data["origin_group_id"] = int(context.args[0])
        except ValueError:
            context.user_data["origin_group_id"] = DEFAULT_GROUP_ID
    else:
        context.user_data["origin_group_id"] = DEFAULT_GROUP_ID

    await update.message.reply_text(
        "Welcome to the Room Booking Bot! 🚪\n\nPlease select your *Team*:",
        parse_mode="Markdown",
        reply_markup=build_team_keyboard(),
    )
    return TEAM


async def team_choice(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()

    selected_team = query.data.replace("TEAM_", "")
    context.user_data["team"] = selected_team

    await query.edit_message_text(
        text=f"Selected Team: *{selected_team}*\n\nNow, please select a room:",
        parse_mode="Markdown",
        reply_markup=build_room_keyboard(),
    )
    return ROOM


async def back_to_team(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        text="Please select your *Team*:",
        parse_mode="Markdown",
        reply_markup=build_team_keyboard(),
    )
    return TEAM


async def room_choice(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "BACK_TO_TEAM":
        return await back_to_team(update, context)

    if query.data.startswith("PAGE_"):
        offset = int(query.data.replace("PAGE_", ""))
        await query.edit_message_text(
            text=f"Selected Room: *{context.user_data['room']}*\n\nSelect an allowed day (Mon, Tue, Thu, Fri):",
            parse_mode="Markdown",
            reply_markup=build_date_keyboard(offset),
        )
        return DATE

    selected_room = query.data
    context.user_data["room"] = selected_room

    await query.edit_message_text(
        text=f"Selected Room: *{selected_room}*\n\nSelect an allowed day (Mon, Tue, Thu, Fri):",
        parse_mode="Markdown",
        reply_markup=build_date_keyboard(0),
    )
    return DATE


async def back_to_room(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()

    selected_team = context.user_data.get("team", "N/A")
    await query.edit_message_text(
        text=f"Selected Team: *{selected_team}*\n\nNow, please select a room:",
        parse_mode="Markdown",
        reply_markup=build_room_keyboard(),
    )
    return ROOM


async def date_choice(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query

    if query.data == "IGNORE":
        await query.answer(
            "❌ You cannot select a past date!", show_alert=True
        )
        return DATE

    await query.answer()

    if query.data == "BACK_TO_ROOM":
        return await back_to_room(update, context)

    selected_date = query.data.replace("DATE_", "")

    if not is_date_in_current_week(selected_date):
        await query.answer(
            "❌ You can only book dates for the current week!",
            show_alert=True,
        )
        return DATE

    context.user_data["booking_date"] = selected_date
    room = context.user_data["room"]

    booked_slots = await asyncio.to_thread(
        sync_get_booked_slots_summary, room, selected_date
    )

    if booked_slots:
        schedule_text = "📅 *Existing Reservations \\(Start \\- End\\):*\n" + "\n".join(
            [esc(slot) for slot in booked_slots]
        )
    else:
        schedule_text = "🟢 *No bookings yet for this date\\! Entire schedule is open\\.*"

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back to Date Selection", callback_data="BACK_TO_DATE")]]
    )

    await query.edit_message_text(
        text=(
            f"🚪 Room: *{esc(room)}*\n"
            f"📅 Date: *{esc(selected_date)}*\n\n"
            f"{schedule_text}\n\n"
            f"⏰ *Operating Hours:* 13:00 \\- 17:00 \\(1:00 PM \\- 5:00 PM\\)\n\n"
            f"Please *type your desired Start Time* \\(e\\.g\\., `13:00`, `13:30`, `14:15`, or `1:30`\\):\n"
            f"_\(Or click Back below to pick another date\)_"
        ),
        parse_mode="MarkdownV2",
        reply_markup=keyboard,
    )
    return START_TIME


async def back_to_date(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()

    selected_room = context.user_data.get("room", "N/A")
    await query.edit_message_text(
        text=f"Selected Room: *{esc(selected_room)}*\n\nSelect a date to book for this week:",
        parse_mode="MarkdownV2",
        reply_markup=build_date_keyboard(),
    )
    return DATE


async def receive_start_time(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    text_input = update.message.text.strip()
    start_total_mins = parse_time_to_minutes(text_input)

    if start_total_mins is None:
        await update.message.reply_text(
            "❌ Invalid time format\\. Please type the start time like `13:30`, `14:00`, or `1:30`\\.",
            parse_mode="MarkdownV2",
        )
        return START_TIME

    OPEN_MIN = 13 * 60
    CLOSE_MIN = 17 * 60

    if start_total_mins < OPEN_MIN or start_total_mins >= CLOSE_MIN:
        await update.message.reply_text(
            "❌ Start time must be between *13:00 \\(1:00 PM\\)* and *17:00 \\(5:00 PM\\)*\\.",
            parse_mode="MarkdownV2",
        )
        return START_TIME

    # Check if selected start time has already passed TODAY
    booking_date_str = context.user_data["booking_date"]
    now = datetime.now(LOCAL_TZ)
    today_str = now.strftime("%Y-%m-%d")

    if booking_date_str == today_str:
        current_time_mins = now.hour * 60 + now.minute
        if start_total_mins <= current_time_mins:
            curr_hr, curr_mn = now.hour, now.minute
            await update.message.reply_text(
                f"❌ That time has already passed today \\(Current time is `{curr_hr:02d}:{curr_mn:02d}`\\)\\. Please select a future time\\.",
                parse_mode="MarkdownV2",
            )
            return START_TIME

    room = context.user_data["room"]
    booking_date = context.user_data["booking_date"]

    is_conflict = await is_slot_conflicting(
        room, booking_date, start_total_mins, 1
    )
    if is_conflict:
        await update.message.reply_text(
            "❌ That start time lands inside an existing booking\\! Please check the schedule above and try another start time\\.",
            parse_mode="MarkdownV2",
        )
        return START_TIME

    context.user_data["start_total_mins"] = start_total_mins

    selected_team = context.user_data["team"]
    remaining_mins = await asyncio.to_thread(
        sync_get_team_quota, selected_team
    )

    hr = start_total_mins // 60
    mn = start_total_mins % 60
    time_str = f"{hr:02d}:{mn:02d}"

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back to Start Time", callback_data="BACK_TO_START_TIME")]]
    )

    await update.message.reply_text(
        text=(
            f"⏱️ Start Time Selected: *{esc(time_str)}*\n"
            f"👥 Team *{esc(selected_team)}* remaining quota this week: *{remaining_mins} minutes*\n\n"
            f"Please type the duration in minutes you want to book \\(e\\.g\\., `15`, `30`, `45`, `60`\\):"
        ),
        parse_mode="MarkdownV2",
        reply_markup=keyboard,
    )
    return ENTER_MINUTES


async def receive_minutes(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    text_input = update.message.text.strip()

    try:
        minutes = int(text_input)
    except ValueError:
        cancel_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel Booking", callback_data="CANCEL")]
        ])
        await update.message.reply_text(
            "❌ Please enter a valid number for minutes \\(e\\.g\\., 30, 45, 60\\)\\.",
            parse_mode="MarkdownV2",
            reply_markup=cancel_keyboard,
        )
        return ENTER_MINUTES

    if minutes <= 0:
        cancel_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel Booking", callback_data="CANCEL")]
        ])
        await update.message.reply_text(
            "❌ Minutes must be greater than 0\\.",
            parse_mode="MarkdownV2",
            reply_markup=cancel_keyboard,
        )
        return ENTER_MINUTES

    selected_team = context.user_data["team"]
    remaining_mins = await asyncio.to_thread(
        sync_get_team_quota, selected_team
    )

    # Attach a Cancel button when team has insufficient quota or 0 time left
    if minutes > remaining_mins:
        cancel_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel Booking", callback_data="CANCEL")]
        ])
        await update.message.reply_text(
            f"❌ Team *{esc(selected_team)}* cannot book {minutes} mins\\. You only have *{remaining_mins} minutes* left this week\\.",
            parse_mode="MarkdownV2",
            reply_markup=cancel_keyboard,
        )
        return ENTER_MINUTES

    start_total_mins = context.user_data["start_total_mins"]
    end_total_mins = start_total_mins + minutes

    if end_total_mins > 17 * 60:
        max_allowed_mins = (17 * 60) - start_total_mins
        st_hr = start_total_mins // 60
        st_mn = start_total_mins % 60
        st_str = f"{st_hr:02d}:{st_mn:02d}"
        cancel_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel Booking", callback_data="CANCEL")]
        ])
        await update.message.reply_text(
            f"❌ Rooms close at 5:00 PM\\. The maximum duration you can book starting from {esc(st_str)} is *{max_allowed_mins} minutes*\\.",
            parse_mode="MarkdownV2",
            reply_markup=cancel_keyboard,
        )
        return ENTER_MINUTES

    room = context.user_data["room"]
    booking_date = context.user_data["booking_date"]

    is_conflict = await is_slot_conflicting(
        room, booking_date, start_total_mins, minutes
    )
    if is_conflict:
        await update.message.reply_text(
            "❌ *Conflict Detected\\!* Your duration overlaps with another team's booking\\. Please try a shorter duration or pick a different start time with /book\\.",
            parse_mode="MarkdownV2",
        )
        return ConversationHandler.END

    context.user_data["duration_minutes"] = minutes

    st_hr, st_mn = start_total_mins // 60, start_total_mins % 60
    end_hr, end_mn = end_total_mins // 60, end_total_mins % 60

    time_slot_str = f"{st_hr:02d}:{st_mn:02d} - {end_hr:02d}:{end_mn:02d}"
    context.user_data["time_slot_str"] = time_slot_str

    keyboard = [
        [
            InlineKeyboardButton("✅ Confirm", callback_data="CONFIRM"),
            InlineKeyboardButton("❌ Cancel", callback_data="CANCEL"),
        ]
    ]

    time_slot_esc = f"{st_hr:02d}:{st_mn:02d} \\- {end_hr:02d}:{end_mn:02d}"

    await update.message.reply_text(
        text=(
            f"📋 *Reservation Summary*\n"
            f"• *Telegram User:* {esc(context.user_data['telegram_user'])}\n"
            f"• *Team:* {esc(selected_team)}\n"
            f"• *Room:* {esc(room)}\n"
            f"• *Date:* {esc(booking_date)}\n"
            f"• *Time:* `{time_slot_esc}` \\({minutes} mins\\)\n\n"
            f"Confirm booking?"
        ),
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CONFIRM


def sync_write_turso_booking(
    room, booking_date, start_minutes, duration_minutes, user_id, tg_user, team
):
    conn = get_turso_conn()
    conn.execute(
        "INSERT INTO bookings (room, booking_date, start_minutes, duration_minutes, user_id, user_handle, team) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            room,
            booking_date,
            start_minutes,
            duration_minutes,
            user_id,
            tg_user,
            team,
        ),
    )
    conn.commit()


async def send_group_notification(context, target_group_id, message):
    """Safely send notification to the group without unhandled task exceptions."""
    try:
        await context.bot.send_message(
            chat_id=target_group_id,
            message_thread_id=BOOKING_TOPIC_ID if BOOKING_TOPIC_ID else None,
            text=message,
            parse_mode="MarkdownV2",
        )
    except Exception as e:
        print(f"❌ Failed to send group alert: {e}")


async def confirm_booking(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "CANCEL":
        await query.edit_message_text("Booking cancelled.")
        return ConversationHandler.END

    tg_user = context.user_data.get("telegram_user", "Unknown User")
    team = context.user_data.get("team", "N/A")
    room = context.user_data.get("room")
    booking_date = context.user_data.get("booking_date")
    start_total_mins = context.user_data.get("start_total_mins")
    duration_minutes = context.user_data.get("duration_minutes")
    time_slot_str = context.user_data.get("time_slot_str")
    user_id = update.effective_user.id

    if not all([room, booking_date, start_total_mins, duration_minutes]):
        await query.edit_message_text(
            "⚠️ Missing booking details. Please start over with /book."
        )
        return ConversationHandler.END

    try:
        await asyncio.to_thread(
            sync_write_turso_booking,
            room,
            booking_date,
            start_total_mins,
            duration_minutes,
            user_id,
            tg_user,
            team,
        )
        await asyncio.to_thread(
            sync_update_team_quota, team, duration_minutes
        )
    except Exception as e:
        print(f"Turso write/quota error: {e}")
        await query.edit_message_text(
            "⚠️ Database error occurred. Please try again."
        )
        return ConversationHandler.END

    asyncio.create_task(
        asyncio.to_thread(
            sync_save_booking_to_sheet,
            user_id,
            tg_user,
            team,
            room,
            time_slot_str,
            booking_date,
        )
    )

    target_group_id = (
        context.user_data.get("origin_group_id") or DEFAULT_GROUP_ID
    )
    if target_group_id:
        booking_alert_message = (
            f"📢 *New Room Reservation*\n\n"
            f"👥 *Team:* {esc(team)} \\({esc(tg_user)}\\)\n"
            f"🚪 *Room:* {esc(room)}\n"
            f"📅 *Date:* {esc(booking_date)}\n"
            f"⏰ *Time:* `{esc(time_slot_str)}` \\({duration_minutes} mins\\)"
        )
        asyncio.create_task(
            send_group_notification(
                context, target_group_id, booking_alert_message
            )
        )

    await query.edit_message_text(
        f"✅ Reservation confirmed for *{room}* on *{booking_date}* (`{time_slot_str}`)! {duration_minutes} mins deducted from your weekly quota.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Booking cancelled.")
    else:
        await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END

async def restart_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Resets all stored state and restarts the booking conversation."""
    user = update.effective_user
    context.user_data.clear()

    context.user_data["telegram_user"] = (
        f"@{user.username}" if user.username else user.first_name
    )

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
        r"🔄 *Booking Process Restarted\!*" + "\n\nPlease select your *Team*:",
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return TEAM


async def error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Logs unexpected errors."""
    print(f"Exception while handling an update: {context.error}")


async def run_cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    await asyncio.to_thread(cleanup_expired_bookings)

async def post_init(application: Application):
    from telegram import BotCommand
    commands = [
        BotCommand("book", "Book a room slot"),
        BotCommand("restart", "Restart the current booking process"),
        BotCommand("cancel", "Cancel current operation"),
    ]
    await application.bot.set_my_commands(commands)

# --- MAIN APPLICATION ENTRYPOINT ---
def main():
    sync_init_turso_db()

    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)

    app = (
        Application.builder()
        .token(TOKEN)
        .request(request)
        .post_init(post_init)
        .build()
    )

    app.add_error_handler(error_handler)

    if app.job_queue:
        app.job_queue.run_repeating(run_cleanup_job, interval=600, first=10)

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("book", start),
            CommandHandler("start", start),
            CommandHandler("restart", restart_command),
        ],
        states={
            TEAM: [CallbackQueryHandler(team_choice, pattern="^TEAM_")],
            ROOM: [
                CallbackQueryHandler(back_to_team, pattern="^BACK_TO_TEAM$"),
                CallbackQueryHandler(cancel, pattern="^CANCEL$"),
                CallbackQueryHandler(room_choice),
            ],
            DATE: [
                CallbackQueryHandler(back_to_room, pattern="^BACK_TO_ROOM$"),
                CallbackQueryHandler(cancel, pattern="^CANCEL$"),
                CallbackQueryHandler(date_choice, pattern="^(DATE_|PAGE_|IGNORE)"),
            ],
            START_TIME: [
                CallbackQueryHandler(back_to_date, pattern="^BACK_TO_DATE$"),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, receive_start_time
                ),
            ],
            ENTER_MINUTES: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, receive_minutes
                )
            ],
            CONFIRM: [
                CallbackQueryHandler(
                    confirm_booking, pattern="^(CONFIRM|CANCEL)$"
                )
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel),
            CommandHandler("restart", restart_command),
            CallbackQueryHandler(cancel, pattern="^CANCEL$"),
        ],
        per_chat=True,
        per_user=True,
        per_message=False,
    )

    app.add_handler(conv_handler)
    print("⚡ Room booking bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()