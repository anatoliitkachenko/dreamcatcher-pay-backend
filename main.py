import os
import hmac
import hashlib
import base64
from datetime import datetime, timedelta, date
from dateutil.relativedelta import relativedelta
from fastapi import FastAPI, Request, HTTPException, APIRouter 
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from datetime import datetime, timedelta, date
from typing import List, Optional, Dict
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

# ❗ ПРОВЕРИТЬ/НАСТРОИТЬ: Убедитесь, что эти URL точны
# Запрос приходит с 'https://www.dreamcatcher.guru'
FRONTEND_DOMAIN_WWW = "https://www.dreamcatcher.guru" # <--- 🟢 ДОБАВЛЕНО 'www.'
FRONTEND_DOMAIN_NO_WWW = "https://dreamcatcher.guru" # На всякий случай, если иногда без www
BACKEND_DOMAIN = "https://payapi.dreamcatcher.guru" # Если ваш API на другом поддомене

# Возможные источники для тестирования, включая локальные
origins = [
    FRONTEND_DOMAIN_WWW,    # <--- 🟢 ИСПОЛЬЗУЕМ С 'www.'
    FRONTEND_DOMAIN_NO_WWW, # <--- Добавьте и без 'www.', если это возможно
    BACKEND_DOMAIN,         # Только если API и фронтенд на разных доменах/поддоменах и нужны взаимные запросы
    # "http://localhost",
    # "http://127.0.0.1",
    # "http://localhost:xxxx", # Замените xxxx на порт, если тестируете локально фронтенд
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS", "PUT", "DELETE", "PATCH"],
    allow_headers=["*"], 
    expose_headers=["*"],
    max_age=600
)

class CheckoutSession(BaseModel):
    user_id: str
    plan_type: str
    username: Optional[str] = None
    first_name: Optional[str] = None

class WidgetParamsRequest(BaseModel):
    user_id: str
    plan_type: str 
    lang: Optional[str] = 'UA' # Язык виджета (UA, RU, EN)
    client_first_name: Optional[str] = None
    client_last_name: Optional[str] = None
    client_email: Optional[str] = None
    client_phone: Optional[str] = None

def make_wayforpay_signature(secret_key: str, params_list: List[str]) -> str:
    sign_str = ';'.join(str(x) for x in params_list)
    # Для большинства API WayForPay подпись HMAC-MD5 в hex-формате
    return hmac.new(secret_key.encode(), sign_str.encode(), hashlib.md5).hexdigest()

