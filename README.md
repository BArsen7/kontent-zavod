# Autopilot Content: Автопилот для ведения соцсетей

![Python](https://img.shields.io/badge/Python-3.11-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green)
![VK API](https://img.shields.io/badge/VK_API-Latest-blue)
![License](https://img.shields.io/badge/License-MIT-yellow)

## 📖 Описание проекта

**Autopilot Content** — это система автоматизации ведения социальных сетей, разработанная специально для микроселлеров, экспертов личного бренда и небольших бизнесов. Проект решает главную проблему контент-маркетинга: регулярность публикаций без ежедневных затрат времени на создание контента.

Система работает по принципу «запустил и забыл»: вы один раз настраиваете параметры (нишу, темы, стиль общения), а дальше она автономно генерирует посты, создаёт уникальные изображения, формирует контент-план на период вперёд и автоматически публикует материалы в нужное время. Особый акцент сделан на работу с ВКонтакте — самой популярной социальной сетью в РФ.

**Ключевые возможности:**
- 🔐 **Авторизация по email и паролю** — регистрация, вход, защищённые сессии (httponly-cookie)
- 👥 **Управление несколькими сообществами VK** — добавление сообществ с проверкой прав администратора, динамическая регистрация токенов
- 🤖 **Генерация текста** через локальные LLM (Ollama: Qwen2.5) или облачной API (GigaChat)
- 🎨 **Создание изображений** через Kandinsky (GigaChat Image API)
- 🧑‍💼 **Персональный ИИ-контент-менеджер** — чат с нейросетью, который задаёт уточняющие вопросы и генерирует контент-план на неделю / 2 недели / месяц (подробнее в [CONTENT_MANAGER_README.md](CONTENT_MANAGER_README.md))
- ⏰ **Фоновый планировщик** (APScheduler) для автоматической публикации одобренных постов каждые 30 минут
- 📊 **Веб-интерфейс** (Jinja2): главная страница со списком постов, страница контент-менеджера, страница входа/регистрации
- 🛠️ **CLI-скрипты** для ручного управления и автоматизации рутинных задач
- 🔄 **CommunityManager (мастер-бот)** — управление множеством сообществ VK через единый интерфейс и LongPoll (архитектура описана в [ARCHITECTURE.md](ARCHITECTURE.md))

---

## 🏗️ Архитектура проекта

Проект может работать как на одном устройстве, так и в распределённой схеме «станция генерации + сервер публикации»:

```
┌─────────────────────────┐      ┌──────────────────────────┐
│   Ноутбук (Fedora)      │      │  Сервер (Banana Pi / VPS)│
│   iGPU, 32GB RAM        │      │  (Armbian, 4GB RAM)      │
│                         │      │                          │
│  ┌───────────────────┐  │      │  ┌────────────────────┐  │
│  │   Ollama Server   │  │      │  │   FastAPI Server   │  │
│  │   (LLM Models)    │  │      │  │   (REST API + Web) │  │
│  │                   │  │      │  │                    │  │
│  │ • qwen2.5:14b     │  │      │  │ • CommunityManager │  │
│  │ • qwen2.5:7b      │◄─┼──────┼─►│ • APScheduler      │  │
│  │ • llama3:8b       │  │ HTTP │  │ • SQLite DB        │  │
│  └───────────────────┘  │      │  │ • VKPublisher      │  │
│                         │      │  └────────────────────┘  │
└─────────────────────────┘      └────────────┬─────────────┘
                                              │
                                     ┌────────▼────────┐
                                     │   ВКонтакте     │
                                     │   (Публикация)  │
                                     └─────────────────┘
```

### Основные компоненты

| Компонент | Файл | Назначение |
|-----------|------|------------|
| FastAPI-приложение | `app.py` | Точка входа: веб-роуты, API, lifespan (запуск БД, CommunityManager, планировщика) |
| Настройки | `config.py` | Класс `Settings` (pydantic-settings), чтение `.env` |
| Модели БД | `models.py` | SQLAlchemy-модели: `User`, `UserCommunity`, `PlatformAccount`, `ContentPlan`, `Post`, `PostStats`, `ContentPlanPeriod`, `ChatMessage` |
| Генерация текста | `generators/text_generator.py` | `generate_text()` — обёртки над Ollama и GigaChat |
| Генерация изображений | `generators/image_generator.py` | `generate_kandinsky()` — Kandinsky через GigaChat API |
| Публикация | `publishers/vk_publisher.py` | `VKPublisher` — публикация в одно сообщество (токен из настроек) |
| Мастер-бот | `managers/community_manager.py` | `CommunityManager` — управление множеством сообществ, LongPoll, `/add_token` |
| Сервис паков | `services/content_service.py` | `generate_weekly_pack()` — генерация недельного пакета постов |
| Контент-менеджер | `services/content_manager_service.py` | Чат с ИИ, генерация и обновление планов, проверка истекающих периодов |
| Планировщик | `services/scheduler.py` | APScheduler: `check_and_publish()` каждые 30 минут |
| Legacy-бот | `bots/vk_bot.py` | Одиночный VK-бот (LongPoll) — оставлен для совместимости, в `app.py` не запускается |

### Поток данных

1. **Генерация**: пользователь инициирует генерацию через веб-интерфейс (`POST /api/generate/weekly`), чат контент-менеджера или CLI-скрипт → Ollama/GigaChat генерируют тексты, Kandinsky — изображения → результаты сохраняются в SQLite со статусом `draft`.
2. **Модерация**: просмотр черновиков в веб-интерфейсе или через API (`POST /api/posts/{id}/approve`) меняет статус на `approved`.
3. **Публикация**: APScheduler каждые 30 минут находит посты `approved` с `publish_at <= now()` и публикует их через `VKPublisher` (токен из `.env`); ручная публикация — `POST /api/publish/vk/{post_id}` через `CommunityManager` (токены сообществ из БД). Статус меняется на `published`.

---

## 💻 Установка на ноутбуке (Fedora)

Ноутбук используется как мощная станция для генерации контента. Здесь работают модели Ollama, которые требуют значительных ресурсов CPU/GPU и оперативной памяти.

### Шаг 1: Клонирование репозитория

```bash
git clone <URL_РЕПОЗИТОРИЯ>/autopilot-content.git
cd autopilot-content
```

> ⚠️ **Важно**: Убедитесь, что у вас установлен Git. Если нет:
> ```bash
> sudo dnf install git -y
> ```

### Шаг 2: Создание виртуального окружения

```bash
python3.11 -m venv venv
source venv/bin/activate
```

### Шаг 3: Установка зависимостей

```bash
pip install -r requirements.txt
```

Актуальный список зависимостей (`requirements.txt`):

| Пакет | Назначение |
|-------|------------|
| `fastapi` | Веб-фреймворк и REST API |
| `uvicorn[standard]` | ASGI-сервер |
| `sqlalchemy` | ORM для SQLite |
| `pydantic-settings` | Конфигурация из `.env` |
| `python-dotenv` | Загрузка переменных окружения |
| `requests` | HTTP-клиент (Ollama, GigaChat, Kandinsky) |
| `jinja2` | HTML-шаблоны веб-интерфейса |
| `python-multipart` | Обработка form-данных (логин/регистрация) |
| `apscheduler` | Фоновый планировщик публикаций |
| `vk-api` | Клиент ВКонтакте (публикация, LongPoll) |
| `passlib[bcrypt]` | Хеширование паролей пользователей |

### Шаг 4: Установка Ollama

Ollama — локальный сервер для запуска больших языковых моделей. Он необходим для генерации текстов постов без отправки данных в облако.

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Проверьте, что сервис запущен, и включите автозапуск:

```bash
systemctl status ollama
sudo systemctl start ollama
sudo systemctl enable ollama
```

### Шаг 5: Скачивание моделей

В коде по умолчанию используется модель `qwen2.5:14b` (параметр `model` функции `generate_text_ollama`):

```bash
# Основная модель для генерации постов (баланс качества и скорости)
ollama pull qwen2.5:14b

# Лёгкая модель для быстрых задач (если будете менять вручную в коде)
ollama pull qwen2.5:7b
```

> ⚠️ **Внимание**: модели занимают много места на диске (`qwen2.5:14b` — около 9 GB). Проверьте свободное место: `df -h`.

Проверьте, что модели загружены:

```bash
ollama list
```

### Шаг 6: Настройка переменных окружения (.env)

Скопируйте шаблон файла конфигурации:

```bash
cp .env.example .env
nano .env
```

Переменные, которые реально читает приложение (`config.py`):

| Переменная | Обязательная | Описание | Пример значения |
|------------|--------------|----------|-----------------|
| `DB_PATH` | нет (есть дефолт) | Путь к файлу SQLite БД | `data/autopilot.db` |
| `OLLAMA_URL` | для генерации через Ollama | URL сервера Ollama | `http://localhost:11434` |
| `GIGACHAT_KEY` | для Kandinsky/GigaChat | API-ключ GigaChat | `your_gigachat_key_here` |
| `GIGACHAT_SECRET` | для Kandinsky/GigaChat | Секретный ключ GigaChat | `your_gigachat_secret_here` |
| `VK_TOKEN` | для планировщика | Токен сообщества VK (использует `VKPublisher` в scheduler) | `vk1.a.ABC123...` |
| `VK_GROUP_ID` | для планировщика | ID группы VK (число) | `123456789` |
| `TG_BOT_TOKEN` | нет | Зарезервировано под будущего Telegram-бота | — |
| `TG_PROXY_URL` | нет | Прокси (зарезервировано) | — |
| `LOG_LEVEL` | нет (дефолт `INFO`) | Уровень логирования | `INFO` |

Пример заполненного `.env`:

```ini
# Database settings
DB_PATH=data/autopilot.db

# AI Services
OLLAMA_URL=http://localhost:11434
GIGACHAT_KEY=your_gigachat_key_here
GIGACHAT_SECRET=your_gigachat_secret_here

# VKontakte settings
VK_TOKEN=vk1.a.ABC123xyz789...
VK_GROUP_ID=123456789

# Telegram settings (reserved for future use)
TG_BOT_TOKEN=
TG_PROXY_URL=

# Logging
LOG_LEVEL=INFO
```

> ℹ️ Для добавления сообщества через веб-интерфейс токен сообщества вводится прямо в форме — постоянные `VK_TOKEN`/`VK_GROUP_ID` нужны только планировщику `services/scheduler.py`. Поля `VK_CLIENT_ID`, `VK_CLIENT_SECRET`, `VK_REDIRECT_URI` в `.env.example` зарезервированы под будущий OAuth и текущим кодом не используются (`Settings` игнорирует неизвестные переменные окружения — `extra="ignore"`).

### Возможные ошибки при запуске

**`pydantic_core.ValidationError: Extra inputs are not permitted (vk_client_id ...)`**

Причина: устаревшая версия `config.py`, в которой класс `Settings` не допускал лишних переменных в `.env`, при этом `.env.example` содержит зарезервированные поля `VK_CLIENT_ID`/`VK_CLIENT_SECRET`/`VK_REDIRECT_URI`.

Решение: обновите `config.py` до текущей версии (в `Settings.model_config` добавлен `extra="ignore"`) либо удалите эти строки из своего `.env`. После этого `python app.py` запускается без ошибок.

> ⚠️ **Критично важно**: Никогда не коммитьте файл `.env` в репозиторий! Он добавлен в `.gitignore`. Токены и ключи должны храниться в секрете.

### Шаг 7: Инициализация базы данных

Таблицы создаются автоматически при старте приложения (lifespan-хук вызывает `init_db()`). Дополнительно можно инициализировать БД вручную и заполнить тестовыми данными:

```bash
# Только создание таблиц
python scripts/setup_db.py

# Создание таблиц + тестовые данные
python scripts/setup_db.py --with-test-data

# Пересоздание таблиц ВНИМАНИЕ: удалит все данные
python scripts/setup_db.py --drop-existing
```

Создаваемые таблицы: `users`, `user_communities`, `platform_accounts`, `content_plans`, `posts`, `post_stats`, `content_plan_periods`, `chat_messages`.

### Шаг 8: Запуск веб-интерфейса и API

Запустите сервер FastAPI из корня проекта:

```bash
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

Параметры:
- `--host 0.0.0.0` — делает сервер доступным из локальной сети
- `--port 8000` — порт для подключения
- `--reload` — автоматическая перезагрузка при изменении кода (удобно для разработки)

> ✅ **Проверка**: Откройте браузер и перейдите по адресу:
> - Локально: `http://localhost:8000`
> - Из сети: `http://<IP_НОУТБУКА>:8000`
>
> Вы увидите страницу входа. После регистрации/входа — главную страницу со списком постов и вкладку «Контент-менеджер».

---

## 🍌 Установка на сервер (Banana Pi M4 Zero / Armbian)

Сервер работает 24/7: хранит базу данных, публикует одобренные посты по расписанию и слушает события сообществ. Генерация при этом может оставаться на ноутбуке (нужен только сетевой доступ к Ollama).

### Шаг 1: Обновление системы

```bash
sudo apt update && sudo apt upgrade -y
```

### Шаг 2: Установка Python и системных зависимостей

```bash
sudo apt install -y python3 python3-venv python3-pip git
```

### Шаг 3: Монтирование внешнего SSD

#### 3.1. Подключение и определение диска

```bash
lsblk
```

Найдите новый диск (например, `sda`) объёмом, соответствующим вашему SSD.

#### 3.2. Форматирование диска (если новый)

```bash
sudo mkfs.ext4 /dev/sda1
```

#### 3.3. Создание точки монтирования

```bash
sudo mkdir -p /mnt/autopilot_ssd
sudo mount /dev/sda1 /mnt/autopilot_ssd
```

#### 3.4. Настройка авто-монтирования при загрузке

Узнайте UUID диска и добавьте запись в `/etc/fstab`:

```bash
sudo blkid /dev/sda1
echo 'UUID=<ваш-uuid> /mnt/autopilot_ssd ext4 defaults,noatime 0 2' | sudo tee -a /etc/fstab
sudo mount -a   # проверка без перезагрузки
```

### Шаг 4: Клонирование репозитория

```bash
cd /mnt/autopilot_ssd
git clone <URL_РЕПОЗИТОРИЯ>/autopilot-content.git
cd autopilot-content
```

### Шаг 5: Создание виртуального окружения

```bash
python3 -m venv venv
source venv/bin/activate
```

### Шаг 6: Установка зависимостей

```bash
pip install -r requirements.txt
```

### Шаг 7: Настройка переменных окружения (.env)

```bash
cp .env.example .env
nano .env
```

Заполните переменные для сервера:

```ini
# AI Services (Ollama на ноутбуке — укажите его IP в локальной сети)
OLLAMA_URL=http://<IP_НОУТБУКА>:11434

# GigaChat (для Kandinsky и опциональной генерации текста)
GIGACHAT_KEY=your_gigachat_key
GIGACHAT_SECRET=your_gigachat_secret

# Database (путь на внешнем SSD)
DB_PATH=/mnt/autopilot_ssd/data/autopilot.db

# VK (нужно планировщику services/scheduler.py)
VK_TOKEN=vk1.a.ABC123xyz789...
VK_GROUP_ID=123456789

# Logging
LOG_LEVEL=INFO
```

> ⚠️ **Критично важно**:
> - `DB_PATH` должен указывать на внешний SSD для надёжности (директория создаётся автоматически `database.py`)
> - `OLLAMA_URL` должен указывать на IP-адрес вашего ноутбука в локальной сети
> - Убедитесь, что ноутбук и сервер находятся в одной сети и порт 11434 открыт

### Шаг 8: Инициализация базы данных

```bash
python scripts/setup_db.py
```

(При первом запуске uvicorn таблицы также создадутся автоматически.)

### Шаг 9: Настройка systemd-сервиса для автозапуска

Создайте файл `/etc/systemd/system/autopilot.service`:

```ini
[Unit]
Description=Autopilot Content Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/mnt/autopilot_ssd/autopilot-content
Environment="PATH=/mnt/autopilot_ssd/autopilot-content/venv/bin"
ExecStart=/mnt/autopilot_ssd/autopilot-content/venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000

Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

#### Активация сервиса

```bash
sudo systemctl daemon-reload
sudo systemctl enable autopilot.service
sudo systemctl start autopilot.service
sudo systemctl status autopilot.service
```

#### Управление сервисом

```bash
# Остановить сервис
sudo systemctl stop autopilot.service

# Перезапустить сервис
sudo systemctl restart autopilot.service

# Просмотр логов в реальном времени
sudo journalctl -u autopilot.service -f

# Просмотр последних 100 строк логов
sudo journalctl -u autopilot.service -n 100
```

---

## 🚀 Использование

### Веб-интерфейс

Откройте браузер и перейдите по адресу:

```
http://localhost:8000
# или из сети:
http://<IP_СЕРВЕРА>:8000
```

Страницы приложения:

| Страница | URL | Описание |
|----------|-----|----------|
| Вход/регистрация | `/login` | Форма авторизации по email/паролю и регистрации |
| Главная | `/` | Таблица всех постов (доступна после входа) |
| Контент-менеджер | `/content-manager` | Чат с ИИ-маркетологом и генерация контент-планов |

#### 🔐 Авторизация

Система использует собственную авторизацию по email и паролю (не OAuth VK):

1. Откройте `/login` (при отсутствии сессии главная страница перенаправляет туда)
2. Зарегистрируйтесь (email + пароль, опционально имя) или войдите
3. Пароли хранятся в БД в виде bcrypt-хешей (`passlib`), сессия — httponly-cookie `session_id` (7 дней)

> ⚠️ **Важно**:
> - Сессии хранятся в памяти процесса (`_session_store` в `app.py`). После перезапуска сервера пользователей нужно входить заново. Для продакшена замените на Redis/БД.
> - API контент-менеджера и управления сообществами требует авторизации (HTTP 401 без сессии).

#### 👥 Управление сообществами

После входа можно привязать сообщества VK к своему аккаунту:

- `POST /api/user/communities/add` — добавить сообщество (параметры `group_id` и `token`). Система проверяет токен через VK API и убеждается, что у вас есть права администратора, прежде чем сохранить связь в таблице `user_communities`.
- `GET /api/user/communities` — список ваших сообществ.
- `DELETE /api/user/communities/{group_id}` — отвязать сообщество.

Как получить токен сообщества:
1. Создайте Standalone-приложение в [VK Developers](https://dev.vk.com/)
2. Запросите права: `groups, wall, photos`
3. Получите ключ доступа пользователя-администратора и используйте его

Для публикации в несколько сообществ без привязки к пользователю используется мастер-бот `CommunityManager` (см. [ARCHITECTURE.md](ARCHITECTURE.md)):
- `POST /api/communities/register` — зарегистрировать сообщество (токен сохраняется в `platform_accounts`, запускается LongPoll-поток)
- Команда `/add_token <токен> <group_id>` прямо в диалоге с ботом

#### 📊 Работа с постами

1. **Генерация пака**: `POST /api/generate/weekly` — запускает генерацию 7 постов в фоновом режиме (BackgroundTasks); ниша зафиксирована в коде (`3d_cookies`)
2. **Одобрение**: `POST /api/posts/{id}/approve` — меняет статус `draft → approved`
3. **Публикация**: `POST /api/publish/vk/{post_id}` — публикация через CommunityManager (посты со статусом `draft` или `approved`); если `?group_id=` не указан, берётся первое зарегистрированное сообщество. Статус меняется на `published`.
4. **Автопубликация**: планировщик сам публикует посты `approved` с наступившим `publish_at`, используя `VKPublisher` с токеном из `.env` (см. ниже про `VK_TOKEN`/`VK_GROUP_ID`)

#### 🧑‍💼 Контент-менеджер

Вкладка `/content-manager` — персональный ИИ-ассистент маркетолога:

1. Создайте период (неделя / 2 недели / месяц)
2. Ответьте на уточняющие вопросы ИИ в чате
3. Нажмите «Сгенерировать план» — посты появятся в общей таблице `posts`
4. План автоматически предложит обновить себя за ≤3 дня до конца периода

Полное описание функционала — в [CONTENT_MANAGER_README.md](CONTENT_MANAGER_README.md).

---

### VK-бот (CommunityManager)

Мастер-бот запускается автоматически вместе с приложением (отдельный поток в lifespan). Каждое зарегистрированное сообщество получает собственный LongPoll-цикл; бот отвечает на сообщения внутри сообщества.

| Команда | Описание |
|---------|----------|
| `Старт` / `/start` | Приветствие и список команд |
| `Статус` / `/status` | Статистика постов по статусам |
| `Сгенерировать` / `/generate` | Генерация недельного пака в фоновом потоке |
| `Одобрить` / `/approve` | Последний черновик + кнопки «Одобрить/Отклонить» |
| `/add_token <токен> <group_id>` | Динамическая привязка нового сообщества |

> ℹ️ Старый модуль `bots/vk_bot.py` (привязка к одному сообществу через `VK_TOKEN`/`VK_GROUP_ID`) оставлен в репозитории для совместимости, но `app.py` его не запускает — вместо него используется `CommunityManager`.

---

### CLI-скрипты

Скрипты находятся в папке `scripts/` и предназначены для автоматизации рутинных задач. Запускаются из корня проекта внутри виртуального окружения.

#### 1. `generate_post.py` — Генерация одиночного поста

**Аргументы**:
- `--topic` (обязательный): Тема поста
- `--type` (по умолчанию «польза»): польза / вовлечение / развлечение / продажа
- `--model` (по умолчанию «ollama»): ollama / gigachat

```bash
python scripts/generate_post.py --topic "Как мыть 3D-формочки" --type "польза"
python scripts/generate_post.py --topic "Смешные случаи с печеньем" --type "развлечение" --model "gigachat"
python scripts/generate_post.py --topic "Новогодняя скидка 20%" --type "продажа"
```

Пост сохраняется в БД со статусом `draft` (для `gigachat` требуется `GIGACHAT_KEY`/`GIGACHAT_SECRET`).

#### 2. `generate_weekly.py` — Генерация недельного пака

**Аргументы**:
- `--niche` (по умолчанию `3d_cookie_cutters`): Ниша/тематика контента
- `--model` (по умолчанию `ollama`): Модель для генерации

```bash
python scripts/generate_weekly.py --niche "3d_cookie_cutters"
python scripts/generate_weekly.py
```

Создаёт контент-план на неделю и 7 постов разных типов со статусом `draft`.

#### 3. `publish_pending.py` — Публикация одобренных постов

**Аргументы**:
- `--limit` (по умолчанию все): Ограничить количество публикуемых постов
- `--dry-run`: Показать, что будет опубликовано, без реальной публикации

```bash
python scripts/publish_pending.py
python scripts/publish_pending.py --limit 3 --dry-run
```

Ищет посты `approved` и публикует их через `VKPublisher` (использует `VK_TOKEN` и `VK_GROUP_ID` из `.env`). При ошибке публикации статус не меняется — пост будет повторён позже.

#### 4. `setup_db.py` — Инициализация базы данных

**Аргументы**:
- `--with-test-data`: Добавить тестовые данные после создания таблиц
- `--drop-existing`: Удалить существующие таблицы перед созданием (⚠️ все данные будут потеряны)

```bash
python scripts/setup_db.py
python scripts/setup_db.py --with-test-data
```

---

## 🌐 API Endpoints

Проект предоставляет REST API для интеграции с внешними системами.

### Базовый URL

```
http://<IP_СЕРВЕРА>:8000
```

### Документация API

Полная интерактивная документация (Swagger UI) доступна по адресу:

```
http://<IP_СЕРВЕРА>:8000/docs
```

### Таблица endpoints (соответствует app.py)

**Веб-страницы:**

| Метод | Путь | Описание | Авторизация |
|-------|------|----------|-------------|
| `GET` | `/` | Главная страница со списком постов | нет (без сессии — пустой список) |
| `GET` | `/content-manager` | Страница контент-менеджера | нет (API страницы — да) |
| `GET` | `/login` | Страница входа/регистрации | нет |

**Аутентификация:**

| Метод | Путь | Описание | Параметры |
|-------|------|----------|-----------|
| `POST` | `/auth/login` | Вход | form: `email`, `password` |
| `POST` | `/auth/register` | Регистрация | form: `email`, `password`, `first_name`, `last_name` |
| `GET` | `/logout` | Выход (удаляет сессию и cookie) | — |
| `GET` | `/api/user/me` | Текущий пользователь | требует сессию |

**Посты и публикация:**

| Метод | Путь | Описание | Параметры |
|-------|------|----------|-----------|
| `GET` | `/api/posts` | Список всех постов | — |
| `POST` | `/api/generate/weekly` | Генерация недельного пака (фон) | ниша фиксирована: `3d_cookies` |
| `POST` | `/api/posts/{id}/approve` | Одобрить черновик (`draft → approved`) | path: `id` |
| `POST` | `/api/publish/vk/{id}` | Опубликовать через CommunityManager | path: `id`, query: `group_id` (опц.) |
| `GET` | `/api/status` | Проверка работоспособности API | — |

**Сообщества:**

| Метод | Путь | Описание | Авторизация |
|-------|------|----------|-------------|
| `GET` | `/api/user/communities` | Сообщества текущего пользователя | да |
| `POST` | `/api/user/communities/add` | Добавить сообщество (проверка прав админа VK) | да |
| `DELETE` | `/api/user/communities/{group_id}` | Отвязать сообщество | да |
| `GET` | `/api/communities` | Все сообщества CommunityManager | нет |
| `POST` | `/api/communities/register` | Зарегистрировать сообщество в мастер-боте | нет |
| `DELETE` | `/api/communities/{group_id}` | Удалить сообщество из мастер-бота | нет |

**Контент-менеджер:**

| Метод | Путь | Описание | Авторизация |
|-------|------|----------|-------------|
| `GET` | `/api/content-manager/periods` | Список периодов пользователя | да |
| `POST` | `/api/content-manager/period/create` | Создать период (`period_type`: week/two_weeks/month) | да |
| `GET` | `/api/content-manager/chat/{period_id}` | История чата | да |
| `POST` | `/api/content-manager/chat/{period_id}/send` | Сообщение в чат, ответ ИИ | да |
| `POST` | `/api/content-manager/period/{id}/generate-plan` | Сгенерировать план из чата | да |
| `PUT` | `/api/content-manager/period/{id}/update` | Обновить план (JSON-body: модификации) | да |
| `POST` | `/api/content-manager/check-expiring` | Проверить истекающие планы | да |
| `GET` | `/api/content-manager/period/{id}/posts` | Посты периода | да |

### Примеры запросов cURL

#### Регистрация и вход:

```bash
# Регистрация
curl -X POST http://localhost:8000/auth/register \
  -d "email=user@example.com" -d "password=Secret123" \
  -d "first_name=Анна" -d "last_name=Петрова" -c cookies.txt

# Вход
curl -X POST http://localhost:8000/auth/login \
  -d "email=user@example.com" -d "password=Secret123" -c cookies.txt
```

#### Генерация недельного пака (фоновая задача):

```bash
curl -X POST http://localhost:8000/api/generate/weekly
```

Ответ:

```json
{
  "status": "started",
  "message": "Генерация запущена в фоновом режиме",
  "count": 7
}
```

#### Получение списка постов:

```bash
curl http://localhost:8000/api/posts
```

Ответ (фрагмент):

```json
[
  {
    "id": 10,
    "content_plan_id": 1,
    "post_type": "benefit",
    "topic": "Как мыть 3D-формочки",
    "text_draft": "Правильный уход за формочками...",
    "image_url": "...",
    "status": "draft",
    "publish_at": null,
    "published_at": null
  }
]
```

#### Одобрение поста:

```bash
curl -X POST http://localhost:8000/api/posts/10/approve
```

Ответ:

```json
{ "success": true, "post_id": 10, "status": "approved" }
```

#### Ручная публикация в ВК:

```bash
curl -X POST "http://localhost:8000/api/publish/vk/10?group_id=123456789"
```

Ответ:

```json
{
  "success": true,
  "post_id": 10,
  "group_id": 123456789,
  "platform_post_id": 567890,
  "url": "https://vk.com/wall-123456789_567890",
  "status": "published"
}
```

#### Добавление сообщества (авторизованный пользователь):

```bash
curl -X POST "http://localhost:8000/api/user/communities/add?group_id=123456789&token=vk1.a.xxx" \
  -b cookies.txt
```

---

## 📁 Структура проекта

```
autopilot-content/
├── app.py                      # FastAPI: lifespan, веб-роуты, API, аутентификация
├── config.py                   # Settings (pydantic-settings), чтение .env
├── database.py                 # Engine, SessionLocal, init_db()
├── models.py                   # SQLAlchemy-модели (8 таблиц)
├── requirements.txt            # Зависимости Python
├── .env.example                # Шаблон переменных окружения
├── .gitignore                  # Игнорируемые файлы Git
├── README.md                   # Эта документация
├── ARCHITECTURE.md             # Архитектура мастер-бота CommunityManager
├── CONTENT_MANAGER_README.md   # Документация ИИ-контент-менеджера
│
├── bots/                       # Legacy: одиночный VK-бот (в app.py не запускается)
│   └── vk_bot.py
│
├── generators/                 # Модуль генерации контента
│   ├── text_generator.py       # Текст: Ollama (/api/generate), GigaChat
│   └── image_generator.py      # Изображения: Kandinsky (GigaChat Image API)
│
├── managers/                   # Мастер-бот
│   └── community_manager.py    # CommunityManager: мульти-сообщества, LongPoll
│
├── publishers/                 # Модуль публикации
│   ├── base_publisher.py       # Абстрактный базовый паблишер
│   └── vk_publisher.py         # Публикация ВКонтакте (текст + изображение)
│
├── services/                   # Бизнес-логика
│   ├── content_service.py      # generate_weekly_pack(): недельный пак постов
│   ├── content_manager_service.py  # Чат с ИИ, планы, автообновление
│   └── scheduler.py            # APScheduler: check_and_publish() каждые 30 мин
│
├── scripts/                    # CLI-скрипты
│   ├── generate_post.py        # Генерация одиночного поста
│   ├── generate_weekly.py      # Генерация недельного пака
│   ├── publish_pending.py      # Публикация одобренных постов (--limit, --dry-run)
│   └── setup_db.py             # Инициализация БД (--with-test-data, --drop-existing)
│
├── web/
│   └── templates/              # Jinja2-шаблоны
│       ├── index.html          # Главная (таблица постов)
│       ├── login.html          # Вход/регистрация
│       └── content_manager.html # Интерфейс контент-менеджера
│
└── data/                       # Данные (каталог создаётся автоматически)
    └── autopilot.db            # SQLite база данных
```

### Краткое описание файлов:

| Файл/Папка | Назначение |
|------------|------------|
| `app.py` | Точка входа FastAPI: lifespan (init_db, CommunityManager, scheduler), роуты, сессии, bcrypt |
| `config.py` | Класс Settings: db_path, ollama_url, gigachat_key/secret, vk_token/group_id, tg_*, log_level |
| `database.py` | SQLite engine (`check_same_thread=False`), SessionLocal, get_db(), init_db() |
| `models.py` | User, UserCommunity, PlatformAccount, ContentPlan, Post, PostStats, ContentPlanPeriod, ChatMessage |
| `generators/text_generator.py` | generate_text_ollama (qwen2.5:14b), generate_text_gigachat, generate_text (диспетчер) |
| `generators/image_generator.py` | _get_gigachat_token (OAuth-авторизация GigaChat), generate_kandinsky |
| `publishers/vk_publisher.py` | VKPublisher: walls.post с загрузкой фото через docs.getMessagesUploadServer |
| `managers/community_manager.py` | CommunityManager + CommunityAccount: register/unregister, publish_to_community, LongPoll-потоки, команды бота |
| `services/content_service.py` | generate_weekly_pack(niche, db): план + 7 постов (prompts по типам) |
| `services/content_manager_service.py` | Периоды, чат-история, generate_ai_response, генерация/обновление плана, check_expiring |
| `services/scheduler.py` | start_scheduler/stop_scheduler, check_and_publish (каждые 30 мин, Europe/Moscow) |
| `bots/vk_bot.py` | Legacy-бот для одного сообщества (не активен) |
| `scripts/*.py` | CLI-утилиты для ручного управления |
| `web/templates/*.html` | Три страницы интерфейса |

---

## ❓ FAQ (Частые вопросы)

### 1. Как добавить новую площадку (ОК, MAX)?

Все паблишеры наследуются от абстрактного класса `BasePublisher` (`publishers/base_publisher.py`) с методом `publish(post_data)`. Создайте новый класс по аналогии с `publishers/vk_publisher.py`:

```python
# publishers/ok_publisher.py
from publishers.base_publisher import BasePublisher

class OKPublisher(BasePublisher):
    def __init__(self, api_key: str, app_id: str):
        self.api_key = api_key
        self.app_id = app_id

    def publish(self, post_data: dict) -> dict:
        """Публикация поста в ОК. Возвращает {'success': bool, ...}."""
        # 1. Загрузка изображения (если есть)
        # 2. Отправка текста с изображением через OK API
        # 3. Возврат результата
        ...
```

Затем зарегистрируйте площадку в `CommunityManager` (расширьте валидацию `CommunityAccount.platform`) или используйте напрямую в `app.py`/планировщике.

### 2. Как изменить модель для генерации текста?

- Название модели задано значением по умолчанию в `generate_text_ollama(model="qwen2.5:14b")` (`generators/text_generator.py`). Чтобы сменить модель глобально, измените параметр по умолчанию или передавайте `model` явно из вызывающего кода.
- Разово через CLI: `python scripts/generate_post.py --topic "Тема" --model "gigachat"` (выбор между ollama/gigachat).
- Отдельная легковесная модель: `python scripts/generate_weekly.py --niche "handmade_jewelry"`.

> ℹ️ Переменных `OLLAMA_MODEL`/`OLLAMA_TIMEOUT` в `config.py` нет — модель выбирается в коде, а не через `.env`.

### 3. Бот не отвечает в ВК. Что делать?

**Чеклист для диагностики:**

1. **Проверьте, что сообщество зарегистрировано**: `GET /api/communities` — оно должно быть в списке. Если пусто — зарегистрируйте через `POST /api/communities/register` или командой `/add_token` (для legacy-бота — через `.env`).
2. **Проверьте токен**: он должен начинаться с `vk1.a.` и не содержать пробелов; права — `groups, wall, photos`.
3. **Проверьте настройки сообщества**: Управление → Работа с API → включите «Личные сообщения» и LongPoll events API.
4. **Проверьте логи**: `sudo journalctl -u autopilot.service -f` — ищите `ApiError`, `AuthError`, ошибки LongPoll.
5. **Перезапустите сервис**: `sudo systemctl restart autopilot.service`.
6. **Проверьте сеть**: `ping api.vk.com`.

### 4. Как посмотреть логи?

Логи пишутся в stdout/stderr процесса (настройка `logging.basicConfig` в `app.py`, уровень — `LOG_LEVEL` из `.env`).

```bash
# Логи systemd-сервиса (рекомендуется)
sudo journalctl -u autopilot.service -f
sudo journalctl -u autopilot.service -n 100
sudo journalctl -u autopilot.service --since today

# Если запущено через uvicorn напрямую — логи в терминале запуска;
# для сохранения в файл:
uvicorn app:app --host 0.0.0.0 --port 8000 2>&1 | tee logs/app.log
```

### 5. Как сделать резервную копию базы данных?

```bash
# Копирование файла БД (путь — DB_PATH из .env, по умолчанию data/autopilot.db)
cp data/autopilot.db backups/autopilot_$(date +%Y%m%d).db

# Автоматизация через cron (ежедневно в 3:00)
crontab -e
```

Добавьте строку (экранируйте `%`):

```
0 3 * * * cp /mnt/autopilot_ssd/data/autopilot.db /mnt/autopilot_ssd/backups/autopilot_$(date +\%Y\%m\%d).db
```

### 6. Как обновить проект?

```bash
# Остановка сервиса
sudo systemctl stop autopilot.service

# Переход в директорию проекта и активация venv
cd /mnt/autopilot_ssd/autopilot-content
source venv/bin/activate

# Pull изменений из репозитория
git pull origin main

# Установка новых зависимостей (если есть)
pip install -r requirements.txt

# Синхронизация схемы БД (new tables создаются init_db автоматически)
python scripts/setup_db.py

# Запуск сервиса и проверка статуса
sudo systemctl start autopilot.service
sudo systemctl status autopilot.service
```

### 7. Почему пользователи «разлогиниваются» после рестарта сервера?

Сессии хранятся в словаре в памяти процесса (`_session_store` в `app.py`) — это осознанное упрощение для демонстрации. После перезапуска все cookie становятся невалидными. Для продакшена вынесите сессии в Redis или отдельную таблицу БД.

### 8. Нужно ли настраивать VK_TOKEN/VK_GROUP_ID в .env?

Только если вы хотите, чтобы **планировщик** (`services/scheduler.py`) публиковал посты автоматически — он использует `VKPublisher` с этими значениями и без них просто не стартует (в логах появится предупреждение). Ручная публикация через `POST /api/publish/vk/{id}` и веб-интерфейс работают через `CommunityManager` с токенами сообществ из БД.

### 9. Приложение падает при запуске: `ValidationError: Extra inputs are not permitted (vk_client_id...)`

Причина: в `.env` остались зарезервированные поля `VK_CLIENT_ID`/`VK_CLIENT_SECRET`/`VK_REDIRECT_URI`, а используется устаревшая версия `config.py` со строгим `Settings`. В текущей версии добавлен `extra="ignore"` — неизвестные переменные окружения игнорируются. Обновите `config.py` (или удалите эти строки из `.env`) и запустите `python app.py` снова. Подробности — в разделе «Возможные ошибки при запуске» инструкции по установке.

---

## 📄 Лицензия и контакты

### Лицензия

Этот проект распространяется под лицензией MIT. Текст лицензии доступен в файле `LICENSE` в корне репозитория.

### Контакты

- **Разработчик**: Your Name
- **Email**: your.email@example.com
- **GitHub**: [github.com/yourusername](https://github.com/yourusername)

### Поддержка проекта

Если проект оказался полезным, пожалуйста:
- ⭐ Поставьте звезду на GitHub
- 🐛 Сообщайте о багах через Issues
- 💡 Предлагайте улучшения через Discussions
- 🔀 Отправляйте Pull Request с исправлениями

---

## 🎯 Заключение

**Autopilot Content** — это готовое решение для автоматизации ведения социальных сетей: генерация контента (текст + изображения), персональный ИИ-контент-менеджер, модерация черновиков и автоматическая публикация по расписанию.

**Что вы получаете:**
- ✅ Автономную генерацию контента через локальные LLM и Kandinsky
- ✅ Контент-планы на неделю/2 недели/месяц, построенные в диалоге с ИИ
- ✅ Управление несколькими сообществами VK (веб-интерфейс, API, мастер-бот, CLI)
- ✅ Гибкое планирование и автопубликацию (APScheduler)
- ✅ Масштабируемую архитектуру (станция генерации + сервер публикации)

**Следующие шаги:**
1. Установите проект по инструкциям выше
2. Настройте `.env` (Ollama, GigaChat, VK)
3. Зарегистрируйтесь в веб-интерфейсе и добавьте сообщество
4. Создайте период в контент-менеджере и сгенерируйте первый план
5. Одобрите посты и наблюдайте за автоматической публикацией

🚀 **Успешного запуска!**

---

*Документация актуальна на октябрь 2026 года. Версия проекта: 1.0.0*
