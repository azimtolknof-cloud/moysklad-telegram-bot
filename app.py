import os
import re
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
# ENV (Render'da qo'yiladi)
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPERATOR_CHAT_ID = str(os.getenv("OPERATOR_CHAT_ID", "")).strip()  # operator tg id
MOYSKLAD_TOKEN = os.getenv("MOYSKLAD_TOKEN", "").strip()          # MoySklad API token (login/parol emas)
MOYSKLAD_WEBHOOK_SECRET = os.getenv("MOYSKLAD_WEBHOOK_SECRET", "").strip()  # ixtiyoriy
DB_PATH = os.getenv("DB_PATH", "data.sqlite3").strip()

# MoySklad base URL
MS_API_BASE_PRIMARY = os.getenv("MS_API_BASE", "https://online.moysklad.ru/api/remap/1.2").strip()
MS_API_BASE_FALLBACK = "https://api.moysklad.ru/api/remap/1.2"

MS_DEMAND_ENDPOINT = "/entity/demand"   # Отгрузка
MS_CASHIN_ENDPOINT = "/entity/cashin"   # Приходный ордер

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
        phone_norm TEXT,
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
# Telegram helpers
# =========================
def tg_api(method: str, payload: dict):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN env yo‘q (Render env ga qo‘ying)")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    r = requests.post(url, json=payload, timeout=25)
    r.raise_for_status()
    return r.json()

def tg_send(chat_id: str, text: str, reply_markup=None, disable_web_page_preview=True):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": disable_web_page_preview
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_api("sendMessage", payload)

