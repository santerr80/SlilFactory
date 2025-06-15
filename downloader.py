# downloader.py

import logging
import os
import re
import shutil
import subprocess
import time
import json
import xmltodict
import requests
from bs4 import BeautifulSoup
from tqdm import tqdm
from pathvalidate import sanitize_filename

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from selenium.common.exceptions import TimeoutException
from urllib.parse import urljoin, urlparse

from html_processor import process_and_save_html
from config import IGNORE_KEYWORDS_IN_TITLES
from progress_tracker import ProgressTracker

logger = logging.getLogger(__name__)

class KinescopeDownloader:
    """
    Класс для скачивания видео с Kinescope, использующего технологию MPEG-DASH.
    Исправленная версия, основанная на рабочем коде из старой версии.
    """
    def __init__(self, session, output_dir, referer, debug=False):
        self.session = session
        self.output_dir = output_dir
        self.referer = referer
        self.debug = debug
    
    def download_video_by_id(self, video_id, video_name):
        self.video_id = video_id
        self.video_name = sanitize_filename(video_name)
        self.base_url = "https://kinescope.io"
        self.output_path = os.path.join(self.output_dir, f"{self.video_name}.mp4")
        return self._download()

    def _get_media_chunk(self, url, byte_range):
        """Скачивает один чанк данных по URL и диапазону байт."""
        headers = {'Range': f"bytes={byte_range}"}
        try:
            response = self.session.get(url, headers=headers, stream=True, timeout=60)
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
        
        with tqdm(total=total_segments, desc=f"Скачивание ({stream_type_rus})", unit="seg", leave=False) as pbar:
            for seg_info in segments:
                media_range = seg_info['@mediaRange']
                media_url_part = seg_info.get('@media')
                
                # Если у сегмента свой файл, используем его, иначе - базовый URL потока
                final_media_url = urljoin(stream_base_url, media_url_part) if media_url_part else stream_base_url
                
                stream_data += self._get_media_chunk(final_media_url, media_range)
                pbar.update(1)
        
        logger.info(f"✔ Поток {stream_type_rus} успешно загружен.")
        return stream_data

    def _download(self):
        """Основной метод для скачивания и сборки видео."""
        
        # Явное сообщение о начале скачивания конкретного видео
        logger.info(f"Начинаю полное скачивание и сборку видео: '{self.video_name}.mp4'")

        # 0. Проверяем наличие ffmpeg
        if not shutil.which("ffmpeg"):
            logger.critical("Утилита 'ffmpeg' не найдена в системе. Она необходима для сборки видео.")
            logger.critical("Пожалуйста, установите ffmpeg и убедитесь, что путь к нему добавлен в системную переменную PATH.")
            return False

        # 1. Получаем и парсим MPD манифест (используем прямой путь, как в старой версии)
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
        if not video_data: 
            logger.error("Не удалось загрузить видеопоток.")
            return False

        # 3. Ищем и скачиваем аудиопоток
        audio_set = next((s for s in adaptation_sets if s.get('@mimeType', '').startswith('audio/')), None)
        if not audio_set:
            logger.error("Аудиопоток не найден в манифесте.")
            return False
        
        audio_representation = audio_set['Representation']
        if isinstance(audio_representation, list):
            audio_representation = audio_representation[0]
            
        audio_data = self._download_stream(audio_representation, 'audio')
        if not audio_data: 
            logger.error("Не удалось загрузить аудиопоток.")
            return False

        # 4. Сохраняем временные файлы и собираем финальное видео
        temp_video_path = os.path.join(self.output_dir, f"{self.video_id}.video")
        temp_audio_path = os.path.join(self.output_dir, f"{self.video_id}.audio")

        try:
            logger.info("Сохраняю временные аудио/видео файлы...")
            with open(temp_video_path, 'wb') as f: 
                f.write(video_data)
            with open(temp_audio_path, 'wb') as f: 
                f.write(audio_data)

            logger.info(f"Собираю финальный файл '{self.video_name}.mp4' с помощью ffmpeg...")
            # Команда для сборки без перекодирования
            convert_cmd = [
                "ffmpeg", "-y",
                "-i", temp_video_path,
                "-i", temp_audio_path,
                "-c", "copy",
                "-bsf:a", "aac_adtstoasc",
                self.output_path
            ]
            
            # Запускаем ffmpeg, скрывая его стандартный вывод и указывая кодировку
            result = subprocess.run(
                convert_cmd,
                check=True,
                capture_output=True,
                encoding='utf-8',
                errors='ignore'
            )
            if self.debug:
                logger.debug(f"ffmpeg stdout: {result.stdout}")
                logger.debug(f"ffmpeg stderr: {result.stderr}")
            
            logger.info(f"✔ Видео '{self.video_name}' успешно сохранено: {self.output_path}")
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
            if os.path.exists(temp_video_path): 
                os.remove(temp_video_path)
            if os.path.exists(temp_audio_path): 
                os.remove(temp_audio_path)

