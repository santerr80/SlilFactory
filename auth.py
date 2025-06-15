import requests
import logging
import os
from getpass import getpass
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ==================================================================================================
# АУТЕНТИФИКАЦИЯ И УПРАВЛЕНИЕ СЕССИЕЙ
# ==================================================================================================

def login_to_skillfactory(username=None, password=None):
    """
    Выполняет вход в SkillFactory, обрабатывая CSRF, и возвращает аутентифицированную сессию.
    """
    if not username:
        username = input("Введите email от SkillFactory: ")
    if not password:
        password = getpass("Введите пароль: ")
        
    logger.info("Попытка входа в SkillFactory...")
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'ru-RU,ru;q=0.9,tr-RU;q=0.8,tr;q=0.7,en-US;q=0.6,en;q=0.5',
        'Origin': 'https://lms.skillfactory.ru',
        'Referer': 'https://lms.skillfactory.ru/',
        'sec-ch-ua': '"Google Chrome";v="137", "Chromium";v="137", "Not/A)Brand";v="24"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"'
    })

    # Шаг 1: Получение CSRF токена
    csrf_url = "https://lms.skillfactory.ru/csrf/api/v1/token"
    logger.debug(f"Получение CSRF токена с {csrf_url}")
    try:
        csrf_response = session.get(csrf_url, timeout=10)
        csrf_response.raise_for_status()
        csrf_token = session.cookies.get('csrftoken')
        if not csrf_token:
            logger.error("Не удалось получить CSRF токен из cookies.")
            return None
        logger.debug("CSRF токен успешно получен.")
        session.headers.update({'X-CSRFToken': csrf_token})
    except requests.RequestException as e:
        logger.error(f"Ошибка при получении CSRF токена: {e}")
        return None

    # Шаг 2: Вход с использованием токена
    login_url = "https://lms.skillfactory.ru/api/user/v1/account/login_session/"
    login_payload = {
        'email': username,
        'password': password,
        'remember': False
    }
    logger.debug(f"Отправка данных для входа на {login_url}")
    try:
        # Добавляем заголовки для формы
        session.headers.update({
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-Requested-With': 'XMLHttpRequest',
            'USE-JWT-COOKIE': 'true'
        })
        login_response = session.post(login_url, data=login_payload, timeout=15)
        logger.debug(f"Статус ответа: {login_response.status_code}")
        logger.debug(f"Заголовки ответа: {dict(login_response.headers)}")
        login_response.raise_for_status()

        if "sessionid" in session.cookies:
            logger.info("Вход выполнен успешно! Сессия активна.")
            # Обновляем заголовки для последующих запросов
            session.headers.update({
                'Accept': 'application/json, text/plain, */*',
                'Accept-Language': 'ru-RU,ru;q=0.9,tr-RU;q=0.8,tr;q=0.7,en-US;q=0.6,en;q=0.5',
                'Origin': 'https://apps.skillfactory.ru',
                'Referer': 'https://apps.skillfactory.ru/',
                'USE-JWT-COOKIE': 'true',
                'sec-ch-ua': '"Google Chrome";v="137", "Chromium";v="137", "Not/A)Brand";v="24"',
                'sec-ch-ua-mobile': '?0',
                'sec-ch-ua-platform': '"Windows"'
            })
            return session
        else:
            logger.error("Вход не удался: 'sessionid' отсутствует в cookies.")
            return None
    except requests.RequestException as e:
        logger.error(f"Критическая ошибка при попытке входа: {e}")
        if hasattr(e, 'response') and e.response is not None:
             logger.error(f"Ответ сервера: {e.response.text}")
        return None

def initialize_session_for_course(session, course_id):
    """
    Инициализирует сессию для указанного курса, переходя по страницам
    для получения необходимых cookies для домена apps.skillfactory.ru.
    """
    logger.info(f"Инициализация сессии для курса {course_id}...")

    # Шаг 1: Переход на страницу курса в apps.skillfactory.ru
    course_url = f"https://apps.skillfactory.ru/learning/course/{course_id}/home"
    logger.debug(f"Переход на страницу курса: {course_url}")
    
    # Обновляем заголовки для запроса
    session.headers.update({
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'Accept-Language': 'ru-RU,ru;q=0.9,tr-RU;q=0.8,tr;q=0.7,en-US;q=0.6,en;q=0.5',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
        'Sec-Ch-Ua': '"Google Chrome";v="137", "Chromium";v="137", "Not/A)Brand";v="24"',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Upgrade-Insecure-Requests': '1',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36'
    })

    try:
        response = session.get(course_url, allow_redirects=True, timeout=15)
        response.raise_for_status()
        final_url = response.url
        logger.debug(f"Успешный переход, итоговый URL: {final_url}")
        
        # Проверяем, что мы действительно на странице курса
        if course_id not in final_url:
            logger.error(f"Не удалось перейти на страницу курса. Текущий URL: {final_url}")
            return False

        logger.info(f"Сессия для домена 'apps' успешно инициализирована. Финальный URL: {final_url}")

        # Обновляем заголовки для последующих API-запросов
        session.headers.update({
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'ru-RU,ru;q=0.9,tr-RU;q=0.8,tr;q=0.7,en-US;q=0.6,en;q=0.5',
            'Origin': 'https://apps.skillfactory.ru',
            'Referer': final_url,
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-site',
            'USE-JWT-COOKIE': 'true'
        })
        
        return True
    except requests.RequestException as e:
        logger.error(f"Ошибка при инициализации сессии для курса: {e}")
        if hasattr(e, 'response') and e.response is not None:
             logger.error(f"Ответ сервера: {e.response.text}")
        return False 