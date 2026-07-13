# -*- coding: utf-8 -*-
"""
기도회 신청 텔레그램 봇
- 오후 1시~8시, 10분 단위 타임슬롯 (총 42개)
- 슬롯당 최대 10명 (구역장 1명 + 동반자 인원 합산 기준)
- 회/구역명 / 구역장 이름 / 연락처(010-XXXX-XXXX 검증) / 동반자(콤마 구분) 입력
- 참여완료 체크 (본인만)
- 관리자는 버튼/텍스트로 타임별 명단 조회, 삭제, 제목 설정, 관리자 추가/삭제
- 데이터 저장: PostgreSQL (Render 재배포에도 데이터 유지)
"""

import os
import re
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg2
import psycopg2.extras

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
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


def _parse_admin_ids(raw: str):
    ids = set()
    for x in raw.replace(" ", "").split(","):
        if not x:
            continue
        if x.isdigit() or (x.startswith("-") and x[1:].isdigit()):
            ids.add(int(x))
        else:
            logger.warning(
                "ADMIN_IDS 값 중 '%s'는 숫자 ID가 아니라서 무시했어요. "
                "@userinfobot 에게 물어보면 나오는 숫자 ID를 넣어주세요.",
                x,
            )
    return ids


ADMIN_IDS = _parse_admin_ids(os.environ.get("ADMIN_IDS", ""))

DATABASE_URL = os.environ.get("DATABASE_URL", "")

MAX_PER_SLOT = 10  # 10분 슬롯당 최대 인원 (구역장 + 동반자 합산)
HOURS = list(range(13, 20))  # 13시 ~ 19시 (각 시간당 6개 슬롯, 마지막 슬롯 19:50)
MINUTES = [0, 10, 20, 30, 40, 50]
HOUR_CAPACITY = MAX_PER_SLOT * len(MINUTES)

PHONE_PATTERN = re.compile(r"^01[016789]-\d{3,4}-\d{4}$")

# 대화 상태
SELECT_SLOT, ENTER_GROUP, ENTER_LEADER, ENTER_PHONE, ENTER_COMPANIONS, CONFIRM = range(6)

DEFAULT_EVENT_TITLE = "릴레이 기도회"


# ---------------------------------------------------------------------------
# DB (PostgreSQL)
# ---------------------------------------------------------------------------
def get_db_admin_ids():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM admins")
        return {row["user_id"] for row in cur.fetchall()}
    finally:
        conn.close()


def add_db_admin(user_id: int, added_by: int) -> bool:
    """추가 성공(신규)이면 True, 이미 있었으면 False."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM admins WHERE user_id = %s", (user_id,))
        if cur.fetchone():
            return False
        cur.execute(
            "INSERT INTO admins (user_id, added_by, added_at) VALUES (%s, %s, %s)",
            (user_id, added_by, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def remove_db_admin(user_id: int) -> bool:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM admins WHERE user_id = %s", (user_id,))
        deleted = cur.rowcount > 0
        conn.commit()
        return deleted
    finally:
        conn.close()


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL 환경변수가 설정되지 않았습니다.")
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS signups (
                id SERIAL PRIMARY KEY,
                slot_time TEXT NOT NULL,
                group_name TEXT NOT NULL DEFAULT '',
                rep_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                companions TEXT,
                telegram_user_id BIGINT,
                telegram_username TEXT,
                created_at TEXT NOT NULL,
                checked_in INTEGER NOT NULL DEFAULT 0,
                checked_in_at TEXT
            )
            """
        )
        # 기존 배포 DB에 group_name 컬럼이 없을 수 있어 안전하게 추가
        cur.execute("ALTER TABLE signups ADD COLUMN IF NOT EXISTS group_name TEXT NOT NULL DEFAULT ''")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                user_id BIGINT PRIMARY KEY,
                added_by BIGINT,
                added_at TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def get_setting(key: str, default: str) -> str:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
        row = cur.fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_setting(key: str, value: str):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO settings (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 인원수 계산 유틸 (구역장 1명 + 동반자 콤마 구분 인원)
# ---------------------------------------------------------------------------
def companion_count(companions: str) -> int:
    s = (companions or "").strip()
    if not s or s == "없음":
        return 0
    return len([c for c in s.split(",") if c.strip()])


def signup_headcount(companions: str) -> int:
    return 1 + companion_count(companions)


def get_slot_headcount(slot_time: str) -> int:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT companions FROM signups WHERE slot_time = %s", (slot_time,)
        )
        rows = cur.fetchall()
        return sum(signup_headcount(r["companions"]) for r in rows)
    finally:
        conn.close()


