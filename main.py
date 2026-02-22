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
import random
from PIL import Image, ImageDraw, ImageFont, ImageFilter

BASE_FONT = ImageFont.load_default()

def _is_red_suit(suit: str) -> bool:
    return suit in ("♥", "♦")

def _bg(w: int, h: int) -> Image.Image:
    # 깔끔한 어두운 그라데이션 배경 + 아주 은은한 점 패턴
    top = (13, 16, 26, 255)
    bot = (8, 10, 18, 255)

    base = Image.new("RGBA", (w, h), top)
    overlay = Image.new("RGBA", (w, h), bot)
    mask = Image.new("L", (w, h))
    md = ImageDraw.Draw(mask)
    for y in range(h):
        md.line([(0, y), (w, y)], fill=int(255 * (y / max(1, h - 1))))
    base.paste(overlay, (0, 0), mask)

    dots = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    dd = ImageDraw.Draw(dots)
    for y in range(0, h, 18):
        for x in range(0, w, 18):
            a = random.randint(0, 18)
            dd.ellipse([x, y, x + 2, y + 2], fill=(255, 255, 255, a))
    base.alpha_composite(dots)
    return base

def _shadow_box(w: int, h: int, radius=20, alpha=140) -> Image.Image:
    sh = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sh)
    sd.rounded_rectangle([0, 0, w, h], radius=26, fill=(0, 0, 0, alpha))
    return sh.filter(ImageFilter.GaussianBlur(radius))

def _draw_text_smooth(img: Image.Image, x: int, y: int, text: str, scale: int, fill):
    """
    ✅ NEAREST 확대 대신:
    1) 작은 글씨를 큰 캔버스에 찍고
    2) LANCZOS로 '부드럽게' 키움
    """
    tmp = Image.new("RGBA", (420, 130), (0, 0, 0, 0))
    d = ImageDraw.Draw(tmp)

    # 얇은 외곽선(가독성)
    for ox, oy in [(-1,0),(1,0),(0,-1),(0,1)]:
        d.text((2+ox, 2+oy), text, font=BASE_FONT, fill=(0,0,0,220))
    d.text((2, 2), text, font=BASE_FONT, fill=fill)

    # 부드러운 확대
    tmp = tmp.resize((tmp.size[0]*scale, tmp.size[1]*scale), resample=Image.LANCZOS)
    img.alpha_composite(tmp, (x, y))

def draw_suit_shape(d: ImageDraw.ImageDraw, cx: int, cy: int, suit: str, size: int = 38):
    red = suit in ("♥", "♦")
    color = (220, 60, 60, 255) if red else (20, 20, 20, 255)
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

