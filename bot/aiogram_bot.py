import asyncio
import logging
import html
import json
import io
import traceback
from datetime import datetime

import openai
from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import CommandStart, Command
from aiogram.types import FSInputFile, Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode

import config
import database
import openai_utils

# setup
db = database.Database()
logger = logging.getLogger(__name__)

user_semaphores = {}
user_tasks = {}

HELP_MESSAGE = """Commands:
⚪ /retry – Regenerate last bot answer
⚪ /new – Start new dialog
⚪ /mode – Select chat mode
⚪ /settings – Show settings
⚪ /balance – Show balance
⚪ /help – Show help

🎨 Generate images from text prompts in <b>👩‍🎨 Artist</b> /mode
👥 Add bot to <b>group chat</b>: /help_group_chat
🎤 You can send <b>Voice Messages</b> instead of text
"""

HELP_GROUP_CHAT_MESSAGE = """You can add bot to any <b>group chat</b> to help and entertain its participants!

Instructions (see <b>video</b> below):
1. Add the bot to the group chat
2. Make it an <b>admin</b>, so that it can see messages (all other rights can be restricted)
3. You're awesome!

To get a reply from the bot in the chat – @ <b>tag</b> it or <b>reply</b> to its message.
For example: "{bot_username} write a poem about Telegram"
"""


def split_text_into_chunks(text, chunk_size):
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]


async def register_user_if_not_exists(user: types.User, chat_id: int):
    if not db.check_if_user_exists(user.id):
        db.add_new_user(
            user.id,
            chat_id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name
        )
        db.start_new_dialog(user.id)

    if db.get_user_attribute(user.id, "current_dialog_id") is None:
        db.start_new_dialog(user.id)

    if user.id not in user_semaphores:
        user_semaphores[user.id] = asyncio.Semaphore(1)

    if db.get_user_attribute(user.id, "current_model") is None:
        db.set_user_attribute(user.id, "current_model", config.models["available_text_models"][0])

    # back compatibility for n_used_tokens field
    n_used_tokens = db.get_user_attribute(user.id, "n_used_tokens")
    if isinstance(n_used_tokens, int) or isinstance(n_used_tokens, float):  # old format
        new_n_used_tokens = {
            "gpt-3.5-turbo": {
                "n_input_tokens": 0,
                "n_output_tokens": n_used_tokens
            }
        }
        db.set_user_attribute(user.id, "n_used_tokens", new_n_used_tokens)

    # voice message transcription
    if db.get_user_attribute(user.id, "n_transcribed_seconds") is None:
        db.set_user_attribute(user.id, "n_transcribed_seconds", 0.0)

    # image generation
    if db.get_user_attribute(user.id, "n_generated_images") is None:
        db.set_user_attribute(user.id, "n_generated_images", 0)


async def is_bot_mentioned(message: types.Message, bot: Bot):
    try:
        if message.chat.type == "private":
            return True

        if message.text is not None and ("@" + bot.username) in message.text:
            return True

        if message.reply_to_message is not None:
            if message.reply_to_message.from_user.id == bot.id:
                return True
    except:
        return True
    else:
        return False


async def start_handle(message: types.Message, bot: Bot):
    await register_user_if_not_exists(message.from_user, message.chat.id)
    user_id = message.from_user.id

    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    db.start_new_dialog(user_id)

    reply_text = "Hi! I'm <b>ChatGPT</b> bot implemented with OpenAI API 🤖\n\n"
    reply_text += HELP_MESSAGE

    await message.reply(reply_text, parse_mode=ParseMode.HTML)
    await show_chat_modes_handle(message, bot)


async def help_handle(message: types.Message, bot: Bot):
    await register_user_if_not_exists(message.from_user, message.chat.id)
    user_id = message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    await message.reply(HELP_MESSAGE, parse_mode=ParseMode.HTML)


