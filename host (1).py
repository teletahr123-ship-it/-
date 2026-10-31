import os, sys, ast, time, shutil, asyncio, zipfile, subprocess, json
from pathlib import Path
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

TOKEN = os.environ["BOT_TOKEN"]
CHANNEL = int(os.environ["BACKUP_CHANNEL"])
ADMIN = int(os.environ.get("ADMIN_ID") or 0)
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "350"))   # يعمل ثم ينسخ احتياطياً (قبل ~10 دقائق من النهاية)
MAX_USERS = int(os.environ.get("MAX_USERS", "15"))
MAX_FILE = 5 * 1024 * 1024
MAX_UNZIPPED = 20 * 1024 * 1024
MAX_BACKUP = 19 * 1024 * 1024    # البوت لا يستطيع تنزيل أكثر من 20MB

DATA = Path("users").resolve()
START = time.time()
background = set()


def sh(*args):
    return subprocess.run(args, capture_output=True, text=True)


def cname(uid):
    return f"ubot_{uid}"


def is_running(uid):
    return sh("docker", "inspect", "-f", "{{.State.Running}}", cname(uid)).stdout.strip() == "true"


ALIASES = {
    "PIL": "pillow", "cv2": "opencv-python-headless", "yaml": "pyyaml",
    "bs4": "beautifulsoup4", "telegram": "python-telegram-bot",
    "dotenv": "python-dotenv", "sklearn": "scikit-learn", "Crypto": "pycryptodome",
    "pyrogram": "pyrogram tgcrypto", "googleapiclient": "google-api-python-client",
    "dateutil": "python-dateutil", "jwt": "pyjwt", "telebot": "pyTelegramBotAPI",
}


def auto_requirements(d):
    """يولّد requirements.txt من الـ imports إذا لم يرفعه المستخدم."""
    if (d / "requirements.txt").exists():
        return
    local = {p.stem for p in d.rglob("*.py")} | {p.name for p in d.iterdir() if p.is_dir()}
    found = set()
    for f in d.rglob("*.py"):
        try:
            tree = ast.parse(f.read_text(errors="ignore"))
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                found |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                found.add(n.module.split(".")[0])
    pkgs = []
    for m in sorted(found):
        if m in sys.stdlib_module_names or m in local:
            continue
        pkgs.append(ALIASES.get(m, m))
    if pkgs:
        (d / "requirements.txt").write_text("\n".join(pkgs) + "\n")


def start_container(uid):
    d = DATA / str(uid)
    if not (d / "main.py").exists():
        return False
    auto_requirements(d)
    sh("docker", "rm", "-f", cname(uid))
    cmd = (
        "if [ -f requirements.txt ]; then "
        "pip install -q --no-cache-dir --target /tmp/libs -r requirements.txt; fi; "
        "PYTHONPATH=/tmp/libs exec python -u main.py"
    )
    r = sh(
        "docker", "run", "-d", "--name", cname(uid),
        "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
        "--memory", "256m", "--cpus", "0.25", "--pids-limit", "128",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--tmpfs", "/tmp:size=300m,exec",
        "-v", f"{d}:/app", "-w", "/app",
        "python:3.12-slim", "sh", "-c", cmd,
    )
    return r.returncode == 0


def stop_container(uid):
    sh("docker", "stop", "-t", "3", cname(uid))


def user_ids():
    return sorted(p.name for p in DATA.iterdir() if p.is_dir()) if DATA.exists() else []


def safe_extract(zpath, dest):
    with zipfile.ZipFile(zpath) as z:
        if sum(i.file_size for i in z.infolist()) > MAX_UNZIPPED:
            raise ValueError("حجم الملفات بعد فك الضغط كبير")
        for i in z.infolist():
            target = (dest / i.filename).resolve()
            if not str(target).startswith(str(dest.resolve()) + os.sep) and target != dest.resolve():
                raise ValueError("مسار غير آمن داخل الملف المضغوط")
        z.extractall(dest)


# ---------- أوامر المستخدمين ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلاً 👋 أرسل لي ملف بوتك:\n"
        "• ملف `.py` واحد (يصبح main.py)\n"
        "• أو `.zip` فيه main.py\n"
        "• و`requirements.txt` اختياري لتثبيت المكتبات\n\n"
        "الأوامر: /mybots /logs /restart /stop /delete"
    )


