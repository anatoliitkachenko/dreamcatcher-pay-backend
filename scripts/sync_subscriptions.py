import os
import asyncio
import logging
from datetime import datetime

import aiohttp
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

# Настройка логирования для нашего скрипта
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Загрузка конфигурации ---
# Загружаем переменные из файла .env, который лежит в той же папке
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
WAYFORPAY_MERCHANT_ACCOUNT = os.getenv("WAYFORPAY_MERCHANT_ACCOUNT")
WAYFORPAY_MERCHANT_PASSWORD = os.getenv("WAYFORPAY_MERCHANT_PASSWORD")

# Проверяем, что все переменные загрузились
if not all([MONGO_URI, WAYFORPAY_MERCHANT_ACCOUNT, WAYFORPAY_MERCHANT_PASSWORD]):
    logging.error("Критическая ошибка: не все переменные окружения загружены (.env).")
    exit()

# API URL для запросов статуса
WFP_REGULAR_API_URL = "https://api.wayforpay.com/regularApi"


async def check_wfp_status(session, order_reference: str) -> dict | None:
    """Отправляет запрос STATUS в WayForPay и возвращает ответ."""
    request_data = {
        "requestType": "STATUS",
        "merchantAccount": WAYFORPAY_MERCHANT_ACCOUNT,
        "merchantPassword": WAYFORPAY_MERCHANT_PASSWORD,
        "orderReference": order_reference
    }
    try:
        async with session.post(WFP_REGULAR_API_URL, json=request_data, timeout=15) as resp:
            if resp.status == 200:
                return await resp.json()
            logging.error(f"Ошибка запроса статуса для {order_reference}. Статус: {resp.status}")
            return None
    except Exception as e:
        logging.error(f"Исключение при запросе статуса для {order_reference}: {e}")
        return None


async def sync_statuses():
    """Основная функция для синхронизации статусов подписок."""
    logging.info("--- Начало сессии синхронизации статусов подписок ---")
    
    mongo_client = None
    updated_count = 0
    discrepancy_count = 0

    try:
        # Подключаемся к MongoDB
        mongo_client = AsyncIOMotorClient(MONGO_URI)
        db = mongo_client["dream_database"]
        subscriptions_collection = db["subscriptions"]

        # Находим всех пользователей, у которых подписка считается активной в нашей базе
        active_subs_cursor = subscriptions_collection.find({"is_active": 1})
        
        async with aiohttp.ClientSession() as session:
            # Асинхронно итерируемся по найденным подпискам
            async for sub in active_subs_cursor:
                user_id = sub.get("user_id")
                order_ref = sub.get("last_payment_order_ref")

                if not order_ref:
                    logging.warning(f"Пропуск user_id {user_id}, отсутствует orderReference.")
                    continue

                logging.info(f"Проверка статуса для user_id {user_id}, order_ref {order_ref}...")
                
                wfp_response = await check_wfp_status(session, order_ref)

                if wfp_response and wfp_response.get("reasonCode") == 4100:
                    wfp_status = wfp_response.get("status")
                    logging.info(f"Статус в WayForPay для user_id {user_id} - '{wfp_status}'.")

                    # Самое главное: если статус в WayForPay НЕ 'Active'
                    if wfp_status != "Active":
                        discrepancy_count += 1
                        logging.warning(f"!!! РАСХОЖДЕНИЕ НАЙДЕНО для user_id {user_id}. Локальный статус: active, статус WFP: {wfp_status}. Деактивируем подписку.")
                        
                        # Обновляем запись в нашей базе
                        result = await subscriptions_collection.update_one(
                            {"_id": sub["_id"]},
                            {"$set": {"is_active": 0, "last_sync_status": f"Deactivated on {datetime.utcnow().isoformat()}"}}
                        )
                        if result.modified_count > 0:
                            updated_count += 1
                            logging.info(f"Подписка для user_id {user_id} успешно деактивирована в локальной БД.")

                else:
                    reason = wfp_response.get('reason', 'Нет ответа') if wfp_response else 'Нет ответа'
                    logging.error(f"Не удалось получить корректный статус от WFP для user_id {user_id}. Причина: {reason}")
    
    except Exception as e:
        logging.error(f"Критическая ошибка в процессе синхронизации: {e}", exc_info=True)
    finally:
        if mongo_client:
            mongo_client.close()
        logging.info(f"--- Сессия синхронизации завершена. Найдено расхождений: {discrepancy_count}. Обновлено записей: {updated_count}. ---")


if __name__ == "__main__":
    # Эта конструкция позволяет запускать скрипт напрямую из командной строки
    asyncio.run(sync_statuses())