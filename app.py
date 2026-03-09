import os
import time
import hmac
import hashlib
import sqlite3
import threading
from datetime import datetime, timedelta
from urllib.parse import quote

import requests
from flask import Flask, request, abort

app = Flask(__name__)

# ==========================================================
# ENV
# ==========================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPERATOR_CHAT_ID = str(os.getenv("OPERATOR_CHAT_ID", "5086400903"))

MOYSKLAD_TOKEN = os.getenv("MOYSKLAD_TOKEN", "")  # ✅ TOKEN (Bearer)
MOYSKLAD_WEBHOOK_SECRET = os.getenv("MOYSKLAD_WEBHOOK_SECRET", "")  # ixtiyoriy
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")  # https://xxx.onrender.com

DB_PATH = os.getenv("DB_PATH", "data.sqlite3")

MS_API_BASE = "https://online.moysklad.ru/api/remap/1.2"
MS_DEMAND_ENDPOINT = "/entity/demand"   # Отгрузка
MS_CASHIN_ENDPOINT = "/entity/cashin"   # Приходный ордер (sizda boshqacha bo‘lsa moslaymiz)

# ==========================================================
# DB
# ==========================================================
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

# ==========================================================
# Telegram helpers
# ==========================================================
def tg_api(method: str, payload: dict):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN env yo‘q")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()

