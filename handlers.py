import os
import re
import asyncio
from datetime import datetime, timedelta
import pytz

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

import database as db

# Constants & Configurations
TEAMS = [f"Team {i}" for i in range(1, 17)]
ROOMS = ["A203", "A205"]
ALLOWED_DAYS = [0, 1, 2, 3, 4]
DEFAULT_GROUP_ID = os.environ.get("DEFAULT_GROUP_ID")
BOOKING_TOPIC_ID = os.environ.get("BOOKING_TOPIC_ID")
LOCAL_TZ = pytz.timezone("Asia/Phnom_Penh")

# Conversation States
TEAM, ROOM, DATE, START_TIME, ENTER_MINUTES, CONFIRM = range(6)


# --- Helpers ---
def esc(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    return re.sub(r"([_\[\]\(\)~`>#+\-=|{}.!])", r"\\\1", text)


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


async def is_slot_conflicting(
    room: str, date_str: str, new_start_min: int, new_duration: int
) -> bool:
    return await asyncio.to_thread(
        db.sync_is_slot_conflicting, room, date_str, new_start_min, new_duration
    )


# --- Keyboards ---
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
    CLOSE_MIN = 17 * 60

    base_date = today_date + timedelta(days=offset_days)
    monday_of_week = base_date - timedelta(days=base_date.weekday())

    keyboard = []

    for i in range(7):
        day_date = monday_of_week + timedelta(days=i)
        if day_date.weekday() in ALLOWED_DAYS:
            label = day_date.strftime("%a, %b %d")
            val = day_date.strftime("%Y-%m-%d")

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


# --- Handlers ---
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

    if not db.is_date_in_current_week(selected_date):
        await query.answer(
            "❌ You can only book dates for the current week!",
            show_alert=True,
        )
        return DATE

    context.user_data["booking_date"] = selected_date
    room = context.user_data["room"]

    booked_slots = await asyncio.to_thread(
        db.sync_get_booked_slots_summary, room, selected_date
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
            f"_\\(Or click Back below to pick another date\\)_"
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
            r"❌ Invalid time format\. Please type the start time like `13:30`, `14:00`, or `1:30`\.",
            parse_mode="MarkdownV2",
        )
        return START_TIME

    if start_total_mins % 15 != 0:
        await update.message.reply_text(
            r"❌ Start time must be in 15\-minute intervals \(e\.g\., `13:00`, `13:15`, `13:30`\)\.",
            parse_mode="MarkdownV2",
        )
        return START_TIME

    OPEN_MIN = 13 * 60
    CLOSE_MIN = 17 * 60

    if start_total_mins < OPEN_MIN or start_total_mins >= CLOSE_MIN:
        await update.message.reply_text(
            r"❌ Start time must be between *13:00 \(1:00 PM\)* and *17:00 \(5:00 PM\)*\.",
            parse_mode="MarkdownV2",
        )
        return START_TIME

    booking_date_str = context.user_data["booking_date"]
    now = datetime.now(LOCAL_TZ)
    today_str = now.strftime("%Y-%m-%d")

    if booking_date_str == today_str:
        current_time_mins = now.hour * 60 + now.minute
        if start_total_mins <= current_time_mins:
            curr_hr, curr_mn = now.hour, now.minute
            await update.message.reply_text(
                rf"❌ That time has already passed today \(Current time is `{curr_hr:02d}:{curr_mn:02d}`\)\. Please select a future time\.",
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
            r"❌ That start time lands inside an existing booking\! Please check the schedule above and try another start time\.",
            parse_mode="MarkdownV2",
        )
        return START_TIME

    context.user_data["start_total_mins"] = start_total_mins

    selected_team = context.user_data["team"]
    remaining_mins = await asyncio.to_thread(
        db.sync_get_team_quota, selected_team
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
    cancel_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel Booking", callback_data="CANCEL")]
    ])

    try:
        minutes = int(text_input)
    except ValueError:
        await update.message.reply_text(
            r"❌ Please enter a valid number for minutes \(e\.g\., 15, 30, 45, 60\)\.",
            parse_mode="MarkdownV2",
            reply_markup=cancel_keyboard,
        )
        return ENTER_MINUTES

    MIN_DURATION = 15
    if minutes < MIN_DURATION:
        await update.message.reply_text(
            rf"❌ Booking duration must be at least *{MIN_DURATION} minutes*\.",
            parse_mode="MarkdownV2",
            reply_markup=cancel_keyboard,
        )
        return ENTER_MINUTES

    if minutes % 15 != 0:
        await update.message.reply_text(
            r"❌ Booking duration must be in 15\-minute intervals \(e\.g\., 15, 30, 45, 60\)\.",
            parse_mode="MarkdownV2",
            reply_markup=cancel_keyboard,
        )
        return ENTER_MINUTES

    selected_team = context.user_data["team"]
    remaining_mins = await asyncio.to_thread(
        db.sync_get_team_quota, selected_team
    )

    if minutes > remaining_mins:
        await update.message.reply_text(
            rf"❌ Team *{esc(selected_team)}* cannot book {minutes} mins\. You only have *{remaining_mins} minutes* left this week\.",
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
        await update.message.reply_text(
            rf"❌ Rooms close at 5:00 PM\. The maximum duration you can book starting from {esc(st_str)} is *{max_allowed_mins} minutes*\.",
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
        booked_slots = await asyncio.to_thread(
            db.sync_get_booked_slots_summary, room, booking_date
        )

        if booked_slots:
            schedule_text = "📅 *Existing Reservations \\(Start \\- End\\):*\n" + "\n".join(
                [esc(slot) for slot in booked_slots]
            )
        else:
            schedule_text = "🟢 *No bookings yet for this date\\! Entire schedule is open\\.*"

        retry_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Start Time", callback_data="BACK_TO_START_TIME")],
            [InlineKeyboardButton("❌ Cancel Booking", callback_data="CANCEL")]
        ])

        st_hr, st_mn = start_total_mins // 60, start_total_mins % 60
        st_str = f"{st_hr:02d}:{st_mn:02d}"

        await update.message.reply_text(
            text=(
                f"❌ *Conflict Detected\\!*\n"
                f"A duration of *{minutes} minutes* starting at `{esc(st_str)}` overlaps with another booking\\.\n\n"
                f"{schedule_text}\n\n"
                f"Please type a *shorter duration in minutes* \\(e\\.g\\., `15`, `30`\\) or choose an option below:"
            ),
            parse_mode="MarkdownV2",
            reply_markup=retry_keyboard,
        )
        return ENTER_MINUTES

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


