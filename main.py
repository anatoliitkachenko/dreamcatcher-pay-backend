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

    widget_params_to_send = {
        "merchantAccount": WAYFORPAY_MERCHANT_ACCOUNT,
        "merchantDomainName": WAYFORPAY_DOMAIN,
        "authorizationType": "SimpleSignature",
        # merchantSignature будет добавлен позже
        "orderReference": order_ref,
        "orderDate": str(order_date),
        "amount": str(amount),
        "currency": "UAH",
        "productName": [product_name_str],
        "productPrice": [str(amount)],
        "productCount": ["1"],
        "language": request_data.lang.upper() if request_data.lang and request_data.lang.upper() in ["UA", "RU", "EN"] else "UA",
        "serviceUrl": f"{base_backend_url}/api/pay/wayforpay-webhook",
        
        "clientFirstName": request_data.client_first_name or "N/A", # 🟢 Или реальные данные от бота
        "clientLastName": request_data.client_last_name or "N/A",   # 🟢
        "clientEmail": request_data.client_email or f"user_{user_id_str}@example.com", # 🟢 Должен быть валидный email
        "clientPhone": request_data.client_phone or "380000000000"    # 🟢 Должен быть валидный телефон
    }

    if plan_type == "subscription":
        today_date_obj = date.today()
        next_month_date = today_date_obj + relativedelta(months=1) 
        regular_start_date_str = next_month_date.strftime("%Y-%m-%d")
    
    widget_params_to_send.update({
            "regularMode": "month",
            "regularAmount": str(amount), 
            "regularCount": "0",          
            "regularStartDate": regular_start_date_str,
            "regularInterval": "1"        
        })

        # 🟢 ЕСЛИ ПЕРЕДАЕМ REGULAR-ПАРАМЕТРЫ, ОНИ ДОЛЖНЫ БЫТЬ В ПОДПИСИ!
        # 🔴 УТОЧНИТЕ ПОРЯДОК В ДОКУМЕНТАЦИИ WAYFORPAY ДЛЯ PURCHASE С РЕКУРРЕНТАМИ!
        # Это ПРЕДПОЛОЖИТЕЛЬНЫЙ порядок, их нужно добавить к `signature_params_list` выше.
        # Пример добавления (ПОРЯДОК ВАЖЕН И ДОЛЖЕН БЫТЬ ПРОВЕРЕН):
        # signature_params_list.extend([
        #     request_data.client_first_name or "N/A", # clientFirstName (если он в подписи)
        #     request_data.client_last_name or "N/A",  # clientLastName (если он в подписи)
        #     request_data.client_phone or "380000000000", # clientPhone (если он в подписи)
        #     request_data.client_email or f"user_{user_id_str}@example.com", # clientEmail (если он в подписи)
        #     # ... ДРУГИЕ ПОЛЯ, если они есть в подписи для Purchase...
        #     str(amount), # regularAmount (возможно, эти regular-поля идут позже)
        #     "month",     # regularMode
        #     "1",         # regularInterval
        #     "0",         # regularCount
        #     regular_start_date_str # regularStartDate
        # ])
        # 🔴 НА ДАННЫЙ МОМЕНТ Я НЕ ВКЛЮЧАЮ ИХ В ПОДПИСЬ, ТАК КАК ТОЧНЫЙ ПОРЯДОК НЕИЗВЕСТЕН.
        # 🔴 ЕСЛИ WAYFORPAY ТРЕБУЕТ ИХ В ПОДПИСИ ДЛЯ ВИДЖЕТА С REGULAR ПАРАМЕТРАМИ, ПЛАТЕЖ НЕ ПРОЙДЕТ.

    merchant_signature = make_wayforpay_signature(WAYFORPAY_SECRET_KEY, signature_params_list)
    widget_params_to_send["merchantSignature"] = merchant_signature # Добавляем подпись в параметры для виджета

    logger.info(f"Финальные параметры для виджета WayForPay (с подписью): {widget_params_to_send}")

    base_backend_url = os.getenv('BACKEND_URL_BASE', 'https://payapi.dreamcatcher.guru')

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
        "user_id": int(user_id_str),
        "plan_type": plan_type,
        "amount": amount,
        "status": "widget_params_generated",
        "created_utc": datetime.utcnow(),
        "widget_request_data": request_data.model_dump(),
        "sent_to_wfp_params": widget_params_to_send # Логируем то, что отправляем
    })
    
    return widget_params_to_send

