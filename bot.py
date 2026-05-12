import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from google import genai

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

COOLDOWN = 30
FW_COOLDOWN = 300
FW_SPAM_LIMIT = 5
BACKUP_DIR = 'backups'
MAX_BACKUPS = 4
BACKUP_INTERVAL = 1800
USERBASE_DIR = Path('userbase')
MESSAGES_FILE = 'messages.json'
NOTES_FILE = 'data/notes.json'
WARN_FILE = 'data/warns'
WARN_MUTE_DURATION = 604800
WARN_THRESHOLD = 3

with open('data/tg_token.txt') as f:
    TG_TOKEN = f.read().strip()

with open('data/gemini_token.txt') as f:
    API_KEYS = [line.strip() for line in f if line.strip()]

if os.path.exists('data/ai_token.txt'):
    with open('data/ai_token.txt') as f:
        AI_KEYS = [line.strip() for line in f if line.strip()]
else:
    AI_KEYS = []

with open('data/prompt.txt', encoding='utf-8') as f:
    FW_PROMPT = f.read().strip()

clients = [genai.Client(api_key=k) for k in API_KEYS]
ai_clients = [genai.Client(api_key=k) for k in AI_KEYS or API_KEYS]
client_idx = 0
ai_client_idx = 0

user_cooldowns: dict[str, float] = {}
fw_cooldowns: dict[str, float] = {}
fw_spam_count: dict[str, int] = {}
ai_cooldowns: dict[str, float] = {}
ai_spam_count: dict[str, int] = {}
AI_COOLDOWN = 30
AI_SPAM_LIMIT = 3

OP_FILES_DIR = Path('op_list')
OP_RULES: dict[int, list[str]] = {}


def user_path(uid: int) -> Path:
    return USERBASE_DIR / f'{uid}.json'


def load_user(uid: int) -> dict | None:
    p = user_path(uid)
    if p.exists():
        return json.loads(p.read_text(encoding='utf-8'))
    return None


def save_user(data: dict):
    USERBASE_DIR.mkdir(exist_ok=True)
    p = user_path(data['user_id'])
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def get_or_create_user(uid: int, username: str = '', first_name: str = ''):
    data = load_user(uid)
    if data:
        if username or first_name:
            data['username'] = username
            data['first_name'] = first_name
        data['last_seen'] = time.time()
        save_user(data)
        return data
    data = {
        'user_id': uid,
        'username': username,
        'first_name': first_name,
        'op_level': 0,
        'is_banned': 0,
        'ban_expiry': None,
        'ban_chat_id': None,
        'is_muted': 0,
        'mute_expiry': None,
        'mute_chat_id': None,
        'created_at': time.time(),
        'last_seen': time.time(),
        'warns': [],
    }
    save_user(data)
    return data


def update_user(uid: int, **kwargs):
    data = load_user(uid) or get_or_create_user(uid)
    data.update(kwargs)
    save_user(data)


def is_user_banned(uid: int) -> tuple[bool, str]:
    data = load_user(uid)
    if data and data.get('is_banned'):
        expiry = data.get('ban_expiry')
        if expiry is None:
            return True, 'permanent'
        if expiry > time.time():
            return True, 'temporary'
    return False, ''


def is_user_muted(uid: int) -> tuple[bool, str]:
    data = load_user(uid)
    if data and data.get('is_muted'):
        expiry = data.get('mute_expiry')
        if expiry is None:
            return True, 'permanent'
        if expiry > time.time():
            return True, 'temporary'
    return False, ''


def user_has_op(uid: int, command: str) -> bool:
    data = load_user(uid)
    if not data or not data.get('op_level'):
        return False
    lvl = data['op_level']
    for level in sorted(OP_RULES.keys(), reverse=True):
        if lvl >= level:
            if command.lower() in OP_RULES.get(level, []):
                return True
    return False


def find_user_by_username(username: str) -> SimpleNamespace | None:
    if not USERBASE_DIR.exists():
        return None
    for f in USERBASE_DIR.iterdir():
        if f.suffix != '.json':
            continue
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
            if data.get('username', '').lower() == username.lower():
                return SimpleNamespace(
                    id=data['user_id'],
                    username=data.get('username', '') or str(data['user_id']),
                    first_name=data.get('first_name', '') or str(data['user_id']),
                )
        except Exception:
            continue
    return None


def load_message_id_counter() -> int:
    if os.path.exists(MESSAGES_FILE):
        msgs = json.loads(open(MESSAGES_FILE, encoding='utf-8').read())
        return max((m.get('id', 0) for m in msgs), default=0) + 1
    return 1


def save_message(msg_data: dict):
    msgs = []
    if os.path.exists(MESSAGES_FILE):
        msgs = json.loads(open(MESSAGES_FILE, encoding='utf-8').read())
    msgs.append(msg_data)
    with open(MESSAGES_FILE, 'w', encoding='utf-8') as f:
        json.dump(msgs, f, ensure_ascii=False, indent=2)


def load_op_rules():
    OP_RULES.clear()
    if not OP_FILES_DIR.exists():
        OP_FILES_DIR.mkdir()
        (OP_FILES_DIR / 'op_lv1.txt').write_text('ban\nmute\nunban\nunmute', encoding='utf-8')
        (OP_FILES_DIR / 'op_lv2.txt').write_text('ban\ndban\nmute\ndmute\nunban\nunmute\naddnote\ndelnote', encoding='utf-8')
        (OP_FILES_DIR / 'op_lv3.txt').write_text('ban\ndban\nmute\ndmute\nunban\nunmute\nclear\naddnote\ndelnote', encoding='utf-8')
    for f in sorted(OP_FILES_DIR.iterdir()):
        m = re.match(r'op_lv(\d+)\.txt', f.name)
        if m:
            lvl = int(m.group(1))
            cmds = [l.strip().lower() for l in f.read_text(encoding='utf-8').splitlines() if l.strip()]
            OP_RULES[lvl] = cmds