def process_content_block(driver, session, block_data, all_blocks, parent_block, html_filepath, output_dir, no_videos, progress_tracker=None):
    content_url = block_data.get('lms_web_url')
    display_name = block_data.get('display_name', 'Без названия')
    if not content_url:
        logger.warning(f"У блока '{display_name}' отсутствует lms_web_url. Пропускаю.")
        return
    logger.info(f"Обрабатываю страницу: '{display_name}' ({content_url})")
    try:
        driver.get(content_url)
        
        # Ждем загрузки контента
        kinescope_selector_str = "iframe[src*='kinescope.io']"
        unit_iframe_selector_str = "iframe#unit-iframe"
        xblock_selector_str = "div.xblock"
        combined_wait_selector = f"{kinescope_selector_str}, {unit_iframe_selector_str}, {xblock_selector_str}"
        
        WebDriverWait(driver, 40).until(EC.presence_of_element_located((By.CSS_SELECTOR, combined_wait_selector)))
        logger.info("✔ Контент урока обнаружен.")
        
        # Дополнительное ожидание загрузки интерактивных элементов
        logger.debug("Ожидание загрузки интерактивных элементов и заданий...")
        try:
            # Ждем исчезновения основных спиннеров и загрузчиков
            WebDriverWait(driver, 30).until_not(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".xblock-student_view-loading"))
            )
            logger.debug("✔ Основные загрузчики исчезли.")
        except:
            logger.debug("Основные загрузчики не найдены или не исчезли в течение 30 секунд.")
        
        # Дополнительная пауза для AJAX-загрузки интерактивных элементов
        logger.debug("Дополнительная пауза 5 секунд для загрузки интерактивного контента...")
        time.sleep(5)
        
        # Проверяем, есть ли еще активные спиннеры и ждем их завершения
        try:
            spinner_selectors = [
                ".spinner-border", 
                ".loading-spinner", 
                ".fa-spinner",
                "[class*='spinner']",
                "[class*='loading']"
            ]
            
            for selector in spinner_selectors:
                try:
                    WebDriverWait(driver, 10).until_not(
                        EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                    )
                    logger.debug(f"✔ Спиннеры {selector} исчезли.")
                except:
                    pass  # Спиннеры этого типа не найдены или не исчезли
                    
        except Exception as e:
            logger.debug(f"Не удалось дождаться исчезновения всех спиннеров: {e}")
        
        # Пауза для загрузки скриптов и удаление спиннеров
        logger.debug("Пауза 2 секунды, чтобы дать скриптам страницы отработать...")
        time.sleep(2)
        try:
            logger.debug("Принудительное удаление индикаторов загрузки через JS...")
            js_command = """
            // Удаляем основные классы загрузчиков
            const loadingSelectors = [
                '.xblock-student_view-loading',
                '.spinner-border',
                '.loading-spinner',
                '.fa-spinner',
                '[class*="spinner"]',
                '[class*="loading"]',
                '.d-flex.justify-content-center',
                '.text-center > .spinner-border'
            ];
            
            loadingSelectors.forEach(selector => {
                document.querySelectorAll(selector).forEach(el => {
                    // Проверяем, является ли элемент спиннером по содержимому
                    const isSpinner = el.classList.toString().includes('spinner') || 
                                     el.classList.toString().includes('loading') ||
                                     el.innerHTML.includes('spinner') ||
                                     (el.children.length === 0 && el.textContent.trim() === '');
                    
                    if (isSpinner) {
                        const parent = el.parentElement;
                        el.remove();
                        
                        // Если родительский элемент остался пустым, удаляем и его
                        if (parent && parent.children.length === 0 && parent.textContent.trim() === '') {
                            // Проверяем, что это не важный контейнер
                            if (!parent.classList.contains('xblock') && 
                                !parent.classList.contains('vertical') &&
                                !parent.id.includes('problem')) {
                                parent.remove();
                            }
                        }
                    }
                });
            });
            
            // Дополнительно ищем по атрибутам
            document.querySelectorAll('[role="status"], [aria-label*="loading"], [aria-label*="Loading"]').forEach(el => {
                el.remove();
            });
            """
            driver.execute_script(js_command)
            logger.debug("Попытка удаления индикаторов завершена.")
        except Exception as e:
            logger.warning(f"Не удалось удалить индикаторы загрузки через JS: {e}")

        # Ожидание завершения рендеринга MathJax и удаление дублирующихся формул
        try:
            logger.debug("Ожидание завершения рендеринга MathJax...")
            # Устанавливаем больший таймаут для async script
            driver.set_script_timeout(60)
            
            # Ждем, пока MathJax завершит рендеринг
            mathjax_wait_script = """
            const callback = arguments[arguments.length - 1];
            
            function waitForMathJax() {
                if (typeof MathJax !== 'undefined') {
                    if (MathJax.Hub && MathJax.Hub.Queue) {
                        // MathJax v2
                        MathJax.Hub.Queue(() => {
                            setTimeout(callback, 500); // Дополнительная пауза после завершения
                        });
                    } else if (MathJax.startup && MathJax.startup.promise) {
                        // MathJax v3
                        MathJax.startup.promise.then(() => {
                            setTimeout(callback, 500);
                        }).catch(() => {
                            setTimeout(callback, 500);
                        });
                    } else {
                        // MathJax найден, но API не распознан
                        setTimeout(callback, 2000);
                    }
                } else {
                    // MathJax не найден, продолжаем без ожидания
                    setTimeout(callback, 1000);
                }
            }
            
            waitForMathJax();
            """
            driver.execute_async_script(mathjax_wait_script)
            
            # Удаляем временные preview элементы MathJax и очищаем пустые контейнеры
            logger.debug("Удаление временных preview элементов MathJax и очистка пустых контейнеров...")
            cleanup_mathjax_script = """
            console.log('=== Начало очистки MathJax ===');
            
            // Сначала найдем все проблемные элементы для диагностики
            const allMathJaxElements = document.querySelectorAll('[id*="MathJax"], [id*="MJX"], [class*="MathJax"], [class*="mjx"]');
            console.log('Найдено MathJax элементов:', allMathJaxElements.length);
            
            // 1. ТОЛЬКО удаляем временные preview элементы с префиксом MJXp-
            const previewElements = document.querySelectorAll('[id^="MJXp-"]');
            console.log('Найдено preview элементов для удаления:', previewElements.length);
            
            previewElements.forEach((el, index) => {
                console.log(`Удаляем preview элемент ${index + 1}:`, el.id, el.tagName);
                el.remove();
            });
            
            // 2. Удаляем элементы с классами preview
            document.querySelectorAll('.MJXp-preview, .mjx-preview').forEach(el => {
                console.log('Удаляем preview класс:', el.tagName, el.className);
                el.remove();
            });
            
            // 3. НОВОЕ: Удаляем пустые блоки MathJax_Preview (как на скриншоте)
            const mathJaxPreviewElements = document.querySelectorAll('.MathJax_Preview');
            console.log('Найдено MathJax_Preview элементов для удаления:', mathJaxPreviewElements.length);
            
            mathJaxPreviewElements.forEach((el, index) => {
                console.log(`Удаляем MathJax_Preview элемент ${index + 1}:`, el.tagName, el.className);
                el.remove();
            });
            
            // 4. Показываем финальные элементы MathJax (если они скрыты)
            const finalElements = document.querySelectorAll('[id^="MathJax-Element-"]');
            console.log('Найдено финальных MathJax элементов:', finalElements.length);
            
            finalElements.forEach(el => {
                if (el.style.display === 'none') {
                    el.style.display = '';
                    console.log('Показали скрытый элемент:', el.id);
                }
            });
            
            // 5. Убеждаемся, что MathJax_SVG элементы видимы
            const svgElements = document.querySelectorAll('.MathJax_SVG');
            console.log('Найдено MathJax_SVG элементов:', svgElements.length);
            
            svgElements.forEach(el => {
                if (el.style.display === 'none') {
                    el.style.display = 'inline-block';
                    console.log('Показали MathJax_SVG элемент');
                }
            });
            
            console.log('=== Очистка MathJax завершена (консервативный режим) ===');
            """
            driver.execute_script(cleanup_mathjax_script)
            logger.debug("Очистка MathJax preview элементов завершена.")
            
        except Exception as e:
            logger.warning(f"Не удалось дождаться завершения MathJax или очистить preview элементы: {e}")
            # В случае ошибки, хотя бы попытаемся базовую очистку
            try:
                basic_cleanup = """
                document.querySelectorAll('[id^="MJXp-"]').forEach(el => el.remove());
                """
                driver.execute_script(basic_cleanup)
                logger.debug("Выполнена базовая очистка MathJax preview элементов.")
            except:
                pass

        # Синхронизация cookies
        for cookie in driver.get_cookies():
            session.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'])
        final_page_url = driver.current_url
        
        # Улучшенный поиск и скачивание ВСЕХ видео на странице
        downloaded_videos = []
        if not no_videos:
            # Собираем все iframe с kinescope на странице
            kinescope_iframes = []
            
            try:
                # Сначала ищем iframe с kinescope в основном контенте
                iframe_elements = driver.find_elements(By.CSS_SELECTOR, kinescope_selector_str)
                for iframe_element in iframe_elements:
                    iframe_src = iframe_element.get_attribute('src')
                    if iframe_src:
                        kinescope_iframes.append(iframe_src)
                        logger.debug(f"Найден Kinescope iframe в основном контенте: {iframe_src}")
            except:
                logger.debug("Kinescope iframe не найден в основном контенте")
            
            # Также проверяем в unit-iframe
            try:
                driver.switch_to.frame(driver.find_element(By.CSS_SELECTOR, unit_iframe_selector_str))
                iframe_elements = driver.find_elements(By.CSS_SELECTOR, kinescope_selector_str)
                for iframe_element in iframe_elements:
                    iframe_src = iframe_element.get_attribute('src')
                    if iframe_src and iframe_src not in kinescope_iframes:
                        kinescope_iframes.append(iframe_src)
                        logger.debug(f"Найден Kinescope iframe в unit-iframe: {iframe_src}")
            except:
                logger.debug("Kinescope iframe не найден в unit-iframe")
            finally:
                driver.switch_to.default_content()
            
            # Скачиваем каждое найденное видео
            if kinescope_iframes:
                logger.info(f"✔ Обнаружено {len(kinescope_iframes)} Kinescope видео. Начинаю скачивание...")
                
                for i, iframe_src in enumerate(kinescope_iframes):
                    try:
                        video_id_match = re.search(r'kinescope\.io/(?:embed/)?([a-zA-Z0-9]+)', iframe_src)
                        if not video_id_match: 
                            logger.warning(f"Не удалось извлечь ID видео из iframe src: {iframe_src}")
                            continue
                        
                        video_id = video_id_match.group(1)
                        # Создаем уникальное имя файла для каждого видео
                        if len(kinescope_iframes) == 1:
                            video_name = display_name
                        else:
                            video_name = f"{display_name}_video_{i+1}"
                        
                        video_filename = f"{sanitize_filename(video_name)}.mp4"
                        video_path = os.path.join(os.path.dirname(html_filepath), video_filename)
                        
                        # Проверяем, существует ли уже видеофайл
                        if os.path.exists(video_path):
                            file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
                            logger.info(f"✔ Видео '{video_filename}' уже существует ({file_size_mb:.1f} МБ). Пропускаю скачивание.")
                            downloaded_videos.append({
                                'iframe_src': iframe_src,
                                'video_id': video_id, 
                                'filename': video_filename
                            })
                            continue
                        
                        logger.info(f"Извлечен video_id: {video_id} для видео '{video_name}'")
                        
                        downloader = KinescopeDownloader(
                            session=session, 
                            output_dir=os.path.dirname(html_filepath), 
                            referer=final_page_url
                        )
                        
                        if downloader.download_video_by_id(video_id, video_name):
                            downloaded_videos.append({
                                'iframe_src': iframe_src,
                                'video_id': video_id, 
                                'filename': video_filename
                            })
                            logger.info(f"✔ Видео успешно скачано: {video_filename}")
                        else:
                            logger.error(f"Не удалось скачать видео для '{video_name}' (ID: {video_id})")
                            
                    except Exception as e:
                        logger.error(f"Ошибка при загрузке видео из {iframe_src}: {e}", exc_info=True)
            else:
                logger.debug(f"Видео для '{display_name}' не найдено.")

        # Переключаемся обратно в основной контент
        driver.switch_to.default_content()
        page_soup = BeautifulSoup(driver.page_source, 'html.parser')
        
        # Встраиваем контент из unit-iframe если есть
        try:
            unit_iframe_element = page_soup.find('iframe', {'id': 'unit-iframe'})
            if unit_iframe_element:
                driver.switch_to.frame(driver.find_element(By.ID, 'unit-iframe'))
                iframe_soup = BeautifulSoup(driver.page_source, 'html.parser')
                driver.switch_to.default_content()
                if page_soup.head and iframe_soup.head:
                    for tag in iframe_soup.head.find_all(['link', 'style']):
                        page_soup.head.append(tag)
                unit_iframe_element.replace_with(*iframe_soup.body.contents)
        except Exception as e:
            logger.warning(f"Не удалось встроить контент из #unit-iframe: {e}")

        html_content = str(page_soup)
        
        process_and_save_html(
            html_content=html_content, 
            block_data=block_data, 
            parent_block=parent_block, 
            all_blocks=all_blocks, 
            lesson_path=html_filepath, 
            base_url=final_page_url, 
            session=session, 
            downloaded_videos=downloaded_videos,  # Передаем список всех скачанных видео
            output_dir=output_dir
        )
        logger.info(f"✔ Страница '{display_name}' полностью обработана и сохранена.")
        
        # Отслеживание прогресса
        if progress_tracker:
            try:
                file_size_mb = 0
                if os.path.exists(html_filepath):
                    file_size_mb = os.path.getsize(html_filepath) / (1024 * 1024)
                
                # Добавляем размер видео если есть
                if downloaded_videos:
                    for video in downloaded_videos:
                        video_path = os.path.join(os.path.dirname(html_filepath), video['filename'])
                        if os.path.exists(video_path):
                            file_size_mb += os.path.getsize(video_path) / (1024 * 1024)
                
                progress_tracker.mark_completed(
                    block_id=block_data.get('id'),
                    block_data=block_data,
                    file_path=html_filepath,
                    file_size_mb=file_size_mb,
                    has_video=bool(downloaded_videos)
                )
            except Exception as e:
                logger.warning(f"Не удалось обновить прогресс для '{display_name}': {e}")
        
    except Exception as e:
        logger.error(f"Критическая ошибка при обработке страницы '{display_name}': {e}", exc_info=True)
        
        # Отмечаем как неудачный в трекере прогресса
        if progress_tracker:
            try:
                progress_tracker.mark_failed(
                    block_id=block_data.get('id'),
                    block_data=block_data,
                    error_message=str(e)
                )
            except Exception as tracker_error:
                logger.warning(f"Не удалось обновить прогресс (ошибка) для '{display_name}': {tracker_error}")

