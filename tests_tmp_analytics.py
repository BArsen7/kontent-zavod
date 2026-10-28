import datetime as dt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from models import (User, UserCommunity, PlatformAccount, HistoricalPost,
                    CommunityGenerationContext, MarketingStrategy, ContentPlanPeriod)
from services.content_manager_service import (
    build_analytics_block, build_context_block, _top_historical_posts,
    get_configured_generation_context, initialize_chat_with_questions,
    generate_ai_response, CONTEXT_SETUP_FORM_MESSAGE,
)

engine = create_engine("sqlite:///:memory:")
Base.metadata.create_all(engine)
db = sessionmaker(bind=engine)()

u = User(id=1, email="t@t.t", password_hash="x")
db.add(u)
db.add(UserCommunity(id=10, user_id=1, group_id=-123, group_name="Выпечка", group_token="tok"))
p = ContentPlanPeriod(id=7, user_id=1, period_type="week",
                      start_date=dt.date(2026,10,15), end_date=dt.date(2026,10,21), status="draft")
db.add(p); db.commit()

# --- ДАННЫХ НЕТ: analytics пуст, старт — сообщение о форме настроек ---
assert build_analytics_block(db, p) == "", "analytics должен быть пуст без данных"
assert get_configured_generation_context(db, 10) is None
msg = initialize_chat_with_questions(db, 7)
assert msg == CONTEXT_SETUP_FORM_MESSAGE and "форму настроек" in msg, msg[:200]
print("FORM MSG:", msg.replace("\n"," | ")[:120])
# второй заход: чат уже инициализирован -> приветствие не дублируется (None)
msg = initialize_chat_with_questions(db, 7)
assert msg is None, "повторная инициализация не должна создавать дубль приветствия"

# новая анкета в другом сообществе (is_configured=False) -> то же сообщение о форме
ctx10 = CommunityGenerationContext(user_community_id=10, goal="Продажи",
                                   target_audience="Женщины 25-45", is_configured=False)
db.add(ctx10)
db.add(UserCommunity(id=11, user_id=1, group_id=-321, group_name="Тест2", group_token="tok"))
db.add(CommunityGenerationContext(id=99, user_community_id=11, goal="Путешествия",
                                  target_audience="", is_configured=False))
p3 = ContentPlanPeriod(id=9, user_id=1, period_type="week",
                       start_date=dt.date(2026,10,15), end_date=dt.date(2026,10,21), status="draft")
db.add(p3); db.commit()
msg = initialize_chat_with_questions(db, 9)
assert msg == CONTEXT_SETUP_FORM_MESSAGE, msg
print("PARTIAL CTX -> form-message branch ok")

# Частичная анкета + есть история -> всё ещё ветка формы (не приветствие-аналитика)
db.add(PlatformAccount(id=50, account_id=-123, platform="vk", access_token="t"))
for i, (txt, er, top) in enumerate([("Рецепт медовика на меду", 8.4, True),
                                    ("Мастер-класс по крему", 5.1, True),
                                    ("Сухая новость о доставке", 0.3, False)]):
    db.add(HistoricalPost(id=i+1, community_id=50, vk_post_id=100+i, text=txt,
                          engagement_rate=er, is_top_performer=top))
db.commit()
p4 = ContentPlanPeriod(id=10, user_id=1, period_type="week",
                       start_date=dt.date(2026,10,15), end_date=dt.date(2026,10,21), status="draft")
db.add(p4); db.commit()
msg = initialize_chat_with_questions(db, 10)
assert msg == CONTEXT_SETUP_FORM_MESSAGE and "медовик" not in msg, msg
print("PARTIAL CTX + HISTORY -> form branch ok")


# --- ДАННЫЕ ЕСТЬ: аналитическое приветствие БЕЗ вопросов ---
# берём именно анкету сообщества 10 (в тесте есть и другая, незаполненная)
ctx = db.query(CommunityGenerationContext).filter_by(user_community_id=10).first()
ctx.is_configured = True; ctx.community_specifics = "Домашняя выпечка"; ctx.goal="Продажи"
ctx.image_source_preferences = '["ai", "web_search"]'
ctx.current_state_and_what_works = "Печенье с цветным узором заходит лучше всего"
db.add(MarketingStrategy(community_id=50, rubrics=[{"name":"Рецепты"},{"name":"Мастер-классы"}],
                         posting_schedule="Вт/Пт 18:00"))
db.commit()

a = build_analytics_block(db, p)
print("ANALYTICS:\n"+a)
assert "<ANALYTICS>" in a and "Лучшие посты:" in a and "медовик" in a.lower().replace("рецепт ","")
assert "ER=8.40%" in a and "ЦА: Женщины 25-45" in a and "Цель: Продажи" in a
assert "Рецепты, Мастер-классы" in a and "Вт/Пт 18:00" in a
top = _top_historical_posts(db, -123)
assert len(top) == 2 and top[0].engagement_rate == 8.4, [(t.text,t.er) for t in top]

c = build_context_block(db, p)
assert c.startswith("<CONTEXT>") and "</CONTEXT>" in c and "<ANALYTICS>" in c
assert "Предпочтения по источникам картинок" in c

# LLM недоступен (Ollama не запущен) -> fallback-шаблон, базовые вопросы НЕ задаются
w = initialize_chat_with_questions(db, 7)
print("WELCOME:", w)
assert "проанализировал" in w and "медовик" in w.lower() and "Какая цель сообщества?" not in w

# generate_ai_response: system_prompt получает ANALYTICS и запрет базовых вопросов
import services.content_manager_service as cms
captured = {}
def fake_generate_text(prompt=None, system_prompt=None, use_local=None, **kw):
    captured.update(prompt=prompt, system_prompt=system_prompt, use_local=use_local)
    return "Готово, смотрим план."
orig = cms.generate_text
cms.generate_text = fake_generate_text
try:
    r = generate_ai_response(db, 7, "давай план", 1)
    assert r == "Готово, смотрим план."
    assert "<ANALYTICS>" in captured["system_prompt"]
    assert "НЕ задавай базовые вопросы" in captured["system_prompt"]
    print("generate_ai_response system_prompt OK (use_local=%s)" % captured["use_local"])
finally:
    cms.generate_text = orig

# generate_ai_response с незаполненной анкетой -> инструкция задавать блоки вопросов
captured.clear()
u2 = User(id=2, email="u2@t.t", password_hash="x")
db.add(u2)
db.add(UserCommunity(id=20, user_id=2, group_id=-999, group_name="Путешествия", group_token="tok2"))
p2 = ContentPlanPeriod(id=8, user_id=2, period_type="week",
                       start_date=dt.date(2026,10,15), end_date=dt.date(2026,10,21), status="draft")
db.add(p2); db.commit()
cms.generate_text = fake_generate_text
try:
    generate_ai_response(db, 8, "привет", 2)
    assert "<ANALYTICS>" not in captured["system_prompt"]
    assert "небольшими блоками" in captured["system_prompt"]
    print("generate_ai_response unconfigured-branch OK")
finally:
    cms.generate_text = orig

print("ALL ANALYTICS/CONTEXT TESTS PASSED")