@payment_api_router.post("/get-widget-params") # Имя эндпоинта изменено
async def get_widget_payment_params(request_data: WidgetParamsRequest):
    logger.info(f"Запрос на параметры для виджета (/api/pay/get-widget-params): {request_data}")

    user_id_str = request_data.user_id
    plan_type = request_data.plan_type
    
    product_name_str = ""
    amount = 0

    if plan_type == "subscription":
        amount = 300 
        product_name_str = "AI Dream Analysis (Subscription)"
        order_ref_prefix = "widget_sub"
    elif plan_type == "single":
        amount = 40 
        product_name_str = "AI Dream Analysis (Single)"
        order_ref_prefix = "widget_single"
    else:
        logger.error(f"Invalid plan_type '{plan_type}' received for widget params.")
        raise HTTPException(status_code=400, detail="Invalid plan_type. Allowed: 'subscription', 'single'.")

    # Генерируем orderReference, включая telegram_user_id для идентификации
    order_ref = f"{order_ref_prefix}_{user_id_str}_{int(datetime.utcnow().timestamp())}"
    order_date = int(datetime.utcnow().timestamp()) # Timestamp

    signature_params_list = [
        WAYFORPAY_MERCHANT_ACCOUNT,
        WAYFORPAY_DOMAIN,       # Это ваш merchantDomainName
        order_ref,
        str(order_date),
        str(amount),
        "UAH",                  # Валюта
        product_name_str,       # productName[0]
        "1",                    # productCount[0]
        str(amount)             # productPrice[0]
    ]
    
    if plan_type == "subscription":
        today_date_obj = date.today()
        next_month_date = today_date_obj + relativedelta(months=1) 
        regular_start_date_str = next_month_date.strftime("%Y-%m-%d")
    
    signature_params_list.extend([
        str(amount), # regularAmount
        "month",     # regularMode
        "1",         # regularInterval
        "0",         # regularCount (0 = неограниченно)
        regular_start_date_str # regularStartDate
        ])

    merchant_signature = make_wayforpay_signature(WAYFORPAY_SECRET_KEY, signature_params_list)

    base_backend_url = os.getenv('BACKEND_URL_BASE', 'https://payapi.dreamcatcher.guru') # ❗ ПРОВЕРИТЬ/НАСТРОИТЬ

    widget_params = {
        "merchantAccount": WAYFORPAY_MERCHANT_ACCOUNT,
        "merchantDomainName": WAYFORPAY_DOMAIN,
        "authorizationType": "SimpleSignature",
        "merchantSignature": merchant_signature,
        "orderReference": order_ref,
        "orderDate": str(order_date),
        "amount": str(amount),
        "currency": "UAH",
        "productName": [product_name_str], # Массив
        "productPrice": [str(amount)],    # Массив
        "productCount": ["1"],            # Массив
        "language": request_data.lang.upper() if request_data.lang and request_data.lang.upper() in ["UA", "RU", "EN"] else "UA",
        "serviceUrl": f"{base_backend_url}/api/pay/wayforpay-webhook", # URL для веб-хуков
        
        # Опциональные данные клиента (если бот их передает)
        "clientFirstName": request_data.client_first_name or "",
        "clientLastName": request_data.client_last_name or "",
        "clientEmail": request_data.client_email or "",
        "clientPhone": request_data.client_phone or ""
    }

    # Добавление параметров для регулярных платежей (если это подписка)
    # Эти параметры нужны, чтобы WayForPay создал recToken
    if plan_type == "subscription":
        today_date_obj = date.today()
        next_month_date = today_date_obj + relativedelta(months=1) 
        regular_start_date_str = next_month_date.strftime("%Y-%m-%d")
        widget_params.update({
            "regularMode": "month",
            "regularAmount": str(amount), 
            "regularCount": "0",          
            "regularStartDate": regular_start_date_str,
            "regularInterval": "1"        
        })

    logger.info(f"Сформированные параметры для виджета WayForPay: {widget_params}")
    
    # Сохраняем информацию о попытке платежа
    try:
        user_id_int = int(user_id_str)
    except ValueError:
        logger.error(f"Неверный user_id '{user_id_str}' для сохранения в payment_attempts.")
        raise HTTPException(status_code=400, detail="Invalid user_id format.")

    await db["payment_attempts"].insert_one({
        "orderReference": order_ref,
        "user_id": user_id_int,
        "plan_type": plan_type,
        "amount": amount,
        "status": "widget_params_generated",
        "created_utc": datetime.utcnow(),
        "widget_request_data": request_data.model_dump()
    })
    
    return widget_params