def get_hour_headcount(hour: int) -> int:
    conn = get_conn()
    try:
        cur = conn.cursor()
        prefix = f"{hour:02d}:"
        cur.execute(
            "SELECT companions FROM signups WHERE slot_time LIKE %s", (f"{prefix}%",)
        )
        rows = cur.fetchall()
        return sum(signup_headcount(r["companions"]) for r in rows)
    finally:
        conn.close()


def insert_signup(slot_time, group_name, rep_name, phone, companions, user_id, username):
    """정원(인원수 기준) 체크 후 삽입. 성공하면 새 신청의 id, 정원 초과면 None을 반환.
    테이블 락으로 동시 신청 시에도 정원이 초과되지 않도록 보장."""
    new_headcount = signup_headcount(companions)
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("LOCK TABLE signups IN SHARE ROW EXCLUSIVE MODE")
        cur.execute(
            "SELECT companions FROM signups WHERE slot_time = %s", (slot_time,)
        )
        rows = cur.fetchall()
        current_headcount = sum(signup_headcount(r["companions"]) for r in rows)
        if current_headcount + new_headcount > MAX_PER_SLOT:
            conn.rollback()
            return None
        cur.execute(
            """
            INSERT INTO signups
                (slot_time, group_name, rep_name, phone, companions, telegram_user_id, telegram_username, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                slot_time,
                group_name,
                rep_name,
                phone,
                companions,
                user_id,
                username,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        return new_id
    finally:
        conn.close()


def mark_checked_in(signup_id: int, requester_user_id: int):
    """참여완료 체크. 결과를 ('ok'|'already'|'not_owner'|'not_found') 형태로 반환."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM signups WHERE id = %s", (signup_id,))
        row = cur.fetchone()
        if row is None:
            return "not_found"
        if row["telegram_user_id"] != requester_user_id:
            return "not_owner"
        if row["checked_in"]:
            return "already"
        cur.execute(
            "UPDATE signups SET checked_in = 1, checked_in_at = %s WHERE id = %s",
            (datetime.now().isoformat(timespec="seconds"), signup_id),
        )
        conn.commit()
        return "ok"
    finally:
        conn.close()


def get_signups_for_hour(hour: int):
    conn = get_conn()
    try:
        cur = conn.cursor()
        prefix = f"{hour:02d}:"
        cur.execute(
            "SELECT * FROM signups WHERE slot_time LIKE %s ORDER BY slot_time, id",
            (f"{prefix}%",),
        )
        return cur.fetchall()
    finally:
        conn.close()


def get_signups_for_user(user_id: int):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM signups WHERE telegram_user_id = %s ORDER BY slot_time",
            (user_id,),
        )
        return cur.fetchall()
    finally:
        conn.close()


def delete_signup(signup_id: int) -> bool:
    """신청 건 삭제. 삭제됐으면 True, 존재하지 않으면 False."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM signups WHERE id = %s", (signup_id,))
        deleted = cur.rowcount > 0
        conn.commit()
        return deleted
    finally:
        conn.close()


def delete_signup_by_owner(signup_id: int, requester_user_id: int) -> str:
    """본인 확인 후 삭제. 결과를 ('ok'|'not_owner'|'not_found') 형태로 반환."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM signups WHERE id = %s", (signup_id,))
        row = cur.fetchone()
        if row is None:
            return "not_found"
        if row["telegram_user_id"] != requester_user_id:
            return "not_owner"
        cur.execute("DELETE FROM signups WHERE id = %s", (signup_id,))
        conn.commit()
        return "ok"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------
def is_admin(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    return user_id in get_db_admin_ids()


MAIN_MENU_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("신청시작"), KeyboardButton("내 신청 확인")]],
    resize_keyboard=True,
)