def load_warn_threshold():
    global WARN_THRESHOLD
    p = Path(WARN_FILE)
    if p.exists():
        WARN_THRESHOLD = int(p.read_text().strip())
    else:
        p.parent.mkdir(exist_ok=True)
        p.write_text('3')


def add_warn(uid: int, reason: str) -> int:
    data = load_user(uid) or get_or_create_user(uid)
    warns = data.get('warns', [])
    warns.append({
        'id': len(warns) + 1,
        'reason': reason,
        'date': datetime.now().isoformat(),
        'active': True,
    })
    data['warns'] = warns
    save_user(data)
    return len([w for w in warns if w.get('active')])


def get_warns(uid: int) -> list:
    data = load_user(uid)
    if not data:
        return []
    return [w for w in data.get('warns', []) if w.get('active')]


def remove_warn(uid: int, index: int | None = None) -> dict | None:
    data = load_user(uid)
    if not data:
        return None
    warns = data.get('warns', [])
    active = [w for w in warns if w.get('active')]
    if not active:
        return None
    if index is None:
        target = active[-1]
    else:
        if 1 <= index <= len(active):
            target = active[index - 1]
        else:
            return None
    for w in warns:
        if w.get('active') and w['id'] == target['id']:
            w['active'] = False
            break
    data['warns'] = warns
    save_user(data)
    return target


def load_notes() -> dict:
    if os.path.exists(NOTES_FILE):
        return json.loads(Path(NOTES_FILE).read_text(encoding='utf-8'))
    return {}


def save_notes(notes: dict):
    Path(NOTES_FILE).parent.mkdir(exist_ok=True)
    Path(NOTES_FILE).write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding='utf-8')


def is_admin(update: Update) -> bool:
    return update.effective_user.id in load_admin_ids()


def load_admin_ids() -> list[int]:
    file = 'admin_id.txt'
    if os.path.exists(file):
        with open(file) as f:
            ids = [int(line.strip()) for line in f if line.strip()]
            return ids
    return []


def add_admin_id(uid: int):
    ids = load_admin_ids()
    if uid not in ids:
        with open('admin_id.txt', 'a') as f:
            f.write(f'{uid}\n')


def remove_admin_id(uid: int):
    ids = [i for i in load_admin_ids() if i != uid]
    with open('admin_id.txt', 'w') as f:
        for i in ids:
            f.write(f'{i}\n')


def user_key(user) -> str:
    return user.username or f'id:{user.id}'


def parse_duration(s: str) -> float | None:
    s = s.lower().strip()
    if s in ('perm', 'permanent', 'forever', '0', ''):
        return None
    m = re.match(r'^(\d+)\s*(м|m|мин|min|h|ч|час|d|д|дн|w|н|нед)?$', s)
    if not m:
        return None
    val = int(m.group(1))
    unit = m.group(2) or 'm'
    mult = {'m': 60, 'мин': 60, 'min': 60, 'h': 3600, 'ч': 3600, 'час': 3600,
            'd': 86400, 'д': 86400, 'дн': 86400, 'w': 604800, 'н': 604800, 'нед': 604800}
    return val * mult.get(unit, 60)


def get_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = update.message.reply_to_message
    if reply:
        return reply.from_user
    if context.args:
        first_arg = context.args[0].lstrip('@')
        if first_arg.isdigit():
            return SimpleNamespace(id=int(first_arg), username=first_arg, first_name=first_arg)
        found = find_user_by_username(first_arg)
        if found:
            return found
    return None


def make_backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_name = f'backup_{ts}'
    backup_path = os.path.join(BACKUP_DIR, backup_name)
    os.makedirs(backup_path, exist_ok=True)
    if USERBASE_DIR.exists():
        shutil.copytree(USERBASE_DIR, os.path.join(backup_path, 'userbase'), dirs_exist_ok=True)
    if os.path.exists(MESSAGES_FILE):
        shutil.copy2(MESSAGES_FILE, os.path.join(backup_path, MESSAGES_FILE))
    if os.path.exists('data'):
        shutil.copytree('data', os.path.join(backup_path, 'data'), dirs_exist_ok=True)
    shutil.make_archive(backup_path, 'zip', BACKUP_DIR, backup_name)
    shutil.rmtree(backup_path)
    zip_path = backup_path + '.zip'
    backups = sorted(Path(BACKUP_DIR).glob('backup_*.zip'), key=os.path.getmtime)
    while len(backups) > MAX_BACKUPS:
        backups[0].unlink()
        backups.pop(0)
    logger.info(f'Бекап: {zip_path}')


async def cleanup_expired(bot):
    now = time.time()
    if not USERBASE_DIR.exists():
        return
    for f in USERBASE_DIR.iterdir():
        if f.suffix != '.json':
            continue
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
            changed = False
            if data.get('is_banned') and data.get('ban_expiry') and data['ban_expiry'] <= now:
                chat_id = data.get('ban_chat_id')
                if chat_id:
                    try:
                        await bot.unban_chat_member(chat_id=chat_id, user_id=data['user_id'])
                    except Exception:
                        pass
                data['is_banned'] = 0
                data['ban_expiry'] = None
                data['ban_chat_id'] = None
                changed = True
            if data.get('is_muted') and data.get('mute_expiry') and data['mute_expiry'] <= now:
                chat_id = data.get('mute_chat_id')
                if chat_id:
                    try:
                        await bot.restrict_chat_member(
                            chat_id=chat_id,
                            user_id=data['user_id'],
                            permissions=ChatPermissions(
                                can_send_messages=True,
                                can_send_media_messages=True,
                                can_send_polls=True,
                                can_send_other_messages=True,
                                can_add_web_page_previews=True,
                            ),
                        )
                    except Exception:
                        pass
                data['is_muted'] = 0
                data['mute_expiry'] = None
                data['mute_chat_id'] = None
                changed = True
            if changed:
                save_user(data)
        except Exception:
            continue


