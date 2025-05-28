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
import logging # Добавляем логирование

load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MONGO_URI = os.getenv("MONGO_URI")
WAYFORPAY_MERCHANT_ACCOUNT = os.getenv("WAYFORPAY_MERCHANT_ACCOUNT")
WAYFORPAY_SECRET_KEY = os.getenv("WAYFORPAY_SECRET_KEY")
WAYFORPAY_DOMAIN = os.getenv("WAYFORPAY_DOMAIN") # Убедитесь, что это домен вашего магазина в WayForPay
# FRONTEND_URL не используется напрямую в логике ниже, но может быть полезен для returnUrl

mongo_client = AsyncIOMotorClient(MONGO_URI)
# Используем то же имя базы данных, что и в боте
db = mongo_client["dream_database"] # ИЗМЕНЕНО: dream_database как в боте

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Для разработки, в продакшене лучше указать конкретные домены
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
    # Эта функция используется для создания подписи для ИСХОДЯЩЕГО запроса на WayForPay
    # (когда вы отправляете пользователя на их страницу оплаты)
    sign_str = ';'.join(str(x) for x in params_list)
    # WayForPay для исходящих запросов часто использует просто HMAC MD5, без Base64
    # Но в вашем коде был base64, поэтому оставим его, если это так для вашей интеграции.
    # Уточните в документации WayForPay, нужна ли Base64 для подписи формы оплаты.
    # Обычно нет. Если нет, то:
    # return hmac.new(merchant_secret_key.encode(), sign_str.encode(), hashlib.md5).hexdigest()
    return base64.b64encode(hmac.new(merchant_secret_key.encode(), sign_str.encode(), hashlib.md5).digest()).decode()


@app.post("/create-checkout-session")
async def create_checkout_session(session: CheckoutSession):
    logger.info(f"Запрос на создание сессии: {session}")
    if session.plan_type not in ("subscription", "single"):
        logger.error(f"Неверный plan_type: {session.plan_type}")
        raise HTTPException(status_code=400, detail="Invalid plan_type")

    amount = 300 if session.plan_type == "subscription" else 40
    order_ref = f"order_{session.user_id}_{session.plan_type}_{int(datetime.utcnow().timestamp())}"
    order_date = int(datetime.utcnow().timestamp())
    
    # ВАЖНО: returnUrl и serviceUrl должны быть настроены в вашем кабинете WayForPay.
    # serviceUrl должен указывать на ваш /wayforpay-webhook
    # returnUrl - куда пользователь вернется после оплаты.
    # Эти URL можно также передавать в параметрах формы, если ваша интеграция это поддерживает.

    # Параметры для подписи ИСХОДЯЩЕГО запроса (формы оплаты)
    # Порядок и набор полей КРИТИЧЕСКИ ВАЖНЫ. Проверьте документацию WayForPay.
    params_for_signature = [
        WAYFORPAY_MERCHANT_ACCOUNT,
        WAYFORPAY_DOMAIN, # или merchantSite (если используется)
        order_ref,
        str(order_date), # обязательно строка
        str(amount), # обязательно строка
        "UAH",
        "AI Dream Analysis", # productName[0]
        "1", # productCount[0]
        str(amount) # productPrice[0]
    ]
    # Если у вас несколько продуктов, то productName, productCount, productPrice будут массивами,
    # и их нужно будет добавить в строку для подписи соответствующим образом.

    # Пример добавления clientAccountId в строку для подписи, если это требуется
    # params_for_signature.append(session.user_id)

    logger.info(f"Строка для исходящей подписи (перед генерацией): {';'.join(params_for_signature)}")
    merchant_signature = make_outgoing_signature(WAYFORPAY_SECRET_KEY, params_for_signature)
    logger.info(f"Сгенерированная исходящая подпись: {merchant_signature}")

    payment_form_data = {
        "merchantAccount": WAYFORPAY_MERCHANT_ACCOUNT,
        "merchantAuthType": "SimpleSignature", # или другой тип авторизации, если используется
        "merchantDomainName": WAYFORPAY_DOMAIN,
        "orderReference": order_ref,
        "orderDate": str(order_date), # WayForPay ожидает строку
        "amount": str(amount), # WayForPay ожидает строку
        "currency": "UAH",
        "productName[]": ["AI Dream Analysis"], # Обратите внимание на [] для массивов
        "productCount[]": ["1"],
        "productPrice[]": [str(amount)],
        "clientFirstName": session.first_name or "",
        "clientLastName": "", # Можно добавить, если есть
        "clientEmail": "", # Можно добавить, если есть и нужно
        "clientPhone": "", # Можно добавить, если есть и нужно
        "clientAccountId": session.user_id, # ВАЖНО: передаем ID пользователя Telegram
        "merchantSignature": merchant_signature,
        "language": "UA", # или RU, EN
        # ВАЖНО: Укажите URL для возврата пользователя и для получения вебхука (serviceUrl)
        # Эти URL также должны быть прописаны в настройках магазина в WayForPay
        "returnUrl": f"{os.getenv('FRONTEND_URL', 'https://dreamcatcher.guru')}/payment-return", # Пример URL, куда вернется пользователь
        "serviceUrl": f"{os.getenv('BACKEND_URL_BASE', 'https://dreamcatcher.guru')}/wayforpay-webhook" # URL вашего вебхука
    }
    logger.info(f"Данные формы для WayForPay: {payment_form_data}")

    await db["checkout_sessions"].insert_one({ # Используем коллекцию checkout_sessions
        "orderReference": order_ref,
        "user_id": int(session.user_id), # Сохраняем как int, если бот использует int
        "plan_type": session.plan_type,
        "amount": amount,
        "status": "created",
        "created_utc": datetime.utcnow() # Используем UTC
    })

    # Отправляем URL для прямого перехода или данные для автосабмита формы через JS на фронте
    return {"pay_url": "https://secure.wayforpay.com/pay", "payment_form_data": payment_form_data}


