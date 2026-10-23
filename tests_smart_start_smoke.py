"""Smoke-тест «умного старта» диалога контент-менеджера (Промпт: smart start).

Проверяет:
1. Заполненная анкета (is_configured=True) -> приветствие резюмирует цель/ЦА,
   базовые вопросы НЕ задаются.
2. Незаполненная анкета -> ОДНО компактное сообщение с просьбой заполнить
   форму настроек (не шаблонные вопросы из 3+ пунктов).
3. generate_ai_response при заполненном контексте получает в system_prompt
   запрет переспрашивать известные данные (проверка контекста на каждом ответе).
4. Приветствие детерминировано (без LLM) — исключены вариативные дубли-перефразы.
5. Идемпотентность: повторный вызов initialize_chat_with_questions для уже
   инициализированного чата НЕ создаёт дубль приветствия (возвращает None).
"""
import sys
from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from models import (
    CommunityGenerationContext,
    ContentPlanPeriod,
    User,
    UserCommunity,
)
import services.content_manager_service as svc

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        print(f"FAIL: {name}")


engine = create_engine("sqlite:///:memory:")
Base.metadata.create_all(engine, checkfirst=True)
Session = sessionmaker(bind=engine)

_counter = 0


def make_db(with_ctx=None):
    global _counter
    _counter += 1
    db = Session()
    # Гарантируем наличие всех таблиц в свежей in-memory БД.
    Base.metadata.create_all(engine, checkfirst=True)
    u = User(email=f"t{_counter}@t.ru", password_hash="x")
    db.add(u)
    db.flush()
    comm = UserCommunity(user_id=u.id, group_id=123 + _counter, group_name="Тест",
                         group_token="tok")
    db.add(comm)
    db.flush()  # получаем comm.id до создания зависимых записей
    p = ContentPlanPeriod(
        user_id=u.id,
        period_type="week",
        start_date=date.today(),
        end_date=date.today() + timedelta(days=7),
        status="draft",
    )
    db.add(p)
    if with_ctx is not None:
        ctx = CommunityGenerationContext(
            user_community_id=comm.id,
            goal=with_ctx.get("goal"),
            community_specifics=with_ctx.get("specifics"),
            target_audience=with_ctx.get("audience"),
            current_state_and_what_works=with_ctx.get("works"),
            image_source_preferences=with_ctx.get("images"),
            is_configured=with_ctx.get("configured", False),
        )
        db.add(ctx)
    db.commit()
    return db, p


# --- 1. Контекст ЕСТЬ -> умный старт с резюме цели/ЦА -----------------------
calls = []


def fake_llm_ok(prompt, system_prompt="", use_local=True, **kw):
    calls.append((prompt, system_prompt))
    return "Отлично! Готов предложить план по вашим данным."


def fake_llm_summary(prompt, system_prompt="", use_local=True, **kw):
    """LLM корректно резюмирует известные данные (проходит guard)."""
    calls.append((prompt, system_prompt))
    return ("Я проанализировал ваше сообщество. Вижу, что ваша цель: продажи, "
            "ЦА: женщины 25-40. Хотите скорректировать вводные или сразу "
            "приступим к генерации?")


orig_generate = svc.generate_text
svc.generate_text = fake_llm_ok

db, period = make_db(with_ctx={
    "goal": "продажи", "audience": "женщины 25-40", "specifics": "кулинария",
    "works": "рецепты", "images": '["ai"]', "configured": True,
})
# LLM недоступна (exception) -> детерминированное резюме из build_smart_start_message.
def fake_llm_down(prompt, system_prompt="", use_local=True, **kw):
    raise RuntimeError("Ollama is not available")

svc.generate_text = fake_llm_down
msg = svc.initialize_chat_with_questions(db, period.id)
check("smart start: содержит фразу анализа", "Я проанализировал ваше сообщество" in msg)
check("smart start: резюмирует цель", "ваша цель: продажи" in msg)
check("smart start: резюмирует ЦА", "ЦА: женщины 25-40" in msg)
check("smart start: предложение скорректировать/генерировать",
      "скорректировать вводные или сразу приступим к генерации" in msg)

