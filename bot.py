import logging
import os
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from supabase import create_client, Client
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
from support_config import SUPPORT_CHAT_IDS

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL is missing")
if not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY is missing")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("preparena_student")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

MAIN_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("📝 Available Tests", callback_data="st|tests"),
        InlineKeyboardButton("🏆 My Results", callback_data="st|results"),
    ],
    [
        InlineKeyboardButton("👤 My Profile", callback_data="st|profile"),
        InlineKeyboardButton("💬 Support", callback_data="st|support"),
    ],
])

# Telegram user id -> currently selected numerical question id.
awaiting_numerical: dict[int, str] = {}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def private(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == "private")


def student_id(update: Update) -> Optional[int]:
    return update.effective_user.id if update.effective_user else None


def display_name(update: Update) -> str:
    user = update.effective_user
    if not user:
        return "Student"
    name = " ".join(x for x in [user.first_name, user.last_name] if x).strip()
    return name or user.username or "Student"


def get_or_create_student(update: Update) -> Optional[dict]:
    uid = student_id(update)
    if uid is None:
        return None
    user = update.effective_user
    name = display_name(update)
    username = user.username if user else None
    try:
        response = supabase.table("students").select("*").eq("telegram_user_id", uid).limit(1).execute()
        if response.data:
            row = response.data[0]
            supabase.table("students").update({"display_name": name, "username": username, "updated_at": now_utc().isoformat()}).eq("id", row["id"]).execute()
            row.update({"display_name": name, "username": username})
            return row
        response = supabase.table("students").insert({"telegram_user_id": uid, "display_name": name, "username": username}).execute()
        return response.data[0] if response.data else None
    except Exception:
        logger.exception("Could not create/update student")
        return None


def main_text() -> str:
    return "🎓 <b>PrepArena</b>\n\nChoose an option below."


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not private(update):
        return
    student = get_or_create_student(update)
    if not student:
        await update.message.reply_text("❌ Could not set up your student account. Please try again.")
        return
    await update.message.reply_text(
        f"👋 Welcome, <b>{student.get('display_name') or 'Student'}</b>!\n\n"
        "Your Telegram account is your permanent PrepArena student identity.\n\n"
        + main_text(),
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


async def available_tests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not private(update):
        return
    message = update.effective_message
    if not message:
        return
    get_or_create_student(update)
    try:
        response = (
            supabase.table("tests")
            .select("id,name,description,status,scheduled_at,duration_seconds")
            .in_("status", ["SCHEDULED", "LIVE"])
            .order("scheduled_at", desc=False)
            .limit(50)
            .execute()
        )
        tests = response.data or []
    except Exception:
        logger.exception("Failed to load available tests")
        await message.reply_text("❌ Could not load tests right now.")
        return

    if not tests:
        await message.reply_text("📝 <b>Available Tests</b>\n\nNo tests are available right now.", parse_mode="HTML", reply_markup=MAIN_KEYBOARD)
        return

    keyboard = []
    for test in tests:
        status = test.get("status")
        icon = "🟢" if status == "LIVE" else "🗓"
        keyboard.append([InlineKeyboardButton(f"{icon} {test.get('name') or 'Unnamed Test'}", callback_data=f"st|open|{test['id']}")])
    keyboard.append([InlineKeyboardButton("🏠 Main Menu", callback_data="st|home")])
    await message.reply_text("📝 <b>Available Tests</b>\n\nSelect a test:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def open_test(query, test_id: str) -> None:
    try:
        response = supabase.table("tests").select("id,name,description,status,scheduled_at,duration_seconds,instructions,started_at,ends_at").eq("id", test_id).limit(1).execute()
        if not response.data:
            await query.edit_message_text("❌ Test not found.")
            return
        test = response.data[0]
        q_count = supabase.table("questions").select("id").eq("test_id", test_id).execute().data or []
        status = test.get("status")
        if status == "LIVE":
            action = InlineKeyboardButton("▶️ Join Test", callback_data=f"st|join|{test_id}")
        else:
            action = InlineKeyboardButton("🗓 Waiting for Host", callback_data=f"st|noop|{test_id}")
        scheduled = parse_dt(test.get("scheduled_at"))
        schedule_text = scheduled.astimezone().strftime("%d-%m-%Y %H:%M") if scheduled else "Not scheduled"
        duration = test.get("duration_seconds")
        duration_text = f"{int(duration)//60} min" if duration else "Not set"
        text = (
            f"📝 <b>{test.get('name') or 'Test'}</b>\n\n"
            f"{test.get('description') or 'No description.'}\n\n"
            f"Status: <b>{status}</b>\n"
            f"Questions: {len(q_count)}\n"
            f"Duration: {duration_text}\n"
            f"Scheduled: {schedule_text}\n"
        )
        if test.get("instructions"):
            text += f"\n📋 <b>Instructions</b>\n{test['instructions']}\n"
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[action], [InlineKeyboardButton("⬅️ Back", callback_data="st|tests")]]))
    except Exception:
        logger.exception("Failed to open test")
        await query.edit_message_text("❌ Could not load this test.")


