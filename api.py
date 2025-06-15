import logging
import re
import requests
from bs4 import BeautifulSoup
import json

from auth import initialize_session_for_course

logger = logging.getLogger(__name__)


def get_course_structure(session, course_url):
    """
    Извлекает ID курса, инициализирует сессию и запрашивает полную структуру курса по API.
    """
    course_id_match = re.search(r'(course-v1:[a-zA-Z0-9\._\+\-]+)', course_url)
    if not course_id_match:
        logger.error(f"Не удалось извлечь канонический ID курса (вида 'course-v1:...') из URL: {course_url}")
        return None
    course_id = course_id_match.group(1).rstrip('/')  # убираем только слэш в конце, точку оставляем
    logger.info(f"Извлечен и очищен ID курса: {course_id}")

    if not initialize_session_for_course(session, course_id):
        logger.error("Инициализация сессии провалена. Невозможно продолжить.")
        return None

    # Обновляем заголовки для API-запроса в соответствии с HAR
    session.headers.update({
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'ru-RU,ru;q=0.9,tr-RU;q=0.8,tr;q=0.7,en-US;q=0.6,en;q=0.5',
        'Origin': 'https://apps.skillfactory.ru',
        'Referer': 'https://apps.skillfactory.ru/',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-site',
        'USE-JWT-COOKIE': 'true',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
        'sec-ch-ua': '"Google Chrome";v="137", "Chromium";v="137", "Not/A)Brand";v="24"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"'
    })

    # Сначала пытаемся получить полную структуру, потом метаданные
    endpoints = [
        f"https://lms.skillfactory.ru/api/extended/outline/{course_id}",
        f"https://lms.skillfactory.ru/api/course_home/course_metadata/{course_id}"
    ]
    params = {'browser_timezone': 'Europe/Moscow'}
    
    # Добавляем специальные заголовки для API запросов
    api_headers = {
        'Accept': 'application/json, text/plain, */*',
        'Origin': 'https://apps.skillfactory.ru',
        'Referer': 'https://apps.skillfactory.ru/',
        'use-jwt-cookie': 'true',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-site'
    }

    for api_url in endpoints:
        logger.info(f"Пробую запрос структуры курса с {api_url}")
        try:
            response = session.get(api_url, params=params, headers=api_headers, timeout=30)
            logger.debug(f"Статус ответа: {response.status_code}")
            if response.status_code == 200:
                data = response.json()
                # Проверяем, что course_blocks не null
                if data.get('course_blocks') is not None:
                    logger.info(f"Структура курса успешно получена с {api_url}!")
                    return data
                else:
                    logger.warning(f"API {api_url} ответил успешно, но course_blocks равен null. Возможно, курс устарел или не поддерживается.")
                    logger.debug(f"Полный ответ: {response.text[:1000]}")
            elif response.status_code == 404:
                logger.warning(f"API {api_url} вернул 404 Not Found. Пробую следующий.")
                continue
            else:
                response.raise_for_status()
        except requests.RequestException as e:
            logger.error(f"Ошибка при запросе структуры курса с {api_url}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Ответ сервера: {e.response.text}")
    
    logger.error("Не удалось получить структуру курса ни по одному из известных эндпоинтов.")
    return None

def get_enrolled_courses_data(session):
    """
    Получает список курсов пользователя, парся данные из личного кабинета.
    """
    logger.info("Получение списка доступных курсов из ЛК...")
    lk_url = "https://student-lk.skillfactory.ru/"
    try:
        response = session.get(lk_url, timeout=20)
        response.raise_for_status()

        # Используем регулярное выражение для извлечения JSON данных о курсах
        match = re.search(r'\\"studentCourses\\":(\[.*?\])', response.text)
        
        if not match:
            logger.error("Не удалось найти информацию о курсах на странице личного кабинета.")
            return []

        # Очищаем и декодируем JSON
        courses_json_str = match.group(1).replace('\\"', '"')
        
        all_courses = []
        try:
            courses_data = json.loads(courses_json_str)
            for course in courses_data:
                # Извлекаем данные, как в оригинальном скрипте
                course_key = course.get('courseKey')
                course_name = course.get('courseName')
                # course_url_name нам не нужен напрямую, т.к. мы используем courseKey (ID)
                if course_key and course_name:
                    all_courses.append({
                        'id': course_key,
                        'name': course_name,
                        'course_url_name': course_key # Используем ID как заполнитель
                    })
        except json.JSONDecodeError as e:
            logger.error(f"Критическая ошибка декодирования JSON: {e}")
            return []
        
        if not all_courses:
            logger.error("Не найдено ни одного курса с корректными данными.")
            return []
        
        logger.info(f"Найдено {len(all_courses)} курсов.")
        return sorted(all_courses, key=lambda item: item['name'])

    except requests.RequestException as e:
        logger.error(f"Ошибка при загрузке страницы личного кабинета: {e}")
        return []