import json
import os
import logging
from datetime import datetime
from pathvalidate import sanitize_filename

logger = logging.getLogger(__name__)

class ProgressTracker:
    """Класс для отслеживания прогресса скачивания курса"""
    
    def __init__(self, course_name, output_dir):
        self.course_name = course_name
        self.output_dir = output_dir
        self.progress_file = os.path.join(output_dir, f"{sanitize_filename(course_name)}_progress.json")
        self.progress_data = self._load_progress()
    
    def _load_progress(self):
        """Загружает прогресс из JSON файла"""
        if os.path.exists(self.progress_file):
            try:
                with open(self.progress_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                logger.info(f"Загружен прогресс скачивания: {len(data.get('completed', {}))} завершенных элементов")
                return data
            except Exception as e:
                logger.warning(f"Не удалось загрузить файл прогресса: {e}")
        
        # Создаем новый файл прогресса
        new_progress = {
            "course_name": self.course_name,
            "created_at": datetime.now().isoformat(),
            "last_updated": datetime.now().isoformat(),
            "completed": {},  # block_id -> info
            "failed": {},     # block_id -> error_info
            "skipped": {},    # block_id -> reason
            "statistics": {
                "total_processed": 0,
                "total_size_mb": 0,
                "videos_downloaded": 0,
                "html_files_created": 0
            }
        }
        
        # Сразу сохраняем новый файл прогресса
        try:
            with open(self.progress_file, 'w', encoding='utf-8') as f:
                json.dump(new_progress, f, ensure_ascii=False, indent=2)
            logger.info(f"Создан новый файл прогресса: {self.progress_file}")
        except Exception as e:
            logger.error(f"Не удалось создать файл прогресса: {e}")
        
        return new_progress
    
    def _save_progress(self):
        """Сохраняет прогресс в JSON файл"""
        try:
            self.progress_data["last_updated"] = datetime.now().isoformat()
            with open(self.progress_file, 'w', encoding='utf-8') as f:
                json.dump(self.progress_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Не удалось сохранить прогресс: {e}")
    
    def _file_exists_and_valid(self, file_path):
        """Проверяет, существует ли файл и имеет ли он разумный размер"""
        if not file_path or not os.path.exists(file_path):
            return False
        
        try:
            # Проверяем размер файла
            file_size = os.path.getsize(file_path)
            if file_size < 100:  # Файл меньше 100 байт подозрителен
                return False
            
            # Для HTML файлов проверяем содержимое
            if file_path.endswith('.html'):
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read(500)  # Читаем первые 500 символов
                    # Проверяем, что это действительно HTML с контентом
                    if '<html' not in content.lower() and '<body' not in content.lower():
                        return False
                    # Проверяем, что это не пустая страница или страница с ошибкой
                    if len(content.strip()) < 50:
                        return False
            
            return True
        except Exception as e:
            logger.debug(f"Ошибка при проверке файла {file_path}: {e}")
            return False
    
    def validate_and_cleanup_progress(self):
        """Проверяет реальное состояние файловой системы и обновляет прогресс"""
        logger.info("Проверяю соответствие прогресса реальному состоянию файловой системы...")
        
        cleaned_completed = {}
        cleaned_failed = {}
        updated_stats = {
            "total_processed": 0,
            "total_size_mb": 0,
            "videos_downloaded": 0,
            "html_files_created": 0
        }
        
        # Проверяем завершенные элементы
        for block_id, info in self.progress_data["completed"].items():
            file_path = info.get('file_path')
            
            if self._file_exists_and_valid(file_path):
                # Файл существует и валиден - оставляем в прогрессе
                cleaned_completed[block_id] = info
                
                # Обновляем статистику
                updated_stats["total_processed"] += 1
                updated_stats["total_size_mb"] += info.get('file_size_mb', 0)
                if info.get('has_video', False):
                    updated_stats["videos_downloaded"] += 1
                if file_path and file_path.endswith('.html'):
                    updated_stats["html_files_created"] += 1
                    
                logger.debug(f"✅ Файл существует: {info.get('display_name', block_id)}")
            else:
                # Файл не существует или поврежден - удаляем из завершенных
                logger.warning(f"❌ Файл не найден или поврежден, удаляю из прогресса: {info.get('display_name', block_id)} -> {file_path}")
        
        # Проверяем неудачные элементы - оставляем как есть, но можем очистить старые
        for block_id, info in self.progress_data["failed"].items():
            # Оставляем неудачные элементы для повторной попытки
            cleaned_failed[block_id] = info
        
        # Обновляем данные прогресса
        removed_count = len(self.progress_data["completed"]) - len(cleaned_completed)
        if removed_count > 0:
            logger.info(f"🧹 Удалено из прогресса {removed_count} элементов с отсутствующими файлами")
            
            self.progress_data["completed"] = cleaned_completed
            self.progress_data["failed"] = cleaned_failed
            self.progress_data["statistics"] = updated_stats
            
            # Добавляем запись о валидации
            self.progress_data["last_validated"] = datetime.now().isoformat()
            
            self._save_progress()
            logger.info("✅ Прогресс обновлен в соответствии с файловой системой")
        else:
            logger.info("✅ Все файлы из прогресса найдены, обновление не требуется")
    
    def is_completed(self, block_id):
        """Проверяет, завершен ли блок"""
        return block_id in self.progress_data["completed"]
    
    def mark_completed(self, block_id, block_data, file_path=None, file_size_mb=0, has_video=False):
        """Отмечает блок как завершенный"""
        self.progress_data["completed"][block_id] = {
            "display_name": block_data.get('display_name', 'Без названия'),
            "type": block_data.get('type', 'unknown'),
            "completed_at": datetime.now().isoformat(),
            "file_path": file_path,
            "file_size_mb": round(file_size_mb, 2),
            "has_video": has_video
        }
        
        # Обновляем статистику
        stats = self.progress_data["statistics"]
        stats["total_processed"] += 1
        stats["total_size_mb"] += file_size_mb
        if has_video:
            stats["videos_downloaded"] += 1
        if file_path and file_path.endswith('.html'):
            stats["html_files_created"] += 1
        
        self._save_progress()
        logger.debug(f"Отмечен как завершенный: {block_data.get('display_name', block_id)}")
    
    def mark_failed(self, block_id, block_data, error_message):
        """Отмечает блок как неудачный"""
        self.progress_data["failed"][block_id] = {
            "display_name": block_data.get('display_name', 'Без названия'),
            "type": block_data.get('type', 'unknown'),
            "failed_at": datetime.now().isoformat(),
            "error": str(error_message)
        }
        self._save_progress()
        logger.warning(f"Отмечен как неудачный: {block_data.get('display_name', block_id)} - {error_message}")
    
    def mark_skipped(self, block_id, block_data, reason):
        """Отмечает блок как пропущенный"""
        self.progress_data["skipped"][block_id] = {
            "display_name": block_data.get('display_name', 'Без названия'),
            "type": block_data.get('type', 'unknown'),
            "skipped_at": datetime.now().isoformat(),
            "reason": reason
        }
        self._save_progress()
        logger.debug(f"Отмечен как пропущенный: {block_data.get('display_name', block_id)} - {reason}")
    
    def get_statistics(self):
        """Возвращает статистику прогресса"""
        stats = self.progress_data["statistics"].copy()
        stats.update({
            "completed_count": len(self.progress_data["completed"]),
            "failed_count": len(self.progress_data["failed"]),
            "skipped_count": len(self.progress_data["skipped"])
        })
        return stats
    
    def print_progress_table(self):
        """Выводит таблицу прогресса в консоль"""
        print("\n" + "="*80)
        print(f"ПРОГРЕСС СКАЧИВАНИЯ: {self.course_name}")
        print("="*80)
        
        stats = self.get_statistics()
        print(f"📊 Общая статистика:")
        print(f"   • Завершено: {stats['completed_count']}")
        print(f"   • Неудачно: {stats['failed_count']}")
        print(f"   • Пропущено: {stats['skipped_count']}")
        print(f"   • Общий размер: {stats['total_size_mb']:.1f} МБ")
        print(f"   • HTML файлов: {stats['html_files_created']}")
        print(f"   • Видео скачано: {stats['videos_downloaded']}")
        
        if self.progress_data["completed"]:
            print(f"\n✅ Завершенные элементы ({len(self.progress_data['completed'])}):")
            print("-" * 80)
            for block_id, info in self.progress_data["completed"].items():
                size_info = f" ({info['file_size_mb']:.1f} МБ)" if info['file_size_mb'] > 0 else ""
                video_info = " 🎥" if info['has_video'] else ""
                print(f"   {info['display_name'][:60]:<60} {size_info:<10} {video_info}")
        
        if self.progress_data["failed"]:
            print(f"\n❌ Неудачные элементы ({len(self.progress_data['failed'])}):")
            print("-" * 80)
            for block_id, info in self.progress_data["failed"].items():
                print(f"   {info['display_name'][:50]:<50} | {info['error'][:25]}")
        
        if self.progress_data["skipped"]:
            print(f"\n⏭️  Пропущенные элементы ({len(self.progress_data['skipped'])}):")
            print("-" * 80)
            for block_id, info in self.progress_data["skipped"].items():
                print(f"   {info['display_name'][:50]:<50} | {info['reason'][:25]}")
        
        print("="*80)
    
    def get_resume_point(self, all_blocks):
        """Находит точку для возобновления скачивания"""
        completed_ids = set(self.progress_data["completed"].keys())
        failed_ids = set(self.progress_data["failed"].keys())
        
        # Находим все блоки, которые еще не обработаны
        remaining_blocks = []
        for block_id, block_data in all_blocks.items():
            if block_id not in completed_ids and block_id not in failed_ids:
                remaining_blocks.append((block_id, block_data))
        
        return remaining_blocks
    
    def should_skip_block(self, block_id, force_overwrite=False):
        """Определяет, нужно ли пропустить блок с проверкой файловой системы"""
        if force_overwrite:
            return False
        
        # Проверяем, есть ли блок в завершенных
        if block_id not in self.progress_data["completed"]:
            return False
        
        # Получаем информацию о файле
        block_info = self.progress_data["completed"][block_id]
        file_path = block_info.get('file_path')
        
        # Проверяем, существует ли файл реально
        if self._file_exists_and_valid(file_path):
            return True  # Файл существует и валиден - пропускаем
        else:
            # Файл не существует или поврежден - удаляем из завершенных
            logger.warning(f"🔄 Файл не найден, повторно скачиваю: {block_info.get('display_name', block_id)} -> {file_path}")
            del self.progress_data["completed"][block_id]
            
            # Обновляем статистику
            stats = self.progress_data["statistics"]
            stats["total_processed"] = max(0, stats["total_processed"] - 1)
            stats["total_size_mb"] = max(0, stats["total_size_mb"] - block_info.get('file_size_mb', 0))
            if block_info.get('has_video', False):
                stats["videos_downloaded"] = max(0, stats["videos_downloaded"] - 1)
            if file_path and file_path.endswith('.html'):
                stats["html_files_created"] = max(0, stats["html_files_created"] - 1)
            
            self._save_progress()
            return False  # Не пропускаем - нужно скачать заново
    
    def cleanup_progress_file(self):
        """Удаляет файл прогресса (для полного перезапуска)"""
        if os.path.exists(self.progress_file):
            os.remove(self.progress_file)
            logger.info("Файл прогресса удален") 