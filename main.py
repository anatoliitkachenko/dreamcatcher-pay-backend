import os
import hmac
import hashlib
import base64
from datetime import datetime, timedelta, date
from dateutil.relativedelta import relativedelta
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

app = FastAPI()
payment_api_router = APIRouter(prefix="/api/pay")

app.add_middleware(
    CORSMiddleware,
    # 🔴 Укажите здесь адрес, где будет жить ваш pay-helper.html или другие клиенты
    allow_origins=["https://dreamcatcher.guru", "https://payapi.dreamcatcher.guru"],
    allow_credentials=True,
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

class CheckoutSession(BaseModel):
    user_id: str
    plan_type: str
    username: Optional[str] = None
    first_name: Optional[str] = None

def make_wayforpay_signature(secret_key: str, params_list: List[str]) -> str:
    sign_str = ';'.join(str(x) for x in params_list)
    # Для большинства API WayForPay подпись HMAC-MD5 в hex-формате
    return hmac.new(secret_key.encode(), sign_str.encode(), hashlib.md5).hexdigest()

@payment_api_router.post("/create-checkout-session")
async def create_checkout_session(session: CheckoutSession):
    logger.info(f"Запрос на создание сессии (/api/pay/create-checkout-session): {session}")

    if session.plan_type != "subscription":
        # Этот эндпоинт теперь только для инициации подписки
        raise HTTPException(status_code=400, detail="Invalid plan_type for this endpoint, only 'subscription' allowed.")

    amount = 300 # Сумма первого платежа и последующих регулярных
    order_ref = f"sub_{session.user_id}_{int(datetime.utcnow().timestamp())}" # "sub" для подписки
    order_date = int(datetime.utcnow().timestamp())

    # Параметры для регулярного платежа
    today_date_obj = date.today()

    next_month_date = today_date_obj + relativedelta(months=1) 

    params_for_signature = [
        WAYFORPAY_MERCHANT_ACCOUNT,
        WAYFORPAY_DOMAIN,
        order_ref,
        str(order_date),
        str(amount),
        "UAH",
        "AI Dream Analysis (Subscription)", # Название продукта
        "1", # Количество
        str(amount) # Цена
    ]
    # 🔴 ВАЖНО: Список полей и их ПОРЯДОК ДОЛЖЕН ТОЧНО СООТВЕТСТВОВАТЬ
    # 🔴 документации WayForPay для метода Purchase с регулярными платежами!
    # 🔴 Это ПРИМЕРНЫЙ порядок, основанный на стандартной логике. ПРОВЕРЬТЕ!
    params_for_signature = [
        WAYFORPAY_MERCHANT_ACCOUNT,
        WAYFORPAY_DOMAIN,
        order_ref,
        str(order_date),
        str(amount),
        "UAH",
        "AI Dream Analysis (Subscription)", # productName[0]
        "1", # productCount[0]
        str(amount), # productPrice[0]
        # Добавляем параметры регулярного платежа (в ПРАВИЛЬНОМ ПОРЯДКЕ!)
        str(amount), # regularAmount
        "month",     # regularMode
        "1",         # regularInterval
        "0",         # regularCount (0 = неограниченно)
        regular_start_date_str # regularStartDate
        # 🔴 Убедитесь, что clientAccountId и другие поля не должны быть здесь!
        # 🔴 Обычно, поля client* не участвуют в подписи SimpleSignature.
    ]
    # Используем правильную функцию подписи (hex)
    merchant_signature = make_wayforpay_signature(WAYFORPAY_SECRET_KEY, params_for_signature)

    base_backend_url = os.getenv('BACKEND_URL_BASE', 'https://payapi.dreamcatcher.guru')
    frontend_url_for_return = os.getenv('FRONTEND_URL', 'https://dreamcatcher.guru')

    payment_form_data = {
        "merchantAccount": WAYFORPAY_MERCHANT_ACCOUNT,
        "merchantAuthType": "SimpleSignature",
        "merchantDomainName": WAYFORPAY_DOMAIN,
        "orderReference": order_ref,
        "orderDate": str(order_date),
        "amount": str(amount),
        "currency": "UAH",
        "productName[]": ["AI Dream Analysis (Subscription)"],
        "productCount[]": ["1"],
        "productPrice[]": [str(amount)],
        "clientFirstName": session.first_name or "",
        "clientAccountId": session.user_id, # Очень важно для веб-хуков
        "merchantSignature": merchant_signature,
        "language": "UA",
        "returnUrl": f"{frontend_url_for_return}/payment-return.html",
        "serviceUrl": f"{base_backend_url}/api/pay/wayforpay-webhook",

        # Параметры для регулярного платежа
        "regularMode": "month",
        "regularAmount": str(amount), # Сумма последующих списаний
        "regularCount": "0",          # 0 - означает неограниченное количество регулярных платежей
        "regularStartDate": regular_start_date_str,
        "regularInterval": "1"        # Интервал (1 месяц)
    }
    logger.info(f"Данные формы для WayForPay (регулярный): {payment_form_data}")
    await db["checkout_sessions"].insert_one({
        "orderReference": order_ref,
        "user_id": int(session.user_id),
        "plan_type": session.plan_type, # "subscription"
        "amount": amount,
        "status": "created_recurring_initial", # Новый статус
        "created_utc": datetime.utcnow()
    })
    return {"pay_url": "https://secure.wayforpay.com/pay", "payment_form_data": payment_form_data}

@payment_api_router.post("/wayforpay-webhook")
async def wayforpay_webhook(request: Request):
    data = await request.json()
    logger.info(f"Получен вебхук от WayForPay (/api/pay/wayforpay-webhook): {data}")

    # ... (код проверки подписи вебхука, как у вас был) ...
    # ... (ВАЖНО: используйте make_wayforpay_signature с hex-выводом для вебхуков) ...

    received_signature = data.get("merchantSignature")
    order_ref = data.get("orderReference")

    # Формируем строку для подписи ВЕБХУКА (порядок и набор полей из документации WayForPay)
    # 🔴 ВАЖНО: Список полей и их ПОРЯДОК ДОЛЖЕН ТОЧНО СООТВЕТСТВОВАТЬ
# 🔴 документации WayForPay для веб-хуков! ЭТО ТОЛЬКО ПРИМЕР!
    sign_fields_webhook = [
        data.get("merchantAccount"),
        data.get("orderReference"),
        data.get("amount"),
        data.get("currency"),
        data.get("authCode"),
        data.get("cardPan"),
        data.get("transactionStatus"),
        data.get("reasonCode")
]
# 🔴 Возможно, нужно добавить или убрать поля!
    # Убираем None, если какие-то поля не пришли, и приводим к строке
    sign_fields_webhook_clean = [str(f) for f in sign_fields_webhook if f is not None]

    webhook_signature_string = ';'.join(sign_fields_webhook_clean)
    # Для вебхуков обычно используется make_wayforpay_signature (с hexdigest)
    calculated_signature = make_wayforpay_signature(WAYFORPAY_SECRET_KEY, sign_fields_webhook_clean)

    if calculated_signature != received_signature:
        logger.error(f"Неверная подпись вебхука для orderReference {order_ref}. String: {webhook_signature_string}, Calc: {calculated_signature}, Recv: {received_signature}")
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    logger.info(f"Подпись вебхука для orderReference {order_ref} верна.")

    transaction_status = data.get("transactionStatus")
    user_id_str = data.get("clientAccountId")
    rec_token = data.get("recToken") # Токен для регулярных платежей

    current_time_utc = datetime.utcnow()
    tz_kyiv = timezone('Europe/Kyiv')

    if not user_id_str: # Должен приходить из clientAccountId
        logger.error(f"clientAccountId (user_id) отсутствует в вебхуке для orderReference {order_ref}")
        # ... (код ответа WayForPay с accept) ...
        response_time_utc_ts = int(current_time_utc.timestamp())
        response_signature_str = f"{order_ref};accept;{response_time_utc_ts}"
        response_signature = make_wayforpay_signature(WAYFORPAY_SECRET_KEY, [order_ref, "accept", str(response_time_utc_ts)])
        return {"orderReference": order_ref, "status": "accept", "time": response_time_utc_ts, "signature": response_signature}

    user_id = int(user_id_str)

    # Обновляем сессию в checkout_sessions
    await db["checkout_sessions"].update_one(
        {"orderReference": order_ref, "user_id": user_id},
        {"$set": {"status": transaction_status, "webhook_received_utc": current_time_utc, "webhook_data": data}},
        upsert=True # Важно, если заказ инициирован кнопкой и не был предварительно записан
    )

    if transaction_status == "Approved":
        checkout_session = await db["checkout_sessions"].find_one({"orderReference": order_ref, "user_id": user_id})
        plan_type = checkout_session.get("plan_type") if checkout_session else None

        # Если это была подписка (первый или последующий регулярный платеж)
        if order_ref.startswith("sub_") or (checkout_session and plan_type == "subscription"):
            end_date_utc = current_time_utc + timedelta(days=30)
            end_date_kyiv_str = end_date_utc.astimezone(tz_kyiv).strftime("%Y-%m-%d")
            current_date_kyiv_str = current_time_utc.astimezone(tz_kyiv).strftime("%Y-%m-%d")

            update_data = {
                "user_id": user_id, "is_active": 1,
                "subscription_start": current_date_kyiv_str, # или обновляем только end_date
                "subscription_end": end_date_kyiv_str,
                "cancel_requested": 0,
                "plan_type": "subscription" # Явно указываем
            }
            if rec_token: # Если это первый платеж регулярной подписки
                update_data["recToken"] = rec_token
                update_data["last_successful_charge_utc"] = current_time_utc

            await db["subscriptions"].update_one(
                {"user_id": user_id},
                {"$set": update_data},
                upsert=True
            )
            logger.info(f"Подписка для user_id {user_id} активирована/продлена до {end_date_kyiv_str}. recToken: {rec_token}")

        # Если это был разовый платеж (от кнопки "оплатить один сон")
        elif order_ref.startswith("single_") or (checkout_session and plan_type == "single"):
            current_date_kyiv_str = current_time_utc.astimezone(tz_kyiv).strftime("%Y-%m-%d")
            current_month_kyiv_str = current_time_utc.astimezone(tz_kyiv).strftime("%Y-%m")
            await db["usage_limits"].update_one(
                {"user_id": user_id, "date": current_date_kyiv_str},
                {"$set": {"unlimited_today": 1},
                    "$setOnInsert": {
                    "user_id": user_id, "date": current_date_kyiv_str, "dream_count": 0,
                    "monthly_count": 0, "last_reset_month": current_month_kyiv_str,
                    "first_usage_date": current_date_kyiv_str}},
                upsert=True)
            logger.info(f"Разовый платеж (unlimited_today) для user_id {user_id} на {current_date_kyiv_str}")

    # Отправляем подтверждение WayForPay
    response_time_utc_ts = int(current_time_utc.timestamp())
    # Строка для подписи ответа: orderReference;status;time
    response_params = [order_ref, "accept", str(response_time_utc_ts)]
    response_signature = make_wayforpay_signature(WAYFORPAY_SECRET_KEY, response_params)
    return {"orderReference": order_ref, "status": "accept", "time": response_time_utc_ts, "signature": response_signature}

# Пример функции для вызова API регулярного платежа WayForPay
# WAYFORPAY_API_URL = "https://api.wayforpay.com/api" # Уточните URL

# async def charge_recurring_payment(user_id: int, order_reference: str, amount: float, currency: str, rec_token: str):
#     order_date = int(datetime.utcnow().timestamp())
#     params_for_signature = [
#         WAYFORPAY_MERCHANT_ACCOUNT,
#         order_reference, # Уникальный для каждого списания
#         str(amount),
#         currency,
#         rec_token,
#         str(order_date)
#     ]
#     # Уточните точный список полей для подписи регулярного платежа в документации!
#     signature = make_wayforpay_signature(WAYFORPAY_SECRET_KEY, params_for_signature)

#     payload = {
#         "transactionType": "REGULAR_PAYMENT", # Или другой, по документации
#         "merchantAccount": WAYFORPAY_MERCHANT_ACCOUNT,
#         "orderReference": order_reference,
#         "amount": amount,
#         "currency": currency,
#         "recToken": rec_token,
#         "orderDate": order_date,
#         "comment": "Monthly subscription renewal",
#         "merchantSignature": signature
#     }
#     async with httpx.AsyncClient() as client:
#         try:
#             response = await client.post(WAYFORPAY_API_URL, json=payload)
#             response.raise_for_status() # Вызовет исключение для 4xx/5xx
#             logger.info(f"Recurring payment API response for order {order_reference}: {response.json()}")
#             return response.json()
#         except httpx.HTTPStatusError as e:
#             logger.error(f"HTTP error charging recurring payment for order {order_reference}: {e.response.text}")
#             return None
#         except Exception as e:
#             logger.error(f"Error charging recurring payment for order {order_reference}: {e}")
#             return None

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