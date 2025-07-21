import logging
import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler
from datetime import datetime
from telegram.ext import ConversationHandler, MessageHandler, filters
import html
import telegram.error
import math
import random
import redis
import json

# Токен бота, полученный от @BotFather
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# Проверяем, что токен был установлен
if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable not set.")

# --- НАСТРОЙКА REDIS КЛИЕНТА ---
REDIS_URL = os.environ.get("REDIS_URL")

if not REDIS_URL:
    raise ValueError("REDIS_URL environment variable not set. Please ensure Redis is configured on Render.")

try:
    # Инициализируем Redis-клиент
    # decode_responses=True позволяет получать строки Python вместо байтов
    r = redis.from_url(REDIS_URL, decode_responses=True)
    r.ping() # Проверяем соединение
    logging.info("Successfully connected to Redis.")
except redis.exceptions.ConnectionError as e:
    logging.error(f"Could not connect to Redis: {e}")
    raise SystemExit("Exiting: Redis connection failed.")


# Ключи для хранения данных в Redis
EVENT_DATA_KEY = "event_data" # Глобальные данные события
# Теперь chat-специфичные ключи будут использовать форматирование: f"{KEY}:{chat_id}"
MAIN_MESSAGE_ID_KEY = "main_message_id"
MAIN_CHAT_ID_KEY = "main_chat_id"
SHUFFLED_TEAMS_KEY = "shuffled_teams"
SHUFFLE_ERROR_KEY = "shuffle_error"

# --- Функции для работы с Redis ---
event_data = {} # Глобальная переменная для хранения event_data

def load_global_event_data_from_redis():
    """
    Загружает глобальное состояние события (event_data) из Redis.
    Эта функция вызывается только один раз при старте приложения.
    """
    global event_data
    event_data_json = r.get(EVENT_DATA_KEY)
    if event_data_json:
        event_data.update(json.loads(event_data_json))
        logger.info("Global event data loaded from Redis.")
    else:
        logger.info("No global event data found in Redis. Initializing default.")
        event_data = {
            'status': 'open',
            'title': None,
            'participants': {},
            'plus_ones': []
        }

