# navigation.py

import logging
import os
from pathvalidate import sanitize_filename
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from urllib.parse import urlparse
import time

from config import IGNORE_KEYWORDS_IN_TITLES

logger = logging.getLogger(__name__)


def _rewire_navigation_links(soup, current_block_id, parent_block, all_blocks):
    """
    'Оживляет' навигационные ссылки в скачанном HTML, заменяя их на локальные пути.
    """
    if not parent_block or parent_block.get('type') not in ['sequential']:
        return soup 

    siblings = parent_block.get('children', [])
    try:
        current_index = siblings.index(current_block_id)
    except ValueError:
        return soup

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
        prev_button.attrs.pop('disabled', None)

    if next_button:
        if current_index < len(siblings) - 1:
            next_block_id = siblings[current_index + 1]
            next_block_data = all_blocks.get(next_block_id)
            if next_block_data:
                next_filename = f"{sanitize_filename(next_block_data.get('display_name', ''))}.html"
                next_button.name = 'a'
                next_button['href'] = next_filename
        next_button.attrs.pop('disabled', None)
    
    tabs_container = soup.select_one('.sequence-tab-view-navigation__tabs-container')
    if tabs_container:
        for tab in tabs_container.find_all(True, recursive=False):
             tab.decompose() 

        for i, block_id in enumerate(siblings):
            block_data = all_blocks.get(block_id)
            if block_data and block_data.get('type') == 'vertical':
                filename = f"{sanitize_filename(block_data.get('display_name', ''))}.html"
                new_tab_link = soup.new_tag('a', href=filename)
                new_tab_div = soup.new_tag('div', **{'class': 'sf-unit-tab sequence-tab-view-navigation__tab'})
                if block_id == current_block_id:
                    new_tab_div['class'].append('sf-unit-tab--current')
                new_tab_link.append(new_tab_div)
                tabs_container.append(new_tab_link)

    return soup


def find_root_block(course_structure):
    try:
        all_blocks = course_structure['course_blocks']['blocks']
        for block_id, block_data in all_blocks.items():
            if block_data.get('type') == 'course':
                logger.info(f"Корневой элемент успешно найден: {block_id}")
                return block_id, all_blocks
        logger.error("В словаре блоков не найден элемент с type='course'.")
        return None, None
    except (KeyError, TypeError):
        logger.error("Структура JSON не соответствует ожидаемой.")
        return None, None


def build_navigation_tree(block_id, all_blocks):
    block_data = all_blocks.get(block_id)
    if not block_data: return None
    display_name = block_data.get('display_name', 'N/A')
    if any(keyword in display_name.lower() for keyword in IGNORE_KEYWORDS_IN_TITLES):
        return None
    node = {'id': block_id, 'display_name': display_name, 'type': block_data.get('type', 'N/A'), 'children': []}
    if 'children' in block_data:
        for child_id in block_data['children']:
            child_node = build_navigation_tree(child_id, all_blocks)
            if child_node:
                node['children'].append(child_node)
    return node


def choose_course_from_list(courses):
    if not courses: return None
    print("\nДоступные курсы:")
    for i, course in enumerate(courses):
        print(f"[{i + 1}] {course.get('name', 'Без названия')} (ID: {course.get('id')})")
    while True:
        try:
            choice = input(f"\nВыберите курс (1-{len(courses)}) или введите 'q' для выхода: ")
            if choice.lower() == 'q': return None
            choice_idx = int(choice) - 1
            if 0 <= choice_idx < len(courses):
                return courses[choice_idx]
            else:
                print("Неверный номер.")
        except ValueError:
            print("Некорректный ввод.")


def interactive_navigate(course_tree, all_blocks, session, output_dir, no_videos, force_overwrite):
    # === ИЗМЕНЕНИЕ ЗДЕСЬ: Импорт перенесен внутрь функции ===
    from downloader import download_material 
    from progress_tracker import ProgressTracker
    
    # Создаем ProgressTracker для интерактивного режима
    course_name = course_tree.get('display_name', 'Курс')
    progress_tracker = ProgressTracker(course_name, output_dir)
    
    # Валидируем прогресс с файловой системой
    progress_tracker.validate_and_cleanup_progress()
    
    # Показываем текущий прогресс
    progress_tracker.print_progress_table()
    
    driver = None
    driver_initialized = False
    try:
        path_stack = []
        current_node = course_tree
        while True:
            print("\n" + "="*80)
            breadcrumbs = " > ".join([p['display_name'] for p in path_stack] + [current_node['display_name']])
            print(f"Текущий раздел: {breadcrumbs}")
            print("="*80)
            if current_node['children']:
                for i, child in enumerate(current_node['children']):
                    print(f"  [{i+1}] {child['display_name']} (Тип: {child['type']})")
            else:
                print("В этом разделе нет вложенных элементов.")
            print("\nДоступные действия:\n  [номер] - перейти\n  [d] - скачать\n  [b] - назад\n  [p] - показать прогресс\n  [q] - выйти")
            choice = input("Ваш выбор: ").strip().lower()

            if choice.isdigit():
                try:
                    choice_idx = int(choice) - 1
                    if 0 <= choice_idx < len(current_node['children']):
                        path_stack.append(current_node)
                        current_node = current_node['children'][choice_idx]
                except IndexError:
                    print("! Неверный номер.")
            elif choice == 'd':
                if not driver_initialized:
                    logger.info("Для интерактивного режима будет запущен единый браузер.")
                    options = webdriver.ChromeOptions()
                    options.add_experimental_option("excludeSwitches", ["enable-logging"])
                    service = Service(ChromeDriverManager().install())
                    driver = webdriver.Chrome(service=service, options=options)
                    driver.get("https://lms.skillfactory.ru/404")
                    time.sleep(1)
                    for cookie in session.cookies:
                        driver.add_cookie({k: v for k, v in cookie.__dict__.items() if k != '_rest'})
                    logger.info("Cookies сессии успешно переданы в браузер.")
                    driver_initialized = True
                
                # Исключаем корневой блок курса из пути, чтобы избежать дублирования
                # Корневой блок курса (type='course') уже учтен в output_dir
                relative_path_parts = []
                for p in path_stack:
                    # Получаем данные блока из all_blocks
                    block_data = all_blocks.get(p['id'])
                    # Пропускаем корневой блок курса
                    if block_data and block_data.get('type') != 'course':
                        relative_path_parts.append(p['display_name'])
                
                sanitized_parts = [sanitize_filename(p) for p in relative_path_parts]
                download_path = os.path.join(output_dir, *sanitized_parts)
                os.makedirs(download_path, exist_ok=True)
                parent_block_data = all_blocks.get(path_stack[-1]['id']) if path_stack else None
                logger.info(f"Начинаю скачивание '{current_node['display_name']}' в '{download_path}'...")
                download_material(driver, session, current_node['id'], all_blocks, download_path, output_dir, no_videos, force_overwrite, parent_block=parent_block_data, progress_tracker=progress_tracker)
                logger.info("Скачивание завершено.")
                
                # Показываем обновленный прогресс после скачивания
                progress_tracker.print_progress_table()
            elif choice == 'p':
                # Показать текущий прогресс
                progress_tracker.print_progress_table()
            elif choice == 'b':
                if path_stack:
                    current_node = path_stack.pop()
                else:
                    print("! Вы уже на верхнем уровне.")
            elif choice == 'q':
                break
            else:
                print("! Неизвестная команда.")
    finally:
        if driver:
            logger.info("Закрытие браузера.")
            driver.quit()