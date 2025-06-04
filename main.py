import os
import hmac
import hashlib
import base64
import re
import json
from datetime import datetime, timedelta, date
from dateutil.relativedelta import relativedelta
from fastapi import FastAPI, Request, HTTPException, APIRouter, Body
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from datetime import datetime, timedelta, date
from typing import List, Optional, Dict, Any, Union
from motor.motor_asyncio import AsyncIOMotorClient
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import logging
import aiohttp
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

# URL для внутреннего API уведомлений бота
BOT_NOTIFICATION_URL = os.getenv('BOT_NOTIFICATION_URL', 'http://157.90.119.107:8001/internal-api/notify') # <--- УКАЖИТЕ РЕАЛЬНЫЙ URL и порт!

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

class WayForPayServiceWebhook(BaseModel):
    merchantAccount: str
    orderReference: str
    merchantSignature: str # Подпись от WayForPay, которую нужно проверить
    amount: float
    currency: str
    authCode: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    createdDate: Optional[int] = None # В примере ответа на CHECK_STATUS это int, в примере serviceUrl webhook - тоже
    processingDate: Optional[int] = None
    cardPan: Optional[str] = None
    cardType: Optional[str] = None
    issuerBankCountry: Optional[str] = None # В примере ответа на CHECK_STATUS это строка "UA", в serviceUrl webhook "980"
    issuerBankName: Optional[str] = None
    recToken: Optional[str] = None
    transactionStatus: str
    reason: Optional[str] = None 
    reasonCode: Optional[int] = None
    paymentSystem: Optional[str] = None
    repayUrl: Optional[str] = None
    class Config:
        extra = 'allow' # или 'ignore'

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
        amount = 1 # Было 300, теперь 1 для теста
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
        "merchantAuthType": "SimpleSignature",
        "merchantDomainName": WAYFORPAY_DOMAIN,
        "merchantSignature": merchant_signature, # Используем "простую" подпись
        "language": request_data.lang.upper() if request_data.lang and request_data.lang.upper() in ["UA", "RU", "EN"] else "UA",
        "serviceUrl": f"{base_backend_url}/api/pay/wayforpay-webhook",
        "orderReference": order_ref,
        "orderDate": str(order_date),
        "amount": str(amount),
        "currency": "UAH",
        "productName": [product_name_str],
        "productPrice": [str(amount)],
        "productCount": ["1"],
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
            "regularMode": "monthly",
            "regularAmount": str(amount), 
            "regularCount": "12",          
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

# --- Функция для генерации подписи ответа вашего serviceUrl для WayForPay ---
def make_service_response_signature(secret_key: str, order_reference: str, status: str, time_unix: int) -> str:
    sign_str = f"{order_reference};{status};{str(time_unix)}"
    logger.info(f"Service URL response string to sign: '{sign_str}'")
    signature = hmac.new(secret_key.encode(), sign_str.encode(), hashlib.md5).hexdigest()
    logger.info(f"Service URL response generated signature: '{signature}'")
    return signature

def verify_service_webhook_signature(secret_key: str, data: WayForPayServiceWebhook) -> bool:
    auth_code_for_sig = data.authCode if data.authCode is not None else ""
    card_pan_for_sig = data.cardPan if data.cardPan is not None else ""
    # reasonCode может быть int или str, приводим к строке для единообразия
    reason_code_for_sig = str(data.reasonCode) if data.reasonCode is not None else ""


    fields_for_signature_check = [
        data.merchantAccount,
        data.orderReference,
        str(int(data.amount)) if data.amount and data.amount == int(data.amount) else str(data.amount),
        data.currency,
        auth_code_for_sig,
        card_pan_for_sig,
        data.transactionStatus,
        reason_code_for_sig 
    ]
    
    sign_str_to_check = ';'.join(fields_for_signature_check)
    expected_signature = hmac.new(secret_key.encode(), sign_str_to_check.encode(), hashlib.md5).hexdigest()
    
    logger.info(f"Verifying service webhook signature. String: '{sign_str_to_check}', Expected: '{expected_signature}', Received: '{data.merchantSignature}'")
    if expected_signature == data.merchantSignature:
        logger.info(f"Service webhook signature VERIFIED for OrderRef: {data.orderReference}")
        return True
    else:
        logger.error(f"!!! Service webhook signature MISMATCH for OrderRef: {data.orderReference} !!!")
        return False

