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
    Получает список курсов пользователя через API enrollment.
    """
    logger.info("Получение списка доступных курсов из ЛК...")
    
    # Сначала пробуем API enrollment (возвращает курсы, на которые записан пользователь)
    enrollment_url = "https://lms.skillfactory.ru/api/enrollment/v1/enrollment"
    try:
        response = session.get(enrollment_url, timeout=20)
        response.raise_for_status()
        
        enrollments = response.json()
        logger.info(f"Получено {len(enrollments)} записей из API enrollment")
        
        all_courses = []
        for enrollment in enrollments:
            # Проверяем разные возможные структуры данных
            course_info = None
            course_id = None
            course_name = None
            
            # Вариант 1: информация в ключе 'course'
            if 'course' in enrollment and enrollment['course']:
                course_info = enrollment['course']
                course_id = course_info.get('id') or course_info.get('course_id')
                course_name = course_info.get('name') or course_info.get('display_name')
            
            # Вариант 2: информация на верхнем уровне
            if not course_id:
                course_id = enrollment.get('course_id')
            if not course_name:
                course_name = enrollment.get('course_name') or enrollment.get('display_name')
            
            # Вариант 3: информация в course_details
            if not course_id or not course_name:
                course_details = enrollment.get('course_details', {})
                if not course_id:
                    course_id = course_details.get('course_id') or course_details.get('id')
                if not course_name:
                    course_name = course_details.get('course_name') or course_details.get('display_name')
            
            # Проверяем, что курс активен (если есть такая информация)
            is_active = enrollment.get('is_active', True)  # По умолчанию считаем активным
            
            if course_id and course_name and is_active:
                all_courses.append({
                    'id': course_id,
                    'name': course_name,
                    'course_url_name': course_id  # Используем ID как заполнитель
                })
                logger.debug(f"Добавлен курс из enrollment: {course_name} (ID: {course_id})")
            else:
                logger.debug(f"Пропущена запись enrollment: course_id={course_id}, course_name={course_name}, is_active={is_active}")
        
        if all_courses:
            logger.info(f"Найдено {len(all_courses)} курсов через API enrollment.")
            return sorted(all_courses, key=lambda item: item['name'])
        else:
            logger.warning("API enrollment не вернул активные курсы, пробую альтернативный метод...")
        
    except requests.RequestException as e:
        logger.error(f"Ошибка при запросе API enrollment: {e}")
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка декодирования JSON из API enrollment: {e}")
    
    # Если API enrollment не сработал, пробуем courses API, но фильтруем только те, на которые записан пользователь
    courses_url = "https://lms.skillfactory.ru/api/courses/v1/courses/"
    try:
        logger.info("Пробую получить курсы через API courses, но буду фильтровать только доступные...")
        response = session.get(courses_url, timeout=20)
        response.raise_for_status()
        
        data = response.json()
        courses_list = data.get('results', [])
        logger.info(f"Получено {len(courses_list)} курсов из API courses")
        
        # Получаем список ID курсов из enrollment для фильтрации
        enrolled_course_ids = set()
        try:
            enrollment_response = session.get(enrollment_url, timeout=20)
            if enrollment_response.status_code == 200:
                enrollments = enrollment_response.json()
                for enrollment in enrollments:
                    # Извлекаем ID курса любым доступным способом
                    course_id = None
                    if 'course' in enrollment and enrollment['course']:
                        course_id = enrollment['course'].get('id') or enrollment['course'].get('course_id')
                    if not course_id:
                        course_id = enrollment.get('course_id')
                    if not course_id:
                        course_details = enrollment.get('course_details', {})
                        course_id = course_details.get('course_id') or course_details.get('id')
                    
                    if course_id:
                        enrolled_course_ids.add(course_id)
                        
                logger.info(f"Найдено {len(enrolled_course_ids)} курсов в enrollment для фильтрации: {enrolled_course_ids}")
        except:
            logger.warning("Не удалось получить список записанных курсов для фильтрации")
        
        all_courses = []
        for course in courses_list:
            course_id = course.get('course_id')
            course_name = course.get('name')
            
            # Если у нас есть список записанных курсов, фильтруем по нему
            if enrolled_course_ids and course_id not in enrolled_course_ids:
                logger.debug(f"Пропущен курс {course_name} - не найден в enrollment")
                continue
            
            if course_id and course_name:
                all_courses.append({
                    'id': course_id,
                    'name': course_name,
                    'course_url_name': course_id
                })
                logger.debug(f"Добавлен курс: {course_name} (ID: {course_id})")
        
        if all_courses:
            logger.info(f"Найдено {len(all_courses)} курсов через API courses (отфильтровано).")
            return sorted(all_courses, key=lambda item: item['name'])
        elif not enrolled_course_ids:
            # Если не удалось получить список для фильтрации, возвращаем все курсы
            logger.warning("Не удалось отфильтровать курсы, возвращаю все доступные")
            all_courses = []
            for course in courses_list:
                course_id = course.get('course_id')
                course_name = course.get('name')
                if course_id and course_name:
                    all_courses.append({
                        'id': course_id,
                        'name': course_name,
                        'course_url_name': course_id
                    })
            return sorted(all_courses, key=lambda item: item['name'])
        
    except requests.RequestException as e:
        logger.error(f"Ошибка при запросе API courses: {e}")
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка декодирования JSON из API courses: {e}")
    
    logger.error("Не удалось получить список курсов ни одним из способов.")
    return []