def hour_keyboard(prefix: str):
    """1시~7시(19시) 선택 키보드. prefix로 신청용/관리자용 콜백 구분.
    신청용(prefix='req_')일 때는 시간대별 잔여 인원을 같이 보여준다."""
    show_capacity = prefix == "req_"
    buttons = []
    row = []
    for h in HOURS:
        if show_capacity:
            filled = get_hour_headcount(h)
            label = f"{h}시({filled}/{HOUR_CAPACITY}명)"
            if filled >= HOUR_CAPACITY:
                label += " 마감"
        else:
            label = f"{h}시"
        row.append(InlineKeyboardButton(label, callback_data=f"{prefix}hour_{h}"))
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
        filled = get_slot_headcount(slot)
        label = f"{slot} ({filled}/{MAX_PER_SLOT})" + (" 마감" if filled >= MAX_PER_SLOT else "")
        cb = "full" if filled >= MAX_PER_SLOT else f"slot_{slot}"
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
    title = get_setting("event_title", DEFAULT_EVENT_TITLE)
    await update.message.reply_text(
        f"🙏{title} 신청 봇입니다.\n\n아래 메뉴 버튼을 이용해주세요.",
        reply_markup=MAIN_MENU_KEYBOARD,
    )
    await update.message.reply_text(
        "오후 1시 ~ 8시, 10분 단위로 신청하실 수 있어요.\n"
        f"타임당 최대 {MAX_PER_SLOT}명(구역장+동반자 합산)까지 신청 가능합니다.\n\n"
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
        f"선택하신 타임: {slot}\n\n회/구역명을 입력해주세요."
    )
    return ENTER_GROUP


async def group_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group_name = update.message.text.strip()
    if not group_name:
        await update.message.reply_text("회/구역명을 입력해주세요.")
        return ENTER_GROUP
    context.user_data["group_name"] = group_name
    await update.message.reply_text("구역장 이름을 입력해주세요.")
    return ENTER_LEADER


async def leader_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("구역장 이름을 입력해주세요.")
        return ENTER_LEADER
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
        "같이 갈 구역원이 있다면 이름을 콤마(,)로 구분해서 입력해주세요.\n"
        "예: 김혜림,장희연,강유전\n"
        "없으면 '없음'이라고 입력해주세요."
    )
    return ENTER_COMPANIONS


async def companions_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    companions = update.message.text.strip()
    headcount = signup_headcount(companions)

    if headcount > MAX_PER_SLOT:
        await update.message.reply_text(
            f"한 타임 최대 인원은 {MAX_PER_SLOT}명이에요 (구역장 포함). "
            f"입력하신 인원은 총 {headcount}명이라 넘어가요.\n"
            "동반자 수를 줄여서 다시 입력해주세요."
        )
        return ENTER_COMPANIONS

    slot = context.user_data["slot_time"]
    current = get_slot_headcount(slot)
    if current + headcount > MAX_PER_SLOT:
        remaining = max(MAX_PER_SLOT - current, 0)
        await update.message.reply_text(
            f"'{slot}' 타임에 남은 자리가 {remaining}명뿐이에요 (입력하신 인원 총 {headcount}명).\n"
            "동반자 수를 줄이거나, 다른 타임을 선택해주세요.\n"
            "(동반자 수를 줄이려면 다시 입력, 다른 타임을 원하시면 '취소' 입력 후 재시작해주세요)"
        )
        return ENTER_COMPANIONS

    context.user_data["companions"] = companions

    name = context.user_data["rep_name"]
    phone = context.user_data["phone"]
    group_name = context.user_data["group_name"]

    summary = (
        "📋 신청 내용을 확인해주세요.\n\n"
        f"⏰ 시간: {slot}\n"
        f"🏠 회/구역: {group_name}\n"
        f"👤 구역장: {name}\n"
        f"📞 연락처: {phone}\n"
        f"👥 같이 가는 사람: {companions} (총 {headcount}명)\n\n"
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
    group_name = context.user_data.get("group_name")
    name = context.user_data.get("rep_name")
    phone = context.user_data.get("phone")
    companions = context.user_data.get("companions")
    user = query.from_user

    logger.info(
        "[제출 시도] user_id=%s username=%s slot=%s group=%s name=%s companions=%s",
        user.id, user.username, slot, group_name, name, companions,
    )

    signup_id = insert_signup(
        slot, group_name, name, phone, companions, user.id, user.username or ""
    )

    logger.info("[제출 결과] user_id=%s -> signup_id=%s", user.id, signup_id)

    if signup_id is None:
        await query.edit_message_text(
            "😥 죄송해요, 방금 사이에 정원이 다 찼어요.\n'신청시작' 버튼으로 다른 타임을 선택해주세요."
        )
    else:
        headcount = signup_headcount(companions)
        checkin_keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🙋 참여완료", callback_data=f"checkin_{signup_id}")]]
        )
        await query.edit_message_text(
            f"✅ 신청 완료되었습니다!\n\n"
            f"⏰ {slot} / 🏠 {group_name} / 👤 {name} / 👥 {companions} (총 {headcount}명)\n\n"
            "신청해주셔서 감사합니다 🙏\n\n"
            "기도회에 실제로 참여하신 후 아래 버튼을 눌러주세요.",
            reply_markup=checkin_keyboard,
        )
    context.user_data.clear()
    return ConversationHandler.END


