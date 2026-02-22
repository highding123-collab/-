import os
import sqlite3
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont, ImageFilter
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

# ================== CONFIG ==================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "dragon_tiger.db")

STARTING_POINTS = 200_000
ROUND_SECONDS = 45
REVEAL_DELAY_SECONDS = 2

DAILY_REWARD = 10_000

ADMIN_ID_ENV = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(ADMIN_ID_ENV) if ADMIN_ID_ENV.isdigit() else None

# Dragon / Tiger / Tie
CHOICES = {"D": "용(Dragon)", "T": "호(Tiger)", "I": "타이(Tie)"}
PAYOUT = {"D": 2.0, "T": 2.0, "I": 9.0}  # 원금 포함 배수

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
RANK_VALUE = {r: i + 1 for i, r in enumerate(RANKS)}  # A=1 ... K=13

BASE_FONT = ImageFont.load_default()

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
        )""")

        con.execute("""
        CREATE TABLE IF NOT EXISTS game_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            round_id INTEGER NOT NULL,
            phase TEXT NOT NULL,        -- BETTING | CLOSED
            ends_at INTEGER NOT NULL,   -- unix ts
            last_result TEXT
        )""")

        con.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            round_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            choice TEXT NOT NULL,       -- D/T/I
            amount INTEGER NOT NULL,
            placed_at INTEGER NOT NULL,
            PRIMARY KEY (round_id, user_id)
        )""")

        # ✅ 그림장용 히스토리
        con.execute("""
        CREATE TABLE IF NOT EXISTS road_history (
            round_id INTEGER PRIMARY KEY,
            result TEXT NOT NULL,       -- D/T/I
            dragon TEXT NOT NULL,       -- e.g. "9♥"
            tiger TEXT NOT NULL,        -- e.g. "10♠"
            created_at INTEGER NOT NULL
        )""")

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
        if not row or int(row["points"]) < amount:
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

def clear_bets(round_id: int):
    with db() as con:
        con.execute("DELETE FROM bets WHERE round_id=?", (round_id,))

def insert_road(round_id: int, result: str, dragon_txt: str, tiger_txt: str):
    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO road_history(round_id, result, dragon, tiger, created_at) VALUES(?,?,?,?,?)",
            (round_id, result, dragon_txt, tiger_txt, int(datetime.now(tz=timezone.utc).timestamp()))
        )

