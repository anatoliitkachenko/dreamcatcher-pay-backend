import os
import hmac
import hashlib
import base64
from fastapi import FastAPI, Request, HTTPException, APIRouter 
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from datetime import datetime, timedelta
from typing import Optional
from motor.motor_asyncio import AsyncIOMotorClient
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import logging
from pytz import timezone 

load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MONGO_URI = os.getenv("MONGO_URI")
WAYFORPAY_MERCHANT_ACCOUNT = os.getenv("WAYFORPAY_MERCHANT_ACCOUNT")
WAYFORPAY_SECRET_KEY = os.getenv("WAYFORPAY_SECRET_KEY")
WAYFORPAY_DOMAIN = os.getenv("WAYFORPAY_DOMAIN")

mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["dream_database"]

app = FastAPI() # Основное приложение FastAPI

# Создаем роутер с префиксом, который ожидает Nginx
# Все пути, определенные в этом роутере, будут начинаться с /api/pay
payment_api_router = APIRouter(prefix="/api/pay")

@payment_api_router.api_route("/payment-return", methods=["GET", "POST"], include_in_schema=False)
async def payment_return(request: Request):
    form = await request.form() if request.method == "POST" else {}
    status = form.get("transactionStatus") or "Unknown"
    approved = status == "Approved"

    html = f"""
    <html><body style="font-family:sans-serif;text-align:center;padding-top:40px">
        <h2>{'✅ Оплата получена' if approved else '⏳ Платёж не завершён'}</h2>
        <p>{'Можете вернуться в бот.' if approved else 'Если вы закрыли форму случайно, попробуйте оплатить ещё раз.'}</p>
        <script>setTimeout(()=>window.close(),1500)</script>
    </body></html>"""
    return HTMLResponse(html)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class CheckoutSession(BaseModel):
    user_id: str
    plan_type: str
    username: Optional[str] = None
    first_name: Optional[str] = None

def make_outgoing_signature(merchant_secret_key, params_list):
    sign_str = ';'.join(str(x) for x in params_list)
    # Для WayForPay подпись формы обычно делается через HMAC MD5 и затем hexdigest.
    # Уточните, действительно ли вам нужен Base64 для исходящей подписи.
    # Если нет (что более типично), то должно быть:
    # return hmac.new(merchant_secret_key.encode(), sign_str.encode(), hashlib.md5).hexdigest()
    # Оставляю ваш вариант с Base64, если вы уверены, что он нужен.
    return base64.b64encode(hmac.new(merchant_secret_key.encode(), sign_str.encode(), hashlib.md5).digest()).decode()


# Используем роутер для определения пути
@payment_api_router.post("/create-checkout-session")
async def create_checkout_session(session: CheckoutSession):
    logger.info(f"Запрос на создание сессии (/api/pay/create-checkout-session): {session}")
    if session.plan_type not in ("subscription", "single"):
        logger.error(f"Неверный plan_type: {session.plan_type}")
        raise HTTPException(status_code=400, detail="Invalid plan_type")

    amount = 300 if session.plan_type == "subscription" else 40
    order_ref = f"order_{session.user_id}_{session.plan_type}_{int(datetime.utcnow().timestamp())}"
    order_date = int(datetime.utcnow().timestamp())

    params_for_signature = [
        WAYFORPAY_MERCHANT_ACCOUNT,
        WAYFORPAY_DOMAIN,
        order_ref,
        str(order_date),
        str(amount),
        "UAH",
        "AI Dream Analysis",
        "1",
        str(amount)
    ]
    
    logger.info(f"Строка для исходящей подписи (перед генерацией): {';'.join(params_for_signature)}")
    merchant_signature = make_outgoing_signature(WAYFORPAY_SECRET_KEY, params_for_signature)
    logger.info(f"Сгенерированная исходящая подпись: {merchant_signature}")

    # ВАЖНО: serviceUrl теперь должен включать префикс /api/pay/
    # BACKEND_URL_BASE должен быть https://dreamcatcher.guru
    # FRONTEND_URL для returnUrl - https://dreamcatcher.guru
    base_backend_url = os.getenv('BACKEND_URL_BASE', 'https://payapi.dreamcatcher.guru')
    
    payment_form_data = {
        "merchantAccount": WAYFORPAY_MERCHANT_ACCOUNT,
        "merchantAuthType": "SimpleSignature",
        "merchantDomainName": WAYFORPAY_DOMAIN,
        "orderReference": order_ref,
        "orderDate": str(order_date),
        "amount": str(amount),
        "currency": "UAH",
        "productName[]": ["AI Dream Analysis"],
        "productCount[]": ["1"],
        "productPrice[]": [str(amount)],
        "clientFirstName": session.first_name or "",
        "clientAccountId": session.user_id,
        "merchantSignature": merchant_signature,
        "language": "UA",
        "returnUrl": os.getenv("RETURN_URL"),
        # ИЗМЕНЕНО: serviceUrl теперь с префиксом /api/pay/
        "serviceUrl": f"{base_backend_url}/api/pay/wayforpay-webhook"
    }
    logger.info(f"Данные формы для WayForPay: {payment_form_data}")

    await db["checkout_sessions"].insert_one({
        "orderReference": order_ref,
        "user_id": int(session.user_id),
        "plan_type": session.plan_type,
        "amount": amount,
        "status": "created",
        "created_utc": datetime.utcnow()
    })

    return {"pay_url": "https://secure.wayforpay.com/pay", "payment_form_data": payment_form_data}

