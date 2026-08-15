import os
import json
import asyncio
import logging
import re
import sqlite3
import tempfile
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, PollAnswerHandler, filters
)
from google import genai
from google.genai import types

try:
    import pymupdf
except ImportError:
    pymupdf = None

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_API_KEY_2 = os.getenv("GEMINI_API_KEY_2", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.7-flash").strip()
GEMINI_FALLBACK_MODELS = [
    GEMINI_MODEL,
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
]

DB_PATH = os.getenv("DB_PATH", "data/neet_ai.db")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("NEET_WALA_BOT")

clients = {}
quiz_timer_tasks = {}
pdf_requests = {}

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

        CREATE TABLE IF NOT EXISTS schedules(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            owner_id INTEGER NOT NULL,
            time_hm TEXT NOT NULL,
            topic TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 5,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_run_date TEXT
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
    # Use ONLY the configured stable model. Do not silently fall back to a
    # different model (which can unexpectedly hit a different quota).
    return [GEMINI_MODEL or "gemini-3.7-flash"]

def ask_ai(prompt, system=None, json_mode=False):
    keys = [k for k in (GEMINI_API_KEY, GEMINI_API_KEY_2) if k]
    if not keys:
        raise RuntimeError(
            "GEMINI_API_KEY missing. Railway Variables में API key जोड़ें."
        )

    last = None
    for key_no, key in enumerate(keys, 1):
        client = get_client(key)
        for model in model_candidates(client):
            try:
                kwargs = {"max_output_tokens": 8000}
                if system:
                    kwargs["system_instruction"] = system
                if json_mode:
                    kwargs["response_mime_type"] = "application/json"

                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(**kwargs),
                )
                text = getattr(response, "text", None)
                if text:
                    log.info("Gemini success: %s using API key #%s", model, key_no)
                    return text.strip()
            except Exception as e:
                last = e
                msg = str(e)
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    log.warning("Gemini quota on API key #%s; trying next key.", key_no)
                    continue
                log.warning("Gemini model %s failed on API key #%s: %s",
                            model, key_no, e)

    raise RuntimeError(f"Gemini request failed on both API keys: {last}")


SYSTEM = """तुम NEET परीक्षा के लिए हिंदी AI Study Assistant हो।
उत्तर NCERT-केंद्रित, तथ्यात्मक और सरल हिंदी में दो।
मनगढ़ंत तथ्य या citation मत बनाओ।"""

# ---------------- HELPERS ----------------

def parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.replace("```json", "", 1).replace("```", "", 1).strip()
    return json.loads(text)

def make_pdf_question_prompt(book_text, count):
    """Build a prompt for generating fresh NEET Biology questions from PDF text."""
    return f"""
नीचे दी गई PDF सामग्री के आधार पर ठीक {count} नए, मूल NEET Biology MCQ बनाओ।

PDF सामग्री:
{book_text}

महत्वपूर्ण नियम:
- प्रश्न PDF के concepts/facts/relationships पर आधारित हों।
- PDF में जितने मूल प्रश्न हैं, उनकी संख्या output को सीमित नहीं करती।
- प्रश्नों को शब्दशः copy मत करो; नए तरीके से पूछो।
- एक ही तथ्य को मामूली शब्द बदलकर दोबारा मत पूछो।
- NEET level के conceptual, statement-based, application और Assertion-Reason प्रश्नों का अच्छा mix रखो।
- NCERT-केंद्रित रहो।
- हिंदी में बनाओ।
- हर प्रश्न में ठीक 4 options और केवल 1 सही उत्तर हो।
- correct_index केवल 0,1,2,3 हो।
- explanation मत बनाओ; केवल question, options और correct_index दो।

सिर्फ JSON array दो:
[
  {{
    "question": "प्रश्न",
    "options": ["A", "B", "C", "D"],
    "correct_index": 0
  }}
]
"""

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
}}