async def backup_loop(app):
    while True:
        await asyncio.sleep(BACKUP_INTERVAL)
        make_backup()
        await cleanup_expired(app.bot)


async def process_with_gemini(text: str) -> tuple[str, str]:
    global client_idx
    prompt = FW_PROMPT.replace('{text}', text)
    for attempt in range(len(clients)):
        idx = (client_idx + attempt) % len(clients)
        try:
            resp = await clients[idx].aio.models.generate_content(
                model='gemma-4-31b-it', contents=prompt
            )
            client_idx = (idx + 1) % len(clients)
            lines = resp.text.strip().split('\n', 1)
            tag = lines[0].strip().upper()
            body = lines[1].strip() if len(lines) > 1 else ''
            return tag, body
        except Exception as e:
            logger.warning(f'API ключ {idx+1} ошибка: {e}')
    raise Exception('Все API ключи недоступны')


async def process_ai_direct(text: str) -> str:
    global ai_client_idx
    for attempt in range(len(ai_clients)):
        idx = (ai_client_idx + attempt) % len(ai_clients)
        try:
            resp = await ai_clients[idx].aio.models.generate_content(
                model='gemma-4-31b-it', contents=text
            )
            ai_client_idx = (idx + 1) % len(ai_clients)
            return resp.text
        except Exception as e:
            logger.warning(f'AI ключ {idx+1} ошибка: {e}')
    raise Exception('Все AI ключи недоступны')


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f'/start от @{user.username or user.id}')
    get_or_create_user(user.id, user.username or '', user.first_name or '')
    admins = load_admin_ids()
    if not admins or (user.username or '').lower() == 'voxelsy':
        add_admin_id(user.id)
        logger.info(f'Админ зарегистрирован: {user.id} (@{user.username})')
        me = await context.bot.get_me()
        url = f'https://t.me/{me.username}?startgroup=true'
        keyboard = [[InlineKeyboardButton('Добавить бота в чат', url=url)]]
        markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text('Ты зарегистрирован как администратор.', reply_markup=markup)
    else:
        await update.message.reply_text('Бот работает.')


async def handle_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    identifier = user_key(update.effective_user)
    now = time.time()
    int_uid = update.effective_user.id
    get_or_create_user(int_uid, update.effective_user.username or '', update.effective_user.first_name or '')

    privileged = is_admin(update) or user_has_op(int_uid, 'clear')

    if not privileged:
        banned, btype = is_user_banned(int_uid)
        if banned:
            txt = 'Ты забанен навсегда.' if btype == 'permanent' else 'Ты забанен.'
            await update.message.reply_text(txt)
            return

        muted, mtype = is_user_muted(int_uid)
        if muted:
            txt = 'Ты в муте навсегда.' if mtype == 'permanent' else 'Ты в муте.'
            await update.message.reply_text(txt)
            return

    if not context.args:
        await update.message.reply_text('Напиши текст после команды.')
        return

    if not privileged:
        fw_end = fw_cooldowns.get(uid)
        if fw_end and now < fw_end:
            fw_spam_count[uid] = fw_spam_count.get(uid, 0) + 1
            left = int(fw_end - now)
            if fw_spam_count[uid] > FW_SPAM_LIMIT:
                update_user(int_uid, is_banned=1, ban_expiry=None)
                try:
                    await context.bot.ban_chat_member(chat_id=update.effective_chat.id, user_id=int_uid)
                except Exception:
                    pass
                logger.warning(f'Спам-бан @{identifier}: {fw_spam_count[uid]} сообщений за кулдаун')
                await update.message.reply_text('Ты забанен за спам.')
                return
            await update.message.reply_text(f'Не по теме. Кулдаун {left // 60} мин.')
            return

        last = user_cooldowns.get(uid)
        if last and now - last < COOLDOWN:
            left = int(COOLDOWN - (now - last))
            await update.message.reply_text(f'Кулдаун {left} сек.')
            return

    content = ' '.join(context.args)
    logger.info(f'Запрос от @{identifier}: {content[:50]}...')

    save_message({
        'id': load_message_id_counter(),
        'command': f'/{context.command}',
        'text': content,
        'from': identifier,
        'date': datetime.now().isoformat(),
    })

    user_cooldowns[uid] = now
    msg = await update.message.reply_text('Проверяю запрос...')

    try:
        tag, body = await process_with_gemini(content)
        logger.info(f'Gemini: tag={tag}, body={len(body)} символов')
    except Exception as e:
        logger.error(f'Ошибка Gemini: {e}')
        await msg.edit_text('Ошибка обработки запроса. Попробуй позже.')
        return

    if tag == 'JAILBREAK':
        update_user(int_uid, is_banned=1, ban_expiry=None)
        try:
            await context.bot.ban_chat_member(chat_id=update.effective_chat.id, user_id=int_uid)
        except Exception:
            pass
        logger.warning(f'Jailbreak-бан @{identifier}: {content[:100]}')
        await msg.edit_text('Ты забанен за попытку взлома бота.')
        return

    if tag == 'INSULT':
        logger.info(f'Оскорбление от @{identifier}: {content[:100]}')
        await msg.edit_text('Не оскорбляй бота или админа.')
        return

    if tag == 'NOT_FIRMWARE':
        fw_cooldowns[uid] = now + FW_COOLDOWN
        fw_spam_count[uid] = 0
        await msg.edit_text(f'Не о прошивке. Кулдаун 5 мин.\n\n{body}')
        logger.info(f'Не прошивка от @{identifier}, кулдаун 5 мин')
        return

    admin_ids = load_admin_ids()
    text = f'Новый запрос\n\nКраткое:\n{body}\n\nОригинальное:\n{content}'

    if admin_ids:
        sent = False
        for aid in admin_ids:
            try:
                await context.bot.send_message(chat_id=aid, text=text)
                sent = True
            except Exception as e:
                logger.error(f'Ошибка отправки админу {aid}: {e}')
        if sent:
            await msg.edit_text('Запрос отправлен администраторам.')
        else:
            await msg.edit_text('Ошибка отправки администраторам.')
    else:
        await msg.edit_text('Админ не зарегистрирован. @voxelsy напиши /start.')


