import telebot
from telebot import types

TOKEN = "8661373105:AAHgv-nQ17CZ-aqzTKKF2xMkfctsStq6-vA"
bot = telebot.TeleBot(TOKEN)

GROUP_ID = -1002347522104


@bot.message_handler(commands=["start", "menu"])
def send_menu(message):
  # Твоє текст-привітання
  text = (
      "👋 Привіт! Це помічник 8-Г класу.\n\nТут ти завжди можеш швидко знайти"
      " всю необхідну інформацію. Обери потрібний розділ нижче: 🔽"
  )

  keyboard = types.InlineKeyboardMarkup(row_width=1)

  btn1 = types.InlineKeyboardButton(
      "📅 Розклад уроків",
      url="https://drive.google.com/file/d/1CcnlziZLO-cDUbM9fWmhsAJOMMoGp3E3/preview",
  )
  btn2 = types.InlineKeyboardButton(
      "📜 Правила групи",
      url="https://telegra.ph/Pravila-nashogo-chatu-8-G-klas-07-05",
  )
  btn3 = types.InlineKeyboardButton(
      "🧹 Правила чергування",
      url="https://telegra.ph/Pravila-cherguvannya-v-klas%D1%96-12-30",
  )

  keyboard.add(btn1, btn2, btn3)

  bot.send_message(GROUP_ID, text, reply_markup=keyboard)


# Примусово прибираємо вебхук
bot.remove_webhook()

print("Бот успішно запущено і готовий працювати...")
bot.polling(none_stop=True)
