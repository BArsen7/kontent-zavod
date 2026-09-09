"""
Мастер-бот для управления множественными сообществами VK.
Позволяет регистрировать несколько сообществ с разными токенами
и управлять каждым через единый интерфейс.
"""
import logging
import threading
import time
from typing import Dict, Optional, List
from dataclasses import dataclass

import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType

from database import SessionLocal
from models import PlatformAccount, Post

logger = logging.getLogger(__name__)


@dataclass
class CommunityAccount:
    """Представление аккаунта сообщества."""
    id: int
    platform: str
    account_id: str
    access_token: str
    group_id: int
    
    def __post_init__(self):
        """Валидация после инициализации."""
        if self.platform != "vk":
            raise ValueError(f"Поддерживается только платформа 'vk', получено: {self.platform}")


class CommunityManager:
    """
    Менеджер для управления множественными сообществами VK.
    
    Мастер-бот получает токены от разных сообществ и через API
    управляет каждым по отдельности, не будучи привязанным к одному.
    """
    
    def __init__(self):
        """Инициализация менеджера сообществ."""
        self._communities: Dict[int, CommunityAccount] = {}
        self._vk_sessions: Dict[int, vk_api.VkApi] = {}
        self._longpolls: Dict[int, VkBotLongPoll] = {}
        self._running = False
        self._threads: Dict[int, threading.Thread] = {}
        
        logger.info("Инициализация CommunityManager")
    
    def load_communities_from_db(self) -> int:
        """
        Загружает сообщества из базы данных.
        
        Returns:
            Количество загруженных сообществ.
        """
        db = SessionLocal()
        try:
            accounts = db.query(PlatformAccount).filter(
                PlatformAccount.platform == "vk"
            ).all()
            
            loaded_count = 0
            for account in accounts:
                try:
                    community = CommunityAccount(
                        id=account.id,
                        platform=account.platform,
                        account_id=account.account_id,
                        access_token=account.access_token,
                        group_id=int(account.account_id)
                    )
                    self.register_community(community)
                    loaded_count += 1
                except Exception as e:
                    logger.error(f"Ошибка загрузки сообщества {account.account_id}: {e}")
            
            logger.info(f"Загружено {loaded_count} сообществ из БД")
            return loaded_count
        finally:
            db.close()
    
    def register_community(self, community: CommunityAccount) -> bool:
        """
        Регистрирует новое сообщество для управления.
        
        Args:
            community: Данные сообщества для регистрации.
            
        Returns:
            True если регистрация успешна, False иначе.
        """
        try:
            # Проверяем валидность токена
            vk_session = vk_api.VkApi(token=community.access_token)
            vk_session.auth()
            
            # Получаем информацию о группе для проверки
            vk = vk_session.get_api()
            groups_info = vk.groups.getById(group_id=community.group_id)
            
            if not groups_info or len(groups_info) == 0:
                logger.error(f"Группа {community.group_id} не найдена или недоступна")
                return False
            
            group_name = groups_info[0].get('name', 'Unknown')
            logger.info(f"Токен валиден для группы: {group_name} (ID: {community.group_id})")
            
            # Сохраняем в базу данных если ещё нет
            db = SessionLocal()
            try:
                existing = db.query(PlatformAccount).filter(
                    PlatformAccount.account_id == str(community.group_id)
                ).first()
                
                if not existing:
                    new_account = PlatformAccount(
                        platform=community.platform,
                        account_id=community.account_id,
                        access_token=community.access_token
                    )
                    db.add(new_account)
                    db.commit()
                    logger.info(f"Сообщество {community.group_id} сохранено в БД")
                else:
                    # Обновляем токен если изменился
                    if existing.access_token != community.access_token:
                        existing.access_token = community.access_token
                        db.commit()
                        logger.info(f"Токен сообщества {community.group_id} обновлён")
            finally:
                db.close()
            
            # Регистрируем в менеджере
            self._communities[community.group_id] = community
            self._vk_sessions[community.group_id] = vk_session
            
            logger.info(f"Сообщество {community.group_id} ({group_name}) зарегистрировано")
            
            # Если менеджер запущен, запускаем LongPoll для нового сообщества
            if self._running:
                self._start_community_listener(community.group_id)
            
            return True
            
        except vk_api.AuthError as e:
            logger.error(f"Ошибка аутентификации для сообщества {community.group_id}: {e}")
            return False
        except vk_api.exceptions.ApiError as e:
            logger.error(f"API ошибка при регистрации сообщества {community.group_id}: {e}")
            return False
        except Exception as e:
            logger.error(f"Неожиданная ошибка при регистрации сообщества {community.group_id}: {e}")
            return False
    
    def unregister_community(self, group_id: int) -> bool:
        """
        Удаляет сообщество из управления.
        
        Args:
            group_id: ID группы для удаления.
            
        Returns:
            True если удаление успешно, False иначе.
        """
        if group_id not in self._communities:
            logger.warning(f"Сообщество {group_id} не найдено в списке управляемых")
            return False
        
        # Останавливаем поток прослушивания
        if group_id in self._threads:
            self._stop_community_listener(group_id)
        
        # Удаляем из словарей
        self._communities.pop(group_id, None)
        self._vk_sessions.pop(group_id, None)
        self._longpolls.pop(group_id, None)
        
        # Удаляем из БД
        db = SessionLocal()
        try:
            account = db.query(PlatformAccount).filter(
                PlatformAccount.account_id == str(group_id)
            ).first()
            if account:
                db.delete(account)
                db.commit()
                logger.info(f"Сообщество {group_id} удалено из БД")
        finally:
            db.close()
        
        logger.info(f"Сообщество {group_id} удалено из управления")
        return True
    
    def get_community_list(self) -> List[Dict]:
        """
        Возвращает список всех управляемых сообществ.
        
        Returns:
            Список словарей с информацией о сообществах.
        """
        result = []
        for group_id, community in self._communities.items():
            result.append({
                "id": community.id,
                "group_id": community.group_id,
                "platform": community.platform,
                "account_id": community.account_id,
                "active": group_id in self._threads
            })
        return result
    
    def _get_vk_api(self, group_id: int):
        """
        Получает API объект для конкретного сообщества.
        
        Args:
            group_id: ID группы.
            
        Returns:
            API объект или None если сообщество не найдено.
        """
        if group_id not in self._vk_sessions:
            logger.error(f"Сообщество {group_id} не найдено")
            return None
        
        return self._vk_sessions[group_id].get_api()
    
    def publish_to_community(
        self,
        group_id: int,
        post_data: dict
    ) -> dict:
        """
        Публикует пост в конкретном сообществе.
        
        Args:
            group_id: ID группы для публикации.
            post_data: Данные поста (text, image_path).
            
        Returns:
            Результат публикации.
        """
        vk = self._get_vk_api(group_id)
        if not vk:
            return {
                "success": False,
                "error": f"Сообщество {group_id} не найдено"
            }
        
        try:
            text = post_data.get("text", "")
            image_path = post_data.get("image_path")
            
            if not text:
                return {
                    "success": False,
                    "error": "Текст поста обязателен"
                }
            
            attachments = []
            
            # Загрузка изображения если есть
            if image_path:
                from vk_api.upload import VkUpload
                upload = VkUpload(self._vk_sessions[group_id])
                
                try:
                    uploaded = upload.photo_wall(
                        photo=image_path,
                        group_id=group_id
                    )
                    
                    if uploaded and len(uploaded) > 0:
                        photo = uploaded[0]
                        owner_id = photo.get('owner_id')
                        photo_id = photo.get('id')
                        attachment = f"photo-{abs(owner_id)}_{photo_id}"
                        attachments.append(attachment)
                except Exception as e:
                    logger.warning(f"Не удалось загрузить фото: {e}")
            
            # Публикация на стене
            post_params = {
                "owner_id": -group_id,
                "message": text,
            }
            
            if attachments:
                post_params["attachments"] = ",".join(attachments)
            
            response = vk.wall.post(**post_params)
            post_id = response.get("post_id")
            
            if post_id:
                return {
                    "success": True,
                    "post_id": post_id,
                    "group_id": group_id,
                    "url": f"https://vk.com/wall-{group_id}_{post_id}"
                }
            else:
                return {
                    "success": False,
                    "error": "VK API не вернул post_id"
                }
                
        except Exception as e:
            logger.error(f"Ошибка публикации в сообщество {group_id}: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _start_community_listener(self, group_id: int):
        """
        Запускает прослушивание событий для конкретного сообщества.
        
        Args:
            group_id: ID группы для прослушивания.
        """
        if group_id in self._threads:
            logger.warning(f"Поток для сообщества {group_id} уже запущен")
            return
        
        thread = threading.Thread(
            target=self._run_community_longpoll,
            args=(group_id,),
            name=f"CommunityListener-{group_id}",
            daemon=True
        )
        thread.start()
        self._threads[group_id] = thread
        logger.info(f"Запущен слушатель событий для сообщества {group_id}")
    
    def _stop_community_listener(self, group_id: int):
        """
        Останавливает прослушивание событий для сообщества.
        
        Args:
            group_id: ID группы для остановки.
        """
        if group_id not in self._threads:
            return
        
        # Флаг остановки будет проверяться в цикле LongPoll
        # Просто удаляем поток из словаря, цикл завершится сам
        self._threads.pop(group_id, None)
        self._longpolls.pop(group_id, None)
        logger.info(f"Остановлен слушатель событий для сообщества {group_id}")
    
    def _run_community_longpoll(self, group_id: int):
        """
        Цикл прослушивания LongPoll для конкретного сообщества.
        
        Args:
            group_id: ID группы для прослушивания.
        """
        logger.info(f"Запуск LongPoll для сообщества {group_id}")
        
        while self._running and group_id in self._communities:
            try:
                # Инициализируем LongPoll если нужно
                if group_id not in self._longpolls:
                    self._longpolls[group_id] = VkBotLongPoll(
                        self._vk_sessions[group_id],
                        group_id
                    )
                    logger.debug(f"LongPoll инициализирован для группы {group_id}")
                
                longpoll = self._longpolls[group_id]
                
                for event in longpoll.listen():
                    if not self._running or group_id not in self._communities:
                        break
                    
                    self._process_event(group_id, event)
                    
            except vk_api.exceptions.LongPollError as e:
                logger.warning(f"LongPoll ошибка для группы {group_id}: {e}. Переподключение...")
                self._longpolls.pop(group_id, None)
                time.sleep(5)
            except vk_api.exceptions.ApiError as e:
                logger.error(f"API ошибка для группы {group_id}: {e}. Переподключение...")
                self._vk_sessions.pop(group_id, None)
                self._longpolls.pop(group_id, None)
                time.sleep(5)
            except Exception as e:
                logger.error(f"Неожиданная ошибка в LongPoll группы {group_id}: {e}")
                time.sleep(5)
        
        logger.info(f"LongPoll для сообщества {group_id} остановлен")
    
    def _process_event(self, group_id: int, event):
        """
        Обрабатывает событие от сообщества.
        
        Args:
            group_id: ID группы от которой пришло событие.
            event: Событие LongPoll.
        """
        try:
            if event.type != VkBotEventType.MESSAGE_NEW:
                return
            
            message_data = event.object.message
            peer_id = message_data.get("peer_id")
            text = message_data.get("text", "").strip().lower()
            from_id = message_data.get("from_id")
            
            # Игнорируем сообщения от самого бота
            if from_id == group_id * -1:
                return
            
            logger.info(f"[Группа {group_id}] Сообщение от peer_id={peer_id}: {text[:50]}")
            
            # Обработка команд
            if text in ["старт", "/start"]:
                self._handle_start(group_id, peer_id)
            elif text in ["статус", "/status"]:
                self._handle_status(group_id, peer_id)
            elif text in ["сгенерировать", "/generate"]:
                self._handle_generate(group_id, peer_id)
            elif text in ["одобрить", "/approve"]:
                self._handle_approve(group_id, peer_id)
            elif text.startswith("/add_token"):
                # Команда для добавления нового токена (только для админов)
                self._handle_add_token(group_id, peer_id, text)
            else:
                # Проверка payload от кнопок
                payload = message_data.get("payload")
                if payload:
                    import json
                    try:
                        payload_data = json.loads(payload) if isinstance(payload, str) else payload
                        if isinstance(payload_data, dict):
                            post_id = payload_data.get("post_id")
                            action = payload_data.get("action")
                            if post_id and action in ["approve", "reject"]:
                                self._handle_approve_action(
                                    group_id, peer_id, post_id, action
                                )
                                return
                    except (json.JSONDecodeError, TypeError):
                        pass
                
                # Неизвестная команда
                self._send_message(
                    group_id, peer_id,
                    "❓ Не распознал команду. Используйте /start для списка команд."
                )
                
        except Exception as e:
            logger.error(f"Ошибка обработки события для группы {group_id}: {e}")
    
    def _send_message(self, group_id: int, peer_id: int, message: str, keyboard: Optional[dict] = None):
        """
        Отправляет сообщение от имени конкретного сообщества.
        
        Args:
            group_id: ID группы от имени которой отправляется сообщение.
            peer_id: ID получателя.
            message: Текст сообщения.
            keyboard: JSON-клавиатура (опционально).
        """
        vk = self._get_vk_api(group_id)
        if not vk:
            logger.error(f"Не могу отправить сообщение: группа {group_id} не найдена")
            return
        
        import random
        params = {
            "peer_id": peer_id,
            "message": message,
            "random_id": random.randint(0, 2**31 - 1),
        }
        
        if keyboard:
            params["keyboard"] = keyboard
        
        try:
            vk.messages.send(**params)
            logger.debug(f"[Группа {group_id}] Сообщение отправлено peer_id={peer_id}")
        except Exception as e:
            logger.error(f"[Группа {group_id}] Ошибка отправки сообщения: {e}")
    
    def _handle_start(self, group_id: int, peer_id: int):
        """Обработка команды /start."""
        welcome = (
            f"👋 Привет! Я мастер-бот для управления автоконтентом.\n\n"
            f"📋 Сейчас я управляю несколькими сообществами.\n"
            f"Это сообщество ID: {group_id}\n\n"
            f"Доступные команды:\n"
            f"• /start — Показать это сообщение\n"
            f"• /status — Статистика постов\n"
            f"• /generate — Создать пак постов на неделю\n"
            f"• /approve — Одобрить последний черновик"
        )
        self._send_message(group_id, peer_id, welcome, self._get_main_keyboard())
    
    def _handle_status(self, group_id: int, peer_id: int):
        """Обработка команды /status."""
        db = SessionLocal()
        try:
            draft_count = db.query(Post).filter(Post.status == "draft").count()
            approved_count = db.query(Post).filter(Post.status == "approved").count()
            published_count = db.query(Post).filter(Post.status == "published").count()
            
            status_msg = (
                f"📊 Статус постов (все сообщества):\n\n"
                f"📝 Черновики: {draft_count}\n"
                f"✅ Одобрено: {approved_count}\n"
                f"📢 Опубликовано: {published_count}\n\n"
                f"Всего: {draft_count + approved_count + published_count}"
            )
            self._send_message(group_id, peer_id, status_msg)
        finally:
            db.close()
    
    def _handle_generate(self, group_id: int, peer_id: int):
        """Обработка команды /generate."""
        self._send_message(
            group_id, peer_id,
            "⏳ Запускаю генерацию недельного пакета..."
        )
        
        def run_generation():
            from services.content_service import generate_weekly_pack
            db = SessionLocal()
            try:
                result = generate_weekly_pack(niche="3d_cookies", db=db)
                response = (
                    f"✅ Генерация завершена!\n\n"
                    f"Создано постов: {len(result)}\n"
                    f"Посты доступны для одобрения через /approve"
                )
                self._send_message(group_id, peer_id, response)
            except Exception as e:
                self._send_message(group_id, peer_id, f"❌ Ошибка генерации: {e}")
            finally:
                db.close()
        
        thread = threading.Thread(target=run_generation, daemon=True)
        thread.start()
    
    def _handle_approve(self, group_id: int, peer_id: int):
        """Обработка команды /approve."""
        db = SessionLocal()
        try:
            last_draft = db.query(Post).filter(
                Post.status == "draft"
            ).order_by(Post.id.desc()).first()
            
            if not last_draft:
                self._send_message(
                    group_id, peer_id,
                    "📭 Нет черновиков. Сначала создайте посты через /generate"
                )
                return
            
            preview = (last_draft.text_draft or last_draft.text_final or "(нет текста)")[:400]
            
            approve_msg = (
                f"📝 Последний черновик (ID: {last_draft.id}):\n\n"
                f"Тип: {last_draft.post_type}\n"
                f"Тема: {last_draft.topic}\n\n"
                f"Текст:\n{preview}\n\n"
                f"Изображение: {'✅' if last_draft.image_url else '❌'}\n\n"
                f"Одобрить этот пост?"
            )
            
            keyboard = self._get_approval_keyboard(last_draft.id)
            self._send_message(group_id, peer_id, approve_msg, keyboard)
        finally:
            db.close()
    
    def _handle_approve_action(self, group_id: int, peer_id: int, post_id: int, action: str):
        """Обработка одобрения/отклонения поста."""
        db = SessionLocal()
        try:
            post = db.query(Post).filter(Post.id == post_id).first()
            
            if not post:
                self._send_message(group_id, peer_id, f"❌ Пост ID {post_id} не найден")
                return
            
            if action == "approve":
                if post.status != "draft":
                    self._send_message(
                        group_id, peer_id,
                        f"⚠️ Пост уже имеет статус '{post.status}'"
                    )
                    return
                
                post.status = "approved"
                db.commit()
                self._send_message(
                    group_id, peer_id,
                    f"✅ Пост ID {post_id} одобрен!"
                )
            elif action == "reject":
                self._send_message(
                    group_id, peer_id,
                    f"❌ Пост ID {post_id} отклонён"
                )
        except Exception as e:
            self._send_message(group_id, peer_id, f"❌ Ошибка: {e}")
            db.rollback()
        finally:
            db.close()
    
    def _handle_add_token(self, group_id: int, peer_id: int, text: str):
        """
        Обработка команды добавления нового токена.
        Формат: /add_token <token> <group_id>
        """
        parts = text.split()
        if len(parts) < 3:
            self._send_message(
                group_id, peer_id,
                "❌ Использование: /add_token <токен> <group_id>\n\n"
                "Пример: /add_token abc123 12345678"
            )
            return
        
        token = parts[1]
        try:
            new_group_id = int(parts[2])
        except ValueError:
            self._send_message(group_id, peer_id, "❌ group_id должен быть числом")
            return
        
        # Создаём новое сообщество
        new_community = CommunityAccount(
            id=0,  # Будет установлен в БД
            platform="vk",
            account_id=str(new_group_id),
            access_token=token,
            group_id=new_group_id
        )
        
        if self.register_community(new_community):
            self._send_message(
                group_id, peer_id,
                f"✅ Сообщество {new_group_id} успешно добавлено!\n"
                f"Теперь я управляю {len(self._communities)} сообществами."
            )
        else:
            self._send_message(
                group_id, peer_id,
                f"❌ Не удалось добавить сообщество {new_group_id}.\n"
                f"Проверьте токен и права доступа."
            )
    
    def _get_main_keyboard(self) -> dict:
        """Возвращает основную клавиатуру."""
        return {
            "one_time": False,
            "inline": False,
            "buttons": [
                [
                    {
                        "action": {
                            "type": "text",
                            "label": "Сгенерировать пак"
                        },
                        "color": "primary"
                    }
                ],
                [
                    {
                        "action": {
                            "type": "text",
                            "label": "Статус"
                        },
                        "color": "secondary"
                    },
                    {
                        "action": {
                            "type": "text",
                            "label": "Одобрить"
                        },
                        "color": "positive"
                    }
                ]
            ]
        }
    
    def _get_approval_keyboard(self, post_id: int) -> dict:
        """Возвращает клавиатуру одобрения."""
        return {
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
    
    def run(self):
        """
        Запускает мастер-бота для управления всеми зарегистрированными сообществами.
        """
        logger.info("Запуск CommunityManager...")
        self._running = True
        
        # Загружаем сообщества из БД
        count = self.load_communities_from_db()
        
        if count == 0:
            logger.warning("Нет зарегистрированных сообществ. Используйте /add_token для добавления.")
        
        # Запускаем слушатели для всех сообществ
        for group_id in self._communities:
            self._start_community_listener(group_id)
        
        # Держим главный поток активным
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Получен сигнал остановки")
        finally:
            self.stop()
    
    def stop(self):
        """Останавливает все слушатели событий."""
        logger.info("Остановка CommunityManager...")
        self._running = False
        
        # Ждем завершения потоков
        for group_id in list(self._threads.keys()):
            self._stop_community_listener(group_id)
        
        logger.info("CommunityManager остановлен")
