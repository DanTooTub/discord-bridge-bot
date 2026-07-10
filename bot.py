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
    print("❌ Ошибка: Убедись, что переменные окружения заданы!")
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
    await bot.tree.sync()

@bot.tree.command(name="bconnect", description="Связать каналы")
@app_commands.checks.has_permissions(administrator=True)
async def bconnect(interaction: discord.Interaction, source: discord.TextChannel, target: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    
    key = f"bridge:{source.id}"
    target_id_str = str(target.id)
    
    current_targets_raw = await redis.lrange(key, 0, -1)
    # Декодируем байты из Redis в строки
    current_targets = [t.decode('utf-8') if isinstance(t, bytes) else str(t) for t in current_targets_raw]
    
    if target_id_str in current_targets:
        await interaction.followup.send("⚠️ Мост уже существует!")
        return

    await redis.rpush(key, target_id_str)
    await interaction.followup.send(f"✅ Связано: {source.mention} -> {target.mention}")

@bot.tree.command(name="bdisconnect", description="Удалить связь")
@app_commands.checks.has_permissions(administrator=True)
async def bdisconnect(interaction: discord.Interaction, source: discord.TextChannel, target: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    
    key = f"bridge:{source.id}"
    target_id_str = str(target.id)
    
    await redis.lrem(key, 0, target_id_str)
    await redis.lrem(key, 0, target_id_str.encode('utf-8'))
    
    await interaction.followup.send("❌ Связь удалена.")

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
        try:
            # Исправленное декодирование байтов
            if isinstance(target_id_raw, bytes):
                target_id = int(target_id_raw.decode('utf-8'))
            else:
                target_id = int(target_id_raw)
        except (ValueError, TypeError):
            continue
        
        target_channel = bot.get_channel(target_id)
        if not target_channel:
            try: target_channel = await bot.fetch_channel(target_id)
            except: continue

        files = [await a.to_file() for a in message.attachments]
        webhook = await get_target_webhook(target_channel)
        
        if webhook:
            await webhook.send(
                content=message.content or None,
                username=f"{message.author.display_name} [{message.guild.name}]",
                avatar_url=message.author.display_avatar.url,
                embeds=[discord.Embed.from_dict(e.to_dict()) for e in message.embeds],
                files=files or discord.utils.MISSING
            )

    await bot.process_commands(message)

async def main():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', 10000).start()
    await bot.start(TOKEN)

if __name__ == '__main__':
    asyncio.run(main())
