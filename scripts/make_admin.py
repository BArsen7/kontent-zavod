"""
CLI-скрипт для выдачи прав администратора панели пользователю.

Использование:
    python scripts/make_admin.py user@example.com
    python scripts/make_admin.py user@example.com --revoke   # снять права

Скрипт находит пользователя по email в БД и устанавливает is_admin = True.
Если пользователь не найден — выводит ошибку и завершается с кодом 1.
"""

import argparse
import logging
import sys
from pathlib import Path

# Добавляем корень проекта в PATH для импортов
sys.path.insert(0, str(Path(__file__).parent.parent))

from database import SessionLocal, init_db
from models import User

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("make_admin")


def parse_args() -> argparse.Namespace:
    """Парсинг аргументов командной строки."""
    parser = argparse.ArgumentParser(
        description="Выдать (или снять) права администратора панели у пользователя по email",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "email",
        type=str,
        help="Email пользователя (совпадает с регистром при регистрации)",
    )
    parser.add_argument(
        "--revoke",
        action="store_true",
        help="Снять права администратора вместо выдачи",
    )
    return parser.parse_args()


def main() -> int:
    """Основная функция скрипта. Возвращает код выхода процесса."""
    args = parse_args()
    email = args.email.strip().lower()

    if not email:
        logger.error("Email не может быть пустым")
        return 1

    try:
        # Гарантируем актуальную схему БД (колонка users.is_admin)
        init_db()

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.email == email).first()
            if user is None:
                # Резервный поиск без приведения регистра (на случай legacy-данных)
                user = db.query(User).filter(User.email == args.email.strip()).first()

            if user is None:
                logger.error(
                    f"Пользователь с email '{args.email}' не найден в базе данных. "
                    "Проверьте email (он должен точно совпадать с указанным при регистрации)."
                )
                print(f"\n❌ Пользователь '{args.email}' не найден.")
                return 1

            new_value = not args.revoke
            if user.is_admin == new_value:
                status = "уже является" if new_value else "уже не является"
                print(f"ℹ️  Пользователь {user.email} ({status}) администратором. Изменений нет.")
                return 0

            user.is_admin = new_value
            db.commit()
            db.refresh(user)

            if new_value:
                print(f"✅ Пользователь {user.email} (ID: {user.id}) теперь АДМИНИСТРАТОР панели.")
                logger.info(f"Выданы права администратора: id={user.id}, email={user.email}")
            else:
                print(f"✅ Права администратора сняты у пользователя {user.email} (ID: {user.id}).")
                logger.info(f"Сняты права администратора: id={user.id}, email={user.email}")
            return 0
        finally:
            db.close()

    except Exception as e:
        logger.exception(f"Критическая ошибка при выполнении скрипта: {e}")
        print(f"\n❌ Ошибка: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