नियम:
- options ठीक 4 हों
- correct_index केवल 0,1,2,3 हो
- हिंदी में
- एक ही सही उत्तर हो
- duplicate या अस्पष्ट प्रश्न नहीं
- explanation मत बनाओ; केवल question, options और correct_index दो
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
    "correct_index": 0
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
- explanation मत बनाओ; केवल question, options और correct_index दो
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
    """Generate exactly count valid, non-duplicate questions in small batches."""
    clean, seen = [], set()
    attempts = 0
    max_attempts = max(6, count // 2 + 4)

    while len(clean) < count and attempts < max_attempts:
        remaining = count - len(clean)
        batch = min(5, remaining)

        try:
            raw = await asyncio.to_thread(
                ask_ai, make_quiz_prompt(topic, batch), SYSTEM, True
            )
            data = parse_json(raw)
        except Exception:
            attempts += 1
            await asyncio.sleep(0.8)
            continue

        if isinstance(data, list):
            for q in data:
                if not isinstance(q, dict):
                    continue
                try:
                    idx = int(q.get("correct_index", -1))
                except Exception:
                    idx = -1
                opts = q.get("options")
                question = str(q.get("question", "")).strip()
                if (
                    question and isinstance(opts, list) and len(opts) == 4
                    and idx in (0, 1, 2, 3)
                ):
                    key = " ".join(question.lower().split())
                    if key not in seen:
                        seen.add(key)
                        clean.append({
                            "question": question[:300],
                            "options": [str(x)[:100] for x in opts],
                            "correct_index": idx,
                            "explanation": "",
                        })
                        if len(clean) >= count:
                            break
        attempts += 1

    if len(clean) < count:
        raise ValueError(
            f"AI से {len(clean)}/{count} unique questions बने। "
            f"Gemini quota/network की स्थिति भी check करें."
        )
    return clean[:count]



# ---------------- DAILY SCHEDULER ----------------
IST = ZoneInfo("Asia/Kolkata")
scheduler_task = None

async def chat_admin(update):
    chat=update.effective_chat; user=update.effective_user
    if chat.type=="private": return True
    try:
        m=await update.get_bot().get_chat_member(chat.id,user.id)
        return m.status in ("creator","administrator")
    except Exception:
        return False

def add_schedule(chat_id, owner_id, time_hm, topic, count):
    with db() as c:
        cur=c.execute("INSERT INTO schedules(chat_id,owner_id,time_hm,topic,count) VALUES(?,?,?,?,?)",
                      (chat_id,owner_id,time_hm,topic,count))
        return cur.lastrowid

def list_schedules(chat_id):
    with db() as c:
        return c.execute("SELECT id,time_hm,topic,count,enabled FROM schedules WHERE chat_id=? ORDER BY id",(chat_id,)).fetchall()

def delete_schedule(chat_id,sid):
    with db() as c:
        return c.execute("DELETE FROM schedules WHERE chat_id=? AND id=?",(chat_id,sid)).rowcount>0

def due_schedules():
    now=datetime.now(IST); hm=now.strftime("%H:%M"); today=now.strftime("%Y-%m-%d")
    with db() as c:
        return c.execute("""SELECT id,chat_id,topic,count FROM schedules
                            WHERE enabled=1 AND time_hm=?
                            AND (last_run_date IS NULL OR last_run_date<>?)""",(hm,today)).fetchall()

def mark_schedule_run(sid,today):
    with db() as c:c.execute("UPDATE schedules SET last_run_date=? WHERE id=?",(today,sid))

async def run_scheduled_poll(bot,row):
    sid=row["id"]; chat_id=row["chat_id"]; topic=row["topic"]; count=int(row["count"])
    mark_schedule_run(sid,datetime.now(IST).strftime("%Y-%m-%d"))
    try:
        status=await bot.send_message(chat_id,f"🗓️ Scheduled Quiz\n📚 {topic}\n🤖 {count} questions बन रहे हैं...\n⏱️ कोई timer नहीं होगा।")
        questions=await generate_poll_questions(topic,count)
        if not questions:
            await status.edit_text("❌ Scheduled quiz के questions नहीं बने।"); return
        for n,q in enumerate(questions,1):
            msg=await bot.send_poll(chat_id=chat_id,question=f"Q{n}/{len(questions)}  {q['question']}",
                                    options=q["options"],type="quiz",
                                    correct_option_id=q["correct_index"],is_anonymous=False)
            save_poll(msg.poll.id,chat_id,topic,q["question"],q["correct_index"],"")
            if n<len(questions): await asyncio.sleep(.8)
        await status.edit_text(f"✅ {len(questions)} scheduled polls भेज दिए!\n📚 {topic}\n⏱️ कोई timer नहीं है।")
    except Exception as e:
        log.exception("Scheduled poll failed")
        try: await status.edit_text(f"❌ Scheduled Poll error:\n{str(e)[:1000]}")
        except Exception: pass

async def scheduler_loop(bot):
    log.info("Daily scheduler started: Asia/Kolkata")
    while True:
        try:
            for row in due_schedules():
                asyncio.create_task(run_scheduled_poll(bot,row))
        except Exception: log.exception("Scheduler loop error")
        await asyncio.sleep(20)

async def scheduler_post_init(app):
    global scheduler_task
    scheduler_task=asyncio.create_task(scheduler_loop(app.bot))

async def scheduler_post_shutdown(app):
    global scheduler_task
    if scheduler_task:
        scheduler_task.cancel()
        try: await scheduler_task
        except asyncio.CancelledError: pass
        scheduler_task=None

async def schedule_cmd(update,context):
    if not await chat_admin(update):
        await update.effective_message.reply_text("❌ Schedule बनाने के लिए group admin होना जरूरी है."); return
    args=list(context.args)
    if len(args)<2:
        await update.effective_message.reply_text("उदाहरण:\n/shedulear 21:00 कोशिका 5\n🕘 समय IST में होगा.\n⏱️ Poll में timer नहीं होगा."); return
    hm=args[0]
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d",hm):
        await update.effective_message.reply_text("❌ Time format HH:MM रखें. उदाहरण: /shedulear 21:00 कोशिका 5"); return
    count=5
    if args[-1].isdigit():
        count=max(1,min(50,int(args[-1]))); args=args[:-1]
    topic=" ".join(args[1:]).strip() or "NEET Biology"
    sid=add_schedule(update.effective_chat.id,update.effective_user.id,hm,topic,count)
    await update.effective_message.reply_text(f"✅ Schedule #{sid} बन गया!\n🕘 रोज़ {hm} IST\n📚 {topic}\n📝 {count} questions\n⏱️ कोई timer नहीं\n\nबंद करें: /sheduleardel {sid}")

async def schedule_list_cmd(update,context):
    if not await chat_admin(update):
        await update.effective_message.reply_text("❌ केवल group admin."); return
    rows=list_schedules(update.effective_chat.id)
    if not rows: await update.effective_message.reply_text("📭 कोई schedule नहीं है."); return
    await update.effective_message.reply_text("🗓️ Schedules (IST):\n"+"\n".join(
        f"#{r['id']} • {r['time_hm']} • {r['topic']} • {r['count']} Q" for r in rows))

async def schedule_delete_cmd(update,context):
    if not await chat_admin(update):
        await update.effective_message.reply_text("❌ केवल group admin."); return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("उदाहरण: /sheduleardel 1"); return
    sid=int(context.args[0])
    await update.effective_message.reply_text(
        f"🗑️ Schedule #{sid} हटा दिया गया." if delete_schedule(update.effective_chat.id,sid)
        else f"❌ Schedule #{sid} नहीं मिला.")

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
        "/askar आपका सवाल — AI जवाब\n"
        "/leaderboardar — Top 10\n"
        "/profilear — आपकी progress\n"
        "/resumear — अधूरा quiz जारी करें\n"
        "/pdfar — PDF भेजें → default 50 NEET Bio polls\n"
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
        f"⏳ AI {topic} पर {count} प्रश्न बना रहा है...\n"
        f"⏱️ हर प्रश्न के लिए 30 सेकंड।"
    )
    try:
        questions = []
        # Small batches reduce malformed JSON and allow retrying one failed batch.
        for start_i in range(0, count, 5):
            batch = min(5, count - start_i)
            for attempt in range(3):
                try:
                    part = await generate_questions(topic, batch)
                    if len(part) >= batch:
                        questions.extend(part[:batch])
                        break
                except Exception as e:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(1)
        if len(questions) < count:
            raise ValueError(f"AI ने केवल {len(questions)}/{count} questions दिए.")
        save_quiz(update.effective_user.id, topic, questions[:count])
        await send_next_inline_question(update, context, update.effective_user.id)
    except Exception as e:
        await update.message.reply_text(
            f"❌ Quiz generation error:\n{str(e)[:1000]}"
        )

