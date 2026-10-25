"""
NhanDanhSap Auto Cược & Key Management System:
- Single Device Locking (HWID / Device Binding)
- Auto-Expiration Engine
- Auto Keep-Alive 24/24 (Anti-Sleep)
- Updated Prediction APIs
"""
import base64
import hashlib
import json
import logging
import os
import random
import secrets
import string
import threading
import time
from collections import deque
from datetime import datetime, timedelta

import requests
import socketio
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
from flask import Flask, jsonify, request, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=True, PERMANENT_SESSION_LIFETIME=86400)

BOT_TOKEN = '8545966200:AAFWHF2rZppjoMX27bGichmeaRgoqUWCzds'
ADMIN_ID = 8375848425
ADMIN_CONTACT = '@nhan161019'
BOT_USERNAME = 'taixiuvipallgame_bot'

BANK_BIN = 'BIDV'
BANK_ACC = '8887596710'
BANK_NAME = 'NGUYEN THANH NHAN'

bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML')
DB_FILE = os.path.join(BASE_DIR, 'database.json')

KEY_PRICES = {
    1: {"name": "1 Ngày", "price": 45000},
    3: {"name": "3 Ngày", "price": 75000},
    7: {"name": "1 Tuần", "price": 110000},
    30: {"name": "1 Tháng", "price": 170000},
    365: {"name": "Vĩnh Viễn (VV)", "price": 250000},
}

db_lock = threading.Lock()

