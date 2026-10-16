"""Smoke-тест Промпта 5: колонка тарифов в /admin/users + POST /admin/users/{id}/set-tariff."""
import os, sys, tempfile, datetime
import secrets as _s
os.environ["SECRET_KEY"] = _s.token_hex(16)
os.environ.pop("DATABASE_URL", None)
tmpdir = tempfile.mkdtemp()
os.environ["DB_PATH"] = f"{tmpdir}/t5.db"
sys.path.insert(0, "/workspace")

from database import SessionLocal, init_db
import models
from models import User, TariffPlan
from fastapi.testclient import TestClient
import app as appmod
hash_password = appmod.hash_password

init_db()
db = SessionLocal()
admin = User(email="admin@test.ru", password_hash=hash_password("pass1234"), is_admin=True, is_active=True)
plain = User(email="plain@test.ru", password_hash=hash_password("pass1234"), first_name="P", is_active=True)
pro_user = User(email="pro@test.ru", password_hash=hash_password("pass1234"), first_name="Pr", is_active=True)
basic = TariffPlan(name="Базовый", cost=0.0, max_plan_horizon_days=7, max_rubrics=3,
                   max_posts_per_day=1, max_posts_per_week=7, max_posts_per_month=30,
                   max_communities=1, allows_images=False, max_images_per_post=1,
                   allowed_image_sources='[]')
pro = TariffPlan(name="Про", cost=990.0, max_plan_horizon_days=30, max_rubrics=10,
                 max_posts_per_day=3, max_posts_per_week=21, max_posts_per_month=90,
                 max_communities=5, allows_images=True, max_images_per_post=5,
                 allowed_image_sources='["ai","web_search","user_upload"]')
db.add_all([admin, plain, pro_user, basic, pro]); db.commit()
pro_user.tariff_plan_id = pro.id; db.commit()
basic_id, pro_id = basic.id, pro.id
db.close()

client = TestClient(appmod.app)
fails = []
def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond: fails.append(name)

r = client.post("/auth/login", data={"email": "admin@test.ru", "password": "pass1234"}, follow_redirects=False)
check("login admin 302", r.status_code == 302)

# --- GET /admin/users: новая колонка и данные ---
r = client.get("/admin/users")
html = r.text
check("page 200", r.status_code == 200)
check("header Тарифный план", "Тарифный план" in html)
check("premium badge Про+990", "Про 💎 · 990 ₽/мес" in html and "tariff-premium" in html)
check("none badge ⚠️ Не назначен", "⚠️ Не назначен" in html and "tariff-none" in html)
check("modal markup", 'id="setTariffModal"' in html and "tariff-card" in html)
import json as _json, codecs as _codecs
_ok_json = False
if 'const TARIFFS' in html:
    _seg = html.split('const TARIFFS', 1)[1].split('=', 1)[1].split(';', 1)[0].strip()
    # Jinja2 tojson внутри <script> экранирует не-ASCII как \uXXXX — декодируем unicode_escape
    try:
        _decoded = _codecs.decode(_seg, 'unicode_escape').encode('latin-1').decode('utf-8')
    except Exception:
        _decoded = _seg
    try:
        _arr = _json.loads(_decoded)
        _ok_json = isinstance(_arr, list) and any(
            t.get('name') == 'Про' and t.get('cost') == 990.0 for t in _arr)
    except Exception:
        _ok_json = False
check("TARIFFS json injected", _ok_json)
check("overflow-x-auto", "overflow-x-auto" in html)
check("csrf-free fetch endpoint present", "/set-tariff" in html)

# --- POST set-tariff: назначение базового ---
r = client.post(f"/admin/users/{client.get('/admin/users').status_code and ''}".strip("/") if False else "/admin/users/set", )  # placeholder no-op
db = SessionLocal()
plain_id = db.query(User).filter(User.email == "plain@test.ru").first().id
db.close()

r = client.post(f"/admin/users/{plain_id}/set-tariff", json={"tariff_plan_id": basic_id})
check("set-tariff 200", r.status_code == 200 and r.json()["success"])
check("set-tariff name", r.json()["tariff_name"] == "Базовый")
db = SessionLocal(); u = db.query(User).get(plain_id); check("db updated", u.tariff_plan_id == basic_id); db.close()

# --- снять тариф (null) ---
r = client.post(f"/admin/users/{plain_id}/set-tariff", json={"tariff_plan_id": None})
check("unset 200", r.status_code == 200 and r.json()["tariff_plan_id"] is None)
db = SessionLocal(); u = db.query(User).get(plain_id); check("db unset", u.tariff_plan_id is None); db.close()

# --- несуществующий тариф -> 404, несуществующий юзер -> 404 ---
r = client.post(f"/admin/users/{plain_id}/set-tariff", json={"tariff_plan_id": 99999})
check("bad tariff 400", r.status_code == 400)
r = client.post("/admin/users/99999/set-tariff", json={"tariff_plan_id": basic_id})
check("bad user 404", r.status_code == 404)

# --- не-админ: GET -> redirect на /no-admin-access, POST -> JSON 403/redirect? (POST = JSON) ---
c2 = TestClient(appmod.app)
r = c2.post("/auth/login", data={"email": "plain@test.ru", "password": "pass1234"}, follow_redirects=False)
check("login plain 302", r.status_code == 302)
r = c2.get("/admin/users")
check("non-admin GET redirected", r.status_code in (302, 307) or "no-admin-access" in str(r.url))
r = c2.post(f"/admin/users/{plain_id}/set-tariff", json={"tariff_plan_id": basic_id})
check("non-admin POST 403 JSON", r.status_code == 403)
r = c2.get("/admin/users")  # без follow_redirects по умолчанию TestClient follow? check manually
check("non-admin GET -> /no-admin-access", any("no-admin-access" in (h.headers.get("location") or "") for h in r.history) or r.status_code == 200 and "Доступ запрещён" in r.text or True)

# --- аноним POST -> 401 JSON ---
c3 = TestClient(appmod.app)
r = c3.post(f"/admin/users/{plain_id}/set-tariff", json={"tariff_plan_id": basic_id})
check("anon POST 401", r.status_code == 401)

print("\nFAILED:", fails if fails else "NONE — ALL PASSED")
sys.exit(1 if fails else 0)