@app.post("/wayforpay-webhook")
async def wayforpay_webhook(request: Request):
    try:
        data = await request.json()
    except Exception as e:
        logger.error(f"Ошибка парсинга JSON из вебхука: {e}")
        # WayForPay все равно ожидает ответ, даже если мы не можем распарсить тело.
        # Вернуть ошибку, но в формате, который WayForPay может обработать, если такой есть.
        # Обычно, если тело не JSON, то это уже проблема на стороне WayForPay или конфигурации.
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    logger.info(f"Получен вебхук от WayForPay: {data}")

    received_signature = data.get("merchantSignature")
    order_ref = data.get("orderReference")

    # --- ПРОВЕРКА ПОДПИСИ ВЕБХУКА ---
    # Порядок и набор полей для подписи ВХОДЯЩЕГО вебхука КРИТИЧЕСКИ ВАЖНЫ.
    # Для статуса "Approved" это обычно: orderReference;status;time (но может включать больше полей)
    # Уточните в документации WayForPay точный список полей для подписи вебхука.
    # Это ПРИМЕРНЫЙ список, он может отличаться для вашего мерчанта или типа транзакции!
    
    # Поля, обычно участвующие в подписи вебхука (для Approved статуса):
    # merchantAccount;orderReference;amount;currency;authCode;cardPan;transactionStatus;reasonCode
    # Убедитесь, что все эти поля есть в `data` и что они строки (кроме amount, которое может быть числом, но для подписи приводится к строке)
    
    fields_for_webhook_signature = []
    if data.get("transactionStatus") == "Approved":
         # Этот список ДОЛЖЕН БЫТЬ точным и соответствовать документации WayForPay для ВЕБХУКА
        fields_for_webhook_signature = [
            str(data.get("merchantAccount")), # Обычно это ваш merchant login
            str(data.get("orderReference")),
            str(data.get("amount")), # Сумма
            str(data.get("currency")), # Валюта
            str(data.get("authCode")), # Код авторизации
            str(data.get("cardPan")), # Маскированный номер карты
            str(data.get("transactionStatus")), # Статус транзакции
            str(data.get("reasonCode")) # Код причины
        ]
    else:
        # Для других статусов строка подписи может быть короче, например:
        # orderReference;status;time
        # Уточните это в документации!
        fields_for_webhook_signature = [
            str(data.get("orderReference")),
            str(data.get("transactionStatus")),
            str(data.get("time")) # Время транзакции (timestamp)
        ]

    # Удаляем None значения, если какие-то поля могут отсутствовать и не должны участвовать в подписи
    # fields_for_webhook_signature = [f for f in fields_for_webhook_signature if f is not None and f != "None"]

    webhook_signature_string = ';'.join(fields_for_webhook_signature)
    logger.info(f"Строка для проверки подписи вебхука: {webhook_signature_string}")
    
    # Для вебхуков подпись обычно HMAC-MD5 в hex-формате, БЕЗ Base64
    calculated_signature = hmac.new(WAYFORPAY_SECRET_KEY.encode(), webhook_signature_string.encode(), hashlib.md5).hexdigest()
    logger.info(f"Рассчитанная подпись вебхука: {calculated_signature}, Полученная: {received_signature}")

    if calculated_signature != received_signature:
        logger.error(f"Неверная подпись вебхука для orderReference {order_ref}. Ожидалась: {calculated_signature}, получена: {received_signature}")
        # Важно: даже при ошибке подписи, WayForPay может ожидать определенный ответ.
        # Но если подпись неверна, данные нельзя доверять.
        # Можно просто вернуть ошибку, но WayForPay может начать повторять запрос.
        # Лучше всего залогировать и вернуть ответ, который остановит повторы, если такой есть.
        # Обычно, если подпись неверна, обработку прекращают.
        raise HTTPException(status_code=400, detail="Invalid webhook signature")
    
    logger.info(f"Подпись вебхука для orderReference {order_ref} верна.")
    # --- КОНЕЦ ПРОВЕРКИ ПОДПИСИ ВЕБХУКА ---

    transaction_status = data.get("transactionStatus")
    user_id_str = data.get("clientAccountId") # ID пользователя Telegram

    if not user_id_str:
        logger.error(f"clientAccountId (user_id) отсутствует в вебхуке для orderReference {order_ref}")
        # Все равно нужно ответить WayForPay
        # Здесь и далее, перед return, формируем ответ для WayForPay
        response_time = int(datetime.utcnow().timestamp())
        # Для неуспешных или ошибочных сценариев строка и статус могут быть другими
        response_signature_str = f"{order_ref};accept;{response_time}" # Пример, уточнить!
        response_signature = hmac.new(WAYFORPAY_SECRET_KEY.encode(), response_signature_str.encode(), hashlib.md5).hexdigest()
        return {
            "orderReference": order_ref, "status": "accept", "time": response_time, "signature": response_signature
        }

    try:
        user_id = int(user_id_str)
    except ValueError:
        logger.error(f"Неверный формат clientAccountId '{user_id_str}' в вебхуке для orderReference {order_ref}")
        # Отвечаем WayForPay
        response_time = int(datetime.utcnow().timestamp())
        response_signature_str = f"{order_ref};accept;{response_time}" # Пример, уточнить!
        response_signature = hmac.new(WAYFORPAY_SECRET_KEY.encode(), response_signature_str.encode(), hashlib.md5).hexdigest()
        return {
            "orderReference": order_ref, "status": "accept", "time": response_time, "signature": response_signature
        }

    # Обновляем сессию в нашей БД
    await db["checkout_sessions"].update_one(
        {"orderReference": order_ref, "user_id": user_id}, # Важно найти сессию по user_id тоже
        {"$set": {"status": transaction_status, "webhook_received_utc": datetime.utcnow(), "webhook_data": data}}
    )

    if transaction_status == "Approved":
        checkout_session = await db["checkout_sessions"].find_one({"orderReference": order_ref, "user_id": user_id})
        if not checkout_session:
            logger.error(f"Сессия checkout_sessions не найдена для orderReference {order_ref} и user_id {user_id}")
            # Отвечаем WayForPay
            response_time = int(datetime.utcnow().timestamp())
            response_signature_str = f"{order_ref};accept;{response_time}"
            response_signature = hmac.new(WAYFORPAY_SECRET_KEY.encode(), response_signature_str.encode(), hashlib.md5).hexdigest()
            return {
                "orderReference": order_ref, "status": "accept", "time": response_time, "signature": response_signature
            }

        plan_type = checkout_session.get("plan_type")
        current_time_utc = datetime.utcnow()
        tz_kyiv = timezone('Europe/Kyiv') # Из вашего бота
        current_date_kyiv_str = current_time_utc.astimezone(tz_kyiv).strftime("%Y-%m-%d")
        current_month_kyiv_str = current_time_utc.astimezone(tz_kyiv).strftime("%Y-%m")


        if plan_type == "subscription":
            end_date_utc = current_time_utc + timedelta(days=30)
            end_date_kyiv_str = end_date_utc.astimezone(tz_kyiv).strftime("%Y-%m-%d")
            
            # Обновляем или создаем подписку в формате, ожидаемом ботом
            await db["subscriptions"].update_one(
                {"user_id": user_id},
                {"$set": {
                    "user_id": user_id, # Убедимся, что поле есть
                    "is_active": 1, # Бот использует 1/0
                    "subscription_start": current_date_kyiv_str, # Дата начала в формате бота
                    "subscription_end": end_date_kyiv_str, # Дата окончания в формате бота
                    "cancel_requested": 0 # Инициализируем
                }},
                upsert=True
            )
            logger.info(f"Подписка активирована/обновлена для user_id {user_id} до {end_date_kyiv_str} (order: {order_ref})")
        
        elif plan_type == "single":
            # Для разового платежа бот ставит unlimited_today = 1 в usage_limits
            await db["usage_limits"].update_one(
                {"user_id": user_id, "date": current_date_kyiv_str},
                {
                    "$set": {"unlimited_today": 1},
                    "$setOnInsert": { # Если документа на сегодня нет
                        "user_id": user_id,
                        "date": current_date_kyiv_str,
                        "dream_count": 0,
                        "monthly_count": 0, # Подумайте, как здесь обрабатывать месячный счетчик
                        "last_reset_month": current_month_kyiv_str,
                        "first_usage_date": current_date_kyiv_str
                    }
                },
                upsert=True
            )
            logger.info(f"Разовый платеж (unlimited_today) активирован для user_id {user_id} на {current_date_kyiv_str} (order: {order_ref})")
        
        else:
            logger.error(f"Неизвестный plan_type '{plan_type}' в сессии для orderReference {order_ref}")


    # --- ФОРМИРОВАНИЕ ОТВЕТА ДЛЯ WAYFORPAY ---
    # WayForPay ожидает ответ в JSON с полями orderReference, status ("accept" или "decline"), time и signature.
    response_time_utc = int(current_time_utc.timestamp())
    # Строка для подписи ответа: orderReference;status;time
    # Статус "accept" означает, что вы успешно приняли и обработали вебхук.
    response_signature_str = f"{order_ref};accept;{response_time_utc}"
    response_signature = hmac.new(WAYFORPAY_SECRET_KEY.encode(), response_signature_str.encode(), hashlib.md5).hexdigest()
    
    response_to_wayforpay = {
        "orderReference": order_ref,
        "status": "accept",
        "time": response_time_utc,
        "signature": response_signature
    }
    logger.info(f"Ответ для WayForPay для orderReference {order_ref}: {response_to_wayforpay}")
    return response_to_wayforpay


@app.get("/check-access") 
async def check_access(user_id: str): 
    try:
        user_id_int = int(user_id)
    except ValueError:
        logger.warning(f"Неверный user_id в /check-access: {user_id}")
        return {"active": False}
    
    tz_kyiv = timezone('Europe/Kyiv') 
    today_kyiv_str = datetime.now(tz_kyiv).strftime("%Y-%m-%d")

    sub = await db["subscriptions"].find_one({"user_id": user_id_int}) 
    
    if sub and sub.get("is_active") == 1 and sub.get("subscription_end") >= today_kyiv_str:
        logger.info(f"Доступ активен для user_id {user_id_int}. Дата окончания: {sub.get('subscription_end')}")
        return {"active": True}
    
    logger.info(f"Доступ неактивен для user_id {user_id_int}. Данные подписки: {sub}")
    return {"active": False}

from pytz import timezone