async def help_group_chat_handle(message: types.Message, bot: Bot):
    await register_user_if_not_exists(message.from_user, message.chat.id)
    user_id = message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    text = HELP_GROUP_CHAT_MESSAGE.format(bot_username="@" + bot.username)

    await message.reply(text, parse_mode=ParseMode.HTML)
    if hasattr(config, 'help_group_chat_video_path'):
        video = FSInputFile(config.help_group_chat_video_path)
        await message.reply_video(video)


def get_chat_mode_menu(page_index: int):
    n_chat_modes_per_page = config.n_chat_modes_per_page
    text = f"Select <b>chat mode</b> ({len(config.chat_modes)} modes available):"

    # buttons
    chat_mode_keys = list(config.chat_modes.keys())
    page_chat_mode_keys = chat_mode_keys[page_index * n_chat_modes_per_page:(page_index + 1) * n_chat_modes_per_page]

    # Использование InlineKeyboardBuilder из aiogram 3.x
    builder = InlineKeyboardBuilder()
    for chat_mode_key in page_chat_mode_keys:
        name = config.chat_modes[chat_mode_key]["name"]
        builder.button(text=name, callback_data=f"set_chat_mode|{chat_mode_key}")
    
    # Добавляем по строке за раз
    keyboard = []
    for i in range(0, len(page_chat_mode_keys)):
        keyboard.append([builder.as_markup().inline_keyboard[i][0]])
    
    # pagination
    if len(chat_mode_keys) > n_chat_modes_per_page:
        is_first_page = (page_index == 0)
        is_last_page = ((page_index + 1) * n_chat_modes_per_page >= len(chat_mode_keys))

        pagination_buttons = []
        if not is_first_page:
            pagination_buttons.append(types.InlineKeyboardButton(text="«", callback_data=f"show_chat_modes|{page_index - 1}"))
        if not is_last_page:
            pagination_buttons.append(types.InlineKeyboardButton(text="»", callback_data=f"show_chat_modes|{page_index + 1}"))
        
        if pagination_buttons:
            keyboard.append(pagination_buttons)

    return text, types.InlineKeyboardMarkup(inline_keyboard=keyboard)


async def show_chat_modes_handle(message: types.Message, bot: Bot):
    await register_user_if_not_exists(message.from_user, message.chat.id)
    user_id = message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    text, reply_markup = get_chat_mode_menu(0)
    await message.reply(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def show_chat_modes_callback_handle(query: types.CallbackQuery, bot: Bot):
    await register_user_if_not_exists(query.from_user, query.message.chat.id)
    user_id = query.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    await query.answer()

    page_index = int(query.data.split("|")[1])
    if page_index < 0:
        return

    text, reply_markup = get_chat_mode_menu(page_index)
    try:
        await query.message.edit_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except Exception as e:
        if "message is not modified" in str(e).lower():
            pass
        else:
            raise


async def set_chat_mode_handle(query: types.CallbackQuery, bot: Bot):
    await register_user_if_not_exists(query.from_user, query.message.chat.id)
    user_id = query.from_user.id

    await query.answer()

    chat_mode = query.data.split("|")[1]

    db.set_user_attribute(user_id, "current_chat_mode", chat_mode)
    db.start_new_dialog(user_id)

    await bot.send_message(
        query.message.chat.id,
        f"{config.chat_modes[chat_mode]['welcome_message']}",
        parse_mode=ParseMode.HTML
    )


async def main():
    # Инициализация бота
    bot = Bot(token=config.telegram_token)
    dp = Dispatcher()
    
    # Регистрация обработчиков
    dp.message.register(start_handle, CommandStart())
    dp.message.register(help_handle, Command('help'))
    dp.message.register(help_group_chat_handle, Command('help_group_chat'))
    dp.message.register(show_chat_modes_handle, Command('mode'))
    
    # Регистрация колбеков
    dp.callback_query.register(
        show_chat_modes_callback_handle, 
        lambda c: c.data and c.data.startswith('show_chat_modes')
    )
    dp.callback_query.register(
        set_chat_mode_handle, 
        lambda c: c.data and c.data.startswith('set_chat_mode')
    )

    # Запуск бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())