async def chapter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("उदाहरण: /chapterar कोशिका 10")
        return
    await quiz_cmd(update, context)

async def _quiz_timeout(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    user_id = data["user_id"]
    expected_index = data["index"]
    row = get_quiz(user_id)
    if not row or row["current_index"] != expected_index:
        return

    questions = json.loads(row["questions_json"])
    if expected_index >= len(questions):
        return

    q = questions[expected_index]
    update_quiz(user_id, expected_index + 1, row["score"])
    await context.bot.send_message(
        chat_id=data["chat_id"],
        text=f"⏰ समय समाप्त!\nसही उत्तर: {chr(65+q['correct_index'])}) "
             f"{q['options'][q['correct_index']]}"
    )

    new_row = get_quiz(user_id)
    if new_row and new_row["current_index"] < len(questions):
        await send_next_inline_question(
            data["chat_id"], context, user_id
        )
    elif new_row:
        await context.bot.send_message(
            chat_id=data["chat_id"],
            text=f"🏁 Quiz पूरा!\n🏆 Score: {new_row['score']}\n"
                 f"📚 {new_row['topic']}\nकुल प्रश्न: {len(questions)}"
        )
        clear_quiz(user_id)

async def send_next_inline_question(target, context, user_id=None):
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
    buttons = [[InlineKeyboardButton(
        f"{chr(65+j)}) {opt}", callback_data=f"ans:{i}:{j}"
    )] for j, opt in enumerate(q["options"])]
    text = (
        f"📝 प्रश्न {i+1}/{len(questions)}\n"
        f"📚 {row['topic']}\n"
        f"⏱️ 30 सेकंड\n\n{q['question']}"
    )

    if getattr(target, "message", None):
        sent = await target.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(buttons)
        )
        chat_id = target.effective_chat.id
    elif getattr(target, "effective_message", None):
        sent = await target.effective_message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(buttons)
        )
        chat_id = target.effective_chat.id
    else:
        sent = await context.bot.send_message(
            chat_id=target if isinstance(target, int) else user_id,
            text=text, reply_markup=InlineKeyboardMarkup(buttons)
        )
        chat_id = sent.chat_id

    old = quiz_timer_tasks.pop(user_id, None)
    if old:
        old.cancel()
    task = asyncio.create_task(
        _quiz_timeout_after_30(context, user_id, i, chat_id)
    )
    quiz_timer_tasks[user_id] = task

