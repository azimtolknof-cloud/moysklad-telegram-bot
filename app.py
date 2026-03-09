import os
import time
import hmac
import hashlib
import sqlite3
import threading
from datetime import datetime, timedelta

import requests
from flask import Flask, request, abort

app = Flask(__name__)

# =========================
# ENV
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPERATOR_CHAT_ID = os.getenv("OPERATOR_CHAT_ID", "5086400903")

MOYSKLAD_TOKEN = os.getenv("MOYSKLAD_TOKEN", "")
MOYSKLAD_WEBHOOK_SECRET = os.getenv("MOYSKLAD_WEBHOOK_SECRET", "")

BASE_URL = os.getenv("BASE_URL", "")  # https://xxxx.onrender.com
DB_PATH = os.getenv("DB_PATH", "data.sqlite3")

MS_API_BASE = "https://online.moysklad.ru/api/remap/1.2"
MS_DEMAND_ENDPOINT = "/entity/demand"   # Отгрузка
MS_CASHIN_ENDPOINT = "/entity/cashin"   # Приходный ордер (keyin tekshiramiz)

# =========================
# DB
# =========================
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS clients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_chat_id TEXT UNIQUE,
        phone TEXT,
        counterparty_id TEXT,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS pending (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT,                    -- shipment | payment
        ms_entity TEXT,               -- demand | cashin
        ms_id TEXT,
        counterparty_id TEXT,
        amount_minor INTEGER,
        currency TEXT,
        status TEXT,                  -- pending | approved | rejected | expired
        created_at TEXT,
        reminded INTEGER DEFAULT 0,
        tg_chat_id TEXT,
        tg_message_id TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT,                    -- shipment | payment
        ms_id TEXT,
        counterparty_id TEXT,
        amount_minor INTEGER,
        currency TEXT,
        created_at TEXT
    )
    """)

    conn.commit()
    conn.close()

init_db()

# =========================
# Utils
# =========================
def normalize_phone(raw: str) -> str:
    """+998 77 252 60 60 -> 998772526060"""
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if digits.startswith("0"):
        digits = digits[1:]
    return digits

def amount_to_text(amount_minor: int, currency: str):
    v = amount_minor / 100.0
    return f"{v:,.2f} {currency}".replace(",", " ")

# =========================
# Telegram helpers
# =========================
def tg_api(method: str, payload: dict):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN env yo‘q")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()

def tg_send(chat_id: str, text: str, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_api("sendMessage", payload)

def tg_answer_callback(callback_id: str, text: str):
    return tg_api("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

# =========================
# MoySklad helpers (TOKEN AUTH)
# =========================
def ms_headers():
    if not MOYSKLAD_TOKEN:
        raise RuntimeError("MOYSKLAD_TOKEN env yo‘q")
    return {
        "Authorization": f"Bearer {MOYSKLAD_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

def ms_get(path: str, params: dict | None = None):
    url = MS_API_BASE + path
    r = requests.get(url, headers=ms_headers(), params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def ms_put(path: str, data: dict):
    url = MS_API_BASE + path
    r = requests.put(url, headers=ms_headers(), json=data, timeout=30)
    r.raise_for_status()
    return r.json()

def verify_ms_signature(raw_body: bytes, header_sig: str) -> bool:
    if not MOYSKLAD_WEBHOOK_SECRET:
        return True
    mac = hmac.new(MOYSKLAD_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, header_sig)

def ms_find_counterparty_by_phone(phone_raw: str):
    """
    MoySklad Counterparty’da 'phone' field bo‘yicha topamiz.
    Har xil format bo‘lgani uchun bir nechta variant bilan qidiramiz.
    """
    ph = normalize_phone(phone_raw)
    if not ph:
        return None

    candidates = []
    # ko‘p uchraydigan formatlar
    if ph.startswith("998"):
        candidates.append("+" + ph)         # +9987725...
        candidates.append(ph)               # 9987725...
        candidates.append(ph[3:])           # 7725...
    else:
        candidates.append(ph)

    for val in candidates:
        # MoySklad filter sintaksisi: filter=phone=<value>
        try:
            res = ms_get("/entity/counterparty", params={"filter": f"phone={val}", "limit": 1})
            rows = res.get("rows", [])
            if rows:
                return rows[0]  # dict
        except Exception:
            pass

    # fallback: search (nom/telefon bo‘yicha umumiy qidiruv)
    try:
        res = ms_get("/entity/counterparty", params={"search": phone_raw, "limit": 1})
        rows = res.get("rows", [])
        if rows:
            return rows[0]
    except Exception:
        pass

    return None

# =========================
# Business logic (DB)
# =========================
def find_client_by_counterparty(counterparty_id: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM clients WHERE counterparty_id = ?", (counterparty_id,))
    row = cur.fetchone()
    conn.close()
    return row

def create_pending(kind, ms_entity, ms_id, counterparty_id, amount_minor, currency, tg_chat_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO pending (kind, ms_entity, ms_id, counterparty_id, amount_minor, currency, status, created_at, tg_chat_id)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
    """, (kind, ms_entity, ms_id, counterparty_id, int(amount_minor), currency, datetime.utcnow().isoformat(), str(tg_chat_id)))
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid

