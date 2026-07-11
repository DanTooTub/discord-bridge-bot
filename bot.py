# Copyright (C) 2026 DanTooTub
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA

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

# ================= КОМАНДА: /bcreate =================
@bot.tree.command(name="bcreate", description="Создать мост (single) или инициализировать кросс-сеть (cross)")
@app_commands.choices(mode=[
    app_commands.Choice(name="single", value="single"),
    app_commands.Choice(name="cross", value="cross")
])
async def bcreate(interaction: discord.Interaction, mode: str, source: str = None, target: str = None, name: str = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ У вас должны быть права администратора!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    if mode == "single":
        if not source or not target:
            await interaction.followup.send("❌ Для режима single необходимо указать source и target!")
            return
        try:
            source_id = extract_id(source)
            target_id = extract_id(target)
        except ValueError:
            await interaction.followup.send("❌ Укажите корректные ID каналов.")
            return

        key = f"bridge:{source_id}"
        target_id_str = str(target_id)
        
        try:
            current_targets_raw = await redis.lrange(key, 0, -1) or []
            current_targets = [t.decode('utf-8') if isinstance(t, bytes) else str(t) for t in current_targets_raw]
            
            if target_id_str in current_targets:
                await interaction.followup.send("⚠️ Этот обычный мост уже существует!")
                return

            await redis.rpush(key, target_id_str)
            await interaction.followup.send(f"✅ Обычный мост создан! `{source_id}` -> `{target_id}`")
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка Redis: {e}")

    elif mode == "cross":
        if not name:
            await interaction.followup.send("❌ Для режима cross необходимо указать параметр name (имя моста)!")
            return
        
        safe_name = re.sub(r'[^a-zA-Z0-9_-]', '', name).lower()
        net_key = f"crossnet:{safe_name}"
        
        try:
            exists = await redis.exists(net_key)
            if exists:
                await interaction.followup.send(f"⚠️ Кросс-мост с именем `{safe_name}` уже существует!")
                return
                
            await redis.rpush(net_key, "INIT_MARKER")
            await interaction.followup.send(f"👑 Кросс-мост чатов `{safe_name}` успешно инициализирован! Используйте `/bconnect` для добавления каналов.")
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка создания кросс-моста: {e}")

# ================= КОМАНДА: /bconnect =================
@bot.tree.command(name="bconnect", description="Подключить канал к существующему кросс-мосту")
async def bconnect(interaction: discord.Interaction, bname: str, channel: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ У вас должны быть права администратора!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
        
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '', bname).lower()
    net_key = f"crossnet:{safe_name}"
    
    try:
        channel_id = extract_id(channel)
    except ValueError:
        await interaction.followup.send("❌ Укажите корректный ID канала.")
        return
        
    try:
        exists = await redis.exists(net_key)
        if not exists:
            await interaction.followup.send(f"❌ Кросс-моста с именем `{safe_name}` не существует! Сначала создайте его через `/bcreate`.")
            return
            
        channel_id_str = str(channel_id)
        current_channels_raw = await redis.lrange(net_key, 0, -1) or []
        current_channels = [c.decode('utf-8') if isinstance(c, bytes) else str(c) for c in current_channels_raw]
        
        if channel_id_str in current_channels:
            await interaction.followup.send(f"⚠️ Этот канал уже подключен к мосту `{safe_name}`!")
            return
            
        await redis.rpush(net_key, channel_id_str)
        await redis.set(f"channelnet:{channel_id_str}", safe_name)
        
        await interaction.followup.send(f"🔗 Канал `{channel_id}` успешно подключен к глобальному кросс-мосту `{safe_name}`!")
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка подключения: {e}")

# ================= ОБРАБОТКА СООБЩЕНИЙ =================
@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    if message.webhook_id and message.webhook_id in [wh.id for wh in cached_webhooks.values()]:
        return

    current_channel_id_str = str(message.channel.id)
    targets_to_send = set()

    # --- ЛОГИКА 1: Проверяем обычные мосты (Single) ---
    single_key = f"bridge:{current_channel_id_str}"
    single_targets_raw = await redis.lrange(single_key, 0, -1)
    if single_targets_raw:
        for t_raw in single_targets_raw:
            try:
                t_str = t_raw.decode('utf-8').strip("'\" ") if isinstance(t_raw, bytes) else str(t_raw).strip("'\" ")
                targets_to_send.add(int(t_str))
            except:
                continue

    # --- ЛОГИКА 2: Проверяем кросс-мосты (Cross) ---
    cross_net_name_raw = await redis.get(f"channelnet:{current_channel_id_str}")
    if cross_net_name_raw:
        cross_net_name = cross_net_name_raw.decode('utf-8') if isinstance(cross_net_name_raw, bytes) else str(cross_net_name_raw)
        cross_targets_raw = await redis.lrange(f"crossnet:{cross_net_name}", 0, -1) or []
        
        for t_raw in cross_targets_raw:
            try:
                t_str = t_raw.decode('utf-8').strip("'\" ") if isinstance(t_raw, bytes) else str(t_raw).strip("'\" ")
                if t_str == "INIT_MARKER":
                    continue
                t_id = int(t_str)
                if t_id != message.channel.id:
                    targets_to_send.add(t_id)
            except:
                continue

    # --- ОТПРАВКА СООБЩЕНИЙ ВО ВСЕ НАЙДЕННЫЕ ТАРГЕТЫ ---
    if targets_to_send:
        files = [await a.to_file() for a in message.attachments]
        
        for target_id in targets_to_send:
            target_channel = bot.get_channel(target_id)
            if not target_channel:
                try:
                    target_channel = await bot.fetch_channel(target_id)
                except:
                    continue

            webhook = await get_target_webhook(target_channel)
            if webhook:
                try:
                    guild_name = f" [{message.guild.name}]" if message.guild else ""
                    await webhook.send(
                        content=message.content or None,
                        username=f"{message.author.display_name}{guild_name}",
                        avatar_url=message.author.display_avatar.url if message.author.display_avatar else None,
                        embeds=[discord.Embed.from_dict(e.to_dict()) for e in message.embeds],
                        files=files or discord.utils.MISSING
                    )
                except Exception as e:
                    print(f"🔴 Ошибка отправки вебхука в {target_id}: {e}")

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
