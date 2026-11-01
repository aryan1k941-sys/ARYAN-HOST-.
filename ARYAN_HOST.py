import os
import sys
import re
import ast
import json
import time
import asyncio
import logging
import sqlite3
import hashlib
import secrets
import subprocess
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import psutil
from cryptography.fernet import Fernet, InvalidToken
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest

# =============================== CONFIG ======================================
BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
HOST_DIR = BASE / "hosted_files"
CLONE_DIR = DATA / "clones"
for folder in (DATA, HOST_DIR, CLONE_DIR):
    folder.mkdir(parents=True, exist_ok=True)

MAIN_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("ADMIN_ID", "8751555373"))
CLONE_MODE = os.getenv("CLONE_MODE", "0") == "1"
CLONE_OWNER_ID = int(os.getenv("CLONE_OWNER_ID", str(OWNER_ID)))
DB_PATH = Path(os.getenv("DB_PATH", str(DATA / ("clone_" + str(CLONE_OWNER_ID) + ".db" if CLONE_MODE else "main.db"))))
MAX_FILE_BYTES = 1_000_000
FAM_BASE = "https://famgateway.in"
DOCKER_IMAGE = "python:3.12-slim"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ARYAN_HOST")

if not MAIN_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable missing. Add your Telegram bot token.")

