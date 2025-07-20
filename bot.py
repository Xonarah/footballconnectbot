import logging
import os # Импортируем модуль os для доступа к переменным окружения
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

# Токен бота, полученный от @BotFather
# TOKEN = "" # Эту строку теперь удаляем или комментируем
# Вместо этого получаем токен из переменной окружения
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# Проверяем, что токен был установлен
if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable not set.")

# Временное хранилище для данных о событии (для простоты, пока без базы данных)
event_data = {
    'status': 'open',  # 'open' или 'closed'
    'title': None,  # Title will be set by user after /start or via 'Edit Title' button
    'participants': {},  # user_id: {'name': name, 'status': 'going'/'not_going'/'maybe', 'username': username_or_none}
    'plus_ones': []      # List to store individual +1 entries.
                         # Each entry will be a dictionary: {'added_by_id': user_id, 'added_by_name': user_name, 'added_by_username': username_or_none}
}

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
    # Escape user_name for HTML to prevent parsing errors
    escaped_user_name = html.escape(user_name)
    return f'<a href="tg://user?id={user_id}">{escaped_user_name}</a>'


async def get_event_message_and_keyboard(context: ContextTypes.DEFAULT_TYPE) -> tuple[str, InlineKeyboardMarkup]:
    """Generates the event message text and inline keyboard."""
    global event_data # Use global event data

    direct_going_participants = [] # For those who directly marked "Going"
    plus_one_entries_formatted = [] # For already formatted +1 entries
    not_going_list = []
    maybe_list = []
    total_going_count = 0

    # Separate participants by status
    for user_id, user_info in event_data['participants'].items():
        name = user_info['name']
        status = user_info['status']
        username = user_info.get('username') # Get username if available

        # Formulate display name (clickable)
        display_name = get_clickable_name(user_id, name, username)

        if status == 'going':
            direct_going_participants.append(display_name)
            total_going_count += 1
        elif status == 'not_going':
            not_going_list.append(display_name)
        elif status == 'maybe':
            maybe_list.append(display_name)

    # Format +1 entries separately
    for plus_one_entry in event_data['plus_ones']:
        added_by_id = plus_one_entry['added_by_id']
        added_by_name = plus_one_entry['added_by_name']
        added_by_username = plus_one_entry.get('added_by_username') # Get username if available

        # Formulate clickable name of the adder
        clickable_adder = get_clickable_name(added_by_id, added_by_name, added_by_username)
        plus_one_entries_formatted.append(f"➕ (+1 from {clickable_adder})")
        total_going_count += 1

    # Formulate message text
    message_text = ""
    # Event title now in <b> HTML tags and escaped
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
    if 'shuffled_teams' in context.chat_data and context.chat_data['shuffled_teams']:
        message_text += "--- TEAM COMPOSITIONS ---\n"
        team_emojis = ["🔵", "🔴", "🟡", "🟢", "🟣", "⚪"] # More colors for more teams
        for i, team in enumerate(context.chat_data['shuffled_teams']):
            emoji = team_emojis[i % len(team_emojis)] # Alternate emojis
            message_text += f"{emoji} Team {i+1}:\n"
            if team:
                message_text += "\n".join([f"- {player}" for player in team]) + "\n"
            else:
                message_text += "  (Empty)\n"
        message_text += "------------------------\n\n"
    elif 'shuffle_error' in context.chat_data and context.chat_data['shuffle_error']:
        message_text += f"\n❗️ {context.chat_data['shuffle_error']}\n\n"

    keyboard = [] # Start with an empty keyboard

    if event_data['status'] == 'open':
        status_buttons = [
            InlineKeyboardButton("✅ Going", callback_data="set_status_going"),
            InlineKeyboardButton("❌ Not Going", callback_data="set_status_not_going"),
            InlineKeyboardButton("🤔 Thinking", callback_data="set_status_maybe"),
        ]

        # Buttons for +/-1
        plus_minus_buttons = [
            InlineKeyboardButton("➕ (+1)", callback_data="add_plus_one"),
            InlineKeyboardButton("➖ (-1)", callback_data="remove_plus_one"),
            InlineKeyboardButton("🔄 Reset", callback_data="reset_my_status"),
        ]
        keyboard.append(status_buttons)
        keyboard.append(plus_minus_buttons)

    # Button to toggle collection status
    toggle_status_button = InlineKeyboardButton(
        "⛔ Close Vote" if event_data['status'] == 'open' else "▶️ Open Vote", # Changed text
        callback_data="admin_close_collection" if event_data['status'] == 'open' else "admin_open_collection"
    )

    current_admin_buttons_row = [toggle_status_button]

    # Add shuffle button ONLY if collection is closed
    if event_data['status'] == 'closed': # Shuffle button only when closed
        shuffle_button = InlineKeyboardButton("🔀 Shuffle", callback_data="admin_shuffle_teams")
        current_admin_buttons_row.append(shuffle_button)

    # Add 'Edit Title' button only if collection is open
    if event_data['status'] == 'open':
        current_admin_buttons_row.extend([
            InlineKeyboardButton("✏️ Edit Title", callback_data="admin_set_title")
        ])

    keyboard.append(current_admin_buttons_row) # Add row with admin buttons

    reply_markup = InlineKeyboardMarkup(keyboard)

    # Use 'HTML' for Parse Mode for clickable links
    return message_text, reply_markup


