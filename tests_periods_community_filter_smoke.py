"""Smoke-тест: «Мои планы» в контент-менеджере показываются только для выбранного сообщества."""
import os, sys, tempfile, json, datetime
import secrets as _s
os.environ["SECRET_KEY"] = _s.token_hex(16)
os.environ.pop("DATABASE_URL", None)
tmpdir = tempfile.mkdtemp()
os.environ["DB_PATH"] = f"{tmpdir}/periods_filter.db"
sys.path.insert(0, "/workspace")

from database import SessionLocal, init_db
import models
from models import User, UserCommunity, ContentPlanPeriod
from fastapi.testclient import TestClient
import app as appmod
hash_password = appmod.hash_password

init_db()
db = SessionLocal()
user = User(email="u@test.ru", password_hash=hash_password("pass1234"), is_active=True)
db.add(user); db.commit()
c1 = UserCommunity(user_id=user.id, group_id=111, group_name="Клуб А", group_token="t")
c2 = UserCommunity(user_id=user.id, group_id=222, group_name="Клуб Б", group_token="t")
db.add_all([c1, c2]); db.commit()
c1_id, c2_id = c1.id, c2.id

now = datetime.datetime.utcnow()
def mk(info):
    p = ContentPlanPeriod(
        user_id=user.id, period_type="week",
        start_date=now.date(), end_date=now.date() + datetime.timedelta(days=6),
        status="draft", community_info=info,
    )
    db.add(p); db.commit(); db.refresh(p)
    return p.id

p_c1 = mk(json.dumps({"period_community_id": c1_id, "group_name": "Клуб А"}, ensure_ascii=False))
p_c2 = mk(json.dumps({"period_community_id": c2_id, "group_name": "Клуб Б"}, ensure_ascii=False))
p_none = mk(None)
p_old = mk("Информация о сообществе: Клуб Б - группа 222")
p_generic = mk("Информация о сообществах отсутствует")
c1_id_, c2_id_ = c1_id, c2_id
db.close()

client = TestClient(appmod.app)
fails = []
def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond: fails.append(name)

r = client.post("/auth/login", data={"email": "u@test.ru", "password": "pass1234"}, follow_redirects=False)
check("login 302", r.status_code == 302)

# Без параметра — все планы (обратная совместимость)
r = client.get("/api/content-manager/periods")
ids = {p["id"] for p in r.json()["periods"]}
check("no param -> all periods", r.status_code == 200 and ids >= {p_c1, p_c2, p_none})

# Фильтр по сообществу 1
r = client.get(f"/api/content-manager/periods?community_id={c1_id_}")
ids = {p["id"] for p in r.json()["periods"]}
check("community 1 -> only its plan", r.status_code == 200 and ids == {p_c1})

# Фильтр по сообществу 2 (включая старый plain-text период с упоминанием)
r = client.get(f"/api/content-manager/periods?community_id={c2_id_}")
ids = {p["id"] for p in r.json()["periods"]}
check("community 2 -> own + legacy text match", r.status_code == 200 and ids == {p_c2, p_old})

# community_id=0 -> непривязанные (community_info пуст или generic-текст без упоминаний сообществ)
r = client.get("/api/content-manager/periods?community_id=0")
ids = {p["id"] for p in r.json()["periods"]}
check("community 0 -> unbound only", r.status_code == 200 and ids == {p_none, p_generic})

# Чужое/несуществующее сообщество -> 404
r = client.get("/api/content-manager/periods?community_id=99999")
check("unknown community -> 404", r.status_code == 404)

# Ответ содержит community_id периода
r = client.get(f"/api/content-manager/periods?community_id={c1_id_}")
p0 = r.json()["periods"][0]
check("response has community_id", p0.get("community_id") == c1_id_)

print("\nRESULT:", "ALL PASS" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
