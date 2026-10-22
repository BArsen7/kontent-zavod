"""Смоук-тест НОВОГО сценария онбординга: выбор сообщества -> фоновый парсинг
(VK + AI-паспорт + MarketingStrategy) -> вопросы только по недостающим полям
с кнопками -> сохранение CommunityGenerationContext -> редирект ?onboarded=1.

Внешние сети (VK API, LLM) полностью замоканы.
"""
import os, tempfile, json

TMP = tempfile.mkdtemp()
db_file = os.path.join(TMP, "test_ob_smart.db")
os.environ["DATABASE_PATH"] = db_file
os.environ.setdefault("DATABASE_URL", "")

import database
database.DB_FILE = db_file
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
new_engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
database.engine = new_engine
database.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=new_engine)
models.Base.metadata.create_all(bind=new_engine)

# --- Мок LLM: никогда не обращаться к Ollama/GigaChat ----------------------
import generators.text_generator as tg
LLM_CALLS = {"n": 0}
def fake_generate_text(prompt, system_prompt="", use_local=True):
    LLM_CALLS["n"] += 1
    if "Извлеки из диалога" in prompt:
        return '{"community_name": "Уютная Пекарня", "goal": null, "specifics": null, "audience": "Женщины 25-45"}'
    return "А какая главная цель ведения сообщества?"
tg.generate_text = fake_generate_text
import services.onboarding_service as ob
ob.generate_text = fake_generate_text

# --- Мок VK API: без реальных сетевых вызовов ------------------------------
import services.vk_analytics_service as vka
vka.get_group_info = lambda token, group_id, fields="": {
    "name": "Уютная Пекарня", "description": "Печём торты на заказ в Москве.",
    "members_count": 1200,
}
_orig_import_history = vka.import_history
vka.import_history = lambda community_id, limit=50, db=None: 7

import app as app_module
app_module.init_db = lambda: None
from fastapi.testclient import TestClient
client = TestClient(app_module.app)

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)

# Регистрация + создание пользователя с реальным (мок) сообществом VK
resp = client.post("/auth/register", data={
    "email": "baker@test.ru", "password": "Secret123!",
    "first_name": "Анна", "last_name": "Пекарь"}, follow_redirects=False)
check("register redirects to /onboarding", resp.status_code == 302 and resp.headers["location"] == "/onboarding")

sess = database.SessionLocal()
u = sess.query(models.User).filter(models.User.email == "baker@test.ru").first()
uc = models.UserCommunity(user_id=u.id, group_id=-123456, group_name="Уютная Пекарня",
                          group_token="tok-test", is_admin=True, can_post=True)
sess.add(uc); sess.commit(); sess.refresh(uc)
pk = uc.id
sess.close()

# GET /onboarding — страница с выпадающим списком сообществ
r = client.get("/onboarding")
html = r.text
check("page renders with community select", r.status_code == 200 and 'id="obCommunity"' in html)
check("select contains community", "Уютная Пекарня" in html)
check("buttons UI present in template", "renderButtons" in html and "ob-btn" in html)
check("parsing indicator present", 'id="obParsing"' in html or "Анализирую" in html)
check("polling logic present", "parse-status" in html)
# До выбора сообщества чат пуст: статичное приветствие в чате отсутствует,
# поле ввода скрыто до нажатия «Анализировать».
check("no greeting before community selected",
      "Привет! Я твой ИИ-контент менеджер" not in html and "setInputVisible(false)" in html)

# ШАГ 1: приветствие больше НЕ запрашивается у бэкенда до выбора сообщества —
# страница с выпадающим списком показывает пустой чат, и консьерж «заговаривает»
# только после нажатия «Анализировать →» (см. onboarding.html).

# ШАГ 2: выбор сообщества -> запуск парсинга
r = client.post("/api/onboarding/select-community", json={"community_id": pk})
d = r.json()
check("select-community accepted", r.status_code == 200 and d.get("task_id"))
check("select-community returns name", d.get("community_name") == "Уютная Пекарня")
task_id = d["task_id"]

# Polling статуса парсинга
r = client.post("/api/onboarding/chat", json={"message": ""})
if r.json().get("status") == "parsing":
    task_id = r.json()["parsing_task_id"]
    rr = client.get(f"/api/onboarding/parse-status/{task_id}")
    check("parse-status endpoint works", rr.status_code == 200 and rr.json()["parsed_status"] in ("done", "error", "parsing"))