async def send_main_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends or edits the main bot message."""
    message_text, reply_markup = await get_event_message_and_keyboard(context)

    # If main_message_id is not present or main_chat_id doesn't match current chat, send a new message
    if 'main_message_id' not in context.chat_data or context.chat_data['main_chat_id'] != update.effective_chat.id:
        sent_message = await update.effective_message.reply_html(text=message_text, reply_markup=reply_markup)
        context.chat_data['main_message_id'] = sent_message.message_id
        context.chat_data['main_chat_id'] = sent_message.chat_id
        logger.info(f"New main message sent. ID: {sent_message.message_id}")
    else: # Otherwise, try to edit the existing one
        try:
            await context.bot.edit_message_text(
                chat_id=context.chat_data['main_chat_id'],
                message_id=context.chat_data['main_message_id'],
                text=message_text,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
            logger.info(f"Main message {context.chat_data['main_message_id']} updated.")
        except telegram.error.BadRequest as e:
            if "Message is not modified" in str(e):
                logger.info(f"Main message {context.chat_data['main_message_id']} was not modified (no new data). Ignoring.")
            else:
                logger.warning(f"Failed to update main message (ID: {context.chat_data['main_message_id']}) due to BadRequest: {e}. Sending new message.")
                sent_message = await update.effective_message.reply_html(text=message_text, reply_markup=reply_markup)
                context.chat_data['main_message_id'] = sent_message.message_id
                context.chat_data['main_chat_id'] = sent_message.chat_id
        except Exception as e:
            logger.warning(f"An unexpected error occurred while updating the main message (ID: {context.chat_data['main_message_id']}): {e}. Sending new message.")
            sent_message = await update.effective_message.reply_html(text=message_text, reply_markup=reply_markup)
            context.chat_data['main_message_id'] = sent_message.message_id
            context.chat_data['main_chat_id'] = sent_message.chat_id


async def start_command_title_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for /start to prompt for title and reset event data."""
    global event_data
    logger.info(f"'/start' command received from user {update.effective_user.id}.")

    # Reset event_data for a new event
    event_data = {
        'status': 'open',
        'title': None,
        'participants': {},
        'plus_ones': []
    }
    # Clear chat_data specific to the previous event (like shuffled teams, main message ID)
    context.chat_data.clear()

    await update.message.reply_text("Please enter the event title:")
    logger.info(f"Prompted user {update.effective_user.id} to enter new title for a new event.")
    return TITLE_STATE

async def set_title_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for 'Edit Title' button to prompt for title."""
    await update.callback_query.answer("Enter new title.") # Small pop-up notification
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Please enter the new event title in the chat."
    )
    logger.info(f"'Edit Title' button pressed by user {update.effective_user.id}. Prompting for title.")
    return TITLE_STATE

async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receives new title from user and updates it."""
    global event_data
    if update.message and update.message.text:
        event_data['title'] = update.message.text.strip() # Save new title, remove leading/trailing spaces
        await update.message.reply_text(f"Event title updated to: {event_data['title']}")
        logger.info(f"Event title updated to: '{event_data['title']}' by user {update.effective_user.id}")

        await send_main_message(update, context) # Update the main message with new title

        return ConversationHandler.END # End the conversation
    else:
        logger.warning("receive_title called but no text message found or message is empty. Remaining in TITLE_STATE.")
        await update.message.reply_text("Please enter the new title as text.")
        return TITLE_STATE # Stay in state if no text or empty


