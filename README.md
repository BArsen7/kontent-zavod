# Autopilot Content: Автопилот для ведения соцсетей

![Python](https://img.shields.io/badge/Python-3.11-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green)
![VK API](https://img.shields.io/badge/VK_API-Latest-blue)
![License](https://img.shields.io/badge/License-MIT-yellow)

## 📖 Описание проекта

**Autopilot Content** — это полноценная система автоматизации ведения социальных сетей, разработанная специально для микроселлеров, экспертов личного бренда и небольших бизнесов. Проект решает главную проблему контент-маркетинга: регулярность публикаций без ежедневных затрат времени на создание контента.

Система работает по принципу «запустил и забыл»: вы один раз настраиваете параметры (нишу, темы, стиль общения), а дальше автономно генерирует посты, создаёт уникальные изображения, формирует контент-план на неделю вперёд и автоматически публикует материалы в нужное время. Особый акцент сделан на работу с ВКонтакте — самой популярной социальной сетью в РФ, которая не требует VPN для доступа и обеспечивает максимальный охват аудитории.

**Ключевые возможности:**
- 🔐 **Авторизация через ВКонтакте** — вход через OAuth, доступ только для администраторов сообществ
- 👥 **Управление несколькими сообществами** — добавление, переключение между группами VK
- 🤖 **Генерация текста** через локальные LLM (Ollama: Qwen2.5, Llama3) или облачные API (GigaChat)
- 🎨 **Создание изображений** через Kandinsky 2.1/3.0 или Stable Diffusion
- 📅 **Автопланирование** публикаций с гибким графиком (утро/день/вечер)
- 🤖 **VK-бот** для управления через сообщения сообщества (статус, генерация, одобрение)
- ⏰ **Фоновый планировщик** (APScheduler) для автоматической публикации одобренных постов
- 📊 **Веб-интерфейс** для просмотра статистики, управления постами и настройками
- 🛠️ **CLI-скрипты** для ручного управления и автоматизации рутинных задач

---

## 🏗️ Архитектура проекта

Проект разделён на два основных компонента, работающих на разных устройствах:

```
┌─────────────────────────┐      ┌──────────────────────────┐
│   Ноутбук (Fedora)      │      │  Banana Pi M4 Zero       │
│   iGPU, 32GB RAM        │      │  (Armbian, 4GB RAM)      │
│                         │      │                          │
│  ┌───────────────────┐  │      │  ┌────────────────────┐  │
│  │   Ollama Server   │  │      │  │   FastAPI Server   │  │
│  │   (LLM Models)    │  │      │  │   (REST API)       │  │
│  │                   │  │      │  │                    │  │
│  │ • qwen2.5:14b     │  │      │  │ • VK Bot (LongPoll)│  │
│  │ • qwen2.5:7b      │◄─┼──────┼─►│ • APScheduler      │  │
│  │ • llama3:8b       │  │ HTTP │  │ • SQLite DB        │  │
│  └───────────────────┘  │      │  │ • VK Publisher     │  │
│                         │      │  └────────────────────┘  │
│  Генерация контента:    │      │                          │
│  • Текст постов         │      │  Хранение и публикация:  │
│  • Промты для картинок  │      │  • Посты (draft/approved)│
│  • Контент-план         │      │  • Изображения (Kandinsky)│
│                         │      │  • Автопубликация       │  │
└─────────────────────────┘      └──────────────────────────┘
           │                                 │
           └───────────────┬─────────────────┘
                           │
                  ┌────────▼────────┐
                  │   ВКонтакте     │
                  │   (Публикация)  │
                  └─────────────────┘
```

### Поток данных:

1. **Генерация (Ноутбук)**:
   - Пользователь инициирует генерацию через веб-интерфейс, VK-бота или CLI
   - Ollama генерирует тексты постов и промты для изображений
   - Через API вызывается Kandinsky для создания картинок
   - Результаты сохраняются в SQLite БД со статусом `draft`

2. **Модерация (Любое устройство)**:
   - Пользователь просматривает черновики через веб-интерфейс или VK-бота
   - Команда «Одобрить» меняет статус на `approved` и устанавливает время публикации

3. **Публикация (Banana Pi)**:
   - APScheduler каждые 30 минут проверяет БД
   - Находит посты со статусом `approved` и `publish_at <= now()`
   - VKPublisher отправляет пост в сообщество ВКонтакте
   - Статус меняется на `published`, результат логируется

---

## 💻 Установка на ноутбуке (Fedora)

Ноутбук используется как мощная станция для генерации контента. Здесь работают модели Ollama, которые требуют значительных ресурсов CPU/GPU и оперативной памяти.

### Шаг 1: Клонирование репозитория

```bash
git clone https://github.com/yourusername/autopilot-content.git
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

> ⚠️ **Требование**: Python версии 3.11 или выше. Проверьте версию:
> ```bash
> python3 --version
> ```
> Если версия ниже 3.11, установите:
> ```bash
> sudo dnf install python3.11 python3.11-pip python3.11-venv -y
> ```

### Шаг 3: Установка зависимостей

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

> ⚠️ **Внимание**: Некоторые пакеты (например, `sqlalchemy`, `vk_api`) могут требовать компиляции. Убедитесь, что установлены системные зависимости:
> ```bash
> sudo dnf install gcc python3-devel libjpeg-devel zlib-devel -y
> ```

### Шаг 4: Установка Ollama

Ollama — это локальный сервер для запуска больших языковых моделей. Он необходим для генерации текстов постов без отправки данных в облако.

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

После установки проверьте, что сервис запущен:

```bash
systemctl status ollama
```

Если сервис не активен, запустите его:

```bash
systemctl start ollama
systemctl enable ollama  # для автозапуска при загрузке
```

### Шаг 5: Скачивание моделей

Для работы проекта необходимы следующие модели:

```bash
# Основная модель для генерации постов (баланс качества и скорости)
ollama pull qwen2.5:14b

# Лёгкая модель для быстрых задач
ollama pull qwen2.5:7b

# Альтернативная модель (опционально)
ollama pull llama3:8b
```

> ⚠️ **Внимание**: Модели занимают много места на диске:
> - `qwen2.5:14b` — около 9 GB
> - `qwen2.5:7b` — около 5 GB
> - `llama3:8b` — около 5 GB
> 
> Убедитесь, что на диске достаточно свободного места:
> ```bash
> df -h
> ```

Проверьте, что модели загружены:

```bash
ollama list
```

### Шаг 6: Настройка переменных окружения (.env)

Скопируйте шаблон файла конфигурации:

```bash
cp .env.example .env
```

Откройте файл `.env` в редакторе:

```bash
nano .env
```

Заполните следующие переменные:

#### Обязательные переменные:

| Переменная | Описание | Пример значения |
|------------|----------|-----------------|
| `OLLAMA_URL` | URL сервера Ollama | `http://localhost:11434` |
| `OLLAMA_MODEL` | Основная модель для генерации | `qwen2.5:14b` |
| `DB_PATH` | Путь к SQLite базе данных | `sqlite:///data/database.db` |

#### Для авторизации через ВКонтакте (OAuth):

| Переменная | Описание | Пример значения |
|------------|----------|-----------------|
| `VK_CLIENT_ID` | ID приложения VK (Standlone) | `12345678` |
| `VK_CLIENT_SECRET` | Секрет приложения VK | `ABCdefGHI123...` |
| `VK_REDIRECT_URI` | URI перенаправления после OAuth | `http://127.0.0.1:8000/auth/vk/callback` |

> ℹ️ **Как получить VK Client ID и Secret**:
> 1. Перейдите в [VK Developers](https://dev.vk.com/)
> 2. Создайте новое приложение типа **Standalone-приложение**
> 3. В настройках приложения укажите **Redirect URI**: `http://127.0.0.1:8000/auth/vk/callback`
> 4. Скопируйте **ID приложения** и **Защищённый ключ** в `.env`

#### Опциональные переменные (для GigaChat):

| Переменная | Описание | Пример значения |
|------------|----------|-----------------|
| `GIGACHAT_KEY` | API ключ GigaChat | `your_gigachat_key` |
| `GIGACHAT_SECRET` | Секретный ключ GigaChat | `your_gigachat_secret` |

> ⚠️ **Критично важно**: Никогда не коммитьте файл `.env` в репозиторий! Он уже добавлен в `.gitignore`. Токены и ключи должны храниться в секрете.

Пример заполненного `.env`:

```ini
# Ollama Configuration
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=qwen2.5:14b
OLLAMA_TIMEOUT=120

# GigaChat Configuration (optional)
GIGACHAT_KEY=
GIGACHAT_SECRET=

# Database
DB_PATH=sqlite:///data/database.db

# VK Configuration
VK_TOKEN=vk1.a.ABC123xyz789...
VK_GROUP_ID=123456789

# Application
LOG_LEVEL=INFO
MEDIA_FOLDER=data/media
UPLOAD_FOLDER=data/uploads
```

### Шаг 7: Инициализация базы данных

Создайте необходимые таблицы в базе данных:

```bash
python scripts/setup_db.py
```

Если вы хотите добавить тестовые данные для проверки работы:

```bash
python scripts/setup_db.py --with-test-data
```

> ✅ **Успех**: Вы должны увидеть сообщение:
> ```
> [INFO] База данных успешно инициализирована.
> [INFO] Таблицы созданы: posts, settings, logs.
> ```

### Шаг 8: Запуск веб-интерфейса и API

Запустите сервер FastAPI:

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
> Вы должны увидеть веб-интерфейс с таблицей постов.

---

## 🍌 Установка на Banana Pi M4 Zero (Armbian)

Banana Pi M4 Zero — это компактный одноплатный компьютер, который работает 24/7 как сервер для хранения данных, управления VK-ботом и автоматической публикации постов.

### Шаг 1: Обновление системы

```bash
sudo apt update && sudo apt upgrade -y
```

> ⚠️ **Внимание**: Процесс обновления может занять 10-30 минут в зависимости от скорости интернета. Не прерывайте процесс.

### Шаг 2: Установка Python и системных зависимостей

```bash
sudo apt install python3 python3-pip python3-venv sqlite3 git curl wget -y
```

Проверьте версии установленных пакетов:

```bash
python3 --version  # Должно быть 3.11+
pip3 --version
sqlite3 --version
```

> ⚠️ **Требование**: Если версия Python ниже 3.11, установите её вручную:
> ```bash
> sudo apt install software-properties-common -y
> sudo add-apt-repository ppa:deadsnakes/ppa
> sudo apt update
> sudo apt install python3.11 python3.11-pip python3.11-venv -y
> ```

### Шаг 3: Монтирование внешнего SSD

Для надёжного хранения данных рекомендуется использовать внешний SSD (минимум 32 GB).

#### 3.1. Подключение и определение диска

Подключите SSD к USB-порту Banana Pi и определите его имя:

```bash
lsblk
```

Вы должны увидеть что-то вроде:

```
NAME   MAJ:MIN RM   SIZE RO TYPE MOUNTPOINT
mmcblk0  179:0    0  29.8G  0 disk 
├─mmcblk0p1 179:1    0  29.8G  0 part /
sda      8:0    0 111.8G  0 disk 
└─sda1   8:1    0 111.8G  0 part 
```

В данном примере `sda1` — это ваш SSD.

#### 3.2. Форматирование диска (если новый)

> ⚠️ **Внимание**: Это удалит все данные на диске! Убедитесь, что диск пуст или данные сохранены.

```bash
sudo mkfs.ext4 /dev/sda1
```

#### 3.3. Создание точки монтирования

```bash
sudo mkdir -p /mnt/autopilot_ssd
sudo mount /dev/sda1 /mnt/autopilot_ssd
```

#### 3.4. Настройка авто-монтирования при загрузке

Добавьте запись в `/etc/fstab`:

```bash
echo '/dev/sda1 /mnt/autopilot_ssd ext4 defaults,noatime 0 2' | sudo tee -a /etc/fstab
```

Проверьте правильность записи:

```bash
cat /etc/fstab
```

Перезагрузите систему и проверьте, что диск смонтировался автоматически:

```bash
sudo reboot
df -h | grep autopilot_ssd
```

> ✅ **Успех**: Вы должны увидеть строку с точкой монтирования `/mnt/autopilot_ssd`.

### Шаг 4: Клонирование репозитория

```bash
cd /mnt/autopilot_ssd
git clone https://github.com/yourusername/autopilot-content.git autopilot-content
cd autopilot-content
```

### Шаг 5: Создание виртуального окружения

```bash
python3 -m venv venv
source venv/bin/activate
```

> ⚠️ **Важно**: Используйте `python3`, а не `python`, если у вас несколько версий Python.

### Шаг 6: Установка зависимостей

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

> ⚠️ **Внимание**: На слабых устройствах установка может занять 5-10 минут. Некоторые пакеты могут требовать компиляции.

Если возникнут ошибки с компиляцией, установите дополнительные пакеты:

```bash
sudo apt install build-essential libssl-dev libffi-dev python3-dev libjpeg-dev zlib1g-dev -y
```

### Шаг 7: Настройка переменных окружения (.env)

```bash
cp .env.example .env
nano .env
```

Заполните переменные для Banana Pi:

```ini
# Ollama Configuration (указываем IP ноутбука)
OLLAMA_URL=http://<IP_НОУТБУКА>:11434
OLLAMA_MODEL=qwen2.5:14b
OLLAMA_TIMEOUT=120

# GigaChat Configuration (если используется)
GIGACHAT_KEY=your_gigachat_key
GIGACHAT_SECRET=your_gigachat_secret

# Database (путь на внешнем SSD)
DB_PATH=sqlite:////mnt/autopilot_ssd/data/database.db

# VK Configuration
VK_TOKEN=vk1.a.ABC123xyz789...
VK_GROUP_ID=123456789

# Application
LOG_LEVEL=INFO
MEDIA_FOLDER=/mnt/autopilot_ssd/data/media
UPLOAD_FOLDER=/mnt/autopilot_ssd/data/uploads
LOG_FILE=/mnt/autopilot_ssd/logs/app.log
```

> ⚠️ **Критично важно**:
> - `DB_PATH` должен указывать на внешний SSD для надёжности
> - `OLLAMA_URL` должен указывать на IP-адрес вашего ноутбука в локальной сети
> - Убедитесь, что ноутбук и Banana Pi находятся в одной сети

### Шаг 8: Создание необходимых папок

```bash
mkdir -p /mnt/autopilot_ssd/data/media
mkdir -p /mnt/autopilot_ssd/data/uploads
mkdir -p /mnt/autopilot_ssd/logs
```

Настройте права доступа:

```bash
chmod 755 /mnt/autopilot_ssd/data
chmod 755 /mnt/autopilot_ssd/logs
```

### Шаг 9: Инициализация базы данных

```bash
python scripts/setup_db.py
```

> ✅ **Успех**: Сообщение об успешном создании таблиц.

### Шаг 10: Настройка systemd-сервиса для автозапуска

Создайте файл сервиса:

```bash
sudo nano /etc/systemd/system/autopilot.service
```

Вставьте следующее содержимое:

```ini
[Unit]
Description=Autopilot Content Service
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=pi
Group=pi
WorkingDirectory=/mnt/autopilot_ssd/autopilot-content
ExecStart=/mnt/autopilot_ssd/autopilot-content/venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=10
EnvironmentFile=/mnt/autopilot_ssd/autopilot-content/.env
StandardOutput=append:/mnt/autopilot_ssd/logs/service.log
StandardError=append:/mnt/autopilot_ssd/logs/service_error.log

# Limits
LimitNOFILE=65535
Nice=-5

[Install]
WantedBy=multi-user.target
```

> ⚠️ **Важно**: Замените `User=pi` на ваше имя пользователя, если оно отличается:
> ```bash
> whoami
> ```

#### Активация сервиса

```bash
# Перезагрузить конфигурацию systemd
sudo systemctl daemon-reload

# Включить автозапуск при загрузке
sudo systemctl enable autopilot.service

# Запустить сервис
sudo systemctl start autopilot.service

# Проверить статус
sudo systemctl status autopilot.service
```

> ✅ **Успех**: Вы должны увидеть `Active: active (running)`.

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

#### 🔐 Авторизация

При первом посещении вам будет предложено войти через ВКонтакте:

1. Нажмите кнопку **"Войти через ВКонтакте"**
2. Разрешите приложению доступ к вашему профилю и сообществам
3. После авторизации вы увидите своё имя и аватар в шапке сайта

> ⚠️ **Важно**: Доступ к генерации постов имеют только пользователи, которые являются **администраторами** хотя бы одного сообщества ВКонтакте.

#### 👥 Управление сообществами

После авторизации в шапке появится панель выбора сообщества:

1. **Выбор сообщества**: Выпадающий список со всеми вашими сообществами, где вы администратор
2. **Добавить сообщество**: Кнопка "+ Добавить сообщество" открывает модальное окно

##### Как добавить новое сообщество:

1. Нажмите **"+ Добавить сообщество"**
2. Введите **ID сообщества** (можно узнать в настройках группы VK)
3. Введите **токен доступа** сообщества
   - Как получить токен: создайте Standalone-приложение в [VK Developers](https://dev.vk.com/)
   - Запросите права: `groups,wall,photos`
   - Используйте полученный токен
4. Система автоматически проверит, что вы являетесь администратором
5. Нажмите **"Добавить"**

> ✅ При успехе сообщество появится в выпадающем списке

#### 📊 Интерфейс панели управления

После выбора сообщества становятся доступны:

1. **Кнопка генерации**: "✨ Сгенерировать пак на неделю" — создаёт 7 постов
2. **Таблица постов**:
   | ID | Тема | Тип | Статус | Изображение | Действия |
   |----|------|-----|--------|-------------|----------|
   | 1 | Как мыть 3D-формочки | польза | draft | ✓ Есть | [Одобрить] |
   | 2 | Тест формочек | вовлечение | approved | ✓ Есть | [В ВК] |
   | 3 | Скидка 20% | продажа | published | ✗ Нет | Готово |

3. **Статистика**: 4 карточки сверху показывают количество постов по статусам

#### Примеры действий:

- **Генерация контента**: Нажмите "Сгенерировать пак на неделю" → система создаст 7 постов разных типов (польза, вовлечение, развлечение, продажа)
- **Одобрение поста**: Для черновика нажмите "Одобрить" → статус изменится на `approved`, пост готов к публикации
- **Публикация**: Для одобренного поста нажмите "В ВК" → пост будет опубликован в выбранном сообществе
- **Переключение между сообществами**: Выберите другое сообщество в выпадающем списке → все действия будут применяться к нему

---

### VK-бот

Бот работает через сообщения сообщества ВКонтакте. Чтобы начать, добавьте бота в сообщество и дайте права на отправку сообщений.

#### Как начать работу:

1. Откройте сообщество ВКонтакте
2. Нажмите "Написать сообщение"
3. Отправьте команду: `Старт` или `/start`

#### Доступные команды:

| Команда | Алиас | Описание | Пример ответа |
|---------|-------|----------|---------------|
| `Старт` | `/start` | Приветствие и список команд | "Привет! Я бот Autopilot Content. Доступные команды:..." |
| `Статус` | `/status` | Статистика постов по статусам | "📊 Статус:<br>Draft: 5<br>Approved: 2<br>Published: 12" |
| `Сгенерировать` | `/generate` | Запуск генерации недельного пака | "🚀 Запускаю генерацию пака на неделю...<br>✅ Готово! Создано 7 постов." |
| `Одобрить` | `/approve` | Показ последнего draft + кнопки | "📝 Последний черновик:<br>[Текст поста]<br>[Изображение]<br>Одобрить?" |

#### Пример диалога:

```
Вы: Старт

Бот: 👋 Привет! Я бот Autopilot Content.

Доступные команды:
• Статус — показать статистику постов
• Сгенерировать — создать пак на неделю
• Одобрить — просмотреть и одобрить черновик

[Клавиатура с кнопками]
```

```
Вы: Статус

Бот: 📊 Статистика постов:

📝 Draft (черновики): 5
✅ Approved (одобрено): 2
📢 Published (опубликовано): 12

Всего постов: 19
```

```
Вы: Одобрить

Бот: 📝 Последний черновик:

Тема: Как мыть 3D-формочки после использования
Тип: польза

Текст:
"Правильный уход за 3D-формочками продлит их срок службы...
[полный текст поста]

[Изображение прикреплено]

Одобрить этот пост для публикации?

[Клавиатура: ✅ Одобрить | ❌ Отклонить]
```

> ⚠️ **Если бот не отвечает**:
> 1. Проверьте, включены ли "Сообщения сообщества" в настройках группы
> 2. Убедитесь, что у бота есть права на отправку сообщений
> 3. Проверьте токен в файле `.env`
> 4. Посмотрите логи: `sudo journalctl -u autopilot.service -f`

---

### CLI-скрипты

Скрипты находятся в папке `scripts/` и предназначены для автоматизации рутинных задач.

#### 1. `generate_post.py` — Генерация одиночного поста

**Описание**: Создаёт один пост с заданной темой, типом и моделью генерации.

**Аргументы**:
- `--topic` (обязательный): Тема поста
- `--type` (опционально, по умолчанию "польза"): Тип поста (польза/вовлечение/развлечение/продажа)
- `--model` (опционально, по умолчанию "ollama"): Модель для генерации (ollama/gigachat)

**Примеры использования**:

```bash
# Генерация поста о мойке формочек
python scripts/generate_post.py --topic "Как мыть 3D-формочки" --type "польза"

# Генерация развлекательного поста через GigaChat
python scripts/generate_post.py --topic "Смешные случаи с печеньем" --type "развлечение" --model "gigachat"

# Генерация продающего поста
python scripts/generate_post.py --topic "Новогодняя скидка 20%" --type "продажа"
```

**Пример вывода**:

```
[INFO] Генерация поста на тему: Как мыть 3D-формочки
[INFO] Используемая модель: ollama (qwen2.5:14b)
[INFO] Текст сгенерирован успешно (длина: 450 символов)
[INFO] Изображение сгенерировано: data/media/post_123.png
[INFO] Пост сохранён в БД с ID: 123, статус: draft
✅ Пост создан!
   ID: 123
   Тема: Как мыть 3D-формочки
   Тип: польза
   Статус: draft
   Изображение: data/media/post_123.png
```

---

#### 2. `generate_weekly.py` — Генерация недельного пака

**Описание**: Создаёт 7 постов на неделю с разнообразными темами и типами.

**Аргументы**:
- `--niche` (опционально, по умолчанию "3d_cookie_cutters"): Ниша для генерации
- `--model` (опционально, по умолчанию "ollama"): Модель для генерации

**Примеры использования**:

```bash
# Генерация пака для ниши 3D-формочки
python scripts/generate_weekly.py --niche "3d_cookie_cutters"

# Генерация пака для другой ниши
python scripts/generate_weekly.py --niche "handmade_jewelry" --model "gigachat"

# Генерация с моделью по умолчанию
python scripts/generate_weekly.py
```

**Пример вывода**:

```
[INFO] Запуск генерации недельного пака для ниши: 3d_cookie_cutters
[INFO] Используемая модель: ollama (qwen2.5:14b)
[INFO] Генерация поста 1/7... ✅
[INFO] Генерация поста 2/7... ✅
[INFO] Генерация поста 3/7... ✅
[INFO] Генерация поста 4/7... ✅
[INFO] Генерация поста 5/7... ✅
[INFO] Генерация поста 6/7... ✅
[INFO] Генерация поста 7/7... ✅

📦 Недельный пак готов!

Сгенерированные посты:
┌────┬──────────────────────────────────┬─────────────┬────────┐
│ ID │ Тема                             │ Тип         │ Статус │
├────┼──────────────────────────────────┼─────────────┼────────┤
│ 10 │ Как мыть 3D-формочки             │ польза      │ draft  │
│ 11 │ Тест формочек на прочность       │ вовлечение  │ draft  │
│ 12 │ История 3D-печати                │ развлечение │ draft  │
│ 13 │ Скидка 20% до конца недели       │ продажа     │ draft  │
│ 14 │ Идеи для новогоднего печенья     │ польза      │ draft  │
│ 15 │ Ваше любимое печенье?            │ вовлечение  │ draft  │
│ 16 │ Закулисье нашей мастерской       │ развлечение │ draft  │
└────┴──────────────────────────────────┴─────────────┴────────┘

Всего создано: 7 постов
```

---

#### 3. `publish_pending.py` — Публикация одобренных постов

**Описание**: Находит все посты со статусом `approved` и публикует их ВКонтакте.

**Аргументы**: Нет (публикует все одобренные посты).

**Пример использования**:

```bash
python scripts/publish_pending.py
```

**Пример вывода**:

```
[INFO] Поиск одобренных постов для публикации...
[INFO] Найдено 3 поста со статусом 'approved'

📢 Публикация постов:

Пост ID: 11
  Тема: Тест формочек на прочность
  Статус: Отправка в ВК...
  ✅ Успешно опубликован! Post ID в ВК: 567890
  Статус в БД обновлён: published

Пост ID: 14
  Тема: Идеи для новогоднего печенья
  Статус: Отправка в ВК...
  ✅ Успешно опубликован! Post ID в ВК: 567891
  Статус в БД обновлён: published

Пост ID: 15
  Тема: Ваше любимое печенье?
  Статус: Отправка в ВК...
  ❌ Ошибка: Превышен лимит запросов
  Статус в БД: approved (попытка позже)

═══════════════════════════════════════
Итоги:
  Успешно: 2
  Ошибки: 1
═══════════════════════════════════════
```

---

#### 4. `setup_db.py` — Инициализация базы данных

**Описание**: Создаёт таблицы БД и опционально добавляет тестовые данные.

**Аргументы**:
- `--with-test-data` (флаг): Добавить тестовые посты для проверки

**Примеры использования**:

```bash
# Только создание таблиц
python scripts/setup_db.py

# Создание таблиц + тестовые данные
python scripts/setup_db.py --with-test-data
```

**Пример вывода**:

```
[INFO] Инициализация базы данных...
[INFO] Подключение к БД: sqlite:///data/database.db
[INFO] Создание таблицы: posts
[INFO] Создание таблицы: settings
[INFO] Создание таблицы: logs
[INFO] Таблицы успешно созданы.

✅ База данных готова к работе!
```

С флагом `--with-test-data`:

```
[INFO] Добавление тестовых данных...
[INFO] Создан тестовый пост ID: 1 (draft)
[INFO] Создан тестовый пост ID: 2 (approved)
[INFO] Создан тестовый пост ID: 3 (published)
[INFO] Тестовые данные добавлены.

✅ База данных готова к работе!
   Тестовых постов: 3
```

---

## 🌐 API Endpoints

Проект предоставляет REST API для интеграции с внешними системами.

### Базовый URL

```
http://<IP_BANANA_PI>:8000/api
```

### Документация API

Полная интерактивная документация доступна по адресу:

```
http://<IP_BANANA_PI>:8000/docs
```

Это Swagger UI с возможностью тестирования запросов прямо в браузере.

### Таблица endpoints

| Метод | Путь | Описание | Параметры | Пример запроса | Пример ответа |
|-------|------|----------|-----------|----------------|---------------|
| `GET` | `/` | Веб-интерфейс | - | `GET /` | HTML-страница |
| `POST` | `/api/generate/weekly` | Генерация пака на неделю | `niche` (str), `model` (str) | `{"niche": "3d_cookie_cutters"}` | `{"status": "success", "posts_count": 7}` |
| `GET` | `/api/posts` | Список всех постов | `status` (query), `limit` (query) | `GET /api/posts?status=draft&limit=10` | `[{"id": 1, "topic": "...", "status": "draft"}, ...]` |
| `GET` | `/api/posts/{id}` | Получение поста по ID | `id` (path) | `GET /api/posts/123` | `{"id": 123, "topic": "...", "text": "...", "image_path": "..."}` |
| `POST` | `/api/posts/{id}/approve` | Одобрение поста | `id` (path), `publish_at` (body) | `{"publish_at": "2025-01-15T10:00:00"}` | `{"status": "success", "message": "Post approved"}` |
| `POST` | `/api/posts/{id}/reject` | Отклонение поста | `id` (path) | `POST /api/posts/123/reject` | `{"status": "success", "message": "Post rejected"}` |
| `DELETE` | `/api/posts/{id}` | Удаление поста | `id` (path) | `DELETE /api/posts/123` | `{"status": "success", "message": "Post deleted"}` |
| `POST` | `/api/publish/vk/{id}` | Ручная публикация в ВК | `id` (path) | `POST /api/publish/vk/123` | `{"status": "success", "vk_post_id": 567890}` |
| `GET` | `/api/stats` | Статистика по постам | - | `GET /api/stats` | `{"draft": 5, "approved": 2, "published": 12}` |

### Примеры запросов cURL

#### Генерация недельного пака:

```bash
curl -X POST http://localhost:8000/api/generate/weekly \
  -H "Content-Type: application/json" \
  -d '{"niche": "3d_cookie_cutters", "model": "ollama"}'
```

Ответ:

```json
{
  "status": "success",
  "message": "Weekly pack generated successfully",
  "posts_count": 7,
  "posts": [
    {"id": 10, "topic": "Как мыть 3D-формочки", "status": "draft"},
    {"id": 11, "topic": "Тест формочек на прочность", "status": "draft"},
    ...
  ]
}
```

#### Получение списка постов:

```bash
curl -X GET "http://localhost:8000/api/posts?status=draft&limit=10"
```

Ответ:

```json
[
  {
    "id": 10,
    "topic": "Как мыть 3D-формочки",
    "type": "польза",
    "status": "draft",
    "created_at": "2025-01-14T10:00:00",
    "publish_at": null
  },
  {
    "id": 11,
    "topic": "Тест формочек на прочность",
    "type": "вовлечение",
    "status": "draft",
    "created_at": "2025-01-14T10:05:00",
    "publish_at": null
  }
]
```

#### Одобрение поста:

```bash
curl -X POST http://localhost:8000/api/posts/10/approve \
  -H "Content-Type: application/json" \
  -d '{"publish_at": "2025-01-15T10:00:00"}'
```

Ответ:

```json
{
  "status": "success",
  "message": "Post approved successfully",
  "post_id": 10,
  "publish_at": "2025-01-15T10:00:00"
}
```

#### Ручная публикация в ВК:

```bash
curl -X POST http://localhost:8000/api/publish/vk/11
```

Ответ:

```json
{
  "status": "success",
  "message": "Post published to VK",
  "vk_post_id": 567890,
  "post_id": 11
}
```

---

## 📁 Структура проекта

```
autopilot-content/
├── app.py                      # Главный файл приложения (FastAPI + lifespan)
├── config.py                   # Конфигурация и переменные окружения
├── database.py                 # Настройка SQLAlchemy и сессий БД
├── models.py                   # SQLAlchemy модели (Post, Setting, Log)
├── schemas.py                  # Pydantic схемы для валидации данных
├── requirements.txt            # Зависимости Python
├── .env.example                # Шаблон переменных окружения
├── .gitignore                  # Игнорируемые файлы Git
├── README.md                   # Эта документация
│
├── bots/                       # Модуль ботов
│   ├── __init__.py             # Пустой файл пакета
│   └── vk_bot.py               # VK-бот (LongPoll, команды, клавиатуры)
│
├── generators/                 # Модуль генерации контента
│   ├── __init__.py             # Пустой файл пакета
│   ├── text_generator.py       # Генерация текста (Ollama, GigaChat)
│   └── image_generator.py      # Генерация изображений (Kandinsky API)
│
├── publishers/                 # Модуль публикации
│   ├── __init__.py             # Пустой файл пакета
│   └── vk_publisher.py         # Публикация ВКонтакте (текст + изображение)
│
├── services/                   # Бизнес-логика
│   ├── __init__.py             # Пустой файл пакета
│   ├── content_service.py      # Сервис генерации недельных паков
│   └── scheduler.py            # APScheduler для автопубликации
│
├── scripts/                    # CLI-скрипты
│   ├── __init__.py             # Пустой файл пакета
│   ├── generate_post.py        # Генерация одиночного поста
│   ├── generate_weekly.py      # Генерация недельного пака
│   ├── publish_pending.py      # Публикация одобренных постов
│   └── setup_db.py             # Инициализация базы данных
│
├── data/                       # Данные (хранятся на внешнем SSD)
│   ├── database.db             # SQLite база данных
│   ├── media/                  # Сгенерированные изображения
│   └── uploads/                # Загруженные пользователем файлы
│
└── logs/                       # Логи приложения
    ├── app.log                 # Основной лог
    ├── service.log             # Лог systemd-сервиса
    └── service_error.log       # Лог ошибок systemd-сервиса
```

### Краткое описание файлов:

| Файл/Папка | Назначение |
|------------|------------|
| `app.py` | Точка входа FastAPI, настройка lifespan, роуты API |
| `config.py` | Чтение `.env`, класс Settings с валидацией |
| `database.py` | Engine, SessionLocal, init_db() |
| `models.py` | Классы Post, Setting, Log (таблицы БД) |
| `schemas.py` | Pydantic-модели для request/response |
| `bots/vk_bot.py` | VK-бот: LongPoll, обработка команд, клавиатуры |
| `generators/text_generator.py` | Генерация текста через Ollama/GigaChat |
| `generators/image_generator.py` | Генерация изображений через Kandinsky |
| `publishers/vk_publisher.py` | Отправка постов в ВКонтакте |
| `services/content_service.py` | Высокоуровневая логика генерации паков |
| `services/scheduler.py` | APScheduler: задача check_and_publish() |
| `scripts/*.py` | CLI-утилиты для ручного управления |

---

## ❓ FAQ (Частые вопросы)

### 1. Как добавить новую площадку (ОК, MAX)?

Для добавления новой социальной сети создайте новый файл в папке `publishers/` по аналогии с `vk_publisher.py`.

**Пример структуры для OK.ru:**

```python
# publishers/ok_publisher.py
import logging
from typing import Optional

logger = logging.getLogger(__name__)

class OKPublisher:
    def __init__(self, api_key: str, app_key: str, secret_key: str):
        self.api_key = api_key
        self.app_key = app_key
        self.secret_key = secret_key
    
    def publish(self, text: str, image_path: Optional[str] = None) -> int:
        """
        Публикация поста в ОК.
        
        :param text: Текст поста
        :param image_path: Путь к изображению (опционально)
        :return: ID поста в ОК
        """
        logger.info(f"Публикация в ОК: {text[:50]}...")
        
        # TODO: Реализовать логику публикации через OK API
        # 1. Загрузка изображения (если есть)
        # 2. Отправка текста с изображением
        # 3. Возврат ID поста
        
        post_id = 123456  # заглушка
        logger.info(f"Пост опубликован в ОК с ID: {post_id}")
        return post_id
```

Затем импортируйте класс в `app.py` или `services/scheduler.py` и используйте аналогично `VKPublisher`.

---

### 2. Как изменить модель для генерации текста?

Есть три способа:

#### Способ 1: Через `.env` (постоянно)

Откройте файл `.env` и измените переменную:

```ini
OLLAMA_MODEL=qwen2.5:7b
```

Перезапустите приложение:

```bash
sudo systemctl restart autopilot.service
```

#### Способ 2: Через CLI-скрипт (разово)

```bash
python scripts/generate_post.py --topic "Тема" --model "gigachat"
```

#### Способ 3: Через API (программно)

```bash
curl -X POST http://localhost:8000/api/generate/weekly \
  -H "Content-Type: application/json" \
  -d '{"niche": "3d_cookie_cutters", "model": "llama3:8b"}'
```

**Доступные модели:**
- `qwen2.5:14b` — баланс качества и скорости (рекомендуется)
- `qwen2.5:7b` — быстрая, меньше ресурсов
- `llama3:8b` — альтернативная модель Meta
- `gigachat` — облачная модель Сбербанк (требует API-ключи)

---

### 3. Бот не отвечает в ВК. Что делать?

**Чеклист для диагностики:**

1. **Проверьте токен**:
   ```bash
   cat .env | grep VK_TOKEN
   ```
   Убедитесь, что токен начинается с `vk1.a.` и не содержит пробелов.

2. **Проверьте ID группы**:
   - ID должен быть числом (без минуса)
   - Узнать ID можно через [reg.ru](https://reg.ru/domain/name-server) или отправив запрос:
     ```bash
     curl "https://api.vk.com/method/groups.getById?group_ids=<ваш_shortname>&access_token=<токен>&v=5.131"
     ```

3. **Проверьте права бота**:
   - Зайдите в управление сообществом → Настройки → Работа с API
   - Убедитесь, что включены "Сообщения сообщества"
   - Дайте боту права: "Чтение сообщений", "Отправка сообщений"

4. **Проверьте логи**:
   ```bash
   sudo journalctl -u autopilot.service -f
   ```
   Ищите ошибки типа `LongPollError`, `ApiError`, `AuthError`.

5. **Перезапустите сервис**:
   ```bash
   sudo systemctl restart autopilot.service
   ```

6. **Проверьте подключение к интернету**:
   ```bash
   ping api.vk.com
   ```

---

### 4. Как посмотреть логи?

Есть несколько способов:

#### Способ 1: Логи systemd-сервиса (рекомендуется)

```bash
# Логи в реальном времени
sudo journalctl -u autopilot.service -f

# Последние 100 строк
sudo journalctl -u autopilot.service -n 100

# Логи за сегодня
sudo journalctl -u autopilot.service --since today

# Логи за конкретную дату
sudo journalctl -u autopilot.service --since "2025-01-15 00:00:00" --until "2025-01-15 23:59:59"
```

#### Способ 2: Файлы логов приложения

```bash
# Основной лог
tail -f /mnt/autopilot_ssd/logs/app.log

# Лог сервиса
tail -f /mnt/autopilot_ssd/logs/service.log

# Лог ошибок сервиса
tail -f /mnt/autopilot_ssd/logs/service_error.log
```

#### Способ 3: Через веб-интерфейс

Откройте `http://<IP_BANANA_PI>:8000` и перейдите на вкладку "Логи" (если реализовано).

---

### 5. Как сделать резервную копию базы данных?

```bash
# Копирование файла БД
cp /mnt/autopilot_ssd/data/database.db /mnt/autopilot_ssd/backups/database_$(date +%Y%m%d).db

# Автоматизация через cron (ежедневно в 3:00)
crontab -e
```

Добавьте строку:

```
0 3 * * * cp /mnt/autopilot_ssd/data/database.db /mnt/autopilot_ssd/backups/database_$(date +\%Y\%m\%d).db
```

---

### 6. Как обновить проект?

```bash
# Остановка сервиса
sudo systemctl stop autopilot.service

# Переход в директорию проекта
cd /mnt/autopilot_ssd/autopilot-content

# Активация venv
source venv/bin/activate

# Pull изменений из репозитория
git pull origin main

# Установка новых зависимостей (если есть)
pip install -r requirements.txt

# Миграция БД (если есть изменения в моделях)
python scripts/setup_db.py

# Запуск сервиса
sudo systemctl start autopilot.service

# Проверка статуса
sudo systemctl status autopilot.service
```

---

## 📄 Лицензия и контакты

### Лицензия

Этот проект распространяется под лицензией MIT. Вы можете свободно использовать, изменять и распространять код в соответствии с условиями лицензии.

Текст лицензии доступен в файле `LICENSE` в корне репозитория.

### Контакты

- **Разработчик**: Your Name
- **Email**: your.email@example.com
- **Telegram**: @yourusername
- **GitHub**: [github.com/yourusername](https://github.com/yourusername)

### Поддержка проекта

Если проект оказался полезным, пожалуйста:
- ⭐ Поставьте звезду на GitHub
- 🐛 Сообщайте о багах через Issues
- 💡 Предлагайте улучшения через Discussions
- 🔀 Отправляйте Pull Request с исправлениями

---

## 🎯 Заключение

**Autopilot Content** — это готовое решение для автоматизации ведения социальных сетей. Система экономит часы времени на создание контента, обеспечивает регулярность публикаций и позволяет сосредоточиться на развитии бизнеса, а не на рутинных задачах.

**Что вы получаете:**
- ✅ Автономную генерацию контента (текст + изображения)
- ✅ Гибкое планирование публикаций
- ✅ Управление через VK-бота, веб-интерфейс или CLI
- ✅ Масштабируемую архитектуру (ноутбук + Banana Pi)
- ✅ Полную документацию и поддержку

**Следующие шаги:**
1. Установите проект на ноутбук и Banana Pi по инструкциям выше
2. Настройте переменные окружения в `.env`
3. Запустите генерацию первого недельного пака
4. Одобрите посты и наблюдайте за автоматической публикацией

🚀 **Успешного запуска!**

---

*Документация актуальна на январь 2025 года. Версия проекта: 1.0.0*
