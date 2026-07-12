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
import sqlitecloud

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(BASE_DIR, "variables.env")
load_dotenv(dotenv_path=env_path)

TOKEN = os.getenv("DISCORD_TOKEN")
DB_CONN_STR = os.getenv("SQLITE_CLOUD_CONNECTION_STRING")

if not TOKEN or not DB_CONN_STR:
    print("❌ Ошибка переменных окружения!")
    exit(1)

# Инициализируем подключение к SQLite Cloud
db = sqlitecloud.connect(DB_CONN_STR)
cursor = db.cursor()

# Создаем таблицы, если их нет (для сохранения структуры "не сломать что есть")
cursor.execute("""
CREATE TABLE IF NOT EXISTS bridges (
    bridge_id TEXT PRIMARY KEY,
    mode TEXT NOT EXISTS CHECK (mode IN ('single', 'cross'))
);
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS bridge_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bridge_id TEXT,
    channel_id TEXT,
    FOREIGN KEY(bridge_id) REFERENCES bridges(bridge_id) ON DELETE CASCADE
);
""")
db.commit()

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
    return web.Response(text="Мост на SQLite Cloud работает!")

@bot.event
async def on_ready():
    print(f"✅ Бот онлайн (SQLite Cloud): {bot.user.name}")
    try:
        await bot.tree.sync()
        print("🔮 Команды синхронизированы!")
    except Exception as e:
        print(f"🔴 Ошибка синхронизации: {e}")

# ================= КОМАНДА: /bcreate =================
@bot.tree.command(name="bcreate", description="Создать мост трансляции (single) или глобальную сеть чатов (cross)")
@app_commands.choices(mode=[
    app_commands.Choice(name="single", value="single"),
    app_commands.Choice(name="cross", value="cross")
])
async def bcreate(interaction: discord.Interaction, mode: str, source: str = None, name: str = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ У вас должны быть права администратора!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    bridge_id = str(extract_id(source)) if mode == "single" and source else re.sub(r'[^a-zA-Z0-9_-]', '', name or "").lower()

    if mode == "single" and not source:
        await interaction.followup.send("❌ Для режима single необходимо указать source (ID канала-источника)!")
        return
    if mode == "cross" and not name:
        await interaction.followup.send("❌ Для режима cross необходимо указать параметр name (имя моста)!")
        return

    try:
        cursor.execute("SELECT 1 FROM bridges WHERE bridge_id = ?", (bridge_id,))
        if cursor.fetchone():
            await interaction.followup.send(f"⚠️ Мост `{bridge_id}` уже существует!")
            return

        cursor.execute("INSERT INTO bridges (bridge_id, mode) VALUES (?, ?)", (bridge_id, mode))
        db.commit()
        
        msg = f"📈 Мост создан! Вызовите `/bconnect bname:{bridge_id}`" if mode == "single" else f"👑 Кросс-мост `{bridge_id}` создан!"
        await interaction.followup.send(msg)
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка БД: {e}")

# ================= КОМАНДА: /bconnect =================
@bot.tree.command(name="bconnect", description="Подключить канал к мосту")
async def bconnect(interaction: discord.Interaction, bname: str, channel: str = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ У вас должны быть права администратора!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    safe_bname = re.sub(r'[^a-zA-Z0-9_-]', '', bname).lower()
    target_channel_id_str = str(extract_id(channel)) if channel else str(interaction.channel_id)

    try:
        cursor.execute("SELECT mode FROM bridges WHERE bridge_id = ?", (safe_bname,))
        res = cursor.fetchone()
        if not res:
            await interaction.followup.send(f"❌ Моста со значением `{safe_bname}` не существует!")
            return
        
        mode = res[0]

        cursor.execute("SELECT 1 FROM bridge_channels WHERE bridge_id = ? AND channel_id = ?", (safe_bname, target_channel_id_str))
        if cursor.fetchone():
            await interaction.followup.send(f"⚠️ Канал <#{target_channel_id_str}> уже подключен к `{safe_bname}`!")
            return

        cursor.execute("INSERT INTO bridge_channels (bridge_id, channel_id) VALUES (?, ?)", (safe_bname, target_channel_id_str))
        db.commit()
        
        await interaction.followup.send(f"✅ Канал <#{target_channel_id_str}> успешно подключен к мосту `{safe_bname}` ({mode})!")
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка подключения: {e}")

# ================= КОМАНДА: /bdelete =================
@bot.tree.command(name="bdelete", description="Полностью удалить мост")
async def bdelete(interaction: discord.Interaction, name: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ У вас должны быть права администратора!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '', name).lower()

    try:
        cursor.execute("SELECT 1 FROM bridges WHERE bridge_id = ?", (safe_name,))
        if not cursor.fetchone():
            await interaction.followup.send(f"❌ Моста `{safe_name}` не существует.")
            return

        # Каскадное удаление (благодаря FOREIGN KEY ... ON DELETE CASCADE) зачистит и каналы
        cursor.execute("DELETE FROM bridges WHERE bridge_id = ?", (safe_name,))
        db.commit()
        await interaction.followup.send(f"🗑️ Мост `{safe_name}` успешно удален!")
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка удаления: {e}")

# ================= ОБРАБОТКА СООБЩЕНИЙ =================
@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user or (message.webhook_id and message.webhook_id in [wh.id for wh in cached_webhooks.values()]):
        return

    current_channel_id_str = str(message.channel.id)
    targets_to_send = set()

    try:
        # 1. Проверяем Single-мосты (где текущий канал является именем моста, т.е. источником)
        cursor.execute("SELECT mode FROM bridges WHERE bridge_id = ?", (current_channel_id_str,))
        bridge_res = cursor.fetchone()
        if bridge_res and bridge_res[0] == "single":
            cursor.execute("SELECT channel_id FROM bridge_channels WHERE bridge_id = ?", (current_channel_id_str,))
            for row in cursor.fetchall():
                targets_to_send.add(int(row[0]))

        # 2. Проверяем Cross-мосты (ищем cross-мосты, к которым привязан этот канал)
        cursor.execute("""
            SELECT bc.bridge_id FROM bridge_channels bc
            JOIN bridges b ON bc.bridge_id = b.bridge_id
            WHERE bc.channel_id = ? AND b.mode = 'cross'
        """, (current_channel_id_str,))
        
        cross_bridges = [row[0] for row in cursor.fetchall()]
        for b_id in cross_bridges:
            cursor.execute("SELECT channel_id FROM bridge_channels WHERE bridge_id = ?", (b_id,))
            for row in cursor.fetchall():
                t_id = int(row[0])
                if t_id != message.channel.id:
                    targets_to_send.add(t_id)
                    
    except Exception as e:
        print(f"🔴 Ошибка запроса к SQLite Cloud: {e}")

    # --- ОТПРАВКА СООБЩЕНИЙ ВО ВСЕ НАЙДЕННЫЕ ТАРГЕТЫ ---
    if targets_to_send:
        files = [await a.to_file() for a in message.attachments]
        for target_id in targets_to_send:
            target_channel = bot.get_channel(target_id) or await bot.fetch_channel(target_id)
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
