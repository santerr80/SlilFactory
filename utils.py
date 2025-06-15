import logging
import os
import requests
from tqdm import tqdm

logger = logging.getLogger(__name__)

def download_file(url, filepath, session):
    """
    Скачивает файл по URL и сохраняет его по указанному пути, используя сессию.
    Показывает прогресс-бар с помощью tqdm.
    """
    try:
        response = session.get(url, stream=True, timeout=30)
        response.raise_for_status()

        content_type = response.headers.get('content-type', '').lower()
        if 'html' in content_type or 'json' in content_type:
             logger.warning(f"Сервер вернул {content_type} вместо файла для URL: {url}")
             return False

        total_size = int(response.headers.get('content-length', 0))
        with open(filepath, 'wb') as f, tqdm(
            total=total_size, unit='iB', unit_scale=True,
            desc=os.path.basename(filepath), leave=False
        ) as pbar:
            for data in response.iter_content(chunk_size=8192):
                f.write(data)
                pbar.update(len(data))

        logger.debug(f"Файл '{os.path.basename(filepath)}' успешно скачан.")
        return True
    except requests.RequestException as e:
        logger.error(f"Ошибка при скачивании файла {url}: {e}")
        return False