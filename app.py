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
OPERATOR_CHAT_ID = str(os.getenv("OPERATOR_CHAT_ID", "")).strip()  # operator telegram chat_id
MOYSKLAD_TOKEN = os.getenv("MOYSKLAD_TOKEN", "").strip()  # MoySklad API token (Bearer)
MOYSKLAD_WEBHOOK_SECRET = os.getenv("MOYSKLAD_WEBHOOK_SECRET", "").strip()  # ixtiyoriy

# MoySklad API base: sizda ishlagani shu bo'lyapti
MS_API_BASE_PRIMARY = os.getenv("MS_API_BASE", "https://api.moysklad.ru/api/remap/1.2").strip()
MS_API_BASE_FALLBACK = "https://online.moysklad.ru/api/remap/1.2"  # ayrim akkauntlarda 410 bo'ladi

DB_PATH = os.getenv("DB_PATH", "data.sqlite3").strip()

MS_DEMAND_ENDPOINT = "/entity/demand"   # Отгрузка
MS_CASHIN_ENDPOINT = "/entity/cashin"   # Приходный ордер

# MUHIM: MoySklad sizda aynan shuni talab qilyapti (error 1062)
ACCEPT_JSON_UTF8 = "application/json;charset=utf-8"


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
        kind TEXT,
        ms_entity TEXT,
        ms_id TEXT,
        counterparty_id TEXT,
        amount_minor INTEGER,
        currency TEXT,
        status TEXT,
        created_at TEXT,
        reminded INTEGER DEFAULT 0,
        tg_chat_id TEXT,
        tg_message_id TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT,
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
        raise RuntimeError("TELEGRAM_BOT_TOKEN env yo'q")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    r = requests.post(url, json=payload, timeout=25)
    r.raise_for_status()
    return r.json()


def tg_send(chat_id: str, text: str, reply_markup=None, disable_web_page_preview=True):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": disable_web_page_preview,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_api("sendMessage", payload)