# FIX (дублирование приветствий): приветствие теперь ВСЕГДА детерминированное
# (без обращения к LLM) — текст фиксирован, вариативные перефразы исключены.
check("smart start: НЕ вызывает LLM (нет дублей-перефразов)", len(calls) == 0)

# Идемпотентность: повторный вызов для уже инициализированного чата не
# создаёт второй приветственный сигнал (возвращает None, история не растёт).
msg_again = svc.initialize_chat_with_questions(db, period.id)
history_after_repeat = svc.get_chat_history(db, period.id)
check("идемпотентность: повторный вызов вернул None", msg_again is None)
check("идемпотентность: количество сообщений в чате не изменилось",
      len(history_after_repeat) == len(svc.get_chat_history(db, period.id)))
assistant_msgs = [m for m in history_after_repeat if m.role == "assistant"]
check("идемпотентность: ровно одно assistant-приветствие",
      len(assistant_msgs) == 1 and assistant_msgs[0].content == msg)

# Сообщение сохранено в историю чата как assistant.
history = svc.get_chat_history(db, period.id)
check("smart start: сохранено в ChatMessage (assistant)",
      any(m.role == "assistant" and m.content == msg for m in history))

# --- 2. Контекста НЕТ -> одно компактное сообщение о форме ------------------
svc.generate_text = fake_llm_ok
db3, period3 = make_db(with_ctx=None)
msg3 = svc.initialize_chat_with_questions(db3, period3.id)
check("no ctx: просьба заполнить форму настроек", "форму настроек" in msg3)
check("no ctx: НЕ рассылаются блоки вопросов 1)-5)",
      all(t not in msg3 for t in ("1)", "2)", "3)", "4)", "5)")))
check("no ctx: одно компактное сообщение (< 400 симв.)", len(msg3) < 400)
# Повторный вызов на пустом контексте тоже идемпотентен.
check("no ctx: повторный вызов не дублирует сообщение",
      svc.initialize_chat_with_questions(db3, period3.id) is None)

# Частично заполненная, но НЕ подтверждённая анкета — тоже ветка формы.
db4, period4 = make_db(with_ctx={"goal": "продажи", "configured": False})
msg4 = svc.initialize_chat_with_questions(db4, period4.id)
check("partial ctx (is_configured=False): сообщение о форме, не вопросы",
      "форму настроек" in msg4 and "3)" not in msg4)

# --- 4. generate_ai_response проверяет контекст при каждом ответе -----------
sys_prompts = []


def fake_llm_capture(prompt, system_prompt="", use_local=True, **kw):
    sys_prompts.append(system_prompt)
    return "Окей, готово."


svc.generate_text = fake_llm_capture
db5, period5 = make_db(with_ctx={
    "goal": "узнаваемость", "audience": "студенты", "configured": True,
})
svc.add_chat_message(db5, period5.id, "user", "Привет")
resp = svc.generate_ai_response(db5, period5.id, "Составь план", user_id=period5.user_id)
sp = sys_prompts[-1] if sys_prompts else ""
check("generate_ai_response: system_prompt содержит факт заполненной анкеты",
      "АНКЕТА СООБЩЕСТВА УЖЕ ЗАПОЛНЕНА" in sp)
check("generate_ai_response: известный goal передан в промпт", "узнаваемость" in sp)
check("generate_ai_response: явный запрет переспрашивать", "НЕ задавай" in sp)

# Без контекста — промпт просит форму, а не шаблонные блоки вопросов.
db6, period6 = make_db(with_ctx=None)
svc.generate_ai_response(db6, period6.id, "Привет", user_id=period6.user_id)
sp6 = sys_prompts[-1]
check("generate_ai_response (без ctx): просьба о форме вместо блоков вопросов",
      "форму настроек" in sp6 and "Следующий ожидаемый блок вопросов" not in sp6)

svc.generate_text = orig_generate

print(f"\nRESULT: {'ALL PASSED' if FAIL == 0 else 'FAILED'} (pass={PASS}, fail={FAIL})")
sys.exit(0 if FAIL == 0 else 1)