def load_db():
    default = {"users": {}, "keys": {}}
    with db_lock:
        if not os.path.exists(DB_FILE):
            return default
        try:
            with open(DB_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for k in default: data.setdefault(k, default[k])
                return data
        except Exception as e:
            logger.error("Lỗi đọc DB: %s", e)
            return default

def save_db(data):
    with db_lock:
        try:
            with open(DB_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error("Lỗi ghi DB: %s", e)

def get_device_id():
    ua = request.headers.get('User-Agent', '')
    ip = request.remote_addr or '127.0.0.1'
    return hashlib.sha256(f"{ua}_{ip}".encode()).hexdigest()[:16]

users = {}
users_lock = threading.RLock()

# CẬP NHẬT ĐƯỜNG DẪN API DỰ ĐOÁN MỚI
TABLES = {
    "tx": {"label": "Hũ Tài Xỉu", "host": "https://wtx.tele68.com", "path": "tx/", "namespace": "/tx", "api_url": "https://dungloapinuamaythangnguconmemay.onrender.com/lc79hu"},
    "md5": {"label": "MD5", "host": "https://wtxmd52.tele68.com", "path": "txmd5/", "namespace": "/txmd5", "api_url": "https://dungloapinuamaythangnguconmemay.onrender.com/lc79-md5"},
}

def _new_table_state():
    return {
        "connected": False, "session_id": None, "tick": None, "game_state": None,
        "last_result": None, "history": deque(maxlen=100), "bet_logs": deque(maxlen=50), "last_event_at": None, "error": None,
        "auto_enabled": False, "bet_amount": 2000, "target_profit": 200000,
        "bet_mode": "fixed", "max_loss": 5, "profit_loss": 0, "win_streak": 0,
        "current_prediction": "TAI", "confidence": "—", "has_bet_this_session": False, "waiting_for_result": False
    }

def _public_state(user):
    with user["lock"]:
        return {
            "nickname": user.get("nickname"), "balance": user.get("balance"),
            "logged_in": True,
            "tables": {key: {**{k: v for k, v in state.items() if k not in ("history", "bet_logs")},
                             "history": list(state["history"]), "bet_logs": list(state["bet_logs"])}
                       for key, state in user["tables"].items()},
        }

def _login_tele68(username, password):
    pw_md5 = hashlib.md5(password.encode("utf-8")).hexdigest()
    params = {"c": 3, "un": username, "pw": pw_md5, "cp": "R", "cl": "R", "pf": "web", "at": ""}
    r = requests.get("https://apifo88daigia.tele68.com/api", params=params, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise ValueError(str(data.get("message") or "Đăng nhập thất bại. Kiểm tra tài khoản/mật khẩu."))
    if not data.get("sessionKey") or not data.get("accessToken"):
        raise ValueError("API đăng nhập thiếu sessionKey/accessToken.")
    
    key = data["sessionKey"].replace("-", "+").replace("_", "/")
    key += "=" * ((4 - len(key) % 4) % 4)
    try:
        session_data = json.loads(base64.b64decode(key).decode("utf-8"))
    except Exception:
        try:
            session_data = json.loads(base64.urlsafe_b64decode(key).decode("utf-8"))
        except Exception as exc:
            raise ValueError("Không đọc được sessionKey từ API.") from exc

    nickname = session_data.get("nickname") or session_data.get("nickName")
    if not nickname:
        raise ValueError("Không tìm thấy nickname trong sessionKey.")
    r2 = requests.post(
        "https://wlb.tele68.com/v1/lobby/auth/login?cp=R&cl=R&pf=web&at=",
        headers={"content-type": "application/json", "User-Agent": "Mozilla/5.0"},
        json={"nickName": nickname, "accessToken": data["accessToken"]}, timeout=15)
    r2.raise_for_status()
    lobby = r2.json()
    token = lobby.get("token")
    if not token:
        raise ValueError("Lobby không trả về token.")
    money = (lobby.get("remoteLoginResp") or {}).get("money", 0)
    return {"token": token, "nickname": nickname, "balance": money}

def _fetch_external_prediction(table_key):
    config = TABLES.get(table_key)
    if not config or not config.get("api_url"):
        return "TAI", "—"
    try:
        r = requests.get(config["api_url"], timeout=5)
        r.raise_for_status()
        data = r.json()
        du_doan = str(data.get("du_doan", "Tài")).strip().lower()
        pred = "XIU" if "xỉu" in du_doan or "xiu" in du_doan else "TAI"
        confidence = data.get("do_tin_cay", "—")
        return pred, confidence
    except Exception:
        return "TAI", "—"

def _connect_table_socket(user_id, table_key, token):
    config = TABLES[table_key]
    with users_lock:
        user = users.get(user_id)
    if not user: return
    sio = socketio.Client(reconnection=True, reconnection_attempts=15, logger=False, engineio_logger=False)
    user["sockets"][table_key] = sio
    state = user["tables"][table_key]
    ns = config["namespace"]

    @sio.event(namespace=ns)
    def connect():
        with user["lock"]: state["connected"] = True

    @sio.event(namespace=ns)
    def disconnect():
        with user["lock"]: state["connected"] = False

    @sio.on("new-session", namespace=ns)
    def on_new_session(data):
        data = data if isinstance(data, dict) else {}
        with user["lock"]:
            state["session_id"] = data.get("id")
            state["has_bet_this_session"] = False
            if state["auto_enabled"] and state["target_profit"] is not None:
                if state["profit_loss"] >= state["target_profit"]: state["auto_enabled"] = False
            pred, conf = _fetch_external_prediction(table_key)
            state["current_prediction"] = pred
            state["confidence"] = conf

    @sio.on("tick-update", namespace=ns)
    def on_tick_update(data):
        data = data if isinstance(data, dict) else {}
        with user["lock"]:
            state["tick"] = data.get("tick")
            state["game_state"] = data.get("state")
            is_betting = state["game_state"] in ["BETTING", "betting", "OPEN"] or data.get("canBet") == True
            if state["auto_enabled"] and is_betting and 5 <= state["tick"] <= 45 and not state["has_bet_this_session"]:
                try:
                    sio.emit("bet", {"type": state["current_prediction"], "amount": int(state["bet_amount"])}, namespace=ns)
                    state["has_bet_this_session"] = True
                    sio.emit("get-current-my-info", None, namespace=ns)
                except Exception as exc:
                    logger.error("Lỗi cược: %s", exc)

    @sio.on("bet-result", namespace=ns)
    def on_bet_result(data):
        data = data if isinstance(data, dict) else {}
        if data.get("success") or data.get("status") == 1 or "postBalance" in data or "balance" in data:
            with user["lock"]:
                if "postBalance" in data: user["balance"] = data["postBalance"]
                elif "balance" in data: user["balance"] = data["balance"]
                state["waiting_for_result"] = True

    @sio.on("session-result", namespace=ns)
    def on_session_result(data):
        data = data if isinstance(data, dict) else {}
        result = data.get("resultTruyenThong")
        dices = data.get("dices")
        try: dices = [int(x) for x in dices[:3]] if isinstance(dices, list) and len(dices) >= 3 else None
        except: dices = None
        norm_result = result if result in ("TAI", "XIU") else "HOAN"
        
        with user["lock"]:
            state["last_result"] = {"session_id": state.get("session_id"), "result": norm_result, "dices": dices, "total": sum(dices) if dices else None}
            state["history"].append(state["last_result"])
            pred = state.get("current_prediction")
            bet_amt = int(state.get("bet_amount", 2000))
            is_win = None
            if state.get("waiting_for_result") and pred:
                if norm_result == pred:
                    state["profit_loss"] += int(bet_amt * 0.98)
                    state["win_streak"] += 1
                    if state["bet_mode"] == "martingale": state["bet_amount"] = state.get("base_bet_amount", bet_amt)
                    is_win = True
                elif norm_result in ("TAI", "XIU"):
                    state["profit_loss"] -= bet_amt
                    state["win_streak"] = 0
                    if state["bet_mode"] == "martingale": state["bet_amount"] = bet_amt * 2
                    is_win = False
                state["waiting_for_result"] = False
            state["bet_logs"].appendleft({"session_id": state.get("session_id"), "prediction": pred, "confidence": state.get("confidence"), "bet_amount": bet_amt if state.get("has_bet_this_session") else 0, "result": norm_result, "dices": dices, "total": sum(dices) if dices else None, "is_win": is_win})

    @sio.on("my-info", namespace=ns)
    def on_my_info(data):
        if isinstance(data, dict) and "balance" in data:
            with user["lock"]: user["balance"] = data.get("balance")

    try:
        sio.connect(config["host"], socketio_path=config["path"], transports=["websocket"], auth={"token": token}, headers={"User-Agent": "Mozilla/5.0"})
        sio.wait()
    except Exception as exc:
        logger.warning("Socket failed: %s", exc)
        with user["lock"]: state["connected"] = False

# ==========================================
# TELEGRAM BOT HANDLERS & KEY MANAGEMENT
# ==========================================
@bot.message_handler(commands=['start', 'menu'])
def cmd_start(message):
    user_id = message.from_user.id
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("💳 BẢNG GIÁ & THUÊ KEY", callback_data="btn_thuekey"))
    markup.add(InlineKeyboardButton("📢 KÊNH THÔNG BÁO", url="https://t.me/thongbaotoolgamevip"))
    
    if user_id == ADMIN_ID:
        markup.add(InlineKeyboardButton("👑⚙️ QUẢN TRỊ ADMIN", callback_data="admin_menu"))

    bot.send_message(message.chat.id, f"👋 Xin chào <b>{message.from_user.first_name}</b>!\n🤖 Hệ thống Auto Cược NhanDanhSap chính thức hoạt động.\n\n👇 Bấm nút bên dưới để chọn gói thuê Key:", reply_markup=markup)

@bot.message_handler(commands=['thuekey'])
def cmd_thuekey(message):
    show_pricing_plans(message.chat.id, message.from_user.id)

def show_pricing_plans(chat_id, user_id):
    markup = InlineKeyboardMarkup(row_width=2)
    for days, info in KEY_PRICES.items():
        markup.add(InlineKeyboardButton(f"⚡ {info['name']} — {info['price']:,}đ", callback_data=f"buy_{days}"))
    markup.add(InlineKeyboardButton("🏠 Menu Chính", callback_data="btn_menu"))
    
    text = (f"🔥 <b>BẢNG GIÁ THUÊ KEY VIP</b>\n"
            f"╔══════════════════════════╗\n"
            f"║ ⚡ 1 Ngày ........... 45.000đ\n"
            f"║ 🔥 3 Ngày ........... 75.000đ\n"
            f"║ 🌟 1 Tuần ......... 110.000đ\n"
            f"║ 💎 1 Tháng ........ 170.000đ\n"
            f"║ 👑 Vĩnh Viễn (VV) .. 250.000đ\n"
            f"╚══════════════════════════╝\n\n"
            f"👇 Chọn gói thời gian bạn muốn mua:")
    bot.send_message(chat_id, text, reply_markup=markup)

def send_qr_payment(chat_id, user_id, days):
    info = KEY_PRICES.get(days, KEY_PRICES[30])
    content = f"THUEKEY {user_id} {days}D"
    qr_url = f"https://img.vietqr.io/image/BIDV-8887596710-compact2.png?amount={info['price']}&addInfo={content}&accountName={BANK_NAME}"
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ ĐÃ CHUYỂN KHOẢN", callback_data=f"paid_{user_id}_{days}"))
    markup.add(InlineKeyboardButton("◀️ Chọn lại gói", callback_data="btn_thuekey"))
    
    caption = (f"💎 <b>THANH TOÁN GÓI: {info['name']}</b>\n"
               f"💰 Số tiền cần chuyển: <b>{info['price']:,}đ</b>\n"
               f"🏦 Ngân hàng: <b>{BANK_BIN}</b>\n"
               f"📍 Số TK: <code>{BANK_ACC}</code>\n"
               f"👤 Chủ TK: <b>{BANK_NAME}</b>\n"
               f"📝 Nội dung CK: <code>{content}</code>\n\n"
               f"⚠️ <i>Chuyển khoản chính xác số tiền & nội dung, sau đó bấm nút bên dưới!</i>")
    try:
        bot.send_photo(chat_id, qr_url, caption=caption, reply_markup=markup)
    except:
        bot.send_message(chat_id, caption, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    data = call.data

    if data == "btn_thuekey":
        bot.answer_callback_query(call.id)
        show_pricing_plans(chat_id, user_id)
    elif data == "btn_menu":
        bot.answer_callback_query(call.id)
        cmd_start(call.message)
    elif data == "admin_menu":
        if user_id != ADMIN_ID: return bot.answer_callback_query(call.id, "❌ Không có quyền!", show_alert=True)
        bot.answer_callback_query(call.id)
        admin_markup = InlineKeyboardMarkup(row_width=2)
        admin_markup.add(
            InlineKeyboardButton("➕ Tạo Key 1 Ngày", callback_data="gen_1"),
            InlineKeyboardButton("➕ Tạo Key 3 Ngày", callback_data="gen_3"),
            InlineKeyboardButton("➕ Tạo Key 7 Ngày", callback_data="gen_7"),
            InlineKeyboardButton("➕ Tạo Key 30 Ngày", callback_data="gen_30"),
            InlineKeyboardButton("➕ Tạo Key Vĩnh Viễn", callback_data="gen_365"),
            InlineKeyboardButton("🏠 Menu Chính", callback_data="btn_menu")
        )
        bot.edit_message_text(chat_id=chat_id, message_id=call.message.message_id, text="👑⚙️ <b>QUẢN TRỊ ADMIN PANEL</b>\nChọn mốc thời gian muốn tạo Key nhanh:", reply_markup=admin_markup)
    elif data.startswith("buy_"):
        bot.answer_callback_query(call.id)
        days = int(data.split("_")[1])
        send_qr_payment(chat_id, user_id, days)
    elif data.startswith("paid_"):
        _, target_uid, days = data.split("_")
        days = int(days)
        bot.answer_callback_query(call.id, "✅ Đã gửi báo cáo đến Admin!")
        bot.edit_message_caption(chat_id=chat_id, message_id=call.message.message_id, caption="⏳ <b>ĐÃ GỬI YÊU CẦU XÁC NHẬN!</b>\nAdmin đang kiểm tra bill chuyển khoản và sẽ gửi Key cho bạn ngay.", reply_markup=None)
        
        admin_markup = InlineKeyboardMarkup()
        admin_markup.add(InlineKeyboardButton(f"⚡ Duyệt cấp Key {days} Ngày", callback_data=f"genkey_{target_uid}_{days}"))
        username = f"@{call.from_user.username}" if call.from_user.username else call.from_user.first_name
        bot.send_message(ADMIN_ID, f"🔔 <b>CÓ KHÁCH BÁO ĐÃ CK MUA GÓI {days} NGÀY!</b>\n👤 Khách: {username} (ID: <code>{target_uid}</code>)\n\n👇 Bấm nút để duyệt cấp key:", reply_markup=admin_markup)
    elif data.startswith("gen_"):
        if user_id != ADMIN_ID: return bot.answer_callback_query(call.id, "❌ Không có quyền!", show_alert=True)
        days = int(data.split("_")[1])
        key_code = "AZ-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        
        db = load_db()
        db.setdefault("keys", {})[key_code] = {"days": days, "device_id": None, "activated_at": None, "expiry_at": None}
        save_db(db)
        
        bot.answer_callback_query(call.id, f"✅ Đã tạo thành công!")
        bot.edit_message_text(chat_id=chat_id, message_id=call.message.message_id, text=f"✅ <b>TẠO KEY THÀNH CÔNG:</b>\n🔑 <code>{key_code}</code>\n⏳ Thời hạn: {days} ngày", reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("👑 Quay lại Admin Panel", callback_data="admin_menu")))
    elif data.startswith("genkey_"):
        if user_id != ADMIN_ID: return bot.answer_callback_query(call.id, "❌ Không có quyền!", show_alert=True)
        _, target_uid, days = data.split("_")
        days = int(days)
        key_code = "AZ-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        
        db = load_db()
        db.setdefault("keys", {})[key_code] = {"days": days, "device_id": None, "activated_at": None, "expiry_at": None}
        save_db(db)
        
        bot.answer_callback_query(call.id, f"✅ Đã cấp Key!")
        bot.edit_message_text(chat_id=chat_id, message_id=call.message.message_id, text=f"✅ <b>ĐÃ CẤP KEY CHO ID {target_uid}:</b>\n🔑 <code>{key_code}</code> ({days} ngày)")
        try:
            bot.send_message(target_uid, f"🎉 <b>GIAO DỊCH ĐÃ ĐƯỢC DUYỆT!</b>\n🔑 Key kích hoạt web của bạn:\n<code>{key_code}</code>\n\n👉 Nhập key này vào trang web để mở khóa hệ thống.")
        except: pass

@bot.message_handler(commands=['taokey'])
def cmd_taokey(message):
    if message.from_user.id != ADMIN_ID: return bot.reply_to(message, "❌ Lệnh chỉ dành cho Admin!")
    parts = message.text.split()
    days = int(parts[1]) if len(parts) > 1 else 30
    key_code = "AZ-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    
    db = load_db()
    db.setdefault("keys", {})[key_code] = {"days": days, "device_id": None, "activated_at": None, "expiry_at": None}
    save_db(db)
    
    bot.reply_to(message, f"✅ <b>Tạo Key thành công:</b>\n🔑 <code>{key_code}</code>\n⏳ Thời hạn: {days} ngày")

def run_telegram_bot():
    while True:
        try: bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except: time.sleep(5)

threading.Thread(target=run_telegram_bot, daemon=True).start()

# AUTO KEEP-ALIVE SYSTEM (GIỮ WEB VÀ BOT CHẠY 24/24)
def keep_alive():
    time.sleep(10)
    while True:
        try:
            render_url = os.getenv("RENDER_EXTERNAL_URL")
            if render_url:
                requests.get(f"{render_url}/health", timeout=10)
                logger.info("⚡ Keep-alive ping OK")
        except Exception as e:
            logger.warning("Keep-alive err: %s", e)
        time.sleep(300)

threading.Thread(target=keep_alive, daemon=True).start()

# ==========================================
# FLASK WEB & API ROUTES
# ==========================================
@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")

@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "nhandanhsap-backend"})

