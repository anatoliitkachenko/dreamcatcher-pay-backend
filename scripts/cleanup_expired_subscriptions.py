import os
import asyncio
import logging
from datetime import datetime
from pytz import timezone

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

# Настройка логирования для нашего скрипта
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Загрузка конфигурации ---
load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")

if not MONGO_URI:
    logging.error("Критическая ошибка: переменная MONGO_URI не найдена в .env.")
    exit()


async def cleanup_expired():
    """
    Основная функция для деактивации подписок, истекших по дате.
    """
    logging.info("--- Начало сессии очистки истекших подписок ---")
    
    mongo_client = None
    deactivated_count = 0

    try:
        # Подключаемся к MongoDB
        mongo_client = AsyncIOMotorClient(MONGO_URI)
        db = mongo_client["dream_database"]
        subscriptions_collection = db["subscriptions"]

        # Используем таймзону Киева, как в остальном проекте
        tz_kyiv = timezone('Europe/Kyiv') 
        today_str = datetime.now(tz_kyiv).strftime("%Y-%m-%d")

        # Формируем запрос к БД:
        # - Найти все документы, где подписка еще активна (is_active: 1)
        # - И где дата окончания (subscription_end) строго меньше (<), чем сегодняшняя дата.
        query = {
            "is_active": 1,
            "subscription_end": {"$lt": today_str}
        }
        
        logging.info(f"Поиск истекших подписок по запросу: {query}")

        # Используем update_many для эффективности: обновляем все найденные документы одним запросом
        result = await subscriptions_collection.update_many(
            query,
            {"$set": {"is_active": 0, "last_sync_status": f"Deactivated by cleanup script on {datetime.utcnow().isoformat()}"}}
        )
        
        deactivated_count = result.modified_count

        if deactivated_count > 0:
            logging.info(f"Успешно деактивировано {deactivated_count} истекших подписок.")
        else:
            logging.info("Не найдено истекших подписок для деактивации.")

    except Exception as e:
        logging.error(f"Критическая ошибка в процессе очистки: {e}", exc_info=True)
    finally:
        if mongo_client:
            mongo_client.close()
        logging.info(f"--- Сессия очистки завершена. Деактивировано записей: {deactivated_count}. ---")


if __name__ == "__main__":
    # Эта конструкция позволяет запускать скрипт напрямую из командной строки
    asyncio.run(cleanup_expired())