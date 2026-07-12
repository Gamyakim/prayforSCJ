# -*- coding: utf-8 -*-
"""
기도회 신청 텔레그램 봇
- 오후 1시~8시, 10분 단위 타임슬롯 (총 42개)
- 슬롯당 최대 10명(대표자 기준)
- 대표자 이름 / 연락처(010-XXXX-XXXX 검증) / 동반자 입력
- 관리자는 버튼으로 타임별 명단 조회
"""

import os
import re
import sqlite3
import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 환경설정
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
# 콤마로 구분된 관리자 텔레그램 숫자 ID (예: "111111111,222222222")
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x
}

DB_PATH = os.environ.get("DB_PATH", "prayer_signup.db")

MAX_PER_SLOT = 10
HOURS = list(range(13, 20))  # 13시 ~ 19시 (각 시간당 6개 슬롯, 마지막 슬롯 19:50)
MINUTES = [0, 10, 20, 30, 40, 50]

PHONE_PATTERN = re.compile(r"^01[016789]-\d{3,4}-\d{4}$")

# 대화 상태
SELECT_SLOT, ENTER_NAME, ENTER_PHONE, ENTER_COMPANIONS, CONFIRM = range(5)


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS signups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_time TEXT NOT NULL,
            rep_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            companions TEXT,
            telegram_user_id INTEGER,
            telegram_username TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def count_slot(slot_time: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "SELECT COUNT(*) as cnt FROM signups WHERE slot_time = ?", (slot_time,)
    )
    cnt = cur.fetchone()["cnt"]
    conn.close()
    return cnt


