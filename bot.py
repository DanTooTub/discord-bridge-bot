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
    print("❌ Ошибка: Убедись, что переменные окружения заданы в Render!")
    exit(1)

# Инициализируем клиент Redis
redis = Redis(url=REDIS_URL, token=REDIS_TOKEN)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

cached_webhooks = {}

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
        print(f"🔴 Ошибка вебхука: {e}")
        return None

async def handle(request):
    return web.Response(text="Мост активен!")

@bot.event
async def on_ready():
    print(f"✅ Бот авторизован: {bot.user.name}")
    try:
        synced = await bot.tree.sync()
        print(f"🔮 Синхронизировано слэш-команд: {len(synced)}")
    except Exception as e:
        print(f"🔴 Ошибка синхронизации команд в Discord API: {e}")

# СЛЭШ-КОМАНДА: Связать каналы
@bot.tree.command(name="bconnect", description="Связать исходный канал с целевым")
@app_commands.describe(source="Канал, ОТКУДА забирать сообщения", target="Канал, КУДА пересылать сообщения")
@app_commands.checks.has_permissions(administrator=True)
async def bconnect(interaction: discord.Interaction, source: discord.TextChannel, target: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    
    # Берем ID напрямую из свойств объекта, принудительно превращая в строку
    source_id_str = str(source.id)
    target_id_str = str(target.id)
    
    key = f"bridge:{source_id_str}"
    
    try:
        current_targets_raw = await redis.lrange(key, 0, -1) or []
        current_targets = [t.decode('utf-8') if isinstance(t, bytes) else str(t) for t in current_targets_raw]
        
        if target_id_str in current_targets:
            await interaction.followup.send(f"⚠️ Мост между {source.mention} и {target.mention} уже существует!")
            return

        await redis.rpush(key, target_id_str)
        await interaction.followup.send(f"✅ Успешно создан мост:\n📥 Из: {source.mention}\n📤 В: {target.mention}")
    except Exception as e:
        print(f"🔴 Ошибка внутри bconnect: {e}")
        await interaction.followup.send(f"❌ Ошибка базы данных: {e}")

# СЛЭШ-КОМАНДА: Разорвать связь
@bot.tree.command(name="bdisconnect", description="Удалить связь между каналами")
@app_commands.checks.has_permissions(administrator=True)
async def bdisconnect(interaction: discord.Interaction, source: discord.TextChannel, target: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    
    source_id_str = str(source.id)
    target_id_str = str(target.id)
    
    key = f"bridge:{source_id_str}"
    
    try:
        await redis.lrem(key, 0, target_id_str)
        await redis.lrem(key, 0, target_id_str.encode('utf-8'))
        await interaction.followup.send(f"❌ Мост между {source.mention} и {target.mention} успешно удален!")
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка удаления: {e}")

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    if message.webhook_id and message.webhook_id in [wh.id for wh in cached_webhooks.values()]:
        return

    key = f"bridge:{message.channel.id}"
    target_channels_data = await redis.lrange(key, 0, -1)

    if not target_channels_data:
        return

    for target_id_raw in target_channels_data:
        # Максимально агрессивная очистка ID от байтовых оберток
        try:
            if isinstance(target_id_raw, bytes):
                target_id_str = target_id_raw.decode('utf-8').strip("'\" ")
            else:
                target_id_str = str(target_id_raw).strip("'\" ")
            
            target_id = int(target_id_str)
        except Exception as e:
            print(f"🔴 Ошибка конвертации ID из базы ({target_id_raw}): {e}")
            continue
        
        target_channel = bot.get_channel(target_id)
        if not target_channel:
            try: 
                target_channel = await bot.fetch_channel(target_id)
            except Exception as e: 
                print(f"🔴 Дискорд не нашёл канал с ID {target_id}: {e}")
                continue

        files = [await a.to_file() for a in message.attachments]
        webhook = await get_target_webhook(target_channel)
        
        if webhook:
            try:
                guild_name = f" [{message.guild.name}]" if message.guild else ""
                await webhook.send(
                    content=message.content or None,
                    username=f"{message.author.display_name}{guild_name}",
                    avatar_url=message.author.display_avatar.url,
                    embeds=[discord.Embed.from_dict(e.to_dict()) for e in message.embeds],
                    files=files or discord.utils.MISSING
                )
            except Exception as e:
                print(f"🔴 Ошибка отправки вебхука в {target_id}: {e}")

    await bot.process_commands(message)

async def main():
    # Запуск веб-сервера aiohttp для прохождения проверок Render
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', 10000).start()
    
    print("🌐 HTTP-сервер запущен на порту 10000")
    await bot.start(TOKEN)

if __name__ == '__main__':
    asyncio.run(main())