def mark_pending(pid: int, status: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE pending SET status = ? WHERE id = ?", (status, pid))
    conn.commit()
    conn.close()

def write_ledger(kind, ms_id, counterparty_id, amount_minor, currency):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO ledger (kind, ms_id, counterparty_id, amount_minor, currency, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (kind, ms_id, counterparty_id, int(amount_minor), currency, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def calc_debt(counterparty_id: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT kind, amount_minor, currency FROM ledger WHERE counterparty_id = ?", (counterparty_id,))
    rows = cur.fetchall()
    conn.close()

    sums = {}
    for r in rows:
        curcy = r["currency"] or "UZS"
        sums.setdefault(curcy, 0)
        if r["kind"] == "shipment":
            sums[curcy] += int(r["amount_minor"])
        elif r["kind"] == "payment":
            sums[curcy] -= int(r["amount_minor"])
    return sums

def client_by_chat(chat_id: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM clients WHERE tg_chat_id = ?", (chat_id,))
    row = cur.fetchone()
    conn.close()
    return row

# =========================
# Routes
# =========================
@app.get("/")
def index():
    return {
        "ok": True,
        "endpoints": {
            "health": "/health",
            "telegram_webhook": "/telegram (POST)",
            "moysklad_webhook": "/moysklad/webhook (POST)"
        }
    }

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/telegram")
def telegram_webhook():
    upd = request.json or {}

    # callback buttons
    if "callback_query" in upd:
        cq = upd["callback_query"]
        data = cq.get("data", "")
        cb_id = cq["id"]
        from_chat_id = str(cq["from"]["id"])

        try:
            action, pid_s = data.split(":")
            pid = int(pid_s)
        except Exception:
            tg_answer_callback(cb_id, "Noto‘g‘ri tugma")
            return {"ok": True}

        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM pending WHERE id = ?", (pid,))
        p = cur.fetchone()
        conn.close()

        if not p:
            tg_answer_callback(cb_id, "Topilmadi")
            return {"ok": True}
        if p["status"] != "pending":
            tg_answer_callback(cb_id, "Bu allaqachon yakunlangan")
            return {"ok": True}

        if action == "approve":
            mark_pending(pid, "approved")
            write_ledger(p["kind"], p["ms_id"], p["counterparty_id"], p["amount_minor"], p["currency"])

            # MoySklad’da hujjatni o‘tkazish
            try:
                if p["ms_entity"] == "demand":
                    ms_put(f"{MS_DEMAND_ENDPOINT}/{p['ms_id']}", {"applicable": True})
                elif p["ms_entity"] == "cashin":
                    ms_put(f"{MS_CASHIN_ENDPOINT}/{p['ms_id']}", {"applicable": True})
            except Exception:
                tg_send(OPERATOR_CHAT_ID, f"⚠️ MoySklad update xato. ms_id={p['ms_id']}")

            sums = calc_debt(p["counterparty_id"])
            lines = [f"- {amount_to_text(minor, curcy)}" for curcy, minor in sums.items()]
            debt_text = "\n".join(lines) if lines else "0"

            tg_answer_callback(cb_id, "Tasdiqlandi ✅")
            tg_send(from_chat_id, f"✅ Tasdiqlandi.\nHozirgi qarzingiz:\n{debt_text}")
            tg_send(OPERATOR_CHAT_ID, f"✅ Mijoz tasdiqladi: {p['kind']} / ms_id={p['ms_id']}")
        else:
            mark_pending(pid, "rejected")
            tg_answer_callback(cb_id, "Rad etildi ❌")
            tg_send(from_chat_id, "❌ Rad etildi. Operator siz bilan bog‘lanadi.")
            tg_send(OPERATOR_CHAT_ID, f"❌ Mijoz rad etdi: {p['kind']} / ms_id={p['ms_id']}")

        return {"ok": True}

    msg = upd.get("message")
    if not msg:
        return {"ok": True}

    chat_id = str(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()

    if text in ["/start", "/help"]:
        kb = {
            "keyboard": [[{"text": "📲 Telefon raqamni yuborish", "request_contact": True}]],
            "resize_keyboard": True,
            "one_time_keyboard": True
        }
        tg_send(chat_id, "Assalomu alaykum! Telefon raqamingizni yuboring (verifikatsiya uchun).", kb)
        return {"ok": True}

    # contact sharing
    if "contact" in msg:
        phone = msg["contact"].get("phone_number", "")

        # 1) DB ga yozamiz
        conn = db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO clients (tg_chat_id, phone, counterparty_id, created_at)
            VALUES (?, ?, NULL, ?)
            ON CONFLICT(tg_chat_id) DO UPDATE SET phone=excluded.phone
        """, (chat_id, phone, datetime.utcnow().isoformat()))
        conn.commit()
        conn.close()

        # 2) MoySklad’dan telefon bo‘yicha kontragent qidiramiz
        cp = None
        try:
            cp = ms_find_counterparty_by_phone(phone)
        except Exception:
            cp = None

        if cp and cp.get("id"):
            counterparty_id = cp["id"]

            conn = db()
            cur = conn.cursor()
            cur.execute("UPDATE clients SET counterparty_id=? WHERE tg_chat_id=?", (counterparty_id, chat_id))
            conn.commit()
            conn.close()

            tg_send(chat_id, f"✅ Telefon qabul qilindi: {phone}\n✅ MoySklad’da kontragent topildi va bog‘landi.")
            tg_send(OPERATOR_CHAT_ID, f"✅ Avto-bog‘landi: chat_id={chat_id}, phone={phone}, counterparty_id={counterparty_id}")
        else:
            tg_send(chat_id, f"✅ Telefon qabul qilindi: {phone}\n⚠️ MoySklad’da bu telefon bilan kontragent topilmadi. Operator bog‘laydi.")
            tg_send(OPERATOR_CHAT_ID, f"📲 Telefon verifikatsiya (topilmadi): chat_id={chat_id}, phone={phone}")

        return {"ok": True}

    if text.lower() in ["qarzim", "/debt", "/balance", "balance"]:
        c = client_by_chat(chat_id)
        if not c or not c["counterparty_id"]:
            tg_send(chat_id, "Hali MoySklad kontragentga bog‘lanmagansiz. /start → telefon yuboring.")
            return {"ok": True}

        sums = calc_debt(c["counterparty_id"])
        lines = [f"- {amount_to_text(minor, curcy)}" for curcy, minor in sums.items()]
        debt_text = "\n".join(lines) if lines else "0"
        tg_send(chat_id, f"📌 Hozirgi qarzingiz:\n{debt_text}")
        return {"ok": True}

    tg_send(chat_id, "Buyruqlar: /start, /balance")
    return {"ok": True}

@app.post("/moysklad/webhook")
def moysklad_webhook():
    raw = request.get_data()
    sig = request.headers.get("X-Lognex-Signature", "")
    if not verify_ms_signature(raw, sig):
        abort(401)

    payload = request.json or {}
    events = payload.get("events", [])
    if not isinstance(events, list):
        return {"ok": True}

    for ev in events:
        meta = (ev.get("meta") or {})
        href = meta.get("href", "")
        if not href:
            continue

        try:
            path = href.split("/api/remap/1.2")[-1]
        except Exception:
            continue

        try:
            doc = ms_get(path)
        except Exception:
            continue

        agent = doc.get("agent") or {}
        cp_meta = (agent.get("meta") or {})
        cp_href = cp_meta.get("href", "")
        counterparty_id = cp_href.rstrip("/").split("/")[-1] if cp_href else None
        if not counterparty_id:
            continue

        client = find_client_by_counterparty(counterparty_id)
        if not client:
            tg_send(OPERATOR_CHAT_ID, f"⚠️ Kontragent Telegramga bog‘lanmagan. counterparty_id={counterparty_id}")
            continue

        tg_chat_id = client["tg_chat_id"]

        ms_entity = path.split("/entity/")[-1].split("/")[0]
        ms_id = doc.get("id") or path.rstrip("/").split("/")[-1]

        amount_minor = int(doc.get("sum") or 0)
        curcy = "UZS"
        rate = doc.get("rate")
        if isinstance(rate, dict):
            cur = rate.get("currency")
            if isinstance(cur, dict):
                curcy = cur.get("isoCode", "UZS")

        if ms_entity == "demand":
            kind = "shipment"
            title = "🧾 Yangi OTGRUZKA"
        elif ms_entity == "cashin":
            kind = "payment"
            title = "💰 Yangi TO‘LOV (Приходный орder)"
        else:
            continue

        pid = create_pending(kind, ms_entity, ms_id, counterparty_id, amount_minor, curcy, tg_chat_id)

        amount_txt = amount_to_text(amount_minor, curcy)
        text = f"{title}\nSumma: {amount_txt}\nTasdiqlaysizmi?"
        markup = {
            "inline_keyboard": [[
                {"text": "✅ Ha, tasdiqlayman", "callback_data": f"approve:{pid}"},
                {"text": "❌ Yo‘q, rad etaman", "callback_data": f"reject:{pid}"}
            ]]
        }
        try:
            res = tg_send(tg_chat_id, text, markup)
            msg_id = str(res["result"]["message_id"])
            conn = db()
            cur = conn.cursor()
            cur.execute("UPDATE pending SET tg_message_id=? WHERE id=?", (msg_id, pid))
            conn.commit()
            conn.close()
        except Exception:
            tg_send(OPERATOR_CHAT_ID, f"⚠️ Telegramga yuborilmadi. pending_id={pid}")

    return {"ok": True}

# =========================
# Reminder loop (4 soat o‘tsa operatorga)
# =========================
def reminder_worker():
    while True:
        try:
            conn = db()
            cur = conn.cursor()
            cur.execute("SELECT * FROM pending WHERE status='pending' AND reminded=0")
            rows = cur.fetchall()
            now = datetime.utcnow()
            for r in rows:
                created = datetime.fromisoformat(r["created_at"])
                if now - created >= timedelta(hours=4):
                    tg_send(OPERATOR_CHAT_ID, f"⏰ 4 soat bo‘ldi, mijoz tasdiqlamadi. pending_id={r['id']} ms_id={r['ms_id']}")
                    cur.execute("UPDATE pending SET reminded=1 WHERE id=?", (r["id"],))
            conn.commit()
            conn.close()
        except Exception:
            pass
        time.sleep(300)

threading.Thread(target=reminder_worker, daemon=True).start()