async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    int_uid = update.effective_user.id
    identifier = user_key(update.effective_user)
    get_or_create_user(int_uid, update.effective_user.username or '', update.effective_user.first_name or '')

    banned, btype = is_user_banned(int_uid)
    if banned:
        await update.message.reply_text('Ты забанен.')
        return

    now = time.time()
    cd_end = ai_cooldowns.get(uid)
    if cd_end and now < cd_end:
        ai_spam_count[uid] = ai_spam_count.get(uid, 0) + 1
        left = int(cd_end - now)
        if ai_spam_count[uid] > AI_SPAM_LIMIT:
            update_user(int_uid, is_banned=1, ban_expiry=None)
            try:
                await context.bot.ban_chat_member(chat_id=update.effective_chat.id, user_id=int_uid)
            except Exception:
                pass
            logger.warning(f'AI-спам бан @{identifier}: {ai_spam_count[uid]} сообщений за кулдаун')
            await update.message.reply_text('Ты забанен за спам.')
            return
        await update.message.reply_text(f'Подожди {left} сек.')
        return

    ai_spam_count[uid] = 0

    text = update.message.text
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text('Напиши текст после /ai')
        return
    content = parts[1]
    msg = await update.message.reply_text('Думаю...')
    try:
        resp = await process_ai_direct(content)
        ai_cooldowns[uid] = now + AI_COOLDOWN
        await msg.edit_text(resp[:4096])
    except Exception as e:
        logger.error(f'Ошибка AI: {e}')
        await msg.edit_text('Ошибка.')


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE, target_user=None):
    user = target_user or update.effective_user
    data = get_or_create_user(user.id, user.username or '', user.first_name or '')
    info = [
        f'ID: {data["user_id"]}',
        f'Username: @{data.get("username") or "нет"}',
        f'Имя: {data.get("first_name") or "нет"}',
        f'Op уровень: {data.get("op_level") or 0}',
    ]
    if data.get('is_banned'):
        expiry = data.get('ban_expiry')
        t = ' (навсегда)' if expiry is None else f' (до {datetime.fromtimestamp(expiry).strftime("%d.%m %H:%M")})'
        info.append(f'Статус: ЗАБАНЕН{t}')
    elif data.get('is_muted'):
        expiry = data.get('mute_expiry')
        t = ' (навсегда)' if expiry is None else f' (до {datetime.fromtimestamp(expiry).strftime("%d.%m %H:%M")})'
        info.append(f'Статус: В МУТЕ{t}')
    else:
        info.append('Статус: активен')
    warns = get_warns(user.id)
    if warns:
        info.append(f'Варнов: {len(warns)}')
    await update.message.reply_text('\n'.join(info))


async def who_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower().replace(' ', '')
    if 'ты' in text or 'tы' in text:
        reply = update.message.reply_to_message
        target = reply.from_user if reply else None
        await whoami(update, context, target)
    else:
        await whoami(update, context)


async def warn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) and not user_has_op(update.effective_user.id, 'warn'):
        return
    target = get_target(update, context)
    if not target:
        await update.message.reply_text('Ответь на сообщение или укажи username.')
        return
    reason = ' '.join(context.args) if context.args else 'не указана'
    get_or_create_user(target.id)
    cnt = add_warn(target.id, reason)
    await update.message.reply_text(f'@{target.username or target.id} получил варн ({cnt}/{WARN_THRESHOLD}). Причина: {reason}')
    try:
        await context.bot.send_message(chat_id=target.id, text=f'Ты получил варн ({cnt}/{WARN_THRESHOLD}). Причина: {reason}')
    except Exception:
        pass
    if cnt >= WARN_THRESHOLD:
        update_user(target.id, is_muted=1, mute_expiry=time.time() + WARN_MUTE_DURATION)
        await update.message.reply_text(f'@{target.username or target.id} автоматически замучен на неделю (достигнут лимит варнов).')
        try:
            await context.bot.send_message(chat_id=target.id, text='Ты автоматически замучен на неделю из-за превышения лимита варнов.')
        except Exception:
            pass


async def warns_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) and not user_has_op(update.effective_user.id, 'warns'):
        return
    reply = update.message.reply_to_message
    uid = reply.from_user.id if reply else update.effective_user.id
    target_name = reply.from_user.username or str(reply.from_user.id) if reply else 'твои'
    warns = get_warns(uid)
    if not warns:
        await update.message.reply_text(f'У {target_name} нет варнов.')
        return
    lines = [f'Варны @{target_name}:']
    for i, w in enumerate(warns, 1):
        lines.append(f'{i}. [{w["date"][:16]}] {w["reason"]}')
    await update.message.reply_text('\n'.join(lines))


async def unwarn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) and not user_has_op(update.effective_user.id, 'unwarn'):
        return
    target = get_target(update, context)
    if not target:
        await update.message.reply_text('Ответь на сообщение или укажи username.')
        return
    idx = None
    if context.args:
        try:
            idx = int(context.args[0])
        except ValueError:
            pass
    removed = remove_warn(target.id, idx)
    if removed:
        await update.message.reply_text(f'Варн {removed["id"]} снят с @{target.username or target.id}.')
        try:
            await context.bot.send_message(chat_id=target.id, text=f'Варн {removed["id"]} снят.')
        except Exception:
            pass
    else:
        await update.message.reply_text('Нет активных варнов или неверный номер.')