async def mybots(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not (DATA / str(uid) / "main.py").exists():
        return await update.message.reply_text("ما عندك بوت مرفوع.")
    st = "🟢 يعمل" if is_running(uid) else "🔴 متوقف"
    await update.message.reply_text(f"بوتك: {st}")


async def logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    r = sh("docker", "logs", "--tail", "30", cname(update.effective_user.id))
    out = (r.stdout + r.stderr).strip() or "لا توجد سجلات."
    await update.message.reply_text(out[-3500:])


async def restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ok = start_container(update.effective_user.id)
    await update.message.reply_text("تمت إعادة التشغيل ✅" if ok else "تعذر التشغيل ❌")


async def stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    stop_container(update.effective_user.id)
    await update.message.reply_text("تم الإيقاف ⏹")


async def delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sh("docker", "rm", "-f", cname(uid))
    shutil.rmtree(DATA / str(uid), ignore_errors=True)
    await update.message.reply_text("تم حذف بوتك وملفاتك 🗑")


async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN:
        return
    ids = user_ids()
    running = sum(is_running(i) for i in ids)
    await update.message.reply_text(f"المستخدمون: {len(ids)}\nيعملون: {running}")


async def on_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    doc = msg.document
    uid = update.effective_user.id
    name = (doc.file_name or "").lower()
    d = DATA / str(uid)

    if doc.file_size > MAX_FILE:
        return await msg.reply_text("الملف أكبر من 5MB.")
    if name not in ("requirements.txt",) and not name.endswith((".py", ".zip")):
        return await msg.reply_text("المسموح: .py أو .zip أو requirements.txt")
    if not d.exists() and len(user_ids()) >= MAX_USERS:
        return await msg.reply_text("الاستضافة ممتلئة حالياً.")

    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "_upload.tmp"
    f = await doc.get_file()
    await f.download_to_drive(tmp)
    try:
        if name.endswith(".zip"):
            for item in d.iterdir():
                if item != tmp:
                    shutil.rmtree(item) if item.is_dir() else item.unlink()
            safe_extract(tmp, d)
        elif name == "requirements.txt":
            shutil.copy(tmp, d / "requirements.txt")
        else:
            shutil.copy(tmp, d / "main.py")
    except Exception as e:
        return await msg.reply_text(f"فشل: {e}")
    finally:
        tmp.unlink(missing_ok=True)

    if (d / "main.py").exists():
        ok = await asyncio.to_thread(start_container, uid)
        await msg.reply_text("تم الرفع والتشغيل ✅" if ok else "تم الرفع لكن فشل التشغيل ❌")
    else:
        await msg.reply_text("تم الحفظ. أرسل main.py ليبدأ التشغيل.")


# ---------- الاستعادة والنسخ الاحتياطي ----------
async def restore(app):
    try:
        chat = await app.bot.get_chat(CHANNEL)
        pinned = chat.pinned_message
        if pinned and pinned.document:
            f = await pinned.document.get_file()
            await f.download_to_drive("backup.zip")
            with zipfile.ZipFile("backup.zip") as z:
                z.extractall(".")
            print("restored from backup")
    except Exception as e:
        print("restore failed:", e)
    DATA.mkdir(exist_ok=True)
    for uid in user_ids():
        await asyncio.to_thread(start_container, uid)


async def backup_task(app):
    await asyncio.sleep(max(0, RUN_MINUTES * 60 - (time.time() - START)))
    ids = user_ids()
    for uid in ids:
        stop_container(uid)
    for p in DATA.rglob("__pycache__"):
        shutil.rmtree(p, ignore_errors=True)
    shutil.make_archive("backup", "zip", root_dir=".", base_dir="users")
    size = os.path.getsize("backup.zip")
    try:
        if size > MAX_BACKUP:
            raise RuntimeError(f"النسخة {size // 1024 // 1024}MB وتجاوزت الحد")
        info = json.dumps({"users": len(ids), "time": time.ctime()}, ensure_ascii=False)
        with open("backup.zip", "rb") as fh:
            m = await app.bot.send_document(CHANNEL, fh, caption=info)
        await app.bot.pin_chat_message(CHANNEL, m.message_id, disable_notification=True)
        report = "📦 نسخة احتياطية\nالمستخدمون:\n" + ("\n".join(ids) or "لا أحد")
        await app.bot.send_message(CHANNEL, report[:4000])
    except Exception as e:
        print("backup failed:", e)
        if ADMIN:
            await app.bot.send_message(ADMIN, f"⚠️ فشل النسخ الاحتياطي: {e}")
    app.stop_running()


async def post_init(app):
    await restore(app)
    t = asyncio.create_task(backup_task(app))
    background.add(t)


def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mybots", mybots))
    app.add_handler(CommandHandler("logs", logs))
    app.add_handler(CommandHandler("restart", restart))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("delete", delete))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.Document.ALL, on_file))
    app.run_polling()


if __name__ == "__main__":
    main()
