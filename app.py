import os
import re
import json
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
OPERATOR_CHAT_ID = str(os.getenv("OPERATOR_CHAT_ID", "5086400903")).strip()

# MoySklad API TOKEN (login/parol EMAS)
MOYSKLAD_TOKEN = os.getenv("MOYSKLAD_TOKEN", "").strip()

# Webhook signature (ixtiyoriy) — faqat POST native webhook bo'lsa
MOYSKLAD_WEBHOOK_SECRET = os.getenv("MOYSKLAD_WEBHOOK_SECRET", "").strip()

DB_PATH = os.getenv("DB_PATH", "data.sqlite3").strip()

# MoySklad base URL:
# Tavsiya: api.moysklad.ru. Ba'zi eski akkauntlarda online.moysklad.ru 410 qaytaradi.
MS_API_BASE_PRIMARY = os.getenv("MS_API_BASE", "https://api.moysklad.ru/api/remap/1.2").strip()
MS_API_BASE_FALLBACK = "https://online.moysklad.ru/api/remap/1.2"

MS_DEMAND_ENDPOINT = "/entity/demand"     # Отгрузка
MS_CASHIN_ENDPOINT = "/entity/cashin"     # Приходный ордер
MS_PAYMENTIN_ENDPOINT = "/entity/paymentin"  # Входящий платеж (agar kerak bo'lsa)

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
        ms_entity TEXT,               -- demand | cashin | paymentin
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
def _ms_auth_header_value() -> str:
    """
    MOYSKLAD_TOKEN ni:
    - agar user env ga "Bearer xxx" deb qo'yib yuborgan bo'lsa ham ishlatadi
    - aks holda "Bearer <token>" qiladi
    """
    t = (MOYSKLAD_TOKEN or "").strip()
    if not t:
        raise RuntimeError("MOYSKLAD_TOKEN env yo‘q (Render env ga qo‘ying)")
    low = t.lower()
    if low.startswith("bearer "):
        return t
    return f"Bearer {t}"

def ms_headers():
    # MoySklad juda talabchan: Accept ni aniq formatda beramiz
    return {
        "Authorization": _ms_auth_header_value(),
        "Accept": "application/json;charset=utf-8",
        "Content-Type": "application/json;charset=utf-8",
    }

def ms_request(method: str, path: str, json_body=None, params=None):
    """
    1) primary base bilan urinadi
    2) agar 410/404 bo'lsa fallback bilan urinadi
    3) agar 401 bo'lsa — token/huquq muammosi (darrov xabar)
    """
    def _do(base: str):
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

    # Token xatosi — shu yerning o'zida aniq chiqaramiz
    if r1.status_code == 401:
        raise RuntimeError("MoySklad 401 Unauthorized: MOYSKLAD_TOKEN noto‘g‘ri yoki huquq yetarli emas.")

    if r1.status_code in (404, 410):
        r2 = _do(MS_API_BASE_FALLBACK)
        if r2.status_code == 401:
            raise RuntimeError("MoySklad 401 Unauthorized: MOYSKLAD_TOKEN noto‘g‘ri yoki huquq yetarli emas.")
        r2.raise_for_status()
        return r2.json()

    # boshqa xatolarni ham matni bilan ko'rsatamiz
    if r1.status_code >= 400:
        try:
            j = r1.json()
            raise RuntimeError(f"MoySklad HTTP {r1.status_code}: {json.dumps(j, ensure_ascii=False)}")
        except Exception:
            raise RuntimeError(f"MoySklad HTTP {r1.status_code}: {r1.text[:500]}")

    return r1.json()

def ms_get(path: str, params=None):
    return ms_request("GET", path, params=params)

def ms_put(path: str, data: dict):
    return ms_request("PUT", path, json_body=data)

