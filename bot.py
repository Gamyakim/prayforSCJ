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
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

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
            created_at TEXT NOT NULL,
            checked_in INTEGER NOT NULL DEFAULT 0,
            checked_in_at TEXT
        )
        """
    )
    # 기존에 이미 배포된 DB에는 컬럼이 없을 수 있어 안전하게 추가 시도
    for col_sql in (
        "ALTER TABLE signups ADD COLUMN checked_in INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE signups ADD COLUMN checked_in_at TEXT",
    ):
        try:
            conn.execute(col_sql)
        except sqlite3.OperationalError:
            pass  # 이미 컬럼이 존재하는 경우
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


def insert_signup(slot_time, rep_name, phone, companions, user_id, username):
    """정원 체크 후 삽입. 성공하면 새 신청의 id, 정원 초과면 None을 반환."""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "SELECT COUNT(*) as cnt FROM signups WHERE slot_time = ?", (slot_time,)
        )
        cnt = cur.fetchone()["cnt"]
        if cnt >= MAX_PER_SLOT:
            conn.rollback()
            return None
        cur = conn.execute(
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
        return cur.lastrowid
    finally:
        conn.close()


def mark_checked_in(signup_id: int, requester_user_id: int):
    """참여완료 체크. 결과를 ('ok'|'already'|'not_owner'|'not_found') 형태로 반환."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM signups WHERE id = ?", (signup_id,)
        ).fetchone()
        if row is None:
            return "not_found"
        if row["telegram_user_id"] != requester_user_id:
            return "not_owner"
        if row["checked_in"]:
            return "already"
        conn.execute(
            "UPDATE signups SET checked_in = 1, checked_in_at = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), signup_id),
        )
        conn.commit()
        return "ok"
    finally:
        conn.close()


def delete_signup(signup_id: int) -> bool:
    """신청 건 삭제. 삭제됐으면 True, 존재하지 않으면 False."""
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM signups WHERE id = ?", (signup_id,))
        conn.commit()
        return cur.rowcount > 0
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

    signup_id = insert_signup(slot, name, phone, companions, user.id, user.username or "")

    if signup_id is None:
        await query.edit_message_text(
            "😥 죄송해요, 방금 정원이 다 찼어요.\n/start 로 다른 타임을 선택해주세요."
        )
    else:
        checkin_keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🙋 참여완료", callback_data=f"checkin_{signup_id}")]]
        )
        await query.edit_message_text(
            f"✅ 신청 완료되었습니다!\n\n"
            f"⏰ {slot} / 👤 {name} / 👥 {companions}\n\n"
            "신청해주셔서 감사합니다 🙏\n\n"
            "기도회에 실제로 참여하신 후 아래 버튼을 눌러주세요.",
            reply_markup=checkin_keyboard,
        )
    context.user_data.clear()
    return ConversationHandler.END


async def checkin_clicked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    signup_id = int(query.data.split("_", 1)[1])
    result = mark_checked_in(signup_id, query.from_user.id)

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
            checked_cnt = sum(1 for e in entries if e["checked_in"])
            lines.append(
                f"\n▶ {slot} — {len(entries)}/{MAX_PER_SLOT}명 (참여완료 {checked_cnt}명)"
            )
            for e in entries:
                status = "✅" if e["checked_in"] else "⏳"
                lines.append(
                    f"  {status} [{e['id']}] {e['rep_name']} / {e['phone']} / 동반자: {e['companions']}"
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
        return  # 관리자가 아니면 조용히 무시 (일반 사용자 텍스트와 우연히 겹치는 것 방지)

    match = re.match(r"^삭제\s+(\d+)$", update.message.text.strip())
    if not match:
        return
    signup_id = int(match.group(1))
    deleted = delete_signup(signup_id)
    if deleted:
        await update.message.reply_text(f"🗑 [{signup_id}]번 신청을 삭제했습니다.")
    else:
        await update.message.reply_text(f"[{signup_id}]번 신청을 찾을 수 없어요.")


# ---------------------------------------------------------------------------
# Render Web Service용 헬스체크 서버
# 텔레그램 봇은 포트를 쓰지 않지만, Render가 Web Service 타입에서
# 포트 응답 여부로 헬스체크를 하기 때문에 아주 작은 서버를 같이 띄워줌
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
        fallbacks=[
            CommandHandler("cancel", cancel_command),
            MessageHandler(filters.Regex(r"^취소$"), cancel_command),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("admin", admin_command))
    # 텔레그램은 한글 슬래시 명령어(/명단)를 지원하지 않아서, 텍스트로 "명단"을 보내면 반응하게 처리
    app.add_handler(MessageHandler(filters.Regex(r"^명단$"), admin_command))
    app.add_handler(MessageHandler(filters.Regex(r"^삭제\s+\d+$"), admin_delete_signup))
    app.add_handler(
        CallbackQueryHandler(admin_hour_selected, pattern=r"^admin_hour_\d+$")
    )
    app.add_handler(CallbackQueryHandler(admin_back, pattern=r"^admin_back$"))
    app.add_handler(CallbackQueryHandler(checkin_clicked, pattern=r"^checkin_\d+$"))

    logger.info("봇 시작...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