@app.post("/api/verify-key")
def api_verify_key():
    body = request.get_json(silent=True) or {}
    key_code = str(body.get("key", "")).strip().upper().replace("–", "-").replace("—", "-")
    if not key_code: return jsonify({"error": "Vui lòng nhập mã Key."}), 400
    
    device_id = get_device_id()
    db = load_db()
    keys_db = db.get("keys", {})
    
    if key_code not in keys_db:
        return jsonify({"error": "Mã Key không tồn tại trên hệ thống!"}), 401
    
    key_info = keys_db[key_code]
    now = datetime.now()
    
    if key_info.get("device_id") and key_info["device_id"] != device_id:
        return jsonify({"error": "❌ Key này đã được kích hoạt trên 1 thiết bị khác!"}), 403
    
    if key_info.get("expiry_at"):
        expiry_time = datetime.fromisoformat(key_info["expiry_at"])
        if now > expiry_time:
            return jsonify({"error": "⏳ Mã Key này đã HẾT HẠN sử dụng!"}), 403
    else:
        days = key_info.get("days", 30)
        expiry_time = now + timedelta(days=days)
        key_info["device_id"] = device_id
        key_info["activated_at"] = now.isoformat()
        key_info["expiry_at"] = expiry_time.isoformat()
        save_db(db)
    
    session["activated"] = True
    session["key"] = key_code
    session["device_id"] = device_id
    session["expiry"] = key_info["expiry_at"]
    session.permanent = True
    
    return jsonify({"ok": True, "message": "Kích hoạt thành công!", "expiry": datetime.fromisoformat(key_info["expiry_at"]).strftime('%d/%m/%Y %H:%M')})

