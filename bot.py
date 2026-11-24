import base64
import json
import os
import sqlite3
import uuid

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

# ---------- CONFIG ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GLADEPAY_KEY = os.environ["GLADEPAY_KEY"]
PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
CHANNEL_ID = os.environ["CHANNEL_ID"]

# ---------- PLANOS ----------
PLANS = {
    "1": ("Pack VIP", 2990),
    "2": ("Pack Premium", 1990),
    "3": ("Pack Basic", 1699),
}

# ---------- LEGENDA NOVA ----------
LEGENDA = (
    "🔒 <b>Escolha seu acesso</b>\n\n"
    "🔥 <b>Pack Basic</b> - R$ 16,99\n"
    "• Vídeos quentes e populares 🔥🥵\n\n"
    "⭐ <b>Pack Premium</b> - R$ 19,90\n"
    "• Conteúdos exclusivos e cenas intensas 🥵\n\n"
    "👑 <b>Pack VIP</b> - R$ 29,90\n"
    "• Conteúdos exclusivos que você nunca viu 🔞😈\n\n"
    "💳 Pagamento por PIX, liberação na hora."
)

TG = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
GP = "https://app.hubgladepay.com/v1"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI()

# ---------- BANCO ----------
db = sqlite3.connect("orders.db", check_same_thread=False)

db.execute(
    """CREATE TABLE IF NOT EXISTS orders (
    public_id TEXT PRIMARY KEY,
    chat_id INTEGER,
    plan TEXT,
    amount INTEGER,
    status TEXT DEFAULT 'PENDING')"""
)

db.commit()


# ---------- TELEGRAM ----------
async def tg(method: str, **data):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{TG}/{method}", json=data)
        return r.json()


async def tg_file(method: str, field: str, path: str, **data):
    async with httpx.AsyncClient(timeout=60) as c:
        with open(path, "rb") as f:
            r = await c.post(
                f"{TG}/{method}",
                data=data,
                files={field: f},
            )
        return r.json()


async def tg_bytes(
    method: str,
    field: str,
    content: bytes,
    filename: str,
    **data
):
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            f"{TG}/{method}",
            data=data,
            files={field: (filename, content)},
        )
        return r.json()


async def send_plans(chat_id: int):
    buttons = [
        [{
            "text": f"{name} - R$ {cents / 100:.2f}".replace(".", ","),
            "callback_data": f"plan:{key}",
        }]
        for key, (name, cents) in PLANS.items()
    ]

    markup = {"inline_keyboard": buttons}

    video = os.path.join(BASE_DIR, "preview.mp4")
    foto = os.path.join(BASE_DIR, "preview.jpg")

    extra = dict(
        chat_id=chat_id,
        caption=LEGENDA,
        parse_mode="HTML",
        has_spoiler="true",
        reply_markup=json.dumps(markup),
    )

    if os.path.exists(video):
        await tg_file(
            "sendVideo",
            "video",
            video,
            supports_streaming="true",
            **extra,
        )

    elif os.path.exists(foto):
        await tg_file(
            "sendPhoto",
            "photo",
            foto,
            **extra,
        )

    else:
        await tg(
            "sendMessage",
            chat_id=chat_id,
            text=LEGENDA,
            parse_mode="HTML",
            reply_markup=markup,
        )


@app.on_event("startup")
async def set_webhook():
    await tg(
        "setWebhook",
        url=f"{PUBLIC_URL}/tg",
        secret_token=WEBHOOK_SECRET,
        allowed_updates=["message", "callback_query"],
    )


@app.post("/tg")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
):
    if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(403)

    update = await request.json()

    msg = update.get("message")

    if msg and msg.get("text", "").startswith("/start"):
        await send_plans(msg["chat"]["id"])

    cb = update.get("callback_query")

    if cb and cb.get("data", "").startswith("plan:"):
        await tg(
            "answerCallbackQuery",
            callback_query_id=cb["id"],
        )

        await create_pix(
            cb["message"]["chat"]["id"],
            cb["data"].split(":")[1],
        )

    return {"ok": True}


# ---------- HELPERS GLADEPAY ----------
def find_pix_code(obj):
    if isinstance(obj, str):
        return obj if obj.startswith("000201") else None

    if isinstance(obj, dict):
        for v in obj.values():
            r = find_pix_code(v)
            if r:
                return r

    if isinstance(obj, list):
        for v in obj:
            r = find_pix_code(v)
            if r:
                return r

    return None


def find_key(obj, names):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in names and isinstance(v, (str, int)) and v:
                return str(v)

        for v in obj.values():
            r = find_key(v, names)
            if r:
                return r

    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, names)
            if r:
                return r

    return None


