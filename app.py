import os
import sys
import subprocess
import threading
import time
import psutil
import shutil
import re
from functools import wraps
from flask import Flask, render_template, request, jsonify, Response
from werkzeug.utils import secure_filename
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB
IS_VERCEL = bool(os.environ.get("VERCEL"))

# يفضّل ضبط التوكن والمالك من متغيرات البيئة على Railway
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID_RAW = os.environ.get("OWNER_ID", "0")
WEB_USERNAME = os.environ.get("WEB_USERNAME", "admin")
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")
try:
    OWNER_ID = int(OWNER_ID_RAW)
except ValueError:
    OWNER_ID = 0

# تخزين العمليات
running_processes = {}
process_outputs = {}
bot_started = False
bot_lock = threading.Lock()


# ==================== التحقق من المالك ====================

def is_owner(update: Update) -> bool:
    """التحقق إذا كان المستخدم هو المالك"""
    user = update.effective_user
    if not user:
        return False
    return user.id == OWNER_ID


def owner_only(func):
    """ديكوراتور لمنع غير المالك من استخدام الأوامر"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update):
            if update.message:
                await update.message.reply_text("❌ غير مصرح لك! هذا البوت للمالك فقط.")
            return
        return await func(update, context)
    return wrapper


# ==================== تثبيت المكتبات تلقائيًا ====================

def auto_install_libraries(file_path):
    """فحص الملف وتثبيت المكتبات الناقصة تلقائيًا"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        imports = re.findall(r'^(?:from|import)\s+([a-zA-Z0-9_]+)', content, re.MULTILINE)
        imports = list(set(imports))

        installed = []
        failed = []

        stdlib = {
            'os', 'sys', 'time', 'datetime', 'json', 're', 'threading', 'subprocess',
            'collections', 'math', 'random', 'string', 'io', 'glob', 'shutil',
            'socket', 'hashlib', 'base64', 'urllib', 'http', 'ssl', 'tempfile',
            'pathlib', 'asyncio', 'typing', 'logging'
        }

        for lib in imports:
            if lib in stdlib:
                continue

            try:
                __import__(lib)
            except ImportError:
                try:
                    result = subprocess.run(
                        [sys.executable, '-m', 'pip', 'install', lib],
                        capture_output=True,
                        text=True,
                        timeout=120
                    )
                    if result.returncode == 0:
                        installed.append(lib)
                    else:
                        failed.append(lib)
                except Exception:
                    failed.append(lib)

        return installed, failed
    except Exception as e:
        return [], [str(e)]


def install_requirements_txt(path='requirements.txt'):
    """تثبيت المكتبات من requirements.txt إذا وجد"""
    if os.path.exists(path):
        try:
            result = subprocess.run(
                [sys.executable, '-m', 'pip', 'install', '-r', path],
                capture_output=True,
                text=True,
                timeout=300
            )
            return result.returncode == 0, result.stdout, result.stderr
        except Exception as e:
            return False, "", str(e)
    return True, "", "No requirements.txt"


# ==================== حماية الويب ====================

def check_web_auth(auth):
    if not WEB_PASSWORD:
        return True
    return bool(auth and auth.username == WEB_USERNAME and auth.password == WEB_PASSWORD)


def require_web_auth():
    return Response(
        'Authentication required',
        401,
        {'WWW-Authenticate': 'Basic realm="VPS Manager"'}
    )


def web_auth_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not check_web_auth(request.authorization):
            return require_web_auth()
        return func(*args, **kwargs)
    return wrapper


# ==================== صفحات الويب ====================

@app.route('/')
@web_auth_required
def index():
    return render_template('index.html')


@app.route('/terminal')
@web_auth_required
def terminal():
    return render_template('terminal.html')


@app.route('/files')
@web_auth_required
def files():
    return render_template('files.html')


@app.route('/processes')
@web_auth_required
def processes():
    return render_template('processes.html')


# ==================== API: حالة النظام ====================

@app.route('/api/system_stats')
@web_auth_required
def system_stats():
    vm = psutil.virtual_memory()
    du = psutil.disk_usage('/')
    return jsonify({
        'cpu': psutil.cpu_percent(),
        'ram': vm.percent,
        'ram_used': vm.used // (1024 * 1024),
        'ram_total': vm.total // (1024 * 1024),
        'disk': du.percent,
        'disk_free': du.free // (1024 * 1024 * 1024)
    })