async def staff_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_ids = load_admin_ids()
    if not admin_ids:
        await update.message.reply_text('Админ не зарегистрирован.')
        return
    lines = ['Администраторы:']
    for admin_uid in admin_ids:
        data = load_user(admin_uid) or get_or_create_user(admin_uid)
        last_seen = data.get('last_seen', 0)
        online = '🟢 Online' if time.time() - last_seen < 300 else '🔴 Offline'
        name = data.get('username') or str(admin_uid)
        lines.append(f'ID: {admin_uid} | @{name} | {online}')
    await update.message.reply_text('\n'.join(lines))


async def notes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    notes = load_notes()
    if not notes:
        await update.message.reply_text('Нет заметок.')
        return
    lines = ['Список заметок:']
    for tag, note in notes.items():
        desc = note.get('desc', note.get('data', '?'))[:60]
        lines.append(f'{tag} — {desc}')
    await update.message.reply_text('\n'.join(lines))


async def addnote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) and not user_has_op(update.effective_user.id, 'addnote'):
        return
    reply = update.message.reply_to_message
    if not reply:
        await update.message.reply_text('Ответь на сообщение, чтобы сохранить его как заметку.')
        return
    if not context.args:
        await update.message.reply_text('Укажи хештег. Пример: /addnote #rom')
        return
    tag = context.args[0]
    if not tag.startswith('#'):
        tag = '#' + tag
    desc = ' '.join(context.args[1:]) if len(context.args) > 1 else ''
    notes = load_notes()
    msg = reply

    if msg.text:
        note_data = {'type': 'text', 'data': msg.text, 'desc': desc or msg.text[:60]}
    elif msg.photo:
        note_data = {'type': 'photo', 'file_id': msg.photo[-1].file_id, 'desc': desc or (msg.caption or 'фото')[:60]}
    elif msg.document:
        note_data = {'type': 'document', 'file_id': msg.document.file_id, 'desc': desc or msg.document.file_name or 'файл'}
    elif msg.audio:
        note_data = {'type': 'audio', 'file_id': msg.audio.file_id, 'desc': desc or (msg.audio.performer or 'аудио')}
    elif msg.voice:
        note_data = {'type': 'voice', 'file_id': msg.voice.file_id, 'desc': desc or 'голосовое'}
    elif msg.video:
        note_data = {'type': 'video', 'file_id': msg.video.file_id, 'desc': desc or (msg.caption or 'видео')[:60]}
    elif msg.sticker:
        note_data = {'type': 'sticker', 'file_id': msg.sticker.file_id, 'desc': desc or 'стикер'}
    else:
        await update.message.reply_text('Неподдерживаемый тип сообщения.')
        return

    notes[tag] = note_data
    save_notes(notes)
    await update.message.reply_text(f'Заметка {tag} сохранена.')


async def delnote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) and not user_has_op(update.effective_user.id, 'delnote'):
        return
    if not context.args:
        await update.message.reply_text('Укажи #хештег или номер.')
        return
    notes = load_notes()
    arg = context.args[0]
    if arg.startswith('#'):
        if arg in notes:
            del notes[arg]
            save_notes(notes)
            await update.message.reply_text(f'Заметка {arg} удалена.')
        else:
            await update.message.reply_text('Заметка не найдена.')
    else:
        try:
            idx = int(arg)
        except ValueError:
            await update.message.reply_text('Укажи #хештег или номер.')
            return
        tags = list(notes.keys())
        if 1 <= idx <= len(tags):
            tag = tags[idx - 1]
            del notes[tag]
            save_notes(notes)
            await update.message.reply_text(f'Заметка {tag} удалена.')
        else:
            await update.message.reply_text('Неверный номер.')


async def hashtag_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.startswith('#'):
        return
    first_word = text.split()[0].lower()
    notes = load_notes()
    if first_word in notes:
        note = notes[first_word]
        chat_id = update.effective_chat.id
        try:
            if note['type'] == 'text':
                await context.bot.send_message(chat_id=chat_id, text=note['data'])
            elif note['type'] == 'photo':
                await context.bot.send_photo(chat_id=chat_id, photo=note['file_id'])
            elif note['type'] == 'document':
                await context.bot.send_document(chat_id=chat_id, document=note['file_id'])
            elif note['type'] == 'audio':
                await context.bot.send_audio(chat_id=chat_id, audio=note['file_id'])
            elif note['type'] == 'voice':
                await context.bot.send_voice(chat_id=chat_id, voice=note['file_id'])
            elif note['type'] == 'video':
                await context.bot.send_video(chat_id=chat_id, video=note['file_id'])
            elif note['type'] == 'sticker':
                await context.bot.send_sticker(chat_id=chat_id, sticker=note['file_id'])
        except Exception as e:
            logger.error(f'Ошибка отправки заметки {first_word}: {e}')


async def plus_warn_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ' '.join(update.message.text.split())
    m = re.match(r'^\+[вb][аa][рp][нh]\s+(.+)$', text, re.IGNORECASE)
    if not m:
        return
    reason = m.group(1)
    context.args = [reason]
    await warn_cmd(update, context)


async def warns_list_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await warns_cmd(update, context)


async def minus_warn_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ' '.join(update.message.text.split())
    m = re.match(r'^\-[вb][аa][рp][нh]\s*(\d+)?$', text, re.IGNORECASE)
    if not m:
        return
    context.args = [m.group(1)] if m.group(1) else []
    await unwarn_cmd(update, context)


async def plus_mute_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ' '.join(update.message.text.split())
    rest = re.sub(r'^\+[мm][уy][тt]\s*', '', text, flags=re.IGNORECASE)
    context.args = rest.split() if rest else []
    await mute_cmd(update, context)


async def plus_dmute_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ' '.join(update.message.text.split())
    rest = re.sub(r'^\+[дd][мm][уy][тt]\s*', '', text, flags=re.IGNORECASE)
    context.args = rest.split() if rest else []
    await dmute_cmd(update, context)


