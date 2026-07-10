import os
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import asyncio
import re
from aiohttp import web
from upstash_redis.asyncio import Redis

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(BASE_DIR, "variables.env")
load_dotenv(dotenv_path=env_path)

TOKEN = os.getenv("DISCORD_TOKEN")
REDIS_URL = os.getenv("UPSTASH_REDIS_REST_URL")
REDIS_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

if not TOKEN or not REDIS_URL or not REDIS_TOKEN:
    print("❌ Ошибка переменных окружения!")
    exit(1)

redis = Redis(url=REDIS_URL, token=REDIS_TOKEN)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

cached_webhooks = {}

def extract_id(channel_mention: str) -> int:
    match = re.search(r'\d+', channel_mention)
    if match:
        return int(match.group())
    raise ValueError("Формат ID неверный")

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
    return web.Response(text="Мост работает!")

@bot.event
async def on_ready():
    print(f"✅ Бот онлайн: {bot.user.name}")
    try:
        await bot.tree.sync()
        print("🔮 Команды синхронизированы!")
    except Exception as e:
        print(f"🔴 Ошибка синхронизации: {e}")

# МАКСИМАЛЬНО ГОЛАЯ КОМАНДА: Без describe и лишних проверок в декораторах
@bot.tree.command(name="bconnect")
async def bconnect(interaction: discord.Interaction, source: str, target: str):
    # Моментальный ответ Дискорду в первую же микросекунду
    await interaction.response.defer(ephemeral=True)
    
    # Ручная проверка прав, чтобы разгрузить Discord API
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ У вас должны быть права администратора!")
        return
    
    try:
        source_id = extract_id(source)
        target_id = extract_id(target)
    except ValueError:
        await interaction.followup.send("❌ Укажите корректные ID каналов цифрами.")
        return

    key = f"bridge:{source_id}"
    target_id_str = str(target_id)
    
    try:
        current_targets_raw = await redis.lrange(key, 0, -1) or []
        current_targets = [t.decode('utf-8') if isinstance(t, bytes) else str(t) for t in current_targets_raw]
        
        if target_id_str in current_targets:
            await interaction.followup.send("⚠️ Этот мост уже существует!")
            return

        await redis.rpush(key, target_id_str)
        await interaction.followup.send(f"✅ Успешно связан мост! `{source_id}` -> `{target_id}`")
    except Exception as e:
        print(f"🔴 Ошибка Redis: {e}")
        await interaction.followup.send("❌ База данных во Франкфурте легла или не ответила.")

@bot.tree.command(name="bdisconnect")
async def bdisconnect(interaction: discord.Interaction, source: str, target: str):
    await interaction.response.defer(ephemeral=True)
    
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ Нет прав.")
        return
        
    try:
        source_id = extract_id(source)
        target_id = extract_id(target)
    except ValueError:
        await interaction.followup.send("❌ Ошибка в ID.")
        return
        
    key = f"bridge:{source_id}"
    target_id_str = str(target_id)
    
    try:
        await redis.lrem(key, 0, target_id_str)
        await redis.lrem(key, 0, target_id_str.encode('utf-8'))
        await interaction.followup.send("❌ Мост удален.")
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка: {e}")

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user or (message.webhook_id and message.webhook_id in [wh.id for wh in cached_webhooks.values()]):
        return

    key = f"bridge:{message.channel.id}"
    try:
        target_channels_data = await redis.lrange(key, 0, -1)
    except:
        return

    if not target_channels_data:
        return

    for target_id_raw in target_channels_data:
        try:
            if isinstance(target_id_raw, bytes):
                target_id = int(target_id_raw.decode('utf-8').strip("'\" "))
            else:
                target_id = int(target_id_raw)
        except:
            continue
        
        target_channel = bot.get_channel(target_id)
        if not target_channel:
            try: target_channel = await bot.fetch_channel(target_id)
            except: continue

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
                print(f"🔴 Ошибка вебхука: {e}")

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
