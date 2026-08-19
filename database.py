import os
import sqlite3
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
import pytz

LOCAL_TZ = pytz.timezone("Asia/Phnom_Penh")
DB_FILE = os.environ.get("SQLITE_DB_PATH", "bookings.db")


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


def start_dummy_server():
    threading.Thread(target=run_dummy_server, daemon=True).start()


# --- Database Connection & Logic ---
def get_db_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def is_date_in_current_week(date_str: str) -> bool:
    try:
        booking_dt = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return False

    today = datetime.now(LOCAL_TZ).date()
    start_of_week = today - timedelta(days=today.weekday())
    end_of_week = start_of_week + timedelta(days=6)

    return start_of_week <= booking_dt <= end_of_week


def sync_init_db():
    conn = get_db_conn()
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
    conn.close()


def sync_get_team_quota(team_name: str) -> int:
    conn = get_db_conn()
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
        conn.close()
        return 120
    quota = row["remaining_minutes"]
    conn.close()
    return quota


def sync_update_team_quota(team_name: str, minutes_to_subtract: int):
    conn = get_db_conn()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE team_quota SET remaining_minutes = remaining_minutes - ? WHERE team = ?",
        (minutes_to_subtract, team_name),
    )
    conn.commit()
    conn.close()


def sync_reset_all_team_quotas():
    conn = get_db_conn()
    cursor = conn.cursor()
    cursor.execute("UPDATE team_quota SET remaining_minutes = 120")
    conn.commit()
    conn.close()


def sync_get_booked_slots_summary(room: str, date_str: str) -> list[str]:
    conn = get_db_conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT start_minutes, duration_minutes, team, user_handle FROM bookings WHERE room = ? AND booking_date = ? ORDER BY start_minutes ASC",
        (room, date_str),
    )
    rows = cursor.fetchall()

    formatted_slots = []
    for row in rows:
        start_val = row["start_minutes"]
        duration = row["duration_minutes"]
        team = row["team"]
        user_handle = row["user_handle"]

        st_min = start_val if start_val > 24 else start_val * 60
        end_min = st_min + duration

        st_hr, st_m = st_min // 60, st_min % 60
        end_hr, end_m = end_min // 60, end_min % 60

        formatted_slots.append(
            f"• 🔴 {st_hr:02d}:{st_m:02d} - {end_hr:02d}:{end_m:02d} ({team} - {user_handle})"
        )

    conn.close()
    return formatted_slots


def sync_is_slot_conflicting(
    room: str, date_str: str, new_start_min: int, new_duration: int
) -> bool:
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT start_minutes, duration_minutes FROM bookings WHERE room = ? AND booking_date = ?",
            (room, date_str),
        )
        rows = cursor.fetchall()

        new_end_min = new_start_min + new_duration
        for row in rows:
            start_val = row["start_minutes"]
            duration = row["duration_minutes"]
            existing_start_min = start_val if start_val > 24 else start_val * 60
            existing_end_min = existing_start_min + duration

            if (
                new_start_min < existing_end_min
                and new_end_min > existing_start_min
            ):
                conn.close()
                return True
        conn.close()
        return False
    except Exception as e:
        print(f"Error checking slot conflict: {e}")
        return True


def sync_cleanup_old_bookings():
    try:
        today_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM bookings WHERE booking_date < ?", (today_str,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error during SQLite database cleanup: {e}")


def sync_write_sqlite_booking(
    room, booking_date, start_minutes, duration_minutes, user_id, tg_user, team
):
    conn = get_db_conn()
    cursor = conn.cursor()
    cursor.execute(
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
    conn.close()

# --- Cancel Booking Queries ---
def sync_get_user_bookings(user_id: int):
    today_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    conn = get_db_conn()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, room, booking_date, start_minutes, duration_minutes, team FROM bookings WHERE user_id = ? AND booking_date >= ? ORDER BY booking_date ASC, start_minutes ASC",
        (user_id, today_str),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def sync_cancel_user_booking(booking_id: int, user_id: int) -> dict | None:
    conn = get_db_conn()
    cursor = conn.cursor()

    # Find the target booking first
    cursor.execute(
        "SELECT team, duration_minutes, room, booking_date, start_minutes FROM bookings WHERE id = ? AND user_id = ?",
        (booking_id, user_id),
    )
    row = cursor.fetchone()

    if not row:
        conn.close()
        return None

    team = row["team"]
    duration = row["duration_minutes"]
    room = row["room"]
    b_date = row["booking_date"]
    start_min = row["start_minutes"]

    # Delete booking record
    cursor.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))

    # Refund team quota
    cursor.execute(
        "UPDATE team_quota SET remaining_minutes = remaining_minutes + ? WHERE team = ?",
        (duration, team),
    )

    conn.commit()
    conn.close()

    return {
        "team": team,
        "duration": duration,
        "room": room,
        "date": b_date,
        "start_minutes": start_min,
    }