# Используем роутер для определения пути
@payment_api_router.post("/wayforpay-webhook")
async def wayforpay_webhook(request: Request):
    try:
        data = await request.json()
    except Exception as e:
        logger.error(f"Ошибка парсинга JSON из вебхука (/api/pay/wayforpay-webhook): {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    logger.info(f"Получен вебхук от WayForPay (/api/pay/wayforpay-webhook): {data}")

    received_signature = data.get("merchantSignature")
    order_ref = data.get("orderReference")
    
    fields_for_webhook_signature = []
    # Список полей для подписи вебхука зависит от статуса и настроек мерчанта,
    # здесь приведен примерный список для 'Approved'. ПРОВЕРЬТЕ ДОКУМЕНТАЦИЮ WAYFORPAY!
    if data.get("transactionStatus") == "Approved":
        fields_for_webhook_signature = [
            str(data.get("merchantAccount")),
            str(data.get("orderReference")),
            str(data.get("amount")),
            str(data.get("currency")),
            str(data.get("authCode")),
            str(data.get("cardPan")),
            str(data.get("transactionStatus")),
            str(data.get("reasonCode"))
        ]
    else:
        # Для других статусов (например, 'Declined', 'Expired') набор полей может быть другим
        fields_for_webhook_signature = [
            str(data.get("orderReference")),
            str(data.get("transactionStatus")),
            str(data.get("time")) 
        ]
    # Важно, чтобы в fields_for_webhook_signature не было None элементов перед join
    fields_for_webhook_signature = [f for f in fields_for_webhook_signature if f is not None]

    webhook_signature_string = ';'.join(fields_for_webhook_signature)
    logger.info(f"Строка для проверки подписи вебхука: {webhook_signature_string}")
    
    calculated_signature = hmac.new(WAYFORPAY_SECRET_KEY.encode(), webhook_signature_string.encode(), hashlib.md5).hexdigest()
    logger.info(f"Рассчитанная подпись вебхука: {calculated_signature}, Полученная: {received_signature}")

    if calculated_signature != received_signature:
        logger.error(f"Неверная подпись вебхука для orderReference {order_ref}.")
        raise HTTPException(status_code=400, detail="Invalid webhook signature")
    
    logger.info(f"Подпись вебхука для orderReference {order_ref} верна.")
    
    transaction_status = data.get("transactionStatus")
    user_id_str = data.get("clientAccountId")
    current_time_utc = datetime.utcnow() # Используем UTC для всех временных меток в БД

    response_time_utc_ts = int(current_time_utc.timestamp()) # Для ответа WayForPay

    if not user_id_str:
        logger.error(f"clientAccountId (user_id) отсутствует в вебхуке для orderReference {order_ref}")
        # Формируем ответ для WayForPay даже при ошибке, чтобы остановить повторы
        response_signature_str = f"{order_ref};accept;{response_time_utc_ts}"
        response_signature = hmac.new(WAYFORPAY_SECRET_KEY.encode(), response_signature_str.encode(), hashlib.md5).hexdigest()
        return {"orderReference": order_ref, "status": "accept", "time": response_time_utc_ts, "signature": response_signature}

    try:
        user_id = int(user_id_str)
    except ValueError:
        logger.error(f"Неверный формат clientAccountId '{user_id_str}' в вебхуке для orderReference {order_ref}")
        response_signature_str = f"{order_ref};accept;{response_time_utc_ts}"
        response_signature = hmac.new(WAYFORPAY_SECRET_KEY.encode(), response_signature_str.encode(), hashlib.md5).hexdigest()
        return {"orderReference": order_ref, "status": "accept", "time": response_time_utc_ts, "signature": response_signature}

    await db["checkout_sessions"].update_one(
        {"orderReference": order_ref, "user_id": user_id},
        {"$set": {"status": transaction_status, "webhook_received_utc": current_time_utc, "webhook_data": data}}
    )

    if transaction_status == "Approved":
        checkout_session = await db["checkout_sessions"].find_one({"orderReference": order_ref, "user_id": user_id})
        if not checkout_session:
            logger.error(f"Сессия checkout_sessions не найдена для orderReference {order_ref} и user_id {user_id} после утверждения.")
            response_signature_str = f"{order_ref};accept;{response_time_utc_ts}"
            response_signature = hmac.new(WAYFORPAY_SECRET_KEY.encode(), response_signature_str.encode(), hashlib.md5).hexdigest()
            return {"orderReference": order_ref, "status": "accept", "time": response_time_utc_ts, "signature": response_signature}

        plan_type = checkout_session.get("plan_type")
        
        # Используем Киевское время только для записи в коллекции бота, если это нужно для совместимости.
        # Внутренне лучше оперировать UTC.
        tz_kyiv = timezone('Europe/Kyiv')
        current_date_kyiv_str = current_time_utc.astimezone(tz_kyiv).strftime("%Y-%m-%d")
        current_month_kyiv_str = current_time_utc.astimezone(tz_kyiv).strftime("%Y-%m")

        if plan_type == "subscription":
            end_date_utc = current_time_utc + timedelta(days=30)
            end_date_kyiv_str = end_date_utc.astimezone(tz_kyiv).strftime("%Y-%m-%d")
            
            await db["subscriptions"].update_one(
                {"user_id": user_id},
                {"$set": {
                    "user_id": user_id,
                    "is_active": 1,
                    "subscription_start": current_date_kyiv_str, # Дата в формате бота
                    "subscription_end": end_date_kyiv_str,   # Дата в формате бота
                    "cancel_requested": 0 
                }},
                upsert=True
            )
            logger.info(f"Подписка активирована/обновлена для user_id {user_id} до {end_date_kyiv_str} (order: {order_ref})")
        
        elif plan_type == "single":
            await db["usage_limits"].update_one(
                {"user_id": user_id, "date": current_date_kyiv_str}, # Дата в формате бота
                {
                    "$set": {"unlimited_today": 1},
                    "$setOnInsert": {
                        "user_id": user_id,
                        "date": current_date_kyiv_str,
                        "dream_count": 0,
                        "monthly_count": 0,
                        "last_reset_month": current_month_kyiv_str,
                        "first_usage_date": current_date_kyiv_str
                    }
                },
                upsert=True
            )
            logger.info(f"Разовый платеж (unlimited_today) активирован для user_id {user_id} на {current_date_kyiv_str} (order: {order_ref})")
        else:
            logger.error(f"Неизвестный plan_type '{plan_type}' в сессии для orderReference {order_ref}")

    response_signature_str = f"{order_ref};accept;{response_time_utc_ts}"
    response_signature = hmac.new(WAYFORPAY_SECRET_KEY.encode(), response_signature_str.encode(), hashlib.md5).hexdigest()
    
    response_to_wayforpay = {
        "orderReference": order_ref,
        "status": "accept",
        "time": response_time_utc_ts,
        "signature": response_signature
    }
    logger.info(f"Ответ для WayForPay для orderReference {order_ref}: {response_to_wayforpay}")
    return response_to_wayforpay

# Используем роутер для определения пути
@payment_api_router.get("/check-access") 
async def check_access_endpoint(user_id: str): # Переименовал, чтобы не конфликтовать с функцией check_access из бота
    try:
        user_id_int = int(user_id)
    except ValueError:
        logger.warning(f"Неверный user_id в /api/pay/check-access: {user_id}")
        return {"active": False}
    
    tz_kyiv = timezone('Europe/Kyiv') 
    today_kyiv_str = datetime.now(tz_kyiv).strftime("%Y-%m-%d")

    sub = await db["subscriptions"].find_one({"user_id": user_id_int}) 
    
    if sub and sub.get("is_active") == 1 and sub.get("subscription_end") >= today_kyiv_str:
        logger.info(f"Доступ активен для user_id {user_id_int} через /api/pay/check-access. Дата окончания: {sub.get('subscription_end')}")
        return {"active": True}
    
    logger.info(f"Доступ неактивен для user_id {user_id_int} через /api/pay/check-access. Данные подписки: {sub}")
    return {"active": False}

# Включаем роутер в основное приложение FastAPI
app.include_router(payment_api_router)