import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import asyncio
from aiohttp import web

# Загружаем переменные
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

cached_webhook = None

# Функция поиска или создания вебхука в целевом канале
async def get_target_webhook(channel):
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
        print(f"⚠️ Нет прав на управление вебхуками в канале {channel.id}!")
        return None
    except Exception as e:
        print(f"Ошибка вебхука: {e}")
        return None

# Простейший веб-обработчик для UptimeRobot
async def handle(request):
    return web.Response(text="Бот Пересыльщик активен и работает на Render!")

@bot.event
async def on_ready():
    print(f"✅ Бот успешно авторизован как: {bot.user.name}")
    print("🚀 МОСТ С ИСПРАВЛЕННЫМИ ВЕБХУКАМИ И ЭМБЕДАМИ НА RENDER!")

@bot.event
async def on_message(message: discord.Message):
    # Защита от зацикливания: игнорируем себя и свои же вебхуки
    if message.author == bot.user or message.webhook_id:
        global cached_webhook
        if cached_webhook and message.webhook_id == cached_webhook.id:
            return
        return

    # Проверяем канал отправки
    if message.channel.id == SOURCE_CHANNEL_ID:
        target_channel = bot.get_channel(TARGET_CHANNEL_ID)
        if target_channel is None:
            try: 
                target_channel = await bot.fetch_channel(TARGET_CHANNEL_ID)
            except: 
                return

        # Скачиваем вложения, если они есть
        files = []
        if message.attachments:
            for attachment in message.attachments:
                try: 
                    files.append(await attachment.to_file())
                except: 
                    pass

        # Копируем аватарку, ник и добавляем имя сервера
        guild_name = f" [{message.guild.name}]" if message.guild else ""
        display_name = f"{message.author.display_name}{guild_name}"
        avatar_url = message.author.display_avatar.url

        # Стучимся за вебхуком
        webhook = await get_target_webhook(target_channel)

        try:
            if webhook:
                # Фикс для Эмбедов: пересобираем их из оригинального сообщения
                webhook_embeds = []
                if message.embeds:
                    for Glen_emb in message.embeds:
                        webhook_embeds.append(discord.Embed.from_dict(Glen_emb.to_dict()))

                content = message.content if message.content else None
                
                # Отправляем (теперь вебхук сожрёт и текст, и файлы, и эмбеды)
                if content or files or webhook_embeds:
                    await webhook.send(
                        content=content,
                        username=display_name,
                        avatar_url=avatar_url,
                        embeds=webhook_embeds if webhook_embeds else discord.utils.MISSING,
                        files=files if files else discord.utils.MISSING
                    )
            else:
                # Резервный вариант без вебхука, если на сервере нет прав
                backup_embeds = []
                if message.embeds:
                    for Glen_emb in message.embeds:
                        backup_embeds.append(discord.Embed.from_dict(Glen_emb.to_dict()))

                if backup_embeds:
                    await target_channel.send(embed=backup_embeds[0], files=files if files else None)
                else:
                    clean_text = f"**{display_name}:** {message.content}"
                    if message.content or files:
                        await target_channel.send(
                            content=clean_text if message.content else f"**{display_name}** прикрепил файлы:", 
                            files=files if files else None
                        )
        except Exception as e:
            print(f"🔴 Ошибка отправки: {e}")

    await bot.process_commands(message)

async def main():
    # Поднимаем веб-сервер для прохождения проверок Render и пинга UptimeRobot
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    
    site = web.TCPSite(runner, '0.0.0.0', 10000)
    await site.start()
    print("🌐 Наземный веб-сервер aiohttp успешно открыл порт 10000!")

    # Стартуем бота
    try:
        await bot.start(TOKEN)
    except KeyboardInterrupt:
        await bot.close()

if __name__ == '__main__':
    asyncio.run(main())
