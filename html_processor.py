# html_processor.py

import logging
import os
import re
import base64
import hashlib
from urllib.parse import urljoin, urlparse, unquote, quote
import requests
from bs4 import BeautifulSoup
from pathvalidate import sanitize_filename
from utils import download_file
from navigation import _rewire_navigation_links

logger = logging.getLogger(__name__)

def _generate_stable_filename(url, extension):
    """Генерирует короткое стабильное имя файла на основе URL"""
    # Используем MD5 для стабильного хеша
    url_hash = hashlib.md5(url.encode('utf-8')).hexdigest()[:8]
    
    # Пытаемся извлечь осмысленную часть из URL
    parsed_url = urlparse(url)
    
    # Проверяем, есть ли осмысленное имя файла в пути
    base_name = os.path.basename(parsed_url.path)
    if base_name and '.' in base_name:
        # Убираем расширение
        base_name = os.path.splitext(base_name)[0]
        # Оставляем только первые 15 символов для краткости
        base_name = sanitize_filename(base_name[:15])
    
    # Если нет осмысленного имени или имя слишком длинное/состоит из хеша
    if (not base_name or 
        len(base_name) > 30 or 
        len(base_name) < 3 or
        not any(c.isalpha() for c in base_name)):  # Нет букв - значит это хеш
        
        # Пытаемся извлечь осмысленную часть из домена
        domain_parts = parsed_url.netloc.split('.')
        if len(domain_parts) >= 2:
            # Берем основную часть домена (например, 'google' из 'lh3.googleusercontent.com')
            domain_base = domain_parts[-2] if domain_parts[-2] not in ['com', 'org', 'net', 'ru'] else domain_parts[0]
            base_name = domain_base[:8]  # Максимум 8 символов
        else:
            base_name = "img"
    
    # Ограничиваем общую длину имени файла (Windows ограничение ~255 символов)
    # Формат: {base_name}_{hash}.{ext} - должно быть не больше 50 символов
    if len(base_name) > 20:
        base_name = base_name[:20]
    
    return f"{base_name}_{url_hash}.{extension}"

def _embed_local_videos(html_content, downloaded_videos):
    """
    Заменяет все iframe плеера Kinescope на стандартные HTML5-теги <video>.
    downloaded_videos: список словарей с ключами 'iframe_src', 'video_id', 'filename'
    """
    if not downloaded_videos:
        return html_content
        
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Создаем словарь для быстрого поиска: video_id -> filename
    video_mapping = {}
    for video in downloaded_videos:
        video_id_match = re.search(r'kinescope\.io/(?:embed/)?([a-zA-Z0-9]+)', video['iframe_src'])
        if video_id_match:
            video_mapping[video_id_match.group(1)] = video['filename']
    
    # Находим ВСЕ iframe с Kinescope и заменяем их
    iframe_tags = soup.find_all('iframe', src=re.compile(r'kinescope\.io/embed'))
    replaced_count = 0
    
    for iframe_tag in iframe_tags:
        iframe_src = iframe_tag.get('src', '')
        video_id_match = re.search(r'kinescope\.io/(?:embed/)?([a-zA-Z0-9]+)', iframe_src)
        
        if video_id_match:
            video_id = video_id_match.group(1)
            if video_id in video_mapping:
                video_filename = video_mapping[video_id]
                
                # Создаем новый тег <video>
                video_tag = soup.new_tag(
                    "video",
                    controls=True,
                    width="100%",
                    preload="metadata"
                )
                video_tag['src'] = video_filename.replace(os.sep, "/")
                
                # Заменяем iframe на видео
                iframe_tag.replace_with(video_tag)
                replaced_count += 1
                logger.debug(f"Заменен iframe (ID: {video_id}) на локальное видео: {video_filename}")
            else:
                logger.warning(f"Не найдено скачанное видео для ID: {video_id}")
    
    if replaced_count > 0:
        logger.info(f"✔ Заменено {replaced_count} iframe на локальные видео")
    
    return str(soup) 