def tg_answer_callback(callback_id: str, text: str):
    return tg_api("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

# =========================
# MoySklad helpers (TOKEN)
# =========================
def ms_headers():
    if not MOYSKLAD_TOKEN:
        raise RuntimeError("MOYSKLAD_TOKEN env yo‘q (Render env ga qo‘ying)")

    # MUHIM: MoySklad Accept faqat shuni qabul qiladi (1062 xatoni shu tuzatadi)
    return {
        "Authorization": f"Bearer {MOYSKLAD_TOKEN}",
        "Accept": "application/json;charset=utf-8",
        "Content-Type": "application/json;charset=utf-8",
    }

def ms_request(method: str, path: str, json_body=None, params=None):
    """
    1) Primary base bilan urinadi
    2) Agar 410 bo‘lsa fallback base (api.moysklad.ru) bilan qayta urinadi
    """
    def _do(base):
        url = base + path
        return requests.request(
            method=method,
            url=url,
            headers=ms_headers(),
            params=params,
            json=json_body,
            timeout=35
        )

    r1 = _do(MS_API_BASE_PRIMARY)
    if r1.status_code == 410:
        r2 = _do(MS_API_BASE_FALLBACK)
        r2.raise_for_status()
        return r2.json()

    r1.raise_for_status()
    return r1.json()

def ms_get(path: str, params=None):
    return ms_request("GET", path, params=params)

def ms_put(path: str, data: dict):
    return ms_request("PUT", path, json_body=data)

def verify_ms_signature(raw_body: bytes, header_sig: str) -> bool:
    if not MOYSKLAD_WEBHOOK_SECRET:
        return True
    mac = hmac.new(MOYSKLAD_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, header_sig)

# =========================
# Phone normalize + variants
# =========================
def normalize_phone(phone_raw: str) -> str:
    d = re.sub(r"\D+", "", phone_raw or "")
    if not d:
        return ""
    if len(d) == 9:
        return "998" + d
    if len(d) == 12 and d.startswith("998"):
        return d
    if len(d) > 12:
        tail = d[-12:]
        if tail.startswith("998"):
            return tail
    if len(d) in (10, 11):
        return "998" + d[-9:]
    return d

def phone_variants(phone_norm_12: str):
    if not phone_norm_12 or len(phone_norm_12) < 12:
        return []

    p9 = phone_norm_12[-9:]      # 772656050
    cc = phone_norm_12[:3]       # 998
    op = phone_norm_12[3:5]      # 77
    rest = phone_norm_12[5:]     # 2656050
    pretty = f"{op} {rest[0:3]} {rest[3:5]} {rest[5:7]}"

    variants = [
        phone_norm_12,
        f"+{phone_norm_12}",
        f"{cc} {pretty}",
        f"+{cc} {pretty}",
        p9,
        pretty,
    ]

    out, seen = [], set()
    for v in variants:
        vv = v.strip()
        if vv and vv not in seen:
            out.append(vv)
            seen.add(vv)
    return out

def norm_digits(s: str) -> str:
    return re.sub(r"\D+", "", s or "")

# =========================
# Business logic
# =========================
def find_client_by_counterparty(counterparty_id: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM clients WHERE counterparty_id = ?", (counterparty_id,))
    row = cur.fetchone()
    conn.close()
    return row

def save_client_phone(tg_chat_id: str, phone_raw: str, phone_norm: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO clients (tg_chat_id, phone, phone_norm, counterparty_id, created_at)
        VALUES (?, ?, ?, NULL, ?)
        ON CONFLICT(tg_chat_id) DO UPDATE SET phone=excluded.phone, phone_norm=excluded.phone_norm
    """, (tg_chat_id, phone_raw, phone_norm, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def bind_client_to_counterparty(tg_chat_id: str, counterparty_id: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE clients SET counterparty_id=? WHERE tg_chat_id=?", (counterparty_id, tg_chat_id))
    conn.commit()
    conn.close()

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

def amount_to_text(amount_minor: int, currency: str):
    v = amount_minor / 100.0
    return f"{v:,.2f} {currency}".replace(",", " ")

def ms_find_counterparty_by_phone(phone_norm_12: str):
    variants = phone_variants(phone_norm_12)
    if not variants:
        return {"ok": True, "found": False, "row": None, "variants": []}

    want12 = phone_norm_12
    want9 = phone_norm_12[-9:]

    candidates = []
    for q in variants:
        data = ms_get("/entity/counterparty", params={"search": q, "limit": 100})
        candidates.extend(data.get("rows", []) or [])

    # unique by id
    uniq = {}
    for r in candidates:
        rid = r.get("id")
        if rid:
            uniq[rid] = r
    rows = list(uniq.values())

    matched = []
    for r in rows:
        d_phone = norm_digits(r.get("phone", ""))
        d_ext = norm_digits(r.get("externalCode", ""))

        if d_phone == want12 or d_phone.endswith(want9):
            matched.append(r)
            continue
        if d_ext == want12 or d_ext.endswith(want9):
            matched.append(r)
            continue

    if len(matched) == 1:
        return {"ok": True, "found": True, "row": matched[0], "variants": variants, "candidates": rows}

    return {"ok": True, "found": False, "row": None, "variants": variants, "candidates": rows, "matched": matched}

# =========================
# Routes (debug)
# =========================
@app.get("/")
def index():
    return {
        "ok": True,
        "endpoints": {
            "health": "/health",
            "telegram_webhook": "/telegram (POST)",
            "moysklad_webhook": "/moysklad/webhook (POST)",
        }
    }

@app.get("/health")
def health():
    return {"ok": True}

# =========================
# Telegram webhook
# =========================
@app.post("/telegram")
def telegram_webhook():
    upd = request.json or {}

    try:
        msg = upd.get("message")
        if not msg:
            return {"ok": True}

        chat_id = str(msg["chat"]["id"])
        text = (msg.get("text") or "").strip()

        if text == "/start":
            kb = {
                "keyboard": [[{"text": "📲 Telefon raqamni yuborish", "request_contact": True}]],
                "resize_keyboard": True,
                "one_time_keyboard": True
            }
            tg_send(chat_id, "Assalomu alaykum! Telefon raqamingizni yuboring (verifikatsiya uchun).", kb)
            return {"ok": True}

        if "contact" in msg:
            phone_raw = msg["contact"].get("phone_number", "")
            phone_norm = normalize_phone(phone_raw)
            save_client_phone(chat_id, phone_raw, phone_norm)

            # MS qidirish + bog'lash
            res = ms_find_counterparty_by_phone(phone_norm)
            if res.get("found"):
                row = res["row"]
                cp_id = row["id"]
                bind_client_to_counterparty(chat_id, cp_id)
                name = row.get("name", "Kontragent")
                tg_send(chat_id, f"✅ Telefon qabul qilindi: {phone_norm}\n✅ MoySklad kontragent topildi va bog'landi: {name}")
                if OPERATOR_CHAT_ID:
                    tg_send(OPERATOR_CHAT_ID, f"✅ TOPILDI: chat_id={chat_id}, phone={phone_norm}, counterparty_id={cp_id}, name={name}")
                return {"ok": True}

            tg_send(chat_id, f"✅ Telefon qabul qilindi: {phone_norm}\n⚠️ MoySklad’da bu telefon bilan kontragent topilmadi. Operator bog'laydi.")
            if OPERATOR_CHAT_ID:
                tg_send(OPERATOR_CHAT_ID, f"⚠️ TOPILMADI: chat_id={chat_id}, phone_norm={phone_norm}, variants={res.get('variants')}")
            return {"ok": True}

        if text.lower() in ["/balance", "balance", "qarzim", "/debt", "debt"]:
            conn = db()
            cur = conn.cursor()
            cur.execute("SELECT * FROM clients WHERE tg_chat_id = ?", (chat_id,))
            c = cur.fetchone()
            conn.close()

            if not c or not c["counterparty_id"]:
                tg_send(chat_id, "Hali MoySklad kontragentga bog'lanmagansiz. /start qilib telefon yuboring.")
                return {"ok": True}

            sums = calc_debt(c["counterparty_id"])
            lines = [f"- {amount_to_text(minor, curcy)}" for curcy, minor in sums.items()]
            debt_text = "\n".join(lines) if lines else "0"
            tg_send(chat_id, f"📌 Hozirgi qarzingiz:\n{debt_text}")
            return {"ok": True}

        tg_send(chat_id, "Buyruqlar: /start, /balance")
        return {"ok": True}

    except Exception as e:
        # Telegram webhook hech qachon 500 qaytarmasin (Telegram qayta-qayta yuboradi)
        if OPERATOR_CHAT_ID:
            try:
                tg_send(OPERATOR_CHAT_ID, f"⚠️ Telegram webhook error: {e}")
            except Exception:
                pass
        return {"ok": True}

# =========================
# MoySklad webhook
# =========================
def _ms_type_to_entity(ms_type: str) -> str | None:
    """
    Scenario webhook `type` ni yuborishi mumkin: Demand / CashIn
    Bizga kerak: demand / cashin
    """
    t = (ms_type or "").strip().lower()
    if t == "demand":
        return "demand"
    if t == "cashin":
        return "cashin"
    return None

def _handle_ms_doc_by_path(path: str):
    """
    path: /entity/demand/<id> kabi
    """
    doc = ms_get(path)

    agent = doc.get("agent") or {}
    cp_href = (agent.get("meta") or {}).get("href", "")
    counterparty_id = cp_href.rstrip("/").split("/")[-1] if cp_href else None
    if not counterparty_id:
        return

    client = find_client_by_counterparty(counterparty_id)
    if not client:
        if OPERATOR_CHAT_ID:
            tg_send(OPERATOR_CHAT_ID, f"⚠️ Kontragent Telegramga bog'lanmagan. counterparty_id={counterparty_id}")
        return

    tg_chat_id = client["tg_chat_id"]

    ms_entity = path.split("/entity/")[-1].split("/")[0]  # demand/cashin
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
        title = "💰 Yangi TO‘LOV (Приходный ордер)"
    else:
        return

    pid = create_pending(kind, ms_entity, ms_id, counterparty_id, amount_minor, curcy, tg_chat_id)

    amount_txt = amount_to_text(amount_minor, curcy)
    text = f"{title}\nSumma: {amount_txt}\nTasdiqlaysizmi?"
    markup = {
        "inline_keyboard": [[
            {"text": "✅ Ha, tasdiqlayman", "callback_data": f"approve:{pid}"},
            {"text": "❌ Yo‘q, rad etaman", "callback_data": f"reject:{pid}"}
        ]]
    }
    tg_send(tg_chat_id, text, markup)

@app.post("/moysklad/webhook")
def moysklad_webhook():
    # 1) Signature (ixtiyoriy)
    raw = request.get_data()
    sig = request.headers.get("X-Lognex-Signature", "")
    if not verify_ms_signature(raw, sig):
        abort(401)

    try:
        # 2) Scenario format: /moysklad/webhook?id=...&type=Demand
        q_id = (request.args.get("id") or "").strip()
        q_type = (request.args.get("type") or "").strip()
        if q_id and q_type:
            ent = _ms_type_to_entity(q_type)
            if ent:
                _handle_ms_doc_by_path(f"/entity/{ent}/{q_id}")
            return {"ok": True}

        # 3) Classic events format
        payload = request.json or {}
        events = payload.get("events", [])
        if isinstance(events, list):
            for ev in events:
                href = ((ev.get("meta") or {}).get("href")) or ""
                if not href:
                    continue
                # href bo'lsa path ga aylantiramiz
                if "/api/remap/1.2" in href:
                    path = href.split("/api/remap/1.2")[-1]
                else:
                    # ehtiyot uchun
                    path = href
                if path.startswith("http"):
                    continue
                _handle_ms_doc_by_path(path)

        return {"ok": True}

    except Exception as e:
        # MoySklad ham 500 ko'rmasin
        if OPERATOR_CHAT_ID:
            try:
                tg_send(OPERATOR_CHAT_ID, f"⚠️ MoySklad webhook error: {e}")
            except Exception:
                pass
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
                    if OPERATOR_CHAT_ID:
                        tg_send(OPERATOR_CHAT_ID, f"⏰ 4 soat bo‘ldi, mijoz tasdiqlamadi. pending_id={r['id']} ms_id={r['ms_id']}")
                    cur.execute("UPDATE pending SET reminded=1 WHERE id=?", (r["id"],))
            conn.commit()
            conn.close()
        except Exception:
            pass
        time.sleep(300)

threading.Thread(target=reminder_worker, daemon=True).start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