async def checkin_clicked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    signup_id = int(query.data.split("_", 1)[1])
    logger.info(
        "[참여완료 클릭] signup_id=%s clicker_user_id=%s clicker_username=%s",
        signup_id, query.from_user.id, query.from_user.username,
    )
    result = mark_checked_in(signup_id, query.from_user.id)
    logger.info("[참여완료 결과] signup_id=%s -> %s", signup_id, result)

    if result == "not_found":
        await query.answer("신청 정보를 찾을 수 없어요.", show_alert=True)
        return
    if result == "not_owner":
        await query.answer("본인 신청 건만 체크인할 수 있어요.", show_alert=True)
        return
    if result == "already":
        await query.answer("이미 참여완료 처리되었어요 🙏", show_alert=True)
        return

    await query.answer("참여 체크 완료! 감사합니다 🙏")
    original_text = query.message.text or ""
    await query.edit_message_text(
        original_text + "\n\n✅ 참여완료 체크되었습니다.",
        reply_markup=None,
    )


async def cancel_signup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("신청이 취소되었습니다. 다시 하시려면 '신청시작' 버튼을 눌러주세요.")
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "신청이 취소되었습니다. 다시 하시려면 '신청시작' 버튼을 눌러주세요.",
        reply_markup=MAIN_MENU_KEYBOARD,
    )
    return ConversationHandler.END


async def my_signups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    user = update.effective_user
    rows = get_signups_for_user(user.id)

    if not rows:
        await update.message.reply_text(
            "신청하신 내역이 없어요.", reply_markup=MAIN_MENU_KEYBOARD
        )
        return ConversationHandler.END

    for r in rows:
        status = "✅ 참여완료" if r["checked_in"] else "⏳ 참여 전"
        headcount = signup_headcount(r["companions"])
        text = (
            f"⏰ {r['slot_time']}\n"
            f"🏠 회/구역: {r['group_name']}\n"
            f"👤 구역장: {r['rep_name']}\n"
            f"📞 연락처: {r['phone']}\n"
            f"👥 동반자: {r['companions']} (총 {headcount}명)\n"
            f"상태: {status}"
        )
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ 이 신청 취소", callback_data=f"usercancel_{r['id']}")]]
        )
        await update.message.reply_text(text, reply_markup=keyboard)
    return ConversationHandler.END


async def user_cancel_clicked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    signup_id = int(query.data.split("_", 1)[1])
    result = delete_signup_by_owner(signup_id, query.from_user.id)

    if result == "not_found":
        await query.answer("이미 취소되었거나 존재하지 않는 신청이에요.", show_alert=True)
        await query.edit_message_reply_markup(reply_markup=None)
        return
    if result == "not_owner":
        await query.answer("본인 신청 건만 취소할 수 있어요.", show_alert=True)
        return

    await query.answer("신청이 취소되었어요.")
    original_text = query.message.text or ""
    await query.edit_message_text(
        original_text + "\n\n❌ 취소된 신청입니다.",
        reply_markup=None,
    )


# ---------------------------------------------------------------------------
# 관리자 핸들러
# ---------------------------------------------------------------------------
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("관리자만 사용할 수 있어요.")
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text(
        "확인하실 시간대를 선택해주세요.", reply_markup=hour_keyboard(prefix="admin_")
    )
    return ConversationHandler.END


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
            filled = sum(signup_headcount(e["companions"]) for e in entries)
            checked_cnt = sum(1 for e in entries if e["checked_in"])
            lines.append(
                f"\n▶ {slot} — {filled}/{MAX_PER_SLOT}명 (참여완료 {checked_cnt}명)"
            )
            for e in entries:
                status = "✅" if e["checked_in"] else "⏳"
                hc = signup_headcount(e["companions"])
                lines.append(
                    f"  {status} [{e['id']}] {e['group_name']} / {e['rep_name']} / "
                    f"{e['phone']} / 동반자: {e['companions']} (총 {hc}명)"
                )
        lines.append("\n삭제하려면 '삭제 (번호)' 형식으로 입력해주세요. 예: 삭제 3")
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


