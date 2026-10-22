import os
import asyncio
import logging
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from google import genai
from google.genai import types as genai_types

# Загрузка локальных переменных из .env (для локального запуска)
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    raise ValueError("ОШИБКА: Переменные TELEGRAM_BOT_TOKEN и GEMINI_API_KEY должны быть заданы!")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
client = genai.Client(api_key=GEMINI_API_KEY)


class MenuState(StatesGroup):
    waiting_for_persons = State()
    waiting_for_days = State()
    waiting_for_images = State()


# Генераторы клавиатур с кнопками
def get_persons_keyboard():
    builder = InlineKeyboardBuilder()
    for i in range(1, 5):
        builder.button(text=str(i), callback_data=f"persons_{i}")
    builder.adjust(4)
    return builder.as_markup()


def get_days_keyboard():
    builder = InlineKeyboardBuilder()
    for d in [1, 2, 3, 7]:
        builder.button(text=f"{d} дн.", callback_data=f"days_{d}")
    builder.adjust(4)
    return builder.as_markup()


@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я бот для расчета рациона и составления списка продуктов по картинкам меню.\n\n"
        "Сколько персон будут питаться? Выберите на кнопке ниже или введите число вручную:",
        reply_markup=get_persons_keyboard()
    )
    await state.set_state(MenuState.waiting_for_persons)


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено. Чтобы начать заново, отправьте команду /start")


# Обработка нажатия кнопок для персон
@dp.callback_query(MenuState.waiting_for_persons, F.data.startswith("persons_"))
async def process_persons_callback(callback: types.CallbackQuery, state: FSMContext):
    persons = int(callback.data.split("_")[1])
    await state.update_data(persons=persons)
    await callback.message.edit_text(f"Количество персон: **{persons}** ✅")
    await callback.message.answer(
        "На сколько дней нужно составить рацион и список продуктов? Выберите на кнопке или введите число:",
        reply_markup=get_days_keyboard()
    )
    await state.set_state(MenuState.waiting_for_days)
    await callback.answer()


# Обработка текстового ввода персон (если ввели цифру вручную)
@dp.message(MenuState.waiting_for_persons)
async def process_persons_text(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Пожалуйста, выберите вариант на кнопках или введите число.")
        return
    persons = int(message.text)
    await state.update_data(persons=persons)
    await message.answer(
        f"Количество персон: **{persons}**.\nНа сколько дней нужно составить рацион и список продуктов?",
        reply_markup=get_days_keyboard()
    )
    await state.set_state(MenuState.waiting_for_days)


# Обработка нажатия кнопок для дней
@dp.callback_query(MenuState.waiting_for_days, F.data.startswith("days_"))
async def process_days_callback(callback: types.CallbackQuery, state: FSMContext):
    days = int(callback.data.split("_")[1])
    await state.update_data(days=days, photos=[])
    await callback.message.edit_text(f"Количество дней: **{days}** ✅")
    await callback.message.answer(
        "Отправьте ровно **2 скриншота** с меню (картинками, по очереди). Как только отправите оба, я произведу расчет.")
    await state.set_state(MenuState.waiting_for_images)
    await callback.answer()


# Обработка текстового ввода дней (если ввели цифру вручную)
@dp.message(MenuState.waiting_for_days)
async def process_days_text(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Пожалуйста, выберите вариант на кнопках или введите число.")
        return
    days = int(message.text)
    await state.update_data(days=days, photos=[])
    await message.answer(
        f"Количество дней: **{days}**.\nОтправьте ровно **2 скриншота** с меню (картинками, по очереди).")
    await state.set_state(MenuState.waiting_for_images)


@dp.message(MenuState.waiting_for_images, F.photo)
async def process_images(message: types.Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])

    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    file_bytes = await bot.download_file(file_info.file_path)

    photos.append(file_bytes.read())
    await state.update_data(photos=photos)

    current_count = len(photos)
    if current_count < 2:
        await message.answer(f"Получено фото {current_count}/2. Жду второе!")
    else:
        await message.answer("Оба фото получены! Считаю пропорции и составляю список через Gemini... ⏳")
        await bot.send_chat_action(message.chat.id, "typing")

        persons = data.get("persons")
        days = data.get("days")

        try:
            prompt = (
                f"У меня есть рацион питания на прикрепленных изображениях. "
                f"Нужно составить итоговый список продуктов с разбивкой по категориям (используй интерактивные чек-боксы `- [ ]` для каждого продукта в списке покупок) "
                f"и подробную разбивку по приемам пищи "
                f"на {days} дня(-ей) для {persons} человек(-а). "
                f"Внимательно пересчитай все пропорции ингредиентов под это количество людей и дней."
            )

            response = client.models.generate_content(
                model='gemini-3.8-flash',
                contents=[
                    prompt,
                    genai_types.Part.from_bytes(data=photos[0], mime_type='image/jpeg'),
                    genai_types.Part.from_bytes(data=photos[1], mime_type='image/jpeg'),
                ]
            )

            result_text = response.text

            # 1. Отправляем текстом (если длинное, разбиваем на части)
            if len(result_text) > 4000:
                for i in range(0, len(result_text), 4000):
                    await message.answer(result_text[i:i + 4000])
            else:
                await message.answer(result_text)

            # 2. Сохраняем в файл и отправляем пользователю документов для удобства
            file_bytes_content = result_text.encode('utf-8')
            document = types.BufferedInputFile(file_bytes_content, filename="menu_and_products.txt")
            await message.answer_document(
                document=document,
                caption="📁 Вот ваш готовый расчет в виде текстового файла для удобного сохранения!"
            )

        except Exception as e:
            error_message = str(e)
            if "429" in error_message or "RESOURCE_EXHAUSTED" in error_message or "Too Many Requests" in error_message:
                await message.answer(
                    "⚠️ **Превышен лимит запросов к нейросети.**\n\n"
                    "Пожалуйста, подождите около 1 минуты и повторите попытку (или отправьте меню заново через /start)."
                )
            else:
                await message.answer(f"Произошла ошибка при обработке: {e}")

        await state.clear()
        await message.answer("Чтобы запустить расчет заново, отправьте команду /start")


@dp.message(MenuState.waiting_for_images)
async def process_images_invalid(message: types.Message):
    await message.answer("Пожалуйста, отправьте именно **изображение (картинку)** с меню, а не текст.")


async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())