@payment_api_router.post("/get-widget-params") # Имя эндпоинта изменено
async def get_widget_payment_params(request_data: WidgetParamsRequest): # Используем новую модель
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
        amount = 40 # ❗ Цена для разового анализа, если отличается от подписки
        product_name_str = "AI Dream Analysis (Single)"
        order_ref_prefix = "widget_single"
    else:
        logger.error(f"Invalid plan_type '{plan_type}' received for widget params.")
        raise HTTPException(status_code=400, detail="Invalid plan_type. Allowed: 'subscription', 'single'.")

    order_ref = f"{order_ref_prefix}_{user_id_str}_{int(datetime.utcnow().timestamp())}"
    order_date = int(datetime.utcnow().timestamp()) # Timestamp

    # 🟢 DEFINE base_backend_url BEFORE its first use
    base_backend_url = os.getenv('BACKEND_URL_BASE', 'https://payapi.dreamcatcher.guru') # ❗ ПРОВЕРЬТЕ, что BACKEND_URL_BASE в .env правильный

    # Параметры для строки подписи виджета
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
    
    # ❗ ВАЖНО ДЛЯ ПОДПИСОК (АВТОСПИСАНИЯ):
    # Если вы передаете 'regularMode', 'regularAmount' и т.д. в виджет,
    # эти параметры ТАКЖЕ ДОЛЖНЫ участвовать в формировании 'merchantSignature'
    # в ТОЧНОМ ПОРЯДКЕ, указанном в документации WayForPay для метода Purchase с рекуррентами.
    # Сейчас я их НЕ добавляю в 'signature_params_list' для простоты и потому что точный порядок
    # для всех этих полей вместе не был предоставлен в документации к виджету.
    # Если WayForPay будет требовать их в подписи, платеж через виджет не пройдет (ошибка WayForPay).
    # Сначала добейтесь работы без regular-параметров в ПОДПИСИ, но передавая их в виджет.
    # WayForPay должен создать recToken при успешной первой оплате, если ваш мерчант-аккаунт это поддерживает.

    merchant_signature = make_wayforpay_signature(WAYFORPAY_SECRET_KEY, signature_params_list)

    # Параметры, которые будут переданы в виджет
    widget_params_to_send = {
        "merchantAccount": WAYFORPAY_MERCHANT_ACCOUNT,
        "merchantDomainName": WAYFORPAY_DOMAIN,
        "authorizationType": "SimpleSignature",
        "merchantSignature": merchant_signature, # Подпись добавляется здесь
        "orderReference": order_ref,
        "orderDate": str(order_date),
        "amount": str(amount),
        "currency": "UAH",
        "productName": [product_name_str],    # Массив
        "productPrice": [str(amount)],       # Массив
        "productCount": ["1"],               # Массив
        "language": request_data.lang.upper() if request_data.lang and request_data.lang.upper() in ["UA", "RU", "EN"] else "UA",
        "serviceUrl": f"{base_backend_url}/api/pay/wayforpay-webhook", # Теперь base_backend_url определен
        
        # Обязательные клиентские данные для виджета (используем заглушки, если нет реальных данных)
        "clientFirstName": request_data.client_first_name or "N/A",
        "clientLastName": request_data.client_last_name or "N/A",
        "clientEmail": request_data.client_email or f"user_{user_id_str}@example.com", # Должен быть валидный формат email
        "clientPhone": request_data.client_phone or "380000000000" # Должен быть валидный формат телефона
    }

    # Добавление параметров для регулярных платежей (если это подписка)
    # Эти параметры нужны, чтобы WayForPay создал recToken
    if plan_type == "subscription":
        today_date_obj = date.today() # Убедитесь, что 'from datetime import date' есть
        # Убедитесь, что 'from dateutil.relativedelta import relativedelta' есть
        next_month_date = today_date_obj + relativedelta(months=1) 
        regular_start_date_str = next_month_date.strftime("%Y-%m-%d")
        
        widget_params_to_send.update({
            "regularMode": "month",
            "regularAmount": str(amount), 
            "regularCount": "0",          
            "regularStartDate": regular_start_date_str,
            "regularInterval": "1"        
        })

    logger.info(f"Финальные параметры для виджета WayForPay (с подписью): {widget_params_to_send}")
    
    try:
        user_id_int = int(user_id_str)
    except ValueError:
        logger.error(f"Неверный user_id '{user_id_str}' для сохранения в payment_attempts.")
        # Не выбрасываем HTTPException здесь, чтобы CORS-заголовки успели установиться,
        # но виджет получит некорректные параметры, если user_id критичен для него.
        # Однако, user_id для WayForPay передается через orderReference.
        # Проблема будет, если user_id не число для записи в вашу БД.
        # Лучше валидировать user_id на входе в Pydantic модели, если он всегда должен быть int.
        raise HTTPException(status_code=400, detail="Invalid user_id format for database.")


    await db["payment_attempts"].insert_one({
        "orderReference": order_ref,
        "user_id": user_id_int, # Используем преобразованный user_id_int
        "plan_type": plan_type,
        "amount": amount,
        "status": "widget_params_generated",
        "created_utc": datetime.utcnow(),
        "widget_request_data": request_data.model_dump(),
        "sent_to_wfp_params": widget_params_to_send # Логируем то, что будет отправлено в виджет
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