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
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN", "") or "").strip()
OPERATOR_CHAT_ID = str(os.getenv("OPERATOR_CHAT_ID", "5086400903")).strip()

# MoySklad API TOKEN (login/parol EMAS)
MOYSKLAD_TOKEN = (os.getenv("MOYSKLAD_TOKEN", "") or "").strip()

# Webhook signature (ixtiyoriy)
MOYSKLAD_WEBHOOK_SECRET = (os.getenv("MOYSKLAD_WEBHOOK_SECRET", "") or "").strip()

# Render URL (ixtiyoriy)
BASE_URL = (os.getenv("BASE_URL", "") or "").strip()

DB_PATH = (os.getenv("DB_PATH", "data.sqlite3") or "").strip()

# MoySklad base URL
# Sizning test JSON'laringiz api.moysklad.ru dan kelgan — shuni primary qilamiz.
MS_API_BASE_PRIMARY = (os.getenv("MS_API_BASE", "https://api.moysklad.ru/api/remap/1.2") or "").strip()
MS_API_BASE_FALLBACK = "https://online.moysklad.ru/api/remap/1.2"

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
def _clean_token(t: str) -> str:
    t = (t or "").strip()
    # "Bearer xxx" bo'lib qolsa
    if t.lower().startswith("bearer "):
        t = t.split(" ", 1)[1].strip()
    # "xxx" yoki 'xxx' bo'lib qolsa
    if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'")):
        t = t[1:-1].strip()
    return t

def ms_headers(method: str):
    token = _clean_token(MOYSKLAD_TOKEN)
    if not token:
        raise RuntimeError("MOYSKLAD_TOKEN env yo‘q (Render env ga qo‘ying)")

    h = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    # GET da Content-Type qo'ymaymiz (400 oldini oladi)
    if method.upper() in ("POST", "PUT", "PATCH"):
        h["Content-Type"] = "application/json"
    return h

def ms_request(method: str, path: str, json_body=None, params=None):
    """
    1) Primary base bilan urinadi (api.moysklad.ru)
    2) Agar 410 bo‘lsa fallback (online.moysklad.ru) bilan qayta urinadi
    """
    def _do(base):
        url = base + path
        return requests.request(
            method=method,
            url=url,
            headers=ms_headers(method),
            params=params,
            json=json_body,
            timeout=35
        )

    r1 = _do(MS_API_BASE_PRIMARY)
    if r1.status_code == 410:
        r2 = _do(MS_API_BASE_FALLBACK)
        if not r2.ok:
            raise RuntimeError(f"MoySklad HTTP {r2.status_code}: {r2.text}")
        return r2.json()

    if not r1.ok:
        raise RuntimeError(f"MoySklad HTTP {r1.status_code}: {r1.text}")

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

    p9 = phone_norm_12[-9:]
    cc = phone_norm_12[:3]
    op = phone_norm_12[3:5]
    rest = phone_norm_12[5:]  # 7 ta raqam

    pretty = f"{op} {rest[0:3]} {rest[3:5]} {rest[5:7]}"  # 77 265 60 50

    variants = [
        phone_norm_12,            # 998772656050
        f"+{phone_norm_12}",      # +998772656050
        f"{cc}{op}{rest}",        # 998772656050 (yana)
        f"{cc} {pretty}",         # 998 77 265 60 50
        f"+{cc} {pretty}",        # +998 77 265 60 50
        p9,                       # 772656050
        pretty,                   # 77 265 60 50
        f"{op}{rest}",            # 772656050 (yana)
        f"{op} {rest[0:3]} {rest[3:5]} {rest[5:7]}",  # 77 265 60 50 (yana)
    ]

    out, seen = [], set()
    for v in variants:
        vv = (v or "").strip()
        if vv and vv not in seen:
            out.append(vv)
            seen.add(vv)
    return out