async def admin_delete_signup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return None  # 관리자가 아니면 조용히 무시 (일반 사용자 텍스트와 우연히 겹치는 것 방지)

    match = re.match(r"^삭제\s+(\d+)$", update.message.text.strip())
    if not match:
        return None
    context.user_data.clear()
    signup_id = int(match.group(1))
    deleted = delete_signup(signup_id)
    if deleted:
        await update.message.reply_text(f"🗑 [{signup_id}]번 신청을 삭제했습니다.")
    else:
        await update.message.reply_text(f"[{signup_id}]번 신청을 찾을 수 없어요.")
    return ConversationHandler.END


async def admin_set_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return None

    match = re.match(r"^제목설정\s+(.+)$", update.message.text.strip())
    if not match:
        return None
    context.user_data.clear()
    title = match.group(1).strip()
    set_setting("event_title", title)
    await update.message.reply_text(
        f"✅ 제목이 변경되었습니다.\n\n미리보기:\n🙏{title} 신청 봇입니다."
    )
    return ConversationHandler.END


async def admin_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return None
    match = re.match(r"^관리자추가\s+(\S+)$", update.message.text.strip())
    if not match:
        return None
    context.user_data.clear()
    arg = match.group(1)

    if arg.isdigit():
        new_id = int(arg)
    elif arg.startswith("@"):
        try:
            chat = await context.bot.get_chat(arg)
            new_id = chat.id
        except Exception:
            await update.message.reply_text(
                f"{arg} 님을 찾을 수 없어요. 그 분이 이 봇과 한 번이라도 대화(예: '신청시작')를 "
                "해본 적이 있어야 아이디로 찾을 수 있어요.\n\n"
                "안 되면 숫자 ID로 추가해주세요.\n"
                "(본인 텔레그램 숫자 ID는 @userinfobot 에게 물어보면 알 수 있어요)\n"
                "예: 관리자추가 123456789"
            )
            return ConversationHandler.END
    else:
        await update.message.reply_text(
            "숫자 ID 또는 @username 형식으로 입력해주세요.\n예: 관리자추가 123456789"
        )
        return ConversationHandler.END

    added = add_db_admin(new_id, update.effective_user.id)
    if added:
        await update.message.reply_text(f"✅ {arg} ({new_id}) 님을 관리자로 추가했습니다.")
    else:
        await update.message.reply_text(f"{arg} ({new_id}) 님은 이미 관리자예요.")
    return ConversationHandler.END


async def admin_remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return None
    match = re.match(r"^관리자삭제\s+(\S+)$", update.message.text.strip())
    if not match:
        return None
    context.user_data.clear()
    arg = match.group(1)

    if arg.isdigit():
        target_id = int(arg)
    elif arg.startswith("@"):
        try:
            chat = await context.bot.get_chat(arg)
            target_id = chat.id
        except Exception:
            await update.message.reply_text(
                f"{arg} 님을 찾을 수 없어요. 숫자 ID로 다시 시도해주세요.\n예: 관리자삭제 123456789"
            )
            return ConversationHandler.END
    else:
        await update.message.reply_text(
            "숫자 ID 또는 @username 형식으로 입력해주세요.\n예: 관리자삭제 123456789"
        )
        return ConversationHandler.END

    if target_id in ADMIN_IDS:
        await update.message.reply_text(
            "이 계정은 Render 환경변수(ADMIN_IDS)에 등록된 관리자라 봇에서는 삭제할 수 없어요.\n"
            "삭제하려면 Render 환경변수에서 직접 지워주세요."
        )
        return ConversationHandler.END
    removed = remove_db_admin(target_id)
    if removed:
        await update.message.reply_text(f"🗑 {arg} ({target_id}) 님을 관리자에서 제외했습니다.")
    else:
        await update.message.reply_text(f"{arg} ({target_id}) 님은 관리자 목록에 없어요.")
    return ConversationHandler.END