def download_material(driver, session, block_id, all_blocks, current_path, output_dir, no_videos, force_overwrite, parent_block=None, progress_tracker=None):
    block_data = all_blocks.get(block_id)
    if not block_data: return
    display_name = block_data.get('display_name', 'Без названия')
    
    # Проверяем, нужно ли пропустить блок (уже завершен)
    if progress_tracker and progress_tracker.should_skip_block(block_id, force_overwrite):
        logger.info(f"Блок '{display_name}' уже завершен. Пропускаю.")
        if progress_tracker:
            progress_tracker.mark_skipped(block_id, block_data, "Уже завершен")
        return
    
    if any(keyword in display_name.lower() for keyword in IGNORE_KEYWORDS_IN_TITLES):
        logger.info(f"Пропускаю административный/вспомогательный раздел: '{display_name}'")
        if progress_tracker:
            progress_tracker.mark_skipped(block_id, block_data, "Административный раздел")
        return
    
    sanitized_name = sanitize_filename(display_name)
    block_type = block_data.get('type')
    if block_type in ['course', 'chapter', 'sequential']:
        # Специальная обработка для корневого блока курса - не создаем дополнительную папку
        # так как папка курса уже создана в main.py
        if block_type == 'course' and parent_block is None:
            # Это корневой блок курса, используем текущий путь без создания дополнительной папки
            new_path = current_path
        else:
            # Для всех остальных блоков создаем папку как обычно
            new_path = os.path.join(current_path, sanitized_name)
            os.makedirs(new_path, exist_ok=True)
        
        children = block_data.get('children', [])
        logger.info(f"Захожу в раздел: '{display_name}'")
        for child_id in children:
            download_material(driver, session, child_id, all_blocks, new_path, output_dir, no_videos, force_overwrite, parent_block=block_data, progress_tracker=progress_tracker)
    elif block_type == 'vertical':
        html_filepath = os.path.join(current_path, f"{sanitized_name}.html")
        if os.path.exists(html_filepath) and not force_overwrite:
            logger.info(f"Файл '{os.path.basename(html_filepath)}' уже существует. Пропускаю.")
            if progress_tracker:
                progress_tracker.mark_skipped(block_id, block_data, "Файл уже существует")
            return
        process_content_block(driver=driver, session=session, block_data=block_data, all_blocks=all_blocks, parent_block=parent_block, html_filepath=html_filepath, output_dir=output_dir, no_videos=no_videos, progress_tracker=progress_tracker)
    else:
        logger.debug(f"Пропущен блок '{display_name}' с типом: {block_type}")
        if progress_tracker:
            progress_tracker.mark_skipped(block_id, block_data, f"Неподдерживаемый тип: {block_type}")

def download_course_content(root_id, all_blocks, session, output_dir, no_videos, force_overwrite, course_name="Курс"):
    # Создаем трекер прогресса
    progress_tracker = ProgressTracker(course_name, output_dir)
    
    # Валидируем прогресс с файловой системой
    progress_tracker.validate_and_cleanup_progress()
    
    # Показываем текущий прогресс
    progress_tracker.print_progress_table()
    
    driver = None
    try:
        logger.info("Инициализация единого экземпляра браузера для скачивания...")
        options = webdriver.ChromeOptions()
        options.add_experimental_option("excludeSwitches", ["enable-logging"])
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.get("https://lms.skillfactory.ru/404")
        time.sleep(1)
        for cookie in session.cookies:
            driver.add_cookie({k: v for k, v in cookie.__dict__.items() if k != '_rest'})
        logger.info("Cookies сессии успешно переданы в браузер.")
        
        download_material(driver, session, root_id, all_blocks, output_dir, output_dir, no_videos, force_overwrite, parent_block=None, progress_tracker=progress_tracker)
        
        # Показываем финальную статистику
        logger.info("Скачивание завершено!")
        progress_tracker.print_progress_table()
        
    finally:
        if driver:
            driver.quit()