# ==================== API: إدارة الملفات ====================

@app.route('/api/list_files')
@web_auth_required
def list_files():
    path = request.args.get('path', '.')
    try:
        files = []
        for item in os.listdir(path):
            full = os.path.join(path, item)
            files.append({
                'name': item,
                'path': full,
                'is_dir': os.path.isdir(full),
                'size': os.path.getsize(full) if os.path.isfile(full) else 0,
                'modified': os.path.getmtime(full)
            })
        files.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
        return jsonify({'success': True, 'files': files, 'path': path})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/upload', methods=['POST'])
@web_auth_required
def upload_file():
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file'})

    file = request.files['file']
    path = request.form.get('path', '.')

    if file.filename == '':
        return jsonify({'success': False, 'error': 'No file selected'})

    os.makedirs(path, exist_ok=True)
    filename = secure_filename(file.filename)
    save_path = os.path.join(path, filename)
    file.save(save_path)

    result = {
        'success': True,
        'message': f'✅ تم رفع {filename}',
        'auto_installed': [],
        'auto_failed': []
    }

    if filename.endswith('.py'):
        installed, failed = auto_install_libraries(save_path)
        if installed:
            result['auto_installed'] = installed
            result['message'] += f'\n📦 تم تثبيت: {", ".join(installed)}'
        if failed:
            result['auto_failed'] = failed
            result['message'] += f'\n⚠️ فشل تثبيت: {", ".join(failed)}'

    if filename == 'requirements.txt' or (filename.endswith('.txt') and 'requirements' in filename.lower()):
        success, stdout, stderr = install_requirements_txt(save_path)
        if success:
            result['message'] += '\n📦 تم تثبيت جميع المكتبات من requirements.txt'
        else:
            result['message'] += f'\n⚠️ فشل تثبيت المكتبات: {stderr[:200]}'

    return jsonify(result)


@app.route('/api/delete', methods=['POST'])
@web_auth_required
def delete_file():
    data = request.json or {}
    path = data.get('path')
    try:
        if not path:
            return jsonify({'success': False, 'error': 'Missing path'})
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        return jsonify({'success': True, 'message': f'✅ تم حذف {os.path.basename(path)}'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/mkdir', methods=['POST'])
@web_auth_required
def make_dir():
    data = request.json or {}
    path = data.get('path', '.')
    name = data.get('name')
    try:
        if not name:
            return jsonify({'success': False, 'error': 'Missing directory name'})
        os.makedirs(os.path.join(path, name), exist_ok=True)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/read_file', methods=['POST'])
@web_auth_required
def read_file_content():
    data = request.json or {}
    path = data.get('path')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        return jsonify({'success': True, 'content': content})
    except Exception:
        return jsonify({'success': False, 'error': 'Cannot read file'})


@app.route('/api/save_file', methods=['POST'])
@web_auth_required
def save_file_content():
    data = request.json or {}
    path = data.get('path')
    content = data.get('content', '')
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/run', methods=['POST'])
@web_auth_required
def run_command():
    data = request.json or {}
    command = data.get('command')
    pid = str(int(time.time() * 1000))

    if not command:
        return jsonify({'success': False, 'error': 'Missing command'})

    try:
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd='.'
        )
        running_processes[pid] = process

        def read_output():
            stdout, stderr = process.communicate()
            process_outputs[pid] = {
                'out': stdout,
                'err': stderr,
                'code': process.returncode
            }

        threading.Thread(target=read_output, daemon=True).start()
        return jsonify({'success': True, 'pid': pid})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/kill', methods=['POST'])
@web_auth_required
def kill_process():
    data = request.json or {}
    pid = data.get('pid')
    if pid in running_processes:
        try:
            running_processes[pid].terminate()
            return jsonify({'success': True})
        except Exception:
            pass
    return jsonify({'success': False})


@app.route('/api/output', methods=['POST'])
@web_auth_required
def get_output():
    data = request.json or {}
    pid = data.get('pid')
    if pid in process_outputs:
        return jsonify({'success': True, 'output': process_outputs[pid]})
    return jsonify({'success': False})


# ==================== بوت التليجرام ====================

async def bot_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("❌ غير مصرح لك!")
        return

    await update.message.reply_text(
        '👑 VPS Manager Bot - المالك فقط\n\n'
        '📋 الأوامر:\n'
        '/stats - حالة النظام\n'
        '/run <أمر> - تشغيل أمر\n'
        '/list - العمليات المشغلة\n'
        '/kill <pid> - إيقاف عملية\n'
        '/files - عرض الملفات\n'
        '/delete <مسار> - حذف ملف أو مجلد\n'
        '/install <مكتبة> - تثبيت مكتبة\n'
        '/python <ملف> - تشغيل ملف Python مع تثبيت تلقائي\n'
        '/help - المساعدة'
    )


@owner_only
async def bot_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cpu = psutil.cpu_percent()
    ram = psutil.virtual_memory().percent
    disk = psutil.disk_usage('/').percent
    await update.message.reply_text(
        f'📊 حالة النظام\n\n'
        f'💻 CPU: {cpu}%\n'
        f'🧠 RAM: {ram}%\n'
        f'💾 Disk: {disk}%'
    )


@owner_only
async def bot_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text('⚠️ اكتب: /run <الأمر>')
        return

    command = ' '.join(context.args)
    pid = str(int(time.time() * 1000))

    try:
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd='.'
        )
        running_processes[pid] = process

        def read_output():
            stdout, stderr = process.communicate()
            process_outputs[pid] = {'out': stdout, 'err': stderr, 'code': process.returncode}

        threading.Thread(target=read_output, daemon=True).start()

        await update.message.reply_text(f'✅ تم التشغيل\n🆔 PID: {pid}')

        await asyncio_sleep(3)
        if pid in process_outputs:
            out = process_outputs[pid]['out']
            err = process_outputs[pid]['err']
            if out:
                await update.message.reply_text(f'📤 الخرج:\n{out[:1000]}')
            if err:
                await update.message.reply_text(f'⚠️ خطأ:\n{err[:1000]}')
    except Exception as e:
        await update.message.reply_text(f'❌ خطأ: {str(e)}')


