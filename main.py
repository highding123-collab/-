import os
import sqlite3
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont, ImageFilter
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "dragon_tiger.db")

STARTING_POINTS = 200000
ROUND_SECONDS = 45
REVEAL_DELAY_SECONDS = 2

DAILY_REWARD = 10000

# 관리자 지급용: Railway Variables에 ADMIN_ID 넣기 (네 텔레그램 숫자 ID)
ADMIN_ID_ENV = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(ADMIN_ID_ENV) if ADMIN_ID_ENV.isdigit() else None

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
RANK_VALUE = {r: i + 1 for i, r in enumerate(RANKS)}  # A=1 ... K=13

CHOICES = {"D": "용(Dragon)", "T": "호(Tiger)", "I": "타이(Tie)"}
PAYOUT = {"D": 2.0, "T": 2.0, "I": 9.0}  # 원금 포함 지급 배수

FONT_PATH = os.getenv("CARD_FONT_PATH", "")

# ================== DB ==================

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

# ================== GAME LOGIC ==================

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

# ================== IMAGE (PIL) ==================

BASE_FONT = ImageFont.load_default()

def _is_red_suit(suit: str) -> bool:
    return suit in ("♥", "♦")

def draw_big_text(img: Image.Image, x: int, y: int, text: str, scale: int = 8, fill=(0, 0, 0, 255)):
    tmp = Image.new("RGBA", (300, 100), (0, 0, 0, 0))
    d = ImageDraw.Draw(tmp)
    d.text((0, 0), text, font=BASE_FONT, fill=fill)
    tmp = tmp.resize((tmp.size[0] * scale, tmp.size[1] * scale), resample=Image.NEAREST)
    img.alpha_composite(tmp, (x, y))

