#!/usr/bin/env python3
# stars_bot_sync.py
# Offline-resilient variant: buyurtmalar SQLite-ga saqlanadi va adminlarga yuborishda muammo bo'lsa pending ga olinadi.
# Pending yozuvlar ishga tushganda va har 60s da qayta jo'natiladi. Adminlar /sync orqali ham majburiy yuborish mumkin.

import logging
import sqlite3
from datetime import datetime, date
from typing import Optional, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# -------------------- SOZLAMALAR --------------------
BOT_TOKEN = "8302951111:AAFoxHDH7N9-i_njkg7uScYJ-3eVqKDDTKE"   # <- shu joyga tokenni qo'ying
PRICE_PER_STAR = 210
MIN_STARS = 50
MAX_STARS = 10000

ADMINS: List[int] = [8287301829]   # to'liq huquqli adminlar
BLOCKED_ADMIN = 860825533          # bu ID ga avtomatik bildirishnoma yuborilmasin

CHANNEL_LINK = "https://t.me/arzonstarslar"
DB_FILE = "stars_bot_sync.db"

# -------------------- LOGGING --------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# -------------------- DATABASE HELPERS --------------------
_conn: Optional[sqlite3.Connection] = None


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_seen DATE,
            seen_channel INTEGER DEFAULT 0
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            buy_type TEXT,
            stars INTEGER,
            amount INTEGER,
            created_at TIMESTAMP
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER,
            tries INTEGER DEFAULT 0,
            last_try TIMESTAMP
        )
        """
    )
    conn.commit()


def ensure_user(user_id: int, username: Optional[str]):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO users (user_id, username, first_seen, seen_channel) VALUES (?, ?, ?, 0)",
        (user_id, username, date.today().isoformat()),
    )
    cur.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
    conn.commit()


def has_seen_channel(user_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT seen_channel FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    return bool(row and row["seen_channel"] == 1)


def set_seen_channel(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET seen_channel = 1 WHERE user_id = ?", (user_id,))
    conn.commit()


def add_order(user_id: int, username: str, buy_type: str, stars: int, amount: int) -> int:
    """Yangi buyurtmani orders jadvalga qo'shadi va order_id qaytaradi."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO orders (user_id, username, buy_type, stars, amount, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, username, buy_type, stars, amount, datetime.now()),
    )
    conn.commit()
    return cur.lastrowid


def mark_pending(order_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO pending_notifications (order_id, tries, last_try) VALUES (?, 0, NULL)", (order_id,)
    )
    conn.commit()


def increment_pending_try(pending_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE pending_notifications SET tries = tries + 1, last_try = ? WHERE id = ?", (datetime.now(), pending_id)
    )
    conn.commit()


def remove_pending(pending_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM pending_notifications WHERE id = ?", (pending_id,))
    conn.commit()


def get_pending(limit: int = 50):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT p.id as pending_id, p.order_id, o.user_id, o.username, o.stars, o.amount "
        "FROM pending_notifications p JOIN orders o ON p.order_id = o.id "
        "ORDER BY p.id ASC LIMIT ?",
        (limit,),
    )
    return cur.fetchall()


def get_top5_by_stars(limit: int = 5):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT username, SUM(stars) as total_stars, SUM(amount) as total_amount
        FROM orders
        GROUP BY user_id
        ORDER BY total_stars DESC
        LIMIT ?
        """,
        (limit,),
    )
    return cur.fetchall()


def get_stats_summary():
    conn = get_conn()
    cur = conn.cursor()
    today = date.today().isoformat()
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM users WHERE first_seen = ?", (today,))
    users_today = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM orders")
    total_orders = cur.fetchone()[0] or 0
    cur.execute("SELECT COALESCE(SUM(amount),0) FROM orders")
    total_amount = cur.fetchone()[0] or 0
    return {
        "total_users": total_users,
        "users_today": users_today,
        "total_orders": total_orders,
        "total_amount": total_amount,
    }


# -------------------- HANDLERS --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username_display = f"@{user.username}" if user.username else (user.first
