"""Смоук-тест AI-консьержа (онбординг): диалог -> извлечение JSON -> контекст."""
import os, tempfile, json
os.environ.setdefault("DATABASE_URL", "")

TMP = tempfile.mkdtemp()
db_file = os.path.join(TMP, "test_ob.db")
os.environ["DATABASE_PATH"] = db_file

import database
database.DB_FILE = db_file  # если проект использует константу
from database import engine
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
# Пересоздаём движок на временную БД до импорта app
import models
new_engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
database.engine = new_engine
database.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=new_engine)
models.Base.metadata.create_all(bind=new_engine)

import generators.text_generator as tg
CALLS = {"n": 0}
def fake_generate_text(prompt, system_prompt="", use_local=True):
    CALLS["n"] += 1
    if "Извлеки из диалога" in prompt:
        return '```json\n{"community_name": "Кофейня У Лены", "goal": "Привлекать клиентов", "specifics": null, "audience": "Молодежь 20-35"}\n```'
    return "Кто ваша целевая аудитория?"
tg.generate_text = fake_generate_text
import services.onboarding_service as ob
ob.generate_text = fake_generate_text

import bcrypt
from fastapi.testclient import TestClient
import app as app_module
app_module.init_db = lambda: None  # не трогаем боевую схему в тесте
client = TestClient(app_module.app)

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)

# Регистрация нового пользователя -> редирект на /onboarding
resp = client.post("/auth/register", data={
    "email": "newbie@test.ru", "password": "Secret123!",
    "first_name": "Лена", "last_name": "Тестова"}, follow_redirects=False)
check("register redirects to /onboarding", resp.status_code == 302 and resp.headers["location"] == "/onboarding")

# GET /onboarding — страница рендерится
r = client.get("/onboarding")
check("GET /onboarding 200 + greeting markup", r.status_code == 200 and "AI-консьерж" in r.text)

# content-manager для нового юзера -> авторедирект на онбординг
r = client.get("/content-manager", follow_redirects=False)
check("content-manager gates new user to /onboarding", r.status_code == 302 and r.headers["location"] == "/onboarding")
r = client.get("/content-manager?onboarded=1")
check("content-manager with onboarded=1 renders", r.status_code == 200)

# Статус: нужен онбординг
r = client.get("/api/onboarding/status")
check("status needs_onboarding=True", r.status_code == 200 and r.json()["needs_onboarding"] is True)

# Шаг 1: пустое сообщение -> приветствие
r = client.post("/api/onboarding/chat", json={"message": ""})
check("greeting exact text", r.status_code == 200 and r.json()["reply"].startswith("Привет! Я твой ИИ-контент менеджер"))

# Шаг 2: первый ответ -> LLM-вопрос (мок)
r = client.post("/api/onboarding/chat", json={"message": "Кофейня У Лены, варим вкусный кофе"})
check("concierge question step", r.status_code == 200 and r.json()["status"] == "question" and "аудитори" in r.json()["reply"])

# Шаг 3: второй ответ -> финализация (>= MIN_USER_TURNS)
r = client.post("/api/onboarding/chat", json={"message": "Молодежь 20-35, цель привлекать клиентов"})
data = r.json()
check("configured status", r.status_code == 200 and data.get("status") == "configured")
check("context saved from mock JSON", data.get("context", {}).get("goal") == "Привлекать клиентов"
      and data["context"]["target_audience"] == "Молодежь 20-35" and data["context"]["is_configured"] is True)
check("redirect_url has onboarded flag", "onboarded=1" in data.get("redirect_url", ""))

# Проверка БД: создано сообщество-заглушка + контекст
sess = database.SessionLocal()
from models import UserCommunity, CommunityGenerationContext, User
u = sess.query(User).filter(User.email == "newbie@test.ru").first()
uc = sess.query(UserCommunity).filter(UserCommunity.user_id == u.id).first()
check("placeholder community created", uc is not None and uc.group_name == "Кофейня У Лены" and uc.can_post is False)
ctx = sess.query(CommunityGenerationContext).filter(CommunityGenerationContext.user_community_id == uc.id).first()
check("ctx is_configured True in DB", ctx is not None and ctx.is_configured is True and ctx.goal == "Привлекать клиентов")
sess.close()

# Статус после настройки
r = client.get("/api/onboarding/status")
check("status needs_onboarding=False after setup", r.json()["needs_onboarding"] is False)

# Контент-менеджер больше не редиректит
r = client.get("/content-manager", follow_redirects=False)
check("content-manager open after onboarding", r.status_code == 200)

# Аутентификация эндпоинта
c2 = TestClient(app_module.app)  # без cookie
r = c2.post("/api/onboarding/chat", json={"message": "hi"})
check("chat requires auth (401)", r.status_code == 401)
r = c2.get("/api/onboarding/status")
check("status requires auth (401)", r.status_code == 401)

failed = [n for n, ok in results if not ok]
print("\nTOTAL:", len(results), "FAILED:", len(failed), failed)
raise SystemExit(1 if failed else 0)