async def _quiz_timeout_after_30(context, user_id, expected_index, chat_id):
    try:
        await asyncio.sleep(30)
        row = get_quiz(user_id)
        if not row or row["current_index"] != expected_index:
            return
        questions = json.loads(row["questions_json"])
        q = questions[expected_index]
        update_quiz(user_id, expected_index + 1, row["score"])
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ 30 सेकंड पूरे!\n"
                 f"सही उत्तर: {chr(65+q['correct_index'])}) "
                 f"{q['options'][q['correct_index']]}\n\n"
        )
        new_row = get_quiz(user_id)
        if new_row and new_row["current_index"] < len(questions):
            await send_next_inline_question(chat_id, context, user_id)
        elif new_row:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🏁 Quiz पूरा!\n🏆 Score: {new_row['score']}\n"
                     f"📚 {new_row['topic']}\nकुल प्रश्न: {len(questions)}"
            )
            clear_quiz(user_id)
    finally:
        quiz_timer_tasks.pop(user_id, None)

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

    old = quiz_timer_tasks.pop(query.from_user.id, None)
    if old:
        old.cancel()

    q = questions[i]
    correct = q["correct_index"]
    ok = chosen == correct
    score = row["score"] + (4 if ok else -1)

    result = "✅ सही उत्तर!" if ok else f"❌ गलत! सही उत्तर: {chr(65+correct)}) {q['options'][correct]}"
    update_quiz(query.from_user.id, i + 1, score)
    add_result(query.from_user.id, row["topic"], 1 if ok else 0, 0 if ok else 1)

    await query.edit_message_text(
        f"{result}\n\n📊 Current score: {score}"
    )

    new_row = get_quiz(query.from_user.id)
    if new_row:
        questions2 = json.loads(new_row["questions_json"])
        if new_row["current_index"] >= len(questions2):
            await query.message.reply_text(
                f"🏁 Quiz पूरा!\n🏆 Score: {new_row['score']}\n"
                f"📚 {new_row['topic']}\nकुल प्रश्न: {len(questions2)}"
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
    "correct_index": 0
  }}
]