def fetch_road(limit: int = 200):
    with db() as con:
        rows = con.execute(
            "SELECT round_id, result, dragon, tiger FROM road_history ORDER BY round_id DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return list(reversed(rows))

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

# ================== IMAGE: Broadcast Road Board ==================

def _draw_text(img: Image.Image, x: int, y: int, text: str, scale: int, fill):
    # 작은 글씨 -> LANCZOS 확대 (NEAREST 금지: 픽셀 깨짐 방지)
    tmp = Image.new("RGBA", (520, 140), (0, 0, 0, 0))
    d = ImageDraw.Draw(tmp)
    # 얇은 외곽선
    for ox, oy in [(-1,0),(1,0),(0,-1),(0,1)]:
        d.text((2+ox, 2+oy), text, font=BASE_FONT, fill=(0,0,0,200))
    d.text((2, 2), text, font=BASE_FONT, fill=fill)
    tmp = tmp.resize((tmp.size[0]*scale, tmp.size[1]*scale), resample=Image.LANCZOS)
    img.alpha_composite(tmp, (x, y))

def _bg_gradient(w: int, h: int) -> Image.Image:
    # 방송용 어두운 그라데이션 + 아주 약한 비네팅
    top = (10, 12, 22, 255)
    bot = (6, 7, 14, 255)
    base = Image.new("RGBA", (w, h), top)
    overlay = Image.new("RGBA", (w, h), bot)

    mask = Image.new("L", (w, h))
    md = ImageDraw.Draw(mask)
    for y in range(h):
        md.line([(0, y), (w, y)], fill=int(255 * (y / max(1, h-1))))
    base.paste(overlay, (0, 0), mask)

    # vignette
    v = Image.new("L", (w, h), 0)
    vd = ImageDraw.Draw(v)
    vd.ellipse([-w*0.2, -h*0.2, w*1.2, h*1.2], fill=255)
    v = v.filter(ImageFilter.GaussianBlur(60))
    shade = Image.new("RGBA", (w, h), (0, 0, 0, 120))
    base = Image.composite(base, Image.alpha_composite(base, shade), v)
    return base

def _glow_circle(layer: Image.Image, cx: int, cy: int, r: int, rgb, glow: int = 18):
    # 바깥 글로우
    g = Image.new("RGBA", layer.size, (0,0,0,0))
    gd = ImageDraw.Draw(g)
    gd.ellipse([cx-r, cy-r, cx+r, cy+r], outline=(*rgb, 160), width=10)
    g = g.filter(ImageFilter.GaussianBlur(glow))
    layer.alpha_composite(g)

def _draw_token(img: Image.Image, x: int, y: int, result: str, highlight: bool = False, tie_marks: int = 0):
    """
    방송용 토큰:
    - D = 빨강, T = 파랑
    - tie_marks > 0 이면 토큰 안에 초록 슬래시 표시
    """
    d = ImageDraw.Draw(img)
    r = 16
    cx, cy = x, y

    if result == "D":
        rgb = (255, 55, 90)
    else:
        rgb = (70, 145, 255)

    # 그림자
    d.ellipse([cx-r+2, cy-r+2, cx+r+2, cy+r+2], fill=(0,0,0,140))

    # 글로우
    glow_layer = Image.new("RGBA", img.size, (0,0,0,0))
    _glow_circle(glow_layer, cx, cy, r+3, rgb, glow=14 if not highlight else 22)
    img.alpha_composite(glow_layer)

    # 본체
    d.ellipse([cx-r, cy-r, cx+r, cy+r], fill=(*rgb, 230), outline=(255,255,255,60), width=2)

    # 하이라이트
    d.ellipse([cx-r+4, cy-r+3, cx-r+12, cy-r+11], fill=(255,255,255,70))

    # 타이 마크(초록 슬래시)
    if tie_marks > 0:
        d.line([cx-10, cy+8, cx+10, cy-8], fill=(60, 255, 160, 220), width=3)
        if tie_marks >= 2:
            d.line([cx-10, cy+4, cx+10, cy-12], fill=(60, 255, 160, 190), width=2)

    # 하이라이트 링
    if highlight:
        d.ellipse([cx-r-6, cy-r-6, cx+r+6, cy+r+6], outline=(*rgb, 200), width=4)

def _build_bigroad_positions(results: list[str], rows: int = 6):
    """
    big road 포지션 생성 (D/T만 칸을 차지)
    tie(I)는 마지막 칸에 표시만 추가

    반환:
    placements: list[(col,row,result,tie_marks)]
    last_pos: (col,row) or None
    """
    placements = []
    col = -1
    row = 0
    last = None
    tie_marks = 0
    last_index = None

    for r in results:
        if r == "I":
            # 타이: 마지막 칸에 표시 누적
            if last_index is not None:
                c, rr, res, tm = placements[last_index]
                placements[last_index] = (c, rr, res, tm + 1)
            continue

        if r != last:
            col += 1
            row = 0
        else:
            row += 1
            if row >= rows:
                # 아래로 못가면 오른쪽으로 밀기
                row = rows - 1
                col += 1

        placements.append((col, row, r, 0))
        last_index = len(placements) - 1
        last = r

    last_pos = None
    if placements:
        last_pos = (placements[-1][0], placements[-1][1])
    return placements, last_pos

def render_road_board(round_id: int, winner: str, dragon_txt: str, tiger_txt: str) -> BytesIO:
    """
    방송용 그림장 이미지:
    - 상단: Round / Winner
    - 중앙: Big Road (D=빨강, T=파랑, 타이=초록 슬래시)
    """
    road_rows = fetch_road(limit=240)
    results = [r["result"] for r in road_rows]

    placements, last_pos = _build_bigroad_positions(results, rows=6)

    # 캔버스
    W, H = 1100, 620
    img = _bg_gradient(W, H)
    d = ImageDraw.Draw(img)

    # 상단 바
    bar = Image.new("RGBA", (W, 110), (0,0,0,0))
    bd = ImageDraw.Draw(bar)
    bd.rounded_rectangle([20, 18, W-20, 96], radius=26, fill=(0,0,0,120), outline=(255,255,255,30), width=1)
    img.alpha_composite(bar)

    # 헤더 텍스트
    _draw_text(img, 40, 30, f"Round #{round_id}", scale=6, fill=(235,235,245,255))
    if winner == "D":
        wcol = (255, 55, 90, 255)
    elif winner == "T":
        wcol = (70, 145, 255, 255)
    else:
        wcol = (60, 255, 160, 255)

    _draw_text(img, 370, 30, f"WINNER: {CHOICES[winner]}", scale=6, fill=wcol)
    _draw_text(img, 40, 78, f"DRAGON: {dragon_txt}", scale=5, fill=(200,200,210,255))
    _draw_text(img, 520, 78, f"TIGER: {tiger_txt}", scale=5, fill=(200,200,210,255))

    # Big road panel
    panel_x, panel_y = 40, 140
    panel_w, panel_h = 820, 440

    panel = Image.new("RGBA", (panel_w, panel_h), (0,0,0,0))
    pd = ImageDraw.Draw(panel)
    pd.rounded_rectangle([0, 0, panel_w, panel_h], radius=26, fill=(0,0,0,110), outline=(255,255,255,25), width=1)
    img.alpha_composite(panel, (panel_x, panel_y))

    # grid
    cols = 40
    rows = 6
    cell = 20  # 원 간격용
    # grid 실제 픽셀 간격
    g_cell = 34
    gx0, gy0 = panel_x + 26, panel_y + 26
    gw = cols * g_cell
    gh = rows * g_cell

    # grid lines (은은하게)
    for c in range(cols + 1):
        x = gx0 + c * g_cell
        d.line([x, gy0, x, gy0 + gh], fill=(255,255,255,18), width=1)
    for r in range(rows + 1):
        y = gy0 + r * g_cell
        d.line([gx0, y, gx0 + gw, y], fill=(255,255,255,18), width=1)

    # draw tokens (최근쪽이 오른쪽으로 차도록)
    # placements col이 계속 증가하니까, 마지막 cols 범위만 보여주기
    if placements:
        max_col = placements[-1][0]
    else:
        max_col = 0
    start_col = max(0, max_col - (cols - 1))

    for (c, r, res, tm) in placements:
        if c < start_col:
            continue
        draw_col = c - start_col
        cx = gx0 + draw_col * g_cell + g_cell // 2
        cy = gy0 + r * g_cell + g_cell // 2

        highlight = False
        if last_pos and (c, r) == last_pos:
            highlight = True

        _draw_token(img, cx, cy, res, highlight=highlight, tie_marks=tm)

    # 사이드 HUD (방송용 버튼 느낌)
    hud_x = panel_x + panel_w + 20
    hud_y = panel_y
    hud_w = 220
    hud_h = panel_h

    hud = Image.new("RGBA", (hud_w, hud_h), (0,0,0,0))
    hd = ImageDraw.Draw(hud)
    hd.rounded_rectangle([0, 0, hud_w, hud_h], radius=26, fill=(0,0,0,110), outline=(255,255,255,25), width=1)
    img.alpha_composite(hud, (hud_x, hud_y))

    # HUD 텍스트/아이콘
    _draw_text(img, hud_x + 24, hud_y + 24, "ROAD", scale=6, fill=(235,235,245,255))
    _draw_text(img, hud_x + 24, hud_y + 90, "D = RED", scale=5, fill=(255,55,90,255))
    _draw_text(img, hud_x + 24, hud_y + 130, "T = BLUE", scale=5, fill=(70,145,255,255))
    _draw_text(img, hud_x + 24, hud_y + 170, "I = TIE", scale=5, fill=(60,255,160,255))

    # Footer
    foot = Image.new("RGBA", (W, 52), (0,0,0,0))
    fd = ImageDraw.Draw(foot)
    fd.rectangle([0, 0, W, 52], fill=(0,0,0,100))
    img.alpha_composite(foot, (0, H-52))
    _draw_text(img, 40, H-46, "DT_bot  |  Broadcast Board", scale=5, fill=(180,180,190,255))

    bio = BytesIO()
    bio.name = "road.png"
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio

# ================== DOT COMMANDS ==================

async def send_help(update: Update):
    msg = (
        "🐉🐅 용호 배팅봇 (점(.) 명령어)\n\n"
        "• .startgame : 게임 시작(자동 라운드)\n"
        "• .stopgame  : 게임 중지(말 멈춤)\n"
        "• .bet D 1000 : 용 배팅\n"
        "• .bet T 1000 : 호 배팅\n"
        "• .bet I 1000 : 타이 배팅\n"
        "• .balance : 포인트 확인\n"
        "• .round   : 라운드 상태\n"
        "• .daily   : 일일보상(+10,000)\n"
        "• .give 유저ID 금액 : (관리자) 포인트 지급\n"
        "• .road    : 현재 그림장(로드) 보기\n"
    )
    await update.message.reply_text(msg)

async def handle_startgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    context.application.bot_data["game_chat_id"] = chat_id
    st = get_state()
    await update.message.reply_text(
        f"🎮 용호 시작!\n"
        f"현재 라운드 #{st['round_id']} 배팅 중.\n"
        f".bet D 1000 처럼 배팅!"
    )

async def handle_stopgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["game_chat_id"] = None
    await update.message.reply_text("🛑 게임을 중지했어. 다시 시작하려면 .startgame")

async def handle_balance(update: Update):
    uid = update.effective_user.id
    ensure_user(uid)
    await update.message.reply_text(f"💰 현재 포인트: {get_points(uid):,}")

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
    uid = update.effective_user.id
    ensure_user(uid)
    add_points(uid, DAILY_REWARD)
    await update.message.reply_text(
        f"🎁 일일보상 지급!\n+{DAILY_REWARD:,}\n현재 보유: {get_points(uid):,}"
    )

async def handle_give(update: Update, args: list[str]):
    if ADMIN_ID is None:
        await update.message.reply_text("⛔ ADMIN_ID가 없어. Railway Variables에 ADMIN_ID 넣어줘.")
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
        await update.message.reply_text("유저ID/금액은 숫자여야 해.")
        return
    if amount == 0:
        await update.message.reply_text("금액은 0이 될 수 없어.")
        return
    ensure_user(target_id)
    add_points(target_id, amount)
    await update.message.reply_text(f"💰 지급 완료\n대상: {target_id}\n금액: {amount:,}")

async def handle_bet(update: Update, args: list[str]):
    uid = update.effective_user.id
    ensure_user(uid)

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
        await update.message.reply_text("금액은 숫자여야 해. 예: .bet D 1000")
        return
    if amount <= 0:
        await update.message.reply_text("금액은 1 이상이어야 해.")
        return

    res = place_bet(int(st["round_id"]), uid, choice, amount)
    if res == "ALREADY":
        await update.message.reply_text("이미 이번 라운드에 배팅했어! (라운드당 1번)")
        return
    if res == "NO_MONEY":
        await update.message.reply_text(f"잔액 부족! 현재 포인트: {get_points(uid):,}")
        return

    await update.message.reply_text(
        f"✅ 배팅 완료!\n"
        f"라운드 #{st['round_id']} | {CHOICES[choice]} | {amount:,}\n"
        f"남은 포인트: {get_points(uid):,}"
    )

async def handle_road(update: Update):
    # 마지막 라운드 정보로 그림장 렌더 (없으면 기본)
    rows = fetch_road(limit=1)
    st = get_state()
    rid = int(st["round_id"])
    if rows:
        last = rows[-1]
        winner = last["result"]
        dragon_txt = last["dragon"]
        tiger_txt = last["tiger"]
        board = render_road_board(last["round_id"], winner, dragon_txt, tiger_txt)
    else:
        board = render_road_board(rid, "D", "?", "?")
    await update.message.reply_photo(photo=board)

# ================== GAME TICK ==================

async def game_tick(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.application.bot_data.get("game_chat_id")
    if not chat_id:
        return

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

        dragon_txt = dragon.text()
        tiger_txt = tiger.text()

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

        # ✅ 히스토리 저장 + 베팅 정리
        insert_road(round_id, winner, dragon_txt, tiger_txt)
        clear_bets(round_id)

        # ✅ 채팅 결과
        caption = (
            f"🎴 라운드 #{round_id} 오픈!\n"
            f"🐉 용: {dragon_txt}\n"
            f"🐅 호: {tiger_txt}\n"
            f"🏆 결과: {CHOICES[winner]}\n"
            f"✅ 당첨자 수: {total_winners}명 | 지급 합계: {total_paid:,}"
        )

        # ✅ 그림장 이미지
        board = render_road_board(round_id, winner, dragon_txt, tiger_txt)
        await context.bot.send_photo(chat_id, photo=board, caption=caption)

        # 다음 라운드
        last_result = f"{dragon_txt} vs {tiger_txt} => {CHOICES[winner]}"
        new_round = round_id + 1
        set_state(new_round, "BETTING", now + ROUND_SECONDS, last_result)

        await context.bot.send_message(
            chat_id,
            f"🎲 다음 라운드 #{new_round} 시작!\n"
            f"{ROUND_SECONDS}초 동안 배팅 가능.\n"
            f".bet D 1000 (용) | .bet T 1000 (호) | .bet I 1000 (타이)"
        )

# ================== DOT ROUTER ==================

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    if not text.startswith("."):
        return

    parts = text.split()
    cmd = parts[0].lower()
    args = parts[1:]

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
    elif cmd == ".road":
        await handle_road(update)
    else:
        await update.message.reply_text("알 수 없는 명령어야. .help 를 쳐봐")

# ================== MAIN ==================

def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 환경변수가 없어!")

    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    # 1초마다 tick
    app.job_queue.run_repeating(game_tick, interval=1, first=1)

    app.run_polling()

if __name__ == "__main__":
    main()