def _clean_html(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    selectors_to_remove = ['#hde-container', 'script#hde-chat-widget', 'iframe[src*="mc.yandex.ru"]', 'script[src*="mc.yandex.ru"]', 'noscript']
    for selector in selectors_to_remove:
        for element in soup.select(selector):
            element.decompose()
    for script in soup.find_all('script', string=re.compile(r'ym\(|yaCounter')):
        script.decompose()
    
    # Удаление пустых MathJax_Preview блоков
    for preview in soup.find_all('span', class_='MathJax_Preview'):
        # Удаляем если блок пустой или содержит только пробелы (игнорируем атрибуты)
        text_content = preview.get_text(strip=True)
        if not text_content or text_content in ['', ' ', '\n', '\t']:
            preview.decompose()
    
    # Удаление MJX_Assistive_MathML блоков (скрытые accessibility элементы)
    for assistive in soup.find_all('span', class_='MJX_Assistive_MathML'):
        assistive.decompose()
    
    # Удаление других assistive блоков MathJax
    for assistive in soup.find_all('span', attrs={'role': 'presentation'}):
        if 'MJX' in str(assistive.get('class', [])) or 'MathJax' in str(assistive.get('class', [])):
            assistive.decompose()
    
    # Удаление пустых MathJax_SVG блоков с role="presentation" (это пустые блоки)
    for empty_mathjax in soup.find_all('span', class_='MathJax_SVG', attrs={'role': 'presentation'}):
        empty_mathjax.decompose()
    
    # НЕ трогаем MathJax_SVG без role - это настоящие формулы!
    
    # Очистка data-url атрибутов от ссылок на SkillFactory
    for element in soup.find_all(attrs={'data-url': True}):
        data_url = element.get('data-url', '')
        if 'skillfactory.ru' in data_url or 'course-v1:Skillfactory' in data_url:
            element['data-url'] = 'javascript:void(0); // removed skillfactory data-url'
    
    # Очистка xlink:href атрибутов от ссылок на SkillFactory
    for element in soup.find_all(attrs={'xlink:href': True}):
        xlink_href = element.get('xlink:href', '')
        if 'skillfactory.ru' in xlink_href or 'block-v1:Skillfactory' in xlink_href:
            element['xlink:href'] = 'javascript:void(0); // removed skillfactory xlink'
    
    return str(soup)

def _get_full_css_content(css_url, session, processed_urls):
    if css_url in processed_urls: return ""
    processed_urls.add(css_url)
    try:
        response = session.get(css_url, timeout=15)
        response.raise_for_status()
        content = response.text
        def replace_import(match):
            import_statement = match.group(0)
            url_match = re.search(r'url\((["\']?)(.*?)\1\)|(["\'])(.*?)\3', import_statement)
            if not url_match: return ""
            path = (url_match.group(2) or url_match.group(4)).strip()
            
            # Обработка протокол-относительных URL (начинающихся с //)
            if path.startswith('//'):
                # Извлекаем протокол из css_url
                base_protocol = urlparse(css_url).scheme or 'https'
                absolute_url = f"{base_protocol}:{path}"
            else:
                absolute_url = urljoin(css_url, path)
            
            return _get_full_css_content(absolute_url, session, processed_urls)
        return re.sub(r'(?i)@import[^;]+;', replace_import, content)
    except requests.RequestException as e:
        logger.warning(f"Не удалось скачать CSS {css_url}: {e}")
        return ""

def _download_fonts_from_css(css_content, css_base_url, font_dest_dir, css_location_path, session):
    os.makedirs(font_dest_dir, exist_ok=True)
    def font_replacer(match):
        font_url = match.group(1).strip('\'" ')
        if font_url.startswith('data:'): return match.group(0)
        try:
            # Обработка протокол-относительных URL (начинающихся с //)
            if font_url.startswith('//'):
                # Извлекаем протокол из css_base_url
                base_protocol = urlparse(css_base_url).scheme or 'https'
                absolute_font_url = f"{base_protocol}:{font_url}"
            else:
                absolute_font_url = urljoin(css_base_url, font_url)
            
            font_filename_raw = os.path.basename(urlparse(absolute_font_url).path)
            if not font_filename_raw.lower().endswith(('.woff', '.woff2', '.ttf', '.eot', '.otf')):
                return match.group(0)
            font_filename = sanitize_filename(unquote(font_filename_raw))
            if not font_filename: return match.group(0)
            local_font_path = os.path.join(font_dest_dir, font_filename)
            if not os.path.exists(local_font_path):
                if not download_file(absolute_font_url, local_font_path, session):
                    return "url('')"
            relative_font_path = os.path.relpath(local_font_path, os.path.dirname(css_location_path)).replace("\\", "/")
            return f"url('{relative_font_path}')"
        except Exception as e:
            logger.error(f"Ошибка при обработке шрифта {font_url}: {e}")
            return "url('')"
    return re.sub(r'url\(([^)]+)\)', font_replacer, css_content)

def download_css_and_update_html(base_url, html_content, lesson_file_path, root_css_dir, session):
    soup = BeautifulSoup(html_content, 'html.parser')
    os.makedirs(root_css_dir, exist_ok=True)
    root_font_dir = os.path.join(os.path.dirname(root_css_dir), 'fonts')
    processed_css_urls = set()
    
    for link in soup.find_all('link', rel='stylesheet'):
        href = link.get('href')
        if not href or href.startswith('data:'): continue
        try:
            # Обработка протокол-относительных URL (начинающихся с //)
            if href.startswith('//'):
                # Извлекаем протокол из base_url
                base_protocol = urlparse(base_url).scheme or 'https'
                css_url = f"{base_protocol}:{href}"
            else:
                css_url = urljoin(base_url, href)
            
            css_filename = _generate_stable_filename(css_url, 'css')
            local_css_path = os.path.join(root_css_dir, css_filename)
            
            if css_url not in processed_css_urls:
                if not os.path.exists(local_css_path):
                    logger.debug(f"Скачиваю CSS: {css_url} -> {css_filename}")
                    full_css_content = _get_full_css_content(css_url, session, processed_css_urls)
                    processed_css_with_fonts = _download_fonts_from_css(full_css_content, css_url, root_font_dir, local_css_path, session)
                    with open(local_css_path, 'w', encoding='utf-8') as f:
                        f.write(processed_css_with_fonts)
                else:
                    logger.debug(f"CSS файл уже существует: {css_filename}")
                processed_css_urls.add(css_url)
            
            link['href'] = os.path.relpath(local_css_path, os.path.dirname(lesson_file_path)).replace("\\", "/")
        except Exception as e:
            logger.error(f"Ошибка при обработке CSS {href}: {e}", exc_info=True)
    
    for style_tag in soup.find_all('style'):
        if style_tag.string:
            style_tag.string.replace_with(_download_fonts_from_css(style_tag.string, base_url, root_font_dir, lesson_file_path, session))
    return str(soup)

def _clean_js_content(js_content):
    """Очищает JS файлы от ссылок на SkillFactory серверы"""
    if not js_content:
        return js_content
    
    # Паттерны для замены ссылок на SkillFactory серверы
    replacements = [
        # Основные домены SkillFactory
        (r'https://lms\.skillfactory\.ru', 'javascript:void(0); // removed lms.skillfactory.ru'),
        (r'https://apps\.skillfactory\.ru', 'javascript:void(0); // removed apps.skillfactory.ru'),
        (r'https://cms\.skillfactory\.ru', 'javascript:void(0); // removed cms.skillfactory.ru'),
        (r'https://student-lk\.skillfactory\.ru', 'javascript:void(0); // removed student-lk.skillfactory.ru'),
        (r'https://mentor-lk\.skillfactory\.ru', 'javascript:void(0); // removed mentor-lk.skillfactory.ru'),
        (r'https://staff-lk\.skillfactory\.ru', 'javascript:void(0); // removed staff-lk.skillfactory.ru'),
        (r'https://services\.skillfactory\.ru', 'javascript:void(0); // removed services.skillfactory.ru'),
        (r'https://lms-cdn\.skillfactory\.ru', 'javascript:void(0); // removed lms-cdn.skillfactory.ru'),
        
        # Поддомены и другие варианты
        (r'https://[a-zA-Z0-9\-\.]*\.skillfactory\.ru', 'javascript:void(0); // removed skillfactory.ru subdomain'),
        (r'https://skillfactory\.ru', 'javascript:void(0); // removed skillfactory.ru'),
        
        # Специфичные API endpoints
        (r'/login_refresh["\']?', '/dev/null" // removed login_refresh'),
        (r'/csrf/api/v1/token["\']?', '/dev/null" // removed csrf token'),
        (r'/api/user/v1/[^"\']*["\']?', '/dev/null" // removed user api'),
        
        # Email адреса (чтобы не было попыток отправки)
        (r'mailto:[a-zA-Z0-9\.\-_]+@skillfactory\.ru', 'javascript:void(0); // removed skillfactory email'),
        
        # Телеграм и соцсети (менее критично, но для полноты)
        (r'https://t\.me/skillfactory', 'javascript:void(0); // removed telegram'),
        (r'https://vk\.com/skillfactoryschool', 'javascript:void(0); // removed vk'),
        (r'https://blog\.skillfactory\.ru', 'javascript:void(0); // removed blog'),
    ]
    
    cleaned_content = js_content
    for pattern, replacement in replacements:
        try:
            cleaned_content = re.sub(pattern, replacement, cleaned_content, flags=re.IGNORECASE)
        except Exception as e:
            logger.warning(f"Ошибка при очистке JS паттерна {pattern}: {e}")
    
    return cleaned_content 

def download_js_and_update_html(base_url, html_content, lesson_file_path, root_js_dir, session):
    soup = BeautifulSoup(html_content, 'html.parser')
    os.makedirs(root_js_dir, exist_ok=True)
    
    # Удаляем все старые конфигурации MathJax
    for script in soup.find_all('script', type='text/x-mathjax-config'):
        script.decompose()
        logger.info("Удален старый script type='text/x-mathjax-config'")
    for script in soup.find_all('script', type='text/x-mathjax-config;executed=true'):
        script.decompose()
        logger.info("Удален старый script type='text/x-mathjax-config;executed=true'")
    
    # Удаляем старые настройки window.MathJax
    for script in soup.find_all('script'):
        if script.string and 'window.MathJax' in script.string:
            script.decompose()
            logger.info("Удален старый script с window.MathJax")
    
    if soup.head:
        config_script = soup.new_tag("script", type="text/x-mathjax-config")
        config_script.string = """
        MathJax.Hub.Config({
            jax: ["input/TeX", "output/SVG"],
            extensions: ["tex2jax.js"],
            tex2jax: {
                inlineMath: [['$','$'], ['\\(','\\)']],
                displayMath: [['$$','$$'], ['\\[','\\]']],
                processEscapes: true,
                preview: "none"
            },
            SVG: {
                scale: 100,
                linebreaks: { automatic: true }
            },
            showProcessingMessages: false,
            messageStyle: "none"
        });
        
        // Удаляем preview блоки после рендеринга
        MathJax.Hub.Queue(function () {
            var previews = document.querySelectorAll('.MathJax_Preview');
            for (var i = 0; i < previews.length; i++) {
                previews[i].style.display = 'none';
                previews[i].style.visibility = 'hidden';
                previews[i].style.height = '0';
                previews[i].style.width = '0';
                previews[i].style.margin = '0';
                previews[i].style.padding = '0';
            }
            
            // Удаляем assistive элементы MathJax
            var assistive = document.querySelectorAll('.MJX_Assistive_MathML, span[role="presentation"][class*="MJX"]');
            for (var j = 0; j < assistive.length; j++) {
                assistive[j].style.display = 'none';
                assistive[j].style.visibility = 'hidden';
                assistive[j].style.height = '0';
                assistive[j].style.width = '0';
                assistive[j].style.margin = '0';
                assistive[j].style.padding = '0';
            }
            
            // Удаляем пустые MathJax_SVG блоки с role="presentation"
            var emptyMathJax = document.querySelectorAll('span.MathJax_SVG[role="presentation"]');
            for (var k = 0; k < emptyMathJax.length; k++) {
                emptyMathJax[k].style.display = 'none';
                emptyMathJax[k].style.visibility = 'hidden';
                emptyMathJax[k].style.height = '0';
                emptyMathJax[k].style.width = '0';
                emptyMathJax[k].style.margin = '0';
                emptyMathJax[k].style.padding = '0';
            }
        });
        """
        soup.head.insert(0, config_script)
        logger.info("Добавлена новая конфигурация MathJax с отключенным preview")
    
    for script in soup.find_all('script', src=True):
        src = script.get('src')
        if not src: continue
        
        if 'MathJax.js' in src:
            script['src'] = 'https://cdnjs.cloudflare.com/ajax/libs/mathjax/2.7.5/MathJax.js?config=TeX-AMS-MML_SVG'
            continue
        
        if any(x in src for x in ['google-analytics', 'yandex', 'mc.yandex.ru']):
            script.decompose()
            continue
        
        # Обработка протокол-относительных URL (начинающихся с //)
        if src.startswith('//'):
            # Извлекаем протокол из base_url
            base_protocol = urlparse(base_url).scheme or 'https'
            js_url = f"{base_protocol}:{src}"
        else:
            js_url = urljoin(base_url, src)
        
        js_filename = _generate_stable_filename(js_url, 'js')
        local_js_path = os.path.join(root_js_dir, js_filename)
        
        if not os.path.exists(local_js_path):
            logger.debug(f"Скачиваю JS: {js_url} -> {js_filename}")
            if not download_file(js_url, local_js_path, session):
                script.decompose()
                continue
        else:
            logger.debug(f"JS файл уже существует: {js_filename}")
        
        # Очищаем JS файл от ссылок на SkillFactory (как новый, так и существующий)
        try:
            with open(local_js_path, 'r', encoding='utf-8') as f:
                js_content = f.read()
            
            cleaned_js_content = _clean_js_content(js_content)
            
            # Сохраняем очищенный контент только если он изменился
            if cleaned_js_content != js_content:
                with open(local_js_path, 'w', encoding='utf-8') as f:
                    f.write(cleaned_js_content)
                logger.debug(f"JS файл очищен от ссылок на SkillFactory: {js_filename}")
        except Exception as e:
            logger.warning(f"Не удалось очистить JS файл {js_filename}: {e}")
        
        script['src'] = os.path.relpath(local_js_path, os.path.dirname(lesson_file_path)).replace("\\", "/")
    
    return str(soup)

def download_images_and_documents(base_url, html_content, lesson_path, session):
    soup = BeautifulSoup(html_content, 'html.parser')
    lesson_dir = os.path.dirname(lesson_path)
    images_dir = os.path.join(lesson_dir, "images")
    docs_dir = os.path.join(lesson_dir, "documents")
    os.makedirs(images_dir, exist_ok=True)
    os.makedirs(docs_dir, exist_ok=True)
    
    for img in soup.find_all('img', src=True):
        src = img.get('src', '')
        if not src: continue
        if src.startswith('data:'): continue
        
        try:
            # Обработка протокол-относительных URL (начинающихся с //)
            if src.startswith('//'):
                # Извлекаем протокол из base_url
                base_protocol = urlparse(base_url).scheme or 'https'
                img_url = f"{base_protocol}:{src}"
            else:
                img_url = urljoin(base_url, src)
            
            img_filename = None
            
            # Специальная обработка для asset-v1 ссылок (может быть в пути URL)
            if 'asset-v1:' in img_url:
                # Ищем паттерн asset-v1 в любом месте URL
                asset_match = re.search(r'asset-v1:([^/]+)\+([^/]+)\+([^/]+)\+type@asset\+block[/@]([^/&\s]+)', img_url)
                if asset_match:
                    org, course, run, block_id = asset_match.groups()
                    
                    # Декодируем block_id и очищаем от специальных символов
                    decoded_block_id = unquote(block_id)
                    # Заменяем + и @ на _ для корректного имени файла
                    clean_block_id = decoded_block_id.replace('+', '_').replace('@', '_')
                    img_filename = sanitize_filename(clean_block_id)
                    
                    # Если нет расширения, добавляем .png по умолчанию
                    if not any(img_filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg']):
                        img_filename += '.png'
                    
                    logger.debug(f"Asset изображение: {decoded_block_id} -> {img_filename}")
                    
                    # Пробуем разные URL для скачивания
                    urls_to_try = [
                        img_url,  # Используем исходный URL
                        f"https://apps.skillfactory.ru/asset-v1:{org}+{course}+{run}+type@asset+block@{quote(block_id)}",
                        f"https://lms-cdn.skillfactory.ru/asset-v1:{org}+{course}+{run}+type@asset+block@{quote(block_id)}",
                        f"https://lms.skillfactory.ru/asset-v1:{org}+{course}+{run}+type@asset+block@{quote(block_id)}"
                    ]
                    
                    logger.info(f"Ищу рабочий URL для изображения {decoded_block_id}, пробую {len(urls_to_try)} вариантов")
                    
                    working_url = None
                    for url_try in urls_to_try:
                        try:
                            logger.debug(f"Проверяю URL: {url_try}")
                            res = session.head(url_try, timeout=10, allow_redirects=True)
                            if res.status_code == 200:
                                content_type = res.headers.get('content-type', '').lower()
                                # Проверяем, что это действительно изображение, а не HTML страница
                                if content_type.startswith('image/') or 'image' in content_type:
                                    logger.info(f"Найден рабочий URL для asset изображения: {url_try}")
                                    working_url = url_try
                                    break
                                else:
                                    logger.debug(f"URL вернул неправильный Content-Type: {content_type}")
                            else:
                                logger.debug(f"URL вернул статус {res.status_code}")
                        except requests.RequestException as e:
                            logger.debug(f"Ошибка при проверке URL {url_try}: {e}")
                            continue
                    
                    if working_url: 
                        img_url = working_url
                    else:
                        logger.warning(f"Не найден рабочий URL для asset изображения: {decoded_block_id}")
                else:
                    logger.warning(f"Не удалось извлечь данные из asset-v1 URL: {img_url}")
            else:
                # Обычное изображение - создаем читаемое имя файла
                original_filename = sanitize_filename(os.path.basename(unquote(urlparse(img_url).path)))
                
                # Специальная обработка для известных CDN сервисов с хешированными именами
                is_cdn_hash_url = any(domain in img_url for domain in [
                    'googleusercontent.com',
                    'googleapis.com', 
                    'gstatic.com',
                    'amazonaws.com',
                    'cloudfront.net',
                    'imgur.com',
                    'i.imgur.com'
                ])
                
                # Если имя файла слишком длинное (больше 50 символов) или выглядит как хеш,
                # или это CDN с хешированными именами - создаем более читаемое имя
                if (not original_filename or 
                    len(original_filename) > 50 or 
                    is_cdn_hash_url or
                    (len(original_filename) > 30 and not any(char in original_filename for char in ['_', '-', ' ', '.']))):
                    
                    # Пытаемся определить расширение из Content-Type
                    try:
                        head_response = session.head(img_url, timeout=10, allow_redirects=True)
                        content_type = head_response.headers.get('content-type', '').lower()
                        
                        if 'image/png' in content_type:
                            extension = 'png'
                        elif 'image/jpeg' in content_type or 'image/jpg' in content_type:
                            extension = 'jpg'
                        elif 'image/gif' in content_type:
                            extension = 'gif'
                        elif 'image/webp' in content_type:
                            extension = 'webp'
                        elif 'image/svg' in content_type:
                            extension = 'svg'
                        else:
                            extension = 'png'  # По умолчанию
                            
                    except Exception:
                        # Если не удалось определить, пытаемся извлечь из оригинального имени
                        if '.' in original_filename:
                            extension = original_filename.split('.')[-1].lower()
                            if extension not in ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg']:
                                extension = 'png'
                        else:
                            extension = 'png'
                    
                    # Создаем читаемое имя на основе URL с хешем для уникальности
                    img_filename = _generate_stable_filename(img_url, extension)
                    
                    if is_cdn_hash_url:
                        logger.debug(f"CDN изображение с хешированным именем: {img_url[:50]}... -> {img_filename}")
                    else:
                        logger.debug(f"Длинное имя изображения заменено: {original_filename[:30]}... -> {img_filename}")
                else:
                    # Имя файла нормальное, используем как есть
                    img_filename = original_filename
                    
                    # Добавляем расширение если его нет
                    if not any(img_filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg']):
                        img_filename += '.png'
            
            if not img_filename: 
                logger.warning(f"Не удалось определить имя файла для изображения: {src}")
                continue
            
            local_img_path = os.path.join(images_dir, img_filename)
            
            # Скачиваем изображение
            if download_file(img_url, local_img_path, session):
                # Обновляем src на относительный путь с корректным именем файла
                relative_path = os.path.relpath(local_img_path, lesson_dir).replace(os.sep, '/')
                img['src'] = relative_path
                logger.debug(f"Изображение сохранено: {img_filename}")
            else:
                logger.warning(f"Не удалось скачать изображение: {img_url}")
                
        except Exception as e:
            logger.error(f"Ошибка при обработке изображения {src}: {e}")
    
    # Обработка документов
    doc_exts = ['.pdf', '.zip', '.rar', '.docx', '.xlsx', '.pptx']
    for a in soup.find_all('a', href=True):
        href = a.get('href')
        if href and any(href.lower().endswith(ext) for ext in doc_exts):
            try:
                # Обработка протокол-относительных URL (начинающихся с //)
                if href.startswith('//'):
                    # Извлекаем протокол из base_url
                    base_protocol = urlparse(base_url).scheme or 'https'
                    doc_url = f"{base_protocol}:{href}"
                else:
                    doc_url = urljoin(base_url, href)
                
                doc_filename = None
                
                # Специальная обработка для asset-v1 ссылок документов
                if 'asset-v1:' in doc_url:
                    asset_match = re.search(r'asset-v1:([^/]+)\+([^/]+)\+([^/]+)\+type@asset\+block[/@]([^/&\s]+)', doc_url)
                    if asset_match:
                        org, course, run, block_id = asset_match.groups()
                        
                        # Декодируем block_id и очищаем от специальных символов
                        decoded_block_id = unquote(block_id)
                        clean_block_id = decoded_block_id.replace('+', '_').replace('@', '_')
                        doc_filename = sanitize_filename(clean_block_id)
                        
                        # Если нет расширения, пытаемся определить его
                        if not any(doc_filename.lower().endswith(ext) for ext in doc_exts):
                            # Пробуем определить тип по запросу HEAD
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
                else:
                    # Обычный документ
                    doc_filename = sanitize_filename(os.path.basename(unquote(urlparse(doc_url).path)))
                
                if not doc_filename: 
                    continue
                
                local_doc_path = os.path.join(docs_dir, doc_filename)
                
                if download_file(doc_url, local_doc_path, session):
                    # Обновляем href на относительный путь
                    relative_path = os.path.relpath(local_doc_path, lesson_dir).replace(os.sep, '/')
                    a['href'] = relative_path
                    logger.debug(f"Документ сохранен: {doc_filename}")
                    
            except Exception as e:
                logger.error(f"Ошибка при обработке документа {href}: {e}")
    
    return str(soup) 

def download_notebooks_and_update_html(base_url, html_content, lesson_path, session):
    """
    Скачивает Jupyter ноутбуки (.ipynb) и обновляет ссылки на локальные файлы.
    Также обрабатывает ссылки на Google Colab.
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    lesson_dir = os.path.dirname(lesson_path)
    notebooks_dir = os.path.join(lesson_dir, "notebooks")
    os.makedirs(notebooks_dir, exist_ok=True)
    
    logger.info("Начинаю поиск и скачивание ноутбуков...")
    
    all_links = soup.find_all('a', href=True)
    notebooks_processed = 0
    
    for link in all_links:
        href = link.get('href')
        if not href:
            continue
            
        try:
            # Определяем, является ли ссылка ноутбуком
            is_notebook = False
            notebook_url = None
            notebook_filename = None
            
            # 1. Прямые ссылки на .ipynb файлы
            if href.lower().endswith('.ipynb'):
                is_notebook = True
                # Обработка протокол-относительных URL
                if href.startswith('//'):
                    base_protocol = urlparse(base_url).scheme or 'https'
                    notebook_url = f"{base_protocol}:{href}"
                else:
                    notebook_url = urljoin(base_url, href)
                    
                # Определяем имя файла
                if 'asset-v1:' in notebook_url:
                    # Обработка asset-v1 ноутбуков SkillFactory
                    asset_match = re.search(r'asset-v1:([^/]+)\+([^/]+)\+([^/]+)\+type@asset\+block[/@]([^/&\s]+)', notebook_url)
                    if asset_match:
                        org, course, run, block_id = asset_match.groups()
                        decoded_block_id = unquote(block_id)
                        notebook_filename = sanitize_filename(decoded_block_id)
                        
                        if not notebook_filename.lower().endswith('.ipynb'):
                            notebook_filename += '.ipynb'
                            
                        logger.debug(f"Asset ноутбук: {decoded_block_id} -> {notebook_filename}")
                        
                        # Пробуем разные URL для скачивания
                        urls_to_try = [
                            notebook_url,  # Исходный URL
                            f"https://apps.skillfactory.ru/asset-v1:{org}+{course}+{run}+type@asset+block@{quote(block_id)}",
                            f"https://lms-cdn.skillfactory.ru/asset-v1:{org}+{course}+{run}+type@asset+block@{quote(block_id)}",
                            f"https://lms.skillfactory.ru/asset-v1:{org}+{course}+{run}+type@asset+block@{quote(block_id)}"
                        ]
                        
                        logger.info(f"Ищу рабочий URL для ноутбука {decoded_block_id}")
                        
                        # Проверяем рабочий URL
                        working_url = None
                        for url_try in urls_to_try:
                            try:
                                res = session.head(url_try, timeout=10, allow_redirects=True)
                                if res.status_code == 200:
                                    content_type = res.headers.get('content-type', '').lower()
                                    # Проверяем, что это JSON/ноутбук, а не HTML
                                    if ('application/json' in content_type or 
                                        'text/plain' in content_type or
                                        'application/octet-stream' in content_type):
                                        logger.info(f"Найден рабочий URL для ноутбука: {url_try}")
                                        working_url = url_try
                                        break
                                    else:
                                        logger.debug(f"URL вернул неправильный Content-Type: {content_type}")
                                else:
                                    logger.debug(f"URL вернул статус {res.status_code}")
                            except Exception as e:
                                logger.debug(f"Ошибка при проверке URL {url_try}: {e}")
                                continue
                        
                        if working_url:
                            notebook_url = working_url
                        else:
                            logger.warning(f"Не найден рабочий URL для ноутбука: {decoded_block_id}")
                            continue
                else:
                    # Обычный ноутбук
                    notebook_filename = sanitize_filename(os.path.basename(unquote(urlparse(notebook_url).path)))
                    
            # 2. Ссылки на Google Colab
            elif 'colab.research.google.com' in href:
                is_notebook = True
                logger.info(f"Найдена ссылка на Google Colab: {href}")
                
                # Для Colab создаем информационный файл вместо скачивания
                colab_info = f"""# Google Colab Notebook

Эта ссылка ведет на Google Colab ноутбук:
{href}

## Как открыть:
1. Скопируйте ссылку выше
2. Откройте ее в браузере
3. Войдите в свой аккаунт Google
4. Сохраните копию ноутбука в свой Google Drive (Файл -> Сохранить копию на Диске)

## Примечание:
Google Colab ноутбуки требуют интернет-соединения и аккаунт Google для работы.
Для офлайн использования скачайте .ipynb файл из Colab (Файл -> Скачать -> Скачать .ipynb).
"""
                
                # Создаем имя файла на основе текста ссылки или URL
                link_text = link.get_text(strip=True) or "google_colab_notebook"
                notebook_filename = sanitize_filename(f"{link_text}_colab_info.md")
                
                local_notebook_path = os.path.join(notebooks_dir, notebook_filename)
                
                try:
                    with open(local_notebook_path, 'w', encoding='utf-8') as f:
                        f.write(colab_info)
                    
                    # Обновляем ссылку на локальный файл
                    relative_path = os.path.relpath(local_notebook_path, lesson_dir).replace(os.sep, '/')
                    link['href'] = relative_path
                    
                    # Добавляем подсказку в title
                    link['title'] = f"Google Colab ноутбук (см. {notebook_filename} для инструкций)"
                    
                    logger.info(f"✔ Создана информация о Google Colab: {notebook_filename}")
                    notebooks_processed += 1
                    
                except Exception as e:
                    logger.error(f"Ошибка при создании информации о Colab: {e}")
                
                continue  # Переходим к следующей ссылке
            
            # 3. Кнопки скачивания ноутбуков (определяем по тексту и download атрибуту)
            elif (link.get('download') and 
                  (link.get('download', '').lower().endswith('.ipynb') or 
                   'notebook' in link.get_text().lower() or 
                   'ноутбук' in link.get_text().lower())):
                is_notebook = True
                
                # Получаем имя файла из download атрибута
                download_name = link.get('download')
                if download_name and download_name.lower().endswith('.ipynb'):
                    notebook_filename = sanitize_filename(download_name)
                else:
                    # Генерируем имя на основе текста ссылки
                    link_text = link.get_text(strip=True) or "notebook"
                    notebook_filename = sanitize_filename(f"{link_text}.ipynb")
                
                # Обработка протокол-относительных URL
                if href.startswith('//'):
                    base_protocol = urlparse(base_url).scheme or 'https'
                    notebook_url = f"{base_protocol}:{href}"
                else:
                    notebook_url = urljoin(base_url, href)
                    
                logger.info(f"Найдена кнопка скачивания ноутбука: {link.get_text(strip=True)}")
            
            # Если это ноутбук, скачиваем его
            if is_notebook and notebook_url and notebook_filename:
                notebooks_processed += 1
                logger.info(f"Обрабатываю ноутбук #{notebooks_processed}: {notebook_filename}")
                
                local_notebook_path = os.path.join(notebooks_dir, notebook_filename)
                
                # Проверяем, существует ли уже файл
                if os.path.exists(local_notebook_path):
                    logger.debug(f"Ноутбук уже существует: {notebook_filename}")
                else:
                    # Скачиваем ноутбук
                    logger.info(f"Скачиваю ноутбук: {notebook_url}")
                    if not download_file(notebook_url, local_notebook_path, session):
                        logger.warning(f"Не удалось скачать ноутбук: {notebook_url}")
                        continue
                
                # Обновляем ссылку на локальный файл
                relative_path = os.path.relpath(local_notebook_path, lesson_dir).replace(os.sep, '/')
                old_href = link['href']
                link['href'] = relative_path
                
                # Убеждаемся, что download атрибут указывает на правильное имя файла
                link['download'] = notebook_filename
                
                # Добавляем информацию в title
                link['title'] = f"Jupyter Notebook: {notebook_filename}"
                
                logger.info(f"✔ Ноутбук скачан и ссылка обновлена: {old_href} -> {relative_path}")
                
        except Exception as e:
            logger.error(f"Ошибка при обработке ноутбука {href}: {e}")
    
    if notebooks_processed > 0:
        logger.info(f"✔ Обработано {notebooks_processed} ноутбуков")
    else:
        logger.debug("Ноутбуки не найдены")
    
    return str(soup)

def _embed_local_video(html_content, relative_video_path):
    """Старая функция для замены одного видео - оставлена для совместимости"""
    soup = BeautifulSoup(html_content, 'html.parser')
    iframe_tag = soup.find('iframe', src=re.compile(r'kinescope\.io/embed'))
    if iframe_tag:
        video_tag = soup.new_tag("video", controls=True, width="100%", preload="metadata")
        video_tag['src'] = relative_video_path.replace(os.sep, "/")
        iframe_tag.replace_with(video_tag)
    return str(soup)

def process_and_save_html(html_content, block_data, parent_block, all_blocks, lesson_path, base_url, session, downloaded_videos=None, relative_video_path=None, output_dir=None):
    # Поддерживаем оба варианта для обратной совместимости
    if downloaded_videos:
        html_content = _embed_local_videos(html_content, downloaded_videos)
    elif relative_video_path:
        html_content = _embed_local_video(html_content, relative_video_path)
    html_content = _clean_html(html_content)
    assets_dir = os.path.join(output_dir, '_assets')
    css_dir = os.path.join(assets_dir, 'css')
    js_dir = os.path.join(assets_dir, 'js')
    html_content = download_css_and_update_html(base_url, html_content, lesson_path, css_dir, session)
    html_content = download_js_and_update_html(base_url, html_content, lesson_path, js_dir, session)
    html_content = download_images_and_documents(base_url, html_content, lesson_path, session)
    html_content = download_notebooks_and_update_html(base_url, html_content, lesson_path, session)
    soup = BeautifulSoup(html_content, 'html.parser')
    
    final_soup = _rewire_navigation_links(soup, block_data.get('id'), parent_block, all_blocks)

    # Оставляем только простое правило для скрытия спиннеров и MathJax preview
    if final_soup.head:
        hide_spinner_style = final_soup.new_tag('style')
        hide_spinner_style.string = """
        .xblock-student_view-loading, .spinner-border { display: none !important; }
        .MathJax_Preview { display: none !important; visibility: hidden !important; height: 0 !important; width: 0 !important; margin: 0 !important; padding: 0 !important; }
        span[class*="MathJax_Preview"] { display: none !important; }
        .MJX_Assistive_MathML { display: none !important; visibility: hidden !important; height: 0 !important; width: 0 !important; margin: 0 !important; padding: 0 !important; }
        span[role="presentation"][class*="MJX"] { display: none !important; }
        span.MathJax_SVG[role="presentation"] { display: none !important; }
        .MathJax_SVG { display: inline-block !important; }
        """
        final_soup.head.append(hide_spinner_style)
    
    with open(lesson_path, 'w', encoding='utf-8') as f:
        f.write(str(final_soup)) 