def answer_keyboard(question_id: str, question_type: str, selected: list[str]) -> InlineKeyboardMarkup:
    if question_type == "NUMERICAL":
        rows = [[InlineKeyboardButton("✏️ Enter Answer", callback_data=f"st|num|{question_id}")]]
    else:
        rows = []
        opts = ["A", "B", "C", "D"]
        for i in range(0, len(opts), 2):
            row = []
            for key in opts[i:i+2]:
                prefix = "🟩" if key in selected else "⬜"
                row.append(InlineKeyboardButton(f"{prefix} {key}", callback_data=f"st|ans|{question_id}|{key}"))
            rows.append(row)
    return InlineKeyboardMarkup(rows)


def get_participant(test_id: str, sid: str) -> Optional[dict]:
    response = supabase.table("participants").select("*").eq("test_id", test_id).eq("student_id", sid).limit(1).execute()
    return response.data[0] if response.data else None


def test_is_live(test: dict) -> bool:
    if test.get("status") != "LIVE":
        return False
    ends = parse_dt(test.get("ends_at"))
    return not ends or now_utc() < ends


async def join_test(query, update: Update, test_id: str) -> None:
    student = get_or_create_student(update)
    if not student:
        await query.edit_message_text("❌ Student account could not be loaded.")
        return
    try:
        test_resp = supabase.table("tests").select("*").eq("id", test_id).limit(1).execute()
        if not test_resp.data:
            await query.edit_message_text("❌ Test not found.")
            return
        test = test_resp.data[0]
        if not test_is_live(test):
            await query.edit_message_text("⏳ This test is not accepting answers right now.")
            return
        participant = get_participant(test_id, student["id"])
        if not participant:
            response = supabase.table("participants").insert({"test_id": test_id, "student_id": student["id"], "started_at": now_utc().isoformat(), "status": "IN_PROGRESS"}).execute()
            participant = response.data[0]
        elif participant.get("status") != "IN_PROGRESS":
            await query.edit_message_text("ℹ️ You have already submitted this test.")
            return

        await query.edit_message_text(
            f"🟢 <b>{test.get('name') or 'Test'} started</b>\n\n"
            f"{test.get('instructions') or 'Answer the questions using the buttons below.'}\n\n"
            "All questions are being sent now. The server test timer is authoritative.",
            parse_mode="HTML",
        )
        questions = (
            supabase.table("questions")
            .select("id,question_number,question_text,question_type,marks,negative_marks,photo_file_id")
            .eq("test_id", test_id)
            .order("question_number")
            .execute().data or []
        )
        for q in questions:
            opts = supabase.table("question_options").select("option_key,option_text").eq("question_id", q["id"]).order("display_order").execute().data or []
            body = f"<b>Q{q['question_number']}</b>\n\n{q.get('question_text') or ''}\n\nMarks: {q.get('marks', 0)} | Negative: {q.get('negative_marks', 0)}"
            if opts:
                body += "\n\n" + "\n".join(f"{o['option_key']}. {o['option_text']}" for o in opts)
            if q.get("photo_file_id"):
                await update.effective_chat.send_photo(q["photo_file_id"], caption=body, parse_mode="HTML", reply_markup=answer_keyboard(q["id"], q["question_type"], []))
            else:
                await update.effective_chat.send_message(body, parse_mode="HTML", reply_markup=answer_keyboard(q["id"], q["question_type"], []))
        await update.effective_chat.send_message("⏱ The common test timer is controlled by the server. Late answers will be rejected.", reply_markup=MAIN_KEYBOARD)
    except Exception:
        logger.exception("Failed to join test")
        await query.edit_message_text("❌ Could not start the test. Please try again.")


def get_selected(participant_id: str, question_id: str) -> list[str]:
    response = supabase.table("answers").select("selected_options,numeric_answer").eq("participant_id", participant_id).eq("question_id", question_id).limit(1).execute()
    if not response.data:
        return []
    value = response.data[0].get("selected_options")
    return value if isinstance(value, list) else []