@payment_api_router.post("/wayforpay-webhook")
async def wayforpay_webhook(request: Request):
    data = await request.json()
    logger.info(f"Получен вебхук от WayForPay (/api/pay/wayforpay-webhook): {data}")
    
    received_signature = data.get("merchantSignature")
    order_ref = data.get("orderReference")

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

    sign_fields_webhook_clean = [str(f) for f in sign_fields_webhook if f is not None]

    webhook_signature_string = ';'.join(sign_fields_webhook_clean)
    # Для вебхуков обычно используется make_wayforpay_signature (с hexdigest)
    calculated_signature = make_wayforpay_signature(WAYFORPAY_SECRET_KEY, sign_fields_webhook_clean)

    if calculated_signature != received_signature:
        logger.error(f"Неверная подпись вебхука для orderReference {order_ref}. String: {webhook_signature_string}, Calc: {calculated_signature}, Recv: {received_signature}")
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    logger.info(f"Подпись вебхука для orderReference {order_ref} верна.")

    transaction_status = data.get("transactionStatus")
    user_id_from_client_account_id_str = data.get("clientAccountId")
    rec_token = data.get("recToken") 

    current_time_utc = datetime.utcnow()
    tz_kyiv = timezone('Europe/Kyiv')

    user_id_to_process = None

    if user_id_from_client_account_id_str:
        try:
            user_id_to_process = int(user_id_from_client_account_id_str)
            logger.info(f"User ID {user_id_to_process} получен из clientAccountId.")
        except ValueError:
            logger.error(f"Не удалось преобразовать clientAccountId '{user_id_from_client_account_id_str}' в int.")
            user_id_to_process = None 
    
    if user_id_to_process is None and order_ref:
        try:
            parts = order_ref.split('_')
            if len(parts) >= 3 and parts[0] == "widget" and (parts[1] == "sub" or parts[1] == "single"):
                user_id_to_process = int(parts[2]) 
                logger.info(f"User ID {user_id_to_process} извлечен из orderReference: {order_ref}")
            else:
                logger.warning(f"Не удалось извлечь user_id из orderReference: {order_ref}. Неверный формат префикса или недостаточно частей.")
        except (ValueError, IndexError) as e:
            logger.error(f"Ошибка извлечения user_id из orderReference {order_ref}: {e}")
            user_id_to_process = None 

    if not user_id_to_process:
        logger.error(f"Критическая ошибка: Не удалось определить user_id ни из clientAccountId, ни из orderReference ({order_ref}). Платеж не может быть присвоен пользователю.")
        response_time_utc_ts = int(current_time_utc.timestamp())
        response_params = [order_ref, "accept", str(response_time_utc_ts)]
        response_signature = make_wayforpay_signature(WAYFORPAY_SECRET_KEY, response_params)
        return {"orderReference": order_ref, "status": "accept", "time": response_time_utc_ts, "signature": response_signature}

    user_id = user_id_to_process 
    
    await db["payment_attempts"].update_one( 
        {"orderReference": order_ref},
        {"$set": {
            "user_id": user_id, 
            "status": transaction_status, 
            "webhook_received_utc": current_time_utc, 
            "webhook_data": data
        }},
        upsert=True 
    )

    if transaction_status == "Approved":
        plan_type_from_order_ref = None
        if order_ref.startswith("widget_sub_"):
            plan_type_from_order_ref = "subscription"
        elif order_ref.startswith("widget_single_"):
            plan_type_from_order_ref = "single"
        
        if not plan_type_from_order_ref:
            payment_attempt_doc = await db["payment_attempts"].find_one({"orderReference": order_ref, "user_id": user_id})
            plan_type_from_order_ref = payment_attempt_doc.get("plan_type") if payment_attempt_doc else None
            if payment_attempt_doc:
                logger.info(f"plan_type '{plan_type_from_order_ref}' взят из payment_attempts для orderReference {order_ref}")
            else:
                logger.error(f"Запись payment_attempts не найдена для orderReference {order_ref} и user_id {user_id}")


        if plan_type_from_order_ref == "subscription":
            end_date_utc = current_time_utc + timedelta(days=30) 
            end_date_kyiv_str = end_date_utc.astimezone(tz_kyiv).strftime("%Y-%m-%d")
            current_date_kyiv_str = current_time_utc.astimezone(tz_kyiv).strftime("%Y-%m-%d")

            update_data = {
                "user_id": user_id, "is_active": 1,
                "subscription_start": current_date_kyiv_str,
                "subscription_end": end_date_kyiv_str,
                "cancel_requested": 0,
                "plan_type": "subscription"
            }
            if rec_token:
                update_data["recToken"] = rec_token
                update_data["last_successful_charge_utc"] = current_time_utc
            else:
                logger.warning(f"recToken не получен для подписки user_id {user_id}, orderReference {order_ref}. Автосписания не будут работать.")

            await db["subscriptions"].update_one(
                {"user_id": user_id},
                {"$set": update_data},
                upsert=True
            )
            logger.info(f"Подписка для user_id {user_id} активирована/продлена до {end_date_kyiv_str}. recToken: {rec_token}")

        elif plan_type_from_order_ref == "single":
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
            logger.info(f"Разовый платеж (unlimited_today) для user_id {user_id} на {current_date_kyiv_str} активирован.")
        else:
            logger.error(f"Не удалось определить plan_type для Approved orderReference {order_ref} и user_id {user_id}. Платеж не обработан как услуга.")
    
    response_time_utc_ts = int(current_time_utc.timestamp())

    response_params = [order_ref, "accept", str(response_time_utc_ts)]
    response_signature = make_wayforpay_signature(WAYFORPAY_SECRET_KEY, response_params)
    return {"orderReference": order_ref, "status": "accept", "time": response_time_utc_ts, "signature": response_signature}

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