नियम:
- ठीक {count} प्रश्न
- हर प्रश्न में ठीक 4 options
- केवल एक सही उत्तर
- NEET level
- NCERT-केंद्रित
- सभी प्रश्न एक-दूसरे से अलग
- PDF में दिए गए concepts/content से नए प्रश्न बनाओ; PDF के 22 मूल प्रश्नों की संख्या को output limit मत मानो
- एक ही तथ्य को अलग शब्दों में दोहराने से बचो
- कथन/Assertion-Reason/Conceptual/Application प्रश्नों का अच्छा mix
- हिंदी में
- प्रश्न बहुत लंबे न हों
- explanation मत बनाओ; केवल question, options और correct_index दो
"""

async def generate_poll_questions(topic, count, source_text=""):
    """Generate exactly count unique poll questions, refilling duplicates."""
    clean, seen = [], set()
    attempts = 0
    max_attempts = max(8, count // 3 + 6)

    while len(clean) < count and attempts < max_attempts:
        remaining = count - len(clean)
        batch = min(8, remaining)

        prompt = make_multi_poll_prompt(topic, batch)
        if source_text:
            prompt += (
                "\n\nSOURCE MATERIAL:\n"
                + source_text[:100000]
                + "\n\nCreate new questions from the concepts in this material; "
                  "do not copy source questions verbatim."
            )

        try:
            raw = await asyncio.to_thread(
                ask_ai, prompt, SYSTEM, True
            )
            data = parse_json(raw)
        except Exception:
            attempts += 1
            await asyncio.sleep(0.8)
            continue

        if isinstance(data, list):
            for q in data:
                if not isinstance(q, dict):
                    continue
                try:
                    idx = int(q.get("correct_index", -1))
                except Exception:
                    idx = -1
                opts = q.get("options")
                question = str(q.get("question", "")).strip()
                if (
                    question and isinstance(opts, list) and len(opts) == 4
                    and idx in (0, 1, 2, 3)
                ):
                    key = " ".join(question.lower().split())
                    if key not in seen:
                        seen.add(key)
                        clean.append({
                            "question": question[:300],
                            "options": [str(x)[:100] for x in opts],
                            "correct_index": idx,
                            "explanation": "",
                        })
                        if len(clean) >= count:
                            break
        attempts += 1

    return clean[:count]



async def poll_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    args = list(context.args)
    count = 5

    if args and args[-1].isdigit():
        count = max(1, min(50, int(args[-1])))
        args = args[:-1]

    topic = " ".join(args).strip() if args else "NEET Biology"

    status = await update.effective_message.reply_text(
        f"🤖 {topic} पर {count} Quiz Polls बना रहा हूँ...\n"
        f"⏱️ कोई timer नहीं होगा।"
    )
    try:
        questions = await generate_poll_questions(topic, count)
        if len(questions) < count:
            raise ValueError(
                f"AI से {len(questions)}/{count} unique questions ही बने। "
                f"दोबारा प्रयास करें या दूसरा topic दें."
            )

        for n, q in enumerate(questions, 1):
            msg = await context.bot.send_poll(
                chat_id=update.effective_chat.id,
                question=f"Q{n}/{count}  {q['question']}",
                options=q["options"],
                type="quiz",
                correct_option_id=q["correct_index"],
                is_anonymous=False,
            )
            save_poll(msg.poll.id, update.effective_chat.id, topic,
                      q["question"], q["correct_index"], "")
            if n < len(questions):
                await asyncio.sleep(0.8)

        await status.edit_text(
            f"✅ {count} Quiz Polls भेज दिए गए!\n"
            f"📚 {topic}\n⏱️ कोई timer नहीं है।"
        )
    except Exception as e:
        log.exception("Multi-poll generation failed")
        await status.edit_text(f"❌ Poll error:\n{str(e)[:1400]}")


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

    # No explanation is generated or sent. Keep poll answering fast.
    result = "✅ सही उत्तर!" if ok else "❌ गलत उत्तर!"
    dm_text = (
        f"{result}\n\n"
        f"📚 {info['topic']}\n"
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
            "Could not DM poll result to user %s: %s",
            ans.user.id,
            e,
        )

async def pdf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = 50
    if context.args and context.args[-1].isdigit():
        count = max(1, min(90, int(context.args[-1])))
    pdf_requests[update.effective_chat.id] = count
    await update.message.reply_text(
        f"📚 PDF भेजें। मैं उससे {count} NEET Biology Quiz Polls बनाऊँगा.\n\n"
        f"🧠 NEET level\n🧬 Assertion-Reason / कथन-आधारित भी\n"
        f"⏱️ Poll में कोई timer नहीं होगा।\n\n"
        f"बदलना हो तो: /pdfar 30, /pdfar 50 या /pdfar 90"
    )

async def generate_pdf_questions(book_text, count=50):
    """Generate exactly count unique questions from PDF concepts."""
    clean, seen = [], set()
    attempts = 0
    max_attempts = max(10, count // 3 + 8)

    while len(clean) < count and attempts < max_attempts:
        remaining = count - len(clean)
        batch = min(8, remaining)

        prompt = make_pdf_question_prompt(book_text[:180000], batch)
        prompt += (
            "\n\nIMPORTANT: The PDF may contain fewer original questions than "
            "the requested output. Use its concepts, facts and relationships "
            "to create NEW NEET-level questions. Do not copy questions verbatim "
            "and do not repeat the same fact with trivial wording changes."
        )

        try:
            raw = await asyncio.to_thread(
                ask_ai, prompt, SYSTEM, True
            )
            data = parse_json(raw)
        except Exception:
            attempts += 1
            await asyncio.sleep(0.8)
            continue

        if isinstance(data, list):
            for q in data:
                if not isinstance(q, dict):
                    continue
                try:
                    idx = int(q.get("correct_index", -1))
                except Exception:
                    idx = -1
                opts = q.get("options")
                question = str(q.get("question", "")).strip()
                if (
                    question and isinstance(opts, list) and len(opts) == 4
                    and idx in (0, 1, 2, 3)
                ):
                    key = " ".join(question.lower().split())
                    if key not in seen:
                        seen.add(key)
                        clean.append({
                            "question": question[:300],
                            "options": [str(x)[:100] for x in opts],
                            "correct_index": idx,
                            "explanation": "",
                        })
                        if len(clean) >= count:
                            break
        attempts += 1

    return clean[:count]



async def post_pdf_polls(chat_id, questions, bot, status_message=None):
    total=len(questions)
    for i,q in enumerate(questions,1):
        msg=await bot.send_poll(
            chat_id=chat_id,
            question=f"Q{i}/{total}  {q["question"]}"[:300],
            options=q["options"], type="quiz",
            correct_option_id=q["correct_index"], is_anonymous=False
        )
        save_poll(msg.poll.id,chat_id,"PDF • NEET Biology",
                  q["question"],q["correct_index"],"")
        if i<total: await asyncio.sleep(0.8)
    if status_message:
        await status_message.edit_text(
            f"✅ {total} PDF Quiz Polls भेज दिए गए।\n⏱️ कोई timer नहीं है।"
        )

async def pdf_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.document: return
    if pymupdf is None:
        await update.message.reply_text("❌ PDF package उपलब्ध नहीं है."); return

    count=pdf_requests.pop(update.effective_chat.id, 50)
    status=await update.message.reply_text(
        f"📄 PDF मिल गई। {count} questions तैयार कर रहा हूँ..."
    )
    path=None
    try:
        tg=await update.message.document.get_file()
        with tempfile.NamedTemporaryFile(suffix=".pdf",delete=False) as f:
            path=f.name
        await tg.download_to_drive(path)
        pdf=pymupdf.open(path)
        text="\n".join(page.get_text() for page in pdf).strip()
        pdf.close()
        if not text:
            await status.edit_text("❌ PDF में selectable text नहीं मिला."); return
        await status.edit_text(
            f"🧠 PDF पढ़ ली। अब {count} NEET Biology questions बन रहे हैं..."
        )
        questions=await generate_pdf_questions(text[:180000],count)
        if len(questions)<count:
            await status.edit_text(
                f"⚠️ {len(questions)}/{count} unique questions बने। "
                f"अभी उपलब्ध questions भेज रहा हूँ..."
            )
        if not questions:
            await status.edit_text("❌ PDF से valid questions नहीं बने."); return
        await post_pdf_polls(update.effective_chat.id,questions,context.bot,status)
    except Exception as e:
        log.exception("PDF pipeline failed")
        try: await status.edit_text(f"❌ PDF → Poll error:\n{str(e)[:1200]}")
        except: pass
    finally:
        if path: Path(path).unlink(missing_ok=True)


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

    app = (Application.builder().token(BOT_TOKEN)
           .post_init(scheduler_post_init)
           .post_shutdown(scheduler_post_shutdown)
           .build())

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
    app.add_handler(CommandHandler("shedulear", schedule_cmd))
    app.add_handler(CommandHandler("schedulear", schedule_cmd))
    app.add_handler(CommandHandler("shedulearlistrar", schedule_list_cmd))
    app.add_handler(CommandHandler("sheduleardel", schedule_delete_cmd))

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
