"""
VK Bot для управления через сообщения сообщества.
Использует vk_api с LongPoll для обработки входящих сообщений.
"""
import logging
import time
import threading
from typing import Optional

import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType

from config import settings
from database import SessionLocal
from models import Post
from services.content_service import generate_weekly_pack

logger = logging.getLogger(__name__)


class VKBot:
    """VK бот для управления контентом через сообщения сообщества."""

    def __init__(self, token: str, group_id: int):
        """
        Инициализация VK бота.

        Args:
            token: Токен сообщества VK API.
            group_id: ID группы VK.
        """
        self.token = token
        self.group_id = group_id
        self._vk_session: Optional[vk_api.VkApi] = None
        self._vk: Optional[vk_api.VkApiMethod] = None
        self._longpoll: Optional[VkBotLongPoll] = None
        self._running = False

        logger.info(f"Инициализация VK бота для группы {group_id}")

    @property
    def vk_session(self) -> vk_api.VkApi:
        """Ленивая инициализация сессии VK."""
        if self._vk_session is None:
            self._vk_session = vk_api.VkApi(token=self.token)
            try:
                self._vk_session.auth()
                logger.debug("VK сессия успешно аутентифицирована")
            except vk_api.AuthError as e:
                logger.error(f"Ошибка аутентификации VK: {e}")
                raise
        return self._vk_session

    @property
    def vk(self):
        """Получение API объекта."""
        if self._vk is None:
            self._vk = self.vk_session.get_api()
        return self._vk

    @property
    def longpoll(self) -> VkBotLongPoll:
        """Ленивая инициализация LongPoll."""
        if self._longpoll is None:
            self._longpoll = VkBotLongPoll(self.vk_session, self.group_id)
            logger.debug("LongPoll инициализирован")
        return self._longpoll

    def _send_message(self, peer_id: int, message: str, keyboard: Optional[dict] = None):
        """
        Отправка сообщения пользователю.

        Args:
            peer_id: ID получателя (peer_id).
            message: Текст сообщения.
            keyboard: JSON-клавиатура (опционально).
        """
        params = {
            "peer_id": peer_id,
            "message": message,
            "random_id": 0,  # Будет заменён на уникальное значение
        }

        if keyboard:
            params["keyboard"] = keyboard

        try:
            # Генерируем случайный random_id
            import random
            params["random_id"] = random.randint(0, 2**31 - 1)
            self.vk.messages.send(**params)
            logger.debug(f"Сообщение отправлено peer_id={peer_id}: {message[:50]}...")
        except vk_api.exceptions.ApiError as e:
            logger.error(f"Ошибка отправки сообщения: {e}")
        except Exception as e:
            logger.error(f"Неожиданная ошибка при отправке сообщения: {e}")

    def _get_main_keyboard(self) -> dict:
        """Возвращает основную клавиатуру с кнопками."""
        keyboard = {
            "one_time": False,
            "inline": False,
            "buttons": [
                [
                    {
                        "action": {
                            "type": "text",
                            "payload": None,
                            "label": "Сгенерировать пак"
                        },
                        "color": "primary"
                    }
                ],
                [
                    {
                        "action": {
                            "type": "text",
                            "payload": None,
                            "label": "Статус"
                        },
                        "color": "secondary"
                    },
                    {
                        "action": {
                            "type": "text",
                            "payload": None,
                            "label": "Одобрить"
                        },
                        "color": "positive"
                    }
                ]
            ]
        }
        return keyboard

    def _get_approval_keyboard(self, post_id: int) -> dict:
        """
        Возвращает клавиатуру для одобрения поста.

        Args:
            post_id: ID поста для одобрения.
        """
        keyboard = {
            "one_time": True,
            "inline": False,
            "buttons": [
                [
                    {
                        "action": {
                            "type": "text",
                            "payload": {"post_id": post_id, "action": "approve"},
                            "label": "✅ Одобрить"
                        },
                        "color": "positive"
                    },
                    {
                        "action": {
                            "type": "text",
                            "payload": {"post_id": post_id, "action": "reject"},
                            "label": "❌ Отклонить"
                        },
                        "color": "negative"
                    }
                ]
            ]
        }
        return keyboard

    def _handle_start(self, peer_id: int):
        """Обработка команды /start или 'Старт'."""
        welcome_message = (
            "👋 Привет! Я бот для управления автоконтентом.\n\n"
            "📋 Доступные команды:\n"
            "• Старт или /start — Показать это сообщение\n"
            "• Статус или /status — Показать статистику постов\n"
            "• Сгенерировать или /generate — Создать пак постов на неделю\n"
            "• Одобрить или /approve — Показать последний draft и одобрить\n\n"
            "Выберите действие из меню ниже:"
        )
        self._send_message(peer_id, welcome_message, self._get_main_keyboard())
        logger.info(f"Отправлено приветствие пользователю peer_id={peer_id}")

    def _handle_status(self, peer_id: int):
        """Обработка команды /status или 'Статус'."""
        db = SessionLocal()
        try:
            draft_count = db.query(Post).filter(Post.status == "draft").count()
            approved_count = db.query(Post).filter(Post.status == "approved").count()
            published_count = db.query(Post).filter(Post.status == "published").count()

            status_message = (
                f"📊 Статус постов в базе данных:\n\n"
                f"📝 Черновики (draft): {draft_count}\n"
                f"✅ Одобрено (approved): {approved_count}\n"
                f"📢 Опубликовано (published): {published_count}\n\n"
                f"Всего постов: {draft_count + approved_count + published_count}"
            )
            self._send_message(peer_id, status_message)
            logger.info(f"Отправлен статус пользователю peer_id={peer_id}")
        except Exception as e:
            error_message = f"❌ Ошибка получения статуса: {e}"
            self._send_message(peer_id, error_message)
            logger.error(f"Ошибка в _handle_status: {e}")
        finally:
            db.close()

    def _handle_generate(self, peer_id: int):
        """Обработка команды /generate или 'Сгенерировать'."""
        self._send_message(peer_id, "⏳ Запускаю генерацию недельного пакета... Пожалуйста, подождите.")
        logger.info(f"Запрошена генерация пользователем peer_id={peer_id}")

        def run_generation():
            db = SessionLocal()
            try:
                result = generate_weekly_pack(niche="3d_cookies", db=db)
                response_message = (
                    f"✅ Генерация завершена!\n\n"
                    f"Создано постов: {len(result)}\n\n"
                    f"Посты сохранены в базу данных со статусом 'draft'.\n"
                    f"Используйте команду 'Одобрить' для просмотра и одобрения постов."
                )
                self._send_message(peer_id, response_message)
                logger.info(f"Генерация завершена: {len(result)} постов")
            except Exception as e:
                error_message = f"❌ Ошибка генерации: {e}"
                self._send_message(peer_id, error_message)
                logger.error(f"Ошибка генерации: {e}")
            finally:
                db.close()

        # Запускаем генерацию в отдельном потоке, чтобы не блокировать бота
        thread = threading.Thread(target=run_generation, daemon=True)
        thread.start()

    def _handle_approve(self, peer_id: int):
        """Обработка команды /approve или 'Одобрить'."""
        db = SessionLocal()
        try:
            # Получаем последний draft пост
            last_draft = db.query(Post).filter(Post.status == "draft").order_by(Post.id.desc()).first()

            if not last_draft:
                self._send_message(peer_id, "📭 Нет черновиков для одобрения.\nСначала создайте посты командой 'Сгенерировать'.")
                logger.info(f"Нет draft постов для пользователя peer_id={peer_id}")
                return

            # Формируем сообщение с превью поста
            preview_text = last_draft.text_draft or last_draft.text_final or "(нет текста)"
            if len(preview_text) > 400:
                preview_text = preview_text[:400] + "..."

            approve_message = (
                f"📝 Последний черновик (ID: {last_draft.id}):\n\n"
                f"Тип: {last_draft.post_type}\n"
                f"Тема: {last_draft.topic}\n\n"
                f"Текст:\n{preview_text}\n\n"
                f"Изображение: {'✅' if last_draft.image_url else '❌'}\n\n"
                f"Одобрить этот пост для публикации?"
            )

            keyboard = self._get_approval_keyboard(last_draft.id)
            self._send_message(peer_id, approve_message, keyboard)
            logger.info(f"Показан draft пост ID={last_draft.id} пользователю peer_id={peer_id}")

        except Exception as e:
            error_message = f"❌ Ошибка получения черновика: {e}"
            self._send_message(peer_id, error_message)
            logger.error(f"Ошибка в _handle_approve: {e}")
        finally:
            db.close()

    def _handle_approve_action(self, peer_id: int, post_id: int, action: str):
        """
        Обработка действия одобрения/отклонения поста.

        Args:
            peer_id: ID получателя.
            post_id: ID поста.
            action: Действие ('approve' или 'reject').
        """
        db = SessionLocal()
        try:
            post = db.query(Post).filter(Post.id == post_id).first()

            if not post:
                self._send_message(peer_id, f"❌ Пост ID {post_id} не найден.")
                return

            if action == "approve":
                if post.status != "draft":
                    self._send_message(peer_id, f"⚠️ Пост уже имеет статус '{post.status}'. Одобрять можно только черновики.")
                    return

                post.status = "approved"
                db.commit()
                response = f"✅ Пост ID {post_id} одобрен и готов к публикации!"
                logger.info(f"Пост ID {post_id} одобрен пользователем peer_id={peer_id}")

            elif action == "reject":
                # Для reject просто информируем, статус не меняем (можно добавить свой статус rejected)
                response = f"❌ Пост ID {post_id} отклонён. Остаётся в черновиках."
                logger.info(f"Пост ID {post_id} отклонён пользователем peer_id={peer_id}")
            else:
                response = f"⚠️ Неизвестное действие: {action}"

            self._send_message(peer_id, response)

        except Exception as e:
            error_message = f"❌ Ошибка обработки действия: {e}"
            self._send_message(peer_id, error_message)
            logger.error(f"Ошибка в _handle_approve_action: {e}")
            db.rollback()
        finally:
            db.close()

    def _process_event(self, event):
        """
        Обработка события LongPoll.

        Args:
            event: Событие от LongPoll.
        """
        try:
            event_type = event.type

            if event_type == VkBotEventType.MESSAGE_NEW:
                # Получаем данные сообщения
                message_data = event.object.message
                peer_id = message_data.get("peer_id")
                text = message_data.get("text", "").strip().lower()
                from_id = message_data.get("from_id")

                # Игнорируем сообщения от самого бота
                if from_id == self.group_id * -1:  # Group ID в from_id отрицательный
                    return

                logger.info(f"Получено сообщение от peer_id={peer_id}, from_id={from_id}, текст: {text}")

                # Обработка команд
                if text in ["старт", "/start"]:
                    self._handle_start(peer_id)
                elif text in ["статус", "/status"]:
                    self._handle_status(peer_id)
                elif text in ["сгенерировать", "/generate"]:
                    self._handle_generate(peer_id)
                elif text in ["одобрить", "/approve"]:
                    self._handle_approve(peer_id)
                else:
                    # Проверяем, не является ли сообщение ответом на клавиатуру одобрения
                    payload = message_data.get("payload")
                    if payload:
                        import json
                        try:
                            payload_data = json.loads(payload) if isinstance(payload, str) else payload
                            if isinstance(payload_data, dict):
                                post_id = payload_data.get("post_id")
                                action = payload_data.get("action")
                                if post_id and action in ["approve", "reject"]:
                                    self._handle_approve_action(peer_id, post_id, action)
                                    return
                        except (json.JSONDecodeError, TypeError):
                            pass

                    # Неизвестная команда
                    unknown_message = (
                        "❓ Не распознал команду.\n\n"
                        "Доступные команды:\n"
                        "• Старт — Приветствие и список команд\n"
                        "• Статус — Статистика постов\n"
                        "• Сгенерировать — Создать пак на неделю\n"
                        "• Одобрить — Показать последний черновик"
                    )
                    self._send_message(peer_id, unknown_message)

            else:
                logger.debug(f"Получено событие типа {event_type}, игнорируем")

        except Exception as e:
            logger.error(f"Ошибка обработки события: {e}")

    def run(self):
        """
        Основной цикл обработки событий LongPoll.
        Автоматический reconnect при обрыве соединения.
        """
        self._running = True
        logger.info("Запуск VK бота...")

        while self._running:
            try:
                # Инициализируем LongPoll если ещё не создан
                lp = self.longpoll

                logger.info("LongPoll начал прослушивание событий...")

                for event in lp.listen():
                    if not self._running:
                        break
                    self._process_event(event)

            except vk_api.exceptions.LongPollError as e:
                logger.warning(f"LongPoll ошибка: {e}. Переподключение через 5 секунд...")
                self._longpoll = None  # Сбрасываем LongPoll для пересоздания
                time.sleep(5)
            except vk_api.exceptions.ApiError as e:
                logger.error(f"VK API ошибка: {e}. Переподключение через 5 секунд...")
                self._vk_session = None  # Сбрасываем сессию для пересоздания
                self._longpoll = None
                time.sleep(5)
            except Exception as e:
                logger.error(f"Неожиданная ошибка в цикле LongPoll: {e}. Переподключение через 5 секунд...")
                self._vk_session = None
                self._longpoll = None
                time.sleep(5)

        logger.info("VK бот остановлен")

    def stop(self):
        """Остановка бота."""
        logger.info("Остановка VK бота...")
        self._running = False


# Глобальная переменная для экземпляра бота
_vk_bot: Optional[VKBot] = None


def run_vk_bot():
    """
    Запуск VK бота в отдельном потоке.
    Бот будет работать фоновно, не блокируя основное приложение.
    """
    global _vk_bot

    if not settings.VK_TOKEN:
        logger.warning("VK_TOKEN не настроен. VK бот не будет запущен.")
        return

    if not settings.VK_GROUP_ID:
        logger.warning("VK_GROUP_ID не настроен. VK бот не будет запущен.")
        return

    if _vk_bot is not None:
        logger.warning("VK бот уже запущен")
        return

    _vk_bot = VKBot(token=settings.VK_TOKEN, group_id=settings.VK_GROUP_ID)

    # Запускаем бота в отдельном потоке
    bot_thread = threading.Thread(target=_vk_bot.run, name="VKBotThread", daemon=True)
    bot_thread.start()

    logger.info("VK бот запущен в фоновом потоке")


def get_vk_bot() -> Optional[VKBot]:
    """Получение экземпляра бота."""
    return _vk_bot


def stop_vk_bot():
    """Остановка VK бота."""
    global _vk_bot
    if _vk_bot:
        _vk_bot.stop()
        _vk_bot = None
