import argparse
import requests
import os
import re
import json
import logging
import time
import sys
import base64
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, unquote, quote
from pathvalidate import sanitize_filename
from tqdm import tqdm
from getpass import getpass
from urllib.parse import parse_qs
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.action_chains import ActionChains
import shutil
import subprocess
import xmltodict
# from yt_dlp import YoutubeDL # Больше не используется

# ==================================================================================================
# НАСТРОЙКА ЛОГИРОВАНИЯ
# ==================================================================================================
logger = logging.getLogger(__name__)

# ==================================================================================================
# ГЛОБАЛЬНЫЕ КОНСТАНТЫ И КОНФИГУРАЦИЯ
# ==================================================================================================
# Список ключевых слов в названиях блоков, которые нужно игнорировать при скачивании и в навигации.
# Регистр не учитывается.
IGNORE_KEYWORDS_IN_TITLES = [
    'силлабус', 'добро пожаловать', 'обратная связь', 'полезные материалы',
    'карта курса', 'вводный модуль', 'описание курса'
]

# ==================================================================================================
# АУТЕНТИФИКАЦИЯ И УПРАВЛЕНИЕ СЕССИЕЙ
# ==================================================================================================

def login_to_skillfactory(username, password):
    """
    Выполняет вход в SkillFactory, обрабатывая CSRF, и возвращает аутентифицированную сессию.
    """
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

# ==================================================================================================
# ЛОГИКА СКАЧИВАНИЯ
# ==================================================================================================

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
            else:
                logger.warning(f"{api_url} вернул статус {response.status_code}")
                logger.debug(f"Тело ответа: {response.text[:1000]}")
        except requests.RequestException as e:
            logger.error(f"Ошибка при получении структуры курса с {api_url}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Ответ сервера: {e.response.text}")
    logger.critical("Не удалось получить структуру курса ни по одному из эндпоинтов.")
    logger.critical("Возможные причины:")
    logger.critical("1. Курс устарел и больше не поддерживается API")
    logger.critical("2. У вас нет доступа к этому курсу")
    logger.critical("3. Курс использует старый формат, не совместимый с текущим API")
    logger.critical("Попробуйте выбрать другой курс из списка.")
    return None

def download_file(url, filepath, session):
    """
    Скачивает файл по URL с отображением прогресс-бара.
    """
    try:
        response = session.get(url, stream=True, timeout=30)
        response.raise_for_status()
        
        # Проверяем Content-Type, чтобы убедиться, что это действительно изображение или файл
        content_type = response.headers.get('content-type', '').lower()
        if content_type.startswith('text/html') or content_type.startswith('application/json'):
            logger.warning(f"Сервер вернул {content_type} вместо файла для URL: {url}")
            return False
            
        total_size = int(response.headers.get('content-length', 0))
        
        with open(filepath, 'wb') as f, tqdm(
            total=total_size, unit='iB', unit_scale=True, desc=os.path.basename(filepath)
        ) as bar:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
                bar.update(len(chunk))
        
        # Проверяем размер файла - если он слишком маленький, возможно это не изображение
        file_size = os.path.getsize(filepath)
        if file_size < 100:  # Меньше 100 байт - подозрительно для изображения
            logger.warning(f"Скачанный файл слишком мал ({file_size} байт): {filepath}")
            # Проверяем содержимое файла
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read(500)  # Читаем первые 500 символов
                if '<html' in content.lower() or '{' in content:
                    logger.warning(f"Файл содержит HTML/JSON вместо изображения: {filepath}")
                    os.remove(filepath)
                    return False
        
        logger.info(f"Файл успешно скачан: {filepath}")
        return True
    except requests.RequestException as e:
        logger.error(f"Ошибка при скачивании файла {url}: {e}")
        return False

def _embed_local_video(html_content, relative_video_path):
    """Заменяет iframe плеера на стандартный HTML5-тег <video>."""
    soup = BeautifulSoup(html_content, 'html.parser')
    iframe_tag = soup.find('iframe', src=re.compile(r'kinescope\.io/embed'))

    if iframe_tag:
        logger.info("Найден iframe видеоплеера Kinescope. Заменяю на локальный <video> тег...")
        # Создаем новый тег <video>
        video_tag = soup.new_tag(
            "video",
            controls=True,  # Добавляем стандартные элементы управления
            width="100%",   # Делаем плеер адаптивным по ширине
            preload="metadata" # Предзагружаем метаданные (длительность, размеры)
        )
        # Устанавливаем источник видео
        video_tag['src'] = relative_video_path.replace("\\", "/")
        
        # Заменяем iframe на наш новый тег
        iframe_tag.replace_with(video_tag)
    else:
        logger.warning("Не удалось найти iframe плеера для замены на локальное видео.")
        
    return str(soup)

def _download_kinescope_video_selenium(driver, session, output_path, temp_download_dir):
    """
    Скачивает видео Kinescope. Ожидает, что драйвер УЖЕ находится на странице урока
    и переключен в нужный iframe.
    Возвращает True в случае успеха, иначе False.
    """
    logger.info("Запускаю процедуру скачивания Kinescope видео...")
    
    # 1. Получаем список файлов в папке для загрузок ДО начала операции
    files_before = set(os.listdir(temp_download_dir))
    
    json_log_path = None
    try:
        # Драйвер уже в iframe, сразу начинаем действовать
        wait = WebDriverWait(driver, 30)

        # 3. Находим плеер и кликаем правой кнопкой
        # УЛУЧШЕНИЕ: Пробуем несколько селекторов, чтобы найти плеер, на случай если разметка изменится
        player_selectors = [
            (By.CSS_SELECTOR, "div[class*='k-player']"),       # Новый, более надежный селектор по классу
            (By.CSS_SELECTOR, "div[id^='player_']"),           # Старый селектор по ID
            (By.ID, "v_player"),                               # Возможный статический ID
            (By.CSS_SELECTOR, "div[role='application']"),      # Общий селектор для веб-приложений
            (By.TAG_NAME, "video")                             # Самый крайний случай - кликнуть на само видео
        ]
        
        player_element = None
        for i, selector in enumerate(player_selectors):
            try:
                # Для каждой попытки даем небольшое время
                short_wait = WebDriverWait(driver, 5)
                logger.debug(f"Попытка #{i+1} найти плеер с селектором: {selector}")
                player_element = short_wait.until(EC.presence_of_element_located(selector))
                
                logger.info(f"✔ Элемент плеера успешно найден с селектором: {selector}")
                break # Выходим из цикла, если элемент найден
            except TimeoutException:
                logger.warning(f"Не удалось найти плеер с селектором: {selector}. Пробую следующий...")

        if not player_element:
            logger.error("Не удалось найти элемент видеоплеера ни одним из известных способов. Невозможно скачать видео.")
            # Делаем скриншот для отладки
            try:
                screenshot_path = os.path.abspath("kinescope_player_not_found.png")
                driver.save_screenshot(screenshot_path)
                logger.error(f"Скриншот страницы сохранен в {screenshot_path} для отладки.")
            except Exception as e:
                logger.error(f"Не удалось сделать скриншот: {e}")
            return False
        
        logger.debug("Прокручиваю элемент плеера в видимую область...")
        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", player_element)
        time.sleep(1) # Небольшая пауза, чтобы избежать гонки состояний

        # Используем JavaScript для вызова контекстного меню, т.к. обычный клик может блокироваться
        logger.debug("Выполняю правый клик через JavaScript...")
        driver.execute_script(
            "arguments[0].dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, view: window }));",
            player_element
        )
        logger.debug("Событие contextmenu отправлено элементу плеера.")
        time.sleep(1) # Пауза, чтобы меню успело появиться

        # ИЗМЕНЕНИЕ: Ищем меню в самом верхнем документе (default_content), а не просто в родительском.
        # Это более надежный способ, так как кастомные меню часто добавляются в основной `<body>`.
        logger.debug("Возвращаюсь в самый верхний контекст для поиска меню...")
        driver.switch_to.default_content()

        # 4. Сначала ждем ВИДИМЫЙ контейнер меню, потом ищем в нем кнопку
        menu_selector = (By.CSS_SELECTOR, "div[role='menu']")
        menu_element = wait.until(EC.visibility_of_element_located(menu_selector))
        logger.debug("Контейнер контекстного меню найден.")
        
        save_log_selector = (By.XPATH, ".//*[normalize-space(text())='Сохранить системный журнал']")
        save_log_button = menu_element.find_element(*save_log_selector)
        
        wait.until(EC.element_to_be_clickable(save_log_button)).click()
        logger.info("✔ Нажата кнопка 'Сохранить системный журнал'. Ожидаю загрузку файла...")

        # 5. Ждем появления нового файла в папке загрузок
        for _ in range(30): # Ждем до 30 секунд
            files_after = set(os.listdir(temp_download_dir))
            new_files = files_after - files_before
            if new_files:
                json_log_filename = new_files.pop()
                # Убедимся, что файл полностью скачался
                time.sleep(1) 
                json_log_path = os.path.join(temp_download_dir, json_log_filename)
                logger.info(f"Обнаружен системный журнал: {json_log_filename}")
                break
            time.sleep(1)
        
        if not json_log_path or not os.path.exists(json_log_path):
            logger.error("Системный журнал Kinescope не был скачан. Невозможно продолжить.")
            return False

        # 6. Читаем JSON, извлекаем данные и запускаем KinescopeDownloader
        with open(json_log_path, 'r', encoding='utf-8') as f:
            log_data = json.load(f)

        video_id = log_data.get('state', {}).get('videoId')
        video_name = log_data.get('options', {}).get('playlist', [{}])[0].get('title', f"video_{video_id}")
        referrer = log_data.get('referrer')

        if not all([video_id, video_name, referrer]):
            logger.error("В JSON-журнале отсутствуют необходимые данные (videoId, title или referrer).")
            return False

        downloader = KinescopeDownloader(
            video_id=video_id,
            video_name=video_name,
            referer=referrer,
            session=session,
            output_dir=os.path.dirname(output_path) # Сохраняем в ту же папку
        )
        return downloader.download() # Возвращаем результат работы

    except Exception as e:
        logger.error(f"Произошла ошибка во время скачивания видео через Selenium: {e}", exc_info=True)
        
        # УЛУЧШЕНИЕ: Сохраняем HTML и скриншот для отладки, как было предложено.
        try:
            # Гарантированно выходим в самый верхний контекст
            driver.switch_to.default_content()
            
            debug_html_path = os.path.abspath("DEBUG_video_download_page.html")
            with open(debug_html_path, 'w', encoding='utf-8') as f:
                f.write(driver.page_source)
            logger.error(f"Для отладки сохранен HTML-код страницы в файл: {debug_html_path}")

            screenshot_path = os.path.abspath("DEBUG_video_download_screenshot.png")
            driver.save_screenshot(screenshot_path)
            logger.error(f"Для отладки сохранен скриншот страницы в файл: {screenshot_path}")
        except Exception as debug_e:
            logger.error(f"Не удалось сохранить отладочные файлы: {debug_e}")

        return False
    finally:
        # 8. Очистка (файл журнала)
        if json_log_path and os.path.exists(json_log_path):
            os.remove(json_log_path)
            logger.debug(f"Временный файл журнала {json_log_path} удален.")