def tg_send(chat_id: str, text: str, reply_markup=None):
    payload = {"chat_id": str(chat_id), "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_api("sendMessage", payload)

def tg_answer_callback(callback_id: str, text: str):
    return tg_api("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

# ==========================================================
# MoySklad helpers (TOKEN AUTH)
# ==========================================================
def ms_headers():
    """
    MoySklad API token auth:
    Authorization: Bearer <token>
    """
    if not MOYSKLAD_TOKEN:
        raise RuntimeError("MOYSKLAD_TOKEN env yo‘q")
    return {
        "Authorization": f"Bearer {MOYSKLAD_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

def ms_get(url: str):
    full = MS_API_BASE + url
    r = requests.get(full, headers=ms_headers(), timeout=30)
    r.raise_for_status()
    return r.json()

def ms_put(url: str, data: dict):
    full = MS_API_BASE + url
    r = requests.put(full, json=data, headers=ms_headers(), timeout=30)
    r.raise_for_status()
    return r.json()

def verify_ms_signature(raw_body: bytes, header_sig: str) -> bool:
    """
    MoySklad webhook signature (ixtiyoriy).
    Agar secret qo‘yilmasa - tekshirmaymiz.
    """
    if not MOYSKLAD_WEBHOOK_SECRET:
        return True
    mac = hmac.new(MOYSKLAD_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, header_sig)

# ==========================================================
# Phone normalization (UZ)
# ==========================================================
def only_digits(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())

def uz_local9(phone: str) -> str:
    """
    Har qanday formatdan UZ local 9 raqamni chiqarib beradi.
    Misollar:
      +998772656050 -> 772656050
      998 77 265 60 50 -> 772656050
      77 265 60 50 -> 772656050
      772656050 -> 772656050
    """
    d = only_digits(phone)

    # Agar 12 raqamli bo‘lsa (998XXXXXXXXX), oxirgi 9 raqamni olamiz
    if len(d) >= 12 and d.endswith(d[-9:]):
        # 998 bilan boshlansa ham, boshqa prefix bo‘lsa ham oxirgi 9 raqam logik
        return d[-9:]

    # Agar 9 raqam bo‘lsa - local
    if len(d) == 9:
        return d

    # Agar 10-11 bo‘lsa (masalan 877..., 077...) - oxirgi 9 ni olamiz (ko‘pincha shunday keladi)
    if len(d) > 9:
        return d[-9:]

    # Juda qisqa bo‘lsa - qaytaramiz (keyin match scoringda kam ball oladi)
    return d

def make_phone_variants(phone: str):
    """
    Search uchun variantlar ro‘yxati.
    """
    p9 = uz_local9(phone)
    p12 = "998" + p9 if len(p9) == 9 else ""

    variants = []
    if p12:
        variants.append(p12)          # 998772656050
        variants.append("+" + p12)    # +998772656050
        variants.append("998 " + p9)  # 998 772...
    if p9:
        variants.append(p9)           # 772656050
        # 77 265 60 50 ko‘rinish
        if len(p9) == 9:
            variants.append(f"{p9[0:2]} {p9[2:5]} {p9[5:7]} {p9[7:9]}")

    # unique
    seen = set()
    out = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out

# ==========================================================
# Counterparty matching
# ==========================================================
def score_counterparty_match(cp: dict, p9: str, p12: str, tg_chat_id: str = "") -> int:
    """
    Qanchalik mosligini ball bilan baholaymiz.
    Eng yuqori ball — eng yaxshi match.
    """
    score = 0

    cp_phone_raw = cp.get("phone", "") or ""
    cp_phone_digits = only_digits(cp_phone_raw)
    cp_phone_local9 = uz_local9(cp_phone_digits)

    ext = cp.get("externalCode", "") or ""
    ext_digits = only_digits(ext)

    # 1) Phone bo‘yicha
    if p12 and cp_phone_digits == p12:
        score += 100
    if p9 and cp_phone_local9 == p9:
        score += 95
    if p12 and cp_phone_digits.endswith(p12):
        score += 80
    if p9 and cp_phone_digits.endswith(p9):
        score += 75

    # 2) External code bo‘yicha (agar telefon yoki chat_id saqlansa)
    # Telefon sifatida
    if p12 and ext_digits == p12:
        score += 90
    if p9 and ext_digits.endswith(p9) and len(ext_digits) >= 9:
        score += 70

    # chat_id sifatida
    if tg_chat_id and ext_digits == only_digits(tg_chat_id):
        score += 85

    # 3) Qo‘shimcha: search natijasi bo‘lgani uchun ozgina bazaviy ball
    score += 5

    return score

def ms_find_counterparty_by_phone(phone: str, tg_chat_id: str = ""):
    """
    Telefon kelganda:
    1) search=... bilan candidate olish
    2) phone/externalCode bo‘yicha scoring
    3) eng mosini qaytarish
    """
    p9 = uz_local9(phone)
    p12 = "998" + p9 if len(p9) == 9 else ""

    if not p9:
        return None

    variants = make_phone_variants(phone)

    candidates = []
    seen_ids = set()

    # MoySklad search param: /entity/counterparty?search=<text>
    for q in variants:
        try:
            data = ms_get(f"/entity/counterparty?search={quote(q)}")
            rows = data.get("rows", []) if isinstance(data, dict) else []
            for cp in rows:
                cid = cp.get("id")
                if cid and cid not in seen_ids:
                    seen_ids.add(cid)
                    candidates.append(cp)
        except Exception:
            # Bir variant xato bo‘lsa, boshqasini sinayveramiz
            continue

    if not candidates:
        return None

    best = None
    best_score = -1
    for cp in candidates:
        sc = score_counterparty_match(cp, p9, p12, tg_chat_id=tg_chat_id)
        if sc > best_score:
            best_score = sc
            best = cp

    # Juda past ball bo‘lsa, noaniq — baribir qaytaramiz (MVP), operator tekshiradi.
    return best

# ==========================================================
# Ledger / debt
# ==========================================================
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
    """
    shipment => qarz +, payment => qarz -
    """
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

# ==========================================================
# Routes
# ==========================================================
@app.get("/health")
def health():
    return {"ok": True}

@app.get("/debug")
def debug():
    """
    Tez tekshirish:
    /debug?phone=+998772656050
    """
    phone = request.args.get("phone", "")
    if not phone:
        return {"ok": True, "hint": "Use /debug?phone=+998772656050"}

    try:
        cp = ms_find_counterparty_by_phone(phone, tg_chat_id="")  # tg chat optional
    except Exception as e:
        return {"ok": False, "error": str(e)}

    if not cp:
        return {"ok": True, "found": False, "phone": phone, "p9": uz_local9(phone), "variants": make_phone_variants(phone)}

    return {
        "ok": True,
        "found": True,
        "phone": phone,
        "p9": uz_local9(phone),
        "variants": make_phone_variants(phone),
        "counterparty": {
            "id": cp.get("id"),
            "name": cp.get("name"),
            "phone": cp.get("phone"),
            "externalCode": cp.get("externalCode"),
        }
    }

@app.get("/")
def root():
    # Root 404 bo‘lmasin deb qo‘ydim (sizni chalg‘itmasin)
    return {
        "ok": True,
        "endpoints": {
            "health": "/health",
            "debug": "/debug?phone=+998772656050",
            "telegram_webhook": "/telegram (POST)",
            "moysklad_webhook": "/moysklad/webhook (POST)"
        }
    }

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

        def mark_pending(pid_: int, status_: str):
            c = db()
            k = c.cursor()
            k.execute("UPDATE pending SET status=? WHERE id=?", (status_, pid_))
            c.commit()
            c.close()

        if action == "approve":
            mark_pending(pid, "approved")
            write_ledger(p["kind"], p["ms_id"], p["counterparty_id"], p["amount_minor"], p["currency"])

            # applicable=true
            try:
                if p["ms_entity"] == "demand":
                    ms_put(f"{MS_DEMAND_ENDPOINT}/{p['ms_id']}", {"applicable": True})
                elif p["ms_entity"] == "cashin":
                    ms_put(f"{MS_CASHIN_ENDPOINT}/{p['ms_id']}", {"applicable": True})
            except Exception:
                tg_send(OPERATOR_CHAT_ID, f"⚠️ MS update xato. ms_id={p['ms_id']}")

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

    if text == "/start":
        kb = {
            "keyboard": [[{"text": "📲 Telefon raqamni yuborish", "request_contact": True}]],
            "resize_keyboard": True,
            "one_time_keyboard": True
        }
        tg_send(chat_id, "Assalomu alaykum! Telefon raqamingizni yuboring (verifikatsiya uchun).", kb)
        return {"ok": True}

    # contact
    if "contact" in msg:
        phone = msg["contact"].get("phone_number", "")

        # DB save phone
        conn = db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO clients (tg_chat_id, phone, counterparty_id, created_at)
            VALUES (?, ?, NULL, ?)
            ON CONFLICT(tg_chat_id) DO UPDATE SET phone=excluded.phone
        """, (chat_id, phone, datetime.utcnow().isoformat()))
        conn.commit()
        conn.close()

        # Find counterparty in MoySklad
        try:
            cp = ms_find_counterparty_by_phone(phone, tg_chat_id=chat_id)
        except Exception as e:
            tg_send(chat_id, "⚠️ MoySklad bilan bog‘lanishda xato. Operator tekshiradi.")
            tg_send(OPERATOR_CHAT_ID, f"⚠️ MS lookup error: chat_id={chat_id}, phone={phone}, err={e}")
            return {"ok": True}

        if not cp:
            tg_send(chat_id, f"✅ Telefon qabul qilindi: {phone}\n⚠️ MoySklad’da bu telefon bilan kontragent topilmadi. Operator bog‘laydi.")
            tg_send(OPERATOR_CHAT_ID, f"📲 Telefon verifikatsiya (topilmadi): chat_id={chat_id}, phone={phone}")
            return {"ok": True}

        cp_id = cp.get("id")
        cp_name = cp.get("name", "Kontragent")

        conn = db()
        cur = conn.cursor()
        cur.execute("UPDATE clients SET counterparty_id=? WHERE tg_chat_id=?", (cp_id, chat_id))
        conn.commit()
        conn.close()

        tg_send(chat_id, f"✅ Telefon qabul qilindi: {phone}\n✅ MoySklad kontragent topildi: {cp_name}\nEndi /debt (qarzim) ishlaydi.")
        tg_send(OPERATOR_CHAT_ID, f"✅ Telefon bog‘landi: chat_id={chat_id}, phone={phone}, counterparty={cp_name} ({cp_id})")
        return {"ok": True}

    if text.lower() in ["qarzim", "debt", "/debt", "/balance"]:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM clients WHERE tg_chat_id = ?", (chat_id,))
        c = cur.fetchone()
        conn.close()
        if not c or not c["counterparty_id"]:
            tg_send(chat_id, "Hali MoySklad kontragentga bog‘lanmagansiz. /start qilib telefon yuboring.")
            return {"ok": True}

        sums = calc_debt(c["counterparty_id"])
        lines = [f"- {amount_to_text(minor, curcy)}" for curcy, minor in sums.items()]
        debt_text = "\n".join(lines) if lines else "0"
        tg_send(chat_id, f"📌 Hozirgi qarzingiz:\n{debt_text}")
        return {"ok": True}

    tg_send(chat_id, "Buyruqlar: /start, /debt (qarzim)")
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

        # Telegram chat_id must be already linked in DB
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM clients WHERE counterparty_id=?", (counterparty_id,))
        client = cur.fetchone()
        conn.close()

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
            cur_obj = rate.get("currency")
            if isinstance(cur_obj, dict):
                curcy = cur_obj.get("isoCode", "UZS")

        if ms_entity == "demand":
            kind = "shipment"
            title = "🧾 Yangi OTGRUZKA"
        elif ms_entity == "cashin":
            kind = "payment"
            title = "💰 Yangi TO‘LOV (Приходный орder)"
        else:
            continue

        # create pending
        conn = db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO pending (kind, ms_entity, ms_id, counterparty_id, amount_minor, currency, status, created_at, tg_chat_id)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """, (kind, ms_entity, ms_id, counterparty_id, amount_minor, curcy, datetime.utcnow().isoformat(), str(tg_chat_id)))
        pid = cur.lastrowid
        conn.commit()
        conn.close()

        amount_txt = amount_to_text(amount_minor, curcy)
        text = f"{title}\nSumma: {amount_txt}\nTasdiqlaysizmi?"
        markup = {
            "inline_keyboard": [[
                {"text": "✅ Ha, tasdiqlayman", "callback_data": f"approve:{pid}"},
                {"text": "❌ Yo‘q, rad etaman", "callback_data": f"reject:{pid}"}
            ]]
        }

        try:
            tg_send(tg_chat_id, text, markup)
        except Exception:
            tg_send(OPERATOR_CHAT_ID, f"⚠️ Telegramga yuborilmadi. pending_id={pid}, ms_id={ms_id}")

    return {"ok": True}

# ==========================================================
# Reminder loop (4 soat)
# ==========================================================
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
