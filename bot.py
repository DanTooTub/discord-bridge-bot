import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# --- МИНИ ВЕБ-СЕРВЕР ДЛЯ ОБХОДА СНА ---
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Робот работает 24/7!".encode("utf-8"))

    def log_message(self, format, *args):
        return  # Отключаем спам логами запросов в консоль

def run_web_server():
    port = int(os.getenv("PORT", 8080))
    server = ThreadingHTTPServer(("0.0.0.0", port), PingHandler)
    print(f" Наземный веб-сервер запущен на порту {port}")
    server.serve_forever()
# -------------------------------------

# Автоматически определяем папку, где лежит этот скрипт bot.py
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(BASE_DIR, "variables.env")

load_dotenv(dotenv_path=env_path)

TOKEN = os.getenv("DISCORD_TOKEN")
SOURCE_CHANNEL_ID_RAW = os.getenv("SOURCE_CHANNEL_ID")
TARGET_CHANNEL_ID_RAW = os.getenv("TARGET_CHANNEL_ID")

if not TOKEN or not SOURCE_CHANNEL_ID_RAW or not TARGET_CHANNEL_ID_RAW:
    print("❌ Ошибка загрузки переменных!")
    exit(1)

SOURCE_CHANNEL_ID = int(SOURCE_CHANNEL_ID_RAW)
TARGET_CHANNEL_ID = int(TARGET_CHANNEL_ID_RAW)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

cached_webhook = None

async def get_target_webhook(channel):
    """Ищет существующий вебхук бота в канале или создает новый"""
    global cached_webhook
    if cached_webhook:
        return cached_webhook

    try:
        webhooks = await channel.webhooks()
        for wh in webhooks:
            if wh.name == "Bridge Webhook":
                cached_webhook = wh
                return cached_webhook
        
        cached_webhook = await channel.create_webhook(name="Bridge Webhook")
        return cached_webhook
    except discord.Forbidden:
        print(f"⚠️ Предупреждение: У бота нет прав 'Управление веб-хуками' в канале {channel.id}. Переключаюсь на обычную отправку.")
        return None
    except Exception as e:
        print(f"Ошибка при работе с вебхуками: {e}")
        return None

@bot.event
async def on_ready():
    print(f"Бот успешно авторизован как: {bot.user.name}")

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return
        
    if message.webhook_id:
        global cached_webhook
        if cached_webhook and message.webhook_id == cached_webhook.id:
            return

    if message.channel.id == SOURCE_CHANNEL_ID:
        target_channel = bot.get_channel(TARGET_CHANNEL_ID)
        if target_channel is None:
            try: target_channel = await bot.fetch_channel(TARGET_CHANNEL_ID)
            except: return

        # Подготавливаем файлы (всегда список, даже если пустой)
        files = []
        if message.attachments:
            for attachment in message.attachments:
                try: 
                    files.append(await attachment.to_file())
                except: 
                    pass

        guild_name = f" [{message.guild.name}]" if message.guild else ""
        display_name = f"{message.author.display_name}{guild_name}"
        avatar_url = message.author.display_avatar.url

        # Получаем вебхук
        webhook = await get_target_webhook(target_channel)

        try:
            # РЕЖИМ 1: Через вебхук (если права есть)
            if webhook:
                if message.embeds:
                    # Передаем файлы только если они реально есть в списке
                    await webhook.send(
                        username=display_name,
                        avatar_url=avatar_url,
                        embed=message.embeds[0],
                        files=files if files else discord.utils.MISSING
                    )
                else:
                    content = message.content if message.content else None
                    if content or files:
                        await webhook.send(
                            content=content,
                            username=display_name,
                            avatar_url=avatar_url,
                            files=files if files else discord.utils.MISSING
                        )
            
            # РЕЖИМ 2: Аварийный откат (если вебхук почему-то недоступен)
            else:
                if message.embeds:
                    await target_channel.send(embed=message.embeds[0], files=files if files else None)
                else:
                    clean_text = f"**{display_name}:** {message.content}"
                    if message.content or files:
                        await target_channel.send(
                            content=clean_text if message.content else f"**{display_name}** прикрепил файлы:", 
                            files=files if files else None
                        )
                        
        except Exception as e:
            print(f"Непредвиденная ошибка при отправке: {e}")

    await bot.process_commands(message)

# Запускаем веб-сервер в отдельном потоке до старта бота
web_thread = threading.Thread(target=run_web_server, daemon=True)
web_thread.start()

# Запуск бота
bot.run(TOKEN)