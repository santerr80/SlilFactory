# main.py

import argparse
import logging
import os
import sys
import json
import time
from getpass import getpass

from pathvalidate import sanitize_filename
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from urllib.parse import urlparse

# Импорты из наших модулей
from api import get_course_structure, get_enrolled_courses_data
from auth import login_to_skillfactory
from downloader import download_course_content
from navigation import (
    find_root_block, choose_course_from_list,
    build_navigation_tree, interactive_navigate
)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(module)s] - %(message)s',
    handlers=[
        logging.FileHandler("downloader.log", mode='w', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Скачивает курсы с SkillFactory.")
    parser.add_argument('-u', '--username', help="Ваш email от SkillFactory.")
    parser.add_argument('-p', '--password', help="Ваш пароль. Если не указан, будет запрошен.")
    parser.add_argument('-o', '--output', default='.', help="Папка для сохранения курсов.")
    parser.add_argument('--course_url', help="URL конкретного курса для скачивания.")
    parser.add_argument('--no-videos', action='store_true', help="Не скачивать видео.")
    parser.add_argument('--force-overwrite', action='store_true', help="Принудительно перезаписать существующие файлы.")
    parser.add_argument('--interactive', action='store_true', help="Запустить в интерактивном режиме для выбора курса.")

    args = parser.parse_args()

    # Шаг 1: Логин
    username = args.username or input("Введите email от SkillFactory: ")
    password = args.password or getpass("Введите пароль: ")

    session = login_to_skillfactory(username, password)
    if not session:
        logger.critical("Не удалось авторизоваться. Завершение работы.")
        sys.exit(1)

    # Шаг 2: Получение структуры курса
    course_structure = None
    output_dir = args.output
    course_name_for_dir = ""

    if not args.course_url:
        # Интерактивный выбор
        courses = get_enrolled_courses_data(session)
        if not courses:
            logger.critical("Не удалось получить список курсов. Завершение работы.")
            sys.exit(1)
        
        chosen_course = choose_course_from_list(courses)
        if not chosen_course:
            logger.info("Курс не выбран. Выход.")
            sys.exit(0)
        
        course_url = f"https://lms.skillfactory.ru/courses/{chosen_course['id']}/"
        course_name_for_dir = chosen_course['name']
    else:
        # Прямое указание URL
        course_url = args.course_url

    # Определяем имя папки и путь к кэшу
    if course_name_for_dir:
        output_dir = os.path.join(args.output, sanitize_filename(course_name_for_dir))
    
    os.makedirs(output_dir, exist_ok=True)
    cache_path = os.path.join(output_dir, 'course_structure.json')
    
    use_cache = False
    if os.path.exists(cache_path) and not args.force_overwrite:
        choice = input(f"Найден кэш для курса '{course_name_for_dir}'. Использовать его? (y/n): ").lower()
        if choice in ['y', 'yes']:
            logger.info("Используется кэшированная структура курса.")
            with open(cache_path, 'r', encoding='utf-8') as f:
                course_structure = json.load(f)
            use_cache = True

    if not use_cache:
        logger.info("Получение новой структуры курса с сервера...")
        course_structure = get_course_structure(session, course_url)
        if not course_structure:
            logger.error("Не удалось получить структуру курса.")
            sys.exit(1)
        
        # Если имя курса не было известно (при запуске по URL), извлекаем его сейчас
        if not course_name_for_dir:
             course_name_for_dir = course_structure.get('name', 'unknown_course')
             output_dir = os.path.join(args.output, sanitize_filename(course_name_for_dir))
             os.makedirs(output_dir, exist_ok=True)
             # Переопределяем cache_path, если имя папки изменилось
             cache_path = os.path.join(output_dir, 'course_structure.json')

        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(course_structure, f, ensure_ascii=False, indent=4)
        logger.info(f"Структура курса сохранена в кэш: {cache_path}")


    # Шаг 3: Обработка и скачивание
    root_id, all_blocks = find_root_block(course_structure)
    if not root_id:
        logger.error("Не удалось найти корневой элемент курса.")
        sys.exit(1)

    if args.interactive:
        logger.info("Запуск в интерактивном режиме...")
        course_tree = build_navigation_tree(root_id, all_blocks)
        if not course_tree:
            logger.error("Не удалось построить дерево навигации для интерактивного режима.")
            sys.exit(1)
        interactive_navigate(
            course_tree, all_blocks, session, output_dir, 
            args.no_videos, args.force_overwrite
        )
    else:
        logger.info("Запуск в режиме автоматического скачивания всего курса...")
        download_course_content(
            root_id, all_blocks, session, output_dir,
            args.no_videos, args.force_overwrite, course_name_for_dir
        )

    logger.info("Работа скрипта завершена.")


if __name__ == '__main__':
    main()