# Следующий шаг чата должен дать вопрос с кнопками (умный режим)
r = client.post("/api/onboarding/chat", json={"message": ""})
d = r.json()
check("smart mode question with buttons", d["status"] == "question" and isinstance(d.get("buttons"), list) and len(d["buttons"]) >= 4)
check("question targets goal field", d.get("field") == "goal")
check("progress indicator present", isinstance(d.get("progress"), dict) and d["progress"]["index"] == 1)
check("custom button included", any(b.get("custom") for b in d["buttons"]))

# Ответ кликом по кнопке (значение подставлено из кнопки) → следующий вопрос
goal_value = next(b["value"] for b in d["buttons"] if not b.get("custom"))
r = client.post("/api/onboarding/chat", json={"message": goal_value})
d = r.json()
check("second question after answer", d["status"] == "question" and d.get("field") == "specifics")
check("progress advanced", d["progress"]["index"] == 2)

spec_value = next(b["value"] for b in d["buttons"] if not b.get("custom"))
r = client.post("/api/onboarding/chat", json={"message": spec_value})
d = r.json()
check("third question current_state", d.get("field") == "current_state")
cs_value = next(b["value"] for b in d["buttons"] if not b.get("custom"))
r = client.post("/api/onboarding/chat", json={"message": cs_value})
d = r.json()
check("fourth question image_preferences", d.get("field") == "image_preferences")
img_btn = next(b for b in d["buttons"] if not b.get("custom") and b.get("sources"))
r = client.post("/api/onboarding/chat", json={"message": img_btn["value"]})
d = r.json()

# ШАГ 5: финализация
check("configured status after all answers", d.get("status") == "configured")
ctx = d.get("context") or {}
check("goal saved", ctx.get("goal") == goal_value)
check("specifics saved", ctx.get("community_specifics") == spec_value)
check("current_state saved", ctx.get("current_state_and_what_works") == cs_value)
prefs = json.loads(ctx.get("image_source_preferences") or "[]")
check("image prefs saved as JSON list", set(img_btn["sources"]).issubset(set(prefs)))
check("target_audience from parsed VK passport", bool(ctx.get("target_audience")))
check("redirect url onboarded=1", "onboarded=1" in d.get("redirect_url", ""))

# Проверка БД
sess = database.SessionLocal()
cgc = sess.query(models.CommunityGenerationContext).filter(
    models.CommunityGenerationContext.user_community_id == pk).first()
check("DB context is_configured True", cgc is not None and cgc.is_configured is True)
acc = sess.query(models.PlatformAccount).filter(models.PlatformAccount.account_id == "123456").first()
check("PlatformAccount ensured", acc is not None)
cc = sess.query(models.CommunityContext).filter(models.CommunityContext.community_id == acc.id).first() if acc else None
check("CommunityContext created from onboarding data", cc is not None)
sess.close()

# После настройки контент-менеджер доступен
r = client.get("/content-manager", follow_redirects=False)
check("content-manager open after smart onboarding", r.status_code == 200)

# Fallback: ошибка VK API не роняет онбординг
def broken_info(token, group_id, fields=""):
    raise RuntimeError("VK API недоступен")
vka.get_group_info = broken_info
vka.import_history = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no wall access"))
sess = database.SessionLocal()
uc2 = models.UserCommunity(user_id=u.id, group_id=-999, group_name="Новое Сообщество",
                           group_token="tok2", is_admin=True, can_post=True)
sess.add(uc2); sess.commit(); sess.refresh(uc2)
pk2 = uc2.id
sess.close()
client.post("/api/onboarding/chat", json={"message": "", "reset": True})
r = client.post("/api/onboarding/select-community", json={"community_id": pk2})
check("select-community survives VK error", r.status_code == 200)
task_id2 = r.json()["task_id"]
deadline = 50
d2 = None
while deadline > 0:
    st = client.get(f"/api/onboarding/parse-status/{task_id2}").json()
    if st["parsed_status"] in ("done", "error"):
        break
    import time; time.sleep(0.1); deadline -= 1
r = client.post("/api/onboarding/chat", json={"message": ""})
d2 = r.json()
check("fallback still gives questions with buttons", d2.get("status") == "question" and d2.get("buttons"))

failed = [n for n, ok in results if not ok]
print("\nTOTAL:", len(results), "FAILED:", len(failed), failed)
raise SystemExit(1 if failed else 0)