async def minus_mute_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ' '.join(update.message.text.split())
    rest = re.sub(r'^\-[мm][уy][тt]\s*', '', text, flags=re.IGNORECASE)
    context.args = rest.split() if rest else []
    await unmute_cmd(update, context)


async def plus_ban_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ' '.join(update.message.text.split())
    rest = re.sub(r'^\+[бb][аa][нh]\s*', '', text, flags=re.IGNORECASE)
    context.args = rest.split() if rest else []
    await ban_cmd(update, context)


async def plus_dban_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ' '.join(update.message.text.split())
    rest = re.sub(r'^\+[дd][бb][аa][нh]\s*', '', text, flags=re.IGNORECASE)
    context.args = rest.split() if rest else []
    await dban_cmd(update, context)


async def minus_ban_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = ' '.join(update.message.text.split())
    rest = re.sub(r'^\-[бb][аa][нh]\s*', '', text, flags=re.IGNORECASE)
    context.args = rest.split() if rest else []
    await unban_cmd(update, context)


async def staff_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await staff_cmd(update, context)


async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) and not user_has_op(update.effective_user.id, 'ban'):
        return
    target = get_target(update, context)
    if not target:
        await update.message.reply_text('Ответь на сообщение или укажи username.')
        return
    chat_name = update.effective_chat.title or f'чат {update.effective_chat.id}'
    duration = parse_duration(context.args[0]) if context.args else None
    reason = ' '.join(context.args[1:]) if len(context.args) > 1 else 'не указана'
    if duration is not None and duration <= 0:
        duration = None
    get_or_create_user(target.id)
    update_user(target.id, is_banned=1, ban_expiry=(time.time() + duration) if duration else None, ban_chat_id=update.effective_chat.id)
    t = f' на {duration // 60} мин' if duration else ' навсегда'
    ban_ok = True
    try:
        await context.bot.ban_chat_member(chat_id=update.effective_chat.id, user_id=target.id)
    except Exception as e:
        ban_ok = False
        logger.error(f'Бан @{target.username or target.id} в чате не удался: {e}')
    if ban_ok:
        logger.info(f'Бан в чате {chat_name} @{target.username or target.id}{t}: {reason}')
        await update.message.reply_text(f'@{target.username or target.id} забанен в чате {chat_name}{t}. Причина: {reason}')
    else:
        await update.message.reply_text(f'@{target.username or target.id} забанен в боте, но нет прав на бан в чате.')
    try:
        await context.bot.send_message(chat_id=target.id, text=f'Ты забанен{t} в чате {chat_name}. Причина: {reason}')
    except Exception:
        pass


async def dban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) and not user_has_op(update.effective_user.id, 'dban'):
        return
    target = get_target(update, context)
    if not target:
        await update.message.reply_text('Ответь на сообщение или укажи username.')
        return
    chat_name = update.effective_chat.title or f'чат {update.effective_chat.id}'
    reason = ' '.join(context.args) if context.args else 'не указана'
    get_or_create_user(target.id)
    update_user(target.id, is_banned=1, ban_expiry=None, ban_chat_id=update.effective_chat.id)
    ban_ok = True
    try:
        await context.bot.ban_chat_member(chat_id=update.effective_chat.id, user_id=target.id)
    except Exception as e:
        ban_ok = False
        logger.error(f'Перманентный бан @{target.username or target.id} в чате не удался: {e}')
    if ban_ok:
        logger.info(f'Перманентный бан в чате {chat_name} @{target.username or target.id}: {reason}')
        await update.message.reply_text(f'@{target.username or target.id} забанен в чате {chat_name} навсегда. Причина: {reason}')
    else:
        await update.message.reply_text(f'@{target.username or target.id} забанен в боте, но нет прав на бан в чате.')
    try:
        await context.bot.send_message(chat_id=target.id, text=f'Ты забанен в чате {chat_name} навсегда. Причина: {reason}')
    except Exception:
        pass


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) and not user_has_op(update.effective_user.id, 'unban'):
        return
    target = get_target(update, context)
    if not target:
        await update.message.reply_text('Ответь на сообщение или укажи username.')
        return
    update_user(target.id, is_banned=0, ban_expiry=None, ban_chat_id=None)
    try:
        await context.bot.unban_chat_member(chat_id=update.effective_chat.id, user_id=target.id)
    except Exception as e:
        logger.error(f'Анбан @{target.username or target.id} в чате не удался: {e}')
    await update.message.reply_text(f'@{target.username or target.id} разбанен.')
    try:
        await context.bot.send_message(chat_id=target.id, text='Ты разбанен в боте.')
    except Exception:
        pass


async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) and not user_has_op(update.effective_user.id, 'mute'):
        return
    target = get_target(update, context)
    if not target:
        await update.message.reply_text('Ответь на сообщение или укажи username.')
        return
    chat_name = update.effective_chat.title or f'чат {update.effective_chat.id}'
    duration = parse_duration(context.args[0]) if context.args else None
    reason = ' '.join(context.args[1:]) if len(context.args) > 1 else 'не указана'
    if duration is not None and duration <= 0:
        duration = None
    get_or_create_user(target.id)
    update_user(target.id, is_muted=1, mute_expiry=(time.time() + duration) if duration else None, mute_chat_id=update.effective_chat.id)
    t = f' на {duration // 60} мин' if duration else ' навсегда'
    mute_ok = True
    try:
        until = None if not duration else int(time.time() + duration)
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id,
            user_id=target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
    except Exception as e:
        mute_ok = False
        logger.error(f'Мут @{target.username or target.id} в чате не удался: {e}')
    if mute_ok:
        logger.info(f'Мут в чате {chat_name} @{target.username or target.id}{t}: {reason}')
        await update.message.reply_text(f'@{target.username or target.id} в муте в чате {chat_name}{t}. Причина: {reason}')
    else:
        await update.message.reply_text(f'@{target.username or target.id} в муте в боте, но нет прав на мут в чате.')
    try:
        await context.bot.send_message(chat_id=target.id, text=f'Ты в муте в чате {chat_name}{t}. Причина: {reason}')
    except Exception:
        pass