def active_participant(question_id: str, update: Update) -> Optional[dict]:
    student = get_or_create_student(update)
    if not student:
        return None
    q = supabase.table("questions").select("test_id").eq("id", question_id).limit(1).execute().data
    if not q:
        return None
    return get_participant(q[0]["test_id"], student["id"])


def save_answer(participant_id: str, question_id: str, selected: Optional[list[str]] = None, numeric: Optional[float] = None) -> None:
    payload = {"participant_id": participant_id, "question_id": question_id, "answered_at": now_utc().isoformat(), "updated_at": now_utc().isoformat()}
    if selected is not None:
        payload["selected_options"] = selected
        payload["numeric_answer"] = None
    else:
        payload["selected_options"] = None
        payload["numeric_answer"] = numeric
    existing = supabase.table("answers").select("id").eq("participant_id", participant_id).eq("question_id", question_id).limit(1).execute().data
    if existing:
        supabase.table("answers").update(payload).eq("id", existing[0]["id"]).execute()
    else:
        supabase.table("answers").insert(payload).execute()


async def _callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    parts = (query.data or "").split("|")
    if len(parts) < 2 or parts[0] != "st":
        return
    action = parts[1]
    if action == "home":
        await query.message.reply_text(main_text(), parse_mode="HTML", reply_markup=MAIN_KEYBOARD)
        await query.answer("Main menu")
        return
    if action == "tests":
        await available_tests(update, context)
        await query.answer("Available tests")
        return
    if action == "results":
        # Results are private student data; do not expose them in groups.
        if not private(update):
            await query.answer("Please open the bot privately to view your results.", show_alert=True)
            return
        await results(update, context)
        await query.answer("Results")
        return
    if action == "profile":
        # Profile is private student data; do not expose it in groups.
        if not private(update):
            await query.answer("Please open the bot privately to view your profile.", show_alert=True)
            return
        await profile(update, context)
        await query.answer("Profile")
        return
    if action == "support":
        if not private(update):
            await query.answer("Please open the bot privately to contact support.", show_alert=True)
            return
        await support(update, context)
        await query.answer("Support")
        return
    if action == "open" and len(parts) >= 3:
        await open_test(query, parts[2])
        await query.answer("Test opened")
        return
    if action == "noop":
        await query.answer("The host has not started this test yet.", show_alert=True)
        return
    if action == "join" and len(parts) >= 3:
        await join_test(query, update, parts[2])
        await query.answer("Test opened")
        return
    if action in ("ans", "num") and len(parts) >= 3:
        question_id = parts[2]
        participant = active_participant(question_id, update)
        if not participant:
            await query.answer("You are not participating in this test.", show_alert=True)
            return
        test = supabase.table("tests").select("status,ends_at").eq("id", participant["test_id"]).limit(1).execute().data
        if not test or not test_is_live(test[0]) or participant.get("status") != "IN_PROGRESS":
            await query.answer("Test has ended. Your answer was not changed.", show_alert=True)
            return
        q = supabase.table("questions").select("question_type").eq("id", question_id).limit(1).execute().data
        if not q:
            return
        if action == "num":
            awaiting_numerical[update.effective_user.id] = question_id
            await query.message.reply_text("🔢 Send your numerical answer as a number.")
            return
        key = parts[3] if len(parts) >= 4 else ""
        qtype = q[0].get("question_type")
        if qtype == "MCQ":
            save_answer(participant["id"], question_id, [key])
            await query.answer(f"Answer {key} saved")
        elif qtype == "MULTIPLE_CORRECT":
            selected = get_selected(participant["id"], question_id)
            if key in selected:
                selected.remove(key)
            else:
                selected.append(key)
            selected.sort()
            save_answer(participant["id"], question_id, selected)
            await query.edit_message_reply_markup(reply_markup=answer_keyboard(question_id, qtype, selected))
            await query.answer("Selection saved")
        return


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Top-level callback wrapper: never leave Telegram button clicks hanging."""
    query = update.callback_query
    if not query:
        return
    try:
        await _callbacks(update, context)
    except Exception:
        logger.exception("Student button callback failed: %s", query.data)
        try:
            await query.answer("Something went wrong. Please try again.", show_alert=True)
        except Exception:
            pass



async def numerical_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not private(update) or not update.message or not update.message.text:
        return
    uid = student_id(update)
    if uid is None or uid not in awaiting_numerical:
        return
    qid = awaiting_numerical.pop(uid)
    try:
        value = float(update.message.text.strip())
        participant = active_participant(qid, update)
        if not participant:
            await update.message.reply_text("❌ Participation not found.")
            return
        test = supabase.table("tests").select("status,ends_at").eq("id", participant["test_id"]).limit(1).execute().data
        if not test or not test_is_live(test[0]) or participant.get("status") != "IN_PROGRESS":
            await update.message.reply_text("⏱ Test has ended. Your answer was not changed.")
            return
        save_answer(participant["id"], qid, numeric=value)
        await update.message.reply_text("✅ Numerical answer saved.")
    except ValueError:
        awaiting_numerical[uid] = qid
        await update.message.reply_text("❌ Please send a valid number, for example 12 or 12.5.")
    except Exception:
        logger.exception("Failed to save numerical answer")
        await update.message.reply_text("❌ Could not save the answer. Please try again.")


async def results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not private(update):
        return
    message = update.effective_message
    if not message:
        return
    student = get_or_create_student(update)
    if not student:
        return
    try:
        response = (
            supabase.table("results")
            .select("score,total_marks,correct_count,wrong_count,unattempted_count,percentage,rank,participants(tests(name))")
            .eq("participants.student_id", student["id"])
            .order("calculated_at", desc=True)
            .limit(30)
            .execute()
        )
        rows = response.data or []
        if not rows:
            await message.reply_text("🏆 <b>My Results</b>\n\nNo results are available yet.", parse_mode="HTML", reply_markup=MAIN_KEYBOARD)
            return
        lines = ["🏆 <b>My Results</b>", ""]
        for r in rows:
            p = r.get("participants") or {}
            t = p.get("tests") or {}
            lines.append(f"<b>{t.get('name') or 'Test'}</b>")
            lines.append(f"Score: {r.get('score', 0)}/{r.get('total_marks', 0)} | Rank: {r.get('rank') or '-'}")
            lines.append(f"Correct: {r.get('correct_count', 0)} | Wrong: {r.get('wrong_count', 0)} | Unattempted: {r.get('unattempted_count', 0)}")
            lines.append(f"Percentage: {r.get('percentage', 0)}%\n")
        await message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=MAIN_KEYBOARD)
    except Exception:
        logger.exception("Failed to load results")
        await message.reply_text("❌ Could not load your results right now.")


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not private(update):
        return
    message = update.effective_message
    if not message:
        return
    student = get_or_create_student(update)
    if not student:
        return
    await message.reply_text(
        f"👤 <b>My Profile</b>\n\n"
        f"Name: {student.get('display_name') or 'Student'}\n"
        f"Username: @{student.get('username')}" if student.get('username') else f"👤 <b>My Profile</b>\n\nName: {student.get('display_name') or 'Student'}",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not private(update):
        return
    message = update.effective_message
    if not message:
        return
    await message.reply_text("💬 Send your support message in your next message. It will be forwarded to the PrepArena support account.")
    context.user_data["awaiting_support"] = True


async def support_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not private(update) or not update.effective_message or not update.effective_message.text:
        return
    if not context.user_data.get("awaiting_support"):
        return
    context.user_data.pop("awaiting_support", None)
    try:
        user = update.effective_user
        header = (
            f"💬 PrepArena Student Support\nFrom: {display_name(update)}\nUsername: @{user.username}"
            if user and user.username
            else f"💬 PrepArena Student Support\nFrom: {display_name(update)}"
        )
        for chat_id in SUPPORT_CHAT_IDS:
            await context.bot.send_message(
                chat_id=int(chat_id),
                text=header + "\n\n" + update.effective_message.text,
            )
        await update.effective_message.reply_text(
            "✅ Your message has been sent to support.",
            reply_markup=MAIN_KEYBOARD,
        )
    except Exception:
        logger.exception("Failed to forward support")
        await update.effective_message.reply_text(
            "❌ Could not send your message right now.",
            reply_markup=MAIN_KEYBOARD,
        )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not private(update) or not update.effective_message or not update.effective_message.text:
        return
    if context.user_data.get("awaiting_support"):
        await support_message(update, context)
        return
    uid = student_id(update)
    if uid is not None and uid in awaiting_numerical:
        await numerical_handler(update, context)



async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled student bot error", exc_info=context.error)


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("results", results))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(error_handler)
    logger.info("PrepArena Student Bot starting")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()