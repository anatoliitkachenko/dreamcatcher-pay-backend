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

# main.py - предлагаемые исправления
@payment_api_router.post("/get-widget-params")
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

    order_ref = f"{order_ref_prefix}_{user_id_str}_{int(datetime.utcnow().timestamp())}"
    order_date = int(datetime.utcnow().timestamp())
    
    base_backend_url = os.getenv('BACKEND_URL_BASE', 'https://payapi.dreamcatcher.guru')

    # 1. Упрощаем signature_params_list до базовых полей (согласно документации API Purchase)
    signature_params_list = [
        WAYFORPAY_MERCHANT_ACCOUNT,
        WAYFORPAY_DOMAIN,
        order_ref,
        str(order_date),
        str(amount),
        "UAH",
        product_name_str,   # productName[0]
        "1",                # productCount[0]
        str(amount)         # productPrice[0]
    ]
    
    # Логируем строку, которая будет подписана
    string_to_sign = ';'.join(str(x) for x in signature_params_list)
    logger.info(f"Строка для подписи (String to sign): {string_to_sign}")
    
    merchant_signature = make_wayforpay_signature(WAYFORPAY_SECRET_KEY, signature_params_list)
    logger.info(f"Сгенерированная подпись: {merchant_signature}")

    # Параметры, которые будут переданы в виджет
    widget_params_to_send = {
        "merchantAccount": WAYFORPAY_MERCHANT_ACCOUNT,
        "merchantDomainName": WAYFORPAY_DOMAIN,
        "authorizationType": "SimpleSignature",
        "merchantSignature": merchant_signature, # Используем "простую" подпись
        "orderReference": order_ref,
        "orderDate": str(order_date),
        "amount": str(amount),
        "currency": "UAH",
        "productName": [product_name_str],
        "productPrice": [str(amount)],
        "productCount": ["1"],
        "language": request_data.lang.upper() if request_data.lang and request_data.lang.upper() in ["UA", "RU", "EN"] else "UA",
        "serviceUrl": f"{base_backend_url}/api/pay/wayforpay-webhook",
        
        "clientFirstName": request_data.client_first_name or "N/A",
        "clientLastName": request_data.client_last_name or "N/A",
        "clientEmail": request_data.client_email or f"user_{user_id_str}@example.com",
        "clientPhone": request_data.client_phone or "380000000000"
    }

    if plan_type == "subscription":
        today_date_obj = date.today()
        next_month_date = today_date_obj + relativedelta(months=1)
        # 2. ИСПРАВЛЯЕМ ФОРМАТ ДАТЫ для regularStartDate на ДД.ММ.ГГГГ
        regular_start_date_str = next_month_date.strftime("%d.%m.%Y") 
        
        regular_params_for_widget = {
            "regularMode": "month",
            "regularAmount": str(amount), 
            "regularCount": "0",          
            "regularStartDate": regular_start_date_str, # <--- Используем дату в ПРАВИЛЬНОМ формате
            "regularInterval": "1"        
        }
        widget_params_to_send.update(regular_params_for_widget)

    logger.info(f"Финальные параметры для виджета WayForPay (с подписью): {widget_params_to_send}")
    
    try:
        user_id_int = int(user_id_str)
    except ValueError:
        logger.error(f"Неверный user_id '{user_id_str}' для сохранения в payment_attempts.")
        raise HTTPException(status_code=400, detail="Invalid user_id format for database.")

    await db["payment_attempts"].insert_one({
        "orderReference": order_ref,
        "user_id": user_id_int,
        "plan_type": plan_type,
        "amount": amount,
        "status": "widget_params_generated",
        "created_utc": datetime.utcnow(),
        "widget_request_data": request_data.model_dump(),
        "sent_to_wfp_params": widget_params_to_send
    })
    
    return widget_params_to_send

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