def insert_signup(slot_time, rep_name, phone, companions, user_id, username) -> bool:
    """정원 체크 후 삽입. 성공하면 True, 정원 초과면 False."""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "SELECT COUNT(*) as cnt FROM signups WHERE slot_time = ?", (slot_time,)
        )
        cnt = cur.fetchone()["cnt"]
        if cnt >= MAX_PER_SLOT:
            conn.rollback()
            return False
        conn.execute(
            """
            INSERT INTO signups
                (slot_time, rep_name, phone, companions, telegram_user_id, telegram_username, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                slot_time,
                rep_name,
                phone,
                companions,
                user_id,
                username,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def get_signups_for_hour(hour: int):
    conn = get_conn()
    prefix = f"{hour:02d}:"
    cur = conn.execute(
        "SELECT * FROM signups WHERE slot_time LIKE ? ORDER BY slot_time, id",
        (f"{prefix}%",),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------
def all_slots():
    slots = []
    for h in HOURS:
        for m in MINUTES:
            slots.append(f"{h:02d}:{m:02d}")
    return slots


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def hour_keyboard(prefix: str):
    """1시~7시(19시) 선택 키보드. prefix로 신청용/관리자용 콜백 구분."""
    buttons = []
    row = []
    for h in HOURS:
        row.append(InlineKeyboardButton(f"{h}시", callback_data=f"{prefix}hour_{h}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


def slot_keyboard_for_hour(hour: int):
    buttons = []
    row = []
    for m in MINUTES:
        slot = f"{hour:02d}:{m:02d}"
        cnt = count_slot(slot)
        label = f"{slot} ({cnt}/{MAX_PER_SLOT})" + (" 마감" if cnt >= MAX_PER_SLOT else "")
        cb = "full" if cnt >= MAX_PER_SLOT else f"slot_{slot}"
        row.append(InlineKeyboardButton(label, callback_data=cb))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("◀ 시간 다시 선택", callback_data="back_hours")])
    return InlineKeyboardMarkup(buttons)


# ---------------------------------------------------------------------------
# 신청 흐름 핸들러
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🙏 기도회 신청 봇입니다.\n\n"
        "오후 1시 ~ 8시, 10분 단위로 신청하실 수 있어요.\n"
        "타임당 최대 10명(대표자 기준)까지 신청 가능합니다.\n\n"
        "아래에서 원하시는 시간대를 선택해주세요.",
        reply_markup=hour_keyboard(prefix="req_"),
    )
    return SELECT_SLOT


async def show_hours_again(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "시간대를 선택해주세요.", reply_markup=hour_keyboard(prefix="req_")
    )
    return SELECT_SLOT


async def hour_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    hour = int(query.data.split("_")[-1])
    await query.edit_message_text(
        f"{hour}시 타임을 선택해주세요.", reply_markup=slot_keyboard_for_hour(hour)
    )
    return SELECT_SLOT


async def full_slot_clicked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("이 타임은 정원이 마감되었어요 🙏", show_alert=True)
    return SELECT_SLOT


async def slot_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    slot = query.data.split("_", 1)[1]
    context.user_data["slot_time"] = slot
    await query.edit_message_text(
        f"선택하신 타임: {slot}\n\n대표자 이름을 입력해주세요."
    )
    return ENTER_NAME


async def name_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("이름을 입력해주세요.")
        return ENTER_NAME
    context.user_data["rep_name"] = name
    await update.message.reply_text(
        "연락처를 입력해주세요.\n형식: 010-1234-5678"
    )
    return ENTER_PHONE


async def phone_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    if not PHONE_PATTERN.match(phone):
        await update.message.reply_text(
            "연락처 형식이 올바르지 않아요. 다시 입력해주세요.\n예: 010-1234-5678"
        )
        return ENTER_PHONE
    context.user_data["phone"] = phone
    await update.message.reply_text(
        "같이 갈 사람이 있다면 이름을 입력해주세요.\n없으면 '없음'이라고 입력해주세요."
    )
    return ENTER_COMPANIONS


async def companions_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    companions = update.message.text.strip()
    context.user_data["companions"] = companions

    slot = context.user_data["slot_time"]
    name = context.user_data["rep_name"]
    phone = context.user_data["phone"]

    summary = (
        "📋 신청 내용을 확인해주세요.\n\n"
        f"⏰ 시간: {slot}\n"
        f"👤 대표자: {name}\n"
        f"📞 연락처: {phone}\n"
        f"👥 같이 가는 사람: {companions}\n\n"
        "제출하시겠어요?"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ 제출", callback_data="submit"),
                InlineKeyboardButton("❌ 취소", callback_data="cancel"),
            ]
        ]
    )
    await update.message.reply_text(summary, reply_markup=keyboard)
    return CONFIRM


async def submit_signup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    slot = context.user_data.get("slot_time")
    name = context.user_data.get("rep_name")
    phone = context.user_data.get("phone")
    companions = context.user_data.get("companions")
    user = query.from_user

    ok = insert_signup(slot, name, phone, companions, user.id, user.username or "")

    if not ok:
        await query.edit_message_text(
            "😥 죄송해요, 방금 정원이 다 찼어요.\n/start 로 다른 타임을 선택해주세요."
        )
    else:
        await query.edit_message_text(
            f"✅ 신청 완료되었습니다!\n\n"
            f"⏰ {slot} / 👤 {name} / 👥 {companions}\n\n"
            "신청해주셔서 감사합니다 🙏"
        )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_signup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("신청이 취소되었습니다. 다시 하시려면 /start 를 입력해주세요.")
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("신청이 취소되었습니다. 다시 하시려면 /start 를 입력해주세요.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# 관리자 핸들러
# ---------------------------------------------------------------------------
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("관리자만 사용할 수 있어요.")
        return
    await update.message.reply_text(
        "확인하실 시간대를 선택해주세요.", reply_markup=hour_keyboard(prefix="admin_")
    )


async def admin_hour_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("관리자만 사용할 수 있어요.", show_alert=True)
        return
    await query.answer()
    hour = int(query.data.split("_")[-1])
    rows = get_signups_for_hour(hour)

    if not rows:
        text = f"🕐 {hour}시대 신청 내역이 없습니다."
    else:
        by_slot = {}
        for r in rows:
            by_slot.setdefault(r["slot_time"], []).append(r)

        lines = [f"🕐 {hour}시대 신청 현황\n"]
        for m in MINUTES:
            slot = f"{hour:02d}:{m:02d}"
            entries = by_slot.get(slot, [])
            lines.append(f"\n▶ {slot} — {len(entries)}/{MAX_PER_SLOT}명")
            for e in entries:
                lines.append(
                    f"  · {e['rep_name']} / {e['phone']} / 동반자: {e['companions']}"
                )
        text = "\n".join(lines)

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("◀ 시간 다시 선택", callback_data="admin_back")]]
    )
    await query.edit_message_text(text, reply_markup=keyboard)


async def admin_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("관리자만 사용할 수 있어요.", show_alert=True)
        return
    await query.answer()
    await query.edit_message_text(
        "확인하실 시간대를 선택해주세요.", reply_markup=hour_keyboard(prefix="admin_")
    )


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN 환경변수가 설정되지 않았습니다.")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SELECT_SLOT: [
                CallbackQueryHandler(hour_selected, pattern=r"^req_hour_\d+$"),
                CallbackQueryHandler(show_hours_again, pattern=r"^back_hours$"),
                CallbackQueryHandler(full_slot_clicked, pattern=r"^full$"),
                CallbackQueryHandler(slot_selected, pattern=r"^slot_\d{2}:\d{2}$"),
            ],
            ENTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, name_entered)],
            ENTER_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, phone_entered)],
            ENTER_COMPANIONS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, companions_entered)
            ],
            CONFIRM: [
                CallbackQueryHandler(submit_signup, pattern=r"^submit$"),
                CallbackQueryHandler(cancel_signup, pattern=r"^cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("명단", admin_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(
        CallbackQueryHandler(admin_hour_selected, pattern=r"^admin_hour_\d+$")
    )
    app.add_handler(CallbackQueryHandler(admin_back, pattern=r"^admin_back$"))

    logger.info("봇 시작...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