# ---------- CRIAR PIX ----------
async def create_pix(chat_id: int, plan_key: str):
    if plan_key not in PLANS:
        return

    name, cents = PLANS[plan_key]

    body = {
        "amountCents": cents,
        "reference": f"{chat_id}-{plan_key}-{uuid.uuid4().hex[:8]}",
        "callbackUrl": f"{PUBLIC_URL}/gp/{WEBHOOK_SECRET}",
        "description": name,
    }

    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{GP}/deposito",
            json=body,
            headers={
                "Authorization": f"Bearer {GLADEPAY_KEY}"
            },
        )

    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}

    if r.status_code >= 300 or not data.get("ok"):
        print("Erro GladePay:", r.status_code, data)

        await tg(
            "sendMessage",
            chat_id=chat_id,
            text="Não consegui gerar o PIX agora. Tente de novo em instantes.",
        )
        return

    public_id = find_key(
        data,
        {"publicid", "public_id", "id"},
    )

    if public_id:
        db.execute(
            "INSERT OR REPLACE INTO orders "
            "(public_id, chat_id, plan, amount, status) "
            "VALUES (?, ?, ?, ?, 'PENDING')",
            (public_id, chat_id, plan_key, cents),
        )
        db.commit()

    else:
        print("GladePay não devolveu id:", data)

    pix_code = find_pix_code(data)

    qr_b64 = find_key(
        data,
        {
            "qrcodebase64",
            "qr_code_base64",
            "qrcodeimage",
            "qrimage",
            "qr_image",
        },
    )

    valor = f"R$ {cents / 100:.2f}".replace(".", ",")

    if qr_b64:
        try:
            raw = base64.b64decode(qr_b64.split(",")[-1])

            await tg_bytes(
                "sendPhoto",
                "photo",
                raw,
                "pix.png",
                chat_id=chat_id,
                caption=(
                    f"{name} - {valor}\n"
                    "Pague pelo QR Code ou use o copia e cola abaixo."
                ),
            )

        except Exception as e:
            print("Erro ao enviar QR:", e)

    if pix_code:
        await tg(
            "sendMessage",
            chat_id=chat_id,
            parse_mode="HTML",
            text=(
                f"💳 <b>{name}</b> - {valor}\n\n"
                "Toque no código abaixo para copiar "
                "e pague no app do seu banco:\n\n"
                f"<code>{pix_code}</code>\n\n"
                "Assim que o pagamento for confirmado, "
                "envio seu acesso aqui."
            ),
        )

    else:
        print("Código PIX não encontrado:", data)

        await tg(
            "sendMessage",
            chat_id=chat_id,
            text="Não consegui gerar o PIX agora. Tente de novo em instantes.",
        )


# ---------- CONFIRMAÇÃO DE PAGAMENTO ----------
PAID_STATUS = {
    "paid",
    "approved",
    "completed",
    "confirmed",
    "success",
    "succeeded",
    "pago",
}


@app.post("/gp/{secret}")
async def gladepay_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(403)

    payload = await request.json()

    print("Webhook GladePay:", payload)

    public_id = str(
        payload.get("id")
        or find_key(payload, {"publicid", "public_id", "id"})
        or ""
    )

    status = (
        find_key(payload, {"status"}) or ""
    ).lower()

    if not public_id or status not in PAID_STATUS:
        return {"ok": True}

    row = db.execute(
        "SELECT chat_id, plan, status "
        "FROM orders WHERE public_id = ?",
        (public_id,),
    ).fetchone()

    if not row or row[2] == "PAID":
        return {"ok": True}

    chat_id, plan_key, _ = row

    db.execute(
        "UPDATE orders SET status = 'PAID' WHERE public_id = ?",
        (public_id,),
    )

    db.commit()

    invite = await tg(
        "createChatInviteLink",
        chat_id=CHANNEL_ID,
        member_limit=1,
    )

    link = (invite.get("result") or {}).get("invite_link")

    if link:
        await tg(
            "sendMessage",
            chat_id=chat_id,
            text=(
                "✅ Pagamento confirmado!\n\n"
                f"Seu acesso: {link}\n\n"
                "(O link só funciona uma vez.)"
            ),
        )

    else:
        print("Erro ao criar convite:", invite)

        await tg(
            "sendMessage",
            chat_id=chat_id,
            text=(
                "✅ Pagamento confirmado! "
                "Houve um problema ao gerar seu acesso, "
                "já vamos resolver."
            ),
        )

    return {"ok": True}


@app.get("/")
async def health():
    return {"status": "online"}
