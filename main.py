import os
import sqlite3
import random
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    Application, CommandHandler, ContextTypes
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_PATH = "dragon_tiger.db"

STARTING_POINTS = 200000
ROUND_SECONDS = 45          # 배팅 가능한 시간(초)
REVEAL_DELAY_SECONDS = 3    # 마감 후 오픈까지 딜레이


SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["A","2","3","4","5","6","7","8","9","10","J","Q","K"]
RANK_VALUE = {r:i+1 for i, r in enumerate(RANKS)}  # A=1 ... K=13

CHOICES = {"D": "용(Dragon)", "T": "호(Tiger)", "I": "타이(Tie)"}
# 배당(원하면 카지노처럼 수수료/커미션 붙일 수 있음)
PAYOUT = {"D": 2.0, "T": 2.0, "I": 9.0}  # 승리 시 (원금 포함) 지급 배수


# ---------------- DB ----------------

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            points INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS game_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            round_id INTEGER NOT NULL,
            phase TEXT NOT NULL,        -- BETTING | CLOSED
            ends_at INTEGER NOT NULL,   -- unix ts (seconds)
            last_result TEXT            -- json-ish string for display
        )
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            round_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            choice TEXT NOT NULL,       -- D/T/I
            amount INTEGER NOT NULL,
            placed_at INTEGER NOT NULL,
            PRIMARY KEY (round_id, user_id)
        )
        """)
        # seed game_state
        cur = con.execute("SELECT round_id FROM game_state WHERE id=1")
        row = cur.fetchone()
        if not row:
            now = int(datetime.now(tz=timezone.utc).timestamp())
            con.execute(
                "INSERT INTO game_state (id, round_id, phase, ends_at, last_result) VALUES (1, 1, 'BETTING', ?, NULL)",
                (now + ROUND_SECONDS,)
            )

def ensure_user(user_id: int):
    with db() as con:
        row = con.execute("SELECT user_id, points FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            con.execute(
                "INSERT INTO users (user_id, points, created_at) VALUES (?, ?, ?)",
                (user_id, STARTING_POINTS, datetime.now(tz=timezone.utc).isoformat())
            )

def get_points(user_id: int) -> int:
    with db() as con:
        row = con.execute("SELECT points FROM users WHERE user_id=?", (user_id,)).fetchone()
        return int(row["points"]) if row else 0

def add_points(user_id: int, delta: int):
    with db() as con:
        con.execute("UPDATE users SET points = points + ? WHERE user_id=?", (delta, user_id))

def get_state():
    with db() as con:
        return con.execute("SELECT round_id, phase, ends_at, last_result FROM game_state WHERE id=1").fetchone()

def set_state(round_id: int, phase: str, ends_at: int, last_result: str | None):
    with db() as con:
        con.execute(
            "UPDATE game_state SET round_id=?, phase=?, ends_at=?, last_result=? WHERE id=1",
            (round_id, phase, ends_at, last_result)
        )

def place_bet(round_id: int, user_id: int, choice: str, amount: int) -> str:
    with db() as con:
        # 이미 배팅했는지
        exists = con.execute(
            "SELECT 1 FROM bets WHERE round_id=? AND user_id=?",
            (round_id, user_id)
        ).fetchone()
        if exists:
            return "ALREADY"

        # 포인트 충분한지
        points = con.execute("SELECT points FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not points or points["points"] < amount:
            return "NO_MONEY"

        # 차감 + 기록
        con.execute("UPDATE users SET points = points - ? WHERE user_id=?", (amount, user_id))
        con.execute(
            "INSERT INTO bets (round_id, user_id, choice, amount, placed_at) VALUES (?, ?, ?, ?, ?)",
            (round_id, user_id, choice, amount, int(datetime.now(tz=timezone.utc).timestamp()))
        )
        return "OK"

def fetch_bets(round_id: int):
    with db() as con:
        return con.execute(
            "SELECT user_id, choice, amount FROM bets WHERE round_id=?",
            (round_id,)
        ).fetchall()


# ---------------- GAME LOGIC ----------------

@dataclass(frozen=True)
class Card:
    rank: str
    suit: str
    @property
    def value(self) -> int:
        return RANK_VALUE[self.rank]
    def text(self) -> str:
        return f"{self.rank}{self.suit}"

def draw_card() -> Card:
    # random보다 더 좋은 엔트로피를 원하면 secrets 사용
    r = secrets.choice(RANKS)
    s = secrets.choice(SUITS)
    return Card(r, s)

def decide(dragon: Card, tiger: Card) -> str:
    if dragon.value > tiger.value:
        return "D"
    if tiger.value > dragon.value:
        return "T"
    return "I"


# ---------------- TELEGRAM ----------------

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🐉🐅 용호(Dragon Tiger) 배팅 봇\n\n"
        "명령어:\n"
        "• /startgame : 게임 시작(자동 라운드)\n"
        "• /bet D 1000 : 용(Dragon)에 1000 배팅\n"
        "• /bet T 1000 : 호(Tiger)에 1000 배팅\n"
        "• /bet I 1000 : 타이(Tie)에 1000 배팅\n"
        "• /balance : 내 포인트 확인\n"
        "• /round : 현재 라운드 상태\n\n"
        "예시: /bet D 5000"
    )
    await update.message.reply_text(msg)

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    pts = get_points(user_id)
    await update.message.reply_text(f"💰 현재 포인트: {pts:,}")

async def cmd_round(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_state()
    now = int(datetime.now(tz=timezone.utc).timestamp())
    remain = max(0, st["ends_at"] - now)
    last = st["last_result"] or "없음"
    await update.message.reply_text(
        f"🎲 라운드 #{st['round_id']}\n"
        f"상태: {st['phase']}\n"
        f"마감까지: {remain}s\n"
        f"최근 결과: {last}"
    )

async def cmd_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)

    st = get_state()
    if st["phase"] != "BETTING":
        await update.message.reply_text("⛔ 지금은 배팅 시간이 아니야. 다음 라운드를 기다려줘!")
        return

    if len(context.args) != 2:
        await update.message.reply_text("사용법: /bet D|T|I 금액\n예: /bet D 1000")
        return

    choice = context.args[0].upper().strip()
    if choice not in CHOICES:
        await update.message.reply_text("선택은 D(용) / T(호) / I(타이) 중 하나야.")
        return

    try:
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("금액은 숫자로 입력해줘. 예: /bet D 1000")
        return

    if amount <= 0:
        await update.message.reply_text("금액은 1 이상이어야 해.")
        return

    res = place_bet(st["round_id"], user_id, choice, amount)
    if res == "ALREADY":
        await update.message.reply_text("이미 이번 라운드에 배팅했어! (라운드당 1번)")
        return
    if res == "NO_MONEY":
        pts = get_points(user_id)
        await update.message.reply_text(f"잔액 부족! 현재 포인트: {pts:,}")
        return

    pts = get_points(user_id)
    await update.message.reply_text(
        f"✅ 배팅 완료!\n"
        f"라운드 #{st['round_id']} | {CHOICES[choice]} | {amount:,}\n"
        f"남은 포인트: {pts:,}"
    )

async def startgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 그룹/개인 어디서나 가능. (원하면 그룹에서만 제한 가능)
    chat_id = update.effective_chat.id
    context.application.bot_data["game_chat_id"] = chat_id

    # 즉시 상태 메시지
    st = get_state()
    await update.message.reply_text(
        f"🎮 용호 게임 시작!\n"
        f"현재 라운드 #{st['round_id']} 배팅 진행 중.\n"
        f"/bet D 1000 처럼 배팅해!"
    )

async def game_tick(context: ContextTypes.DEFAULT_TYPE):
    """주기적으로 라운드를 진행하는 JobQueue 콜백"""
    chat_id = context.application.bot_data.get("game_chat_id")
    if not chat_id:
        return  # 시작된 채팅이 없으면 아무것도 안함

    st = get_state()
    now = int(datetime.now(tz=timezone.utc).timestamp())

    # 배팅 마감 시간이 안됐으면 스킵
    if st["phase"] == "BETTING" and now < st["ends_at"]:
        return

    # 배팅 마감 -> CLOSED -> 오픈/정산 -> 다음 라운드
    if st["phase"] == "BETTING":
        # 마감 공지
        set_state(st["round_id"], "CLOSED", now + REVEAL_DELAY_SECONDS, st["last_result"])
        await context.bot.send_message(chat_id, f"⏳ 라운드 #{st['round_id']} 배팅 마감! 곧 오픈!")

    elif st["phase"] == "CLOSED":
        # 오픈 시점이 안됐으면 스킵
        if now < st["ends_at"]:
            return

        # 카드 오픈
        dragon = draw_card()
        tiger = draw_card()
        winner = decide(dragon, tiger)

        # 정산
        bets = fetch_bets(st["round_id"])
        total_winners = 0
        total_paid = 0

        for b in bets:
            uid = int(b["user_id"])
            choice = b["choice"]
            amt = int(b["amount"])

            if choice == winner:
                payout = int(amt * PAYOUT[winner])  # 원금 포함 지급
                add_points(uid, payout)
                total_winners += 1
                total_paid += payout

        result_text = (
            f"🐉 용: {dragon.text()}  vs  🐅 호: {tiger.text()}\n"
            f"🏆 결과: {CHOICES[winner]}\n"
            f"✅ 당첨자 수: {total_winners}명 | 지급 합계: {total_paid:,}"
        )

        # 결과 저장
        last_result = f"{dragon.text()} vs {tiger.text()} => {CHOICES[winner]}"
        await context.bot.send_message(chat_id, f"🎴 라운드 #{st['round_id']} 오픈!\n\n{result_text}")

        # 다음 라운드 시작
        new_round = int(st["round_id"]) + 1
        set_state(new_round, "BETTING", now + ROUND_SECONDS, last_result)

        await context.bot.send_message(
            chat_id,
            f"🎲 다음 라운드 #{new_round} 시작!\n"
            f"{ROUND_SECONDS}초 동안 배팅 가능.\n"
            f"/bet D 1000  (용) /bet T 1000 (호) /bet I 1000 (타이)"
        )


def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 환경변수가 없어!")

    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("startgame", startgame))
    app.add_handler(CommandHandler("bet", cmd_bet))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("round", cmd_round))

    # 1초마다 tick 체크(가볍게 상태만 확인)
    app.job_queue.run_repeating(game_tick, interval=1, first=1)

    app.run_polling()


if __name__ == "__main__":
    main()