def load_chat_specific_state_for_context(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """
    Загружает chat-специфичные данные (ID сообщения, команды, ошибки) из Redis
    и помещает их в context.chat_data для текущего чата.
    Вызывается в начале каждого обработчика, которому нужны эти данные.
    """
    # Если данные уже есть в context.chat_data, не загружаем их снова,
    # чтобы избежать перезаписи актуальных данных, измененных в рамках одного обновления.
    if 'main_message_id' in context.chat_data:
        return 

    logger.info(f"Loading chat-specific state for chat {chat_id} from Redis.")
    
    # Используем chat_id в ключах Redis
    main_message_id = r.get(f"{MAIN_MESSAGE_ID_KEY}:{chat_id}")
    main_chat_id = r.get(f"{MAIN_CHAT_ID_KEY}:{chat_id}")

    shuffled_teams_json = r.get(f"{SHUFFLED_TEAMS_KEY}:{chat_id}")
    # decode_responses=True уже конвертирует "None" в строку "None", а не None
    shuffle_error = r.get(f"{SHUFFLE_ERROR_KEY}:{chat_id}")

    if main_message_id:
        context.chat_data['main_message_id'] = int(main_message_id)
    if main_chat_id:
        context.chat_data['main_chat_id'] = int(main_chat_id)
    
    context.chat_data['shuffled_teams'] = json.loads(shuffled_teams_json) if shuffled_teams_json else []
    context.chat_data['shuffle_error'] = shuffle_error if shuffle_error != 'None' else None # Преобразуем "None" в None Python
    logger.info(f"Chat-specific state loaded for chat {chat_id}.")


def save_event_state(current_main_message_id=None, current_main_chat_id=None,
                     current_shuffled_teams=None, current_shuffle_error=None):
    """
    Сохраняет текущее глобальное состояние события и chat-специфичные данные в Redis.
    Global event_data сохраняется всегда.
    Chat-специфичные данные сохраняются, если передан current_main_chat_id.
    """
    # Сохраняем event_data (глобальное состояние)
    r.set(EVENT_DATA_KEY, json.dumps(event_data))

    # Сохраняем chat-специфичные данные, если есть chat_id для их привязки
    if current_main_chat_id is not None:
        chat_id = current_main_chat_id
        if current_main_message_id is not None:
            r.set(f"{MAIN_MESSAGE_ID_KEY}:{chat_id}", str(current_main_message_id))
        else:
            r.delete(f"{MAIN_MESSAGE_ID_KEY}:{chat_id}") # Удаляем, если ID сообщения нет

        r.set(f"{MAIN_CHAT_ID_KEY}:{chat_id}", str(chat_id))

        if current_shuffled_teams is not None:
            r.set(f"{SHUFFLED_TEAMS_KEY}:{chat_id}", json.dumps(current_shuffled_teams))
        else:
            r.delete(f"{SHUFFLED_TEAMS_KEY}:{chat_id}")
        
        if current_shuffle_error is not None:
            r.set(f"{SHUFFLE_ERROR_KEY}:{chat_id}", current_shuffle_error)
        else:
            r.delete(f"{SHUFFLE_ERROR_KEY}:{chat_id}") # Если ошибка была, но теперь ее нет, удаляем ключ
            
        logger.info(f"Event state saved to Redis for chat {chat_id}.")
    else:
        logger.info("Global event data saved to Redis (no chat-specific data provided for saving).")


# Enable logging to see what's happening
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# States for ConversationHandler
TITLE_STATE = range(1)

# Helper function to create a clickable name
def get_clickable_name(user_id: int, user_name: str, username: str = None) -> str:
    """Returns the user's name as an HTML link to their profile, if possible,
    with proper HTML escaping of the name."""
    escaped_user_name = html.escape(user_name)
    return f'<a href="tg://user?id={user_id}">{escaped_user_name}</a>'


async def get_event_message_and_keyboard(context: ContextTypes.DEFAULT_TYPE) -> tuple[str, InlineKeyboardMarkup]:
    """Generates the event message text and inline keyboard."""
    global event_data # Use global event data

    direct_going_participants = []
    plus_one_entries_formatted = []
    not_going_list = []
    maybe_list = []
    total_going_count = 0

    for user_id, user_info in event_data['participants'].items():
        name = user_info['name']
        status = user_info['status']
        username = user_info.get('username')

        display_name = get_clickable_name(user_id, name, username)

        if status == 'going':
            direct_going_participants.append(display_name)
            total_going_count += 1
        elif status == 'not_going':
            not_going_list.append(display_name)
        elif status == 'maybe':
            maybe_list.append(display_name)

    for plus_one_entry in event_data['plus_ones']:
        added_by_id = plus_one_entry['added_by_id']
        added_by_name = plus_one_entry['added_by_name']
        added_by_username = plus_one_entry.get('added_by_username')

        clickable_adder = get_clickable_name(added_by_id, added_by_name, added_by_username)
        plus_one_entries_formatted.append(f"➕ (+1 from {clickable_adder})")
        total_going_count += 1

    message_text = ""
    title_to_display = event_data['title'] if event_data['title'] else "Event Title (Not Set)"
    message_text += f"<b>{html.escape(title_to_display)}</b>\n\n"

    message_text += "🟢 Going:\n"
    if direct_going_participants or plus_one_entries_formatted:
        all_going_entries_formatted = []
        for name in direct_going_participants:
            all_going_entries_formatted.append(f"✅ {name}")
        for entry in plus_one_entries_formatted:
            all_going_entries_formatted.append(entry)
        message_text += "\n".join(all_going_entries_formatted) + "\n"
    else:
        message_text += "  (Nobody yet)\n"

    if maybe_list:
        message_text += "\n🟡 Thinking:\n"
        message_text += "\n".join([f"❓ {name}" for name in maybe_list]) + "\n"

    if not_going_list:
        message_text += "\n🔴 Not Going:\n"
        message_text += "\n".join([f"❌ {name}" for name in not_going_list]) + "\n"

    message_text += "\n" + "=" * 20 + "\n"
    message_text += f"👥 Total Going: {total_going_count}\n"
    message_text += f"📅 Created: {datetime.now().strftime('%d %B %Y')}\n\n"

    # --- Add team section if shuffled ---
    # Теперь берем shuffled_teams и shuffle_error из context.chat_data
    if 'shuffled_teams' in context.chat_data and context.chat_data['shuffled_teams']:
        message_text += "--- TEAM COMPOSITIONS ---\n"
        team_emojis = ["🔵", "🔴", "🟡", "🟢", "🟣", "⚪"]
        for i, team in enumerate(context.chat_data['shuffled_teams']):
            emoji = team_emojis[i % len(team_emojis)]
            message_text += f"{emoji} Team {i+1}:\n"
            if team:
                message_text += "\n".join([f"- {player}" for player in team]) + "\n"
            else:
                message_text += "  (Empty)\n"
        message_text += "------------------------\n\n"
    elif 'shuffle_error' in context.chat_data and context.chat_data['shuffle_error']:
        message_text += f"\n❗️ {context.chat_data['shuffle_error']}\n\n"

    keyboard = []

    if event_data['status'] == 'open':
        status_buttons = [
            InlineKeyboardButton("✅ Going", callback_data="set_status_going"),
            InlineKeyboardButton("❌ Not Going", callback_data="set_status_not_going"),
            InlineKeyboardButton("🤔 Thinking", callback_data="set_status_maybe"),
        ]

        plus_minus_buttons = [
            InlineKeyboardButton("➕ (+1)", callback_data="add_plus_one"),
            InlineKeyboardButton("➖ (-1)", callback_data="remove_plus_one"),
            InlineKeyboardButton("🔄 Reset", callback_data="reset_my_status"),
        ]
        keyboard.append(status_buttons)
        keyboard.append(plus_minus_buttons)

    toggle_status_button = InlineKeyboardButton(
        "⛔ Close Vote" if event_data['status'] == 'open' else "▶️ Open Vote",
        callback_data="admin_close_collection" if event_data['status'] == 'open' else "admin_open_collection"
    )

    current_admin_buttons_row = [toggle_status_button]

    if event_data['status'] == 'closed':
        shuffle_button = InlineKeyboardButton("🔀 Shuffle", callback_data="admin_shuffle_teams")
        current_admin_buttons_row.append(shuffle_button)

    if event_data['status'] == 'open':
        current_admin_buttons_row.extend([
            InlineKeyboardButton("✏️ Edit Title", callback_data="admin_set_title")
        ])

    keyboard.append(current_admin_buttons_row)
   
    # УДАЛЕНО: keyboard.append([InlineKeyboardButton("✨ New Event", callback_data="admin_new_event")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    return message_text, reply_markup


async def send_main_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends or edits the main bot message."""
    chat_id = update.effective_chat.id
    
    # *** ВАЖНОЕ ИЗМЕНЕНИЕ: Загрузка chat-специфичных данных в context.chat_data ***
    load_chat_specific_state_for_context(chat_id, context)

    message_text, reply_markup = await get_event_message_and_keyboard(context)

    main_message_id = context.chat_data.get('main_message_id')
    main_chat_id = context.chat_data.get('main_chat_id') # Должен быть равен chat_id текущего обновления

    # Если main_message_id не найден в context.chat_data (либо он None), или chat_id не совпадает,
    # отправляем новое сообщение.
    if not main_message_id or main_chat_id != chat_id:
        sent_message = await update.effective_message.reply_html(text=message_text, reply_markup=reply_markup)
        context.chat_data['main_message_id'] = sent_message.message_id
        context.chat_data['main_chat_id'] = sent_message.chat_id
        
        save_event_state(
            sent_message.message_id, 
            sent_message.chat_id,
            context.chat_data.get('shuffled_teams'),
            context.chat_data.get('shuffle_error')
        )
        logger.info(f"New main message sent. ID: {sent_message.message_id} for chat {chat_id}")
    else:
        try:
            await context.bot.edit_message_text(
                chat_id=main_chat_id, # Используем chat_id из context.chat_data
                message_id=main_message_id, # Используем message_id из context.chat_data
                text=message_text,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
            logger.info(f"Main message {main_message_id} updated for chat {chat_id}.")
            
            save_event_state(
                main_message_id,
                main_chat_id,
                context.chat_data.get('shuffled_teams'),
                context.chat_data.get('shuffle_error')
            )
        except telegram.error.BadRequest as e:
            if "Message is not modified" in str(e):
                logger.info(f"Main message {main_message_id} was not modified for chat {chat_id}. Ignoring.")
                save_event_state( # Все равно сохраняем, чтобы обновить TTL, если он есть
                    main_message_id, 
                    main_chat_id,
                    context.chat_data.get('shuffled_teams'),
                    context.chat_data.get('shuffle_error')
                )
            else:
                logger.warning(f"Failed to update main message (ID: {main_message_id}, Chat: {main_chat_id}) due to BadRequest: {e}. Sending new message.")
                sent_message = await update.effective_message.reply_html(text=message_text, reply_markup=reply_markup)
                context.chat_data['main_message_id'] = sent_message.message_id
                context.chat_data['main_chat_id'] = sent_message.chat_id
                
                save_event_state(
                    sent_message.message_id, 
                    context.chat_data.get('main_chat_id'),
                    context.chat_data.get('shuffled_teams'),
                    context.chat_data.get('shuffle_error')
                )
        except Exception as e:
            logger.warning(f"An unexpected error occurred while updating the main message (ID: {main_message_id}, Chat: {main_chat_id}): {e}. Sending new message.")
            sent_message = await update.effective_message.reply_html(text=message_text, reply_markup=reply_markup)
            context.chat_data['main_message_id'] = sent_message.message_id
            context.chat_data['main_chat_id'] = sent_message.chat_id
            
            save_event_state(
                sent_message.message_id, 
                sent_message.chat_id,
                context.chat_data.get('shuffled_teams'),
                context.chat_data.get('shuffle_error')
            )


async def start_command_title_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for /start to prompt for title and reset event data."""
    global event_data
    chat_id = update.effective_chat.id
    logger.info(f"'/start' command received from user {update.effective_user.id} in chat {chat_id}.")

    # Reset global event_data for a new event
    event_data = {
        'status': 'open',
        'title': None,
        'participants': {},
        'plus_ones': []
    }
    
    # Clear context.chat_data for the current chat
    context.chat_data.clear()

    # Also clear Redis entries specific to THIS CHAT and global event_data
    r.delete(EVENT_DATA_KEY) # Global event data
    r.delete(f"{MAIN_MESSAGE_ID_KEY}:{chat_id}") # Chat-specific
    r.delete(f"{MAIN_CHAT_ID_KEY}:{chat_id}")    # Chat-specific
    r.delete(f"{SHUFFLED_TEAMS_KEY}:{chat_id}")  # Chat-specific
    r.delete(f"{SHUFFLE_ERROR_KEY}:{chat_id}")   # Chat-specific
    logger.info(f"Event data and message IDs cleared from Redis for new event for chat {chat_id}.")

    await update.message.reply_text("Please enter the event title:")
    logger.info(f"Prompted user {update.effective_user.id} to enter new title for a new event.")
    return TITLE_STATE

async def set_title_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for 'Edit Title' button to prompt for title."""
    # Загружаем chat-специфичные данные
    load_chat_specific_state_for_context(update.effective_chat.id, context)

    await update.callback_query.answer("Enter new title.")
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Please enter the new event title in the chat."
    )
    logger.info(f"'Edit Title' button pressed by user {update.effective_user.id}. Prompting for title.")
    return TITLE_STATE

async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receives new title from user and updates it."""
    global event_data
    chat_id = update.effective_chat.id
    # Загружаем chat-специфичные данные
    load_chat_specific_state_for_context(chat_id, context)

    if update.message and update.message.text:
        event_data['title'] = update.message.text.strip()
        
        save_event_state(
            context.chat_data.get('main_message_id'),
            context.chat_data.get('main_chat_id'),
            context.chat_data.get('shuffled_teams'), 
            context.chat_data.get('shuffle_error')
        )
        await update.message.reply_text(f"Event title updated to: {event_data['title']}")
        logger.info(f"Event title updated to: '{event_data['title']}' by user {update.effective_user.id}")

        await send_main_message(update, context)

        return ConversationHandler.END
    else:
        logger.warning("receive_title called but no text message found or message is empty. Remaining in TITLE_STATE.")
        await update.message.reply_text("Please enter the new title as text.")
        return TITLE_STATE

async def start_num_teams_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the 'Shuffle' button press, performs checks, and prompts for number of teams with buttons."""
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    # Загружаем chat-специфичные данные
    load_chat_specific_state_for_context(chat_id, context)

    if event_data['status'] == 'open':
        await query.answer("Please close the vote before shuffling teams.")
        await send_main_message(update, context)
        return

    all_players_to_shuffle = []
    for user_id, user_info in event_data['participants'].items():
        if user_info['status'] == 'going':
            all_players_to_shuffle.append(get_clickable_name(user_id, user_info['name'], user_info.get('username')))
    for plus_one_entry in event_data['plus_ones']:
        added_by_id = plus_one_entry['added_by_id']
        added_by_name = plus_one_entry['added_by_name']
        added_by_username = plus_one_entry.get('added_by_username')
        all_players_to_shuffle.append(f"➕ (+1 from {get_clickable_name(added_by_id, added_by_name, added_by_username)})")

    total_players = len(all_players_to_shuffle)

    if total_players < 2:
        error_message = ""
        if total_players == 0:
            error_message = "No players marked as 'Going' to shuffle."
        elif total_players == 1:
            error_message = "Cannot form teams with only one player."
        
        context.chat_data['shuffle_error'] = error_message
        context.chat_data['shuffled_teams'] = []
        
        save_event_state(
            context.chat_data.get('main_message_id'), 
            context.chat_data.get('main_chat_id'),
            context.chat_data['shuffled_teams'], 
            context.chat_data['shuffle_error']
        )
        await query.answer(error_message)
        await send_main_message(update, context)
        return

    context.chat_data['players_for_shuffle'] = all_players_to_shuffle
    context.chat_data['total_players_for_shuffle'] = total_players

    team_buttons = []
    num_cols = 3
    current_row = []
    
    possible_num_teams_options = [2, 3, 4]
    
    for i in possible_num_teams_options:
        if i <= total_players:
            current_row.append(InlineKeyboardButton(str(i), callback_data=f"select_teams_{i}"))
            if len(current_row) == num_cols:
                team_buttons.append(current_row)
                current_row = []
    if current_row:
        team_buttons.append(current_row)

    reply_markup = InlineKeyboardMarkup(team_buttons)
    temp_message = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"There are {total_players} players available. Please select the number of teams:",
        reply_markup=reply_markup
    )
    context.chat_data['temp_shuffle_message_id'] = temp_message.message_id
    context.chat_data['temp_shuffle_message_chat_id'] = temp_message.chat_id
    logger.info(f"User {query.from_user.id} initiated shuffle. Prompting for num teams with buttons.")


async def handle_num_teams_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Receives the desired number of teams from button press and performs the shuffle."""
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    # Загружаем chat-специфичные данные
    load_chat_specific_state_for_context(chat_id, context)

    selected_teams_str = query.data.replace("select_teams_", "")
    num_teams = int(selected_teams_str)

    all_players_to_shuffle = context.chat_data.get('players_for_shuffle', [])
    total_players = context.chat_data.get('total_players_for_shuffle', 0)

    if not (2 <= num_teams <= total_players):
        context.chat_data['shuffle_error'] = "Invalid number of teams selected. Please try again."
        context.chat_data['shuffled_teams'] = []
        
        save_event_state(
            context.chat_data.get('main_message_id'), 
            context.chat_data.get('main_chat_id'),
            context.chat_data['shuffled_teams'], 
            context.chat_data['shuffle_error']
        )
        await query.answer("Invalid selection.")
        if 'temp_shuffle_message_id' in context.chat_data and 'temp_shuffle_message_chat_id' in context.chat_data:
            try:
                await context.bot.delete_message(
                    chat_id=context.chat_data['temp_shuffle_message_chat_id'],
                    message_id=context.chat_data['temp_shuffle_message_id']
                )
                del context.chat_data['temp_shuffle_message_id']
                del context.chat_data['temp_shuffle_message_chat_id']
            except Exception as e:
                logger.warning(f"Failed to delete temp shuffle message on invalid selection: {e}")
        await send_main_message(update, context)
        return

    random.shuffle(all_players_to_shuffle)

    teams = [[] for _ in range(num_teams)]
    for i, player in enumerate(all_players_to_shuffle):
        teams[i % num_teams].append(player)

    context.chat_data['shuffled_teams'] = teams
    context.chat_data['shuffle_error'] = None

    save_event_state(
        context.chat_data.get('main_message_id'), 
        context.chat_data.get('main_chat_id'),
        context.chat_data['shuffled_teams'], 
        context.chat_data['shuffle_error']
    )

    if 'temp_shuffle_message_id' in context.chat_data and 'temp_shuffle_message_chat_id' in context.chat_data:
        try:
            await context.bot.delete_message(
                chat_id=context.chat_data['temp_shuffle_message_chat_id'],
                message_id=context.chat_data['temp_shuffle_message_id']
            )
        except Exception as e:
            logger.warning(f"Failed to delete temp shuffle message: {e}")
        finally:
            del context.chat_data['temp_shuffle_message_id']
            del context.chat_data['temp_shuffle_message_chat_id']

    if 'players_for_shuffle' in context.chat_data:
        del context.chat_data['players_for_shuffle']
    if 'total_players_for_shuffle' in context.chat_data:
        del context.chat_data['total_players_for_shuffle']

    await send_main_message(update, context)
    await query.answer(f"Teams shuffled into {num_teams} teams!")
    logger.info(f"Teams shuffled into {num_teams} teams by user {query.from_user.id}.")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles inline button presses that are not part of ConversationHandlers."""
    query = update.callback_query
    data = query.data
    chat_id = update.effective_chat.id
    logger.info(f"button_callback called with data: {data} from user {query.from_user.id} in chat {chat_id}")

    await query.answer()

    user_id = query.from_user.id
    user_name = query.from_user.full_name
    username = query.from_user.username

    # *** ВАЖНОЕ ИЗМЕНЕНИЕ: Загрузка chat-специфичных данных в context.chat_data ***
    load_chat_specific_state_for_context(chat_id, context)

    # Initialize user if not in participants, including username
    if user_id not in event_data['participants']:
        event_data['participants'][user_id] = {'name': user_name, 'status': None, 'username': username}
    else: # Update name/username in case it changed
        event_data['participants'][user_id]['name'] = user_name
        event_data['participants'][user_id]['username'] = username


    # Clear shuffle data for any action except specific shuffle flows
    if not (data == "admin_shuffle_teams" or data.startswith("select_teams_")):
        context.chat_data['shuffled_teams'] = []
        context.chat_data['shuffle_error'] = None
        
        save_event_state(
            context.chat_data.get('main_message_id'),
            context.chat_data.get('main_chat_id'),
            context.chat_data['shuffled_teams'], 
            context.chat_data['shuffle_error']
        )
        if 'temp_shuffle_message_id' in context.chat_data:
            try:
                await context.bot.delete_message(
                    chat_id=context.chat_data['temp_shuffle_message_chat_id'],
                    message_id=context.chat_data['temp_shuffle_message_id']
                )
            except Exception as e:
                logger.warning(f"Failed to delete temp shuffle message on other button press: {e}")
            finally:
                del context.chat_data['temp_shuffle_message_id']
                del context.chat_data['temp_shuffle_message_chat_id']
                if 'players_for_shuffle' in context.chat_data: del context.chat_data['players_for_shuffle']
                if 'total_players_for_shuffle' in context.chat_data: del context.chat_data['total_players_for_shuffle']


    # Handle 'New Event' button (THIS BLOCK IS NOW OBSOLETE AS BUTTON IS REMOVED, BUT LOGIC REMAINS IF DATA IS SENT)
    if data == "admin_new_event":
        # Запускаем команду /start через ее обработчик, чтобы выполнить всю логику сброса
        await start_command_title_entry(update, context)
        return # Выходим, так как start_command_title_entry сам обновит/отправит сообщение

    # Check for vote status
    if event_data['status'] == 'closed' and not data.startswith("admin_"):
        await query.answer("Vote is closed, participation is unavailable.")
        await send_main_message(update, context)
        return

    # Handle status selection
    if data.startswith("set_status_"):
        new_status = data.replace("set_status_", "")
        event_data['participants'][user_id]['status'] = new_status
        event_data['participants'][user_id]['username'] = username
        
        save_event_state(
            context.chat_data.get('main_message_id'), 
            context.chat_data.get('main_chat_id'),
            context.chat_data.get('shuffled_teams'), 
            context.chat_data.get('shuffle_error')
        )

    elif data == "add_plus_one":
        event_data['plus_ones'].append({
            'added_by_id': user_id,
            'added_by_name': user_name,
            'added_by_username': username
        })
        
        save_event_state(
            context.chat_data.get('main_message_id'),
            context.chat_data.get('main_chat_id'),
            context.chat_data.get('shuffled_teams'), 
            context.chat_data.get('shuffle_error')
        )
    elif data == "remove_plus_one":
        found_and_removed = False
        for i in range(len(event_data['plus_ones']) - 1, -1, -1):
            if event_data['plus_ones'][i]['added_by_id'] == user_id:
                del event_data['plus_ones'][i]
                found_and_removed = True
                break
        if not found_and_removed:
            await query.answer("Cannot decrease, as you have no additional participants.")
        else:
            
            save_event_state(
                context.chat_data.get('main_message_id'),
                context.chat_data.get('main_chat_id'),
                context.chat_data.get('shuffled_teams'), 
                context.chat_data.get('shuffle_error')
            )

    elif data == "reset_my_status":
        if user_id in event_data['participants']:
            del event_data['participants'][user_id]
        event_data['plus_ones'] = [
            entry for entry in event_data['plus_ones']
            if entry['added_by_id'] != user_id
        ]
        
        save_event_state(
            context.chat_data.get('main_message_id'), 
            context.chat_data.get('main_chat_id'),
            context.chat_data.get('shuffled_teams'), 
            context.chat_data.get('shuffle_error')
        )

    # Handle admin commands
    elif data == "admin_close_collection":
        event_data['status'] = 'closed'
        
        save_event_state(
            context.chat_data.get('main_message_id'), 
            context.chat_data.get('main_chat_id'),
            context.chat_data.get('shuffled_teams'), 
            context.chat_data.get('shuffle_error')
        )
        await query.answer("Vote closed!")
    elif data == "admin_open_collection":
        event_data['status'] = 'open'
        
        save_event_state(
            context.chat_data.get('main_message_id'), 
            context.chat_data.get('main_chat_id'),
            context.chat_data.get('shuffled_teams'), 
            context.chat_data.get('shuffle_error')
        )
        await query.answer("Vote opened!")
    
    # If this was not 'admin_new_event' or shuffle-related (which handle send_main_message internally)
    if not (data == "admin_new_event" or data == "admin_shuffle_teams" or data.startswith("select_teams_")):
        await send_main_message(update, context)

# --- НОВЫЙ ХЕНДЛЕР: Ошибка при запуске ConversationHandler ---
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels and ends the conversation."""
    user = update.effective_user
    logger.info("User %s canceled the conversation.", user.first_name)
    await update.message.reply_text(
        "Operation cancelled."
    )
    return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a message when the command /help is issued."""
    chat_id = update.effective_chat.id
    # Загружаем chat-специфичные данные
    load_chat_specific_state_for_context(chat_id, context)
    
    logger.info(f"'/help' command received from user {update.effective_user.id}.")
    await update.message.reply_text(
        "Я бот для сбора на футбол!\n"
        "Используйте /start для начала нового события.\n"
        "Нажмите кнопки, чтобы указать свое участие или управлять событием."
    )

async def post_init(application: Application) -> None:
    """
    Выполняется после инициализации Application и установки вебхука.
    Используется для начальной настройки, которая требует объекта bot.
    """
    # Если вы используете вебхуки, убедитесь, что WEBHOOK_URL установлен
    WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
    if WEBHOOK_URL:
        # Получаем текущую информацию о вебхуке
        webhook_info = await application.bot.get_webhook_info()
        logger.info(f"Current webhook info: {webhook_info}")
        
        # Устанавливаем вебхук, только если он отличается от текущего
        expected_webhook_url = f"{WEBHOOK_URL}/{TOKEN}"
        if webhook_info.url != expected_webhook_url:
            await application.bot.set_webhook(url=expected_webhook_url)
            logger.info(f"Webhook set to: {expected_webhook_url}")
        else:
            logger.info(f"Webhook is already set to: {expected_webhook_url}")
    else:
        logger.info("Running in polling mode (no WEBHOOK_URL set).")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a message to the user."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    if update and update.effective_message:
        await update.effective_message.reply_text("Произошла ошибка. Пожалуйста, попробуйте еще раз.")
    elif update and update.callback_query:
        await update.callback_query.answer("Произошла ошибка. Пожалуйста, попробуйте еще раз.")

# --- ОСНОВНАЯ ФУНКЦИЯ БОТА ---

def main() -> None:
    """Runs the bot."""
    application = Application.builder().token(TOKEN).post_init(post_init).build()

    # *** ВАЖНОЕ ИЗМЕНЕНИЕ: Загрузка глобального состояния из Redis при старте ***
    load_global_event_data_from_redis()
    
    # *** ИЗМЕНЕНИЕ: Удаление проблемного блока из main() ***
    # УДАЛЕНО:
    # if loaded_message_id and loaded_chat_id:
    #     if loaded_chat_id not in application.chat_data:
    #         application.chat_data[loaded_chat_id] = {} # ЭТА СТРОКА ВЫЗЫВАЛА ОШИБКУ
    #     application.chat_data[loaded_chat_id]['main_message_id'] = loaded_message_id
    #     application.chat_data[loaded_chat_id]['main_chat_id'] = loaded_chat_id
    #     application.chat_data[loaded_chat_id]['shuffled_teams'] = loaded_shuffled_teams
    #     application.chat_data[loaded_chat_id]['shuffle_error'] = loaded_shuffle_error
    #     logger.info(f"Loaded main message ID {loaded_message_id} for chat {loaded_chat_id} from Redis.")
    # Вместо этого, `load_chat_specific_state_for_context` будет вызываться в каждом обработчике.
# --- ОБРАБОТЧИКИ КОМАНД ---
    set_title_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_command_title_entry),
            CallbackQueryHandler(set_title_prompt_callback, pattern=r'^admin_set_title$') # Добавляем для кнопки "Edit Title"
        ],
        states={
            TITLE_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title)],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
    )
    application.add_handler(set_title_conv_handler)
    application.add_handler(CommandHandler("help", help_command))

    # --- ОБРАБОТЧИК КНОПОК ---
    # Хендлер для выбора количества команд после "Shuffle"
    application.add_handler(CallbackQueryHandler(handle_num_teams_selection, pattern=r'^select_teams_\d+$'))
    # Хендлер для кнопки "Shuffle" (он теперь начинает процесс, а не сразу перемешивает)
    application.add_handler(CallbackQueryHandler(start_num_teams_selection, pattern=r'^admin_shuffle_teams$'))
    # Общий хендлер для всех остальных кнопок
    application.add_handler(CallbackQueryHandler(button_callback))

    # --- ОБРАБОТЧИК ОШИБОК ---
    application.add_error_handler(error_handler)

    # --- ЗАПУСК БОТА ---
    WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
    if WEBHOOK_URL:
        # Режим вебхука для Render
        application.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get('PORT', 8443)),
            url_path=TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TOKEN}"
        )
        logger.info(f"Бот запущен в режиме вебхука на Render. URL: {WEBHOOK_URL}/{TOKEN}")
    else:
        # Режим long polling для локального запуска или тестирования
        logger.info("Бот запущен в режиме long polling.")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
