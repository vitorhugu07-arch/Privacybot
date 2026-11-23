import base64
import os
import sqlite3
import uuid

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

# ---------- CONFIG (tudo vem do Environment do Render) ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GLADEPAY_KEY = os.environ["GLADEPAY_KEY"]
PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")  # https://seu-app.onrender.com
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]  # texto aleatório longo
CHANNEL_ID = os.environ["CHANNEL_ID"]  # canal/grupo onde o bot é admin

# EDITE AQUI: nome do plano e valor em CENTAVOS (R$ 29,90 = 2990)
PLANS = {
    "1": ("Plano 1", 2990),
    "2": ("Plano 2", 1990),
    "3": ("Plano 3", 1699),
}

TG = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
GP = "https://app.hubgladepay.com/v1"

app = FastAPI()

# ---------- BANCO (SQLite) ----------
db = sqlite3.connect("orders.db", check_same_thread=False)
db.execute(
    """CREATE TABLE IF NOT EXISTS orders (
    public_id TEXT PRIMARY KEY, chat_id INTEGER, plan TEXT,
    amount INTEGER, status TEXT DEFAULT 'PENDING')"""
)
db.commit()


# ---------- TELEGRAM ----------
async def tg(method: str, **data):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{TG}/{method}", json=data)
        return r.json()


async def send_plans(chat_id: int):
    buttons = [
        [{"text": f"{name} - R$ {cents / 100:.2f}".replace(".", ","),
          "callback_data": f"plan:{key}"}]
        for key, (name, cents) in PLANS.items()
    ]
    await tg("sendMessage", chat_id=chat_id,
             text="Escolha seu plano:",
             reply_markup={"inline_keyboard": buttons})


@app.on_event("startup")
async def set_webhook():
    await tg("setWebhook", url=f"{PUBLIC_URL}/tg",
             secret_token=WEBHOOK_SECRET,
             allowed_updates=["message", "callback_query"])


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
        await tg("answerCallbackQuery", callback_query_id=cb["id"])
        await create_pix(cb["message"]["chat"]["id"], cb["data"].split(":")[1])
    return {"ok": True}


# ---------- GLADEPAY: CRIAR PIX ----------
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
        r = await c.post(f"{GP}/deposito", json=body,
                         headers={"Authorization": f"Bearer {GLADEPAY_KEY}"})
    data = r.json()
    if r.status_code >= 300 or not data.get("ok"):
        print("Erro GladePay:", r.status_code, data)
        await tg("sendMessage", chat_id=chat_id,
                 text="Não consegui gerar o PIX agora. Tente de novo em instantes.")
        return

    # grava o publicId: é ele que volta no webhook (campo "id")
    db.execute("INSERT OR REPLACE INTO orders (public_id, chat_id, plan, amount) "
               "VALUES (?,?,?,?)", (data["publicId"], chat_id, plan_key, cents))
    db.commit()

    valor = f"R$ {cents / 100:.2f}".replace(".", ",")
    await tg("sendMessage", chat_id=chat_id,
             text=f"{name} - {valor}\n\nPague o PIX abaixo (copia e cola). "
                  f"Assim que cair, libero seu acesso automaticamente.")
    # QR Code como imagem
    try:
        img = base64.b64decode(data["qrCodeBase64"])
        async with httpx.AsyncClient(timeout=30) as c:
            await c.post(f"{TG}/sendPhoto", data={"chat_id": chat_id},
                         files={"photo": ("pix.png", img, "image/png")})
    except Exception as e:
        print("QR erro:", e)
    await tg("sendMessage", chat_id=chat_id,
             text=f"`{data['qrCodeText']}`", parse_mode="Markdown")


# ---------- GLADEPAY: WEBHOOK DE PAGAMENTO ----------
@app.post("/gp/{secret}")
async def gladepay_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(403)
    payload = await request.json()
    print("Webhook GladePay:", payload)  # confira os campos reais nos logs

    # TODO: validar a assinatura do webhook (ver doc "Webhooks" da GladePay)

    public_id = payload.get("id")
    status = payload.get("status")  # confirmar o nome do campo no payload real
    if status != "SETTLED" or not public_id:
        return {"ok": True}

    row = db.execute("SELECT chat_id, plan, amount, status FROM orders "
                     "WHERE public_id=?", (public_id,)).fetchone()
    if not row or row[3] == "PAID":
        return {"ok": True}  # desconhecido ou já entregue (evita duplicar)
    chat_id, plan, amount, _ = row

    # Se o payload trouxer o valor, confere para evitar fraude
    paid = payload.get("amountCents")
    if paid is not None and int(paid) < amount:
        print("Valor menor que o esperado:", paid, amount)
        return {"ok": True}

    db.execute("UPDATE orders SET status='PAID' WHERE public_id=?", (public_id,))
    db.commit()

    # link de convite de uso único
    link = await tg("createChatInviteLink", chat_id=CHANNEL_ID, member_limit=1)
    url = link.get("result", {}).get("invite_link")
    text = (f"Pagamento confirmado! ✅\n\nSeu acesso: {url}"
            if url else "Pagamento confirmado! ✅ Já já te envio o acesso.")
    await tg("sendMessage", chat_id=chat_id, text=text)
    return {"ok": True}


@app.get("/")
async def health():
    return {"status": "online"}
          
