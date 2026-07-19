# -*- coding: utf-8 -*-
"""
기도회 신청 텔레그램 봇
- 오후 5시~8시(17:00~19:50), 10분 단위 타임슬롯
- 슬롯당 최대 10명 (인솔자 1명 + 동반자 합산 인원 기준)
- 회/지역/팀/구역명 / 인솔자 이름 / 연락처(010-XXXX-XXXX 검증) / 동반자(콤마 구분) 입력
- 참여완료 체크 (본인만)
- 관리자는 버튼/텍스트로 타임별 명단 조회, 전체 명단, 삭제, 제목 설정, 관리자 추가/삭제
- 데이터 저장: PostgreSQL (Render 재배포에도 데이터 유지)
- 구글 시트 실시간 미러링 (선택 사항 - 환경변수 없으면 자동 비활성화)
"""

import os
import re
import json
import asyncio
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg2
import psycopg2.extras
import gspread
from google.oauth2.service_account import Credentials

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
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

# 동반자 입력용 미니앱(WebApp) 주소. Render가 자동으로 넣어주는 RENDER_EXTERNAL_URL을
# 우선 쓰고, 없으면 WEBAPP_BASE_URL을 직접 지정할 수 있게 함. 둘 다 없으면 버튼 없이
# 콤마 직접 입력 방식만 안내한다.
WEBAPP_BASE_URL = (
    os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    or os.environ.get("WEBAPP_BASE_URL", "").rstrip("/")
)

MAX_PER_SLOT = 10  # 10분 슬롯당 최대 "인원" (인솔자 + 동반자 합산)

# 운영 시간: 17:00 ~ 19:50, 10분 단위 전체
HOUR_MINUTES = {
    17: [0, 10, 20, 30, 40, 50],
    18: [0, 10, 20, 30, 40, 50],
    19: [0, 10, 20, 30, 40, 50],
}
HOURS = list(HOUR_MINUTES.keys())


def hour_capacity(hour: int) -> int:
    return len(HOUR_MINUTES.get(hour, [])) * MAX_PER_SLOT


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
# 동반자(콤마 구분) 인원수 계산 유틸
# ---------------------------------------------------------------------------
def companion_count(companions: str) -> int:
    s = (companions or "").strip()
    if not s or s == "없음":
        return 0
    # 콤마(,)로 구분하는 게 기본이지만, 사용자가 띄어쓰기로만 구분해서 입력한 경우도
    # 대비해서 콤마/공백 어느 쪽이든 구분자로 인식해 정확히 센다.
    parts = re.split(r"[,\s]+", s)
    return len([c for c in parts if c.strip()])


def signup_headcount(companions: str) -> int:
    return 1 + companion_count(companions)


# ---------------------------------------------------------------------------
# 슬롯 정원 = 인원수(인솔자+동반자) 기준 (10명)
# ---------------------------------------------------------------------------
def get_slot_headcount(slot_time: str) -> int:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT companions FROM signups WHERE slot_time = %s", (slot_time,))
        rows = cur.fetchall()
        return sum(signup_headcount(r["companions"]) for r in rows)
    finally:
        conn.close()


def get_hour_headcount(hour: int) -> int:
    conn = get_conn()
    try:
        cur = conn.cursor()
        prefix = f"{hour:02d}:"
        cur.execute("SELECT companions FROM signups WHERE slot_time LIKE %s", (f"{prefix}%",))
        rows = cur.fetchall()
        return sum(signup_headcount(r["companions"]) for r in rows)
    finally:
        conn.close()


def insert_signup(slot_time, group_name, rep_name, phone, companions, user_id, username):
    """슬롯 정원(인원수 10명) 체크 후 삽입. 성공하면 새 신청의 id, 정원 초과면 None을 반환.
    테이블 락으로 동시 신청 시에도 정원이 초과되지 않도록 보장."""
    new_headcount = signup_headcount(companions)
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("LOCK TABLE signups IN SHARE ROW EXCLUSIVE MODE")
        cur.execute("SELECT companions FROM signups WHERE slot_time = %s", (slot_time,))
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
                slot_time, group_name, rep_name, phone, companions,
                user_id, username, datetime.now().isoformat(timespec="seconds"),
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