# --- Эндпоинт для приема веб-хуков от WayForPay ---
@payment_api_router.post("/wayforpay-webhook", include_in_schema=False)
async def wayforpay_webhook_handler(request: Request): # Принимаем только объект Request
    content_type = request.headers.get("content-type")
    logger.info(f"ОТРИМАНО ВЕБ-ХУК. Content-Type: {content_type}")

    raw_body = await request.body() # Получаем сырые байты тела запроса
    logger.info(f"RAW Webhook Body (bytes): {raw_body[:1000]}") # Логируем первые 1000 байт сырого тела

    data_to_process = {} # Словарь для данных после парсинга

    try:
        # Декодируем сырое тело запроса в строку.
            # Это предварительный шаг, чтобы потом работать с текстовым JSON.
        body_str_for_parsing = ""
        try:
            body_str_for_parsing = raw_body.decode('utf-8')
        except UnicodeDecodeError as e_unicode:
            logger.error(f"UnicodeDecodeError when decoding raw body: {e_unicode}. Body (partial bytes): {raw_body[:100]}")
            raise ValueError(f"Cannot decode raw body from UTF-8: {e_unicode}") # Прерываем выполнение, если не можем декодировать

        logger.info(f"Attempting to parse the entire DECODED body string as JSON. Decoded body for parsing: {body_str_for_parsing[:1000]}")

        if not body_str_for_parsing.strip(): # Проверяем, не пустая ли строка после удаления пробелов
            logger.warning("Decoded body string is empty or whitespace. Cannot parse as JSON.")
            raise ValueError("Empty or whitespace decoded body string received, cannot parse as JSON.")
            
        # Основная попытка парсинга: считаем, что вся строка body_str_for_parsing - это JSON
        data_to_process = json.loads(body_str_for_parsing)

        # Проверка, что результат парсинга - это словарь
        if not isinstance(data_to_process, dict):
            logger.error(f"Parsing decoded body as JSON did not result in a dictionary. Parsed type: {type(data_to_process)}. Data: {str(data_to_process)[:1000]}")
            raise ValueError(f"Expected a JSON object (dict) after parsing, but got {type(data_to_process)}")
            
        # Если data_to_process пустой словарь {} (валидный JSON), Pydantic это отловит ниже, если поля обязательные
        # Поэтому отдельная проверка if not data_to_process не так критична здесь, если это dict.

        logger.info(f"Данні веб-хука для Pydantic валідації (Parsed Dict from decoded body): {str(data_to_process)[:1000]}")
            
        # Теперь попытка валидации через Pydantic с полученным словарем data_to_process
        webhook_data = WayForPayServiceWebhook(**data_to_process)
        logger.info(f"Веб-хук УСПІШНО провалідований Pydantic: {webhook_data.model_dump_json(indent=2)[:1000]}")

    except Exception as e_parse_or_pydantic: # Ловим ошибки парсинга ИЛИ Pydantic валидации
        logger.error(f"!!! ПОМИЛКА ОБРОБКИ/ВАЛІДАЦІЇ ВЕБ-ХУКА !!!: {e_parse_or_pydantic}")
        # Логируем данные, которые вызвали ошибку (если они были получены)
        if data_to_process: # Если data_to_process было как-то заполнено до ошибки
            logger.error(f"Дані, що викликали помилку (data_to_process): {str(data_to_process)[:1000]}")
        else: # Если data_to_process пустое (например, ошибка на этапе json.loads(body_str_for_log))
            logger.error(f"Дані, що викликали помилку (body_str_for_log): {body_str_for_log[:1000]}")

        # Формируем ответ для WayForPay даже при ошибке
        # Пытаемся извлечь orderReference из сырых данных или data_to_process для ответа
        temp_order_ref = "UNKNOWN_ORDER_REF_ERROR"
        if isinstance(data_to_process, dict) and data_to_process.get("orderReference"):
            temp_order_ref = data_to_process.get("orderReference")
        else: # Попытка найти orderReference в сырой строке (если парсинг до словаря не удался)
            try:
                match_order_ref = re.search(r'"orderReference"\s*:\s*"([^"]+)"', body_str_for_log)
                if match_order_ref:
                    temp_order_ref = match_order_ref.group(1)
            except Exception:
                pass # Игнорируем, если не удалось извлечь

        response_time_unix = int(datetime.utcnow().timestamp())
        try:
            response_sig = make_service_response_signature(WAYFORPAY_SECRET_KEY, temp_order_ref, "accept", response_time_unix)
        except Exception: # На случай, если даже генерация подписи для ответа упадет
            response_sig = "error_generating_signature_on_error_path"
        return {"orderReference": temp_order_ref, "status": "accept", "time": response_time_unix, "signature": response_sig}

    if not verify_service_webhook_signature(WAYFORPAY_SECRET_KEY, webhook_data):
        logger.error(f"CRITICAL: Invalid signature in webhook from WayForPay! OrderRef: {webhook_data.orderReference}. Data will not be processed.")
        # Формируем стандартный "ОК" ответ для WayForPay, чтобы прекратить повторные отправки.
        response_time_unix = int(datetime.utcnow().timestamp())
        response_sig = make_service_response_signature(WAYFORPAY_SECRET_KEY, webhook_data.orderReference, "accept", response_time_unix)
        return {"orderReference": webhook_data.orderReference, "status": "accept", "time": response_time_unix, "signature": response_sig}
    # Если раскомментируете проверку выше, дальнейший код будет выполняться только при верной подписи.

    # Извлечение telegram_user_id из orderReference
    match = re.search(r"_(?P<user_id>\d+)_", webhook_data.orderReference)
    if not match:
        logger.error(f"Could not extract user_id from orderReference: {webhook_data.orderReference}")
        response_time_unix = int(datetime.utcnow().timestamp())
        response_sig = make_service_response_signature(WAYFORPAY_SECRET_KEY, webhook_data.orderReference, "accept", response_time_unix)
        return {"orderReference": webhook_data.orderReference, "status": "accept", "time": response_time_unix, "signature": response_sig}

    telegram_user_id = int(match.group("user_id"))

    # Обновляем запись о попытке платежа (или создаем, если это первый веб-хук по этому orderReference)
    await db["payment_attempts"].update_one(
        {"orderReference": webhook_data.orderReference},
        {"$set": {
            "status": webhook_data.transactionStatus, 
            "wfp_webhook_received_utc": datetime.utcnow(),
            "wfp_webhook_data": webhook_data.model_dump()
            }
        },
        upsert=True # Создаст запись, если такой orderReference еще не было
    )

    if webhook_data.transactionStatus == "Approved":
        logger.info(f"Payment APPROVED for orderReference: {webhook_data.orderReference}, user_id: {telegram_user_id}")
        
        rec_token = webhook_data.recToken
        if not rec_token:
            logger.warning(f"REC TOKEN IS EMPTY for successful payment! OrderRef: {webhook_data.orderReference}. Automatic renewals will not be possible.")
        else:
            logger.info(f"Received recToken: {rec_token} for OrderRef: {webhook_data.orderReference}")

        try:
            kyiv_tz = timezone('Europe/Kyiv')
            current_sub = await db["subscriptions"].find_one({"user_id": telegram_user_id})
            
            start_date_obj = datetime.now(kyiv_tz) # По умолчанию, начало подписки - сейчас
            
            # Если есть активная подписка, продлеваем от ее даты окончания
            if current_sub and current_sub.get("is_active") and current_sub.get("subscription_end"):
                try:
                    current_end_date_obj = datetime.strptime(current_sub["subscription_end"], "%Y-%m-%d")
                    # Важно: если current_end_date_obj не имеет tzinfo, нужно его добавить или сравнивать наивные даты
                    # Для простоты, если дата окончания в будущем, считаем от нее
                    if current_end_date_obj > datetime.now().date(): # Сравниваем только даты
                        start_date_obj = datetime.combine(current_end_date_obj, datetime.min.time()) + timedelta(days=1)
                        start_date_obj = kyiv_tz.localize(start_date_obj) # Локализуем после создания
                except ValueError as ve:
                    logger.warning(f"Invalid subscription_end format ('{current_sub.get('subscription_end')}') for user_id {telegram_user_id}, starting new sub from today. Error: {ve}")
            
            # Рассчитываем новую дату окончания. Если amount = 1 (тест), можно сделать подписку на 1 день для теста.
            # days_to_add = 1 if webhook_data.amount == 1 else 30 
            days_to_add = 30 # Для реальной подписки
            new_end_date_obj = start_date_obj + relativedelta(days=days_to_add)

            update_fields = {
                "subscription_start": start_date_obj.strftime("%Y-%m-%d"),
                "subscription_end": new_end_date_obj.strftime("%Y-%m-%d"),
                "is_active": 1,
                "cancel_requested": 0,
                "rec_token": rec_token,
                "last_payment_order_ref": webhook_data.orderReference,
                "last_payment_status": "Approved",
                "payment_system": webhook_data.paymentSystem,
                "card_pan_mask": webhook_data.cardPan,
                "email_from_payment": webhook_data.email,
                "phone_from_payment": webhook_data.phone,
                "updated_at_utc": datetime.utcnow()
            }
            
            await db["subscriptions"].update_one(
                {"user_id": telegram_user_id},
                {"$set": update_fields, "$setOnInsert": {"user_id": telegram_user_id, "created_at_utc": datetime.utcnow()}},
                upsert=True
            )
            logger.info(f"Subscription activated/extended for user_id: {telegram_user_id} until {new_end_date_obj.strftime('%Y-%m-%d')}. RecToken: {rec_token}")

            # Отправка уведомления об успешной подписке
            notification_data = {
                'user_id': telegram_user_id,
                'message_type': 'subscription_success',
                'details': {
                    'end_date': new_end_date_obj.strftime('%Y-%m-%d'),
                    'rec_token': rec_token,
                    'order_ref': webhook_data.orderReference
                }
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(BOT_NOTIFICATION_URL, json=notification_data) as resp:
                    if resp.status == 200:
                        logger.info(f"Уведомление об успешной подписке отправлено боту для user {telegram_user_id}")
                    else:
                        logger.error(f"Ошибка при отправке уведомления боту (статус {resp.status}): {await resp.text()}")

        except Exception as e:
            logger.error(f"Error updating subscription in DB for user_id {telegram_user_id}: {e}")
            # Запрос все равно должен вернуть "accept", чтобы WayForPay не слал повторно

    elif webhook_data.transactionStatus == "Pending":
        logger.info(f"Payment PENDING for orderReference: {webhook_data.orderReference}, user_id: {telegram_user_id}")
        # Действий с подпиской не предпринимаем, ждем финального статуса.
    
    else: # Declined, Expired и т.д.
        logger.warning(f"Payment NOT APPROVED. Status: {webhook_data.transactionStatus}, Reason: {webhook_data.reason} (Code: {webhook_data.reasonCode}) for orderReference: {webhook_data.orderReference}")
        # TODO: Интеграция с Telegram-ботом для отправки уведомления о неудаче
        await send_telegram_notification_to_user(telegram_user_id, "К сожалению, ваш платеж не прошел. Причина: " + str(webhook_data.reason))


    # Формируем и отправляем ответ WayForPay
    response_time_unix = int(datetime.utcnow().timestamp())
    response_signature = make_service_response_signature(WAYFORPAY_SECRET_KEY, webhook_data.orderReference, "accept", response_time_unix)
    
    return {
        "orderReference": webhook_data.orderReference,
        "status": "accept",
        "time": response_time_unix,
        "signature": response_signature
    }

app.include_router(payment_api_router)