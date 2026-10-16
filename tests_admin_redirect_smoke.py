"""Смоук-тест Промпт 4: редиректы админки вместо JSON-ошибок."""
import os, sys, tempfile, datetime

tmpdir = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(tmpdir, "test.db") if os.environ.get("USE_DB_PATH_ENV") else ""

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from database import Base, engine, SessionLocal
import models as _m  # noqa: F401
Base.metadata.create_all(engine)

from app import app, create_session, hash_password
from models import User
from fastapi.testclient import TestClient

db = SessionLocal()
def mk_user(email, admin=False, blocked=False):
    db.query(User).filter(User.email == email).delete()
    u = User(email=email, password_hash=hash_password("x"), is_active=True,
             is_admin=admin, is_blocked=blocked)
    db.add(u); db.commit(); db.refresh(u)
    return u

admin_u = mk_user("a@test.ru", admin=True)
user_u  = mk_user("u@test.ru")
block_u = mk_user("b@test.ru", blocked=True)

client = TestClient(app, follow_redirects=False)
ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print(f"PASS  {name}")
    else: fail += 1; print(f"FAIL  {name} {extra}")

HTML = {"Accept": "text/html,application/xhtml+xml"}
JSON = {"Accept": "application/json"}

# 1. Неавторизованный GET /admin (браузер) -> 302 на /login?next=/admin
r = client.get("/admin", headers=HTML)
check("GET /admin anon -> 302", r.status_code == 302, r.status_code)
check("  redirect -> /login с next=/admin", "/login?" in r.headers["location"] and "next=%2Fadmin" in r.headers["location"], r.headers.get("location"))

# 2. GET /admin/tariffs-page без входа -> тот же редирект
r = client.get("/admin/tariffs-page", headers=HTML)
check("GET tariffs-page anon -> 302 /login", r.status_code == 302 and "/login?" in r.headers["location"], r.headers.get("location"))

# 3. Авторизованный не-админ GET /admin -> 302 на /no-admin-access
s = create_session(user_u, db)
cu = TestClient(app, cookies={"session_id": s}, follow_redirects=False)
r = cu.get("/admin", headers=HTML)
check("GET /admin user -> 302 /no-admin-access", r.status_code == 302 and r.headers["location"] == "/no-admin-access", r.headers.get("location"))

# 4. Заблокированный пользователь GET /admin -> 302 на /account-blocked
sb = create_session(block_u, db)
cb = TestClient(app, cookies={"session_id": sb}, follow_redirects=False)
r = cb.get("/admin/users", headers=HTML)
check("GET /admin/users blocked -> 302 /account-blocked", r.status_code == 302 and r.headers["location"] == "/account-blocked", r.headers.get("location"))

# 5. POST от не-админа -> JSON 403 (не редирект)
r = cu.post("/admin/users/1/block", headers=JSON)
check("POST block user -> 403 JSON", r.status_code == 403 and "detail" in r.json(), (r.status_code, r.text[:80]))
r = cu.post("/admin/tariffs", json={"name":"t","cost":1}, headers=JSON)
check("POST tariffs user -> 403 JSON", r.status_code == 403, r.status_code)

# 6. DELETE тарифа анонимно через fetch (без Accept html) -> JSON, а не 302
r = client.delete("/admin/tariffs/1", headers=JSON)
check("DELETE tariff anon API -> 401 JSON", r.status_code == 401 and "detail" in r.json(), (r.status_code, r.text[:80]))

# 7. Страница /no-admin-access публична
r = client.get("/no-admin-access", headers=HTML)
check("GET /no-admin-access public -> 200", r.status_code == 200, r.status_code)
check("  содержит заголовок и кнопки", "Доступ запрещён" in r.text and 'href="/"' in r.text and 'href="/logout"' in r.text)

# 8. Админ проходит дальше (рендерится страница)
sa = create_session(admin_u, db)
ca = TestClient(app, cookies={"session_id": sa}, follow_redirects=False)
r = ca.get("/admin", headers=HTML)
check("GET /admin admin -> 200", r.status_code == 200, r.status_code)

# 9. /login?next=/admin сохраняет next в форме
r = client.get("/login?msg=test&next=/admin", headers=HTML)
check("login page keeps next=/admin", r.status_code == 200 and 'value="/admin"' in r.text, r.status_code)

# 10. Вход админа с next_url -> редирект на /admin
r = client.post("/auth/login", data={"email": "a@test.ru", "password": "x", "next_url": "/admin"})
check("login admin with next -> 302 /admin", r.status_code == 302 and r.headers["location"] == "/admin", (r.status_code, r.headers.get("location")))

# 11. Open redirect защищён
r = client.post("/auth/login", data={"email": "a@test.ru", "password": "x", "next_url": "//evil.com"})
check("open redirect blocked -> '/'", r.status_code == 302 and r.headers["location"] == "/", r.headers.get("location"))

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