def _remove_widgets(html_content):
    """
    Удаляет сторонние виджеты (например, чат) и теги <noscript> из HTML-кода.
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Удаляем виджет чата HelpDeskEddy по его контейнеру и по скрипту загрузчика
    for widget_part in soup.select('#hde-container, script#hde-chat-widget'):
        widget_part.decompose()
    logger.info("Сторонние виджеты удалены.")

    # Удаляем теги <noscript>, так как они бесполезны в оффлайн-версии
    for s in soup.select('noscript'):
        s.decompose()
    logger.info("Теги <noscript> удалены.")

    return str(soup)

def _clean_specific_trackers(html_content):
    """
    Очень осторожно удаляет только конкретные элементы трекинга, не трогая CSS и важные ресурсы.
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    removed_count = 0
    
    # Удаляем только конкретные проблемные iframe
    for iframe in soup.find_all('iframe'):
        src = iframe.get('src', '')
        if 'mc.yandex.ru/metrika' in src or 'skillfactory.helpdeskeddy.com' in src:
            logger.debug(f"Удаляю проблемный iframe: {src}")
            iframe.decompose()
            removed_count += 1
    
    # Удаляем скрипты только с явной аналитикой
    for script in soup.find_all('script'):
        src = script.get('src', '')
        if src and ('mc.yandex.ru' in src or 'metrika' in src):
            logger.debug(f"Удаляю скрипт Yandex Metrika: {src}")
            script.decompose()
            removed_count += 1
        elif script.string and any(keyword in script.string.lower() for keyword in ['ym(', 'yaCounter', 'metrika']):
            logger.debug("Удаляю inline-скрипт Yandex Metrika")
            script.decompose()
            removed_count += 1
    
    # Удаляем виджет чата
    for element in soup.select('#hde-container, script#hde-chat-widget'):
        logger.debug(f"Удаляю элемент чата: {element.name}")
        element.decompose()
        removed_count += 1
    
    logger.info(f"Осторожно удалено {removed_count} элементов трекинга (сохранены все CSS).")
    return str(soup)

def _clean_external_references(html_content):
    """
    Удаляет внешние ссылки, API-вызовы и аналитические скрипты, 
    сохраняя только MathJax для отображения математических формул.
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Домены, которые нужно блокировать (только аналитика)
    blocked_domains = [
        'mc.yandex.ru',  # Yandex Metrika
        'googletagmanager.com',  # Google Tag Manager
        'google-analytics.com',  # Google Analytics
        'facebook.com',  # Facebook Pixel
        'vk.com'  # VK Pixel
    ]
    
    # Разрешенные домены для математических формул
    allowed_domains = [
        'cdnjs.cloudflare.com',  # MathJax CDN
        'cdn.mathjax.org'  # Старый MathJax CDN
    ]
    
    removed_count = 0
    
    # Удаляем скрипты с внешними ссылками
    for script in soup.find_all('script'):
        src = script.get('src')
        should_remove = False
        
        if src:
            # Проверяем, не является ли это разрешенным доменом
            if any(domain in src for domain in allowed_domains):
                logger.debug(f"Сохраняю разрешенный скрипт: {src}")
                continue
                
            # Удаляем скрипты с заблокированных доменов
            if any(domain in src for domain in blocked_domains):
                should_remove = True
                logger.debug(f"Удаляю скрипт с заблокированного домена: {src}")
            
            # Удаляем только некоторые внешние скрипты (очень осторожно)
            elif src.startswith(('http://', 'https://')) and not any(domain in src for domain in allowed_domains):
                # Блокируем только очевидную аналитику
                if any(word in src.lower() for word in ['analytics', 'gtag', 'yandex', 'facebook', 'vk.com']):
                    should_remove = True
                    logger.debug(f"Удаляю аналитический скрипт: {src}")
        
        # Проверяем содержимое inline-скриптов
        elif script.string:
            script_content = script.string.lower()
            # Удаляем аналитические и API скрипты
            if any(keyword in script_content for keyword in [
                'yandex.ru', 'google-analytics', 'gtag', 'dataLayer',
                'mc.yandex.ru', 'ym(', 'facebook.com', 'vk.com'
            ]):
                should_remove = True
                logger.debug("Удаляю аналитический/API inline-скрипт")
        
        if should_remove:
            script.decompose()
            removed_count += 1
    
    # Удаляем мета-теги для внешних сервисов, но сохраняем важные для отображения
    for meta in soup.find_all('meta'):
        name = meta.get('name', '').lower()
        property_attr = meta.get('property', '').lower()
        
        # Сохраняем важные мета-теги для корректного отображения
        if any(keyword in name for keyword in ['viewport', 'charset', 'http-equiv']):
            continue
            
        if any(keyword in name or keyword in property_attr for keyword in [
            'yandex', 'google', 'facebook', 'vk', 'twitter'
        ]):
            meta.decompose()
            removed_count += 1
    
    # НЕ удаляем никакие ссылки - оставляем все как есть для CSS
    # Удаляем только явную аналитику в отдельном блоке ниже
    
    # Удаляем формы, отправляющие данные на сервер
    for form in soup.find_all('form'):
        action = form.get('action', '')
        if any(domain in action for domain in blocked_domains) or 'login' in action.lower():
            form.decompose()
            removed_count += 1
    
    # Удаляем iframe с внешними источниками (кроме разрешенных)
    for iframe in soup.find_all('iframe'):
        src = iframe.get('src', '')
        if src and src.startswith(('http://', 'https://')):
            if not any(domain in src for domain in allowed_domains):
                iframe.decompose()
                removed_count += 1
    
    # Очищаем обработчики событий JavaScript
    for element in soup.find_all(attrs={'onclick': True}):
        del element['onclick']
        removed_count += 1
    
    for element in soup.find_all(attrs={'onload': True}):
        del element['onload']
        removed_count += 1
    
    # Удаляем скрытые элементы для отслеживания
    for element in soup.find_all(attrs={'style': lambda x: x and 'display:none' in x.replace(' ', '')}):
        if element.name in ['img', 'div', 'span'] and not element.get_text().strip():
            element.decompose()
            removed_count += 1
    
    logger.info(f"Удалено {removed_count} внешних ссылок и аналитических элементов.")
    logger.info("HTML очищен от внешних обращений, сохранен только MathJax.")
    
    # Дополнительная статистика
    remaining_scripts = len(soup.find_all('script'))
    remaining_links = len(soup.find_all('link'))
    stylesheets = len(soup.find_all('link', rel='stylesheet'))
    logger.info(f"Осталось в документе: {remaining_scripts} скриптов, {remaining_links} ссылок, {stylesheets} CSS файлов")
    
    # Логируем оставшиеся CSS файлы для отладки
    for link in soup.find_all('link', rel='stylesheet'):
        href = link.get('href', '')
        if href:
            logger.debug(f"Сохранен CSS: {href}")
    
    return str(soup)

def _download_fonts_from_css(css_content, css_base_url, font_dest_dir, css_location_path, session):
    """
    Парсит CSS-контент, находит все url() со шрифтами, скачивает их и заменяет пути.
    Также удаляет внешние ссылки из CSS.
    `css_location_path` - это конечный путь CSS файла или HTML файла (для тегов style).
    """
    # Создаем папку для шрифтов, если ее нет
    os.makedirs(font_dest_dir, exist_ok=True)
    
    # Домены, которые нужно блокировать в CSS (только аналитика и API)
    blocked_css_domains = [
        'mc.yandex.ru',
        'googletagmanager.com',
        'google-analytics.com'
    ]
    
    # API домены - блокируем только API пути
    api_css_domains = [
        'lms.skillfactory.ru/api',
        'apps.skillfactory.ru/api'
    ]
    
    # Используем функцию-заменитель с re.sub для корректной замены с учетом контекста
    def font_replacer(match):
        original_url_part = match.group(1)
        # Убираем кавычки и лишние пробелы из захваченной группы
        font_url = original_url_part.strip('\'" ')
        
        if font_url.startswith('data:'):
            return match.group(0) # Возвращаем исходное совпадение (например, url(data:...))

        # Блокируем внешние ссылки на запрещенные домены
        if any(domain in font_url for domain in blocked_css_domains):
            logger.debug(f"Блокирую внешний ресурс в CSS: {font_url}")
            return f"url('')" # Заменяем на пустую ссылку

        try:
            absolute_font_url = urljoin(css_base_url, font_url)
            
            # Блокируем внешние ссылки на запрещенные домены (после создания абсолютного URL)
            if any(domain in absolute_font_url for domain in blocked_css_domains):
                logger.debug(f"Блокирую внешний ресурс в CSS: {absolute_font_url}")
                return f"url('')"
            
            # Блокируем API пути
            if any(domain in absolute_font_url for domain in api_css_domains):
                logger.debug(f"Блокирую API ресурс в CSS: {absolute_font_url}")
                return f"url('')"
            
            font_filename_raw = os.path.basename(urlparse(absolute_font_url).path)
            
            # Пропускаем, если это не шрифт или изображение
            is_font = font_filename_raw.lower().endswith(('.woff', '.woff2', '.ttf', '.eot', '.otf'))
            is_image = font_filename_raw.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp'))
            is_css_resource = font_filename_raw.lower().endswith('.css')
            
            # Для шрифтов - скачиваем
            if is_font:
                logger.debug(f"Найден шрифт для скачивания: {absolute_font_url}")
            # Для изображений и CSS - пропускаем без блокировки (оставляем оригинальный URL)
            elif is_image or is_css_resource:
                logger.debug(f"Пропускаю ресурс (изображение/CSS): {absolute_font_url}")
                return match.group(0)
            # Для всех остальных внешних ресурсов - блокируем только если это явно внешний домен
            else:
                if absolute_font_url.startswith(('http://', 'https://')) and not any(domain in absolute_font_url for domain in ['lms.skillfactory.ru', 'lms-cdn.skillfactory.ru']):
                    logger.debug(f"Блокирую внешний неизвестный ресурс: {absolute_font_url}")
                    return f"url('')"
                logger.debug(f"Оставляю локальный/SF ресурс как есть: {absolute_font_url}")
                return match.group(0)
            
            # Этот код выполняется только для шрифтов
            if not is_font:
                return match.group(0)

            font_filename = sanitize_filename(unquote(font_filename_raw))
            if not font_filename: # Если имя файла пустое после всех обработок
                return match.group(0)

            local_font_path = os.path.join(font_dest_dir, font_filename)

            if not os.path.exists(local_font_path):
                logger.info(f"Скачиваю новый шрифт: {absolute_font_url}")
                if not download_file(absolute_font_url, local_font_path, session):
                    logger.warning(f"Не удалось скачать шрифт: {absolute_font_url}")
                    return f"url('')" # Заменяем на пустую ссылку при ошибке

            # Рассчитываем относительный путь от местоположения CSS до шрифта
            relative_font_path = os.path.relpath(local_font_path, os.path.dirname(css_location_path))
            # Для web всегда используем forward slashes
            relative_font_path = relative_font_path.replace("\\", "/")
            
            # Возвращаем полностью собранный url() с новым путем
            return f"url('{relative_font_path}')"
        except Exception as e:
            logger.error(f"Ошибка при обработке шрифта {font_url}: {e}")
            return f"url('')" # В случае ошибки, заменяем на пустую ссылку

    # Сначала подсчитаем количество url() для статистики
    url_matches = re.findall(r'url\(([^)]+)\)', css_content)
    logger.debug(f"В CSS найдено {len(url_matches)} ресурсов url() для обработки")
    
    # Regex для поиска url() и захвата его содержимого
    processed_css = re.sub(r'url\(([^)]+)\)', font_replacer, css_content)
    
    # Дополнительная очистка CSS от внешних ссылок и трекинга
    # Удаляем @import с внешних доменов
    all_blocked_domains = blocked_css_domains + [domain.split('/')[0] for domain in api_css_domains]
    processed_css = re.sub(r'@import\s+[^;]*(?:' + '|'.join(re.escape(domain) for domain in all_blocked_domains) + ')[^;]*;', '', processed_css, flags=re.IGNORECASE)
    
    return processed_css

def _get_full_css_content(css_url, session, processed_urls):
    """
    Рекурсивно скачивает CSS и все его @import зависимости, возвращая единый файл.
    """
    if css_url in processed_urls:
        return "" # Избегаем циклов и дубликатов
    processed_urls.add(css_url)

    try:
        logger.debug(f"Загрузка CSS и его зависимостей с {css_url}")
        response = session.get(css_url, timeout=15)
        response.raise_for_status()
        content = response.text
    except Exception as e:
        logger.warning(f"Не удалось скачать CSS {css_url}: {e}")
        return ""

    # Находим все @import и заменяем их рекурсивным вызовом
    def replace_import(match):
        import_statement = match.group(0)
        url_match = re.search(r'url\((["\']?)(.*?)\1\)|(["\'])(.*?)\3', import_statement)
        if not url_match:
            return "" # Не удалось распарсить, просто удаляем
        
        path = (url_match.group(2) or url_match.group(4)).strip()
        absolute_url = urljoin(css_url, path)
        
        # Рекурсивный вызов для получения содержимого вложенного CSS
        return _get_full_css_content(absolute_url, session, processed_urls)

    # Используем re.sub с функцией для замены всех @import на их содержимое
    # \s* для учета пробелов, (?i) для регистронезависимости
    content = re.sub(r'(?i)@import[^;]+;', replace_import, content)
    
    return content

def download_css_and_update_html(base_url, html_content, lesson_file_path, root_css_dir, session):
    """
    Парсит HTML, скачивает все CSS (из <link> и <style>) и их зависимости, затем шрифты, и обновляет пути.
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    os.makedirs(root_css_dir, exist_ok=True)
    root_font_dir = os.path.join(os.path.dirname(root_css_dir), 'fonts')
    
    processed_css_urls = set()
    
    # Добавляем стиль для предотвращения мигания неформатированного контента (FOUC)
    preload_style = soup.new_tag('style')
    preload_style.string = """
    /* Предотвращение FOUC - мигания неформатированного контента */
    body { visibility: hidden; opacity: 0; transition: opacity 0.3s ease-in; }
    body.loaded { visibility: visible; opacity: 1; }
    /* Добавляем индикатор загрузки */
    body:before {
        content: "Загрузка...";
        position: fixed;
        top: 50%;
        left: 50%;
        transform: translate(-50%, -50%);
        font-size: 18px;
        z-index: 9999;
        background: rgba(255,255,255,0.9);
        padding: 20px;
        border-radius: 5px;
        box-shadow: 0 2px 10px rgba(0,0,0,0.1);
    }
    body.loaded:before { display: none; }
    """
    if soup.head:
        soup.head.insert(0, preload_style)
    


    css_links = soup.find_all('link', rel='stylesheet')
    logger.info(f"Найдено {len(css_links)} CSS файлов для обработки")
    
    for i, link in enumerate(css_links, 1):
        href = link.get('href')
        logger.debug(f"Обрабатываю CSS #{i}: {href}")
        
        if not href or href.startswith('data:'):
            logger.debug(f"CSS #{i}: пропускаю (data: или пустой)")
            continue

        try:
            css_url = urljoin(base_url, href)
            logger.debug(f"CSS #{i}: полный URL = {css_url}")
            
            # Создаем уникальное имя файла, чтобы избежать коллизий
            css_filename_parts = os.path.splitext(os.path.basename(urlparse(css_url).path))
            css_filename_base = css_filename_parts[0] or "style"
            css_filename_ext = css_filename_parts[1] if len(css_filename_parts) > 1 else ".css"
            # Используем хэш от полного URL для уникальности
            css_filename = f"{sanitize_filename(css_filename_base)}_{abs(hash(css_url))}{css_filename_ext}"

            local_css_path = os.path.join(root_css_dir, css_filename)
            logger.debug(f"CSS #{i}: локальный путь = {local_css_path}")

            if css_url not in processed_css_urls:
                logger.info(f"CSS #{i}: Начинаю полную обработку: {css_url}")
                
                full_css_content = _get_full_css_content(css_url, session, processed_css_urls)
                if not full_css_content:
                    logger.warning(f"CSS #{i}: Не удалось получить содержимое CSS")
                    continue
                
                processed_css_with_fonts = _download_fonts_from_css(full_css_content, css_url, root_font_dir, local_css_path, session)

                with open(local_css_path, 'w', encoding='utf-8') as f:
                    f.write(processed_css_with_fonts)
                logger.info(f"CSS #{i}: ✔ Файл сохранен локально")
            else:
                logger.debug(f"CSS #{i}: уже обработан ранее")
            
            relative_path = os.path.relpath(local_css_path, os.path.dirname(lesson_file_path))
            old_href = link['href']
            link['href'] = relative_path.replace("\\", "/")
            logger.debug(f"CSS #{i}: путь обновлен с '{old_href}' на '{link['href']}'")
            
        except Exception as e:
            logger.error(f"CSS #{i}: Критическая ошибка при обработке {href}: {e}", exc_info=True)
    
    logger.info("Обрабатываю встроенные стили в тегах <style>...")
    for style_tag in soup.find_all('style'):
        if not style_tag.string:
            continue
        
        try:
            logger.debug("Найден тег <style> с контентом, обрабатываю шрифты...")
            original_css = style_tag.string
            processed_css = _download_fonts_from_css(original_css, base_url, root_font_dir, lesson_file_path, session)
            style_tag.string.replace_with(processed_css)
            logger.debug("✔ Шрифты в <style> теге обработаны.")
        except Exception as e:
            logger.error(f"Ошибка при обработке <style> тега: {e}", exc_info=True)
            
    return str(soup)