async def asyncio_sleep(seconds: float):
    import asyncio
    await asyncio.sleep(seconds)


@owner_only
async def bot_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alive = []
    for pid, proc in list(running_processes.items()):
        if proc.poll() is None:
            alive.append(pid)
        else:
            running_processes.pop(pid, None)

    if not alive:
        await update.message.reply_text('📭 لا توجد عمليات مشغلة')
        return

    msg = "العمليات المشغلة:\n\n"
    for pid in alive:
        msg += f"🆔 {pid}\n"
    await update.message.reply_text(msg)


@owner_only
async def bot_kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text('⚠️ اكتب: /kill <pid>')
        return

    pid = context.args[0]
    if pid in running_processes:
        try:
            running_processes[pid].terminate()
            await update.message.reply_text(f'✅ تم إيقاف العملية {pid}')
        except Exception:
            await update.message.reply_text('❌ فشل الإيقاف')
    else:
        await update.message.reply_text('❌ العملية غير موجودة')


@owner_only
async def bot_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    path = context.args[0] if context.args else '.'
    try:
        files = os.listdir(path)
        msg = f"📁 الملفات في {path}:\n\n"
        for f in files[:30]:
            full = os.path.join(path, f)
            if os.path.isdir(full):
                msg += f"📁 {f}/\n"
            else:
                size = os.path.getsize(full)
                msg += f"📄 {f} ({size} bytes)\n"
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f'❌ خطأ: {str(e)}')


@owner_only
async def bot_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text('⚠️ اكتب: /delete <المسار>')
        return

    path = ' '.join(context.args)
    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
            await update.message.reply_text(f'✅ تم حذف المجلد {path}')
        else:
            os.remove(path)
            await update.message.reply_text(f'✅ تم حذف الملف {path}')
    except Exception as e:
        await update.message.reply_text(f'❌ فشل الحذف: {str(e)}')


@owner_only
async def bot_install(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text('⚠️ اكتب: /install <اسم_المكتبة>')
        return

    package = context.args[0]
    await update.message.reply_text(f'📦 جاري تثبيت {package}...')

    try:
        result = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', package],
            capture_output=True,
            text=True,
            timeout=120
        )
        if result.returncode == 0:
            await update.message.reply_text(f'✅ تم تثبيت {package} بنجاح')
        else:
            await update.message.reply_text(f'❌ فشل التثبيت:\n{result.stderr[:500]}')
    except Exception as e:
        await update.message.reply_text(f'❌ خطأ: {str(e)}')