def amount_to_text(amount_minor: int, currency: str):
    v = amount_minor / 100.0
    return f"{v:,.2f} {currency}".replace(",", " ")

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
    """, (kind, ms_entity, ms_id, counterparty_id, int(amount_minor), currency,
          datetime.utcnow().isoformat(), str(tg_chat_id)))
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

def ms_find_counterparty_by_phone(phone_norm_12: str):
    variants = phone_variants(phone_norm_12)
    if not variants:
        return {"ok": True, "found": False, "rows": [], "variants": []}

    want12 = phone_norm_12
    want9 = phone_norm_12[-9:]

    candidates = []
    for q in variants:
        data = ms_get("/entity/counterparty", params={"search": q, "limit": 100})
        for row in data.get("rows", []) or []:
            candidates.append(row)

    uniq = {}
    for r in candidates:
        rid = r.get("id")
        if rid:
            uniq[rid] = r
    rows = list(uniq.values())

    matched = []
    for r in rows:
        phone_field = r.get("phone", "") or ""
        external_code = r.get("externalCode", "") or ""
        d_phone = norm_digits(phone_field)
        d_ext = norm_digits(external_code)

        if d_phone.endswith(want9) or d_phone == want12:
            matched.append(r)
            continue
        if d_ext.endswith(want9) or d_ext == want12:
            matched.append(r)
            continue

    if len(matched) == 1:
        return {"ok": True, "found": True, "row": matched[0], "variants": variants, "candidates": rows}

    return {"ok": True, "found": False, "rows": matched, "variants": variants, "candidates": rows}

# =========================
# Routes (debug)
# =========================
@app.get("/")
def index():
    return {
        "ok": True,
        "endpoints": {
            "health": "/health",
            "debug": "/debug",
            "ms_test": "/ms_test?phone=998772526060",
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
        "has_moysklad_token": bool(_clean_token(MOYSKLAD_TOKEN)),
        "ms_base_primary": MS_API_BASE_PRIMARY,
        "ms_base_fallback": MS_API_BASE_FALLBACK
    }

@app.get("/ms_test")
def ms_test():
    phone = request.args.get("phone", "")
    phone_norm = normalize_phone(phone)
    variants = phone_variants(phone_norm)
    try:
        data = ms_get("/entity/counterparty", params={"search": phone_norm, "limit": 100})
        rows = data.get("rows", []) or []
        return {"ok": True, "phone": phone, "phone_norm": phone_norm, "variants": variants, "rows_count": len(rows), "first": (rows[0] if rows else None)}
    except Exception as e:
        return {"ok": False, "phone": phone, "phone_norm": phone_norm, "variants": variants, "error": str(e)}

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

            try:
                if p["ms_entity"] == "demand":
                    ms_put(f"{MS_DEMAND_ENDPOINT}/{p['ms_id']}", {"applicable": True})
                elif p["ms_entity"] == "cashin":
                    ms_put(f"{MS_CASHIN_ENDPOINT}/{p['ms_id']}", {"applicable": True})
            except Exception as e:
                tg_send(OPERATOR_CHAT_ID, f"⚠️ MoySklad update xato. ms_id={p['ms_id']}\n{e}")

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

    # operator komandalar
    if text.startswith("/bind") and chat_id == OPERATOR_CHAT_ID:
        parts = text.split()
        if len(parts) != 3:
            tg_send(chat_id, "Format: /bind <tg_chat_id> <counterparty_id>")
            return {"ok": True}
        tg_id = parts[1].strip()
        cp_id = parts[2].strip()
        bind_client_to_counterparty(tg_id, cp_id)
        tg_send(chat_id, f"✅ Bog‘landi: tg_chat_id={tg_id} -> counterparty_id={cp_id}")
        tg_send(tg_id, "✅ Operator sizni MoySklad kontragentiga bog‘ladi. Endi /balance ishlaydi.")
        return {"ok": True}

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
            tg_send(chat_id, f"✅ Telefon qabul qilindi: {phone_norm}\n⚠️ MoySklad qidiruv xato: {e}")
            tg_send(OPERATOR_CHAT_ID, f"⚠️ MoySklad qidiruv xato.\nchat_id={chat_id}\nphone={phone_norm}\nerr={e}")
            return {"ok": True}

        if res.get("found"):
            row = res["row"]
            cp_id = row["id"]
            bind_client_to_counterparty(chat_id, cp_id)
            name = row.get("name", "Kontragent")
            tg_send(chat_id, f"✅ Telefon qabul qilindi: {phone_norm}\n✅ MoySklad kontragent topildi va bog‘landi: {name}")
            tg_send(OPERATOR_CHAT_ID, f"✅ Telefon verifikatsiya (TOPILDI): chat_id={chat_id}, phone={phone_norm}, counterparty_id={cp_id}, name={name}")
            return {"ok": True}

        matched = res.get("rows") or []
        variants = res.get("variants") or []

        tg_send(chat_id, f"✅ Telefon qabul qilindi: {phone_norm}\n⚠️ MoySklad’da bu telefon bilan kontragent TOPILMADI (yoki bir nechta chiqdi). Operator bog‘laydi.")
        info = f"📲 Telefon verifikatsiya (TOPILMADI):\nchat_id={chat_id}\nphone_norm={phone_norm}\nvariants={variants}\nmatched_count={len(matched)}"

        if matched:
            lines = []
            for r in matched[:10]:
                lines.append(f"- {r.get('name')} | id={r.get('id')} | phone={r.get('phone')} | externalCode={r.get('externalCode')}")
            info += "\n\nMos kelganlar (matched):\n" + "\n".join(lines)

        info += "\n\nBog‘lash: /bind <tg_chat_id> <counterparty_id>"
        tg_send(OPERATOR_CHAT_ID, info)
        return {"ok": True}

    if text.lower() in ["qarzim", "debt", "/debt", "/balance", "balance"]:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM clients WHERE tg_chat_id = ?", (chat_id,))
        c = cur.fetchone()
        conn.close()

        if not c or not c["counterparty_id"]:
            tg_send(chat_id, "Hali MoySklad kontragentga bog‘lanmagansiz. /start qilib telefon yuboring yoki operator bog‘laydi.")
            return {"ok": True}

        sums = calc_debt(c["counterparty_id"])
        lines = [f"- {amount_to_text(minor, curcy)}" for curcy, minor in sums.items()]
        debt_text = "\n".join(lines) if lines else "0"
        tg_send(chat_id, f"📌 Hozirgi qarzingiz:\n{debt_text}")
        return {"ok": True}

    tg_send(chat_id, "Buyruqlar: /start, /balance, /whoami")
    return {"ok": True}

# =========================
# MoySklad webhook
# =========================
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
        cp_href = (agent.get("meta") or {}).get("href", "")
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
        except Exception as e:
            tg_send(OPERATOR_CHAT_ID, f"⚠️ Telegramga yuborilmadi. pending_id={pid}\n{e}")

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

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