def admin_unmark_checked_in(signup_id: int):
    """관리자가 참여완료 상태를 되돌림. 결과를 ('ok'|'not_checked'|'not_found') 형태로 반환."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM signups WHERE id = %s", (signup_id,))
        row = cur.fetchone()
        if row is None:
            return "not_found"
        if not row["checked_in"]:
            return "not_checked"
        cur.execute(
            "UPDATE signups SET checked_in = 0, checked_in_at = NULL WHERE id = %s",
            (signup_id,),
        )
        conn.commit()
        return "ok"
    finally:
        conn.close()


def get_all_signups():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM signups ORDER BY slot_time, id")
        return cur.fetchall()
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
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM signups WHERE id = %s", (signup_id,))
        deleted = cur.rowcount > 0
        conn.commit()
        return deleted
    finally:
        conn.close()


def get_signup_by_id(signup_id: int):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM signups WHERE id = %s", (signup_id,))
        return cur.fetchone()
    finally:
        conn.close()


def get_slot_headcount_excluding(slot_time: str, exclude_id: int) -> int:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT companions FROM signups WHERE slot_time = %s AND id != %s",
            (slot_time, exclude_id),
        )
        rows = cur.fetchall()
        return sum(signup_headcount(r["companions"]) for r in rows)
    finally:
        conn.close()


def update_signup_field(signup_id: int, column: str, value: str) -> bool:
    """column은 아래 함수 내부 화이트리스트에서만 골라 쓰므로 SQL 인젝션 위험 없음."""
    if column not in ("group_name", "rep_name", "phone", "companions"):
        raise ValueError(f"허용되지 않은 컬럼: {column}")
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE signups SET {column} = %s WHERE id = %s", (value, signup_id)
        )
        updated = cur.rowcount > 0
        conn.commit()
        return updated
    finally:
        conn.close()


def delete_signup_by_owner(signup_id: int, requester_user_id: int) -> str:
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
# 구글 시트 연동 (선택 사항 - 환경변수 없으면 자동으로 비활성화)
# ---------------------------------------------------------------------------
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_SHEETS_CREDENTIALS_JSON = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_JSON", "")

_sheet_cache = {"ws": None, "tried": False}


def _load_google_credentials():
    if not GOOGLE_SHEETS_CREDENTIALS_JSON:
        return None
    try:
        info = json.loads(GOOGLE_SHEETS_CREDENTIALS_JSON)
    except json.JSONDecodeError:
        logger.warning("GOOGLE_SHEETS_CREDENTIALS_JSON 파싱에 실패했어요. JSON 형식을 확인해주세요.")
        return None
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    return Credentials.from_service_account_info(info, scopes=scopes)


def _ensure_sheet_header(ws):
    try:
        expected = ["ID", "시간", "회/지역/팀/구역", "인솔자", "연락처", "동반자", "인원수", "참여여부", "신청일시"]
        first_row = ws.row_values(1)
        if not first_row:
            ws.append_row(expected)
        elif len(first_row) >= 3 and first_row[2] != "회/지역/팀/구역":
            # 예전 헤더("회/구역" 등)가 이미 있으면 새 문구로 갱신
            ws.update("A1", [expected])
    except Exception:
        logger.exception("구글 시트 헤더 설정 실패")


def _get_worksheet():
    if _sheet_cache["ws"] is not None:
        return _sheet_cache["ws"]
    if _sheet_cache["tried"]:
        return None
    _sheet_cache["tried"] = True

    if not GOOGLE_SHEET_ID:
        logger.info("GOOGLE_SHEET_ID가 설정되지 않아 구글 시트 연동을 건너뜁니다.")
        return None
    creds = _load_google_credentials()
    if not creds:
        logger.info("구글 시트 인증 정보가 없어 연동을 건너뜁니다.")
        return None
    try:
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        ws = sh.sheet1
        _ensure_sheet_header(ws)
        _sheet_cache["ws"] = ws
        logger.info("구글 시트 연동 성공")
        return ws
    except Exception:
        logger.exception("구글 시트 연결 실패")
        return None


def sheet_append_signup(signup_id, slot, group_name, rep_name, phone, companions, headcount, created_at):
    ws = _get_worksheet()
    if ws is None:
        return
    try:
        ws.append_row(
            [str(signup_id), slot, group_name, rep_name, phone, companions, headcount, "⏳", created_at]
        )
    except Exception:
        logger.exception("구글 시트 기록 실패 (signup_id=%s)", signup_id)


def _find_sheet_row_by_id(ws, signup_id):
    try:
        cell = ws.find(str(signup_id), in_column=1)
        return cell.row if cell else None
    except Exception:
        logger.exception("구글 시트에서 행 찾기 실패 (signup_id=%s)", signup_id)
        return None


def sheet_update_checkin(signup_id):
    ws = _get_worksheet()
    if ws is None:
        return
    row = _find_sheet_row_by_id(ws, signup_id)
    if row is None:
        return
    try:
        ws.update_cell(row, 8, "✅")
    except Exception:
        logger.exception("구글 시트 참여완료 업데이트 실패 (signup_id=%s)", signup_id)


def sheet_unmark_checkin(signup_id):
    ws = _get_worksheet()
    if ws is None:
        return
    row = _find_sheet_row_by_id(ws, signup_id)
    if row is None:
        return
    try:
        ws.update_cell(row, 8, "⏳")
    except Exception:
        logger.exception("구글 시트 참여완료 되돌리기 실패 (signup_id=%s)", signup_id)


# 시트 컬럼: A=ID, B=시간, C=회/지역/팀/구역, D=인솔자, E=연락처, F=동반자, G=인원수, H=참여여부, I=신청일시
_SHEET_FIELD_COLUMN = {
    "group_name": 3,
    "rep_name": 4,
    "phone": 5,
    "companions": 6,
}


def sheet_update_field(signup_id, field: str, value: str, headcount: int = None):
    ws = _get_worksheet()
    if ws is None:
        return
    row = _find_sheet_row_by_id(ws, signup_id)
    if row is None:
        return
    col = _SHEET_FIELD_COLUMN.get(field)
    if col is None:
        return
    try:
        ws.update_cell(row, col, value)
        if headcount is not None:
            ws.update_cell(row, 7, headcount)
    except Exception:
        logger.exception("구글 시트 필드 수정 실패 (signup_id=%s, field=%s)", signup_id, field)


def sheet_delete_row(signup_id):
    ws = _get_worksheet()
    if ws is None:
        return
    row = _find_sheet_row_by_id(ws, signup_id)
    if row is None:
        return
    try:
        ws.delete_rows(row)
    except Exception:
        logger.exception("구글 시트 행 삭제 실패 (signup_id=%s)", signup_id)


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
    """운영 시간(17~19시) 선택 키보드. 시간대별 인원 현황을 같이 보여준다."""
    buttons = []
    row = []
    for h in HOURS:
        filled = get_hour_headcount(h)
        cap = hour_capacity(h)
        label = f"{h}시({filled}/{cap}명)"
        if filled >= cap:
            label += " 마감"
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
    for m in HOUR_MINUTES.get(hour, []):
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
    user = update.effective_user
    title = get_setting("event_title", DEFAULT_EVENT_TITLE)

    if not is_admin(user.id):
        existing = get_signups_for_user(user.id)
        if existing:
            slot_list = ", ".join(r["slot_time"] for r in existing)
            await update.message.reply_text(
                f"🙏{title}\n\n"
                f"이미 신청하신 내역이 있어요 ({slot_list}).\n"
                "한 분당 신청은 1건만 가능해요.\n\n"
                "다른 시간으로 다시 신청하시려면, '내 신청 확인'에서 "
                "기존 신청을 먼저 취소해주세요.",
                reply_markup=MAIN_MENU_KEYBOARD,
            )
            return ConversationHandler.END

    await update.message.reply_text(
        f"🙏{title} 신청 봇입니다.\n\n아래 메뉴 버튼을 이용해주세요.",
        reply_markup=MAIN_MENU_KEYBOARD,
    )
    await update.message.reply_text(
        "오후 5시 ~ 8시, 10분 단위로 신청하실 수 있어요.\n"
        f"타임당 최대 {MAX_PER_SLOT}명(인솔자+동반자 합산)까지 신청 가능합니다.\n\n"
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
    await query.edit_message_text(f"선택하신 타임: {slot}")

    text = (
        "아래 버튼을 눌러 팝업창에서 회/지역/팀/구역, 인솔자 이름, 연락처, 동반자를 "
        "한 번에 입력하시거나,\n"
        "그냥 회/지역/팀/구역명을 텍스트로 바로 입력하셔도 돼요."
    )
    if WEBAPP_BASE_URL:
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton(
                "📝 신청 정보 입력하기",
                web_app=WebAppInfo(url=f"{WEBAPP_BASE_URL}/webapp"),
            )]],
            resize_keyboard=True,
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=text, reply_markup=keyboard
        )
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="회/지역/팀/구역명을 입력해주세요.",
        )
    return ENTER_GROUP


async def group_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    group_name = update.message.text.strip()
    if not group_name:
        await update.message.reply_text("회/지역/팀/구역명을 입력해주세요.")
        return ENTER_GROUP
    context.user_data["group_name"] = group_name
    await update.message.reply_text("인솔자 이름을 입력해주세요.")
    return ENTER_LEADER


async def form_webapp_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """통합 팝업 폼(회/지역/팀/구역+인솔자+연락처+동반자)에서 한 번에 제출된 데이터 처리."""
    raw = update.effective_message.web_app_data.data
    try:
        data = json.loads(raw)
    except Exception:
        logger.exception("통합 폼 데이터 파싱 실패: %s", raw)
        await update.effective_message.reply_text(
            "입력값을 처리하지 못했어요. 버튼을 다시 눌러 시도해주세요."
        )
        return ENTER_GROUP

    group_name = (data.get("group_name") or "").strip()
    rep_name = (data.get("rep_name") or "").strip()
    phone = (data.get("phone") or "").strip()
    companions_list = data.get("companions") or []
    companions_list = [c.strip() for c in companions_list if isinstance(c, str) and c.strip()]
    companions = ",".join(companions_list) if companions_list else "없음"

    if not group_name:
        await update.effective_message.reply_text(
            "회/지역/팀/구역명이 비어있어요. 버튼을 다시 눌러 입력해주세요."
        )
        return ENTER_GROUP
    if not rep_name:
        await update.effective_message.reply_text(
            "인솔자 이름이 비어있어요. 버튼을 다시 눌러 입력해주세요."
        )
        return ENTER_GROUP
    if not PHONE_PATTERN.match(phone):
        await update.effective_message.reply_text(
            "연락처 형식이 올바르지 않아요 (예: 010-1234-5678). 버튼을 다시 눌러 입력해주세요."
        )
        return ENTER_GROUP

    context.user_data["group_name"] = group_name
    context.user_data["rep_name"] = rep_name
    context.user_data["phone"] = phone

    return await _finalize_companions(
        update.effective_message, context, companions, retry_state=ENTER_GROUP
    )


async def leader_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("인솔자 이름을 입력해주세요.")
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

    text = (
        "같이 갈 구역원이 있으면 알려주세요.\n\n"
        "아래 버튼을 눌러 팝업창에서 한 명씩 입력하시거나,\n"
        "콤마(,)로 구분해서 텍스트로 바로 입력하셔도 돼요.\n"
        "예: 김OO,이OO,박OO\n"
        "없으면 '없음'이라고 입력해주세요."
    )
    if WEBAPP_BASE_URL:
        # 텔레그램 정책상 팝업에서 봇으로 데이터(sendData)를 보내려면
        # 인라인 버튼이 아니라 하단 고정 키보드 버튼으로 열어야 함
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton(
                "📝 동반자 입력하기",
                web_app=WebAppInfo(url=f"{WEBAPP_BASE_URL}/webapp/companions"),
            )]],
            resize_keyboard=True,
        )
        await update.message.reply_text(text, reply_markup=keyboard)
    else:
        await update.message.reply_text(text)
    return ENTER_COMPANIONS


async def _finalize_companions(message, context, companions: str, retry_state=ENTER_COMPANIONS):
    """콤마 입력이든 미니앱 제출이든 공통으로 인원 검증 후 확인 화면을 보여준다.
    retry_state: 인원 초과 등으로 거부됐을 때 되돌아갈 상태.
    통합 폼(ENTER_GROUP) 경로에서 왔으면 통합 폼으로, 동반자 전용 경로에서 왔으면
    동반자 입력으로 정확히 돌려보내야 다음 제출이 엉뚱한 처리기로 새지 않는다."""
    headcount = signup_headcount(companions)

    if headcount > MAX_PER_SLOT:
        await message.reply_text(
            f"한 타임 최대 인원은 {MAX_PER_SLOT}명이에요 (인솔자 포함). "
            f"입력하신 인원은 총 {headcount}명이라 넘어가요.\n"
            "동반자 수를 줄여서 다시 입력해주세요."
        )
        return await _reprompt_for_retry(message, context, retry_state)

    slot = context.user_data["slot_time"]
    current = get_slot_headcount(slot)
    if current + headcount > MAX_PER_SLOT:
        remaining = max(MAX_PER_SLOT - current, 0)
        await message.reply_text(
            f"'{slot}' 타임에 남은 자리가 {remaining}명뿐이에요 (입력하신 인원 총 {headcount}명).\n"
            "동반자 수를 줄이거나, '취소' 입력 후 다른 타임을 선택해주세요."
        )
        return await _reprompt_for_retry(message, context, retry_state)

    context.user_data["companions"] = companions

    name = context.user_data["rep_name"]
    phone = context.user_data["phone"]
    group_name = context.user_data["group_name"]

    summary = (
        "📋 신청 내용을 확인해주세요.\n\n"
        f"⏰ 시간: {slot}\n"
        f"🏠 회/지역/팀/구역: {group_name}\n"
        f"👤 인솔자: {name}\n"
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
    await message.reply_text(summary, reply_markup=keyboard)
    return CONFIRM


async def _reprompt_for_retry(message, context, retry_state):
    """인원 초과 등으로 거부된 후, 다음 입력이 올바른 처리기로 가도록 상태에 맞는
    버튼/안내를 다시 보여준다."""
    if retry_state == ENTER_GROUP:
        # 통합 폼 경로에서 거부된 경우: 이전에 입력했던 값은 버리고 폼을 통째로 다시 받는다
        # (부분적으로 남은 값이 엉뚱하게 재사용되는 것을 방지)
        context.user_data.pop("group_name", None)
        context.user_data.pop("rep_name", None)
        context.user_data.pop("phone", None)
        context.user_data.pop("companions", None)
        if WEBAPP_BASE_URL:
            keyboard = ReplyKeyboardMarkup(
                [[KeyboardButton(
                    "📝 신청 정보 입력하기",
                    web_app=WebAppInfo(url=f"{WEBAPP_BASE_URL}/webapp"),
                )]],
                resize_keyboard=True,
            )
            await message.reply_text(
                "아래 버튼을 다시 눌러서 처음부터 다시 입력해주세요.",
                reply_markup=keyboard,
            )
        else:
            await message.reply_text("회/지역/팀/구역명부터 다시 입력해주세요.")
        return ENTER_GROUP

    # 동반자 입력(ENTER_COMPANIONS) 경로에서 거부된 경우: 동반자만 다시 받으면 됨
    if WEBAPP_BASE_URL:
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton(
                "📝 동반자 입력하기",
                web_app=WebAppInfo(url=f"{WEBAPP_BASE_URL}/webapp/companions"),
            )]],
            resize_keyboard=True,
        )
        await message.reply_text(
            "동반자를 다시 입력해주세요.", reply_markup=keyboard
        )
    else:
        await message.reply_text("동반자를 다시 입력해주세요 (없으면 '없음').")
    return ENTER_COMPANIONS


async def companions_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    companions = update.message.text.strip()
    return await _finalize_companions(update.message, context, companions)


async def companions_webapp_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.effective_message.web_app_data.data
    try:
        names = json.loads(raw)
        names = [n.strip() for n in names if isinstance(n, str) and n.strip()]
    except Exception:
        logger.exception("미니앱에서 받은 동반자 데이터 파싱 실패: %s", raw)
        names = []
    companions = ",".join(names) if names else "없음"
    return await _finalize_companions(update.effective_message, context, companions)


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

    if not is_admin(user.id):
        existing = get_signups_for_user(user.id)
        if existing:
            await query.edit_message_text(
                "😥 이미 신청하신 내역이 있어서 추가 신청이 안 돼요.\n"
                "'내 신청 확인'에서 기존 신청을 먼저 취소해주세요."
            )
            context.user_data.clear()
            return ConversationHandler.END

    signup_id = insert_signup(
        slot, group_name, name, phone, companions, user.id, user.username or ""
    )

    logger.info("[제출 결과] user_id=%s -> signup_id=%s", user.id, signup_id)

    if signup_id is None:
        await query.edit_message_text(
            "😥 죄송해요, 방금 사이에 정원이 다 찼어요.\n"
            "'신청시작' 버튼으로 다른 타임을 선택해주세요."
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="👇 아래 메뉴에서 다시 시도해주세요.",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
    else:
        headcount = signup_headcount(companions)
        await asyncio.to_thread(
            sheet_append_signup,
            signup_id, slot, group_name, name, phone, companions, headcount,
            datetime.now().isoformat(timespec="seconds"),
        )
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
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="👇 다른 신청을 하시거나 내 신청을 확인하려면 아래 메뉴를 이용해주세요.",
            reply_markup=MAIN_MENU_KEYBOARD,
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

    await asyncio.to_thread(sheet_update_checkin, signup_id)

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
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="👇 아래 메뉴를 이용해주세요.",
        reply_markup=MAIN_MENU_KEYBOARD,
    )
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
            f"🏠 회/지역/팀/구역: {r['group_name']}\n"
            f"👤 인솔자: {r['rep_name']}\n"
            f"📞 연락처: {r['phone']}\n"
            f"👥 동반자: {r['companions']} (총 {headcount}명)\n"
            f"상태: {status}"
        )
        buttons = []
        if not r["checked_in"]:
            buttons.append(
                InlineKeyboardButton("🙋 참여완료", callback_data=f"checkin_{r['id']}")
            )
        buttons.append(
            InlineKeyboardButton("❌ 이 신청 취소", callback_data=f"usercancel_{r['id']}")
        )
        keyboard = InlineKeyboardMarkup([buttons])
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

    await asyncio.to_thread(sheet_delete_row, signup_id)

    await query.answer("신청이 취소되었어요.")
    original_text = query.message.text or ""
    await query.edit_message_text(
        original_text + "\n\n❌ 취소된 신청입니다.",
        reply_markup=None,
    )


# ---------------------------------------------------------------------------
# 관리자 핸들러
# ---------------------------------------------------------------------------
def _build_full_list_text() -> str:
    rows = get_all_signups()
    if not rows:
        return "전체 신청 내역이 없어요."

    by_slot = {}
    for r in rows:
        by_slot.setdefault(r["slot_time"], []).append(r)

    total_signups = len(rows)
    total_people = sum(signup_headcount(r["companions"]) for r in rows)
    checked_rows = [r for r in rows if r["checked_in"]]
    total_checked = len(checked_rows)
    total_checked_people = sum(signup_headcount(r["companions"]) for r in checked_rows)

    lines = [
        f"📋 전체 신청 명단\n"
        f"총 {total_signups}건({total_people}명) 신청 / "
        f"참여완료 {total_checked}건({total_checked_people}명)\n"
    ]
    for h in HOURS:
        for m in HOUR_MINUTES.get(h, []):
            slot = f"{h:02d}:{m:02d}"
            entries = by_slot.get(slot)
            if not entries:
                continue
            filled = sum(signup_headcount(e["companions"]) for e in entries)
            checked_entries = [e for e in entries if e["checked_in"]]
            checked_cnt = len(checked_entries)
            checked_people = sum(signup_headcount(e["companions"]) for e in checked_entries)
            lines.append(
                f"\n▶ {slot} — {len(entries)}건({filled}/{MAX_PER_SLOT}명) "
                f"(참여완료 {checked_cnt}건({checked_people}명))"
            )
            for e in entries:
                status = "✅" if e["checked_in"] else "⏳"
                hc = signup_headcount(e["companions"])
                lines.append(
                    f"  {status} [{e['id']}] {e['group_name']} / {e['rep_name']} / "
                    f"{e['phone']} / 동반자: {e['companions']} (총 {hc}명)"
                )
    return "\n".join(lines)


def _build_admin_status_text() -> str:
    env_ids = sorted(ADMIN_IDS)
    db_ids = sorted(get_db_admin_ids())
    lines = ["👑 관리자 현황\n"]
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
    return "\n".join(lines)


def _admin_menu_keyboard():
    kb = hour_keyboard(prefix="admin_")
    rows = list(kb.inline_keyboard)
    rows.append(
        [
            InlineKeyboardButton("📋 전체명단", callback_data="admin_fulllist"),
            InlineKeyboardButton("👑 관리자현황", callback_data="admin_status"),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("관리자만 사용할 수 있어요.")
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text(
        "확인하실 항목을 선택해주세요.", reply_markup=_admin_menu_keyboard()
    )
    return ConversationHandler.END


async def admin_fulllist_clicked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("관리자만 사용할 수 있어요.", show_alert=True)
        return
    await query.answer()
    full_text = _build_full_list_text()
    CHUNK_SIZE = 3500
    for i in range(0, len(full_text), CHUNK_SIZE):
        await context.bot.send_message(
            chat_id=query.message.chat_id, text=full_text[i:i + CHUNK_SIZE]
        )


async def admin_status_clicked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("관리자만 사용할 수 있어요.", show_alert=True)
        return
    await query.answer()
    await context.bot.send_message(
        chat_id=query.message.chat_id, text=_build_admin_status_text()
    )


async def admin_full_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return None
    context.user_data.clear()
    full_text = _build_full_list_text()
    CHUNK_SIZE = 3500
    for i in range(0, len(full_text), CHUNK_SIZE):
        await update.message.reply_text(full_text[i:i + CHUNK_SIZE])
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
        for m in HOUR_MINUTES.get(hour, []):
            slot = f"{hour:02d}:{m:02d}"
            entries = by_slot.get(slot, [])
            filled = sum(signup_headcount(e["companions"]) for e in entries)
            checked_entries = [e for e in entries if e["checked_in"]]
            checked_cnt = len(checked_entries)
            checked_people = sum(signup_headcount(e["companions"]) for e in checked_entries)
            lines.append(
                f"\n▶ {slot} — {len(entries)}건({filled}/{MAX_PER_SLOT}명) "
                f"(참여완료 {checked_cnt}건({checked_people}명))"
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
        "확인하실 항목을 선택해주세요.", reply_markup=_admin_menu_keyboard()
    )


async def admin_delete_signup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return None

    match = re.match(r"^삭제\s+(\d+)$", update.message.text.strip())
    if not match:
        return None
    context.user_data.clear()
    signup_id = int(match.group(1))
    deleted = delete_signup(signup_id)
    if deleted:
        await asyncio.to_thread(sheet_delete_row, signup_id)
        await update.message.reply_text(f"🗑 [{signup_id}]번 신청을 삭제했습니다.")
    else:
        await update.message.reply_text(f"[{signup_id}]번 신청을 찾을 수 없어요.")
    return ConversationHandler.END


async def admin_unmark_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return None

    match = re.match(r"^참여취소\s+(\d+)$", update.message.text.strip())
    if not match:
        return None
    context.user_data.clear()
    signup_id = int(match.group(1))
    result = admin_unmark_checked_in(signup_id)
    if result == "ok":
        await asyncio.to_thread(sheet_unmark_checkin, signup_id)
        await update.message.reply_text(f"↩️ [{signup_id}]번 신청을 참여 전 상태로 되돌렸습니다.")
    elif result == "not_checked":
        await update.message.reply_text(f"[{signup_id}]번 신청은 아직 참여완료 상태가 아니에요.")
    else:
        await update.message.reply_text(f"[{signup_id}]번 신청을 찾을 수 없어요.")
    return ConversationHandler.END


_EDIT_FIELD_MAP = {
    "회": "group_name",
    "인솔자": "rep_name",
    "연락처": "phone",
    "동반자": "companions",
}


async def admin_edit_signup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return None

    match = re.match(
        r"^수정\s+(\d+)\s+(회|인솔자|연락처|동반자)\s+(.+)$",
        update.message.text.strip(),
        re.DOTALL,
    )
    if not match:
        return None
    context.user_data.clear()

    signup_id = int(match.group(1))
    field_label = match.group(2)
    new_value = match.group(3).strip()
    column = _EDIT_FIELD_MAP[field_label]

    row = get_signup_by_id(signup_id)
    if row is None:
        await update.message.reply_text(f"[{signup_id}]번 신청을 찾을 수 없어요.")
        return ConversationHandler.END

    headcount_for_sheet = None

    if column == "phone":
        if not PHONE_PATTERN.match(new_value):
            await update.message.reply_text(
                "연락처 형식이 올바르지 않아요. 예: 010-1234-5678"
            )
            return ConversationHandler.END

    if column == "companions":
        new_headcount = signup_headcount(new_value)
        if new_headcount > MAX_PER_SLOT:
            await update.message.reply_text(
                f"한 건당 최대 인원은 {MAX_PER_SLOT}명이에요 (인솔자 포함). "
                f"입력하신 인원은 총 {new_headcount}명이라 넘어가요."
            )
            return ConversationHandler.END
        others = get_slot_headcount_excluding(row["slot_time"], signup_id)
        if others + new_headcount > MAX_PER_SLOT:
            remaining = max(MAX_PER_SLOT - others, 0)
            await update.message.reply_text(
                f"'{row['slot_time']}' 타임에 이 신청을 제외한 인원이 이미 {others}명이라, "
                f"수정 후 인원은 최대 {remaining}명까지만 가능해요."
            )
            return ConversationHandler.END
        headcount_for_sheet = new_headcount

    update_signup_field(signup_id, column, new_value)
    await asyncio.to_thread(
        sheet_update_field, signup_id, column, new_value, headcount_for_sheet
    )

    await update.message.reply_text(
        f"✅ [{signup_id}]번 신청의 {field_label} 항목을 다음으로 수정했어요:\n{new_value}"
    )
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
    await update.message.reply_text(_build_admin_status_text())
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Render Web Service용 헬스체크 서버 + 신청 정보 입력 미니앱(WebApp) 페이지
# ---------------------------------------------------------------------------
_COMPANIONS_WEBAPP_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>동반자 입력</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", sans-serif;
    background: var(--tg-theme-bg-color, #ffffff);
    color: var(--tg-theme-text-color, #222222);
    margin: 0;
    padding: 16px;
  }
  h2 { font-size: 18px; margin-bottom: 4px; }
  p.desc { font-size: 13px; color: #888; margin-top: 0; margin-bottom: 16px; }
  .row {
    display: flex;
    align-items: center;
    margin-bottom: 8px;
  }
  .row input {
    flex: 1;
    padding: 10px 12px;
    font-size: 15px;
    border: 1px solid #ddd;
    border-radius: 8px;
    background: var(--tg-theme-secondary-bg-color, #f5f5f5);
    color: inherit;
  }
  .row button.remove {
    margin-left: 8px;
    border: none;
    background: none;
    color: #e74c3c;
    font-size: 20px;
    cursor: pointer;
  }
  #addBtn {
    width: 100%;
    padding: 10px;
    margin-top: 4px;
    border: 1px dashed #aaa;
    border-radius: 8px;
    background: none;
    color: inherit;
    font-size: 14px;
    cursor: pointer;
  }
  #submitBtn {
    width: 100%;
    padding: 14px;
    margin-top: 20px;
    border: none;
    border-radius: 8px;
    background: var(--tg-theme-button-color, #2481cc);
    color: var(--tg-theme-button-text-color, #ffffff);
    font-size: 16px;
    font-weight: bold;
    cursor: pointer;
  }
</style>
</head>
<body>
  <h2>👥 같이 갈 구역원</h2>
  <p class="desc">한 명씩 이름을 입력해주세요. 없으면 그대로 제출하면 '없음'으로 처리돼요.</p>
  <div id="rows"></div>
  <button id="addBtn" type="button">➕ 동반자 추가</button>
  <button id="submitBtn" type="button">✅ 제출</button>

<script>
  const tg = window.Telegram.WebApp;
  tg.expand();

  const rowsEl = document.getElementById('rows');

  function addRow(value) {
    const row = document.createElement('div');
    row.className = 'row';
    const input = document.createElement('input');
    input.type = 'text';
    input.placeholder = '이름 입력';
    input.value = value || '';
    const removeBtn = document.createElement('button');
    removeBtn.className = 'remove';
    removeBtn.type = 'button';
    removeBtn.textContent = '✕';
    removeBtn.onclick = () => row.remove();
    row.appendChild(input);
    row.appendChild(removeBtn);
    rowsEl.appendChild(row);
  }

  addRow('');

  document.getElementById('addBtn').onclick = () => addRow('');

  document.getElementById('submitBtn').onclick = () => {
    const inputs = rowsEl.querySelectorAll('input');
    const names = Array.from(inputs)
      .map(i => i.value.trim())
      .filter(v => v.length > 0);
    tg.sendData(JSON.stringify(names));
    tg.close();
  };
</script>
</body>
</html>
"""