async def back_to_start_time(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()

    room = context.user_data.get("room", "N/A")
    selected_date = context.user_data.get("booking_date", "N/A")

    booked_slots = await asyncio.to_thread(
        db.sync_get_booked_slots_summary, room, selected_date
    )

    if booked_slots:
        schedule_text = "📅 *Existing Reservations \\(Start \\- End\\):*\n" + "\n".join(
            [esc(slot) for slot in booked_slots]
        )
    else:
        schedule_text = "🟢 *No bookings yet for this date\\! Entire schedule is open\\.*"

    cancel_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel Booking", callback_data="CANCEL")]
    ])

    await query.edit_message_text(
        text=(
            f"🚪 Room: *{esc(room)}*\n"
            f"📅 Date: *{esc(selected_date)}*\n\n"
            f"{schedule_text}\n\n"
            f"Please *type your desired Start Time* \\(e\\.g\\., `13:00`, `13:30`\\):"
        ),
        parse_mode="MarkdownV2",
        reply_markup=cancel_keyboard,
    )

    return START_TIME


async def send_group_notification(context, target_group_id, message):
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
            db.sync_write_sqlite_booking,
            room,
            booking_date,
            start_total_mins,
            duration_minutes,
            user_id,
            tg_user,
            team,
        )
        await asyncio.to_thread(
            db.sync_update_team_quota, team, duration_minutes
        )
    except Exception as e:
        print(f"SQLite write/quota error: {e}")
        await query.edit_message_text(
            "⚠️ Database error occurred. Please try again."
        )
        return ConversationHandler.END

    target_group_id = (
        context.user_data.get("origin_group_id") or DEFAULT_GROUP_ID
    )
    if target_group_id:
        booking_alert_message = (
            f"📢 *New Room Reservation* \n\n"
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
    user = update.effective_user
    context.user_data.clear()

    context.user_data["telegram_user"] = (
        f"@{user.username}" if user.username else user.first_name
    )

    await update.message.reply_text(
        r"🔄 *Booking Process Restarted\!*" + "\n\nPlease select your *Team*:",
        parse_mode="MarkdownV2",
        reply_markup=build_team_keyboard(),
    )
    return TEAM


# --- CANCEL EXISTING BOOKINGS (DATABASE) ---
async def list_bookings_to_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_bookings = await asyncio.to_thread(db.sync_get_user_bookings, user_id)

    if not user_bookings:
        await update.message.reply_text("You have no active bookings to cancel.")
        return

    keyboard = []
    for b in user_bookings:
        st_min = b["start_minutes"]
        st_hr, st_m = st_min // 60, st_min % 60
        btn_text = f"❌ Cancel: {b['room']} on {b['booking_date']} @ {st_hr:02d}:{st_m:02d}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"cancel_db_{b['id']}")])

    await update.message.reply_text(
        "Select an active reservation to cancel:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_cancel_booking_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    booking_id = int(query.data.replace("cancel_db_", ""))
    user_id = update.effective_user.id

    result = await asyncio.to_thread(db.sync_cancel_user_booking, booking_id, user_id)

    if result:
        st_min = result["start_minutes"]
        st_hr, st_m = st_min // 60, st_min % 60
        await query.edit_message_text(
            f"✅ Cancelled reservation for *{result['room']}* on *{result['date']}* ({st_hr:02d}:{st_m:02d}).\n"
            f"🔄 *{result['duration']} minutes* refunded to {result['team']}'s weekly quota.",
            parse_mode="Markdown",
        )
    else:
        await query.edit_message_text("⚠️ Could not cancel reservation. It may have already been removed.")


async def error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    print(f"Exception while handling an update: {context.error}")


# --- Job Callbacks ---
async def run_cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    await asyncio.to_thread(db.sync_cleanup_old_bookings)


async def weekly_quota_reset_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        await asyncio.to_thread(db.sync_reset_all_team_quotas)
        print("🔄 Weekly quota successfully reset for all teams.")
    except Exception as e:
        print(f"❌ Failed to reset weekly quota: {e}")