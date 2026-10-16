import os, sys, tempfile, datetime
import secrets as _s
os.environ["SECRET_KEY"] = _s.token_hex(16)
os.environ.pop("DATABASE_URL", None)
tmpdir = tempfile.mkdtemp()
os.environ["DB_PATH"] = f"{tmpdir}/t3.db"
sys.path.insert(0, "/workspace")

from database import engine, SessionLocal, init_db
import models
from models import User
from fastapi.testclient import TestClient
import app as appmod
hash_password = appmod.hash_password

init_db()
db = SessionLocal()
now = datetime.datetime.now()
u = User(email="user@test.ru", password_hash=hash_password("pass1234"), first_name="U", last_name="S", is_active=True)
b = User(email="blocked@test.ru", password_hash=hash_password("pass1234"), first_name="B", is_active=True,
         is_blocked=True, blocked_reason="Нарушение правил")
db.add_all([u, b]); db.commit()
db.close()

client = TestClient(appmod.app)
fails = []
def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond: fails.append(name)

# 1. Неавторизованный -> /content-manager редирект на /login?msg&next (без follow)
r = client.get("/content-manager", follow_redirects=False)
check("GET /content-manager без сессии -> 302", r.status_code == 302)
loc = r.headers.get("location", "")
check("редирект на /login с msg и next", loc.startswith("/login?") and "msg=" in loc and "next=%2Fcontent-manager" in loc)

# 2. /login показывает сообщение
r = client.get(loc)
check("GET /login -> 200", r.status_code == 200)
check("сообщение на странице входа", "Для доступа к контент-менеджеру необходимо войти в систему" in r.text)
check("скрытое поле next_url", 'name="next_url" value="/content-manager"' in r.text)
check("контейнер ошибок логина", 'id="loginError"' in r.text)

# 3. Валидация next (open redirect protection)
r = client.get("/login?next=https://evil.com")
check("внешний next отклонён", 'value="/"' in r.text)
r = client.get("/login?next=//evil.com")
check("протокол-релативный next отклонён", 'value="/"' in r.text)

# 4. Логин с correct credentials -> редирект на next_url
r = client.post("/auth/login", data={"email": "user@test.ru", "password": "pass1234", "next_url": "/content-manager"}, follow_redirects=False)
check("POST /auth/login -> 302 на next_url", r.status_code == 302 and r.headers.get("location") == "/content-manager")
check("cookie сессии установлен", "session_id" in r.cookies or any("session" in c for c in r.headers.get("set-cookie","")))

# 5. Авторизованный видит страницу
r = client.get("/content-manager")
check("GET /content-manager после входа -> 200", r.status_code == 200)

# 6. Неверный пароль -> JSON 401 (API-ответ формы)
c2 = TestClient(appmod.app)
r = c2.post("/auth/login", data={"email": "user@test.ru", "password": "wrong", "next_url": "/"})
check("неверный пароль -> 401 JSON", r.status_code == 401 and r.json()["detail"] == "Неверный email или пароль")

# 7. Заблокированный пользователь: логин -> /account-blocked, затем веб-страницы тоже туда
c3 = TestClient(appmod.app)
r = c3.post("/auth/login", data={"email": "blocked@test.ru", "password": "pass1234", "next_url": "/content-manager"}, follow_redirects=False)
check("заблокированный при входе -> /account-blocked", r.status_code == 302 and r.headers.get("location") == "/account-blocked")
r = c3.get("/content-manager", follow_redirects=False)
check("заблокированный на /content-manager -> /account-blocked", r.status_code == 302 and r.headers.get("location") == "/account-blocked")
r = c3.get("/account-blocked")
check("/account-blocked рендерится (200)", r.status_code == 200)

# 8. API по-прежнему возвращает JSON 401 (не редирект)
r = client2 = TestClient(appmod.app).get("/api/posts")
check("API /api/posts без сессии -> 401 JSON", r.status_code == 401)

print("\nRESULT:", "ALL PASSED" if not fails else f"FAILED: {fails}")
sys.exit(0 if not fails else 1)