_FULL_FORM_WEBAPP_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>신청 정보 입력</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", sans-serif;
    background: var(--tg-theme-bg-color, #ffffff);
    color: var(--tg-theme-text-color, #222222);
    margin: 0;
    padding: 16px;
  }
  h2 { font-size: 18px; margin-bottom: 16px; }
  label {
    display: block;
    font-size: 13px;
    color: #888;
    margin-bottom: 4px;
    margin-top: 16px;
  }
  input[type="text"], input[type="tel"] {
    width: 100%;
    box-sizing: border-box;
    padding: 10px 12px;
    font-size: 15px;
    border: 1px solid #ddd;
    border-radius: 8px;
    background: var(--tg-theme-secondary-bg-color, #f5f5f5);
    color: inherit;
  }
  .row {
    display: flex;
    align-items: center;
    margin-bottom: 8px;
  }
  .row input {
    flex: 1;
  }
  .row button.remove {
    margin-left: 8px;
    border: none;
    background: none;
    color: #e74c3c;
    font-size: 20px;
    cursor: pointer;
  }
  #addBtn {
    width: 100%;
    padding: 10px;
    margin-top: 4px;
    border: 1px dashed #aaa;
    border-radius: 8px;
    background: none;
    color: inherit;
    font-size: 14px;
    cursor: pointer;
  }
  #errorMsg {
    color: #e74c3c;
    font-size: 13px;
    margin-top: 12px;
    min-height: 16px;
  }
  #submitBtn {
    width: 100%;
    padding: 14px;
    margin-top: 12px;
    border: none;
    border-radius: 8px;
    background: var(--tg-theme-button-color, #2481cc);
    color: var(--tg-theme-button-text-color, #ffffff);
    font-size: 16px;
    font-weight: bold;
    cursor: pointer;
  }
</style>
</head>
<body>
  <h2>📝 신청 정보 입력</h2>

  <label for="hoeName">회</label>
  <input type="text" id="hoeName" placeholder="예: 자장부청" />

  <label for="areaName">지역</label>
  <input type="text" id="areaName" placeholder="예: 강남지역" />

  <label for="teamName">팀</label>
  <input type="text" id="teamName" placeholder="예: 3팀" />

  <label for="districtName">구역</label>
  <input type="text" id="districtName" placeholder="예: 5구역" />

  <label for="repName">인솔자 이름</label>
  <input type="text" id="repName" placeholder="이름 입력" />

  <label for="phone1">연락처</label>
  <div style="display:flex; align-items:center; gap:6px;">
    <input type="tel" inputmode="numeric" pattern="[0-9]*" id="phone1" maxlength="3" placeholder="010" style="text-align:center;" />
    <span>-</span>
    <input type="tel" inputmode="numeric" pattern="[0-9]*" id="phone2" maxlength="4" placeholder="0000" style="text-align:center;" />
    <span>-</span>
    <input type="tel" inputmode="numeric" pattern="[0-9]*" id="phone3" maxlength="4" placeholder="0000" style="text-align:center;" />
  </div>

  <label>같이 갈 구역원 (없으면 비워두세요)</label>
  <div id="rows"></div>
  <button id="addBtn" type="button">➕ 동반자 추가</button>

  <div id="errorMsg"></div>
  <button id="submitBtn" type="button">✅ 제출</button>

<script>
  const tg = window.Telegram.WebApp;
  tg.expand();

  const rowsEl = document.getElementById('rows');
  const errorEl = document.getElementById('errorMsg');

  function addRow(value) {
    const row = document.createElement('div');
    row.className = 'row';
    const input = document.createElement('input');
    input.type = 'text';
    input.placeholder = '동반자 이름';
    input.value = value || '';
    const removeBtn = document.createElement('button');
    removeBtn.className = 'remove';
    removeBtn.type = 'button';
    removeBtn.textContent = '✕';
    removeBtn.onclick = () => row.remove();
    row.appendChild(input);
    row.appendChild(removeBtn);
    rowsEl.appendChild(row);
  }

  document.getElementById('addBtn').onclick = () => addRow('');

  const phonePattern = /^01[016789]-\\d{3,4}-\\d{4}$/;

  document.getElementById('submitBtn').onclick = () => {
    const hoe = document.getElementById('hoeName').value.trim();
    const area = document.getElementById('areaName').value.trim();
    const team = document.getElementById('teamName').value.trim();
    const district = document.getElementById('districtName').value.trim();
    const repName = document.getElementById('repName').value.trim();
    const phone1 = document.getElementById('phone1').value.trim();
    const phone2 = document.getElementById('phone2').value.trim();
    const phone3 = document.getElementById('phone3').value.trim();
    const phone = phone1 + '-' + phone2 + '-' + phone3;
    const inputs = rowsEl.querySelectorAll('input');
    const companions = Array.from(inputs)
      .map(i => i.value.trim())
      .filter(v => v.length > 0);

    if (!hoe || !area || !team || !district) {
      errorEl.textContent = '회, 지역, 팀, 구역을 모두 입력해주세요.';
      return;
    }
    if (!repName) {
      errorEl.textContent = '인솔자 이름을 입력해주세요.';
      return;
    }
    if (!phonePattern.test(phone)) {
      errorEl.textContent = '연락처 형식이 올바르지 않아요. 예: 010-1234-5678';
      return;
    }
    errorEl.textContent = '';

    tg.sendData(JSON.stringify({
      group_name: hoe + ' ' + area + ' ' + team + ' ' + district,
      rep_name: repName,
      phone: phone,
      companions: companions,
    }));
    tg.close();
  };
</script>
</body>
</html>
"""


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/webapp":
            body = _FULL_FORM_WEBAPP_HTML.encode("utf-8")
        elif path == "/webapp/companions":
            body = _COMPANIONS_WEBAPP_HTML.encode("utf-8")
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


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
            ENTER_GROUP: [
                MessageHandler(filters.StatusUpdate.WEB_APP_DATA, form_webapp_received),
                MessageHandler(filters.TEXT & ~filters.COMMAND, group_entered),
            ],
            ENTER_LEADER: [MessageHandler(filters.TEXT & ~filters.COMMAND, leader_entered)],
            ENTER_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, phone_entered)],
            ENTER_COMPANIONS: [
                MessageHandler(filters.StatusUpdate.WEB_APP_DATA, companions_webapp_received),
                MessageHandler(filters.TEXT & ~filters.COMMAND, companions_entered),
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
            MessageHandler(filters.Regex(r"^전체명단$"), admin_full_list),
            MessageHandler(filters.Regex(r"^삭제\s+\d+$"), admin_delete_signup),
            MessageHandler(filters.Regex(r"^참여취소\s+\d+$"), admin_unmark_checkin),
            MessageHandler(
                filters.Regex(r"^수정\s+\d+\s+(회|인솔자|연락처|동반자)\s+.+$"),
                admin_edit_signup,
            ),
            MessageHandler(filters.Regex(r"^제목설정\s+.+$"), admin_set_title),
            MessageHandler(filters.Regex(r"^관리자추가\s+\S+$"), admin_add_admin),
            MessageHandler(filters.Regex(r"^관리자삭제\s+\S+$"), admin_remove_admin),
            MessageHandler(filters.Regex(r"^관리자목록$"), admin_list_admins),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv_handler)

    # 아래 명령어들은 그룹 -1(더 높은 우선순위)로 등록해서, 신청 도중 어떤 단계에
    # 있든 이 명령어들이 항상 먼저 인식되도록 함 (신청 흐름에 삼켜지는 것 방지).
    # 처리 후에는 ApplicationHandlerStop을 발생시켜, 같은 메시지가 conv_handler(그룹 0)에서
    # 또다시 처리되어 이중 실행되는 것을 막는다.
    def _stop_after(func):
        async def wrapper(update, context):
            await func(update, context)
            raise ApplicationHandlerStop

        return wrapper

    app.add_handler(CommandHandler("admin", _stop_after(admin_command)), group=-1)
    app.add_handler(CommandHandler("cancel", _stop_after(cancel_command)), group=-1)
    app.add_handler(MessageHandler(filters.Regex(r"^취소$"), _stop_after(cancel_command)), group=-1)
    app.add_handler(MessageHandler(filters.Regex(r"^명단$"), _stop_after(admin_command)), group=-1)
    app.add_handler(MessageHandler(filters.Regex(r"^관리자$"), _stop_after(admin_command)), group=-1)
    app.add_handler(MessageHandler(filters.Regex(r"^전체명단$"), _stop_after(admin_full_list)), group=-1)
    app.add_handler(MessageHandler(filters.Regex(r"^삭제\s+\d+$"), _stop_after(admin_delete_signup)), group=-1)
    app.add_handler(MessageHandler(filters.Regex(r"^참여취소\s+\d+$"), _stop_after(admin_unmark_checkin)), group=-1)
    app.add_handler(
        MessageHandler(
            filters.Regex(r"^수정\s+\d+\s+(회|인솔자|연락처|동반자)\s+.+$"),
            _stop_after(admin_edit_signup),
        ),
        group=-1,
    )
    app.add_handler(MessageHandler(filters.Regex(r"^제목설정\s+.+$"), _stop_after(admin_set_title)), group=-1)
    app.add_handler(MessageHandler(filters.Regex(r"^관리자추가\s+\S+$"), _stop_after(admin_add_admin)), group=-1)
    app.add_handler(MessageHandler(filters.Regex(r"^관리자삭제\s+\S+$"), _stop_after(admin_remove_admin)), group=-1)
    app.add_handler(MessageHandler(filters.Regex(r"^관리자목록$"), _stop_after(admin_list_admins)), group=-1)
    app.add_handler(CommandHandler("mine", _stop_after(my_signups_command)), group=-1)
    app.add_handler(
        MessageHandler(filters.Regex(r"^내\s*신청(\s*확인)?$"), _stop_after(my_signups_command)),
        group=-1,
    )
    app.add_handler(CallbackQueryHandler(user_cancel_clicked, pattern=r"^usercancel_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_hour_selected, pattern=r"^admin_hour_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_back, pattern=r"^admin_back$"))
    app.add_handler(CallbackQueryHandler(admin_fulllist_clicked, pattern=r"^admin_fulllist$"))
    app.add_handler(CallbackQueryHandler(admin_status_clicked, pattern=r"^admin_status$"))
    app.add_handler(CallbackQueryHandler(checkin_clicked, pattern=r"^checkin_\d+$"))

    logger.info("봇 시작...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