async def start_num_teams_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the 'Shuffle' button press, performs checks, and prompts for number of teams with buttons."""
    query = update.callback_query
    await query.answer() # Acknowledge the button press

    # Check if vote is closed
    if event_data['status'] == 'open':
        await query.answer("Please close the vote before shuffling teams.")
        await send_main_message(update, context) # Ensure button state is updated
        return

    # Collect players for shuffling
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

    # Modified minimum player check (at least 2 players to form teams)
    if total_players < 2:
        error_message = ""
        if total_players == 0:
            error_message = "No players marked as 'Going' to shuffle."
        elif total_players == 1:
            error_message = "Cannot form teams with only one player."
        
        context.chat_data['shuffle_error'] = error_message
        context.chat_data['shuffled_teams'] = [] # Clear any previous shuffle data
        await query.answer(error_message)
        await send_main_message(update, context) # Update message with error
        return

    # Store players for the next step
    context.chat_data['players_for_shuffle'] = all_players_to_shuffle
    context.chat_data['total_players_for_shuffle'] = total_players # Store total players as well

    # Generate buttons for 2, 3, and 4 teams, if possible
    team_buttons = []
    num_cols = 3 # Number of buttons per row
    current_row = []
    
    # Only offer 2, 3, 4 teams, but only if total_players is sufficient
    possible_num_teams_options = [2, 3, 4]
    
    for i in possible_num_teams_options:
        if i <= total_players: # Only add button if total_players is enough for 'i' teams
            current_row.append(InlineKeyboardButton(str(i), callback_data=f"select_teams_{i}"))
            if len(current_row) == num_cols:
                team_buttons.append(current_row)
                current_row = []
    if current_row: # Add any remaining buttons
        team_buttons.append(current_row)

    reply_markup = InlineKeyboardMarkup(team_buttons)
    temp_message = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"There are {total_players} players available. Please select the number of teams:",
        reply_markup=reply_markup
    )
    context.chat_data['temp_shuffle_message_id'] = temp_message.message_id
    context.chat_data['temp_shuffle_message_chat_id'] = temp_message.chat_id # Store chat ID of temp message
    logger.info(f"User {query.from_user.id} initiated shuffle. Prompting for num teams with buttons.")


async def handle_num_teams_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Receives the desired number of teams from button press and performs the shuffle."""
    query = update.callback_query
    await query.answer()

    selected_teams_str = query.data.replace("select_teams_", "")
    num_teams = int(selected_teams_str)

    all_players_to_shuffle = context.chat_data.get('players_for_shuffle', [])
    total_players = context.chat_data.get('total_players_for_shuffle', 0)

    # Basic validation (should ideally be caught by button generation logic, but good for robustness)
    if not (2 <= num_teams <= total_players): # Ensure num_teams is within valid range (2 to total_players)
        context.chat_data['shuffle_error'] = "Invalid number of teams selected. Please try again."
        await query.answer("Invalid selection.")
        # Attempt to delete temp message before re-sending main
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
        await send_main_message(update, context) # Update main message with error
        return

    random.shuffle(all_players_to_shuffle)

    teams = [[] for _ in range(num_teams)]
    for i, player in enumerate(all_players_to_shuffle):
        teams[i % num_teams].append(player)

    context.chat_data['shuffled_teams'] = teams
    context.chat_data['shuffle_error'] = None

    # Delete the temporary message
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

    # Clear temporary shuffle data
    if 'players_for_shuffle' in context.chat_data:
        del context.chat_data['players_for_shuffle']
    if 'total_players_for_shuffle' in context.chat_data:
        del context.chat_data['total_players_for_shuffle']

    await send_main_message(update, context) # Update main message with shuffled teams
    await query.answer(f"Teams shuffled into {num_teams} teams!")
    logger.info(f"Teams shuffled into {num_teams} teams by user {query.from_user.id}.")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles inline button presses that are not part of ConversationHandlers."""
    query = update.callback_query
    data = query.data
    logger.info(f"button_callback called with data: {data} from user {query.from_user.id}")

    await query.answer() # Acknowledge the button press

    user_id = query.from_user.id
    user_name = query.from_user.full_name
    username = query.from_user.username

    # Initialize user if not in participants, including username
    if user_id not in event_data['participants']:
        event_data['participants'][user_id] = {'name': user_name, 'status': None, 'username': username}

    # Clear shuffle data for any action except specific shuffle flows
    if not (data == "admin_shuffle_teams" or data.startswith("select_teams_")):
        context.chat_data['shuffled_teams'] = []
        context.chat_data['shuffle_error'] = None
        # Also clear temp message ID if it exists (e.g., user presses something else during team selection)
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


    # --- Check for vote status ---
    # If vote is closed and it's not an admin command (excluding 'shuffle' which has its own handler)
    if event_data['status'] == 'closed' and not data.startswith("admin_"):
        await query.answer("Vote is closed, participation is unavailable.")
        await send_main_message(update, context) # Re-send to update button states if needed
        return # Exit function, do not process further

    # Handle status selection
    if data.startswith("set_status_"):
        new_status = data.replace("set_status_", "")
        event_data['participants'][user_id]['status'] = new_status
        event_data['participants'][user_id]['username'] = username # Update username

    # Handle +1/-1
    elif data == "add_plus_one":
        event_data['plus_ones'].append({
            'added_by_id': user_id,
            'added_by_name': user_name,
            'added_by_username': username
        })
    elif data == "remove_plus_one":
        found_and_removed = False
        for i in range(len(event_data['plus_ones']) - 1, -1, -1):
            if event_data['plus_ones'][i]['added_by_id'] == user_id:
                del event_data['plus_ones'][i]
                found_and_removed = True
                break
        if not found_and_removed:
            await query.answer("Cannot decrease, as you have no additional participants.")

    elif data == "reset_my_status":
        if user_id in event_data['participants']:
            del event_data['participants'][user_id]
        event_data['plus_ones'] = [
            entry for entry in event_data['plus_ones']
            if entry['added_by_id'] != user_id
        ]

    # Handle admin commands
    elif data == "admin_close_collection":
        event_data['status'] = 'closed'
        await query.answer("Vote closed!")
    elif data == "admin_open_collection":
        event_data['status'] = 'open'
        await query.answer("Vote opened!")
    # admin_shuffle_teams is now handled by start_num_teams_selection
    # select_teams_X is now handled by handle_num_teams_selection

    # For any actions handled here, update the main message
    if data not in ["admin_set_title", "admin_shuffle_teams"] and not data.startswith("select_teams_"):
        await send_main_message(update, context)


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the current ConversationHandler."""
    await update.message.reply_text("Action canceled.")
    logger.info(f"Conversation canceled by user {update.effective_user.id}")
    await send_main_message(update, context) # Update main message after cancellation
    return ConversationHandler.END