def render_card_image(card, w: int = 250, h: int = 350) -> Image.Image:
    """
    카드도 '깔끔'하게:
    - 하얀 카드 + 얇은 테두리 + 은은한 하이라이트 + 그림자용 여백
    """
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 카드 본체
    d.rounded_rectangle([6, 6, w-6, h-6], radius=22, fill=(250, 250, 252, 255), outline=(40, 40, 48, 255), width=4)

    # 상단 하이라이트
    hi = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hi)
    hd.rounded_rectangle([10, 10, w-10, h//2], radius=18, fill=(255, 255, 255, 40))
    img.alpha_composite(hi)

    color = (220, 60, 60, 255) if _is_red_suit(card.suit) else (20, 20, 20, 255)

    # 좌상단 랭크(작게, 깔끔)
    _draw_text_smooth(img, 18, 14, card.rank, scale=7, fill=color)
    draw_suit_shape(d, 44, 92, card.suit, size=18)

    # 중앙 무늬(큰 포인트)
    draw_suit_shape(d, w//2, h//2 - 10, card.suit, size=52)

    return img

def _glow_border(size_wh, rect_xyxy, color, blur=18, alpha=160):
    """
    승자쪽만 은은하게 글로우(네온을 과하지 않게)
    """
    W, H = size_wh
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    x0,y0,x1,y1 = rect_xyxy

    # 부드러운 글로우
    g = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(g)
    gd.rounded_rectangle([x0, y0, x1, y1], radius=28, outline=(color[0], color[1], color[2], alpha), width=18)
    g = g.filter(ImageFilter.GaussianBlur(blur))
    layer.alpha_composite(g)

    # 얇은 선명 테두리
    ld.rounded_rectangle([x0, y0, x1, y1], radius=28, outline=(color[0], color[1], color[2], 220), width=4)
    return layer

def render_round_image(round_id: int, dragon, tiger, winner: str) -> BytesIO:
    """
    ✅ 깔끔한 레이아웃:
    - 상단: Round + WINNER 한 줄로 정리
    - 중앙: 카드 2장 + 승자 글로우
    - 하단: 카드 값(용/호)만 심플하게
    """
    # 슈퍼샘플링(3배로 그린 후 다운스케일) -> 훨씬 깔끔해짐
    SS = 3
    W, H = 900*SS, 520*SS
    canvas = _bg(W, H)

    # 상단 바
    topbar = Image.new("RGBA", (W, 120*SS), (0, 0, 0, 0))
    td = ImageDraw.Draw(topbar)
    td.rounded_rectangle([20*SS, 18*SS, W-20*SS, 110*SS], radius=26*SS, fill=(0, 0, 0, 90))
    canvas.alpha_composite(topbar)

    # 텍스트 한 줄로
    if winner == "D":
        wcol = (110, 190, 255, 255)
    elif winner == "T":
        wcol = (255, 140, 160, 255)
    else:
        wcol = (255, 210, 120, 255)

    _draw_text_smooth(canvas, 40*SS, 28*SS, f"Round #{round_id}", scale=6, fill=(255,255,255,255))
    _draw_text_smooth(canvas, 360*SS, 28*SS, f"WINNER: {CHOICES[winner]}", scale=6, fill=wcol)

    # 카드 위치(여백 넉넉히)
    card_w, card_h = 250*SS, 350*SS
    d_pos = (120*SS, 150*SS)
    t_pos = (530*SS, 150*SS)

    # 카드 그림자
    sh = _shadow_box(card_w+18*SS, card_h+18*SS, radius=28*SS, alpha=110)
    canvas.alpha_composite(sh, (d_pos[0]-6*SS, d_pos[1]-6*SS))
    canvas.alpha_composite(sh, (t_pos[0]-6*SS, t_pos[1]-6*SS))

    # 카드 렌더(카드 함수는 기본 사이즈라서 SS 반영해서 크게)
    cd = render_card_image(dragon, w=250*SS, h=350*SS)
    ct = render_card_image(tiger,  w=250*SS, h=350*SS)
    canvas.alpha_composite(cd, d_pos)
    canvas.alpha_composite(ct, t_pos)

    # 라벨(간단)
    _draw_text_smooth(canvas, 170*SS, 118*SS, "DRAGON", scale=5, fill=(110,190,255,255))
    _draw_text_smooth(canvas, 600*SS, 118*SS, "TIGER",  scale=5, fill=(255,140,160,255))

    # 승자 글로우(과하지 않게)
    pad = 14*SS
    d_box = (d_pos[0]-pad, d_pos[1]-pad, d_pos[0]+card_w+pad, d_pos[1]+card_h+pad)
    t_box = (t_pos[0]-pad, t_pos[1]-pad, t_pos[0]+card_w+pad, t_pos[1]+card_h+pad)
    if winner == "D":
        canvas.alpha_composite(_glow_border((W,H), d_box, (110,190,255)))
    elif winner == "T":
        canvas.alpha_composite(_glow_border((W,H), t_box, (255,140,160)))
    else:
        canvas.alpha_composite(_glow_border((W,H), d_box, (255,210,120), alpha=120))
        canvas.alpha_composite(_glow_border((W,H), t_box, (255,210,120), alpha=120))

    # 하단 정보(심플하게)
    bottom = Image.new("RGBA", (W, 90*SS), (0,0,0,0))
    bd = ImageDraw.Draw(bottom)
    bd.rounded_rectangle([20*SS, 0, W-20*SS, 85*SS], radius=22*SS, fill=(0,0,0,90))
    canvas.alpha_composite(bottom, (0, 420*SS))

    _draw_text_smooth(canvas, 50*SS, 435*SS, f"🐉 용: {dragon.text()}", scale=6, fill=(235,235,240,255))
    _draw_text_smooth(canvas, 520*SS, 435*SS, f"🐅 호: {tiger.text()}", scale=6, fill=(235,235,240,255))

    # ✅ 최종 다운스케일(핵심)
    final_img = canvas.resize((900, 520), resample=Image.LANCZOS)

    bio = BytesIO()
    bio.name = "dragon_tiger.png"
    final_img.save(bio, format="PNG")
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
