import os
import sqlite3
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "dragon_tiger.db")

STARTING_POINTS = 200000
ROUND_SECONDS = 45
REVEAL_DELAY_SECONDS = 2

DAILY_REWARD = 10000

# 관리자 지급용: Railway Variables에 ADMIN_ID 넣기 (네 텔레그램 숫자 ID)
# 예: ADMIN_ID=123456789
ADMIN_ID_ENV = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(ADMIN_ID_ENV) if ADMIN_ID_ENV.isdigit() else None

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["A","2","3","4","5","6","7","8","9","10","J","Q","K"]
RANK_VALUE = {r: i + 1 for i, r in enumerate(RANKS)}  # A=1 ... K=13

CHOICES = {"D": "용(Dragon)", "T": "호(Tiger)", "I": "타이(Tie)"}
PAYOUT = {"D": 2.0, "T": 2.0, "I": 9.0}  # 원금 포함 지급 배수

FONT_PATH = os.getenv("CARD_FONT_PATH", "")


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
            ends_at INTEGER NOT NULL,   -- unix ts
            last_result TEXT
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

        row = con.execute("SELECT round_id FROM game_state WHERE id=1").fetchone()
        if not row:
            now = int(datetime.now(tz=timezone.utc).timestamp())
            con.execute(
                "INSERT INTO game_state (id, round_id, phase, ends_at, last_result) VALUES (1, 1, 'BETTING', ?, NULL)",
                (now + ROUND_SECONDS,)
            )

def ensure_user(user_id: int):
    with db() as con:
        row = con.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
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
        exists = con.execute(
            "SELECT 1 FROM bets WHERE round_id=? AND user_id=?",
            (round_id, user_id)
        ).fetchone()
        if exists:
            return "ALREADY"

        row = con.execute("SELECT points FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row or row["points"] < amount:
            return "NO_MONEY"

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
    return Card(secrets.choice(RANKS), secrets.choice(SUITS))

def decide(dragon: Card, tiger: Card) -> str:
    if dragon.value > tiger.value:
        return "D"
    if tiger.value > dragon.value:
        return "T"
    return "I"


# ---------------- IMAGE (PIL) ----------------

def _load_font(size: int):
    if FONT_PATH and os.path.exists(FONT_PATH):
        try:
            return ImageFont.truetype(FONT_PATH, size=size)
        except Exception:
            pass
    return ImageFont.load_default()

def _is_red_suit(suit: str) -> bool:
    return suit in ("♥", "♦")

def render_card_image(card: Card, w: int = 240, h: int = 340) -> Image.Image:
    img = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    d = ImageDraw.Draw(img)

    d.rounded_rectangle([(6, 6), (w - 6, h - 6)], radius=18, outline=(0, 0, 0, 255), width=4)

    color = (200, 0, 0, 255) if _is_red_suit(card.suit) else (0, 0, 0, 255)

    font_big = _load_font(90)
    font_mid = _load_font(52)
    font_small = _load_font(40)

    tl = f"{card.rank}{card.suit}"
    d.text((20, 18), tl, font=font_small, fill=color)

    br = f"{card.suit}{card.rank}"
    br_bbox = d.textbbox((0, 0), br, font=font_small)
    br_w = br_bbox[2] - br_bbox[0]
    br_h = br_bbox[3] - br_bbox[1]
    d.text((w - br_w - 20, h - br_h - 18), br, font=font_small, fill=color)

    center = card.suit
    cb = d.textbbox((0, 0), center, font=font_big)
    cx = (w - (cb[2] - cb[0])) // 2
    cy = (h - (cb[3] - cb[1])) // 2 - 10
    d.text((cx, cy), center, font=font_big, fill=color)

    rb = d.textbbox((0, 0), card.rank, font=font_mid)
    rx = (w - (rb[2] - rb[0])) // 2
    ry = cy + 110
    d.text((rx, ry), card.rank, font=font_mid, fill=color)

    return img

def render_round_image(round_id: int, dragon: Card, tiger: Card, winner: str) -> BytesIO:
    W, H = 900, 520
    canvas = Image.new("RGBA", (W, H), (20, 20, 26, 255))
    d = ImageDraw.Draw(canvas)

    title_font = _load_font(40)
    label_font = _load_font(34)
    small_font = _load_font(28)

    title = f"Round #{round_id}  |  결과: {CHOICES[winner]}"
    d.text((30, 25), title, font=title_font, fill=(255, 255, 255, 255))

    card_d = render_card_image(dragon)
    card_t = render_card_image(tiger)

    d.text((140, 95), "🐉 용", font=label_font, fill=(255, 255, 255, 255))
    d.text((610, 95), "🐅 호", font=label_font, fill=(255, 255, 255, 255))

    canvas.alpha_composite(card_d, (90, 140))
    canvas.alpha_composite(card_t, (560, 140))

    d.text((90, 440), f"용: {dragon.text()}", font=small_font, fill=(220, 220, 220, 255))
    d.text((560, 440), f"호: {tiger.text()}", font=small_font, fill=(220, 220, 220, 255))

    bio = BytesIO()
    bio.name = "dragon_tiger.png"
    canvas.save(bio, format="PNG")
    bio.seek(0)
    return bio


# ---------------- TELEGRAM COMMANDS ----------------

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🐉🐅 용호(Dragon Tiger) 배팅 봇\n\n"
        "• /startgame : 게임 시작(자동 라운드)\n"
        "• /stopgame : 게임 중지(메시지 멈춤)\n"
        "• /bet D 1000 : 용 배팅\n"
        "• /bet T 1000 : 호 배팅\n"
        "• /bet I 1000 : 타이 배팅\n"
        "• /balance : 포인트 확인\n"
        "• /round : 현재 라운드 확인\n"
        "• /daily : 일일보상(+10,000)\n"
        "• /give 유저ID 금액 : (관리자) 포인트 지급\n"
    )
    await update.message.reply_text(msg)

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    await update.message.reply_text(f"💰 현재 포인트: {get_points(user_id):,}")

