"""Smoke-тест Промпта 6: модалка назначения тарифа, bulk-эндпоинт, /api/tariff-info без перелогина."""
import os, sys, tempfile
import secrets as _s
os.environ["SECRET_KEY"] = _s.token_hex(16)
os.environ.pop("DATABASE_URL", None)
tmpdir = tempfile.mkdtemp()
os.environ["DB_PATH"] = f"{tmpdir}/t6.db"
sys.path.insert(0, "/workspace")

from database import SessionLocal, init_db
from models import User, TariffPlan
from fastapi.testclient import TestClient
import app as appmod
hash_password = appmod.hash_password

init_db()
db = SessionLocal()
admin = User(email="admin@test.ru", password_hash=hash_password("pass1234"), is_admin=True, is_active=True)
u1 = User(email="u1@test.ru", password_hash=hash_password("pass1234"), first_name="U1", is_active=True)
u2 = User(email="u2@test.ru", password_hash=hash_password("pass1234"), first_name="U2", is_active=True)
basic = TariffPlan(name="Базовый", cost=0.0, max_plan_horizon_days=7, max_rubrics=3,
                   max_posts_per_day=1, max_posts_per_week=7, max_posts_per_month=30,
                   max_communities=1, allows_images=False, max_images_per_post=1,
                   allowed_image_sources='[]')
pro = TariffPlan(name="Про", cost=990.0, max_plan_horizon_days=30, max_rubrics=10,
                 max_posts_per_day=3, max_posts_per_week=21, max_posts_per_month=90,
                 max_communities=5, allows_images=True, max_images_per_post=5,
                 allowed_image_sources='["ai","web_search","user_upload"]')
db.add_all([admin, u1, u2, basic, pro]); db.commit()
ids = {e: db.query(User).filter(User.email == e).first().id for e in
       ("admin@test.ru", "u1@test.ru", "u2@test.ru")}
plan_ids = {n: db.query(TariffPlan).filter(TariffPlan.name == n).first().id for n in ("Базовый", "Про")}
db.close()

client = TestClient(appmod.app)
fails = []
def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond: fails.append(name)

r = client.post("/auth/login", data={"email": "admin@test.ru", "password": "pass1234"}, follow_redirects=False)
check("login admin 302", r.status_code == 302)

# --- 1. GET /admin/users: модалка вместо dropdown, чекбоксы, toast, подсветка ---
html = client.get("/admin/users").text
check("page 200", client.get("/admin/users").status_code == 200)
check("modal #setTariffModal", 'id="setTariffModal"' in html and "modal-overlay" in html)
check("tariff cards markup", "tariff-card-premium" in html or "tariff-modal-cards" in html)
check("save/cancel buttons", 'id="tariffModalSave"' in html and 'id="tariffModalCancel"' in html)
check("bulk bar + checkboxes", 'id="bulkBar"' in html and 'id="checkAllUsers"' in html and "user-check" in html)
check("toast markup", 'id="toast"' in html and "showToast" in html)
check("row-flash animation", "row-flash" in html)
check("data-tariff-id on badges", 'data-tariff-id="none"' in html)
check("no old dropdown left", 'id="tariffDropdown"' not in html)
check("bulk endpoint referenced", "/admin/users/set-tariff-bulk" in html)

# --- 2. set-tariff: успех + валидный ответ для обновления DOM ---
r = client.post(f"/admin/users/{ids['u1@test.ru']}/set-tariff", json={"tariff_plan_id": plan_ids["Про"]})
j = r.json()
check("set-tariff success", r.status_code == 200 and j["success"] and j["tariff_name"] == "Про"
      and j["tariff_plan_id"] == plan_ids["Про"])

# --- 3. Безопасность: несуществующий тариф -> 400 (раньше был 404) ---
r = client.post(f"/admin/users/{ids['u1@test.ru']}/set-tariff", json={"tariff_plan_id": 99999})
check("bad tariff -> 400", r.status_code == 400 and "не найден" in r.json()["detail"])
r = client.post("/admin/users/99999/set-tariff", json={"tariff_plan_id": plan_ids["Базовый"]})
check("bad user -> 404", r.status_code == 404)

# --- 4. Массовое назначение ---
r = client.post("/admin/users/set-tariff-bulk",
                json={"user_ids": [ids["u1@test.ru"], ids["u2@test.ru"]], "tariff_plan_id": plan_ids["Базовый"]})
check("bulk 200 count=2", r.status_code == 200 and r.json()["updated_count"] == 2)
db = SessionLocal()
ok = all(db.query(User).get(uid).tariff_plan_id == plan_ids["Базовый"]
         for uid in (ids["u1@test.ru"], ids["u2@test.ru"]))
db.close()
check("bulk applied in DB", ok)
r = client.post("/admin/users/set-tariff-bulk", json={"user_ids": [], "tariff_plan_id": None})
check("bulk empty -> 400", r.status_code == 400)
r = client.post("/admin/users/set-tariff-bulk", json={"user_ids": [ids["u2@test.ru"]], "tariff_plan_id": 88888})
check("bulk bad tariff -> 400", r.status_code == 400)

# --- 5. Изменение тарифа СРАЗУ видно в /api/tariff-info (без перелогина) ---
c_user = TestClient(appmod.app)
c_user.post("/auth/login", data={"email": "u1@test.ru", "password": "pass1234"}, follow_redirects=False)
r = c_user.get("/api/tariff-info")
j = r.json()
check("tariff-info reflects Базовый immediately",
      r.status_code == 200 and j.get("tariff", {}).get("name") == "Базовый"
      and j.get("limits", {}).get("allows_images") in (False, 0))
# меняем на Про тем же админ-клиентом и проверяем мгновенное обновление у юзера без перелогина
client.post(f"/admin/users/{ids['u1@test.ru']}/set-tariff", json={"tariff_plan_id": plan_ids["Про"]})
r = c_user.get("/api/tariff-info")
j = r.json()
check("tariff-info reflects Про without re-login",
      j.get("tariff", {}).get("name") == "Про" and j.get("limits", {}).get("max_images_per_post") == 5)

# --- 6. Назначение тарифа самому себе разрешено и возвращает успех (логируется) ---
r = client.post(f"/admin/users/{ids['admin@test.ru']}/set-tariff", json={"tariff_plan_id": plan_ids["Про"]})
check("self-set allowed+logged", r.status_code == 200 and r.json()["success"])

# --- 7. Не-админ не может менять тарифы через API ---
c2 = TestClient(appmod.app)
c2.post("/auth/login", data={"email": "u2@test.ru", "password": "pass1234"}, follow_redirects=False)
r = c2.post(f"/admin/users/{ids['u1@test.ru']}/set-tariff", json={"tariff_plan_id": plan_ids["Про"]})
check("non-admin set-tariff 403", r.status_code == 403)
r = c2.post("/admin/users/set-tariff-bulk", json={"user_ids": [ids["u1@test.ru"]], "tariff_plan_id": plan_ids["Про"]})
check("non-admin bulk 403", r.status_code == 403)

print("\nFAILED:", fails if fails else "NONE — ALL PASSED")
sys.exit(1 if fails else 0)
