import os
import hmac
import hashlib
import base64
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from datetime import datetime, timedelta
from typing import Optional
from motor.motor_asyncio import AsyncIOMotorClient
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
WAYFORPAY_MERCHANT_ACCOUNT = os.getenv("WAYFORPAY_MERCHANT_ACCOUNT")
WAYFORPAY_SECRET_KEY = os.getenv("WAYFORPAY_SECRET_KEY")
WAYFORPAY_DOMAIN = os.getenv("WAYFORPAY_DOMAIN")
FRONTEND_URL = os.getenv("FRONTEND_URL") or "http://localhost:8000"

mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo["dreamcatcher"]

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==== Модель запроса на оплату ====
class CheckoutSession(BaseModel):
    user_id: str
    plan_type: str   # "subscription" или "single"
    username: Optional[str] = None
    first_name: Optional[str] = None

# ==== Генерация подписи для WayForPay ====
def make_signature(merchant_secret_key, params_list):
    sign_str = ';'.join(str(x) for x in params_list)
    return base64.b64encode(hmac.new(merchant_secret_key.encode(), sign_str.encode(), hashlib.md5).digest()).decode()

# ==== Endpoint для создания платежа WayForPay ====
@app.post("/create-checkout-session")
async def create_checkout_session(session: CheckoutSession):
    if session.plan_type not in ("subscription", "single"):
        raise HTTPException(status_code=400, detail="Invalid plan_type")

    # Определяем сумму и описание
    amount = 300 if session.plan_type == "subscription" else 40
    order_ref = f"order_{session.user_id}_{int(datetime.utcnow().timestamp())}"

    params = [
        WAYFORPAY_MERCHANT_ACCOUNT,
        WAYFORPAY_DOMAIN,
        order_ref,
        int(datetime.utcnow().timestamp()),
        amount,
        "UAH",
        "AI Dream Analysis",
        "1",
        amount,
        session.first_name or "",
        session.username or "",
        session.user_id
    ]
    sign = make_signature(WAYFORPAY_SECRET_KEY, params)

    pay_url = "https://secure.wayforpay.com/pay"

    payment_form = {
        "merchantAccount": WAYFORPAY_MERCHANT_ACCOUNT,
        "merchantDomainName": WAYFORPAY_DOMAIN,
        "orderReference": order_ref,
        "orderDate": int(datetime.utcnow().timestamp()),
        "amount": amount,
        "currency": "UAH",
        "productName": ["AI Dream Analysis"],
        "productCount": [1],
        "productPrice": [amount],
        "clientFirstName": session.first_name,
        "clientEmail": "",  # можно добавить если на сайте есть email
        "clientPhone": "",  # можно добавить если на сайте есть телефон
        "clientAccountId": session.user_id,
        "merchantSignature": sign,
        "language": "UA"
    }

    # Сохраняем сессию оплаты в БД для отслеживания
    await db.checkout_sessions.insert_one({
        "orderReference": order_ref,
        "user_id": session.user_id,
        "plan_type": session.plan_type,
        "amount": amount,
        "status": "created",
        "created": datetime.utcnow()
    })

    # Отправляем форму (или просто данные для JS)
    return {"pay_url": pay_url, "payment_form": payment_form}

# ==== Webhook от WayForPay ====
@app.post("/wayforpay-webhook")
async def wayforpay_webhook(request: Request):
    data = await request.json()
    merchantSignature = data.get("merchantSignature")
    # Тут проверяем подпись (по документации WayForPay)

    order_ref = data.get("orderReference")
    transactionStatus = data.get("transactionStatus")
    user_id = data.get("clientAccountId")

    if transactionStatus == "Approved":
        # Подписка — 30 дней, разовая — только запись оплаты
        session = await db.checkout_sessions.find_one({"orderReference": order_ref})
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        if session["plan_type"] == "subscription":
            end_date = datetime.utcnow() + timedelta(days=30)
            await db.subscriptions.update_one(
                {"user_id": user_id},
                {"$set": {
                    "is_active": True,
                    "start_date": datetime.utcnow(),
                    "end_date": end_date,
                    "plan_type": "subscription"
                }},
                upsert=True
            )
        else:
            await db.payments.insert_one({
                "user_id": user_id,
                "orderReference": order_ref,
                "amount": session["amount"],
                "paid_at": datetime.utcnow(),
                "plan_type": "single"
            })
        # Обновляем статус сессии
        await db.checkout_sessions.update_one(
            {"orderReference": order_ref},
            {"$set": {"status": "approved", "approved_at": datetime.utcnow()}}
        )
        return {"status": "ok"}
    else:
        await db.checkout_sessions.update_one(
            {"orderReference": order_ref},
            {"$set": {"status": transactionStatus, "updated_at": datetime.utcnow()}}
        )
        return {"status": transactionStatus}

# ==== Endpoint проверки доступа пользователя ====
@app.get("/check-access")
async def check_access(user_id: str):
    sub = await db.subscriptions.find_one({"user_id": user_id})
    if sub and sub.get("is_active") and sub.get("end_date") >= datetime.utcnow():
        return {"active": True}
    return {"active": False}