@app.get("/api/check-auth")
def api_check_auth():
    if not session.get("activated") or not session.get("key"):
        return jsonify({"activated": False}), 401
    
    db = load_db()
    key_code = session.get("key")
    key_info = db.get("keys", {}).get(key_code)
    
    if not key_info or not key_info.get("expiry_at"):
        session.clear()
        return jsonify({"activated": False, "error": "Key không hợp lệ!"}), 401
    
    if datetime.now() > datetime.fromisoformat(key_info["expiry_at"]):
        session.clear()
        return jsonify({"activated": False, "error": "Key đã hết hạn!"}), 401
    
    if key_info.get("device_id") != get_device_id():
        session.clear()
        return jsonify({"activated": False, "error": "Truy cập bị từ chối do khác thiết bị!"}), 401

    return jsonify({"activated": True, "expiry": key_info["expiry_at"]})

@app.post("/api/login")
def api_login():
    if not session.get("activated"): return jsonify({"error": "Chưa kích hoạt Key truy cập."}), 401
    body = request.get_json(silent=True) or {}
    username = str(body.get("username", "")).strip()
    password = body.get("password", "")
    if not username or not password: return jsonify({"error": "Nhập tài khoản và mật khẩu."}), 400
    try: auth = _login_tele68(username, password)
    except Exception as exc: return jsonify({"error": str(exc)}), 401

    old_id = session.get("user_id")
    if old_id:
        with users_lock: old = users.pop(old_id, None)
        if old:
            for s in old.get("sockets", {}).values():
                try: s.disconnect()
                except: pass
    
    user_id = secrets.token_urlsafe(24)
    user = {"nickname": auth["nickname"], "balance": auth["balance"], "token": auth["token"],
            "tables": {key: _new_table_state() for key in TABLES}, "sockets": {}, "lock": threading.RLock()}
    with users_lock: users[user_id] = user
    session["user_id"] = user_id
    for key in TABLES:
        threading.Thread(target=_connect_table_socket, args=(user_id, key, auth["token"]), daemon=True).start()
    return jsonify({"ok": True, "nickname": auth["nickname"], "balance": auth["balance"], "message": "Đăng nhập thành công!"})