async def admin_list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return None
    context.user_data.clear()
    env_ids = sorted(ADMIN_IDS)
    db_ids = sorted(get_db_admin_ids())
    lines = ["👑 관리자 목록\n"]
    if env_ids:
        lines.append("[환경변수 등록 - 봇에서 삭제 불가]")
        lines.extend(f"  · {uid}" for uid in env_ids)
    if db_ids:
        lines.append("\n[봇에서 추가된 관리자]")
        lines.extend(f"  · {uid}" for uid in db_ids)
    if not env_ids and not db_ids:
        lines.append("등록된 관리자가 없어요.")
    lines.append(
        "\n추가: '관리자추가 (숫자ID)' / 삭제: '관리자삭제 (숫자ID)'"
    )
    await update.message.reply_text("\n".join(lines))
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Render Web Service용 헬스체크 서버
# ---------------------------------------------------------------------------
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass  # 헬스체크 요청 로그는 생략


def _run_health_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    logger.info("헬스체크 서버 시작 (port %s)", port)
    server.serve_forever()


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN 환경변수가 설정되지 않았습니다.")

    init_db()

    threading.Thread(target=_run_health_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex(r"^신청시작$"), start),
        ],
        states={
            SELECT_SLOT: [
                CallbackQueryHandler(hour_selected, pattern=r"^req_hour_\d+$"),
                CallbackQueryHandler(show_hours_again, pattern=r"^back_hours$"),
                CallbackQueryHandler(full_slot_clicked, pattern=r"^full$"),
                CallbackQueryHandler(slot_selected, pattern=r"^slot_\d{2}:\d{2}$"),
            ],
            ENTER_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, group_entered)],
            ENTER_LEADER: [MessageHandler(filters.TEXT & ~filters.COMMAND, leader_entered)],
            ENTER_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, phone_entered)],
            ENTER_COMPANIONS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, companions_entered)
            ],
            CONFIRM: [
                CallbackQueryHandler(submit_signup, pattern=r"^submit$"),
                CallbackQueryHandler(cancel_signup, pattern=r"^cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_command),
            MessageHandler(filters.Regex(r"^취소$"), cancel_command),
            CommandHandler("mine", my_signups_command),
            MessageHandler(filters.Regex(r"^내\s*신청(\s*확인)?$"), my_signups_command),
            CommandHandler("admin", admin_command),
            MessageHandler(filters.Regex(r"^명단$"), admin_command),
            MessageHandler(filters.Regex(r"^관리자$"), admin_command),
            MessageHandler(filters.Regex(r"^삭제\s+\d+$"), admin_delete_signup),
            MessageHandler(filters.Regex(r"^제목설정\s+.+$"), admin_set_title),
            MessageHandler(filters.Regex(r"^관리자추가\s+\S+$"), admin_add_admin),
            MessageHandler(filters.Regex(r"^관리자삭제\s+\S+$"), admin_remove_admin),
            MessageHandler(filters.Regex(r"^관리자목록$"), admin_list_admins),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("admin", admin_command))
    # 텔레그램은 한글 슬래시 명령어(/명단)를 지원하지 않아서, 텍스트로 "명단"/"관리자"를 보내면 반응하게 처리
    app.add_handler(MessageHandler(filters.Regex(r"^명단$"), admin_command))
    app.add_handler(MessageHandler(filters.Regex(r"^관리자$"), admin_command))
    app.add_handler(MessageHandler(filters.Regex(r"^삭제\s+\d+$"), admin_delete_signup))
    app.add_handler(MessageHandler(filters.Regex(r"^제목설정\s+.+$"), admin_set_title))
    app.add_handler(MessageHandler(filters.Regex(r"^관리자추가\s+\S+$"), admin_add_admin))
    app.add_handler(MessageHandler(filters.Regex(r"^관리자삭제\s+\S+$"), admin_remove_admin))
    app.add_handler(MessageHandler(filters.Regex(r"^관리자목록$"), admin_list_admins))
    app.add_handler(CommandHandler("mine", my_signups_command))
    app.add_handler(MessageHandler(filters.Regex(r"^내\s*신청(\s*확인)?$"), my_signups_command))
    app.add_handler(CallbackQueryHandler(user_cancel_clicked, pattern=r"^usercancel_\d+$"))
    app.add_handler(
        CallbackQueryHandler(admin_hour_selected, pattern=r"^admin_hour_\d+$")
    )
    app.add_handler(CallbackQueryHandler(admin_back, pattern=r"^admin_back$"))
    app.add_handler(CallbackQueryHandler(checkin_clicked, pattern=r"^checkin_\d+$"))

    logger.info("봇 시작...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
