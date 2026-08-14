import os
import json
import asyncio
import logging
import re
import sqlite3
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, PollAnswerHandler, filters
)
from google import genai
from google.genai import types

try:
    import fitz
except ImportError:
    fitz = None

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_API_KEY_2 = os.getenv("GEMINI_API_KEY_2", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite").strip()
DB_PATH = os.getenv("DB_PATH", "data/neet_ai.db")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("NEET_WALA_BOT")

clients = {}

# ---------------- DATABASE ----------------

def db():
    p = Path(DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            username TEXT,
            score INTEGER DEFAULT 0,
            quizzes INTEGER DEFAULT 0,
            correct INTEGER DEFAULT 0,
            wrong INTEGER DEFAULT 0,
            last_topic TEXT
        );

        CREATE TABLE IF NOT EXISTS quiz_progress(
            user_id INTEGER PRIMARY KEY,
            topic TEXT,
            questions_json TEXT,
            current_index INTEGER DEFAULT 0,
            score INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS polls(
            poll_id TEXT PRIMARY KEY,
            chat_id INTEGER,
            topic TEXT,
            question TEXT,
            correct_index INTEGER,
            explanation TEXT
        );

        CREATE TABLE IF NOT EXISTS answered_polls(
            poll_id TEXT,
            user_id INTEGER,
            PRIMARY KEY(poll_id, user_id)
        );
        """)

def ensure_user(user):
    if not user:
        return
    with db() as c:
        c.execute("""
        INSERT INTO users(user_id,name,username)
        VALUES(?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
          name=excluded.name, username=excluded.username
        """, (
            user.id,
            user.full_name or user.username or str(user.id),
            user.username,
        ))

def add_result(user_id, topic, correct, wrong):
    with db() as c:
        c.execute("""
        UPDATE users
        SET score=score+?, quizzes=quizzes+1,
            correct=correct+?, wrong=wrong+?, last_topic=?
        WHERE user_id=?
        """, (correct * 4 - wrong, correct, wrong, topic, user_id))

def save_quiz(user_id, topic, questions):
    with db() as c:
        c.execute("""
        INSERT INTO quiz_progress(user_id,topic,questions_json,current_index,score)
        VALUES(?,?,?,?,0)
        ON CONFLICT(user_id) DO UPDATE SET
          topic=excluded.topic,
          questions_json=excluded.questions_json,
          current_index=0,
          score=0
        """, (user_id, topic, json.dumps(questions, ensure_ascii=False), 0))

def get_quiz(user_id):
    with db() as c:
        r = c.execute(
            "SELECT * FROM quiz_progress WHERE user_id=?", (user_id,)
        ).fetchone()
    return r

def update_quiz(user_id, index, score):
    with db() as c:
        c.execute("""
        UPDATE quiz_progress SET current_index=?, score=?
        WHERE user_id=?
        """, (index, score, user_id))

def clear_quiz(user_id):
    with db() as c:
        c.execute("DELETE FROM quiz_progress WHERE user_id=?", (user_id,))

def top_users(limit=10):
    with db() as c:
        return c.execute("""
        SELECT name, score, quizzes, correct, wrong
        FROM users
        ORDER BY score DESC, correct DESC
        LIMIT ?
        """, (limit,)).fetchall()

def user_row(user_id):
    with db() as c:
        return c.execute(
            "SELECT * FROM users WHERE user_id=?", (user_id,)
        ).fetchone()

def poll_info(poll_id):
    with db() as c:
        return c.execute(
            "SELECT * FROM polls WHERE poll_id=?", (poll_id,)
        ).fetchone()

def save_poll(poll_id, chat_id, topic, question, correct_index, explanation):
    with db() as c:
        c.execute("""
        INSERT OR REPLACE INTO polls
        (poll_id,chat_id,topic,question,correct_index,explanation)
        VALUES(?,?,?,?,?,?)
        """, (poll_id, chat_id, topic, question, correct_index, explanation))

def already_answered(poll_id, user_id):
    with db() as c:
        return c.execute("""
        SELECT 1 FROM answered_polls WHERE poll_id=? AND user_id=?
        """, (poll_id, user_id)).fetchone() is not None

def mark_answered(poll_id, user_id):
    with db() as c:
        c.execute("""
        INSERT OR IGNORE INTO answered_polls(poll_id,user_id)
        VALUES(?,?)
        """, (poll_id, user_id))

# ---------------- GEMINI ----------------

def get_client(key):
    if key not in clients:
        clients[key] = genai.Client(api_key=key)
    return clients[key]

def model_candidates(client):
    # The default is a currently documented Gemini model. We also discover
    # models exposed by the API so an unavailable model does not hard-fail.
    preferred = [
        "gemini-3.1-flash-lite",
        "gemini-3.1-flash",
        "gemini-3.5-flash",
        GEMINI_MODEL,
    ]
    found = []
    try:
        for m in client.models.list():
            name = getattr(m, "name", "") or ""
            if name.startswith("models/"):
                name = name[7:]
            if name and name not in found:
                found.append(name)
    except Exception as e:
        log.warning("Gemini model listing failed: %s", e)

    deprecated_prefixes = (
        "gemini-1.5-", "gemini-1.0-", "gemini-2.0-", "gemini-2.5-"
    )
    preferred = [
        m for m in preferred
        if m and not m.lower().startswith(deprecated_prefixes)
    ]

    result = []
    for m in preferred:
        if m and m not in result and (not found or m in found):
            result.append(m)

    if found:
        for m in found:
            low = m.lower()
            if (
                "gemini" in low
                and not any(x in low for x in
                            ("embedding", "tts", "image", "audio", "robotics"))
            ):
                if m not in result:
                    result.append(m)
    return result or [GEMINI_MODEL]

def ask_ai(prompt, system=None, json_mode=False):
    keys = [k for k in (GEMINI_API_KEY, GEMINI_API_KEY_2) if k]
    if not keys:
        raise RuntimeError(
            "GEMINI_API_KEY missing. Railway Variables में API key जोड़ें."
        )

    last = None
    for key in keys:
        client = get_client(key)
        for model in model_candidates(client):
            try:
                kwargs = {"max_output_tokens": 8000}
                if system:
                    kwargs["system_instruction"] = system
                if json_mode:
                    kwargs["response_mime_type"] = "application/json"

                config = types.GenerateContentConfig(**kwargs)
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=config,
                )
                text = getattr(response, "text", None)
                if text:
                    log.info("Gemini success: %s", model)
                    return text.strip()
            except Exception as e:
                last = e
                log.warning("Gemini model %s failed: %s", model, e)

    raise RuntimeError(f"Gemini request failed: {last}")

SYSTEM = """तुम NEET परीक्षा के लिए हिंदी AI Study Assistant हो।
उत्तर NCERT-केंद्रित, तथ्यात्मक और सरल हिंदी में दो।
मनगढ़ंत तथ्य या citation मत बनाओ।"""

# ---------------- HELPERS ----------------

def parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.replace("```json", "", 1).replace("```", "", 1).strip()
    return json.loads(text)

def make_question_prompt(topic):
    return f"""
विषय: {topic}

NEET स्तर का केवल 1 मूल MCQ बनाओ।
NCERT-केंद्रित रहो।

सिर्फ इस JSON format में उत्तर दो:
{{
  "question": "प्रश्न",
  "options": ["A विकल्प", "B विकल्प", "C विकल्प", "D विकल्प"],
  "correct_index": 0,
  "explanation": "छोटी और स्पष्ट व्याख्या"
}}

नियम:
- options ठीक 4 हों
- correct_index केवल 0,1,2,3 हो
- हिंदी में
- एक ही सही उत्तर हो
- duplicate या अस्पष्ट प्रश्न नहीं
"""

def make_quiz_prompt(topic, count):
    return f"""
विषय: {topic}
{count} मूल NEET MCQ बनाओ।

सिर्फ JSON array दो:
[
  {{
    "question": "प्रश्न",
    "options": ["A", "B", "C", "D"],
    "correct_index": 0,
    "explanation": "व्याख्या"
  }}
]

नियम:
- ठीक {count} questions
- हर question में ठीक 4 options
- correct_index 0-3
- NCERT आधारित
- हिंदी
- कोई duplicate नहीं
- कम से कम 2 questions concept/application आधारित
"""

async def send_parts(message, text):
    for i in range(0, len(text), 3900):
        await message.reply_text(text[i:i+3900])

async def generate_question(topic):
    raw = await asyncio.to_thread(
        ask_ai, make_question_prompt(topic), SYSTEM, True
    )
    q = parse_json(raw)
    if not isinstance(q, dict):
        raise ValueError("AI ने invalid question दिया.")
    if len(q.get("options", [])) != 4:
        raise ValueError("AI ने 4 options नहीं दिए.")
    idx = int(q.get("correct_index", -1))
    if idx not in (0, 1, 2, 3):
        raise ValueError("AI का correct_index invalid है.")
    return q

async def generate_questions(topic, count):
    # Keep batches modest so Telegram/Railway remains responsive.
    raw = await asyncio.to_thread(
        ask_ai, make_quiz_prompt(topic, count), SYSTEM, True
    )
    data = parse_json(raw)
    if not isinstance(data, list):
        raise ValueError("AI ने question list नहीं दी.")
    clean = []
    for q in data:
        if (
            isinstance(q, dict)
            and isinstance(q.get("question"), str)
            and isinstance(q.get("options"), list)
            and len(q["options"]) == 4
            and int(q.get("correct_index", -1)) in (0,1,2,3)
        ):
            clean.append({
                "question": q["question"],
                "options": q["options"],
                "correct_index": int(q["correct_index"]),
                "explanation": q.get("explanation", ""),
            })
    if len(clean) < count:
        raise ValueError(f"AI ने {len(clean)}/{count} valid questions दिए.")
    return clean[:count]

# ---------------- COMMANDS ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    await update.message.reply_text(
        "👋 नमस्ते! NEET AI Study Bot तैयार है.\n\n"
        "🤖 हर study command AI-powered है.\n\n"
        "📚 Commands:\n"
        "/quizar कोशिका 10 — Interactive MCQ test\n"
        "/pollar कोशिका — Native Telegram quiz poll, बिना timing\n"
        "/chapterar मानव_प्रजनन 10 — Chapter test\n"
        "/askar आपका सवाल — AI explanation\n"
        "/leaderboardar — Top 10\n"
        "/profilear — आपकी progress\n"
        "/resumear — अधूरा quiz जारी करें\n"
        "/pdfar — PDF भेजें → 90 NEET Bio polls\n"
        "/neet720ar — 180-question NEET pattern test\n"
        "/helpar — पूरी मदद"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    question = " ".join(context.args).strip()
    if not question:
        await update.message.reply_text(
            "उदाहरण:\n/askar माइटोकॉन्ड्रिया को powerhouse क्यों कहते हैं?"
        )
        return
    await update.message.reply_text("🤖 AI सोच रहा है...")
    try:
        answer = await asyncio.to_thread(ask_ai, question, SYSTEM, False)
        await send_parts(update.message, answer)
    except Exception as e:
        await update.message.reply_text(f"❌ AI error: {str(e)[:900]}")

async def quiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    if not context.args:
        await update.message.reply_text("उदाहरण: /quizar कोशिका 10")
        return

    count = 10
    if context.args[-1].isdigit():
        count = max(5, min(50, int(context.args[-1])))
        topic = " ".join(context.args[:-1]).strip()
    else:
        topic = " ".join(context.args).strip()

    if not topic:
        topic = "जीव विज्ञान"

    await update.message.reply_text(
        f"⏳ AI {topic} पर {count} प्रश्न बना रहा है..."
    )
    try:
        questions = []
        # Generate in small batches to reduce malformed large JSON responses.
        for start_i in range(0, count, 5):
            batch = min(5, count - start_i)
            questions.extend(await generate_questions(topic, batch))
        save_quiz(update.effective_user.id, topic, questions)
        await send_next_inline_question(update, context, update.effective_user.id)
    except Exception as e:
        await update.message.reply_text(
            f"❌ Quiz generation error:\n{str(e)[:1000]}"
        )

async def chapter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Same AI engine, with explicit chapter wording.
    if not context.args:
        await update.message.reply_text(
            "उदाहरण: /chapterar कोशिका 10"
        )
        return
    await quiz_cmd(update, context)

async def send_next_inline_question(target, context, user_id=None):
    """Send the next interactive question for an Update or CallbackQuery."""
    if user_id is None:
        user_id = getattr(getattr(target, "effective_user", None), "id", None)
        if user_id is None:
            user_id = getattr(getattr(target, "from_user", None), "id", None)

    row = get_quiz(user_id)
    if not row:
        return

    questions = json.loads(row["questions_json"])
    i = row["current_index"]
    if i >= len(questions):
        return

    q = questions[i]
    buttons = [
        [InlineKeyboardButton(
            f"{chr(65+j)}) {opt}",
            callback_data=f"ans:{i}:{j}"
        )]
        for j, opt in enumerate(q["options"])
    ]
    text = (
        f"📝 प्रश्न {i+1}/{len(questions)}\n"
        f"📚 {row['topic']}\n\n{q['question']}"
    )

    if getattr(target, "message", None):
        await target.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        await target.effective_message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(buttons)
        )

async def inline_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, i_s, chosen_s = query.data.split(":")
        i, chosen = int(i_s), int(chosen_s)
    except Exception:
        return

    row = get_quiz(query.from_user.id)
    if not row:
        await query.edit_message_text("यह quiz session नहीं मिला. /quizar से नया शुरू करें.")
        return

    questions = json.loads(row["questions_json"])
    if i != row["current_index"] or i >= len(questions):
        await query.answer("यह प्रश्न पहले ही process हो चुका है.", show_alert=True)
        return

    q = questions[i]
    correct = q["correct_index"]
    ok = chosen == correct
    score = row["score"] + (4 if ok else -1)

    result = "✅ सही उत्तर!" if ok else f"❌ गलत! सही उत्तर: {chr(65+correct)}) {q['options'][correct]}"
    explanation = q.get("explanation", "")

    update_quiz(query.from_user.id, i + 1, score)
    add_result(query.from_user.id, row["topic"], 1 if ok else 0, 0 if ok else 1)

    await query.edit_message_text(
        f"{result}\n\n💡 {explanation}\n\n"
        f"📊 Current score: {score}"
    )

    new_row = get_quiz(query.from_user.id)
    if new_row:
        questions2 = json.loads(new_row["questions_json"])
        if new_row["current_index"] >= len(questions2):
            await query.message.reply_text(
                f"🏁 Quiz पूरा!\n🏆 Score: {new_row['score']}\n"
                f"📚 {new_row['topic']}\n"
                f"कुल प्रश्न: {len(questions2)}"
            )
            clear_quiz(query.from_user.id)
        else:
            await send_next_inline_question(query, context, query.from_user.id)

async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    row = get_quiz(update.effective_user.id)
    if not row:
        await update.message.reply_text(
            "कोई अधूरा quiz नहीं है. /quizar विषय 10 से शुरू करें."
        )
        return
    await update.message.reply_text(
        f"▶️ Resume: {row['topic']}\n"
        f"Question: {row['current_index']+1}/{len(json.loads(row['questions_json']))}"
    )
    await send_next_inline_question(update, context, update.effective_user.id)

async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    rows = top_users(10)
    if not rows:
        await update.message.reply_text("Leaderboard अभी खाली है.")
        return
    lines = ["🏆 TOP 10 LEADERBOARD", ""]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"{i}. {r['name']} — {r['score']} अंक | "
            f"Quiz: {r['quizzes']} | सही: {r['correct']}"
        )
    await update.message.reply_text("\n".join(lines))

async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    r = user_row(update.effective_user.id)
    total = r["correct"] + r["wrong"]
    acc = round(r["correct"] / total * 100, 1) if total else 0
    await update.message.reply_text(
        f"👤 {r['name']}\n"
        f"🏆 Score: {r['score']}\n"
        f"📝 Quiz attempts: {r['quizzes']}\n"
        f"✅ सही: {r['correct']}\n"
        f"❌ गलत: {r['wrong']}\n"
        f"🎯 Accuracy: {acc}%\n"
        f"📚 Last topic: {r['last_topic'] or '-'}"
    )


def make_multi_poll_prompt(topic, count):
    return f"""
तुम NEET Biology question setter हो।

विषय: {topic}
ठीक {count} अलग-अलग NEET-level Biology Quiz MCQ बनाओ।

सिर्फ JSON array दो:
[
  {{
    "question": "प्रश्न",
    "options": ["A", "B", "C", "D"],
    "correct_index": 0,
    "explanation": "छोटी NCERT आधारित व्याख्या"
  }}
]

नियम:
- ठीक {count} प्रश्न
- हर प्रश्न में ठीक 4 options
- केवल एक सही उत्तर
- NEET level
- NCERT-केंद्रित
- सभी प्रश्न एक-दूसरे से अलग
- कथन/Assertion-Reason/Conceptual/Application प्रश्नों का अच्छा mix
- हिंदी में
- प्रश्न बहुत लंबे न हों
"""

async def generate_poll_questions(topic, count):
    raw = await asyncio.to_thread(
        ask_ai, make_multi_poll_prompt(topic, count), SYSTEM, True
    )
    data = parse_json(raw)
    if not isinstance(data, list):
        raise ValueError("AI ने question list नहीं दी.")

    clean = []
    seen = set()
    for q in data:
        if not isinstance(q, dict):
            continue
        options = q.get("options")
        try:
            idx = int(q.get("correct_index", -1))
        except Exception:
            idx = -1
        question = str(q.get("question", "")).strip()
        if (
            question
            and isinstance(options, list)
            and len(options) == 4
            and idx in (0, 1, 2, 3)
        ):
            key = question.lower()
            if key not in seen:
                seen.add(key)
                clean.append({
                    "question": question[:300],
                    "options": [str(x)[:100] for x in options],
                    "correct_index": idx,
                    "explanation": str(q.get("explanation", ""))[:900],
                })

    if len(clean) < count:
        raise ValueError(
            f"AI ने केवल {len(clean)}/{count} valid questions दिए."
        )
    return clean[:count]

async def poll_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /pollar विषय 5  -> 5 separate native Telegram Quiz Polls.
    Polls have NO open_period and NO close_date, so they have no timer.
    Anyone in the group can answer.
    """
    ensure_user(update.effective_user)

    args = list(context.args)
    count = 1

    # Last argument can be the requested number of polls.
    if args:
        try:
            possible_count = int(args[-1])
            if 1 <= possible_count <= 20:
                count = possible_count
                args = args[:-1]
        except ValueError:
            pass

    topic = " ".join(args).strip() if args else "NEET Biology"

    if update.effective_chat.type == "private" and count > 5:
        # Keep private chat from being flooded accidentally.
        count = 5

    status = await update.effective_message.reply_text(
        f"🤖 {topic} पर {count} NEET Quiz Poll "
        f"{'बना रहा हूँ' if count == 1 else 'बना रहा हूँ'}...\n"
        f"⏱️ कोई timer नहीं होगा।"
    )

    try:
        questions = await generate_poll_questions(topic, count)

        for n, q in enumerate(questions, 1):
            msg = await context.bot.send_poll(
                chat_id=update.effective_chat.id,
                question=f"Q{n}/{count}  {q['question']}",
                options=q["options"],
                type="quiz",
                correct_option_id=q["correct_index"],
                is_anonymous=False,
                # IMPORTANT:
                # No open_period and no close_date.
                # The poll remains open and anyone can answer.
            )

            save_poll(
                msg.poll.id,
                update.effective_chat.id,
                topic,
                q["question"],
                q["correct_index"],
                q.get("explanation", ""),
            )

            # Avoid sending all polls in the exact same millisecond.
            if n < len(questions):
                await asyncio.sleep(0.8)

        await status.edit_text(
            f"✅ {count} Quiz Poll {'बन गया' if count == 1 else 'बन गए'}!\n"
            f"📚 विषय: {topic}\n"
            f"👥 Group में कोई भी member answer कर सकता है।\n"
            f"⏱️ कोई timer नहीं है।\n"
            f"💬 Explanation group में नहीं आएगा; "
            f"जिसने answer किया उसे DM में भेजने की कोशिश होगी।"
        )

    except Exception as e:
        log.exception("Multi-poll generation failed")
        err = str(e)
        if "not enough rights" in err.lower() or "CHAT_ADMIN_REQUIRED" in err:
            err += (
                "\n\n⚠️ Bot को group में Polls भेजने की permission दें "
                "(जरूरत हो तो Admin बनाएं)."
            )
        await status.edit_text(f"❌ Poll error:\n{err[:1400]}")


async def poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = update.poll_answer
    info = poll_info(ans.poll_id)
    if not info or not ans.option_ids:
        return
    if already_answered(ans.poll_id, ans.user.id):
        return

    chosen = ans.option_ids[0]
    correct = int(info["correct_index"])
    ok = chosen == correct

    mark_answered(ans.poll_id, ans.user.id)
    ensure_user(ans.user)

    add_result(
        ans.user.id,
        info["topic"],
        1 if ok else 0,
        0 if ok else 1,
    )

    # Do NOT post the explanation into the group.
    # Send the result + explanation privately to the person who answered.
    result = "✅ सही उत्तर!" if ok else "❌ गलत उत्तर!"
    explanation = info["explanation"] or "इस प्रश्न की explanation उपलब्ध नहीं है."

    dm_text = (
        f"{result}\n\n"
        f"📚 {info['topic']}\n"
        f"📝 {info['question']}\n\n"
        f"💡 Explanation:\n{explanation}\n\n"
        f"📊 आपका score update हो गया है।"
    )

    try:
        await context.bot.send_message(
            chat_id=ans.user.id,
            text=dm_text[:3900],
        )
    except Exception as e:
        # Telegram does not allow a bot to start a private chat with a user
        # who has never opened/started the bot.
        log.info(
            "Could not DM poll explanation to user %s: %s",
            ans.user.id,
            e,
        )

async def pdf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 PDF भेजें।\n\n"
        "मैं PDF से NEET Biology के प्रश्न तैयार करके "
        "Telegram Quiz Poll के रूप में इसी chat/group में भेजूँगा।\n\n"
        "🔢 अधिकतम 90 प्रश्न\n"
        "🧠 NEET level\n"
        "🧬 Assertion-Reason / कथन-आधारित प्रश्न भी\n"
        "⏱️ Poll में कोई timer नहीं होगा।"
    )

def make_pdf_question_prompt(book_text, count):
    return f"""
तुम NEET Biology question setter हो।

नीचे किसी Biology book/PDF का text दिया गया है।
इसी दिए गए material के concepts पर आधारित {count} मूल NEET-level MCQ बनाओ।
किसी पुस्तक के लंबे वाक्य copy मत करो; प्रश्न को अपने शब्दों में बनाओ।

Question mix:
- लगभग 50% normal conceptual MCQ
- लगभग 25% Assertion-Reason / कथन-कारण
- लगभग 25% statement-based / multiple-statement MCQ

सिर्फ JSON array दो:
[
  {{
    "question": "प्रश्न",
    "options": ["A", "B", "C", "D"],
    "correct_index": 0,
    "explanation": "छोटी NCERT/NEET स्तर की व्याख्या"
  }}
]

Assertion-Reason के लिए options इस प्रकार रखो:
A) A और R दोनों सही हैं तथा R, A की सही व्याख्या है
B) A और R दोनों सही हैं लेकिन R, A की सही व्याख्या नहीं है
C) A सही है लेकिन R गलत है
D) A गलत है लेकिन R सही है

नियम:
- ठीक {count} प्रश्न
- हर प्रश्न में ठीक 4 options
- केवल एक सही उत्तर
- NEET Biology level
- हिंदी
- दिए गए PDF के concepts से बाहर की मनगढ़ंत जानकारी नहीं
- duplicate questions नहीं
- question और options बहुत लंबे नहीं हों

PDF TEXT:
{book_text}
"""

async def generate_pdf_questions(book_text, count=90):
    questions = []
    # Generate small batches so large PDFs/JSON responses are less likely to fail.
    for offset in range(0, count, 10):
        batch_count = min(10, count - offset)
        raw = await asyncio.to_thread(
            ask_ai,
            make_pdf_question_prompt(book_text, batch_count),
            SYSTEM,
            True
        )
        data = parse_json(raw)
        if not isinstance(data, list):
            raise ValueError("AI ने question list नहीं दी.")

        for q in data:
            try:
                idx = int(q.get("correct_index", -1))
            except Exception:
                idx = -1
            if (
                isinstance(q, dict)
                and isinstance(q.get("question"), str)
                and isinstance(q.get("options"), list)
                and len(q["options"]) == 4
                and idx in (0, 1, 2, 3)
            ):
                questions.append({
                    "question": q["question"][:300],
                    "options": [str(x)[:100] for x in q["options"]],
                    "correct_index": idx,
                    "explanation": str(q.get("explanation", ""))[:700],
                })
        if len(questions) >= count:
            break

    if len(questions) < count:
        raise ValueError(
            f"PDF से केवल {len(questions)}/{count} valid questions बन पाए."
        )

    # Remove exact duplicate questions while keeping order.
    seen = set()
    unique = []
    for q in questions:
        key = q["question"].strip().lower()
        if key not in seen:
            seen.add(key)
            unique.append(q)
    if len(unique) < count:
        raise ValueError(
            f"Duplicate हटाने के बाद {len(unique)}/{count} questions बचे."
        )
    return unique[:count]

async def post_pdf_polls(chat_id, questions, bot, status_message=None):
    total = len(questions)
    for i, q in enumerate(questions, 1):
        msg = await bot.send_poll(
            chat_id=chat_id,
            question=f"Q{i}/{total}  {q['question']}",
            options=q["options"],
            type="quiz",
            correct_option_id=q["correct_index"],
            is_anonymous=False,
            # IMPORTANT: no open_period and no close_date.
            # Therefore there is NO TIMER on these polls.
        )
        save_poll(
            msg.poll.id,
            chat_id,
            "PDF • NEET Biology",
            q["question"],
            q["correct_index"],
            q.get("explanation", ""),
        )

        # Small delay prevents flooding Telegram with 90 polls at once.
        await asyncio.sleep(0.8)

    if status_message:
        await status_message.edit_text(
            f"✅ पूरा! PDF से {total} NEET Biology Quiz Polls group/chat में डाल दिए गए.\n"
            f"🧠 Normal + कथन-आधारित + Assertion-Reason\n"
            f"⏱️ सभी polls में कोई timer नहीं है."
        )

async def pdf_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.document:
        return
    if fitz is None:
        await update.message.reply_text("❌ PDF package उपलब्ध नहीं है.")
        return

    doc = update.message.document
    if doc.file_size and doc.file_size > 15 * 1024 * 1024:
        await update.message.reply_text(
            "❌ PDF 15 MB से छोटी रखें."
        )
        return

    status = await update.message.reply_text(
        "📄 PDF मिल गई.\n"
        "🔍 Text पढ़ रहा हूँ और NEET Biology questions तैयार कर रहा हूँ...\n"
        "⏳ 90 questions में थोड़ा समय लग सकता है."
    )

    path = None
    try:
        tg = await doc.get_file()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            path = f.name
        await tg.download_to_drive(path)

        pdf = fitz.open(path)
        pages_text = []
        for page in pdf:
            pages_text.append(page.get_text())
        pdf.close()

        text = "\n".join(pages_text).strip()
        if not text:
            await status.edit_text(
                "❌ PDF में selectable text नहीं मिला.\n"
                "Scanned/image-only PDF के लिए OCR जोड़ना होगा."
            )
            return

        # Keep enough context for a book while avoiding an enormous API request.
        # The bot generates questions from the extracted material; it does not
        # reproduce the book text in Telegram.
        text = text[:180000]

        await status.edit_text(
            "🧠 PDF पढ़ ली गई.\n"
            "अब 90 NEET Biology questions बन रहे हैं...\n"
            "कथन-कारण और statement-based questions भी शामिल होंगे."
        )

        questions = await generate_pdf_questions(text, 90)

        await status.edit_text(
            "📊 90 questions तैयार हैं.\n"
            "अब इसी group/chat में Quiz Polls भेजे जा रहे हैं...\n"
            "⏱️ कोई poll timer नहीं है."
        )

        await post_pdf_polls(
            update.effective_chat.id,
            questions,
            context.bot,
            status
        )

    except Exception as e:
        log.exception("PDF pipeline failed")
        try:
            await status.edit_text(
                f"❌ PDF → Poll error:\n{str(e)[:1200]}"
            )
        except Exception:
            pass
    finally:
        if path:
            Path(path).unlink(missing_ok=True)

async def neet720_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🧪 NEET pattern test:\n"
        "90 Biology + 45 Physics + 45 Chemistry = 180 questions.\n"
        "यह बड़ा test है; AI generation में थोड़ा समय लग सकता है.\n\n"
        "शुरू करने के लिए /quizar Biology 50, /quizar Physics 50 या /quizar Chemistry 50 इस्तेमाल करें."
    )


async def ashish_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Group AI trigger:
    The bot answers AI questions ONLY when /ASHISH is written.
    Ordinary group messages are ignored.
    """
    ensure_user(update.effective_user)

    raw = update.effective_message.text or ""
    # Remove /ASHISH and optional @botusername.
    question = re.sub(
        r"^\s*/ashish(?:@\w+)?\s*",
        "",
        raw,
        flags=re.IGNORECASE,
    ).strip()

    if not question:
        await update.effective_message.reply_text(
            "🤖 हाँ, मैं यहाँ हूँ।\n"
            "ऐसे लिखें:\n"
            "/ASHISH कोशिका का powerhouse क्या है?"
        )
        return

    await update.effective_message.reply_text("🤖 AI सोच रहा है...")
    try:
        answer = await asyncio.to_thread(ask_ai, question, SYSTEM, False)
        await send_parts(update.effective_message, answer)
    except Exception as e:
        log.exception("ASHISH command failed")
        await update.effective_message.reply_text(
            f"❌ AI error:\n{str(e)[:900]}"
        )

async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # GROUP: ignore ordinary messages. AI replies only through /ASHISH.
    if update.effective_chat.type != "private":
        return

    text = (update.message.text or "").strip()
    if not text:
        return
    ensure_user(update.effective_user)
    try:
        answer = await asyncio.to_thread(ask_ai, text, SYSTEM, False)
        await send_parts(update.message, answer)
    except Exception as e:
        await update.message.reply_text(
            f"❌ AI error:\n{str(e)[:1000]}"
        )

async def error_handler(update, context):
    log.exception("Unhandled bot error", exc_info=context.error)

def build_app():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing in Railway Variables.")
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("startar", start))
    app.add_handler(CommandHandler("helpar", help_cmd))
    app.add_handler(CommandHandler("askar", ask_cmd))
    app.add_handler(CommandHandler("quizar", quiz_cmd))
    app.add_handler(CommandHandler("chapterar", chapter_cmd))
    app.add_handler(CommandHandler("pollar", poll_cmd))
    app.add_handler(CommandHandler("leaderboardar", leaderboard_cmd))
    app.add_handler(CommandHandler("profilear", profile_cmd))
    app.add_handler(CommandHandler("resumear", resume_cmd))
    app.add_handler(CommandHandler("pdfar", pdf_cmd))
    app.add_handler(CommandHandler("neet720ar", neet720_cmd))

    app.add_handler(CallbackQueryHandler(inline_answer, pattern=r"^ans:"))
    app.add_handler(PollAnswerHandler(poll_answer))
    app.add_handler(MessageHandler(filters.Document.PDF, pdf_handler))
    # Telegram command handlers normally use lowercase command names.
    # This Regex handler also accepts the exact /ASHISH trigger in groups.
    app.add_handler(MessageHandler(
        filters.Regex(r"^/ASHISH(?:@\\w+)?(?:\\s+.*)?$"),
        ashish_cmd
    ))
    app.add_handler(CommandHandler("ashish", ashish_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

    app.add_error_handler(error_handler)
    return app

if __name__ == "__main__":
    log.info("NEET WALA BOT starting...")
    build_app().run_polling(allowed_updates=["message", "callback_query", "poll_answer"])