def tg_answer_callback(callback_id: str, text: str):
    return tg_api("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


# =========================
# MoySklad helpers (TOKEN)
# =========================
def ms_headers(has_body: bool = False):
    if not MOYSKLAD_TOKEN:
        raise RuntimeError("MOYSKLAD_TOKEN env yo'q")

    h = {
        "Authorization": f"Bearer {MOYSKLAD_TOKEN}",
        "Accept": ACCEPT_JSON_UTF8,  # MUHIM: aynan shu
    }
    if has_body:
        h["Content-Type"] = ACCEPT_JSON_UTF8  # MUHIM: aynan shu
    return h


def ms_request(method: str, path: str, json_body=None, params=None):
    """
    Primary base bilan urinamiz.
    Agar 410 bo'lsa fallback base bilan qayta urinamiz.
    """
    def _do(base: str):
        url = base + path
        return requests.request(
            method=method,
            url=url,
            headers=ms_headers(has_body=json_body is not None),
            params=params,
            json=json_body,
            timeout=35,
        )

    r = _do(MS_API_BASE_PRIMARY)
    if r.status_code == 410:
        r = _do(MS_API_BASE_FALLBACK)

    if not r.ok:
        try:
            detail = r.json()
        except Exception:
            detail = {"text": r.text}
        raise requests.HTTPError(f"MoySklad HTTP {r.status_code}: {detail}", response=r)

    return r.json()


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
def digits_only(s: str) -> str:
    return re.sub(r"\D+", "", s or "")


def normalize_phone(phone_raw: str) -> str:
    d = digits_only(phone_raw)
    if not d:
        return ""

    # 77 265 60 50 => 772656050 (9)
    if len(d) == 9:
        return "998" + d

    # 998772656050
    if len(d) == 12 and d.startswith("998"):
        return d

    # juda uzun bo'lsa oxirgi 12 ni olamiz
    if len(d) > 12:
        tail = d[-12:]
        if tail.startswith("998"):
            return tail

    # 10/11 bo'lsa oxirgi 9 ni olamiz
    if len(d) in (10, 11):
        return "998" + d[-9:]

    return d


def format_pretty_77(p9: str) -> str:
    # p9: 772656050 => "77 265 60 50"
    op = p9[:2]
    rest = p9[2:]
    return f"{op} {rest[0:3]} {rest[3:5]} {rest[5:7]}"


def phone_variants(phone_norm_12: str):
    if not phone_norm_12 or len(phone_norm_12) != 12 or not phone_norm_12.startswith("998"):
        return []

    p9 = phone_norm_12[-9:]          # 772656050
    pretty = format_pretty_77(p9)    # 77 265 60 50

    variants = [
        phone_norm_12,               # 998772656050
        f"+{phone_norm_12}",         # +998772656050
        f"998 {pretty}",             # 998 77 265 60 50
        f"+998 {pretty}",            # +998 77 265 60 50
        p9,                          # 772656050
        pretty,                      # 77 265 60 50
    ]

    out, seen = [], set()
    for v in variants:
        v = v.strip()
        if v and v not in seen:
            out.append(v)
            seen.add(v)
    return out


def amount_to_text(amount_minor: int, currency: str):
    v = amount_minor / 100.0
    return f"{v:,.2f} {currency}".replace(",", " ")


# =========================
# DB helpers
# =========================
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


def find_client_by_counterparty(counterparty_id: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM clients WHERE counterparty_id = ?", (counterparty_id,))
    row = cur.fetchone()
    conn.close()
    return row


# =========================
# MoySklad search logic
# =========================
def ms_search_counterparty(search_value: str):
    return ms_get("/entity/counterparty", params={"search": search_value, "limit": 100})


def ms_find_counterparty_by_phone(phone_norm_12: str):
    variants = phone_variants(phone_norm_12)
    if not variants:
        return {"found": False, "variants": []}

    want12 = phone_norm_12
    want9 = phone_norm_12[-9:]

    uniq = {}
    matched = {}

    for q in variants:
        data = ms_search_counterparty(q)
        for row in data.get("rows", []) or []:
            rid = row.get("id")
            if rid:
                uniq[rid] = row

            phone_field = digits_only(row.get("phone", ""))
            ext_field = digits_only(row.get("externalCode", ""))
            if phone_field.endswith(want9) or phone_field == want12 or ext_field.endswith(want9) or ext_field == want12:
                if rid:
                    matched[rid] = row

        if len(matched) == 1:
            row = list(matched.values())[0]
            return {"found": True, "row": row, "variants": variants, "candidates": list(uniq.values())}

    if len(matched) == 1:
        row = list(matched.values())[0]
        return {"found": True, "row": row, "variants": variants, "candidates": list(uniq.values())}

    return {
        "found": False,
        "variants": variants,
        "matched": list(matched.values()),
        "candidates": list(uniq.values()),
    }


# =========================
# Pending/Ledger (keyin webhook uchun)
# =========================
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


# =========================
# Routes
# =========================
@app.get("/")
def index():
    return {
        "ok": True,
        "endpoints": {
            "health": "/health",
            "debug": "/debug",
            "ms_test": "/ms_test?phone=99877... (GET)",
            "telegram_webhook": "/telegram (POST)",
            "moysklad_webhook": "/moysklad/webhook (POST)"
        }
    }


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/debug")
def debug():
    return {
        "ok": True,
        "has_telegram_token": bool(TELEGRAM_BOT_TOKEN),
        "has_moysklad_token": bool(MOYSKLAD_TOKEN),
        "ms_base_primary": MS_API_BASE_PRIMARY,
        "ms_base_fallback": MS_API_BASE_FALLBACK,
        "accept_header": ACCEPT_JSON_UTF8,
    }


@app.get("/ms_test")
def ms_test():
    phone = request.args.get("phone", "")
    phone_norm = normalize_phone(phone)
    variants = phone_variants(phone_norm)
    out = {"ok": True, "phone": phone, "phone_norm": phone_norm, "variants": variants}

    try:
        res = ms_find_counterparty_by_phone(phone_norm)
        out.update(res)
        if res.get("found"):
            out["counterparty_id"] = res["row"].get("id")
            out["name"] = res["row"].get("name")
            out["ms_phone"] = res["row"].get("phone")
            out["externalCode"] = res["row"].get("externalCode")
    except Exception as e:
        out["ok"] = False
        out["error"] = str(e)

    return out


# =========================
# Telegram webhook
# =========================
@app.post("/telegram")
def telegram_webhook():
    upd = request.json or {}

    msg = upd.get("message")
    if not msg:
        return {"ok": True}

    chat_id = str(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()

    if text == "/whoami":
        tg_send(chat_id, f"chat_id={chat_id}")
        return {"ok": True}

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

        try:
            res = ms_find_counterparty_by_phone(phone_norm)
        except Exception as e:
            tg_send(chat_id, f"✅ Telefon qabul qilindi: {phone_norm}\n⚠️ MoySklad qidiruv xato: {e}")
            if OPERATOR_CHAT_ID:
                tg_send(OPERATOR_CHAT_ID, f"⚠️ MoySklad qidiruv xato.\nchat_id={chat_id}\nphone_norm={phone_norm}\nerr={e}")
            return {"ok": True}

        if res.get("found"):
            row = res["row"]
            cp_id = row.get("id")
            bind_client_to_counterparty(chat_id, cp_id)
            tg_send(chat_id, f"✅ Telefon qabul qilindi: {phone_norm}\n✅ MoySklad kontragent topildi va bog'landi: {row.get('name','')}")
            return {"ok": True}

        tg_send(chat_id, f"✅ Telefon qabul qilindi: {phone_norm}\n⚠️ MoySklad'da bu telefon bilan kontragent topilmadi (yoki bir nechta). Operator bog'laydi.")
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

    tg_send(chat_id, "Buyruqlar: /start, /balance, /whoami")
    return {"ok": True}


# =========================
# MoySklad webhook (keyin ishlatamiz)
# =========================
@app.post("/moysklad/webhook")
def moysklad_webhook():
    raw = request.get_data()
    sig = request.headers.get("X-Lognex-Signature", "")

    if not verify_ms_signature(raw, sig):
        abort(401)

    return {"ok": True}


# =========================
# Reminder loop (hozircha zarar qilmaydi)
# =========================
def reminder_worker():
    while True:
        time.sleep(300)


threading.Thread(target=reminder_worker, daemon=True).start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