@owner_only
async def bot_python(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text('⚠️ اكتب: /python <اسم_الملف>')
        return

    filename = context.args[0]
    if not os.path.exists(filename):
        await update.message.reply_text(f'❌ الملف {filename} غير موجود')
        return

    await update.message.reply_text(f'🔍 جاري فحص المكتبات في {filename}...')

    installed, failed = auto_install_libraries(filename)

    msg = ""
    if installed:
        msg += f"✅ تم تثبيت: {', '.join(installed)}\n"
    if failed:
        msg += f"⚠️ فشل تثبيت: {', '.join(failed)}\n"

    await update.message.reply_text(f'🚀 جاري تشغيل {filename}...\n{msg}')

    try:
        result = subprocess.run(
            [sys.executable, filename],
            capture_output=True,
            text=True,
            timeout=30
        )

        output = ""
        if result.stdout:
            output += f"📤 الخرج:\n{result.stdout[:1500]}\n"
        if result.stderr:
            output += f"⚠️ الأخطاء:\n{result.stderr[:1500]}\n"

        if output:
            await update.message.reply_text(output)
        else:
            await update.message.reply_text('✅ تم التشغيل بنجاح (لا يوجد خرج)')
    except subprocess.TimeoutExpired:
        await update.message.reply_text('⏰ انتهى وقت التشغيل (30 ثانية)')
    except Exception as e:
        await update.message.reply_text(f'❌ خطأ: {str(e)}')


@owner_only
async def bot_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '👑 VPS Manager Bot - أوامر المالك\n\n'
        '🔹 /stats - عرض استهلاك CPU, RAM, Disk\n'
        '🔹 /run <command> - تشغيل أي أمر في النظام\n'
        '🔹 /list - عرض العمليات المشغلة\n'
        '🔹 /kill <pid> - إيقاف عملية\n'
        '🔹 /files [path] - عرض الملفات\n'
        '🔹 /delete <path> - حذف ملف أو مجلد\n'
        '🔹 /install <package> - تثبيت مكتبة Python\n'
        '🔹 /python <file> - تشغيل ملف Python مع تثبيت تلقائي\n'
        '🔹 /help - هذه المساعدة\n\n'
        '✨ الميزات الإضافية:\n'
        '• عند رفع ملف .py يتم تثبيت المكتبات المطلوبة تلقائيًا\n'
        '• عند رفع requirements.txt يتم تثبيت كل المكتبات\n'
        '• يمكن حذف أي ملف أو مجلد عبر الواجهة أو البوت'
    )


def start_bot():
    """تشغيل بوت التليجرام في الخلفية"""
    global bot_started

    if not BOT_TOKEN or not OWNER_ID:
        print("⚠️ BOT_TOKEN أو OWNER_ID غير مضبوطين. سيتم تشغيل الويب فقط.")
        return

    with bot_lock:
        if bot_started:
            return
        bot_started = True

    try:
        app_bot = Application.builder().token(BOT_TOKEN).build()
        app_bot.add_handler(CommandHandler("start", bot_start))
        app_bot.add_handler(CommandHandler("stats", bot_stats))
        app_bot.add_handler(CommandHandler("run", bot_run))
        app_bot.add_handler(CommandHandler("list", bot_list))
        app_bot.add_handler(CommandHandler("kill", bot_kill))
        app_bot.add_handler(CommandHandler("files", bot_files))
        app_bot.add_handler(CommandHandler("delete", bot_delete))
        app_bot.add_handler(CommandHandler("install", bot_install))
        app_bot.add_handler(CommandHandler("python", bot_python))
        app_bot.add_handler(CommandHandler("help", bot_help))

        print("🤖 Telegram bot is running for owner only...")
        app_bot.run_polling(close_loop=False, stop_signals=None)
    except Exception as e:
        print(f"❌ Failed to start Telegram bot: {e}")


def start_services():
    if BOT_TOKEN and OWNER_ID:
        thread = threading.Thread(target=start_bot, daemon=True)
        thread.start()
    else:
        print("ℹ️ Web app started without Telegram bot. Set BOT_TOKEN and OWNER_ID to enable it.")

    if not WEB_PASSWORD:
        print("⚠️ WEB_PASSWORD is not set. The web panel is currently open without password protection.")


# ابدأ الخدمات عند تشغيل الملف مباشرة
if __name__ == '__main__':
    start_services()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
elif not IS_VERCEL:
    # لتسهيل العمل على منصات مثل Railway إذا تم استدعاء الملف من مشغّل خارجي
    start_services()