async def dmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) and not user_has_op(update.effective_user.id, 'dmute'):
        return
    target = get_target(update, context)
    if not target:
        await update.message.reply_text('Ответь на сообщение или укажи username.')
        return
    chat_name = update.effective_chat.title or f'чат {update.effective_chat.id}'
    reason = ' '.join(context.args) if context.args else 'не указана'
    get_or_create_user(target.id)
    update_user(target.id, is_muted=1, mute_expiry=None, mute_chat_id=update.effective_chat.id)
    mute_ok = True
    try:
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id,
            user_id=target.id,
            permissions=ChatPermissions(can_send_messages=False),
        )
    except Exception as e:
        mute_ok = False
        logger.error(f'Перманентный мут @{target.username or target.id} в чате не удался: {e}')
    if mute_ok:
        logger.info(f'Перманентный мут в чате {chat_name} @{target.username or target.id}: {reason}')
        await update.message.reply_text(f'@{target.username or target.id} в муте в чате {chat_name} навсегда. Причина: {reason}')
    else:
        await update.message.reply_text(f'@{target.username or target.id} в муте в боте, но нет прав на мут в чате.')
    try:
        await context.bot.send_message(chat_id=target.id, text=f'Ты в муте в чате {chat_name} навсегда. Причина: {reason}')
    except Exception:
        pass


async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) and not user_has_op(update.effective_user.id, 'unmute'):
        return
    target = get_target(update, context)
    if not target:
        await update.message.reply_text('Ответь на сообщение или укажи username.')
        return
    update_user(target.id, is_muted=0, mute_expiry=None, mute_chat_id=None)
    await update.message.reply_text(f'@{target.username or target.id} размучен.')
    try:
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id,
            user_id=target.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
    except Exception as e:
        logger.error(f'Размут @{target.username or target.id} в чате не удался: {e}')
    try:
        await context.bot.send_message(chat_id=target.id, text='Ты размучен в боте.')
    except Exception:
        pass


async def op_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text('Укажи уровень. Пример: /op 2')
        return
    target = get_target(update, context)
    if not target:
        target = update.effective_user
    try:
        level = int(context.args[0])
    except ValueError:
        await update.message.reply_text('Уровень должен быть числом.')
        return
    get_or_create_user(target.id)
    update_user(target.id, op_level=level)
    logger.info(f'Op @{target.username or target.id} -> уровень {level}')
    await update.message.reply_text(f'@{target.username or target.id} теперь op уровня {level}.')


async def deop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    target = get_target(update, context)
    if not target:
        await update.message.reply_text('Ответь на сообщение или укажи username.')
        return
    update_user(target.id, op_level=0)
    await update.message.reply_text(f'@{target.username or target.id} снят с op.')


async def bban_old(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    chat = update.effective_chat
    chat_name = chat.title or f'чат {chat.id}'
    reply = update.message.reply_to_message
    if reply:
        target_user = reply.from_user
        get_or_create_user(target_user.id)
        update_user(target_user.id, is_banned=1, ban_expiry=None)
        name = target_user.username or str(target_user.id)
        logger.info(f'Бан в боте: @{name}')
        await update.message.reply_text(f'@{name} забанен в боте.')
        try:
            await context.bot.send_message(chat_id=target_user.id, text='Ты забанен в боте.')
        except Exception:
            pass
        return
    if context.args:
        arg = context.args[0].lstrip('@')
        if arg.isdigit():
            uid = int(arg)
            get_or_create_user(uid)
            update_user(uid, is_banned=1, ban_expiry=None)
            try:
                await context.bot.send_message(chat_id=uid, text='Ты забанен в боте.')
            except Exception:
                pass
        logger.info(f'Бан в боте: @{arg}')
        await update.message.reply_text(f'@{arg} забанен в боте.')
        return
    await update.message.reply_text('Ответь на сообщение или укажи username.')


async def unbban_old(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text('Укажи username. Пример: /unbban username')
        return
    arg = context.args[0].lstrip('@')
    if arg.isdigit():
        uid = int(arg)
        update_user(uid, is_banned=0, ban_expiry=None)
        try:
            await context.bot.send_message(chat_id=uid, text='Ты разбанен в боте.')
        except Exception:
            pass
    await update.message.reply_text(f'@{arg} разбанен в боте.')


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) and not user_has_op(update.effective_user.id, 'clear'):
        return
    if os.path.exists(MESSAGES_FILE):
        os.remove(MESSAGES_FILE)
    logger.info(f'Буфер очищен @{user_key(update.effective_user)}')
    await update.message.reply_text('Буфер сообщений очищен.')


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) and not user_has_op(update.effective_user.id, 'clear'):
        return
    reply = update.message.reply_to_message
    if not reply:
        return
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=reply.message_id)
    except Exception:
        pass
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)
    except Exception:
        pass


async def addadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    target = get_target(update, context)
    if not target:
        await update.message.reply_text('Ответь на сообщение или укажи username.')
        return
    add_admin_id(target.id)
    logger.info(f'Админ добавлен: {target.id} (@{target.username})')
    await update.message.reply_text(f'@{target.username or target.id} теперь администратор.')


async def rmadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    target = get_target(update, context)
    if not target:
        await update.message.reply_text('Ответь на сообщение или укажи username.')
        return
    if target.id == update.effective_user.id:
        await update.message.reply_text('Нельзя снять админа с себя.')
        return
    remove_admin_id(target.id)
    logger.info(f'Админ удалён: {target.id} (@{target.username})')
    await update.message.reply_text(f'@{target.username or target.id} больше не администратор.')