def download_js_and_update_html(base_url, html_content, lesson_file_path, root_js_dir, session):
    """
    Парсит HTML, скачивает только необходимые .js файлы и обновляет их пути на локальные.
    Если скрипт не удается скачать, тег удаляется, чтобы избежать ошибок в браузере.
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    os.makedirs(root_js_dir, exist_ok=True)
    
    # Домены скриптов, которые НЕ нужно скачивать
    blocked_script_domains = [
        'mc.yandex.ru',
        'googletagmanager.com',
        'google-analytics.com'
    ]
    
    # Домены API SkillFactory (блокируем только API скрипты)
    api_script_patterns = [
        '/api/',
        'login_refresh',
        'csrf',
        'user/v1/'
    ]
    
    # Разрешенные домены (только для MathJax)
    allowed_external_domains = [
        'cdnjs.cloudflare.com',
        'cdn.mathjax.org'
    ]
    
    script_tags = soup.find_all('script', src=True)
    logger.info(f"Найдено {len(script_tags)} внешних скриптов для анализа.")

    scripts_to_download = []
    for script in script_tags:
        src = script.get('src')
        if not src or src.startswith('data:'):
            continue

        # --- ИСПРАВЛЕНИЕ MATHJAX: ПРИНУДИТЕЛЬНЫЙ РЕНДЕРИНГ В SVG ---
        if 'MathJax.js' in src:
            # Заменяем конфиг на тот, который использует SVG. Это решает все проблемы с рендерингом и выравниванием.
            cdn_url = 'https://cdnjs.cloudflare.com/ajax/libs/mathjax/2.7.5/MathJax.js?config=TeX-AMS-MML_SVG'
            logger.info(f"Обнаружен MathJax. Заменяю на CDN-версию с SVG-рендерингом: {cdn_url}")
            script['src'] = cdn_url
            continue # Переходим к следующему скрипту, не скачивая этот
        # --- КОНЕЦ ИСПРАВЛЕНИЯ ---

        js_url = urljoin(base_url, src)
        
        # Пропускаем скрипты с заблокированных доменов
        if any(domain in js_url for domain in blocked_script_domains):
            logger.debug(f"Пропускаю заблокированный скрипт: {js_url}")
            script.decompose()
            continue
        
        # Пропускаем API скрипты SkillFactory
        if any(pattern in js_url for pattern in api_script_patterns):
            logger.debug(f"Пропускаю API скрипт: {js_url}")
            script.decompose()
            continue
        
        # Пропускаем внешние скрипты, кроме разрешенных
        if js_url.startswith(('http://', 'https://')):
            if not any(domain in js_url for domain in allowed_external_domains):
                # Но разрешаем статические ресурсы SkillFactory
                if not any(domain in js_url for domain in ['lms-cdn.skillfactory.ru']) or any(pattern in js_url for pattern in api_script_patterns):
                    logger.debug(f"Пропускаю внешний скрипт: {js_url}")
                    script.decompose()
                    continue
        
        # Добавляем в список для скачивания
        scripts_to_download.append((script, js_url))

    logger.info(f"К скачиванию отобрано {len(scripts_to_download)} скриптов.")

    for script, js_url in scripts_to_download:
        try:
            # Создаем уникальное имя файла, как для CSS, чтобы избежать коллизий
            js_filename_parts = os.path.splitext(os.path.basename(urlparse(js_url).path))
            js_filename_base = js_filename_parts[0]
            js_filename_ext = ".js" # Гарантируем расширение
            js_filename = f"{sanitize_filename(js_filename_base)}_{abs(hash(js_url))}{js_filename_ext}"
            
            local_js_path = os.path.join(root_js_dir, js_filename)
            
            # Скачиваем, только если файла еще нет
            if not os.path.exists(local_js_path):
                logger.info(f"Скачиваю скрипт: {js_url}")
                if not download_file(js_url, local_js_path, session):
                    logger.warning(f"Не удалось скачать скрипт: {js_url}, тег будет удален.")
                    script.decompose() # Удаляем тег, если скачивание не удалось
                    continue # Переходим к следующему скрипту
            else:
                logger.info(f"Скрипт уже скачан: {os.path.basename(local_js_path)}")

            # Обновляем src на относительный локальный путь
            relative_path = os.path.relpath(local_js_path, os.path.dirname(lesson_file_path))
            script['src'] = relative_path.replace("\\", "/")

        except Exception as e:
            logger.error(f"Критическая ошибка при обработке скрипта {js_url}: {e}", exc_info=True)
            script.decompose() # Удаляем тег в случае любой другой ошибки
            
    return str(soup)

def download_images_and_update_html(base_url, html_content, lesson_path, session):
    """
    Парсит HTML, скачивает все изображения и обновляет их пути на локальные.
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    images_dir = os.path.join(lesson_path, "images")
    os.makedirs(images_dir, exist_ok=True)
    
    img_tags = soup.find_all('img')
    logger.info(f"Найдено {len(img_tags)} изображений для скачивания.")

    for img in img_tags:
        src = img.get('src')
        if not src:
            continue

        # Обрабатываем data URLs (встроенные изображения в base64)
        if src.startswith('data:'):
            try:
                # Парсим data URL
                match = re.match(r'data:([^;]+);base64,(.+)', src)
                if match:
                    mime_type, base64_data = match.groups()
                    # Определяем расширение файла по MIME типу
                    extension = {
                        'image/png': '.png',
                        'image/jpeg': '.jpg',
                        'image/jpg': '.jpg',
                        'image/gif': '.gif',
                        'image/webp': '.webp',
                        'image/svg+xml': '.svg'
                    }.get(mime_type, '.png')
                    
                    # Генерируем имя файла
                    img_filename = f"embedded_image_{abs(hash(src))}{extension}"
                    local_img_path = os.path.join(images_dir, img_filename)
                    
                    # Декодируем и сохраняем
                    image_data = base64.b64decode(base64_data)
                    with open(local_img_path, 'wb') as f:
                        f.write(image_data)
                    
                    # Обновляем src
                    new_src = os.path.join("images", img_filename).replace("\\", "/")
                    img['src'] = new_src
                    logger.info(f"Встроенное изображение сохранено: {img_filename}")
                    
            except Exception as e:
                logger.error(f"Ошибка при обработке встроенного изображения: {e}")
            continue

        try:
            img_url = ''
            # Явным образом обрабатываем протокол-относительные URL (начинаются с //)
            if src.startswith('//'):
                img_url = 'https:' + src
            # Обрабатываем остальные случаи: абсолютные и относительные URL
            else:
                img_url = urljoin(base_url, src)

            # Извлекаем имя файла из URL
            img_filename_from_url = os.path.basename(urlparse(img_url).path)
            
            # Декодируем для получения читаемого имени
            img_filename_decoded = unquote(img_filename_from_url)
            img_filename_clean = sanitize_filename(img_filename_decoded)
            
            # Если имя файла пустое или содержит только недопустимые символы, генерируем имя на основе хэша URL
            if not img_filename_clean or img_filename_clean.strip() == '':
                img_filename_clean = f"image_{abs(hash(img_url))}.png"
            
            # Для сохранения используем декодированное имя
            local_img_path = os.path.join(images_dir, img_filename_clean)
            
            # Для HTML будем использовать правильно закодированное имя
            display_filename = img_filename_clean
            
            # Специальная обработка для ассетов SkillFactory
            if 'asset-v1:' in img_url:
                # Для ассетов SkillFactory используем прямой URL к CDN
                asset_id_match = re.search(r'asset-v1:([^/]+)\+([^/]+)\+([^/]+)\+type@asset\+block@([^/&]+)', img_url)
                if asset_id_match:
                    org, course, run, block_id = asset_id_match.groups()
                    
                    # Декодируем block_id для читаемого имени файла
                    decoded_block_id = unquote(block_id)
                    
                    # Используем декодированное имя с расширением
                    if not decoded_block_id.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg')):
                        img_filename_clean = f"{sanitize_filename(decoded_block_id)}.png"
                    else:
                        img_filename_clean = sanitize_filename(decoded_block_id)
                        
                    display_filename = img_filename_clean
                    local_img_path = os.path.join(images_dir, img_filename_clean)
                    
                    # Пробуем несколько вариантов URL для ассетов
                    cdn_urls_to_try = [
                        f"https://lms-cdn.skillfactory.ru/asset-v1:{org}+{course}+{run}+type@asset+block@{block_id}",
                        f"https://lms.skillfactory.ru/asset-v1:{org}+{course}+{run}+type@asset+block@{block_id}",
                        f"https://lms-cdn.skillfactory.ru/static/images/{block_id}",
                        # Возвращаемся к оригинальному URL как последний вариант
                        img_url
                    ]
                    
                    logger.info(f"Обнаружен ассет SkillFactory: {decoded_block_id}")
                    
                    # Пробуем каждый URL до первого успешного
                    for cdn_url in cdn_urls_to_try:
                        try:
                            test_response = session.head(cdn_url, timeout=10)
                            if test_response.status_code == 200:
                                content_type = test_response.headers.get('content-type', '').lower()
                                if content_type.startswith('image/') or 'image' in content_type:
                                    logger.info(f"Найден рабочий URL для ассета: {cdn_url}")
                                    img_url = cdn_url
                                    break
                        except:
                            continue
                    else:
                        logger.warning(f"Не найден рабочий URL для ассета {decoded_block_id}, используется исходный URL")
            
            # Скачиваем файл
            logger.info(f"Скачиваю изображение: {img_url}")
            if download_file(img_url, local_img_path, session):
                # Обновляем src на относительный локальный путь.
                # Кодируем имя файла для URL, чтобы браузер правильно его обработал
                encoded_filename = quote(display_filename, safe='')
                new_src = os.path.join("images", encoded_filename).replace("\\", "/")
                img['src'] = new_src
                logger.debug(f"Обновлен src изображения: {display_filename} -> {new_src}")
            else:
                logger.warning(f"Не удалось скачать изображение: {img_url}, src останется без изменений.")
        except Exception as e:
            logger.error(f"Произошла ошибка при обработке изображения {src}: {e}")
            
    # Добавляем скрипт для показа страницы после полной загрузки
    load_script = soup.new_tag('script')
    load_script.string = """
    // Показываем страницу после полной загрузки всех ресурсов
    function showPageWhenReady() {
        var cssLoaded = true;
        var stylesheets = document.querySelectorAll('link[rel="stylesheet"]');
        
        // Проверяем загрузку CSS
        for (var i = 0; i < stylesheets.length; i++) {
            try {
                var sheet = stylesheets[i].sheet;
                if (!sheet || !sheet.cssRules) {
                    cssLoaded = false;
                    break;
                }
            } catch (e) {
                // Если нет доступа к cssRules, считаем что еще не загружен
                cssLoaded = false;
                break;
            }
        }
        
        // Ждем загрузки всех изображений
        var images = document.images;
        var imageCount = images.length;
        var loadedCount = 0;
        
        for (var i = 0; i < imageCount; i++) {
            if (images[i].complete) {
                loadedCount++;
            }
        }
        
        // Если CSS и изображения готовы, показываем страницу
        if (cssLoaded && loadedCount >= imageCount) {
            document.body.classList.add('loaded');
            return true;
        }
        
        return false;
    }
    
    // Проверяем готовность при загрузке DOM
    document.addEventListener('DOMContentLoaded', function() {
        if (!showPageWhenReady()) {
            // Если не готово, проверяем каждые 100мс
            var checkInterval = setInterval(function() {
                if (showPageWhenReady()) {
                    clearInterval(checkInterval);
                }
            }, 100);
            
            // Принудительно показываем через 5 секунд в любом случае
            setTimeout(function() {
                clearInterval(checkInterval);
                document.body.classList.add('loaded');
            }, 5000);
        }
    });
    
    // Дополнительная проверка при загрузке окна
    window.addEventListener('load', function() {
        setTimeout(function() {
            document.body.classList.add('loaded');
        }, 100);
    });
    """
    if soup.body:
        soup.body.append(load_script)
    
    return str(soup)

