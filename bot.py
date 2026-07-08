import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import asyncio
from aiohttp import web

# Загружаем переменные (на Render они подтянутся из панели управления)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(BASE_DIR, "variables.env")
load_dotenv(dotenv_path=env_path)

TOKEN = os.getenv("DISCORD_TOKEN")
SOURCE_CHANNEL_ID_RAW = os.getenv("SOURCE_CHANNEL_ID")
TARGET_CHANNEL_ID_RAW = os.getenv("TARGET_CHANNEL_ID")

if not TOKEN or not SOURCE_CHANNEL_ID_RAW or not TARGET_CHANNEL_ID_RAW:
    print("❌ Ошибка загрузки переменных окружения!")
    exit(1)

SOURCE_CHANNEL_ID = int(SOURCE_CHANNEL_ID_RAW)
TARGET_CHANNEL_ID = int(TARGET_CHANNEL_ID_RAW)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Простейший веб-обработчик, на который будет стучаться UptimeRobot
async def handle(request):
    return web.Response(text="Бот Пересыльщик активен и работает на Render!")

@bot.event
async def on_ready():
    print(f"✅ Бот успешно авторизован как: {bot.user.name}")
    print("🚀 МОСТ ПЕРЕСЫЛКИ НАМЕРТВО ЗАКРЕПЛЕН В ОБЛАКЕ RENDER!")

@bot.event
async def on_message(message: discord.Message):
    # Игнорируем сообщения от самого бота
    if message.author == bot.user or message.webhook_id:
        return

    # Проверяем, что сообщение из нужного канала
    if message.channel.id == SOURCE_CHANNEL_ID:
        target_channel = bot.get_channel(TARGET_CHANNEL_ID)
        if target_channel is None:
            try: 
                target_channel = await bot.fetch_channel(TARGET_CHANNEL_ID)
            except Exception as e: 
                print(f"Не удалось найти целевой канал: {e}")
                return

        # Собираем вложения (картинки, файлы)
        files = []
        if message.attachments:
            for attachment in message.attachments:
                try: 
                    files.append(await attachment.to_file())
                except: 
                    pass

        # Формируем красивое имя автора
        guild_name = f" [{message.guild.name}]" if message.guild else ""
        display_name = f"{message.author.display_name}{guild_name}"

        try:
            # Прямая отправка в канал (самый стабильный вариант для Render)
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
            print(f"Ошибка отправки сообщения: {e}")

    await bot.process_commands(message)

async def main():
    # 1. Мгновенно поднимаем веб-сервер на порту 10000 для прохождения проверки Render
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Порт 10000 — строго для Render
    site = web.TCPSite(runner, '0.0.0.0', 10000)
    await site.start()
    print("🌐 Наземный веб-сервер aiohttp успешно открыл порт 10000!")

    # 2. Запускаем Дискорд бота в этой же петле событий
    try:
        await bot.start(TOKEN)
    except KeyboardInterrupt:
        await bot.close()

if __name__ == '__main__':
    asyncio.run(main())