# Main function that runs the bot
def main() -> None:
    """Runs the bot."""
    application = Application.builder().token(TOKEN).build()

    # ConversationHandler for setting the title (triggered by /start OR 'Edit Title' button)
    set_title_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_command_title_entry),
            CallbackQueryHandler(set_title_prompt_callback, pattern='^admin_set_title$')
        ],
        states={
            TITLE_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )

    application.add_handler(set_title_conv_handler)
    # Handler for the 'Shuffle' button itself (initiates the button selection for teams)
    application.add_handler(CallbackQueryHandler(start_num_teams_selection, pattern='^admin_shuffle_teams$'))
    # Handler for selecting the number of teams via buttons
    application.add_handler(CallbackQueryHandler(handle_num_teams_selection, pattern='^select_teams_\d+$'))
    application.add_handler(CallbackQueryHandler(button_callback)) # This handler goes after and catches all other callbacks

    # --- ИЗМЕНЕНИЯ ЗДЕСЬ: Настройка вебхука вместо polling ---
    # Получаем порт из переменной окружения Render. Render сам устанавливает PORT.
    PORT = int(os.environ.get("PORT", "8080")) # Изменим порт по умолчанию на 8080, если PORT не установлен
                                           # Render обычно использует 10000, но 8080 - это обычный веб-порт

    # Render предоставляет публичный домен через переменную окружения RENDER_EXTERNAL_HOSTNAME
    # Используем HTTPS, как требует Telegram
    RENDER_EXTERNAL_HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if not RENDER_EXTERNAL_HOSTNAME:
        raise ValueError("RENDER_EXTERNAL_HOSTNAME environment variable not set. This is required for webhooks on Render.")

    # url_path может быть любым уникальным путем, например, токеном бота для большей уникальности.
    WEBHOOK_PATH = f"/{TOKEN}" # Делаем путь уникальным, используя токен бота

    # Формируем полный URL вебхука
    WEBHOOK_URL = f"https://{RENDER_EXTERNAL_HOSTNAME}{WEBHOOK_PATH}"

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=WEBHOOK_URL # <--- ЭТО САМОЕ ВАЖНОЕ ИЗМЕНЕНИЕ
    )

    logger.info(f"Бот запущен в режиме вебхука на Render. URL: {WEBHOOK_URL}")

if __name__ == "__main__":
    main()
