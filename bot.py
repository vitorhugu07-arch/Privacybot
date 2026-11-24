import base64
import json
import os
import sqlite3

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

# ---------- CONFIG (tudo vem do Environment do Render) ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
PROMISSE_KEY = os.environ["PROMISSE_KEY"]  # sk_live_...
PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")  # https://seu-app.onrender.com
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]  # texto aleatorio longo
CHANNEL_ID = os.environ["CHANNEL_ID"]  # canal/grupo onde o bot e admin

# EDITE AQUI: nome do plano e valor em CENTAVOS (R$ 29,90 = 2990)
PLANS = {
    "1": ("vitalício VIP", 2990),
    "2": ("vitalício Premium", 1990),
    "3": ("vitalício Basico", 1690),
}

# EDITE AQUI: texto que aparece junto com a foto/video borrado (formato HTML:
# <b>negrito</b>, <i>italico</i>). Use \n para pular linha.
LEGENDA = (
    "🔒 <b>Escolha seu acesso</b>\n\n"
    "🔥 <b>vitalício Basico</b> - R$ 16,90\n"
    "• Vídeos vazados famosinha 🔥🥵\n\n"
    "⭐ <b>vitalício Premium</b> - R$ 19,90\n"
    "• Conteúdos de novinhas baixa renda +dmen⁰r🥵\n\n"
    "👑 <b>vitalício VIP</b> - R$ 29,90\n"
    "• Conteúdos obscuros,incestos, e muito+🔞😈\n\n"
    "💳 Pagamento por PIX, liberação na hora.""
)

TG = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
PM = "https://api.promisse.com.br"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

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


async def tg_file(method: str, field: str, path: str, **data):
    async with httpx.AsyncClient(timeout=60) as c:
        with open(path, "rb") as f:
            r = await c.post(f"{TG}/{method}", data=data, files={field: f})
        return r.json()


async def tg_bytes(method: str, field: str, content: bytes, filename: str, **data):
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{TG}/{method}", data=data, files={field: (filename, content)})
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
        await tg_file("sendVideo", "video", video, supports_streaming="true", **extra)
    elif os.path.exists(foto):
        await tg_file("sendPhoto", "photo", foto, **extra)
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
        await tg("answerCallbackQuery", callback_query_id=cb["id"])
        await create_pix(cb["message"]["chat"]["id"], cb["data"].split(":")[1])
    return {"ok": True}


# ---------- PROMISSEPAY: CRIAR PIX ----------
async def create_pix(chat_id: int, plan_key: str):
    if plan_key not in PLANS:
        return
    name, cents = PLANS[plan_key]

    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                f"{PM}/transactions",
                json={"amount": cents},
                # a chave vai crua, SEM o prefixo "Bearer"
                headers={
                    "Authorization": PROMISSE_KEY,
                    "Content-Type": "application/json",
                },
            )
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text}
    except Exception as e:
        print("Erro de conexao PromissePay:", e)
        data = {}
        r = None

    if r is None or r.status_code >= 300 or data.get("status") == "error" or not data.get("id"):
        print("Erro PromissePay:", getattr(r, "status_code", None), data)
        await tg(
            "sendMessage",
            chat_id=chat_id,
            text="Não consegui gerar o PIX agora. Tente de novo em instantes.",
        )
        return

    public_id = str(data["id"])
    db.execute(
        "INSERT OR REPLACE INTO orders (public_id, chat_id, plan, amount, status) "
        "VALUES (?, ?, ?, ?, 'PENDING')",
        (public_id, chat_id, plan_key, cents),
    )
    db.commit()

    pix_code = data.get("copyPaste")
    qr_b64 = data.get("qrCodeBase64")
    valor = f"R$ {cents / 100:.2f}".replace(".", ",")

    # QR code como imagem
    if qr_b64:
        try:
            raw = base64.b64decode(qr_b64.split(",")[-1])
            await tg_bytes(
                "sendPhoto",
                "photo",
                raw,
                "pix.png",
                chat_id=chat_id,
                caption=f"{name} - {valor}\nPague pelo QR Code ou use o copia e cola abaixo.",
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
                "Toque no código abaixo para copiar e pague no app do seu banco:\n\n"
                f"<code>{pix_code}</code>\n\n"
                "Assim que o pagamento for confirmado, envio seu acesso aqui."
            ),
        )
    else:
        print("copyPaste nao veio na resposta:", data)
        await tg(
            "sendMessage",
            chat_id=chat_id,
            text="Não consegui gerar o PIX agora. Tente de novo em instantes.",
        )


# ---------- PROMISSEPAY: CONFIRMACAO DE PAGAMENTO (webhook) ----------
PAID_STATUS = {"paid", "approved", "completed", "confirmed", "success", "succeeded"}


def all_strings(obj):
    """Junta todos os textos de um JSON (para achar o id da cobranca em qualquer lugar)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from all_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from all_strings(v)


def find_status(obj):
    if isinstance(obj, dict):
        v = obj.get("status")
        if isinstance(v, str):
            return v.lower()
        for x in obj.values():
            r = find_status(x)
            if r:
                return r
    elif isinstance(obj, list):
        for x in obj:
            r = find_status(x)
            if r:
                return r
    return ""


@app.post("/pm/{secret}")
async def promisse_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(403)
    payload = await request.json()
    print("Webhook Promisse:", payload)

    event = ""
    if isinstance(payload, dict):
        event = str(payload.get("event") or payload.get("type") or "").lower()

    # so libera acesso quando o pagamento foi aprovado
    if event:
        aprovado = event == "payment.approved"
    else:
        aprovado = find_status(payload) in PAID_STATUS
    if not aprovado:
        return {"ok": True}

    # acha o pedido: algum texto do aviso tem que ser o id guardado no banco
    row = None
    for s in all_strings(payload):
        row = db.execute(
            "SELECT public_id, chat_id, plan, status FROM orders WHERE public_id = ?",
            (s,),
        ).fetchone()
        if row:
            break
    if not row or row[3] == "PAID":
        return {"ok": True}

    public_id, chat_id, plan_key, _ = row
    db.execute("UPDATE orders SET status = 'PAID' WHERE public_id = ?", (public_id,))
    db.commit()

    # link de convite de uso unico para o canal/grupo
    invite = await tg("createChatInviteLink", chat_id=CHANNEL_ID, member_limit=1)
    link = (invite.get("result") or {}).get("invite_link")

    if link:
        await tg(
            "sendMessage",
            chat_id=chat_id,
            text=f"✅ Pagamento confirmado!\n\nSeu acesso: {link}\n\n(O link só funciona uma vez.)",
        )
    else:
        print("Erro ao criar convite:", invite)
        await tg(
            "sendMessage",
            chat_id=chat_id,
            text="✅ Pagamento confirmado! Houve um problema ao gerar seu acesso, já vamos resolver.",
        )
    return {"ok": True}


@app.get("/")
async def health():
    return {"status": "online"}
            