bot = Bot(MAIN_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# Token encryption key is generated once and stored locally. Keep data/fernet.key safe.
KEY_FILE = DATA / "fernet.key"
if not KEY_FILE.exists():
    KEY_FILE.write_bytes(Fernet.generate_key())
    try:
        KEY_FILE.chmod(0o600)
    except OSError:
        pass
FERNET = Fernet(KEY_FILE.read_bytes())

# ================================ DATABASE ==================================
def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, premium_until TEXT, banned INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS hosted_bots(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, bot_name TEXT,
            file_path TEXT, container_name TEXT, status TEXT DEFAULT 'Stopped')""")
        c.execute("""CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, order_id TEXT,
            amount REAL, kind TEXT, plan_days INTEGER DEFAULT 0, status TEXT DEFAULT 'pending',
            created_at TEXT, gateway TEXT DEFAULT 'fam', qr_url TEXT, checkout_url TEXT, pay_id TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS clones(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, token_enc TEXT,
            bot_username TEXT, status TEXT DEFAULT 'Stopped')""")
        defaults = {
            "clone_price": "50",
            "upi_id": "",
            "fam_api_key": "",
            "zap_api_key": "",
            "payment_gateway": "fam",
            "fresh_channel": "",
            "premium_1": "10",
            "premium_2": "18",
            "premium_3": "25",
            "premium_30": "99",
            "premium_60": "179",
        }
        for k, v in defaults.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
        cols = {row[1] for row in c.execute("PRAGMA table_info(orders)").fetchall()}
        for col, definition in (("gateway", "TEXT DEFAULT 'fam'"), ("qr_url", "TEXT"),
                                ("checkout_url", "TEXT"), ("pay_id", "TEXT")):
            if col not in cols:
                c.execute(f"ALTER TABLE orders ADD COLUMN {col} {definition}")

def setting(key, default=""):
    # Clone instances read shared commercial settings from the main bot database.
    source_db = DATA / "main.db" if CLONE_MODE else DB_PATH
    try:
        conn = sqlite3.connect(source_db, timeout=10)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        conn.close()
        if not row:
            return default
        value = row["value"]
        if key in ("fam_api_key", "zap_api_key"):
            try:
                return FERNET.decrypt(value.encode()).decode()
            except (InvalidToken, ValueError):
                return value  # supports an older plaintext setting; re-save it to encrypt
        return value
    except sqlite3.Error:
        return default

def set_setting(key, value):
    stored = FERNET.encrypt(str(value).encode()).decode() if key in ("fam_api_key", "zap_api_key") and value else str(value)
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, stored))

def ensure_user(uid):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (uid,))
        return c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def premium_active(uid):
    if CLONE_MODE and uid == CLONE_OWNER_ID:
        return True
    row = ensure_user(uid)
    if not row or not row["premium_until"]:
        return False
    try:
        return datetime.fromisoformat(row["premium_until"]) > datetime.now(timezone.utc)
    except ValueError:
        return False

def grant_premium(uid, days):
    until = datetime.now(timezone.utc) + timedelta(days=days)
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (uid,))
        c.execute("UPDATE users SET premium_until=? WHERE user_id=?", (until.isoformat(), uid))

# ================================= STATES ====================================
class Form(StatesGroup):
    upload = State()
    clone_token = State()
    clone_admin = State()
    admin_value = State()
    premium_user = State()
    broadcast = State()

# ================================ KEYBOARDS =================================
def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)

def main_kb(uid):
    rows = [
        [InlineKeyboardButton(text="🤖 Create My Clone Bot", callback_data="clone_buy")],
        [InlineKeyboardButton(text="💎 Buy Premium", callback_data="premium_menu")],
        [InlineKeyboardButton(text="📦 My Clones", callback_data="my_clones"),
         InlineKeyboardButton(text="📊 Server Status", callback_data="status")],
        [InlineKeyboardButton(text="❓ Help", callback_data="help")]
    ]
    if uid == OWNER_ID and not CLONE_MODE:
        rows.append([InlineKeyboardButton(text="👑 ADMIN PANEL", callback_data="admin")])
    return kb(rows)

def clone_home_kb(uid):
    rows = [
        [InlineKeyboardButton(text="📤 Upload Python Bot", callback_data="upload")],
        [InlineKeyboardButton(text="📱 My Hosted Bots", callback_data="my_bots")],
        [InlineKeyboardButton(text="💎 Premium Plans", callback_data="premium_menu")],
        [InlineKeyboardButton(text="📊 Server Status", callback_data="status"),
         InlineKeyboardButton(text="❓ Help", callback_data="help")]
    ]
    if uid == CLONE_OWNER_ID:
        rows.append([InlineKeyboardButton(text="👑 ADMIN PANEL", callback_data="clone_admin")])
    return kb(rows)

def bot_controls(bot_id):
    return kb([
        [InlineKeyboardButton(text="📋 Live Logs", callback_data=f"logs:{bot_id}")],
        [InlineKeyboardButton(text="▶️ Start", callback_data=f"start:{bot_id}"),
         InlineKeyboardButton(text="⏹ Stop", callback_data=f"stop:{bot_id}")],
        [InlineKeyboardButton(text="🗑 Delete", callback_data=f"delete:{bot_id}")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="my_bots")]
    ])

def admin_kb():
    return kb([
        [InlineKeyboardButton(text="💰 Clone Price", callback_data="set:clone_price"),
         InlineKeyboardButton(text="💎 Premium Prices", callback_data="prices")],
        [InlineKeyboardButton(text="🔑 FamGateway API Key", callback_data="set:fam_api_key")],
        [InlineKeyboardButton(text="🔑 ZapUPI API Key", callback_data="set:zap_api_key")],
        [InlineKeyboardButton(text="💳 Payment Gateway", callback_data="gateway_menu")],
        [InlineKeyboardButton(text="🆔 UPI ID", callback_data="set:upi_id")],
        [InlineKeyboardButton(text="📢 Fresh Channel", callback_data="set:fresh_channel")],
        [InlineKeyboardButton(text="👥 Users / Stats", callback_data="stats"),
         InlineKeyboardButton(text="🎁 Give Premium", callback_data="give_premium")],
        [InlineKeyboardButton(text="📣 Broadcast", callback_data="broadcast")],
        [InlineKeyboardButton(text="🔙 Home", callback_data="home")]
    ])

def premium_kb():
    labels = [(1, "1 Day"), (2, "2 Days"), (3, "3 Days"), (30, "1 Month"), (60, "2 Months")]
    rows = []
    for days, label in labels:
        price = setting(f"premium_{days}", "0")
        rows.append([InlineKeyboardButton(text=f"{label} — ₹{price}", callback_data=f"buyplan:{days}")])
    rows.append([InlineKeyboardButton(text="🔙 Back", callback_data="home")])
    return kb(rows)

async def edit(call, text, markup=None):
    try:
        await call.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            await call.message.answer(text, reply_markup=markup, disable_web_page_preview=True)

# ================================ CHANNEL ====================================
async def channel_ok(uid):
    channel = setting("fresh_channel").strip()
    if not channel:
        return True
    try:
        member = await bot.get_chat_member(channel, uid)
        return member.status not in ("left", "kicked")
    except Exception:
        return False

async def gate(msg_or_call):
    uid = msg_or_call.from_user.id
    if uid == (CLONE_OWNER_ID if CLONE_MODE else OWNER_ID):
        return True
    row = ensure_user(uid)
    if row["banned"]:
        if isinstance(msg_or_call, Message):
            await msg_or_call.answer("🚫 You are blocked from using this bot.")
        else:
            await msg_or_call.answer("🚫 You are blocked.", show_alert=True)
        return False
    if not await channel_ok(uid):
        channel = setting("fresh_channel")
        text = f"📢 Pehle hamara channel join karo: {channel}\nJoin karne ke baad /start dabao."
        if isinstance(msg_or_call, Message):
            await msg_or_call.answer(text)
        else:
            await msg_or_call.answer(text, show_alert=True)
        return False
    return True

# ================================ PAYMENTS ===================================
async def create_order(uid, amount, kind, days=0):
    gateway = setting("payment_gateway", "fam").lower()
    timeout = aiohttp.ClientTimeout(total=25)
    if gateway == "zap":
        api_key = setting("zap_api_key").strip()
        if not api_key:
            raise RuntimeError("Admin ne ZapUPI API Key set nahi ki hai.")
        oid = f"ARY{uid}{int(time.time())}{secrets.token_hex(2).upper()}"
        payload = {"zap_key": api_key, "order_id": oid, "amount": f"{float(amount):.2f}",
                   "remark": f"ARYAN HOST | {kind} | {uid}"}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post("https://pay.zapupi.com/api/create-order", json=payload) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400 or str(data.get("status", "")).lower() != "success":
                    raise RuntimeError(str(data.get("message") or data)[:400])
        info = data.get("data") or data
        oid = str(info.get("order_id") or oid)
        qr_url = info.get("payment_image_url") or ""
        checkout_url = info.get("payment_url") or ""
        pay_id = str(info.get("pay_id") or "")
    else:
        api_key = setting("fam_api_key").strip()
        if not api_key:
            raise RuntimeError("Admin ne FamGateway API Key set nahi ki hai.")
        payload = {"amount": float(amount), "customer_name": f"Telegram User {uid}",
                   "customer_email": f"{uid}@telegram.local"}
        headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(FAM_BASE + "/api/create-order", json=payload, headers=headers) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400 or str(data.get("status", "")).lower() != "success":
                    raise RuntimeError(str(data.get("message") or data)[:400])
        info = data.get("data") or data
        oid = str(info.get("order_id", ""))
        qr_url = info.get("qr_url") or ""
        checkout_url = info.get("checkout_url") or ""
        pay_id = ""
        if not oid:
            raise RuntimeError("FamGateway ne Order ID return nahi kiya.")

    with db() as c:
        c.execute("""INSERT INTO orders
            (user_id,order_id,amount,kind,plan_days,status,created_at,gateway,qr_url,checkout_url,pay_id)
            VALUES(?,?,?,?,?,'pending',?,?,?,?,?)""",
            (uid, oid, float(amount), kind, days, datetime.now(timezone.utc).isoformat(),
             gateway, qr_url, checkout_url, pay_id))
    info = dict(info)
    info.update({"_gateway": gateway, "_qr_url": qr_url, "_checkout_url": checkout_url, "_pay_id": pay_id, "_upi_button": info.get("upi_button", ""), "_paytm_button": info.get("paytm_button", "")})
    return oid, info

async def verify_order(uid, oid):
    with db() as c:
        order = c.execute("SELECT * FROM orders WHERE order_id=? AND user_id=?", (oid, uid)).fetchone()
    if not order:
        return False, "Order nahi mila."
    if order["status"] == "paid":
        return True, "Payment pehle hi verify ho chuka hai."
    timeout = aiohttp.ClientTimeout(total=25)
    if order["gateway"] == "zap":
        api_key = setting("zap_api_key").strip()
        if not api_key:
            return False, "ZapUPI API Key admin panel mein set nahi hai."
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post("https://pay.zapupi.com/api/order-status",
                                    json={"zap_key": api_key, "order_id": oid}) as resp:
                data = await resp.json(content_type=None)
        info = data.get("data") or data
        state = str(info.get("status", data.get("status", ""))).lower()
        paid = state in ("success", "successful", "paid", "completed")
    else:
        api_key = setting("fam_api_key").strip()
        if not api_key:
            return False, "FamGateway API Key admin panel mein set nahi hai."
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(FAM_BASE + "/api/verify-order.php",
                                   params={"api_key": api_key, "order_id": oid}) as resp:
                data = await resp.json(content_type=None)
        def find_states(obj):
            found = []
            if isinstance(obj, dict):
                for key, value in obj.items():
                    if str(key).lower() in ("status", "payment_status", "order_status"):
                        found.append(str(value).lower())
                    found.extend(find_states(value))
            elif isinstance(obj, list):
                for value in obj:
                    found.extend(find_states(value))
            return found
        paid = any(x in ("paid", "success", "successful", "completed", "captured") for x in find_states(data))
    if not paid:
        return False, "Payment abhi confirm nahi hua. Thodi der baad Verify dabao."
    with db() as c:
        c.execute("UPDATE orders SET status='paid' WHERE order_id=?", (oid,))
    if order["kind"] == "premium":
        grant_premium(uid, int(order["plan_days"]))
        return True, f"✅ {order['plan_days']} days Premium active ho gaya!"
    if order["kind"] == "clone":
        return True, "Payment verified. Ab apne clone bot ka token bhejo."
    return True, "Payment verified."

async def send_payment_details(call, title, amount, oid, info, verify_callback, back_callback):
    qr_url = info.get("_qr_url") or ""
    checkout = info.get("_checkout_url") or ""
    pay_id = info.get("_pay_id") or ""
    caption = (f"💳 <b>{title}</b>\n💰 Amount: <b>₹{float(amount):.2f}</b>\n"
               f"🧾 Order ID: <code>{oid}</code>\n\n"
               "📲 QR scan karke payment karo. Payment confirm hone ke baad Verify dabao.")
    rows = []
    if pay_id:
        upi_button = info.get("_upi_button") or info.get("payment_data") or ""
        paytm_button = info.get("_paytm_button") or ""
        if upi_button:
            rows.append([InlineKeyboardButton(text="📲 Pay via UPI App", url=upi_button)])
        if paytm_button:
            rows.append([InlineKeyboardButton(text="🟦 Pay via Paytm", url=paytm_button)])
    elif checkout:
        rows.append([InlineKeyboardButton(text="🌐 Pay Now", url=checkout)])
    rows.append([InlineKeyboardButton(text="✅ Verify Payment", callback_data=verify_callback)])
    rows.append([InlineKeyboardButton(text="🔙 Back", callback_data=back_callback)])
    markup = kb(rows)
    if qr_url:
        try:
            await call.message.answer_photo(qr_url, caption=caption, reply_markup=markup)
            await call.answer()
            return
        except Exception as exc:
            log.warning("Could not send QR image: %s", exc)
    await edit(call, caption + ("\n\nPayment link: " + checkout if checkout else
                                "\nQR image gateway se nahi mila. Admin settings check karo."), markup)

# ============================== CLONE MANAGER ===============================
CLONE_PROCESSES = {}

def decrypt_token(value):
    try:
        return FERNET.decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        return ""

async def launch_clone(clone_id, uid, token_enc):
    old = CLONE_PROCESSES.get(clone_id)
    if old and old.returncode is None:
        return True
    token = decrypt_token(token_enc)
    if not token:
        log.error("Could not decrypt clone token id=%s", clone_id)
        return False
    env = os.environ.copy()
    env.update({"BOT_TOKEN": token, "ADMIN_ID": str(uid), "CLONE_OWNER_ID": str(uid),
                "CLONE_MODE": "1", "DB_PATH": str(CLONE_DIR / f"clone_{uid}.db")})
    proc = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).resolve()),
        env=env, cwd=str(BASE), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    CLONE_PROCESSES[clone_id] = proc
    with db() as c:
        c.execute("UPDATE clones SET status='Running' WHERE id=?", (clone_id,))
    return True

async def launch_saved_clones():
    if CLONE_MODE:
        return
    with db() as c:
        rows = c.execute("SELECT id,user_id,token_enc FROM clones").fetchall()
    for row in rows:
        try:
            await launch_clone(row["id"], row["user_id"], row["token_enc"])
        except Exception:
            log.exception("Clone restart failed")

# ============================== DOCKER RUNNER ===============================
def safe_container_name(bot_id):
    return f"aryan-host-{CLONE_OWNER_ID}-{bot_id}".lower()

async def docker_available():
    try:
        p = await asyncio.create_subprocess_exec("docker", "version", "--format", "{{.Server.Version}}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(p.communicate(), timeout=10)
        return p.returncode == 0
    except Exception:
        return False

async def docker_action(args, timeout=180):
    p = await asyncio.create_subprocess_exec("docker", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()
        raise RuntimeError("Docker command timeout.")
    return p.returncode, out.decode(errors="ignore")[-3500:]

# ================================ START ======================================
@dp.message(CommandStart())
async def start_cmd(msg: Message, state: FSMContext):
    await state.clear()
    if not await gate(msg):
        return
    row = ensure_user(msg.from_user.id)
    if CLONE_MODE:
        until = row["premium_until"] or "Not Active"
        active = premium_active(msg.from_user.id)
        txt = (f"🎬 <b>ARYAN HOST — CLONE PANEL</b>\n"
               f"✨ Welcome, <b>{msg.from_user.first_name}</b>!\n\n"
               f"🆔 ID: <code>{msg.from_user.id}</code>\n"
               f"💎 Premium: {'Active 👑' if active else 'Not Active'}\n"
               f"📅 Valid Till: <code>{until}</code>\n\n"
               "👇 Select an option:")
        await msg.answer(txt, reply_markup=clone_home_kb(msg.from_user.id))
    else:
        txt = (f"🎬 <b>ARYAN HOST PRO</b>\n"
               f"✨ Welcome, <b>{msg.from_user.first_name}</b>!\n"
               f"🆔 ID: <code>{msg.from_user.id}</code>\n\n"
               "🤖 Har customer apna alag clone bot bana sakta hai.\n"
               "💎 Clone aur Premium purchase yahan se karein.")
        await msg.answer(txt, reply_markup=main_kb(msg.from_user.id))

@dp.message(Command("admin"))
async def admin_cmd(msg: Message):
    if msg.from_user.id == (CLONE_OWNER_ID if CLONE_MODE else OWNER_ID):
        await msg.answer("👑 ADMIN PANEL", reply_markup=admin_kb() if not CLONE_MODE else kb([[InlineKeyboardButton(text="🎁 Give Premium", callback_data="clone_give")]]))

@dp.callback_query(F.data == "home")
async def home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    if CLONE_MODE:
        await edit(call, "🎬 <b>ARYAN HOST — CLONE PANEL</b>", clone_home_kb(call.from_user.id))
    else:
        await edit(call, "🎬 <b>ARYAN HOST PRO</b>", main_kb(call.from_user.id))

@dp.callback_query(F.data == "help")
async def help_cb(call: CallbackQuery):
    text = ("📌 <b>Help</b>\n1. Premium/Clone buy karein.\n2. Clone ke liye BotFather se naya token bhejein.\n3. Clone bot mein users Premium lekar .py upload kar sakte hain.\n4. VPS par Docker installed hona chahiye.")
    await edit(call, text, kb([[InlineKeyboardButton(text="🔙 Back", callback_data="home")]]))

# ============================= CLONE PURCHASE ===============================
@dp.callback_query(F.data == "clone_buy")
async def clone_buy(call: CallbackQuery, state: FSMContext):
    if CLONE_MODE:
        return await call.answer("This feature is available in main bot only.", show_alert=True)
    price = float(setting("clone_price", "0") or 0)
    if price > 0:
        try:
            oid, info = await create_order(call.from_user.id, price, "clone")
            await send_payment_details(call, "Clone Bot Purchase", price, oid, info,
                                       f"verifyclone:{oid}", "home")
        except Exception as e:
            await call.answer(str(e)[:180], show_alert=True)
    else:
        await state.set_state(Form.clone_token)
        await edit(call, "🤖 <b>Apne naye clone bot ka BotFather token bhejo.</b>\nToken kisi aur ko share mat karna.",
                   kb([[InlineKeyboardButton(text="Cancel", callback_data="home")]]))

@dp.callback_query(F.data.startswith("verifyclone:"))
async def verify_clone_payment(call: CallbackQuery, state: FSMContext):
    oid = call.data.split(":", 1)[1]
    ok, message = await verify_order(call.from_user.id, oid)
    if ok:
        await state.set_state(Form.clone_token)
        await edit(call, message + "\n\nAb BotFather token bhejo.", kb([[InlineKeyboardButton(text="Cancel", callback_data="home")]]))
    else:
        await call.answer(message, show_alert=True)

@dp.message(Form.clone_token)
async def receive_clone_token(msg: Message, state: FSMContext):
    token = (msg.text or "").strip()
    if not re.fullmatch(r"\d{8,12}:[A-Za-z0-9_-]{30,}", token):
        return await msg.answer("❌ Token format sahi nahi hai. BotFather se mila token bhejo.")
    if token == MAIN_TOKEN:
        return await msg.answer("❌ Main hosting bot ka token nahi. Naya bot token bhejo.")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.telegram.org/bot{token}/getMe", timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()
        if not data.get("ok"):
            return await msg.answer("❌ Token invalid hai. BotFather se token dobara check karo.")
        username = data["result"].get("username", "")
    except Exception:
        return await msg.answer("❌ Token verify nahi ho paya. Dobara try karo.")
    await state.update_data(clone_token=token, clone_username=username)
    await state.set_state(Form.clone_admin)
    await msg.answer("✅ Token valid!\nAb apna Telegram Admin ID bhejo (numeric).")

@dp.message(Form.clone_admin)
async def receive_clone_admin(msg: Message, state: FSMContext):
    if not (msg.text or "").strip().isdigit():
        return await msg.answer("❌ Admin ID sirf numbers mein bhejo.")
    uid = int(msg.text.strip())
    data = await state.get_data()
    token = data.get("clone_token", "")
    username = data.get("clone_username", "")
    enc = FERNET.encrypt(token.encode()).decode()
    with db() as c:
        cur = c.execute("INSERT INTO clones(user_id,token_enc,bot_username,status) VALUES(?,?,?,'Stopped')",
                        (uid, enc, username))
        clone_id = cur.lastrowid
    await state.clear()
    await launch_clone(clone_id, uid, enc)
    await msg.answer(f"🎉 <b>Clone Bot Created!</b>\n\n🤖 @{username}\n👤 Admin ID: <code>{uid}</code>\n\nAb tumhara clone bot online hona chahiye. Usko /start bhejo.", reply_markup=main_kb(msg.from_user.id))

@dp.callback_query(F.data == "my_clones")
async def my_clones(call: CallbackQuery):
    if CLONE_MODE:
        return await call.answer("Main bot only.", show_alert=True)
    with db() as c:
        rows = c.execute("SELECT id,user_id,bot_username,status FROM clones WHERE user_id=? OR ?=? ORDER BY id DESC",
                         (call.from_user.id, call.from_user.id, OWNER_ID)).fetchall()
    if not rows:
        return await edit(call, "Abhi koi clone nahi hai.", main_kb(call.from_user.id))
    lines = ["🤖 <b>Your Clones</b>"]
    for r in rows:
        lines.append(f"• @{r['bot_username']} — {r['status']} — ID {r['id']}")
    await edit(call, "\n".join(lines), main_kb(call.from_user.id))

# ================================ PREMIUM ====================================
@dp.callback_query(F.data == "premium_menu")
async def premium_menu(call: CallbackQuery):
    if not CLONE_MODE:
        return await call.answer("Premium plans clone bot ke andar milenge.", show_alert=True)
    await edit(call, "💎 <b>Choose Premium Duration</b>", premium_kb())

@dp.callback_query(F.data.startswith("buyplan:"))
async def buy_plan(call: CallbackQuery):
    if not CLONE_MODE:
        return await call.answer("Premium plans clone bot ke andar milenge.", show_alert=True)
    days = int(call.data.split(":")[1])
    price = float(setting(f"premium_{days}", "0") or 0)
    if price <= 0:
        return await call.answer("Is plan ki price admin ne set nahi ki.", show_alert=True)
    try:
        oid, info = await create_order(call.from_user.id, price, "premium", days)
        await send_payment_details(call, f"{days} Days Premium", price, oid, info,
                                   f"verify:{oid}", "premium_menu")
    except Exception as e:
        await call.answer(str(e)[:180], show_alert=True)

@dp.callback_query(F.data.startswith("verify:"))
async def verify_premium(call: CallbackQuery):
    oid = call.data.split(":", 1)[1]
    ok, message = await verify_order(call.from_user.id, oid)
    if ok:
        await edit(call, message, clone_home_kb(call.from_user.id))
    else:
        await call.answer(message, show_alert=True)

# =============================== HOSTING UI ==================================
@dp.callback_query(F.data == "upload")
async def upload_start(call: CallbackQuery, state: FSMContext):
    if not CLONE_MODE:
        return await call.answer("Create/open your clone bot first.", show_alert=True)
    if not await gate(call):
        return
    if not premium_active(call.from_user.id):
        return await call.answer("Bot upload/run karne ke liye Premium zaroori hai.", show_alert=True)
    await state.set_state(Form.upload)
    await edit(call, "📤 Apni valid Python (.py) file bhejo. Max size 1 MB.\n\n⚠️ Sirf trusted code upload karo. File Docker container mein run hogi.",
               kb([[InlineKeyboardButton(text="🔙 Cancel", callback_data="home")]]))

@dp.message(Form.upload, F.document)
async def upload_file(msg: Message, state: FSMContext):
    if not CLONE_MODE:
        return
    if not premium_active(msg.from_user.id):
        await state.clear()
        return await msg.answer("💎 Premium active nahi hai. Pehle Premium lo.")
    doc = msg.document
    name = (doc.file_name or "").lower()
    if not name.endswith(".py"):
        return await msg.answer("❌ Sirf .py file allowed hai.")
    if doc.file_size and doc.file_size > MAX_FILE_BYTES:
        return await msg.answer("❌ File 1 MB se badi hai.")
    user_dir = HOST_DIR / str(CLONE_OWNER_ID) / str(msg.from_user.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", doc.file_name)[:80]
    dest = user_dir / f"{int(time.time())}_{safe_name}"
    await bot.download(doc, destination=dest)
    try:
        raw = dest.read_text(encoding="utf-8")
        ast.parse(raw)
    except (SyntaxError, UnicodeDecodeError, OSError) as e:
        dest.unlink(missing_ok=True)
        return await msg.answer(f"❌ Python file valid nahi hai: {str(e)[:250]}")
    with db() as c:
        cur = c.execute("INSERT INTO hosted_bots(user_id,bot_name,file_path,container_name) VALUES(?,?,?,?)",
                        (msg.from_user.id, safe_name, str(dest), ""))
        bid = cur.lastrowid
        cname = safe_container_name(bid)
        c.execute("UPDATE hosted_bots SET container_name=? WHERE id=?", (cname, bid))
    await state.clear()
    await msg.answer(f"✅ File upload ho gayi!\n📄 <code>{safe_name}</code>\n\nStart karne ke liye button dabao.",
                     reply_markup=bot_controls(bid))

@dp.callback_query(F.data == "my_bots")
async def my_bots(call: CallbackQuery):
    if not CLONE_MODE:
        return await call.answer("Clone bot ke andar available hai.", show_alert=True)
    with db() as c:
        rows = c.execute("SELECT id,bot_name,status FROM hosted_bots WHERE user_id=? ORDER BY id DESC",
                         (call.from_user.id,)).fetchall()
    if not rows:
        return await edit(call, "📭 Abhi koi bot upload nahi kiya.", clone_home_kb(call.from_user.id))
    rows_kb = [[InlineKeyboardButton(text=f"{'🟢' if r['status']=='Running' else '🔴'} {r['bot_name']}",
                                     callback_data=f"view:{r['id']}")] for r in rows]
    rows_kb.append([InlineKeyboardButton(text="🔙 Back", callback_data="home")])
    await edit(call, "📱 <b>My Hosted Bots</b>", kb(rows_kb))

async def owned_bot(uid, bid):
    with db() as c:
        return c.execute("SELECT * FROM hosted_bots WHERE id=? AND user_id=?", (bid, uid)).fetchone()

@dp.callback_query(F.data.startswith("view:"))
async def view_bot(call: CallbackQuery):
    bid = int(call.data.split(":")[1])
    row = await owned_bot(call.from_user.id, bid)
    if not row:
        return await call.answer("Bot nahi mila.", show_alert=True)
    await edit(call, f"🤖 <b>Bot Control Panel</b>\n📄 {row['bot_name']}\n📊 Status: {row['status']}",
               bot_controls(bid))

@dp.callback_query(F.data.startswith("start:"))
async def start_hosted_bot(call: CallbackQuery):
    bid = int(call.data.split(":")[1])
    row = await owned_bot(call.from_user.id, bid)
    if not row:
        return await call.answer("Not found.", show_alert=True)
    if not premium_active(call.from_user.id):
        return await call.answer("💎 Premium required.", show_alert=True)
    if not await docker_available():
        return await call.answer("Server par Docker installed/running nahi hai.", show_alert=True)
    path = Path(row["file_path"]).resolve()
    if not path.exists():
        return await call.answer("File missing.", show_alert=True)
    cname = row["container_name"] or safe_container_name(bid)
    await call.answer("Starting...")
    # Run only inside a constrained container; never execute uploaded Python on host.
    # Remove a previous exited container with the same name before recreating it.
    await docker_action(["rm", "-f", cname], timeout=20)
    args = ["run", "-d", "--name", cname, "--restart", "unless-stopped",
            "--memory=256m", "--cpus=0.50", "--pids-limit=64",
            "--network=bridge", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m",
            "-v", f"{path.parent}:/app:ro", "-w", "/app",
            DOCKER_IMAGE, "python", "-u", f"/app/{path.name}"]
    rc, output = await docker_action(args, timeout=90)
    if rc != 0:
        await call.message.answer(f"❌ Docker start failed:\n<pre>{output[-2500:]}</pre>")
    else:
        with db() as c:
            c.execute("UPDATE hosted_bots SET status='Running' WHERE id=?", (bid,))
        await call.message.answer("🟢 Bot started in isolated Docker container.", reply_markup=bot_controls(bid))

@dp.callback_query(F.data.startswith("stop:"))
async def stop_hosted_bot(call: CallbackQuery):
    bid = int(call.data.split(":")[1])
    row = await owned_bot(call.from_user.id, bid)
    if not row:
        return await call.answer("Not found.", show_alert=True)
    cname = row["container_name"]
    if await docker_available():
        await docker_action(["rm", "-f", cname], timeout=30)
    with db() as c:
        c.execute("UPDATE hosted_bots SET status='Stopped' WHERE id=?", (bid,))
    await call.answer("Bot stopped.")
    await view_bot(call)

@dp.callback_query(F.data.startswith("logs:"))
async def show_logs(call: CallbackQuery):
    bid = int(call.data.split(":")[1])
    row = await owned_bot(call.from_user.id, bid)
    if not row:
        return await call.answer("Not found.", show_alert=True)
    if not await docker_available():
        return await call.answer("Docker unavailable.", show_alert=True)
    rc, output = await docker_action(["logs", "--tail", "35", row["container_name"]], timeout=20)
    await edit(call, f"📋 <b>Latest Logs</b>\n<pre>{output[-3000:] or 'No logs yet.'}</pre>",
               kb([[InlineKeyboardButton(text="🔄 Refresh", callback_data=f"logs:{bid}")],
                   [InlineKeyboardButton(text="🔙 Back", callback_data=f"view:{bid}")]]))

@dp.callback_query(F.data.startswith("delete:"))
async def delete_hosted_bot(call: CallbackQuery):
    bid = int(call.data.split(":")[1])
    row = await owned_bot(call.from_user.id, bid)
    if not row:
        return await call.answer("Not found.", show_alert=True)
    if await docker_available():
        await docker_action(["rm", "-f", row["container_name"]], timeout=30)
    Path(row["file_path"]).unlink(missing_ok=True)
    with db() as c:
        c.execute("DELETE FROM hosted_bots WHERE id=?", (bid,))
    await call.answer("Deleted.")
    await my_bots(call)

# ================================ ADMIN ======================================
@dp.callback_query(F.data == "admin")
async def admin_panel(call: CallbackQuery):
    if CLONE_MODE or call.from_user.id != OWNER_ID:
        return await call.answer("No access.", show_alert=True)
    await edit(call, "👑 <b>ARYAN HOST — MAIN ADMIN PANEL</b>\nYahan se clone, payment aur Premium settings manage karo.", admin_kb())

@dp.callback_query(F.data == "gateway_menu")
async def gateway_menu(call: CallbackQuery):
    if CLONE_MODE or call.from_user.id != OWNER_ID:
        return await call.answer("No access.", show_alert=True)
    current = setting("payment_gateway", "fam").upper()
    await edit(call, f"💳 <b>Payment Gateway</b>\\nCurrent: <b>{current}</b>\\nSelect gateway:",
               kb([[InlineKeyboardButton(text="FamGateway", callback_data="gateway:fam")],
                   [InlineKeyboardButton(text="ZapUPI", callback_data="gateway:zap")],
                   [InlineKeyboardButton(text="🔙 Back", callback_data="admin")]]))

@dp.callback_query(F.data.startswith("gateway:"))
async def set_gateway(call: CallbackQuery):
    if CLONE_MODE or call.from_user.id != OWNER_ID:
        return await call.answer("No access.", show_alert=True)
    value = call.data.split(":", 1)[1]
    if value not in ("fam", "zap"):
        return await call.answer("Invalid gateway.", show_alert=True)
    set_setting("payment_gateway", value)
    await edit(call, f"✅ Payment gateway ab <b>{value.upper()}</b> hai.", admin_kb())

@dp.callback_query(F.data.startswith("set:"))
async def set_admin_value(call: CallbackQuery, state: FSMContext):
    if CLONE_MODE or call.from_user.id != OWNER_ID:
        return await call.answer("No access.", show_alert=True)
    key = call.data.split(":", 1)[1]
    prompts = {"clone_price": "Clone bot ki price ₹ mein bhejo (0 = free).",
               "fam_api_key": "FamGateway API Key bhejo.",
               "zap_api_key": "ZapUPI API Key bhejo.",
               "upi_id": "UPI ID bhejo.",
               "fresh_channel": "Fresh/mandatory channel username ya chat ID bhejo. Disable karne ke liye 0 bhejo."}
    await state.update_data(setting_key=key)
    await state.set_state(Form.admin_value)
    await edit(call, prompts.get(key, "Value bhejo."), kb([[InlineKeyboardButton(text="Cancel", callback_data="admin")]]))

@dp.message(Form.admin_value)
async def save_admin_value(msg: Message, state: FSMContext):
    if msg.from_user.id != OWNER_ID or CLONE_MODE:
        return
    data = await state.get_data()
    key = data.get("setting_key")
    value = (msg.text or "").strip()
    if key == "clone_price":
        try:
            if float(value) < 0: raise ValueError()
        except ValueError:
            return await msg.answer("Valid amount bhejo.")
    if key == "fresh_channel" and value == "0":
        value = ""
    set_setting(key, value)
    await state.clear()
    await msg.answer("✅ Setting save ho gayi.", reply_markup=admin_kb())

@dp.callback_query(F.data == "prices")
async def premium_prices(call: CallbackQuery):
    if call.from_user.id != OWNER_ID or CLONE_MODE:
        return await call.answer("No access.", show_alert=True)
    rows = []
    for days, label in [(1,"1 Day"),(2,"2 Days"),(3,"3 Days"),(30,"1 Month"),(60,"2 Months")]:
        rows.append([InlineKeyboardButton(text=f"{label}: ₹{setting(f'premium_{days}')}", callback_data=f"set:premium_{days}")])
    rows.append([InlineKeyboardButton(text="🔙 Back", callback_data="admin")])
    await edit(call, "💎 <b>Premium Plan Prices</b>\nPlan select karke new price bhejo.", kb(rows))

@dp.callback_query(F.data.startswith("set:premium_"))
async def set_premium_price(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != OWNER_ID or CLONE_MODE:
        return await call.answer("No access.", show_alert=True)
    key = call.data.split(":",1)[1]
    await state.update_data(setting_key=key)
    await state.set_state(Form.admin_value)
    await edit(call, f"{key.replace('premium_','')} days ka price ₹ mein bhejo.",
               kb([[InlineKeyboardButton(text="Cancel", callback_data="prices")]]))

@dp.callback_query(F.data == "stats")
async def stats(call: CallbackQuery):
    if call.from_user.id != OWNER_ID or CLONE_MODE:
        return await call.answer("No access.", show_alert=True)
    with db() as c:
        users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        clones = c.execute("SELECT COUNT(*) n FROM clones").fetchone()["n"]
    await edit(call, f"👥 Users: <b>{users}</b>\n🤖 Clones: <b>{clones}</b>",
               kb([[InlineKeyboardButton(text="🔙 Back", callback_data="admin")]]))

@dp.callback_query(F.data == "give_premium")
async def give_premium_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != OWNER_ID or CLONE_MODE:
        return await call.answer("No access.", show_alert=True)
    await state.set_state(Form.premium_user)
    await edit(call, "User ID aur days is format mein bhejo:\n<code>123456789 30</code>",
               kb([[InlineKeyboardButton(text="Cancel", callback_data="admin")]]))

@dp.message(Form.premium_user)
async def give_premium_done(msg: Message, state: FSMContext):
    allowed_admin = CLONE_OWNER_ID if CLONE_MODE else OWNER_ID
    if msg.from_user.id != allowed_admin:
        return
    parts = (msg.text or "").split()
    if len(parts) != 2 or not all(x.isdigit() for x in parts):
        return await msg.answer("Format: USER_ID DAYS")
    grant_premium(int(parts[0]), int(parts[1]))
    await state.clear()
    await msg.answer("✅ Premium grant ho gaya.", reply_markup=admin_kb())

@dp.callback_query(F.data == "clone_admin")
async def clone_admin(call: CallbackQuery):
    if not CLONE_MODE or call.from_user.id != CLONE_OWNER_ID:
        return await call.answer("No access.", show_alert=True)
    await edit(call, "👑 <b>Clone Admin Panel</b>\nTum apne clone ke users ko Premium de sakte ho.",
               kb([[InlineKeyboardButton(text="🎁 Give Premium", callback_data="clone_give")],
                   [InlineKeyboardButton(text="🔙 Back", callback_data="home")]]))

@dp.callback_query(F.data == "clone_give")
async def clone_give_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != (CLONE_OWNER_ID if CLONE_MODE else OWNER_ID):
        return await call.answer("No access.", show_alert=True)
    await state.set_state(Form.premium_user)
    await edit(call, "User ID aur days bhejo: <code>123456789 30</code>", kb([[InlineKeyboardButton(text="Cancel", callback_data="home")]]))

@dp.callback_query(F.data == "broadcast")
async def broadcast_start(call: CallbackQuery, state: FSMContext):
    if CLONE_MODE or call.from_user.id != OWNER_ID:
        return await call.answer("No access.", show_alert=True)
    await state.set_state(Form.broadcast)
    await edit(call, "Broadcast message bhejo.", kb([[InlineKeyboardButton(text="Cancel", callback_data="admin")]]))

@dp.message(Form.broadcast)
async def broadcast_send(msg: Message, state: FSMContext):
    if CLONE_MODE or msg.from_user.id != OWNER_ID:
        return
    with db() as c:
        ids = [r["user_id"] for r in c.execute("SELECT user_id FROM users").fetchall()]
    sent = 0
    for uid in ids:
        try:
            await msg.copy_to(uid)
            sent += 1
            await asyncio.sleep(0.04)
        except Exception:
            pass
    await state.clear()
    await msg.answer(f"Broadcast sent: {sent}", reply_markup=admin_kb())

# ================================ STATUS =====================================
@dp.callback_query(F.data == "status")
async def status_cb(call: CallbackQuery):
    cpu = psutil.cpu_percent()
    ram = psutil.virtual_memory().percent
    text = f"📊 <b>Server Status</b>\nCPU: {cpu}%\nRAM: {ram}%"
    await edit(call, text, kb([[InlineKeyboardButton(text="🔙 Back", callback_data="home")]]))

# ================================ RUN ========================================
async def main():
    init_db()
    if not CLONE_MODE:
        await launch_saved_clones()
    log.info("ARYAN HOST %s started", "CLONE" if CLONE_MODE else "MAIN")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