def download_documents_and_update_html(base_url, html_content, lesson_path, session):
    """
    Парсит HTML, находит ссылки на документы (PDF, DOC и т.д.), скачивает их и обновляет пути на локальные.
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    docs_dir = os.path.join(lesson_path, "documents")
    os.makedirs(docs_dir, exist_ok=True)
    
    # Найдем все ссылки, которые могут быть документами
    all_links = soup.find_all('a', href=True)
    document_extensions = ['.pdf', '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx', '.zip', '.rar', '.txt']
    
    logger.info(f"Найдено {len(all_links)} ссылок для проверки на документы.")
    
    processed_docs = 0
    for link in all_links:
        href = link.get('href')
        if not href:
            continue
            
        try:
            # Проверяем, является ли ссылка документом
            is_document = False
            
            # Проверка по расширению в URL
            parsed_url = urlparse(href)
            if any(parsed_url.path.lower().endswith(ext) for ext in document_extensions):
                is_document = True
            
            # Проверка asset-v1 ссылок (они могут быть документами)
            if 'asset-v1:' in href:
                is_document = True
                
            if not is_document:
                continue
                
            processed_docs += 1
            logger.info(f"Обрабатываю документ #{processed_docs}: {href}")
            
            # Создаем полный URL
            if href.startswith('//'):
                doc_url = 'https:' + href
            else:
                doc_url = urljoin(base_url, href)
            
            # Определяем имя файла
            doc_filename = None
            
            # Специальная обработка для ассетов SkillFactory
            if 'asset-v1:' in doc_url:
                asset_id_match = re.search(r'asset-v1:([^/]+)\+([^/]+)\+([^/]+)\+type@asset\+block@([^/&]+)', doc_url)
                if asset_id_match:
                    org, course, run, block_id = asset_id_match.groups()
                    
                    # Декодируем block_id для читаемого имени файла
                    decoded_block_id = unquote(block_id)
                    doc_filename = sanitize_filename(decoded_block_id)
                    
                    # Если нет расширения, пытаемся определить его
                    if not any(doc_filename.lower().endswith(ext) for ext in document_extensions):
                        # Попробуем определить тип файла по запросу HEAD
                        try:
                            head_response = session.head(doc_url, timeout=10)
                            content_type = head_response.headers.get('content-type', '').lower()
                            if 'pdf' in content_type:
                                doc_filename += '.pdf'
                            elif 'word' in content_type or 'document' in content_type:
                                doc_filename += '.docx'
                            elif 'powerpoint' in content_type or 'presentation' in content_type:
                                doc_filename += '.pptx'
                            elif 'excel' in content_type or 'spreadsheet' in content_type:
                                doc_filename += '.xlsx'
                            elif 'zip' in content_type:
                                doc_filename += '.zip'
                            else:
                                doc_filename += '.pdf'  # По умолчанию PDF
                        except:
                            doc_filename += '.pdf'  # По умолчанию PDF
                    
                    # Пробуем разные URL для скачивания
                    cdn_urls_to_try = [
                        f"https://lms-cdn.skillfactory.ru/asset-v1:{org}+{course}+{run}+type@asset+block@{block_id}",
                        f"https://lms.skillfactory.ru/asset-v1:{org}+{course}+{run}+type@asset+block@{block_id}",
                        doc_url  # Оригинальный URL как запасной вариант
                    ]
                    
                    logger.info(f"Найден asset документ: {decoded_block_id}")
                    
                    # Ищем рабочий URL
                    for cdn_url in cdn_urls_to_try:
                        try:
                            test_response = session.head(cdn_url, timeout=10)
                            if test_response.status_code == 200:
                                logger.info(f"Найден рабочий URL для документа: {cdn_url}")
                                doc_url = cdn_url
                                break
                        except:
                            continue
                    else:
                        logger.warning(f"Не найден рабочий URL для документа {decoded_block_id}")
            else:
                # Обычная ссылка - извлекаем имя файла из URL
                filename_from_url = os.path.basename(parsed_url.path)
                doc_filename = sanitize_filename(unquote(filename_from_url))
                
                if not doc_filename:
                    doc_filename = f"document_{abs(hash(doc_url))}.pdf"
            
            # Создаем локальный путь
            local_doc_path = os.path.join(docs_dir, doc_filename)
            
            # Скачиваем документ
            logger.info(f"Скачиваю документ: {doc_url}")
            if download_file(doc_url, local_doc_path, session):
                # Обновляем href на относительный локальный путь
                encoded_filename = quote(doc_filename, safe='')
                new_href = os.path.join("documents", encoded_filename).replace("\\", "/")
                old_href = link['href']
                link['href'] = new_href
                
                # Добавляем атрибут download для принудительного скачивания
                link['download'] = doc_filename
                
                logger.info(f"✔ Документ скачан и обновлена ссылка: {old_href} -> {new_href}")
            else:
                logger.warning(f"Не удалось скачать документ: {doc_url}, ссылка останется без изменений.")
                
        except Exception as e:
            logger.error(f"Ошибка при обработке документа {href}: {e}")
    
    logger.info(f"Обработано {processed_docs} документов.")
    return str(soup)

def _rewire_navigation_links(soup, current_block_id, parent_block, all_blocks):
    """
    'Оживляет' навигационные ссылки в скачанном HTML, заменяя их на локальные пути.
    """
    if not parent_block or parent_block.get('type') not in ['sequential', 'chapter']:
        logger.debug("Невозможно 'оживить' навигацию: родительский блок не найден или неверного типа.")
        return soup 

    siblings = parent_block.get('children', [])
    try:
        current_index = siblings.index(current_block_id)
    except ValueError:
        logger.warning(f"Не удалось найти текущий блок {current_block_id} среди его соседей.")
        return soup

    # Навигация "Предыдущий / Следующий урок"
    prev_button = soup.select_one('.sf-sequence-tab-view__nav-buttons button:first-of-type, .sf-sequence-tab-view__nav-buttons a:first-of-type')
    next_button = soup.select_one('.sf-sequence-tab-view__nav-buttons button:last-of-type, .sf-sequence-tab-view__nav-buttons a:last-of-type')

    if prev_button:
        if current_index > 0:
            prev_block_id = siblings[current_index - 1]
            prev_block_data = all_blocks.get(prev_block_id)
            if prev_block_data:
                prev_filename = f"{sanitize_filename(prev_block_data.get('display_name', ''))}.html"
                prev_button.name = 'a'
                prev_button['href'] = prev_filename
                # Удаляем лишние атрибуты от <button>
                for attr in ['disabled', 'type', 'class']:
                    if prev_button.has_attr(attr): del prev_button[attr]
                prev_button['class'] = 'sf-button sf-button--icon-start'
        else:
            prev_button.decompose()

    if next_button:
        if current_index < len(siblings) - 1:
            next_block_id = siblings[current_index + 1]
            next_block_data = all_blocks.get(next_block_id)
            if next_block_data:
                next_filename = f"{sanitize_filename(next_block_data.get('display_name', ''))}.html"
                next_button.name = 'a'
                next_button['href'] = next_filename
                for attr in ['disabled', 'type', 'class']:
                    if next_button.has_attr(attr): del next_button[attr]
                next_button['class'] = 'sf-button sf-button--icon-start'
        else:
            next_button.decompose()

    # Верхняя панель навигации по урокам
    tabs_container = soup.select_one('.sequence-tab-view-navigation__tabs-container')
    if tabs_container:
        # Очищаем старые табы, которые могут быть кнопками или ссылками
        for child in tabs_container.find_all(recursive=False):
            child.decompose()
        
        for i, block_id in enumerate(siblings):
            block_data = all_blocks.get(block_id)
            if block_data:
                filename = f"{sanitize_filename(block_data.get('display_name', ''))}.html"
                
                # Создаем контейнер-ссылку
                new_tab_link = soup.new_tag('a', href=filename)
                
                # Создаем внутренний div
                new_tab_div = soup.new_tag('div', **{'class': 'sf-unit-tab sequence-tab-view-navigation__tab'})
                if block_id == current_block_id:
                    new_tab_div['class'].append('sf-unit-tab--current')
                
                new_tab_link.append(new_tab_div)
                tabs_container.append(new_tab_link)

    return soup

def process_content_block(driver, session, block_data, all_blocks, parent_block, html_filepath, output_dir, no_videos):
    """
    Универсальная функция для обработки контент-блока.
    Определяет, содержит ли страница видео или текст, и действует соответственно.
    """
    content_url = block_data.get('lms_web_url')
    display_name = block_data.get('display_name', 'Без названия')
    if not content_url:
        logger.warning(f"У блока '{display_name}' отсутствует lms_web_url. Пропускаю.")
        return

    logger.info(f"Обрабатываю страницу: '{display_name}' ({content_url})")
    
    try:
        driver.get(content_url)
        
        # ШАГ 1: Ждем загрузки контента
        kinescope_selector_str = "iframe[src*='kinescope.io']"
        unit_iframe_selector_str = "iframe#unit-iframe"
        xblock_selector_str = "div.xblock"
        combined_wait_selector = f"{kinescope_selector_str}, {unit_iframe_selector_str}, {xblock_selector_str}"
        
        logger.info(f"Ожидаю загрузки контента урока (до 40 секунд)...")
        try:
            WebDriverWait(driver, 40).until(EC.presence_of_element_located((By.CSS_SELECTOR, combined_wait_selector)))
            logger.info("✔ Контент урока обнаружен.")
        except TimeoutException:
            logger.error("За 40 секунд не удалось обнаружить контент. Сохраняю страницу 'как есть'.")
            with open(html_filepath, 'w', encoding='utf-8') as f: f.write(driver.page_source)
            return

        # ====================================================================================
        # НОВЫЙ ШАГ: СИНХРОНИЗАЦИЯ COOKIES и ОПРЕДЕЛЕНИЕ ПРАВИЛЬНОГО BASE_URL
        # ====================================================================================
        # После навигации драйвер мог получить новые/обновленные cookies.
        # Передаем их в сессию requests, чтобы все последующие запросы были аутентифицированы.
        logger.debug("Синхронизирую cookies из браузера в сессию requests...")
        for cookie in driver.get_cookies():
            session.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'])
        logger.debug("✔ Cookies синхронизированы.")

        # Используем URL, который получился в браузере ПОСЛЕ всех редиректов.
        # Это самый надежный источник для относительных путей.
        final_page_url = driver.current_url
        logger.info(f"Финальный URL страницы после редиректов: {final_page_url}")
        # ====================================================================================

        # ШАГ 2: Скачиваем видео, если оно есть
        video_downloaded = False
        if not no_videos:
            iframe_src = None
            try:
                iframe_element = driver.find_element(By.CSS_SELECTOR, kinescope_selector_str)
                iframe_src = iframe_element.get_attribute('src')
            except:
                try:
                    driver.switch_to.frame(driver.find_element(By.CSS_SELECTOR, unit_iframe_selector_str))
                    iframe_element = driver.find_element(By.CSS_SELECTOR, kinescope_selector_str)
                    iframe_src = iframe_element.get_attribute('src')
                except: pass
                finally: driver.switch_to.default_content()
            
            if iframe_src:
                logger.info("✔ Обнаружен Kinescope. Начинаю скачивание видео...")
                try:
                    video_id_match = re.search(r'kinescope\.io/(?:embed/)?([a-zA-Z0-9]+)', iframe_src)
                    if not video_id_match: raise ValueError("Не удалось извлечь ID видео.")
                    
                    downloader = KinescopeDownloader(
                        video_id=video_id_match.group(1), video_name=display_name,
                        referer=content_url, session=session,
                        output_dir=os.path.dirname(html_filepath)
                    )
                    video_downloaded = downloader.download()
                    if not video_downloaded: raise Exception("KinescopeDownloader не смог скачать видео.")
                except Exception as e:
                    logger.error(f"Ошибка при загрузке видео '{display_name}': {e}", exc_info=True)
                    video_downloaded = False

        # ШАГ 3: Собираем "сводный" HTML, встраивая контент iframe в основную страницу.
        logger.info(f"Начинаю полную локализацию страницы '{display_name}'...")
        driver.switch_to.default_content()
        
        page_soup = BeautifulSoup(driver.page_source, 'html.parser')
        
        try:
            unit_iframe_element = driver.find_element(By.ID, 'unit-iframe')
            logger.debug("Найден #unit-iframe. Начинаю слияние контента...")
            
            iframe_url = unit_iframe_element.get_attribute('src')
            driver.switch_to.frame(unit_iframe_element)
            iframe_soup = BeautifulSoup(driver.page_source, 'html.parser')
            driver.switch_to.default_content()
            
            # Сливаем <head>: копируем все <link> и <style> из iframe в head основной страницы
            if page_soup.head and iframe_soup.head:
                for tag in iframe_soup.head.find_all(['link', 'style']):
                    # Исправляем относительные пути в CSS ссылках из iframe
                    if tag.name == 'link' and tag.get('href'):
                        href = tag.get('href')
                        if not href.startswith(('http://', 'https://', '//')):
                            # Преобразуем относительный путь в абсолютный, используя URL iframe
                            absolute_href = urljoin(iframe_url, href)
                            tag['href'] = absolute_href
                            logger.debug(f"Исправлен относительный путь CSS из iframe: {href} -> {absolute_href}")
                    page_soup.head.append(tag)
                logger.debug("Стили из iframe встроены в основной документ с исправленными путями.")

            # Сливаем <body>: заменяем тег iframe на его содержимое
            unit_iframe_tag_in_page = page_soup.find('iframe', {'id': 'unit-iframe'})
            if unit_iframe_tag_in_page and iframe_soup.body:
                # Исправляем относительные пути в изображениях и других ресурсах из iframe
                for img in iframe_soup.body.find_all('img'):
                    src = img.get('src')
                    if src and not src.startswith(('http://', 'https://', '//', 'data:')):
                        absolute_src = urljoin(iframe_url, src)
                        img['src'] = absolute_src
                        logger.debug(f"Исправлен относительный путь изображения из iframe: {src} -> {absolute_src}")
                
                unit_iframe_tag_in_page.replace_with(*iframe_soup.body.contents)
                logger.debug("Содержимое тела iframe встроено в основной документ с исправленными путями.")
                
        except Exception as e:
            logger.warning(f"Не удалось встроить контент из #unit-iframe: {e}")

        html_content_soup = page_soup

        # ШАГ 4: Унифицированная обработка и сохранение объединенной страницы
        
        # Ищем тег <base> для корректного разрешения URL
        base_tag = html_content_soup.find('base')
        base_url = base_tag['href'] if base_tag and base_tag.get('href') else final_page_url # ИЗМЕНЕНО
        logger.info(f"Использую базовый URL для ресурсов: {base_url}")

        lesson_path = os.path.dirname(html_filepath)
        assets_dir = os.path.join(output_dir, '_assets')
        css_dir = os.path.join(assets_dir, 'css')
        js_dir = os.path.join(assets_dir, 'js')
        
        # Преобразуем суп обратно в строку для обработки
        html_content = str(html_content_soup)

        # Сначала обрабатываем все ресурсы, потом очищаем
        # Добавляем диагностику CSS перед обработкой
        temp_soup = BeautifulSoup(html_content, 'html.parser')
        css_links_before = temp_soup.find_all('link', rel='stylesheet')
        logger.info(f"CSS файлов найдено ДО обработки: {len(css_links_before)}")
        for i, link in enumerate(css_links_before, 1):
            href = link.get('href', 'N/A')
            logger.debug(f"CSS #{i}: {href}")
        
        html_content = download_css_and_update_html(base_url, html_content, html_filepath, css_dir, session)
        html_content = download_js_and_update_html(base_url, html_content, html_filepath, js_dir, session)
        html_content = download_images_and_update_html(base_url, html_content, lesson_path, session)
        html_content = download_documents_and_update_html(base_url, html_content, lesson_path, session)
        
        # Очень осторожная очистка только метрики и чата - ПОСЛЕ обработки ресурсов
        html_content = _clean_specific_trackers(html_content)
        
        # Диагностика CSS после всей обработки
        final_soup_diag = BeautifulSoup(html_content, 'html.parser')
        css_links_after = final_soup_diag.find_all('link', rel='stylesheet')
        logger.info(f"CSS файлов осталось ПОСЛЕ обработки: {len(css_links_after)}")
        for i, link in enumerate(css_links_after, 1):
            href = link.get('href', 'N/A')
            logger.debug(f"Финальный CSS #{i}: {href}")

        if video_downloaded:
            video_filepath = os.path.join(lesson_path, f"{sanitize_filename(display_name)}.mp4")
            relative_video_path = os.path.relpath(video_filepath, lesson_path)
            html_content = _embed_local_video(html_content, relative_video_path)
        
        final_soup = BeautifulSoup(html_content, 'html.parser')

        # --- ОКОНЧАТЕЛЬНОЕ ИСПРАВЛЕНИЕ MATHJAX ---
        # 1. Вставляем в <head> скрипт конфигурации, который принудительно включает SVG-рендер.
        #    Это должно выполняться до загрузки самого MathJax.js.
        logger.debug("Внедряю конфигурацию MathJax для принудительного SVG-рендеринга...")
        if final_soup.head:
            config_script_tag = final_soup.new_tag("script", type="text/x-mathjax-config")
            config_script_tag.string = """
            MathJax.Hub.Config({
                jax: ["input/TeX", "output/SVG"],
                extensions: ["tex2jax.js"],
                "HTML-CSS": { availableFonts: ["TeX"] },
                SVG: { availableFonts: ["TeX"] }
            });
            """
            final_soup.head.insert(0, config_script_tag)
            logger.debug("✔ Конфигурация MathJax внедрена.")
        else:
            logger.warning("Не найден тег <head>, конфигурация MathJax не может быть внедрена.")

        # 2. Удаляем все ранее добавленные хаки для перерисовки. Они больше не нужны.
        for old_script in final_soup.find_all("script", string=re.compile(r'MathJax\\.Hub\\.Queue')):
            logger.debug("Удаляю устаревший скрипт для перерисовки MathJax.")
            old_script.decompose()
        # --- КОНЕЦ ИСПРАВЛЕНИЯ ---

        final_soup = _rewire_navigation_links(final_soup, block_data.get('id'), parent_block, all_blocks)

        with open(html_filepath, 'w', encoding='utf-8') as f:
            f.write(str(final_soup))
            
        logger.info(f"✔ Страница '{display_name}' полностью обработана и сохранена.")

    except Exception as e:
        logger.error(f"Критическая ошибка при обработке страницы '{display_name}': {e}", exc_info=True)

def download_material(block_id, all_blocks, current_path, session, no_videos, output_dir, driver, parent_block=None, force_overwrite=False):
    block_data = all_blocks.get(block_id)
    if not block_data:
        logger.warning(f"Пропущен блок: не найден ID {block_id}")
        return

    display_name = block_data.get('display_name', 'Без названия')
    
    # НОВЫЙ УНИВЕРСАЛЬНЫЙ ФИЛЬТР: Пропускаем все блоки, содержащие ключевые слова.
    if any(keyword in display_name.lower() for keyword in IGNORE_KEYWORDS_IN_TITLES):
        logger.info(f"Пропускаю административный/вспомогательный раздел: '{display_name}'")
        return
        
    sanitized_name = sanitize_filename(display_name)
    block_type = block_data.get('type')

    # Обработка директорий
    if block_type in ['course', 'chapter', 'sequential']:
        new_path = os.path.join(current_path, sanitized_name)
        os.makedirs(new_path, exist_ok=True)
        children = block_data.get('children', [])
        if not children:
            logger.info(f"Раздел '{display_name}' не содержит вложенных элементов.")
            return
        logger.info(f"Захожу в раздел: '{display_name}'")
        for child_id in children:
            download_material(child_id, all_blocks, new_path, session, no_videos, output_dir, driver, parent_block=block_data)
        return

    # Обработка контентных блоков (единая точка входа)
    try:
        display_name = block_data.get('display_name', 'Без названия')
        sanitized_name = sanitize_filename(display_name)
        html_filepath = os.path.join(current_path, f"{sanitized_name}.html")
        
        if os.path.exists(html_filepath):
            logger.info(f"Файл '{os.path.basename(html_filepath)}' уже существует. Пропускаю.")
            logger.debug(f"Полный путь к файлу: {html_filepath}")
            return

        process_content_block(driver, session, block_data, all_blocks, parent_block, html_filepath, output_dir, no_videos)
    except Exception as e:
        logger.error(f"Произошла ошибка при обработке блока {block_data.get('display_name', 'N/A')}: {e}")

def find_root_block(course_structure):
    """
    Анализирует структуру курса, находит ID корневого элемента и возвращает его
    вместе со словарем всех блоков курса.
    """
    try:
        all_blocks = course_structure['course_blocks']['blocks']
        for block_id, block_data in all_blocks.items():
            if block_data.get('type') == 'course':
                logger.info(f"Корневой элемент успешно найден: {block_id}")
                return block_id, all_blocks
        
        logger.error("В словаре блоков не найден элемент с type='course'.")
        return None, None
    except (KeyError, TypeError):
        logger.error("Структура JSON не соответствует ожидаемой. Не удалось найти 'course_blocks' -> 'blocks'.")
        return None, None

def download_course_content(root_id, all_blocks, session, output_dir, no_videos=False):
    logger.info("Начинаем пакетное скачивание курса...")
    driver = None
    try:
        logger.info("Инициализация единого экземпляра браузера...")
        options = webdriver.ChromeOptions()
        # options.add_argument('--headless')
        options.add_argument('--disable-gpu')
        options.add_argument('--window-size=1920,1080')
        options.add_experimental_option("excludeSwitches", ["enable-logging"])

        # Настраиваем автоматическую загрузку файлов без диалогового окна
        temp_download_dir = os.path.join(os.path.abspath(output_dir), "_temp_downloads")
        os.makedirs(temp_download_dir, exist_ok=True)
        logger.info(f"Временные файлы (журналы Kinescope) будут сохраняться в: {temp_download_dir}")
        prefs = {"download.default_directory": temp_download_dir}
        options.add_experimental_option("prefs", prefs)

        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        
        driver.get("https://apps.skillfactory.ru/404")
        time.sleep(2)
        current_hostname = urlparse(driver.current_url).hostname
        for cookie in session.cookies:
            if cookie.domain in current_hostname:
                cookie_dict = {'name': cookie.name, 'value': cookie.value, 'domain': cookie.domain, 'path': cookie.path, 'secure': cookie.secure}
                if cookie.expires: cookie_dict['expiry'] = cookie.expires
                if hasattr(cookie, '_rest') and ('HttpOnly' in cookie._rest or 'httponly' in cookie._rest): cookie_dict['httpOnly'] = True
                driver.add_cookie(cookie_dict)
        logger.info("Cookies сессии успешно переданы в единый браузер.")

        download_material(root_id, all_blocks, output_dir, session, no_videos, output_dir, driver=driver)
    finally:
        if driver:
            logger.info("Закрытие единого экземпляра браузера.")
            driver.quit()

def get_enrolled_courses_data(session):
    """
    Получает список курсов пользователя и возвращает его в виде списка словарей.
    """
    logger.info("Получение списка доступных курсов...")
    lk_url = "https://student-lk.skillfactory.ru/"
    try:
        response = session.get(lk_url, timeout=20)
        response.raise_for_status()

        match = re.search(r'\\"studentCourses\\":(\[.*?\])', response.text)
        
        if not match:
            logger.error("Не удалось найти информацию о курсах на странице личного кабинета.")
            with open("lk_page_error.html", "w", encoding="utf-8") as f:
                f.write(response.text)
            logger.debug("HTML-ответ сохранен в lk_page_error.html для анализа.")
            return None

        courses_json_str = match.group(1).replace('\\"', '"')
        
        all_courses = []
        try:
            courses_data = json.loads(courses_json_str)
            for course in courses_data:
                if 'courseKey' in course and 'courseName' in course:
                    all_courses.append({'id': course['courseKey'], 'name': course['courseName']})
        except json.JSONDecodeError as e:
            logger.error(f"Критическая ошибка декодирования JSON: {e}")
            logger.debug(f"Проблемный JSON: {courses_json_str}")
            return None
        
        if not all_courses:
            logger.error("Не найдено ни одного курса с корректными данными.")
            return None
        
        return sorted(all_courses, key=lambda item: item['name'])

    except requests.RequestException as e:
        logger.error(f"Ошибка при загрузке страницы личного кабинета: {e}")
        return None

def choose_course_from_list(courses):
    """
    Отображает список курсов и просит пользователя сделать выбор.
    """
    if not courses:
        return None
        
    print("Выберите курс для скачивания:")
    for i, course in enumerate(courses, 1):
        print(f"  [{i}] {course['name']} (ID: {course['id']})")

    while True:
        try:
            choice_num = int(input("Введите номер курса: "))
            if 1 <= choice_num <= len(courses):
                return courses[choice_num - 1]
            else:
                print("Неверный номер. Пожалуйста, попробуйте еще раз.")
        except ValueError:
            print("Пожалуйста, введите число.")

def build_navigation_tree(block_id, all_blocks):
    """
    Строит навигационное дерево из плоского списка блоков для интерактивной навигации.
    """
    block_data = all_blocks.get(block_id)
    if not block_data:
        return None

    display_name = block_data.get('display_name', 'N/A')
    
    # НОВЫЙ УНИВЕРСАЛЬНЫЙ ФИЛЬТР: Пропускаем все блоки, содержащие ключевые слова.
    if any(keyword in display_name.lower() for keyword in IGNORE_KEYWORDS_IN_TITLES):
        logger.debug(f"Игнорирую блок '{display_name}' при построении дерева навигации.")
        return None

    node = {
        'id': block_id,
        'display_name': display_name,
        'type': block_data.get('type', 'N/A'),
        'children': []
    }

    # Рекурсивно строим дочерние узлы
    if 'children' in block_data:
        for child_id in block_data['children']:
            child_node = build_navigation_tree(child_id, all_blocks)
            if child_node:
                # УДАЛЕНО: Больше не пропускаем "пустые" уроки (vertical), чтобы можно было до них дойти
                # if child_node['type'] == 'vertical' and not child_node['children']:
                #     continue
                node['children'].append(child_node)
    
    return node

def interactive_navigate(root_id, all_blocks, session, output_dir, no_videos=False):
    """
    Интерактивная навигация с использованием одного экземпляра драйвера.
    """
    driver = None
    try:
        driver_initialized = False

        logger.info("Построение навигационного дерева...")
        course_tree = build_navigation_tree(root_id, all_blocks)
        if not course_tree:
            logger.error("Не удалось построить дерево навигации.")
            return
        
        path_stack = []
        current_node = course_tree
        
        while True:
            print("\n" + "="*80)
            current_path_parts = [p['display_name'] for p in path_stack] + [current_node['display_name']]
            breadcrumbs = " > ".join(current_path_parts)
            print(f"Текущий раздел: {breadcrumbs}")
            print("="*80)
            
            if not current_node['children']:
                # --- УЛУЧШЕННОЕ СООБЩЕНИЕ ДЛЯ ЛИСТЬЕВ ДЕРЕВА ---
                node_type = current_node.get('type')
                if node_type == 'vertical':
                    print("Это страница урока. Вы можете скачать ее целиком.")
                elif node_type in ['html', 'video', 'problem']:
                    print(f"Это отдельный контент-блок ('{node_type}'). Вы можете скачать его.")
                else:
                    print("В этом разделе нет вложенных элементов для навигации. Вы можете скачать его.")
            else:
                for i, child in enumerate(current_node['children']):
                    print(f"  [{i+1}] {child['display_name']} (Тип: {child['type']})")
            
            print("\nДоступные действия:")
            print("  [номер] - перейти в раздел/урок")
            print("  [d] - скачать текущий раздел/урок")
            print("  [b] - вернуться назад")
            print("  [q] - выйти")
            
            choice = input("Ваш выбор: ").strip().lower()

            if choice.isdigit():
                try:
                    choice_idx = int(choice) - 1
                    if 0 <= choice_idx < len(current_node['children']):
                        path_stack.append(current_node)
                        current_node = current_node['children'][choice_idx]
                    else:
                        print("! Неверный номер. Пожалуйста, попробуйте еще раз.")
                except IndexError:
                    print("! Неверный номер. Пожалуйста, попробуйте еще раз.")
            elif choice == 'd':
                if not driver_initialized:
                    logger.info("Для интерактивного режима будет запущен единый браузер.")
                    options = webdriver.ChromeOptions()
                    # options.add_argument('--headless')
                    options.add_argument('--disable-gpu')
                    options.add_argument('--window-size=1920,1080')
                    options.add_experimental_option("excludeSwitches", ["enable-logging"])

                    # Настраиваем автоматическую загрузку файлов без диалогового окна
                    temp_download_dir = os.path.join(os.path.abspath(output_dir), "_temp_downloads")
                    os.makedirs(temp_download_dir, exist_ok=True)
                    logger.info(f"Временные файлы (журналы Kinescope) будут сохраняться в: {temp_download_dir}")
                    prefs = {"download.default_directory": temp_download_dir}
                    options.add_experimental_option("prefs", prefs)

                    service = Service(ChromeDriverManager().install())
                    driver = webdriver.Chrome(service=service, options=options)
                    
                    driver.get("https://apps.skillfactory.ru/404")
                    time.sleep(2)
                    current_hostname = urlparse(driver.current_url).hostname
                    for cookie in session.cookies:
                        if cookie.domain in current_hostname:
                            cookie_dict = {'name': cookie.name, 'value': cookie.value, 'domain': cookie.domain, 'path': cookie.path, 'secure': cookie.secure}
                            if cookie.expires: cookie_dict['expiry'] = cookie.expires
                            if hasattr(cookie, '_rest') and ('HttpOnly' in cookie._rest or 'httponly' in cookie._rest): cookie_dict['httpOnly'] = True
                            driver.add_cookie(cookie_dict)
                    logger.info("Cookies сессии успешно переданы в единый браузер.")
                    driver_initialized = True
                
                relative_path_to_parent = current_path_parts[1:-1]
                sanitized_parts = [sanitize_filename(p) for p in relative_path_to_parent]
                download_parent_dir = os.path.join(output_dir, *sanitized_parts)
                os.makedirs(download_parent_dir, exist_ok=True)
                
                parent_block = path_stack[-1] if path_stack else None
                logger.info(f"Начинаю скачивание '{current_path_parts[-1]}' в директорию '{download_parent_dir}'")
                download_material(current_node['id'], all_blocks, download_parent_dir, session, no_videos, output_dir, driver, parent_block=parent_block)
                logger.info("✔ Скачивание завершено.")
                
            elif choice == 'b':
                if path_stack:
                    current_node = path_stack.pop()
                else:
                    print("! Вы уже на верхнем уровне.")
            elif choice == 'q':
                print("Выход.")
                break
            else:
                print("! Неизвестная команда.")
                
    finally:
        if driver:
            logger.info("Закрытие единого экземпляра браузера.")
            driver.quit()

# ==================================================================================================
# KINESCOPE MPEG-DASH DOWNLOADER (АДАПТИРОВАНО ИЗ ПРЕДОСТАВЛЕННЫХ СКРИПТОВ)
# ==================================================================================================

class KinescopeDownloader:
    """
    Класс для скачивания видео с Kinescope, использующего технологию MPEG-DASH.
    Адаптирован из kinescope_downloader.py для работы с requests.session и улучшения логирования.
    """
    def __init__(self, video_id: str, video_name: str, referer: str, session, output_dir, debug=False):
        self.video_id = video_id
        self.video_name = sanitize_filename(video_name)
        self.base_url = "https://kinescope.io"
        self.referer = referer
        self.session = session # Используем общую сессию
        self.output_dir = output_dir
        self.debug = debug

    def _get_media_chunk(self, url, byte_range):
        """Скачивает один чанк данных по URL и диапазону байт."""
        headers = {'Range': f"bytes={byte_range}"}
        try:
            response = self.session.get(url, headers=headers, timeout=60)
            response.raise_for_status()
            return response.content
        except requests.RequestException as e:
            logger.error(f"Ошибка при скачивании чанка {url} (диапазон: {byte_range}): {e}")
            return b''

    def _download_stream(self, representation, stream_type='video'):
        """Скачивает и собирает все сегменты для одного потока (аудио или видео)."""
        stream_type_rus = "видеодорожки" if stream_type == 'video' else "аудиодорожки"
        logger.info(f"Начинаю загрузку потока: {stream_type_rus}...")
        
        # 1. Получаем базовый URL и инициализационный сегмент
        base_url_path = representation.get('BaseURL')
        if not base_url_path:
            logger.error(f"Не найден BaseURL для потока {stream_type}.")
            return None
        
        stream_base_url = urljoin(f"{self.base_url}/{self.video_id}/", base_url_path)

        init_segment_info = representation['SegmentList']['Initialization']
        init_range = init_segment_info['@range']
        init_url = urljoin(stream_base_url, init_segment_info['@sourceURL'])
        
        logger.debug(f"[{stream_type}] Скачиваю инициализационный сегмент...")
        stream_data = self._get_media_chunk(init_url, init_range)
        if not stream_data:
            logger.error(f"Не удалось скачать инициализационный сегмент для {stream_type}.")
            return None

        # 2. Скачиваем и собираем все медиа-сегменты
        segments = representation['SegmentList']['SegmentURL']
        total_segments = len(segments)
        
        with tqdm(total=total_segments, desc=f"Скачивание ({stream_type_rus})", unit="seg") as pbar:
            for seg_info in segments:
                media_range = seg_info['@mediaRange']
                media_url_part = seg_info.get('@media')
                
                # Если у сегмента свой файл, используем его, иначе - базовый URL потока
                final_media_url = urljoin(stream_base_url, media_url_part) if media_url_part else stream_base_url
                
                stream_data += self._get_media_chunk(final_media_url, media_range)
                pbar.update(1)
        
        logger.info(f"✔ Поток {stream_type_rus} успешно загружен.")
        return stream_data

    def download(self):
        """Основной метод для скачивания и сборки видео."""
        
        # УЛУЧШЕНИЕ: Явное сообщение о начале скачивания конкретного видео
        logger.info(f"Начинаю полное скачивание и сборку видео: '{self.video_name}.mp4'")

        # 0. Проверяем наличие ffmpeg
        if not shutil.which("ffmpeg"):
            logger.critical("Утилита 'ffmpeg' не найдена в системе. Она необходима для сборки видео.")
            logger.critical("Пожалуйста, установите ffmpeg и убедитесь, что путь к нему добавлен в системную переменную PATH.")
            return False

        # 1. Получаем и парсим MPD манифест
        mpd_url = f"{self.base_url}/{self.video_id}/master.mpd"
        logger.info(f"Получаю видео-манифест с {mpd_url}")
        try:
            mpd_req = self.session.get(mpd_url, headers={'Referer': self.referer}, timeout=30)
            mpd_req.raise_for_status()
            mpd = xmltodict.parse(mpd_req.content)
        except Exception as e:
            logger.error(f"Не удалось получить или распарсить MPD-манифест: {e}")
            return False

        adaptation_sets = mpd['MPD']['Period']['AdaptationSet']
        
        # 2. Ищем и скачиваем видеопоток лучшего качества
        video_set = next((s for s in adaptation_sets if s.get('@mimeType', '').startswith('video/')), None)
        if not video_set:
            logger.error("Видеопоток не найден в манифесте.")
            return False
            
        representations = video_set['Representation']
        if not isinstance(representations, list):
            representations = [representations]
        
        best_video_repr = max(representations, key=lambda r: int(r.get('@width', 0)))
        logger.info(f"Выбрано лучшее качество видео: {best_video_repr.get('@width')}x{best_video_repr.get('@height')}")
        video_data = self._download_stream(best_video_repr, 'video')
        if not video_data: return False

        # 3. Ищем и скачиваем аудиопоток
        audio_set = next((s for s in adaptation_sets if s.get('@mimeType', '').startswith('audio/')), None)
        if not audio_set:
            logger.error("Аудиопоток не найден в манифесте.")
            return False
        audio_data = self._download_stream(audio_set['Representation'], 'audio')
        if not audio_data: return False

        # 4. Сохраняем временные файлы и собираем финальное видео
        temp_video_path = os.path.join(self.output_dir, f"{self.video_id}.video")
        temp_audio_path = os.path.join(self.output_dir, f"{self.video_id}.audio")
        final_video_path = os.path.join(self.output_dir, f"{self.video_name}.mp4")

        try:
            logger.info("Сохраняю временные аудио/видео файлы...")
            with open(temp_video_path, 'wb') as f: f.write(video_data)
            with open(temp_audio_path, 'wb') as f: f.write(audio_data)

            logger.info(f"Собираю финальный файл '{self.video_name}.mp4' с помощью ffmpeg...")
            # Команда для сборки без перекодирования
            convert_cmd = [
                "ffmpeg", "-y",
                "-i", temp_video_path,
                "-i", temp_audio_path,
                "-c", "copy",
                "-bsf:a", "aac_adtstoasc",
                final_video_path
            ]
            
            # Запускаем ffmpeg, скрывая его стандартный вывод и указывая кодировку
            result = subprocess.run(
                convert_cmd,
                check=True,
                capture_output=True,
                encoding='utf-8',
                errors='ignore'
            )
            logger.debug(f"ffmpeg stdout: {result.stdout}")
            logger.debug(f"ffmpeg stderr: {result.stderr}")
            
            logger.info(f"✔ Видео успешно собрано: {final_video_path}")
            return True
        except subprocess.CalledProcessError as e:
            logger.error("Ошибка при сборке видео с помощью ffmpeg.")
            logger.error(f"Команда: {' '.join(e.cmd)}")
            logger.error(f"Код возврата: {e.returncode}")
            logger.error(f"Вывод ffmpeg (stderr): {e.stderr}")
            return False
        except Exception as e:
            logger.error(f"Ошибка на этапе сохранения или сборки: {e}", exc_info=True)
            return False
        finally:
            # Очистка временных файлов
            if os.path.exists(temp_video_path): os.remove(temp_video_path)
            if os.path.exists(temp_audio_path): os.remove(temp_audio_path)
            
# ==================================================================================================
# ГЛАВНАЯ ФУНКЦИЯ
# ==================================================================================================

def main():
    """
    Главная функция-оркестратор.
    """
    parser = argparse.ArgumentParser(description='SkillFactory Course Downloader')
    parser.add_argument('-u', '--username', required=True, help='Email')
    parser.add_argument('-p', '--password', required=True, help='Password')
    parser.add_argument('--course-url', help='URL курса (опционально, если не указан, будет предложен выбор)')
    parser.add_argument('--debug', '-v', action='store_true', help='Включить отладочное логирование')
    parser.add_argument('--interactive', action='store_true', help='Выбрать разделы для скачивания в интерактивном режиме')
    parser.add_argument('--no-videos', action='store_true', help='Не скачивать видео (по умолчанию видео скачиваются).')
    args = parser.parse_args()

    # --- РАСШИРЕННАЯ НАСТРОЙКА ЛОГИРОВАНИЯ ---
    log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    
    # Убираем все предыдущие обработчики с корневого логгера
    root_logger = logging.getLogger()
    root_logger.handlers = []
    
    # Настройка вывода в консоль
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(log_formatter)
    console_handler.setLevel(logging.INFO) # Только INFO и выше в консоль
    
    # Настройка вывода в файл (всегда на уровне DEBUG)
    file_handler = logging.FileHandler('downloader.log', mode='w', encoding='utf-8')
    file_handler.setFormatter(log_formatter)
    file_handler.setLevel(logging.DEBUG)
    
    # Добавляем обработчики к корневому логгеру
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
    root_logger.setLevel(logging.DEBUG) # Минимальный уровень для захвата всех сообщений
    # --- КОНЕЦ НАСТРОЙКИ ЛОГИРОВАНИЯ ---

    # Шаг 1: Логин
    session = login_to_skillfactory(args.username, args.password)
    if not session:
        logger.critical("Не удалось авторизоваться. Завершение работы.")
        return

    course_structure = None
    course_id_to_download = args.course_url
    output_dir = "" # Инициализируем, чтобы избежать UnboundLocalError

    # РАЗДЕЛЕНИЕ ЛОГИКИ: Интерактивный режим vs. Прямой режим по URL
    if not course_id_to_download:
        # Сценарий 1: Интерактивный выбор курса
        courses = get_enrolled_courses_data(session)
        if not courses:
            logger.critical("Не удалось получить список курсов. Завершение работы.")
            return
        
        selected_course = choose_course_from_list(courses)
        if not selected_course:
            logger.critical("Курс не выбран. Завершение работы.")
            return
        
        course_id_to_download = selected_course['id']
        course_name = selected_course['name']
        
        # Проверяем, есть ли уже скачанная структура
        output_dir = sanitize_filename(course_name)
        os.makedirs(output_dir, exist_ok=True)
        structure_file_path = os.path.join(output_dir, 'course_structure.json')

        if os.path.exists(structure_file_path):
            while True:
                choice = input(f"Найден файл структуры для '{course_name}'.\nИспользовать существующий (y) или скачать новый (n)? ").lower().strip()
                if choice in ['y', 'yes', 'д', 'да']:
                    try:
                        logger.info(f"Загрузка структуры из файла: {structure_file_path}")
                        with open(structure_file_path, 'r', encoding='utf-8') as f:
                            course_structure = json.load(f)
                    except Exception as e:
                        logger.error(f"Не удалось прочитать или распарсить файл {structure_file_path}: {e}")
                        logger.warning("Будет произведена попытка скачать структуру с сервера.")
                        course_structure = None # Принудительная перезагрузка
                    break # Выходим из цикла while
                elif choice in ['n', 'no', 'н', 'нет']:
                    course_structure = None # Принудительная перезагрузка
                    break # Выходим из цикла while
                else:
                    print("Неверный ввод. Пожалуйста, введите y/n.")
        
        # Если структура не была загружена (файл не найден, пользователь выбрал 'n', или файл был поврежден)
        if course_structure is None:
            logger.info("Получение новой структуры курса с сервера...")
            course_structure = get_course_structure(session, course_id_to_download)
    else:
        # Сценарий 2: Прямое указание URL
        logger.info("URL курса предоставлен. Для определения имени директории будет скачана структура курса.")
        logger.warning("Проверка на наличие локальной структуры в этом режиме не поддерживается.")
        course_structure = get_course_structure(session, course_id_to_download)

    # Финальная проверка: получена ли структура в итоге
    if not course_structure:
        logger.critical("Не удалось получить структуру курса. Завершение работы.")
        return

    # Извлекаем корневой элемент и словарь блоков
    root_id, all_blocks = find_root_block(course_structure)
    if not root_id:
        logger.critical("Не удалось обработать структуру курса. Завершение работы.")
        return

    # Шаг 3: Сохранение структуры в файл и НАЧАЛО СКАЧИВАНИЯ
    try:
        # Если output_dir не был определен в интерактивном режиме, определяем его сейчас
        if not output_dir:
            course_title = course_structure.get('name', 'unknown_course')
            output_dir = sanitize_filename(course_title)

        os.makedirs(output_dir, exist_ok=True)
        structure_file_path = os.path.join(output_dir, 'course_structure.json')
        with open(structure_file_path, 'w', encoding='utf-8') as f:
            json.dump(course_structure, f, ensure_ascii=False, indent=4)
        logger.info(f"Структура курса сохранена в: {structure_file_path}")
        
        if args.interactive:
            interactive_navigate(root_id, all_blocks, session, output_dir, no_videos=args.no_videos)
        else:
            download_course_content(root_id, all_blocks, session, output_dir, no_videos=args.no_videos)

    except Exception as e:
        logger.error(f"Ошибка при сохранении файла структуры или скачивании: {e}")
        return

    logger.info("Работа скрипта завершена.")

if __name__ == '__main__':
    main() 