async def cmd_round(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_state()
    now = int(datetime.now(tz=timezone.utc).timestamp())
    remain = max(0, int(st["ends_at"]) - now)
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
        await update.message.reply_text("사용법: /bet D|T|I 금액  (예: /bet D 1000)")
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

    res = place_bet(int(st["round_id"]), user_id, choice, amount)
    if res == "ALREADY":
        await update.message.reply_text("이미 이번 라운드에 배팅했어! (라운드당 1번)")
        return
    if res == "NO_MONEY":
        await update.message.reply_text(f"잔액 부족! 현재 포인트: {get_points(user_id):,}")
        return

    await update.message.reply_text(
        f"✅ 배팅 완료!\n"
        f"라운드 #{st['round_id']} | {CHOICES[choice]} | {amount:,}\n"
        f"남은 포인트: {get_points(user_id):,}"
    )

async def startgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    context.application.bot_data["game_chat_id"] = chat_id

    st = get_state()
    await update.message.reply_text(
        f"🎮 용호 게임 시작!\n"
        f"현재 라운드 #{st['round_id']} 배팅 진행 중.\n"
        f"/bet D 1000 처럼 배팅해!"
    )

# ✅ A 방식: 말(메시지) 멈추기
async def stopgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["game_chat_id"] = None
    await update.message.reply_text("🛑 게임을 중지했어. 다시 시작하려면 /startgame")

# ✅ 유저가 직접 받는 일일보상
async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)

    add_points(user_id, DAILY_REWARD)
    await update.message.reply_text(
        f"🎁 일일보상 지급!\n+{DAILY_REWARD:,} 포인트\n현재 보유: {get_points(user_id):,}"
    )

# ✅ 관리자 지급: /give 유저ID 금액
async def give(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID is None:
        await update.message.reply_text("⛔ ADMIN_ID가 설정되지 않았어. Railway Variables에 ADMIN_ID 넣어줘.")
        return

    if update.effective_user.id != ADMIN_ID:
        return

    if len(context.args) != 2:
        await update.message.reply_text("사용법: /give 유저ID 금액\n예: /give 123456789 50000")
        return

    try:
        target_id = int(context.args[0])
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("유저ID/금액은 숫자로 입력해.")
        return

    if amount == 0:
        await update.message.reply_text("금액은 0이 될 수 없어.")
        return

    ensure_user(target_id)
    add_points(target_id, amount)

    await update.message.reply_text(f"💰 지급 완료\n대상: {target_id}\n금액: {amount:,}")

async def game_tick(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.application.bot_data.get("game_chat_id")
    if not chat_id:
        return  # ✅ stopgame 치면 여기서 멈춤(더 이상 말 안함)

    st = get_state()
    now = int(datetime.now(tz=timezone.utc).timestamp())

    if st["phase"] == "BETTING" and now < int(st["ends_at"]):
        return

    if st["phase"] == "BETTING":
        set_state(int(st["round_id"]), "CLOSED", now + REVEAL_DELAY_SECONDS, st["last_result"])
        await context.bot.send_message(chat_id, f"⏳ 라운드 #{st['round_id']} 배팅 마감! 곧 오픈!")

    elif st["phase"] == "CLOSED":
        if now < int(st["ends_at"]):
            return

        round_id = int(st["round_id"])
        dragon = draw_card()
        tiger = draw_card()
        winner = decide(dragon, tiger)

        bets = fetch_bets(round_id)
        total_winners = 0
        total_paid = 0

        for b in bets:
            uid = int(b["user_id"])
            choice = b["choice"]
            amt = int(b["amount"])
            if choice == winner:
                payout = int(amt * PAYOUT[winner])
                add_points(uid, payout)
                total_winners += 1
                total_paid += payout

        img_bytes = render_round_image(round_id, dragon, tiger, winner)
        caption = (
            f"🎴 라운드 #{round_id} 오픈!\n"
            f"🏆 결과: {CHOICES[winner]}\n"
            f"✅ 당첨자 수: {total_winners}명 | 지급 합계: {total_paid:,}"
        )
        await context.bot.send_photo(chat_id, photo=img_bytes, caption=caption)

        last_result = f"{dragon.text()} vs {tiger.text()} => {CHOICES[winner]}"
        new_round = round_id + 1
        set_state(new_round, "BETTING", now + ROUND_SECONDS, last_result)

        await context.bot.send_message(
            chat_id,
            f"🎲 다음 라운드 #{new_round} 시작!\n"
            f"{ROUND_SECONDS}초 동안 배팅 가능.\n"
            f"/bet D 1000 (용) | /bet T 1000 (호) | /bet I 1000 (타이)"
        )


def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 환경변수가 없어!")

    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("startgame", startgame))
    app.add_handler(CommandHandler("stopgame", stopgame))
    app.add_handler(CommandHandler("bet", cmd_bet))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("round", cmd_round))
    app.add_handler(CommandHandler("daily", daily))
    app.add_handler(CommandHandler("give", give))

    # 1초마다 tick
    app.job_queue.run_repeating(game_tick, interval=1, first=1)

    app.run_polling()


if __name__ == "__main__":
    main()