async def bans_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) and not user_has_op(update.effective_user.id, 'clear'):
        return
    lines = []
    if not USERBASE_DIR.exists():
        await update.message.reply_text('Нет банов.')
        return
    for f in USERBASE_DIR.iterdir():
        if f.suffix != '.json':
            continue
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
            if data.get('is_banned'):
                name = data.get('username') or str(data['user_id'])
                expiry = data.get('ban_expiry')
                if expiry:
                    left = int(expiry - time.time())
                    lines.append(f'@{name} — ещё {left // 60} мин')
                else:
                    lines.append(f'@{name} — навсегда')
        except Exception:
            continue
    if not lines:
        await update.message.reply_text('Нет банов.')
    else:
        await update.message.reply_text('Баны:\n' + '\n'.join(lines))


async def mutes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) and not user_has_op(update.effective_user.id, 'clear'):
        return
    lines = []
    if not USERBASE_DIR.exists():
        await update.message.reply_text('Нет мутов.')
        return
    for f in USERBASE_DIR.iterdir():
        if f.suffix != '.json':
            continue
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
            if data.get('is_muted'):
                name = data.get('username') or str(data['user_id'])
                expiry = data.get('mute_expiry')
                if expiry:
                    left = int(expiry - time.time())
                    lines.append(f'@{name} — ещё {left // 60} мин')
                else:
                    lines.append(f'@{name} — навсегда')
        except Exception:
            continue
    if not lines:
        await update.message.reply_text('Нет мутов.')
    else:
        await update.message.reply_text('Муты:\n' + '\n'.join(lines))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-backup', action='store_true', help='отключить авто-бекапы')
    args = parser.parse_args()

    USERBASE_DIR.mkdir(exist_ok=True)
    load_op_rules()
    load_warn_threshold()
    if not args.no_backup:
        make_backup()

    logger.info(f'Загружено API ключей: {len(clients)}, AI ключей: {len(ai_clients)}')
    logger.info(f'Op уровней: {len(OP_RULES)}')
    logger.info(f'Порог варнов: {WARN_THRESHOLD}')
    logger.info('Бот запускается...')

    async def post_init(app):
        if not args.no_backup:
            app.create_task(backup_loop(app))

    app = Application.builder().token(TG_TOKEN).concurrent_updates(True).post_init(post_init).build()

    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.message and update.message.text:
            parts = update.message.text.split(maxsplit=1)
            context.args = parts[1].split() if len(parts) > 1 else []
            context.command = parts[0].lstrip('/')
        await handle_request(update, context)

    cmd_pattern = r'^/(делайсука|request)(?:\s|$)'

    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('bban', bban_old))
    app.add_handler(CommandHandler('unbban', unbban_old))
    app.add_handler(CommandHandler('ban', ban_cmd))
    app.add_handler(CommandHandler('dban', dban_cmd))
    app.add_handler(CommandHandler('unban', unban_cmd))
    app.add_handler(CommandHandler('mute', mute_cmd))
    app.add_handler(CommandHandler('dmute', dmute_cmd))
    app.add_handler(CommandHandler('unmute', unmute_cmd))
    app.add_handler(CommandHandler('op', op_cmd))
    app.add_handler(CommandHandler('deop', deop_cmd))
    app.add_handler(CommandHandler('ai', ai_cmd))
    app.add_handler(CommandHandler('warn', warn_cmd))
    app.add_handler(CommandHandler('warns', warns_cmd))
    app.add_handler(CommandHandler('unwarn', unwarn_cmd))
    app.add_handler(CommandHandler('staff', staff_cmd))
    app.add_handler(CommandHandler('notes', notes_cmd))
    app.add_handler(CommandHandler('addnote', addnote_cmd))
    app.add_handler(CommandHandler('delnote', delnote_cmd))
    app.add_handler(CommandHandler('clear', clear_cmd))
    app.add_handler(CommandHandler('clean', clear_cmd))
    app.add_handler(CommandHandler('bans', bans_cmd))
    app.add_handler(CommandHandler('mutes', mutes_cmd))
    app.add_handler(CommandHandler('d', delete_cmd))
    app.add_handler(CommandHandler('addadmin', addadmin_cmd))
    app.add_handler(CommandHandler('rmadmin', rmadmin_cmd))

    app.add_handler(MessageHandler(filters.Regex(re.compile(r'^[+\-][вb][аa][рp][нh]', re.IGNORECASE)), plus_warn_handler))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r'^[+\-][вb][аa][рp][нh]', re.IGNORECASE)), minus_warn_handler))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r'^[вb][аa][рp][нh][ыy]', re.IGNORECASE)), warns_list_handler))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r'^\+[мm][уy][тt]', re.IGNORECASE)), plus_mute_handler))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r'^\+[дd][мm][уy][тt]', re.IGNORECASE)), plus_dmute_handler))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r'^\-[мm][уy][тt]', re.IGNORECASE)), minus_mute_handler))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r'^\+[бb][аa][нh]', re.IGNORECASE)), plus_ban_handler))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r'^\+[дd][бb][аa][нh]', re.IGNORECASE)), plus_dban_handler))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r'^\-[бb][аa][нh]', re.IGNORECASE)), minus_ban_handler))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r'^[бb][аa][нh][ыy]', re.IGNORECASE)), bans_cmd))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r'^[мm][уy][тt][ыy]', re.IGNORECASE)), mutes_cmd))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r'^[кk][тt][оo]\s+[аa][дd][мm][иi][нn]', re.IGNORECASE)), staff_handler))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r'^[кk][тt][оo]\s*[я]|^[кk][тt][оo]\s*[тt][ы]', re.IGNORECASE)), who_handler))
    app.add_handler(MessageHandler(filters.Regex(re.compile(r'^#\w+')), hashtag_handler))
    app.add_handler(MessageHandler(filters.Regex(cmd_pattern), wrapper))

    logger.info('Бот запущен')
    app.run_polling()


if __name__ == '__main__':
    main()
