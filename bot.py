import os
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import asyncio
from aiohttp import web
from upstash_redis.asyncio import Redis

# Загружаем переменные окружения
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(BASE_DIR, "variables.env")
load_dotenv(dotenv_path=env_path)

TOKEN = os.getenv("DISCORD_TOKEN")
REDIS_URL = os.getenv("UPSTASH_REDIS_REST_URL")
REDIS_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

if not TOKEN or not REDIS_URL or not REDIS_TOKEN:
    print("❌ Ошибка: Убедись, что DISCORD_TOKEN, UPSTASH_REDIS_REST_URL и UPSTASH_REDIS_REST_TOKEN заданы!")
    exit(1)

# Инициализируем асинхронный клиент Upstash Redis
redis = Redis(url=REDIS_URL, token=REDIS_TOKEN)

# Настройка бота со слэш-командами
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

cached_webhooks = {}

# Поиск или динамическое создание вебхука в целевом канале
async def get_target_webhook(channel):
    if channel.id in cached_webhooks:
        return cached_webhooks[channel.id]
    try:
        webhooks = await channel.webhooks()
        for wh in webhooks:
            if wh.name == f"Bridge-{channel.id}":
                cached_webhooks[channel.id] = wh
                return wh
        wh = await channel.create_webhook(name=f"Bridge-{channel.id}")
        cached_webhooks[channel.id] = wh
        return wh
    except Exception as e:
        print(f"🔴 Ошибка вебхука в канале {channel.id}: {e}")
        return None

# Хендлер веб-сервера для Render / UptimeRobot
async def handle(request):
    return web.Response(text="Мультисерверный мост на Upstash Redis активен!")

@bot.event
async def on_ready():
    print(f"✅ Бот авторизован как: {bot.user.name}")
    try:
        synced = await bot.tree.sync()
        print(f"🔮 Синхронизировано слэш-команд: {len(synced)}")
    except Exception as e:
        print(f"🔴 Ошибка синхронизации команд: {e}")

# СЛЭШ-КОМАНДА: Связать каналы
@bot.tree.command(name="bconnect", description="Связать исходный канал с целевым")
@app_commands.describe(source="Канал, ОТКУДА забирать сообщения", target="Канал, КУДА пересылать сообщения")
@app_commands.checks.has_permissions(administrator=True)
async def bconnect(interaction: discord.Interaction, source: discord.TextChannel, target: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    
    key = f"bridge:{source.id}"
    target_id_str = str(target.id)
    
    # Получаем текущий список целей для этого канала из Redis
    current_targets = await redis.lrange(key, 0, -1)
    
    if current_targets and target_id_str in current_targets:
        await interaction.followup.send(f"⚠️ Мост между {source.mention} и {target.mention} уже существует!")
        return

    # Записываем ID целевого канала в список Redis
    await redis.rpush(key, target_id_str)
    
    await interaction.followup.send(f"✅ Успешно создан мост:\n📥 Из: {source.mention}\n📤 В: {target.mention}")

# СЛЭШ-КОМАНДА: Разорвать связь
@bot.tree.command(name="bdisconnect", description="Удалить связь между каналами")
@app_commands.checks.has_permissions(administrator=True)
async def bdisconnect(interaction: discord.Interaction, source: discord.TextChannel, target: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    
    key = f"bridge:{source.id}"
    target_id_str = str(target.id)
    
    # Удаляем конкретный target_id из списка в Redis (0 означает удалить все совпадения)
    deleted_count = await redis.lrem(key, 0, target_id_str)
    
    if deleted_count > 0:
        await interaction.followup.send(f"❌ Мост между {source.mention} и {target.mention} успешно удален!")
    else:
        await interaction.followup.send(f"⚠️ Связь между этими каналами не найдена в Redis.")

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    # Защита от зацикливания собственных вебхуков бота
    if message.webhook_id and message.webhook_id in [wh.id for wh in cached_webhooks.values()]:
        return

    # Проверяем в Redis, привязаны ли получатели к каналу, откуда пришло сообщение
    key = f"bridge:{message.channel.id}"
    target_channels_data = await redis.lrange(key, 0, -1)

    if not target_channels_data:
        return

    for target_id_bytes in target_channels_data:
        # Приводим полученные данные к числу ID
        target_id = int(target_id_bytes)
        
        target_channel = bot.get_channel(target_id)
        if not target_channel:
            try:
                target_channel = await bot.fetch_channel(target_id)
            except:
                continue

        # Собираем вложения (картинки, файлы), если они есть
        files = []
        if message.attachments:
            for attachment in message.attachments:
                try: 
                    files.append(await attachment.to_file())
                except: 
                    pass

        # Формируем имя отправителя и сервер назначения
        guild_name = f" [{message.guild.name}]" if message.guild else ""
        display_name = f"{message.author.display_name}{guild_name}"
        avatar_url = message.author.display_avatar.url if message.author.display_avatar else None

        webhook = await get_target_webhook(target_channel)
        try:
            if webhook:
                webhook_embeds = []
                if message.embeds:
                    for emb in message.embeds:
                        webhook_embeds.append(discord.Embed.from_dict(emb.to_dict()))

                content = message.content if message.content else None
                if not avatar_url:
                    avatar_url = webhook.url

                # Если есть хоть какой-то контент — отправляем через вебхук
                if content or files or webhook_embeds:
                    await webhook.send(
                        content=content,
                        username=display_name,
                        avatar_url=avatar_url,
                        embeds=webhook_embeds if webhook_embeds else discord.utils.MISSING,
                        files=files if files else discord.utils.MISSING
                    )
        except Exception as e:
            print(f"🔴 Ошибка пересылки в целевой канал {target_id}: {e}")

    await bot.process_commands(message)

async def main():
    # Запуск веб-сервера aiohttp для прохождения проверок Render
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    
    site = web.TCPSite(runner, '0.0.0.0', 10000)
    await site.start()
    print("🌐 HTTP-сервер запущен на порту 10000")

    try:
        await bot.start(TOKEN)
    except KeyboardInterrupt:
        await bot.close()

if __name__ == '__main__':
    asyncio.run(main())