@app.get("/api/state")
def api_state():
    if not session.get("activated"): return jsonify({"error": "Chưa kích hoạt Key."}), 401
    user_id = session.get("user_id")
    with users_lock: user = users.get(user_id)
    if not user: return jsonify({"logged_in": False}), 401
    return jsonify(_public_state(user))

@app.post("/api/autobet/toggle")
def api_autobet_toggle():
    user_id = session.get("user_id")
    with users_lock: user = users.get(user_id)
    if not user: return jsonify({"error": "Chưa đăng nhập."}), 401
    body = request.get_json(silent=True) or {}
    table_key = body.get("table", "tx")
    enabled = bool(body.get("enabled", False))
    with user["lock"]:
        user["tables"][table_key]["auto_enabled"] = enabled
        user["tables"][table_key]["base_bet_amount"] = user["tables"][table_key]["bet_amount"]
    return jsonify({"ok": True, "auto_enabled": enabled})

@app.post("/api/settings")
def api_settings():
    user_id = session.get("user_id")
    with users_lock: user = users.get(user_id)
    if not user: return jsonify({"error": "Chưa đăng nhập."}), 401
    body = request.get_json(silent=True) or {}
    table_key = body.get("table", "tx")
    with user["lock"]:
        st = user["tables"][table_key]
        if "bet_amount" in body: st["bet_amount"] = max(1, int(body["bet_amount"]))
        if "target_profit" in body: st["target_profit"] = int(body["target_profit"])
        if "bet_mode" in body: st["bet_mode"] = str(body["bet_mode"])
    return jsonify({"ok": True})

@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)
