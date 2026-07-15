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
import asyncio
import re
from contextlib import asynccontextmanager
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import sqlitecloud

# Импорты для веб-сайта
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

load_dotenv("variables.env")

# ================= НАСТРОЙКИ БАЗЫ ДАННЫХ =================
# Подключение к SQLite Cloud
SQLITE_CLOUD_URL = os.getenv("SQLITE_CLOUD_URL")

def get_db_conn():
    conn = sqlitecloud.connect(SQLITE_CLOUD_URL)
    return conn

# Инициализируем таблицы базы данных, если их нет
def init_database():
    conn = get_db_conn()
    cursor = conn.cursor()
    # Таблица мета-информации о мостах
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bridges (
        name TEXT PRIMARY KEY,
        mode TEXT NOT NULL,
        creator_channel_id TEXT NOT NULL
    );
    """)
    # Таблица связей каналов (для мостов и кросс-сетей)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bridge_channels (
        bridge_name TEXT,
        channel_id TEXT,
        PRIMARY KEY (bridge_name, channel_id),
        FOREIGN KEY (bridge_name) REFERENCES bridges(name) ON DELETE CASCADE
    );
    """)
    # Таблица приглушенных (muted) каналов
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bridge_mutes (
        channel_id TEXT PRIMARY KEY
    );
    """)
    conn.commit()
    conn.close()

init_database()

# ================= НАСТРОЙКИ DISCORD БОТА =================
TOKEN = os.getenv("DISCORD_TOKEN")
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="b!", intents=intents)

# ================= СОВМЕСТНЫЙ ЗАПУСК (LIFESPAN) =================
@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(bot.start(TOKEN))
    print("🤖 Discord Bot (SQLite Cloud) запущен в фоновом режиме!")
    yield
    print("🔌 Закрытие соединения с Discord...")
    await bot.close()

# ================= НАСТРОЙКА АБСОЛЮТНЫХ ПУТЕЙ =================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(lifespan=lifespan)

STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


# ================= ВЕБ-САЙТ: МАРШРУТЫ (ROUTES) =================
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    total_bridges = 0
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM bridges;")
        total_bridges = cursor.fetchone()[0]
        conn.close()
    except Exception:
        pass

    guilds_count = len(bot.guilds) if bot.is_ready() else 0

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "guilds_count": guilds_count,
            "total_bridges": total_bridges,
            "bot_latency": round(bot.latency * 1000) if bot.is_ready() else 0
        }
    )

@app.get("/ping")
async def ping():
    return {"status": "ok", "bot_ready": bot.is_ready()}


# ================= Вспомогательные функции для вебхуков =================
async def get_or_create_webhook(channel: discord.TextChannel) -> discord.Webhook:
    webhooks = await channel.webhooks()
    for wh in webhooks:
        if wh.name == "Bridge Webhook":
            return wh
    return await channel.create_webhook(name="Bridge Webhook")


# ================= СОБЫТИЯ И ИВЕНТЫ БОТА =================
@bot.event
async def on_ready():
    print(f"✅ Вошли как {bot.user} (ID: {bot.user.id}) в ветке SQLite Cloud")
    try:
        synced = await bot.tree.sync()
        print(f"🔄 Синхронизировано {len(synced)} слэш-команд.")
    except Exception as e:
        print(f"❌ Ошибка синхронизации: {e}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.id == bot.user.id:
        return

    # Защита от бесконечного цикла отправки вебхуков
    if message.webhook_id:
        try:
            if message.author.name == "Bridge Webhook" or (message.author.discriminator == "0000" and "Bridge Webhook" in message.author.name):
                return
        except Exception:
            pass

    conn = get_db_conn()
    cursor = conn.cursor()

    # Проверяем, приглушен ли текущий канал
    cursor.execute("SELECT 1 FROM bridge_mutes WHERE channel_id = ?;", (str(message.channel.id),))
    if cursor.fetchone():
        conn.close()
        return

    # Нам нужно найти все мосты (single или cross), в которых участвует текущий канал
    cursor.execute("""
        SELECT bridge_name, mode FROM bridge_channels 
        JOIN bridges ON bridges.name = bridge_channels.bridge_name
        WHERE channel_id = ?;
    """, (str(message.channel.id),))
    
    active_connections = cursor.fetchall()

    for bridge_name, mode in active_connections:
        # Находим всех получателей в этом мосте
        cursor.execute("SELECT channel_id FROM bridge_channels WHERE bridge_name = ?;", (bridge_name,))
        targets = [row[0] for row in cursor.fetchall()]

        for target_id in targets:
            # Не отправляем сообщение в тот же канал, откуда оно пришло
            if target_id == str(message.channel.id):
                continue

            # Проверяем, не приглушен ли получатель
            cursor.execute("SELECT 1 FROM bridge_mutes WHERE channel_id = ?;", (target_id,))
            if cursor.fetchone():
                continue

            target_channel = bot.get_channel(int(target_id))
            if target_channel:
                try:
                    wh = await get_or_create_webhook(target_channel)
                    username_suffix = f" ({message.guild.name})" if mode == "cross" else ""
                    await wh.send(
                        content=message.content or "",
                        username=f"{message.author.display_name}{username_suffix}",
                        avatar_url=message.author.display_avatar.url,
                        embeds=message.embeds,
                        files=[await f.to_file() for f in message.attachments] if message.attachments else []
                    )
                except Exception as e:
                    print(f"Ошибка трансляции в {target_id}: {e}")

    conn.close()
    await bot.process_commands(message)


# ================= КОМАНДА: /bcreate =================
@bot.tree.command(name="bcreate", description="Создать новый мост или кросс-сеть")
async def bcreate(interaction: discord.Interaction, name: str, mode: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ У вас должны быть права администратора!", ephemeral=True)
        return

    if mode not in ["single", "cross"]:
        await interaction.response.send_message("❌ Неверный режим!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        conn = get_db_conn()
        cursor = conn.cursor()

        clean_name = re.sub(r'[^a-zA-Z0-9_-]', '', name).lower() if mode == "cross" else str(interaction.channel.id)

        # Проверяем, существует ли мост
        cursor.execute("SELECT 1 FROM bridges WHERE name = ?;", (clean_name,))
        if cursor.fetchone():
            await interaction.followup.send(f"❌ Мост с идентификатором `{clean_name}` уже существует.")
            conn.close()
            return

        # Добавляем запись в таблицу мостов
        cursor.execute("""
            INSERT INTO bridges (name, mode, creator_channel_id) 
            VALUES (?, ?, ?);
        """, (clean_name, mode, str(interaction.channel.id)))

        # Добавляем первый канал
        cursor.execute("""
            INSERT INTO bridge_channels (bridge_name, channel_id) 
            VALUES (?, ?);
        """, (clean_name, str(interaction.channel.id)))

        conn.commit()
        conn.close()

        if mode == "single":
            await interaction.followup.send(f"✅ Single-мост успешно создан!\n🔑 **ID моста:** `{clean_name}`\nПодключите его на другом сервере: `/bconnect name:{clean_name}`")
        else:
            await interaction.followup.send(f"✅ Cross-сеть `{clean_name}` создана!\n🔑 **ID для подключения:** `{clean_name}`")

    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка базы данных: {e}")


# ================= КОМАНДА: /bconnect =================
@bot.tree.command(name="bconnect", description="Подключить текущий канал к существующей сети")
async def bconnect(interaction: discord.Interaction, name: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ У вас должны быть права администратора!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        conn = get_db_conn()
        cursor = conn.cursor()

        # Проверяем существование моста
        cursor.execute("SELECT mode FROM bridges WHERE name = ?;", (name,))
        res = cursor.fetchone()

        if not res:
            await interaction.followup.send(f"❌ Сеть или мост `{name}` не найдены.")
            conn.close()
            return

        mode = res[0]
        channel_id_str = str(interaction.channel.id)

        # Проверяем, не подключен ли уже канал к этому мосту
        cursor.execute("SELECT 1 FROM bridge_channels WHERE bridge_name = ? AND channel_id = ?;", (name, channel_id_str))
        if cursor.fetchone():
            await interaction.followup.send("❌ Этот канал уже привязан к этому мосту!")
            conn.close()
            return

        # Добавляем связь
        cursor.execute("INSERT INTO bridge_channels (bridge_name, channel_id) VALUES (?, ?);", (name, channel_id_str))
        conn.commit()
        conn.close()

        await interaction.followup.send(f"🔗 Канал успешно присоединен к мосту `{name}`!")

    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка подключения: {e}")


# ================= КОМАНДА: /brename (SQLite Cloud) =================
@bot.tree.command(name="brename", description="Переименовать существующую кросс-сеть")
@app_commands.describe(
    old_name="Текущее уникальное имя вашей кросс-сети",
    new_name="Новое имя для кросс-сети"
)
async def brename(interaction: discord.Interaction, old_name: str, new_name: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ У вас должны быть права администратора!", ephemeral=True)
        return

    old_name = old_name.strip().lower()
    new_name = re.sub(r'[^a-zA-Z0-9_-]', '', new_name).strip().lower()

    if not new_name:
        await interaction.response.send_message("❌ Новое имя содержит недопустимые символы!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        conn = get_db_conn()
        cursor = conn.cursor()

        # 1. Получаем информацию о мосте и создателе
        cursor.execute("SELECT mode, creator_channel_id FROM bridges WHERE name = ?;", (old_name,))
        res = cursor.fetchone()

        if not res:
            await interaction.followup.send(f"❌ Кросс-сеть `{old_name}` не найдена.")
            conn.close()
            return

        mode, creator_channel_id = res
        if mode != "cross":
            await interaction.followup.send("❌ Вы можете переименовать только кросс-сети!")
            conn.close()
            return

        # 2. Проверяем владельца (принадлежит ли изначальный канал текущему серверу)
        creator_channel = interaction.guild.get_channel(int(creator_channel_id))
        if not creator_channel:
            await interaction.followup.send("❌ Переименовать сеть может только администратор сервера, на котором она была создана!")
            conn.close()
            return

        # 3. Проверяем, не занято ли новое имя
        cursor.execute("SELECT 1 FROM bridges WHERE name = ?;", (new_name,))
        if cursor.fetchone():
            await interaction.followup.send(f"❌ Имя `{new_name}` уже занято другой сетью или мостом.")
            conn.close()
            return

        # 4. Обновляем имя через транзакцию (включая связанные каналы)
        # Отключаем внешние ключи на время ручного каскадного обновления, если нужно, или делаем атомарный UPDATE
        cursor.execute("UPDATE bridges SET name = ? WHERE name = ?;", (new_name, old_name))
        
        conn.commit()
        conn.close()

        await interaction.followup.send(f"🎉 Кросс-сеть успешно переименована из `{old_name}` в `{new_name}`!")

    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка в процессе переименования: {e}")


# ================= КОМАНДА: /bdelete =================
@bot.tree.command(name="bdelete", description="Удалить мост или отключить текущий канал от него")
async def bdelete(interaction: discord.Interaction, name: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ У вас должны быть права администратора!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        conn = get_db_conn()
        cursor = conn.cursor()

        cursor.execute("SELECT mode, creator_channel_id FROM bridges WHERE name = ?;", (name,))
        res = cursor.fetchone()

        if not res:
            await interaction.followup.send(f"❌ Сеть или мост `{name}` не найдены.")
            conn.close()
            return

        mode, creator_channel_id = res
        channel_id_str = str(interaction.channel.id)

        if mode == "single":
            if name == channel_id_str:
                # Владелец удаляет мост полностью
                cursor.execute("DELETE FROM bridges WHERE name = ?;", (name,))
                await interaction.followup.send(f"🗑️ Single-мост `{name}` полностью удален из базы данных.")
            else:
                cursor.execute("DELETE FROM bridge_channels WHERE bridge_name = ? AND channel_id = ?;", (name, channel_id_str))
                await interaction.followup.send(f"🔌 Канал успешно отвязан от моста `{name}`.")

        elif mode == "cross":
            cursor.execute("DELETE FROM bridge_channels WHERE bridge_name = ? AND channel_id = ?;", (name, channel_id_str))
            
            # Проверяем, остались ли каналы в кросс-сети
            cursor.execute("SELECT COUNT(*) FROM bridge_channels WHERE bridge_name = ?;", (name,))
            remaining = cursor.fetchone()[0]

            if remaining == 0:
                cursor.execute("DELETE FROM bridges WHERE name = ?;", (name,))
                await interaction.followup.send(f"🗑️ Кросс-сеть `{name}` опустела и была полностью удалена.")
            else:
                await interaction.followup.send(f"🔌 Этот канал успешно вышел из кросс-сети `{name}`.")

        conn.commit()
        conn.close()

    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка при удалении: {e}")


# ================= КОМАНДА: /blist =================
@bot.tree.command(name="blist", description="Показать список мостов, связанных с каналами этого сервера")
async def blist(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("❌ Эту команду можно использовать только на сервере!", ephemeral=True)
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ У вас должны быть права администратора!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        local_channel_ids = {str(ch.id) for ch in interaction.guild.text_channels}
        conn = get_db_conn()
        cursor = conn.cursor()

        # Выбираем все каналы мостов
        cursor.execute("""
            SELECT bridges.name, bridges.mode, bridge_channels.channel_id 
            FROM bridges
            JOIN bridge_channels ON bridges.name = bridge_channels.bridge_name;
        """)
        rows = cursor.fetchall()

        # Группируем каналы по названию моста
        bridge_data = {}
        for b_name, b_mode, c_id in rows:
            if b_name not in bridge_data:
                bridge_data[b_name] = {"mode": b_mode, "channels": []}
            bridge_data[b_name]["channels"].append(c_id)

        embed = discord.Embed(
            title=f"🌐 Активные связи мостов для сервера {interaction.guild.name}", 
            color=0x2ecc71
        )
        
        shown_count = 0

        async def resolve_channel_name(cid: str) -> str:
            if cid in local_channel_ids:
                # Проверяем, не приглушен ли канал
                cursor.execute("SELECT 1 FROM bridge_mutes WHERE channel_id = ?;", (cid,))
                status_suffix = " 🔇 *(Muted)*" if cursor.fetchone() else ""
                return f"<#{cid}>{status_suffix}"
            try:
                target_id = int(cid)
                channel = bot.get_channel(target_id)
                if not channel:
                    channel = await bot.fetch_channel(target_id)
                if channel and isinstance(channel, discord.abc.GuildChannel):
                    return f"🌐 **{channel.guild.name}** > #{channel.name}"
            except Exception:
                pass
            return f"❓ Неизвестный сервер (ID: {cid})"

        for b_name, data in bridge_data.items():
            b_mode = data["mode"]
            channels = data["channels"]

            # Проверяем, имеет ли текущий сервер отношение к этому мосту
            is_local = (b_name in local_channel_ids) or any(c in local_channel_ids for c in channels)

            if is_local:
                shown_count += 1
                resolved_channels = []
                for cid in channels:
                    name_resolved = await resolve_channel_name(cid)
                    if name_resolved:
                        resolved_channels.append(name_resolved)

                channels_info = "\n".join(resolved_channels) if resolved_channels else "*Нет подключенных каналов*"

                if b_mode == "single":
                    source_info = await resolve_channel_name(b_name)
                    embed.add_field(
                        name=f"📢 Single-Мост (Источник: {source_info})",
                        value=f"➡️ ... транслируется в:\n{channels_info}",
                        inline=False
                    )
                else:
                    embed.add_field(
                        name=f"👑 Cross-сеть: `{b_name}`",
                        value=f"🔗 Связанные каналы:\n{channels_info}",
                        inline=False
                    )

        conn.close()

        if shown_count == 0:
            await interaction.followup.send("📭 На этом сервере не найдено активных мостов.")
        else:
            await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка получения списка: {e}")


# ================= АВТОЗАПУСК СЕРВЕРА С БОТОМ =================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot_sqlite_cloud:app", host="0.0.0.0", port=10000, reload=False)