def draw_suit_shape(d: ImageDraw.ImageDraw, cx: int, cy: int, suit: str, size: int = 38):
    red = suit in ("♥", "♦")
    color = (200, 0, 0, 255) if red else (0, 0, 0, 255)
    s = size

    if suit == "♦":
        pts = [(cx, cy - s), (cx + s, cy), (cx, cy + s), (cx - s, cy)]
        d.polygon(pts, fill=color)
    elif suit == "♥":
        d.ellipse([cx - s, cy - s, cx, cy], fill=color)
        d.ellipse([cx, cy - s, cx + s, cy], fill=color)
        d.polygon([(cx - s - 2, cy - 2), (cx + s + 2, cy - 2), (cx, cy + s + 6)], fill=color)
    elif suit == "♣":
        d.ellipse([cx - s // 2, cy - s - 6, cx + s // 2, cy - 6], fill=color)
        d.ellipse([cx - s, cy - s // 3, cx, cy + s // 2], fill=color)
        d.ellipse([cx, cy - s // 3, cx + s, cy + s // 2], fill=color)
        d.polygon([(cx - 8, cy + s // 2), (cx + 8, cy + s // 2), (cx, cy + s + 14)], fill=color)
    elif suit == "♠":
        d.ellipse([cx - s, cy, cx, cy + s], fill=color)
        d.ellipse([cx, cy, cx + s, cy + s], fill=color)
        d.polygon([(cx - s - 2, cy + 6), (cx + s + 2, cy + 6), (cx, cy - s - 10)], fill=color)
        d.polygon([(cx - 8, cy + s), (cx + 8, cy + s), (cx, cy + s + 22)], fill=color)

def render_card_image(card: Card, w: int = 260, h: int = 360) -> Image.Image:
    img = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    d = ImageDraw.Draw(img)

    d.rounded_rectangle([(8, 8), (w - 8, h - 8)], radius=22, outline=(0, 0, 0, 255), width=6)
    color = (200, 0, 0, 255) if _is_red_suit(card.suit) else (0, 0, 0, 255)

    draw_big_text(img, 18, 14, card.rank, scale=10, fill=color)
    draw_suit_shape(d, 55, 120, card.suit, size=24)

    draw_suit_shape(d, w // 2, h // 2 - 10, card.suit, size=52)
    draw_big_text(img, w // 2 - 70, h // 2 + 90, card.rank, scale=10, fill=color)

    draw_suit_shape(d, w - 55, h - 120, card.suit, size=24)
    draw_big_text(img, w - 150, h - 110, card.rank, scale=7, fill=color)
    return img

def _neon_rect_overlay(size_wh, rect_xyxy, color_rgba, blur_radius=16, glow_layers=2):
    W, H = size_wh
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    x0, y0, x1, y1 = rect_xyxy
    od.rounded_rectangle([x0, y0, x1, y1], radius=26, outline=color_rgba, width=6)

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for i in range(glow_layers):
        width = 14 + i * 6
        alpha = max(40, color_rgba[3] - i * 60)
        gd.rounded_rectangle([x0, y0, x1, y1], radius=26,
                             outline=(color_rgba[0], color_rgba[1], color_rgba[2], alpha),
                             width=width)

    glow = glow.filter(ImageFilter.GaussianBlur(blur_radius))
    overlay.alpha_composite(glow)
    return overlay

def render_round_image(round_id: int, dragon: Card, tiger: Card, winner: str) -> BytesIO:
    W, H = 900, 520
    canvas = Image.new("RGBA", (W, H), (20, 20, 26, 255))

    draw_big_text(canvas, 28, 18, f"Round #{round_id}", scale=6, fill=(255, 255, 255, 255))

    if winner == "D":
        wcol = (120, 190, 255, 255)
    elif winner == "T":
        wcol = (255, 140, 160, 255)
    else:
        wcol = (255, 210, 120, 255)

    draw_big_text(canvas, 28, 70, f"WINNER: {CHOICES[winner]}", scale=5, fill=wcol)
    draw_big_text(canvas, 125, 120, "DRAGON", scale=5, fill=(120, 190, 255, 255))
    draw_big_text(canvas, 615, 120, "TIGER",  scale=5, fill=(255, 140, 160, 255))

    d_pos = (90, 165)
    t_pos = (560, 165)
    card_w, card_h = 260, 360

    canvas.alpha_composite(render_card_image(dragon, card_w, card_h), d_pos)
    canvas.alpha_composite(render_card_image(tiger, card_w, card_h), t_pos)

    pad = 10
    d_box = (d_pos[0] - pad, d_pos[1] - pad, d_pos[0] + card_w + pad, d_pos[1] + card_h + pad)
    t_box = (t_pos[0] - pad, t_pos[1] - pad, t_pos[0] + card_w + pad, t_pos[1] + card_h + pad)

    if winner == "D":
        canvas.alpha_composite(_neon_rect_overlay((W, H), d_box, (120, 190, 255, 220)))
    elif winner == "T":
        canvas.alpha_composite(_neon_rect_overlay((W, H), t_box, (255, 140, 160, 220)))
    else:
        canvas.alpha_composite(_neon_rect_overlay((W, H), d_box, (255, 210, 120, 160), glow_layers=1))
        canvas.alpha_composite(_neon_rect_overlay((W, H), t_box, (255, 210, 120, 160), glow_layers=1))

    if winner == "D":
        d_tag, t_tag = "✅ WIN", "❌ LOSE"
    elif winner == "T":
        d_tag, t_tag = "❌ LOSE", "✅ WIN"
    else:
        d_tag, t_tag = "🤝 TIE", "🤝 TIE"

    draw_big_text(canvas, 120, 405, d_tag, scale=5, fill=(220, 220, 220, 255))
    draw_big_text(canvas, 600, 405, t_tag, scale=5, fill=(220, 220, 220, 255))

    draw_big_text(canvas, 90, 455, f"용: {dragon.rank}{dragon.suit}", scale=5, fill=(220, 220, 220, 255))
    draw_big_text(canvas, 560, 455, f"호: {tiger.rank}{tiger.suit}", scale=5, fill=(220, 220, 220, 255))

    bio = BytesIO()
    bio.name = "dragon_tiger.png"
    canvas.save(bio, format="PNG")
    bio.seek(0)
    return bio

# ================== COMMAND IMPLEMENTATIONS (dot-commands) ==================

async def send_help(update: Update):
    msg = (
        "🐉🐅 용호(Dragon Tiger) 배팅 봇 (점(.) 명령어)\n\n"
        "• .startgame : 게임 시작(자동 라운드)\n"
        "• .stopgame : 게임 중지(메시지 멈춤)\n"
        "• .bet D 1000 : 용 배팅\n"
        "• .bet T 1000 : 호 배팅\n"
        "• .bet I 1000 : 타이 배팅\n"
        "• .balance : 포인트 확인\n"
        "• .round : 현재 라운드 확인\n"
        "• .daily : 일일보상(+10,000)\n"
        "• .give 유저ID 금액 : (관리자) 포인트 지급\n"
    )
    await update.message.reply_text(msg)

async def handle_startgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    context.application.bot_data["game_chat_id"] = chat_id
    st = get_state()
    await update.message.reply_text(
        f"🎮 용호 게임 시작!\n"
        f"현재 라운드 #{st['round_id']} 배팅 진행 중.\n"
        f".bet D 1000 처럼 배팅해!"
    )

async def handle_stopgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["game_chat_id"] = None
    await update.message.reply_text("🛑 게임을 중지했어. 다시 시작하려면 .startgame")

async def handle_balance(update: Update):
    user_id = update.effective_user.id
    ensure_user(user_id)
    await update.message.reply_text(f"💰 현재 포인트: {get_points(user_id):,}")

async def handle_round(update: Update):
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

async def handle_daily(update: Update):
    user_id = update.effective_user.id
    ensure_user(user_id)
    add_points(user_id, DAILY_REWARD)
    await update.message.reply_text(
        f"🎁 일일보상 지급!\n+{DAILY_REWARD:,} 포인트\n현재 보유: {get_points(user_id):,}"
    )

async def handle_give(update: Update, args: list[str]):
    if ADMIN_ID is None:
        await update.message.reply_text("⛔ ADMIN_ID가 설정되지 않았어. Railway Variables에 ADMIN_ID 넣어줘.")
        return
    if update.effective_user.id != ADMIN_ID:
        return
    if len(args) != 2:
        await update.message.reply_text("사용법: .give 유저ID 금액\n예: .give 123456789 50000")
        return
    try:
        target_id = int(args[0])
        amount = int(args[1])
    except ValueError:
        await update.message.reply_text("유저ID/금액은 숫자로 입력해.")
        return
    if amount == 0:
        await update.message.reply_text("금액은 0이 될 수 없어.")
        return
    ensure_user(target_id)
    add_points(target_id, amount)
    await update.message.reply_text(f"💰 지급 완료\n대상: {target_id}\n금액: {amount:,}")

async def handle_bet(update: Update, args: list[str]):
    user_id = update.effective_user.id
    ensure_user(user_id)

    st = get_state()
    if st["phase"] != "BETTING":
        await update.message.reply_text("⛔ 지금은 배팅 시간이 아니야. 다음 라운드를 기다려줘!")
        return

    if len(args) != 2:
        await update.message.reply_text("사용법: .bet D|T|I 금액  (예: .bet D 1000)")
        return

    choice = args[0].upper().strip()
    if choice not in CHOICES:
        await update.message.reply_text("선택은 D(용) / T(호) / I(타이) 중 하나야.")
        return

    try:
        amount = int(args[1])
    except ValueError:
        await update.message.reply_text("금액은 숫자로 입력해줘. 예: .bet D 1000")
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

# ================== GAME TICK ==================

async def game_tick(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.application.bot_data.get("game_chat_id")
    if not chat_id:
        return  # stopgame 치면 멈춤

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
            f"🐉 용: {dragon.text()}\n"
            f"🐅 호: {tiger.text()}\n"
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
            f".bet D 1000 (용) | .bet T 1000 (호) | .bet I 1000 (타이)"
        )

# ================== DOT COMMAND ROUTER ==================

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    if not text.startswith("."):
        return

    parts = text.split()
    cmd = parts[0].lower()
    args = parts[1:]

    # 점 명령어들
    if cmd == ".help":
        await send_help(update)
    elif cmd == ".startgame":
        await handle_startgame(update, context)
    elif cmd == ".stopgame":
        await handle_stopgame(update, context)
    elif cmd == ".balance":
        await handle_balance(update)
    elif cmd == ".round":
        await handle_round(update)
    elif cmd == ".daily":
        await handle_daily(update)
    elif cmd == ".give":
        await handle_give(update, args)
    elif cmd == ".bet":
        await handle_bet(update, args)
    else:
        await update.message.reply_text("알 수 없는 명령어야. .help 를 쳐봐")

# ================== MAIN ==================

def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 환경변수가 없어!")

    init_db()

    app = Application.builder().token(TOKEN).build()

    # 점 명령어 라우터
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    # 1초마다 tick
    app.job_queue.run_repeating(game_tick, interval=1, first=1)

    app.run_polling()

if __name__ == "__main__":
    main()
