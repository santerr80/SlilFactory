#!/usr/bin/env python3
"""
Утилита для управления прогрессом скачивания курсов SkillFactory
"""

import argparse
import os
import json
import sys
from pathvalidate import sanitize_filename
from progress_tracker import ProgressTracker

def list_progress_files(directory="."):
    """Находит все файлы прогресса в директории"""
    progress_files = []
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith('_progress.json'):
                progress_files.append(os.path.join(root, file))
    return progress_files

def show_progress_summary(progress_file):
    """Показывает краткую сводку по файлу прогресса"""
    try:
        with open(progress_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        course_name = data.get('course_name', 'Неизвестный курс')
        stats = data.get('statistics', {})
        completed = len(data.get('completed', {}))
        failed = len(data.get('failed', {}))
        skipped = len(data.get('skipped', {}))
        
        print(f"\n📚 {course_name}")
        print(f"   📁 {progress_file}")
        print(f"   ✅ Завершено: {completed}")
        print(f"   ❌ Неудачно: {failed}")
        print(f"   ⏭️  Пропущено: {skipped}")
        print(f"   📊 Размер: {stats.get('total_size_mb', 0):.1f} МБ")
        print(f"   🎥 Видео: {stats.get('videos_downloaded', 0)}")
        print(f"   📄 HTML: {stats.get('html_files_created', 0)}")
        
    except Exception as e:
        print(f"❌ Ошибка при чтении {progress_file}: {e}")

def show_detailed_progress(progress_file):
    """Показывает детальный прогресс"""
    try:
        with open(progress_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        course_name = data.get('course_name', 'Неизвестный курс')
        
        # Создаем временный трекер для использования метода print_progress_table
        temp_dir = os.path.dirname(progress_file)
        tracker = ProgressTracker(course_name, temp_dir)
        tracker.progress_data = data
        tracker.print_progress_table()
        
    except Exception as e:
        print(f"❌ Ошибка при чтении {progress_file}: {e}")

def clean_progress_file(progress_file):
    """Удаляет файл прогресса"""
    try:
        os.remove(progress_file)
        print(f"✅ Файл прогресса удален: {progress_file}")
    except Exception as e:
        print(f"❌ Ошибка при удалении {progress_file}: {e}")

def reset_failed_items(progress_file):
    """Сбрасывает неудачные элементы для повторной попытки"""
    try:
        with open(progress_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        failed_count = len(data.get('failed', {}))
        if failed_count == 0:
            print("Неудачных элементов не найдено.")
            return
        
        # Очищаем неудачные элементы
        data['failed'] = {}
        
        with open(progress_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        print(f"✅ Сброшено {failed_count} неудачных элементов в {progress_file}")
        
    except Exception as e:
        print(f"❌ Ошибка при сбросе неудачных элементов: {e}")

def main():
    parser = argparse.ArgumentParser(description="Управление прогрессом скачивания курсов SkillFactory")
    parser.add_argument('--list', '-l', action='store_true', help="Показать все файлы прогресса")
    parser.add_argument('--show', '-s', help="Показать детальный прогресс для файла")
    parser.add_argument('--summary', help="Показать краткую сводку для файла")
    parser.add_argument('--clean', '-c', help="Удалить файл прогресса")
    parser.add_argument('--reset-failed', '-r', help="Сбросить неудачные элементы для повторной попытки")
    parser.add_argument('--directory', '-d', default='.', help="Директория для поиска файлов прогресса")
    
    args = parser.parse_args()
    
    if args.list:
        print("🔍 Поиск файлов прогресса...")
        progress_files = list_progress_files(args.directory)
        
        if not progress_files:
            print("Файлы прогресса не найдены.")
            return
        
        print(f"\nНайдено файлов прогресса: {len(progress_files)}")
        print("=" * 80)
        
        for pf in progress_files:
            show_progress_summary(pf)
        
        print("=" * 80)
        print("\nИспользуйте --show <файл> для детального просмотра")
        print("Используйте --clean <файл> для удаления файла прогресса")
        
    elif args.show:
        if not os.path.exists(args.show):
            print(f"❌ Файл не найден: {args.show}")
            return
        show_detailed_progress(args.show)
        
    elif args.summary:
        if not os.path.exists(args.summary):
            print(f"❌ Файл не найден: {args.summary}")
            return
        show_progress_summary(args.summary)
        
    elif args.clean:
        if not os.path.exists(args.clean):
            print(f"❌ Файл не найден: {args.clean}")
            return
        
        confirm = input(f"Вы уверены, что хотите удалить {args.clean}? (y/N): ")
        if confirm.lower() in ['y', 'yes']:
            clean_progress_file(args.clean)
        else:
            print("Отменено.")
            
    elif args.reset_failed:
        if not os.path.exists(args.reset_failed):
            print(f"❌ Файл не найден: {args.reset_failed}")
            return
        
        confirm = input(f"Сбросить неудачные элементы в {args.reset_failed}? (y/N): ")
        if confirm.lower() in ['y', 'yes']:
            reset_failed_items(args.reset_failed)
        else:
            print("Отменено.")
            
    else:
        parser.print_help()

if __name__ == '__main__':
    main() 