def verify_ms_signature(raw_body: bytes, header_sig: str) -> bool:
    if not MOYSKLAD_WEBHOOK_SECRET:
        return True
    mac = hmac.new(MOYSKLAD_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, header_sig or "")

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
    p9 = phone_norm_12[-9:]
    op = phone_norm_12[3:5]
    rest = phone_norm_12[5:]
    pretty = f"{op} {rest[0:3]} {rest[3:5]} {rest[5:7]}"
    variants = [
        phone_norm_12,
        f"+{phone_norm_12}",
        f"998 {pretty}",
        f"+998 {pretty}",
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

def ms_find_counterparty_by_phone(phone_norm_12: str):
    variants = phone_variants(phone_norm_12)
    if not variants:
        return {"found": False, "variants": []}

    want12 = phone_norm_12
    want9 = phone_norm_12[-9:]

    candidates = []
    for q in variants:
        data = ms_get("/entity/counterparty", params={"search": q, "limit": 100})
        candidates.extend(data.get("rows", []) or [])

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
        elif d_ext == want12 or d_ext.endswith(want9):
            matched.append(r)

    if len(matched) == 1:
        return {"found": True, "row": matched[0], "variants": variants}
    return {"found": False, "matched": matched, "variants": variants}

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
    cur.execute("SELECT * FROM clients WHERE counterparty_id=?", (counterparty_id,))
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
    cur.execute("UPDATE pending SET status=? WHERE id=?", (status, pid))
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

def amount_to_text(amount_minor: int, currency: str):
    v = amount_minor / 100.0
    return f"{v:,.2f} {currency}".replace(",", " ")

def calc_debt(counterparty_id: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT kind, amount_minor, currency FROM ledger WHERE counterparty_id=?", (counterparty_id,))
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
# Debug routes
# =========================
@app.get("/")
def index():
    return {
        "ok": True,
        "endpoints": {
            "health": "/health",
            "debug": "/debug",
            "ms_auth": "/debug/ms-auth",
            "telegram_webhook": "/telegram (POST)",
            "moysklad_webhook": "/moysklad/webhook (GET/POST)"
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
        "ms_base_fallback": MS_API_BASE_FALLBACK
    }

@app.get("/debug/ms-auth")
def debug_ms_auth():
    try:
        data = ms_get("/context/employee")
        return {"ok": True, "employee": data.get("name") or True}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# =========================
# Telegram webhook
# =========================
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
        cur.execute("SELECT * FROM pending WHERE id=?", (pid,))
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

            try:
                if p["ms_entity"] == "demand":
                    ms_put(f"{MS_DEMAND_ENDPOINT}/{p['ms_id']}", {"applicable": True})
                elif p["ms_entity"] == "cashin":
                    ms_put(f"{MS_CASHIN_ENDPOINT}/{p['ms_id']}", {"applicable": True})
                elif p["ms_entity"] == "paymentin":
                    ms_put(f"{MS_PAYMENTIN_ENDPOINT}/{p['ms_id']}", {"applicable": True})
            except Exception as e:
                tg_send(OPERATOR_CHAT_ID, f"⚠️ MoySklad update xato. ms_id={p['ms_id']}\n{e}")

            sums = calc_debt(p["counterparty_id"])
            lines = [f"- {amount_to_text(minor, curcy)}" for curcy, minor in sums.items()]
            debt_text = "\n".join(lines) if lines else "0"

            tg_answer_callback(cb_id, "Tasdiqlandi ✅")
            tg_send(from_chat_id, f"✅ Tasdiqlandi.\nHozirgi qarzingiz:\n{debt_text}")
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

    # contact sharing
    if "contact" in msg:
        phone_raw = msg["contact"].get("phone_number", "")
        phone_norm = normalize_phone(phone_raw)
        save_client_phone(chat_id, phone_raw, phone_norm)

        try:
            res = ms_find_counterparty_by_phone(phone_norm)
        except Exception as e:
            tg_send(chat_id, f"✅ Telefon qabul qilindi: {phone_norm}\n⚠️ MoySklad ulanish xato: {e}")
            tg_send(OPERATOR_CHAT_ID, f"⚠️ MoySklad ulanish xato.\nchat_id={chat_id}\nphone={phone_norm}\nerr={e}")
            return {"ok": True}

        if res.get("found"):
            row = res["row"]
            cp_id = row["id"]
            bind_client_to_counterparty(chat_id, cp_id)
            name = row.get("name", "Kontragent")
            tg_send(chat_id, f"✅ Telefon qabul qilindi: {phone_norm}\n✅ MoySklad kontragent topildi va bog'landi: {name}")
            return {"ok": True}

        tg_send(chat_id, f"✅ Telefon qabul qilindi: {phone_norm}\n⚠️ MoySklad’da kontragent topilmadi. Operator bog‘laydi.")
        tg_send(OPERATOR_CHAT_ID, f"📲 Telefon verifikatsiya (TOPILMADI): chat_id={chat_id}, phone_norm={phone_norm}, variants={res.get('variants')}")
        return {"ok": True}

    if text.lower() in ["qarzim", "debt", "/debt", "/balance", "balance"]:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM clients WHERE tg_chat_id=?", (chat_id,))
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

    tg_send(chat_id, "Buyruqlar: /start, /balance, /whoami")
    return {"ok": True}

# =========================
# MoySklad webhook (Scenario GET/Native POST)
# =========================
def _handle_doc_by_id_type(doc_id: str, doc_type: str):
    """
    Scenario odatda ?id=<uuid>&type=<Type> yuboradi.
    Biz shu doc ni API dan olib, kontragentni topib Telegramga yuboramiz.
    """
    if not doc_id or not doc_type:
        return

    t = (doc_type or "").strip().lower()

    # MoySklad Scenario type larini entity ga map qilamiz (case-insensitive)
    type_to_entity = {
        "demand": "demand",
        "cashin": "cashin",
        "paymentin": "paymentin",
        # agar kerak bo'lsa keyin qo'shamiz
    }

    ms_entity = type_to_entity.get(t)
    if not ms_entity:
        # Noma'lum type — hozircha o'tkazib yuboramiz
        return

    if ms_entity == "demand":
        path = f"{MS_DEMAND_ENDPOINT}/{doc_id}"
        kind = "shipment"
        title = "🧾 Yangi OTGRUZKA"
    elif ms_entity == "cashin":
        path = f"{MS_CASHIN_ENDPOINT}/{doc_id}"
        kind = "payment"
        title = "💰 Yangi TO‘LOV (Приходный ордер)"
    else:
        path = f"{MS_PAYMENTIN_ENDPOINT}/{doc_id}"
        kind = "payment"
        title = "💰 Yangi TO‘LOV (Входящий платеж)"

    doc = ms_get(path)

    agent = doc.get("agent") or {}
    cp_href = (agent.get("meta") or {}).get("href", "")
    counterparty_id = cp_href.rstrip("/").split("/")[-1] if cp_href else None
    if not counterparty_id:
        return

    client = find_client_by_counterparty(counterparty_id)
    if not client:
        tg_send(OPERATOR_CHAT_ID, f"⚠️ Kontragent Telegramga bog‘lanmagan. counterparty_id={counterparty_id}")
        return

    tg_chat_id = client["tg_chat_id"]

    amount_minor = int(doc.get("sum") or 0)
    curcy = "UZS"
    rate = doc.get("rate")
    if isinstance(rate, dict):
        cur = rate.get("currency")
        if isinstance(cur, dict):
            curcy = cur.get("isoCode", "UZS")

    pid = create_pending(kind, ms_entity, doc_id, counterparty_id, amount_minor, curcy, tg_chat_id)

    amount_txt = amount_to_text(amount_minor, curcy)
    text = f"{title}\nSumma: {amount_txt}\nTasdiqlaysizmi?"
    markup = {
        "inline_keyboard": [[
            {"text": "✅ Ha, tasdiqlayman", "callback_data": f"approve:{pid}"},
            {"text": "❌ Yo‘q, rad etaman", "callback_data": f"reject:{pid}"}
        ]]
    }
    tg_send(tg_chat_id, text, markup)

@app.route("/moysklad/webhook", methods=["GET", "POST"])
def moysklad_webhook():
    # 1) SCENARIO (odatda GET): ?id=...&type=...
    doc_id = request.args.get("id", "").strip()
    doc_type = request.args.get("type", "").strip()

    # 2) NATIVE WEBHOOK (odatda POST JSON events)
    if request.method == "POST":
        raw = request.get_data()
        sig = request.headers.get("X-Lognex-Signature", "")

        if not verify_ms_signature(raw, sig):
            abort(401)

        payload = request.json or {}
        events = payload.get("events", [])
        if isinstance(events, list) and events:
            # eski format: events -> meta.href dan hujjatni topish
            for ev in events:
                meta = (ev.get("meta") or {})
                href = meta.get("href", "")
                if not href:
                    continue
                try:
                    path = href.split("/api/remap/1.2")[-1]
                    doc = ms_get(path)
                except Exception:
                    continue

                agent = doc.get("agent") or {}
                cp_href = (agent.get("meta") or {}).get("href", "")
                counterparty_id = cp_href.rstrip("/").split("/")[-1] if cp_href else None
                if not counterparty_id:
                    continue

                client = find_client_by_counterparty(counterparty_id)
                if not client:
                    tg_send(OPERATOR_CHAT_ID, f"⚠️ Kontragent Telegramga bog‘lanmagan. counterparty_id={counterparty_id}")
                    continue

                tg_chat_id = client["tg_chat_id"]
                ms_entity = path.split("/entity/")[-1].split("/")[0].lower()
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
                elif ms_entity in ("cashin", "paymentin"):
                    kind = "payment"
                    title = "💰 Yangi TO‘LOV"
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
                tg_send(tg_chat_id, text, markup)

            return {"ok": True}

    # Scenario GET bo'lsa shu yerga tushadi
    if doc_id and doc_type:
        _handle_doc_by_id_type(doc_id, doc_type)
        return {"ok": True, "mode": "scenario", "id": doc_id, "type": doc_type}

    return {"ok": True}

# =========================
# Reminder loop
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

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
