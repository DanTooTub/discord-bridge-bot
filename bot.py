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
from typing import Optional
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from upstash_redis.asyncio import Redis
import aiohttp

# Импорты для веб-сайта
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

load_dotenv("variables.env")

# ================= НАСТРОЙКИ БАЗЫ ДАННЫХ =================
REDIS_URL = os.getenv("UPSTASH_REDIS_REST_URL")
REDIS_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
redis = Redis(url=REDIS_URL, token=REDIS_TOKEN)

# ================= НАСТРОЙКИ DISCORD БОТА =================
TOKEN = os.getenv("DISCORD_TOKEN")
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="b!", intents=intents)

# Логотип Telegram из Wikimedia Commons
TELEGRAM_LOGO_URL = "https://upload.wikimedia.org/wikipedia/commons/thumb/8/82/Telegram_logo.svg/512px-Telegram_logo.svg.png"

# ================= ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ОЧИСТКИ ТЕКСТА DISCORD =================
async def clean_discord_message(content: str, guild: discord.Guild) -> str:
    """Превращает сырые ID упоминаний Discord (<@ID>, <#ID>, <@&ID>, <:name:ID>) в читаемый текст"""
    if not content or not guild:
        return content

    def replace_user(match):
        user_id = int(match.group(1))
        member = guild.get_member(user_id)
        return f"@{member.display_name}" if member else f"@{match.group(1)}"
    content = re.sub(r'<@!?(\d+)>', replace_user, content)

    def replace_channel(match):
        channel_id = int(match.group(1))
        channel = guild.get_channel(channel_id)
        return f"#{channel.name}" if channel else f"#{match.group(1)}"
    content = re.sub(r'<#(\d+)>', replace_channel, content)

    def replace_role(match):
        role_id = int(match.group(1))
        role = guild.get_role(role_id)
        return f"@{role.name}" if role else f"@{match.group(1)}"
    content = re.sub(r'<@&(\d+)>', replace_role, content)

    content = re.sub(r'<a?:([a-zA-Z0-9_]+):\d+>', r':\1:', content)

    return content

# ================= ДИНАМИЧЕСКИЙ ДИСПЕТЧЕР TELEGRAM БОТОВ =================
class TelegramBotInstance:
    """Класс фонового поллинга для отдельного Telegram бота"""
    def __init__(self, bot_name: str, token: str):
        self.bot_name = bot_name
        self.token = token
        self.offset = 0
        self.running = False
        self.task = None

    async def start(self):
        self.running = True
        self.task = asyncio.create_task(self._poll_loop())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def _poll_loop(self):
        print(f"[TG {self.bot_name}] Поллинг Telegram запущен...")
        async with aiohttp.ClientSession() as session:
            while self.running:
                try:
                    url = f"https://api.telegram.org/bot{self.token}/getUpdates?offset={self.offset}&timeout=15"
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            await asyncio.sleep(5)
                            continue
                        
                        data = await resp.json()
                        if not data.get("ok"):
                            await asyncio.sleep(5)
                            continue

                        for update in data["result"]:
                            self.offset = update["update_id"] + 1
                            
                            if "message" in update:
                                await self._handle_message(session, update["message"])
                            elif "edited_message" in update:
                                await self._handle_edited_message(session, update["edited_message"])

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    print(f"[TG {self.bot_name}] Ошибка в цикле обновлений: {e}")
                    await asyncio.sleep(5)

    async def _handle_message(self, session: aiohttp.ClientSession, message: dict):
        chat_id = str(message["chat"]["id"])
        tg_msg_id = str(message["message_id"])
        text = message.get("text", "").strip()
        user = message.get("from", {})
        
        if not text or text.startswith("/"):
            return

        network_name_bytes = await redis.get(f"tg_chat_net:{chat_id}")
        if not network_name_bytes:
            return

        network_name = network_name_bytes.decode('utf-8') if isinstance(network_name_bytes, bytes) else str(network_name_bytes)
        
        cross_key = f"crossnet:{network_name}"
        channels_raw = await redis.lrange(cross_key, 0, -1) or []
        channels = [c.decode('utf-8') if isinstance(c, bytes) else str(c) for c in channels_raw]

        first_name = user.get("first_name", "")
        last_name = user.get("last_name", "")
        full_name = f"{first_name} {last_name}".strip() or "Telegram User"

        for cid in channels:
            chan = bot.get_channel(int(cid))
            if chan:
                try:
                    wh = await get_or_create_webhook(chan)
                    sent_msg = await wh.send(
                        content=text,
                        username=f"[TG] {full_name}",
                        avatar_url=TELEGRAM_LOGO_URL,
                        wait=True
                    )
                    
                    await redis.set(f"msg_map:tg:{chat_id}:{tg_msg_id}:{cid}", str(sent_msg.id), ex=86400)
                    await redis.set(f"msg_map:discord:{sent_msg.id}", f"{chat_id}:{tg_msg_id}", ex=86400)
                except Exception as e:
                    print(f"Ошибка трансляции TG -> Discord в канал {cid}: {e}")

    async def _handle_edited_message(self, session: aiohttp.ClientSession, message: dict):
        chat_id = str(message["chat"]["id"])
        tg_msg_id = str(message["message_id"])
        new_text = message.get("text", "").strip()

        if not new_text:
            return

        network_name_bytes = await redis.get(f"tg_chat_net:{chat_id}")
        if not network_name_bytes:
            return

        network_name = network_name_bytes.decode('utf-8') if isinstance(network_name_bytes, bytes) else str(network_name_bytes)
        cross_key = f"crossnet:{network_name}"
        channels_raw = await redis.lrange(cross_key, 0, -1) or []
        channels = [c.decode('utf-8') if isinstance(c, bytes) else str(c) for c in channels_raw]

        for cid in channels:
            d_msg_id_bytes = await redis.get(f"msg_map:tg:{chat_id}:{tg_msg_id}:{cid}")
            if d_msg_id_bytes:
                d_msg_id = int(d_msg_id_bytes.decode('utf-8') if isinstance(d_msg_id_bytes, bytes) else d_msg_id_bytes)
                chan = bot.get_channel(int(cid))
                if chan:
                    try:
                        wh = await get_or_create_webhook(chan)
                        await wh.edit_message(d_msg_id, content=new_text)
                    except Exception as e:
                        print(f"Ошибка редактирования сообщения в Discord: {e}")

class TelegramManager:
    """Управляющий класс для динамического запуска ботов"""
    def __init__(self):
        self.active_bots = {}

    async def init_all_bots(self):
        bot_names_raw = await redis.smembers("tg_bots_list") or []
        bot_names = [b.decode('utf-8') if isinstance(b, bytes) else str(b) for b in bot_names_raw]
        
        for name in bot_names:
            token_bytes = await redis.get(f"tg_token:{name}")
            if token_bytes:
                token = token_bytes.decode('utf-8') if isinstance(token_bytes, bytes) else str(token_bytes)
                await self.start_bot(name, token)

    async def start_bot(self, name: str, token: str):
        if name in self.active_bots:
            await self.active_bots[name].stop()
        
        instance = TelegramBotInstance(name, token)
        self.active_bots[name] = instance
        await instance.start()

    async def stop_all(self):
        for instance in self.active_bots.values():
            await instance.stop()
        self.active_bots.clear()

tg_manager = TelegramManager()

# ================= СОВМЕСТНЫЙ ЗАПУСК (LIFESPAN) =================
@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(bot.start(TOKEN))
    print("🤖 Discord Bot запущен!")
    await tg_manager.init_all_bots()
    yield
    print("🔌 Закрытие соединений...")
    await tg_manager.stop_all()
    await bot.close()
    await redis.close()

# ================= НАСТРОЙКА АБСОЛЮТНЫХ ПУТЕЙ =================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(lifespan=lifespan)

STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


# ================= ВЕБ-САЙТ И HEALTH-CHECK (ОБРАБОТКА GET И HEAD) =================
@app.get("/", response_class=HTMLResponse)
@app.head("/")
async def home(request: Request):
    try:
        bridge_keys = await redis.keys("bridge:*") or []
        cross_keys = await redis.keys("crossnet:*") or []
        total_bridges = len(bridge_keys) + len(cross_keys)
    except Exception:
        total_bridges = 0

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
@app.head("/ping")
async def ping():
    return {"status": "ok", "bot_ready": bot.is_ready()}


# ================= Вспомогательные функции для вебхуков =================
async def get_or_create_webhook(channel: discord.TextChannel) -> discord.Webhook:
    webhooks = await channel.webhooks()
    for wh in webhooks:
        if wh.name == "Bridge Webhook":
            await redis.sadd("our_webhooks", str(wh.id))
            return wh
    wh = await channel.create_webhook(name="Bridge Webhook")
    await redis.sadd("our_webhooks", str(wh.id))
    return wh

async def cache_all_existing_webhooks():
    """Собирает ID всех вебхуков нашего бота для точного фильтра зацикливания"""
    print("🔍 Кэширование ID наших вебхуков...")
    count = 0
    for guild in bot.guilds:
        for channel in guild.text_channels:
            try:
                if channel.permissions_for(guild.me).manage_webhooks:
                    webhooks = await channel.webhooks()
                    for wh in webhooks:
                        if wh.name == "Bridge Webhook":
                            await redis.sadd("our_webhooks", str(wh.id))
                            count += 1
            except Exception:
                continue
    print(f"✅ Закэшировано {count} наших вебхуков.")


# ================= СОБЫТИЯ И ИВЕНТЫ БОТА =================
@bot.event
async def on_ready():
    print(f"✅ Вошли как {bot.user} (ID: {bot.user.id})")
    await cache_all_existing_webhooks()
    try:
        synced = await bot.tree.sync()
        print(f"🔄 Синхронизировано {len(synced)} слэш-команд глобально.")
    except Exception as e:
        print(f"❌ Ошибка синхронизации команд: {e}")

@bot.event
async def on_message(message: discord.Message):
    # 1. Игнорируем своего собственного бота Discord
    if message.author.id == bot.user.id:
        return

    # 2. ТОЧНЫЙ ФИЛЬТР ВЕБХУКОВ:
    # Игнорируем ТОЛЬКО те вебхуки, которые создал НАШ бот (названы "Bridge Webhook")
    if message.webhook_id is not None:
        is_our_webhook = await redis.sismember("our_webhooks", str(message.webhook_id))
        if is_our_webhook:
            return  # Свои вебхуки сбрасываем (защита от петли)
        # Если это чужой вебхук (Вики-Бот) — код идет ДАЛЬШЕ и пересылает его!

    is_muted = await redis.exists(f"bridge_mute:{message.channel.id}")
    if is_muted:
        return

    channel_id_str = str(message.channel.id)

    # Очищаем текст от мусорных ID
    clean_text = await clean_discord_message(message.content or "", message.guild)

    # 1. Обработка Cross-сетей
    cross_keys_raw = await redis.keys("crossnet:*") or []
    cross_keys = [k.decode('utf-8') if isinstance(k, bytes) else str(k) for k in cross_keys_raw]

    for ck in cross_keys:
        channels_raw = await redis.lrange(ck, 0, -1) or []
        channels = [c.decode('utf-8') if isinstance(c, bytes) else str(c) for c in channels_raw]
        
        if channel_id_str in channels:
            network_name = ck.split(":")[-1]

            # А. Трансляция на другие Discord-каналы
            for target_id in channels:
                if target_id == channel_id_str:
                    continue
                
                if await redis.exists(f"bridge_mute:{target_id}"):
                    continue
                
                target_channel = bot.get_channel(int(target_id))
                if target_channel:
                    try:
                        wh = await get_or_create_webhook(target_channel)
                        sent_wh_msg = await wh.send(
                            content=clean_text,
                            username=f"{message.author.display_name} ({message.guild.name})",
                            avatar_url=message.author.display_avatar.url,
                            embeds=message.embeds,
                            files=[await f.to_file() for f in message.attachments] if message.attachments else [],
                            wait=True
                        )
                        await redis.set(f"d_rel:{message.id}:{target_id}", str(sent_wh_msg.id), ex=86400)
                    except Exception as e:
                        print(f"Ошибка пересылки crossnet в {target_id}: {e}")

            # Б. Трансляция в связанные группы Telegram
            tg_links_raw = await redis.smembers(f"tg_links:{network_name}") or []
            tg_links = [link.decode('utf-8') if isinstance(link, bytes) else str(link) for link in tg_links_raw]

            if tg_links and clean_text:
                async with aiohttp.ClientSession() as session:
                    payload_text = f"[{message.author.display_name} | {message.guild.name}]:\n{clean_text}"
                    for link in tg_links:
                        chat_id, bot_name = link.split(":")
                        token_bytes = await redis.get(f"tg_token:{bot_name}")
                        if token_bytes:
                            token = token_bytes.decode('utf-8') if isinstance(token_bytes, bytes) else str(token_bytes)
                            url = f"https://api.telegram.org/bot{token}/sendMessage"
                            try:
                                async with session.post(url, json={"chat_id": chat_id, "text": payload_text}) as resp:
                                    if resp.status == 200:
                                        tg_res = await resp.json()
                                        if tg_res.get("ok"):
                                            sent_tg_id = tg_res["result"]["message_id"]
                                            await redis.set(f"d2tg:{message.id}:{chat_id}", str(sent_tg_id), ex=86400)
                            except Exception as e:
                                print(f"Ошибка трансляции Discord -> TG в чат {chat_id}: {e}")

    # 2. Обработка Single-мостов
    bridge_key = f"bridge:{channel_id_str}"
    if await redis.exists(bridge_key):
        targets_raw = await redis.lrange(bridge_key, 0, -1) or []
        targets = [t.decode('utf-8') if isinstance(t, bytes) else str(t) for t in targets_raw]

        for target_id in targets:
            if target_id == "INIT_MARKER" or target_id == channel_id_str:
                continue
            
            if await redis.exists(f"bridge_mute:{target_id}"):
                continue

            target_channel = bot.get_channel(int(target_id))
            if target_channel:
                try:
                    wh = await get_or_create_webhook(target_channel)
                    sent_wh_msg = await wh.send(
                        content=clean_text,
                        username=message.author.display_name,
                        avatar_url=message.author.display_avatar.url,
                        embeds=message.embeds,
                        files=[await f.to_file() for f in message.attachments] if message.attachments else [],
                        wait=True
                    )
                    await redis.set(f"d_rel:{message.id}:{target_id}", str(sent_wh_msg.id), ex=86400)
                except Exception as e:
                    print(f"Ошибка пересылки single в {target_id}: {e}")

    await bot.process_commands(message)


# ================= СИНХРОНИЗАЦИЯ РЕДАКТИРОВАНИЯ DISCORD -> (DISCORD & TELEGRAM) =================
@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    if not payload.data.get("content"):
        return

    msg_id = payload.message_id
    raw_content = payload.data["content"]

    channel = bot.get_channel(payload.channel_id)
    guild = channel.guild if channel else None
    clean_text = await clean_discord_message(raw_content, guild)

    keys_raw = await redis.keys(f"d_rel:{msg_id}:*") or []
    for k in keys_raw:
        key_str = k.decode('utf-8') if isinstance(k, bytes) else str(k)
        target_channel_id = key_str.split(":")[-1]
        wh_msg_id_bytes = await redis.get(key_str)
        if wh_msg_id_bytes:
            wh_msg_id = int(wh_msg_id_bytes.decode('utf-8') if isinstance(wh_msg_id_bytes, bytes) else wh_msg_id_bytes)
            target_chan = bot.get_channel(int(target_channel_id))
            if target_chan:
                try:
                    wh = await get_or_create_webhook(target_chan)
                    await wh.edit_message(wh_msg_id, content=clean_text)
                except Exception as e:
                    print(f"Ошибка редактирования сообщения вебхука: {e}")

    tg_keys_raw = await redis.keys(f"d2tg:{msg_id}:*") or []
    if tg_keys_raw:
        async with aiohttp.ClientSession() as session:
            for k in tg_keys_raw:
                key_str = k.decode('utf-8') if isinstance(k, bytes) else str(key_str)
                chat_id = key_str.split(":")[-1]
                tg_msg_id_bytes = await redis.get(key_str)
                if tg_msg_id_bytes:
                    tg_msg_id = int(tg_msg_id_bytes.decode('utf-8') if isinstance(tg_msg_id_bytes, bytes) else tg_msg_id_bytes)
                    
                    network_name_bytes = await redis.get(f"tg_chat_net:{chat_id}")
                    if network_name_bytes:
                        net_name = network_name_bytes.decode('utf-8') if isinstance(network_name_bytes, bytes) else str(network_name_bytes)
                        tg_links_raw = await redis.smembers(f"tg_links:{net_name}") or []
                        for link in tg_links_raw:
                            link_str = link.decode('utf-8') if isinstance(link, bytes) else str(link)
                            c_id, b_name = link_str.split(":")
                            if c_id == chat_id:
                                token_bytes = await redis.get(f"tg_token:{b_name}")
                                if token_bytes:
                                    token = token_bytes.decode('utf-8') if isinstance(token_bytes, bytes) else str(token_bytes)
                                    author_name = payload.data.get("author", {}).get("username", "Discord User")
                                    guild_name = guild.name if guild else "Discord"
                                    
                                    new_payload = f"[{author_name} | {guild_name}]:\n{clean_text}"
                                    url = f"https://api.telegram.org/bot{token}/editMessageText"
                                    try:
                                        await session.post(url, json={"chat_id": chat_id, "message_id": tg_msg_id, "text": new_payload})
                                    except Exception as e:
                                        print(f"Ошибка редактирования сообщения в TG: {e}")


# ================= СИНХРОНИЗАЦИЯ УДАЛЕНИЯ DISCORD -> (DISCORD & TELEGRAM) =================
@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    msg_id = payload.message_id

    keys_raw = await redis.keys(f"d_rel:{msg_id}:*") or []
    for k in keys_raw:
        key_str = k.decode('utf-8') if isinstance(k, bytes) else str(k)
        target_channel_id = key_str.split(":")[-1]
        wh_msg_id_bytes = await redis.get(key_str)
        if wh_msg_id_bytes:
            wh_msg_id = int(wh_msg_id_bytes.decode('utf-8') if isinstance(wh_msg_id_bytes, bytes) else wh_msg_id_bytes)
            target_chan = bot.get_channel(int(target_channel_id))
            if target_chan:
                try:
                    wh = await get_or_create_webhook(target_chan)
                    await wh.delete_message(wh_msg_id)
                except Exception as e:
                    print(f"Ошибка удаления сообщения вебхука: {e}")
            await redis.delete(key_str)

    tg_keys_raw = await redis.keys(f"d2tg:{msg_id}:*") or []
    if tg_keys_raw:
        async with aiohttp.ClientSession() as session:
            for k in tg_keys_raw:
                key_str = k.decode('utf-8') if isinstance(k, bytes) else str(key_str)
                chat_id = key_str.split(":")[-1]
                tg_msg_id_bytes = await redis.get(key_str)
                if tg_msg_id_bytes:
                    tg_msg_id = int(tg_msg_id_bytes.decode('utf-8') if isinstance(tg_msg_id_bytes, bytes) else tg_msg_id_bytes)
                    
                    network_name_bytes = await redis.get(f"tg_chat_net:{chat_id}")
                    if network_name_bytes:
                        net_name = network_name_bytes.decode('utf-8') if isinstance(network_name_bytes, bytes) else str(network_name_bytes)
                        tg_links_raw = await redis.smembers(f"tg_links:{net_name}") or []
                        for link in tg_links_raw:
                            link_str = link.decode('utf-8') if isinstance(link, bytes) else str(link)
                            c_id, b_name = link_str.split(":")
                            if c_id == chat_id:
                                token_bytes = await redis.get(f"tg_token:{b_name}")
                                if token_bytes:
                                    token = token_bytes.decode('utf-8') if isinstance(token_bytes, bytes) else str(token_bytes)
                                    url = f"https://api.telegram.org/bot{token}/deleteMessage"
                                    try:
                                        await session.post(url, json={"chat_id": chat_id, "message_id": tg_msg_id})
                                    except Exception as e:
                                        print(f"Ошибка удаления сообщения в TG: {e}")
                    await redis.delete(key_str)


# ================= КОМАНДА: /bcreate =================
@bot.tree.command(name="bcreate", description="Создать новый мост или кросс-сеть")
@app_commands.describe(
    mode="Режим работы: single (мост один-к-многим) или cross (кросс-сеть)",
    name="Имя кросс-сети (не требуется для режима single)"
)
async def bcreate(interaction: discord.Interaction, mode: str, name: Optional[str] = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ У вас должны быть права администратора!", ephemeral=True)
        return

    if mode not in ["single", "cross"]:
        await interaction.response.send_message("❌ Выберите корректный режим: `single` или `cross`.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        if mode == "single":
            bridge_id = str(interaction.channel.id)
            meta_key = f"bridgemeta:{bridge_id}"
            bridge_key = f"bridge:{bridge_id}"
            
            await redis.set(meta_key, "single")
            await redis.rpush(bridge_key, "INIT_MARKER")
            await interaction.followup.send(f"✅ Single-мост успешно создан!\n🔑 **ID моста:** `{bridge_id}`\nИспользуйте его на другом сервере для привязки: `/bconnect name:{bridge_id}`")
        
        elif mode == "cross":
            if not name:
                await interaction.followup.send("❌ Для режима `cross` обязательно укажите аргумент `name`!")
                return

            clean_name = re.sub(r'[^a-zA-Z0-9_-]', '', name).lower()
            if not clean_name:
                await interaction.followup.send("❌ Недопустимое имя сети!")
                return
            
            meta_key = f"bridgemeta:{clean_name}"
            cross_key = f"crossnet:{clean_name}"

            if await redis.exists(meta_key):
                await interaction.followup.send(f"❌ Мост или сеть с именем `{clean_name}` уже существует!")
                return

            await redis.set(meta_key, "cross")
            await redis.rpush(cross_key, str(interaction.channel.id))
            await interaction.followup.send(f"✅ Cross-сеть `{clean_name}` создана!\nЭтот канал добавлен первым участником.\n🔑 **ID для подключения:** `{clean_name}`")

    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка создания моста: {e}")


# ================= КОМАНДА: /bconnect =================
@bot.tree.command(name="bconnect", description="Подключить канал к существующей сети или мосту")
@app_commands.describe(
    name="Имя вашей кросс-сети или ID single-моста",
    channel="Упоминание (#канал), ID или имя канала (если пусто, подключит текущий канал)"
)
async def bconnect(
    interaction: discord.Interaction, 
    name: str, 
    channel: Optional[str] = None
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ У вас должны быть права администратора!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        meta_key = f"bridgemeta:{name}"
        mode_raw = await redis.get(meta_key)

        if not mode_raw:
            await interaction.followup.send(f"❌ Сеть или мост `{name}` не найдены.")
            return

        mode = mode_raw.decode('utf-8') if isinstance(mode_raw, bytes) else str(mode_raw)
        
        if channel:
            clean_id = re.sub(r'[^0-9]', '', channel)
            if not clean_id:
                await interaction.followup.send("❌ Указан неверный формат канала!")
                return
            
            target_channel = bot.get_channel(int(clean_id))
            if not target_channel:
                try:
                    target_channel = await bot.fetch_channel(int(clean_id))
                except Exception:
                    pass
            
            if not target_channel:
                await interaction.followup.send("❌ Не удалось найти указанный канал!")
                return
        else:
            target_channel = interaction.channel

        channel_id_str = str(target_channel.id)

        if mode == "single":
            bridge_key = f"bridge:{name}"
            targets_raw = await redis.lrange(bridge_key, 0, -1) or []
            targets = [t.decode('utf-8') if isinstance(t, bytes) else str(t) for t in targets_raw]

            if channel_id_str in targets:
                await interaction.followup.send(f"❌ Канал {target_channel.mention} уже привязан к этому мосту!")
                return

            await redis.rpush(bridge_key, channel_id_str)
            await interaction.followup.send(f"🔗 Канал {target_channel.mention} успешно присоединен к мосту `{name}`!")

        elif mode == "cross":
            cross_key = f"crossnet:{name}"
            channels_raw = await redis.lrange(cross_key, 0, -1) or []
            channels = [c.decode('utf-8') if isinstance(c, bytes) else str(c) for c in channels_raw]

            if channel_id_str in channels:
                await interaction.followup.send(f"❌ Канал {target_channel.mention} уже находится в этой кросс-сети!")
                return

            await redis.rpush(cross_key, channel_id_str)
            await interaction.followup.send(f"🔗 Канал {target_channel.mention} успешно подключен к кросс-сети `{name}`!")

    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка подключения: {e}")


# ================= КОМАНДА: /btgbot (Регистрация TG-бота) =================
@bot.tree.command(name="btgbot", description="Зарегистрировать Telegram-бота")
@app_commands.describe(
    token="Токен бота, полученный от @BotFather",
    name="Уникальный текстовый идентификатор бота"
)
async def btgbot(interaction: discord.Interaction, token: str, name: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ У вас должны быть права администратора!", ephemeral=True)
        return

    name = re.sub(r'[^a-zA-Z0-9_-]', '', name).strip().lower()
    token = token.strip()

    if not name or not token:
        await interaction.response.send_message("❌ Укажите корректные параметры токена и имени бота!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        await redis.set(f"tg_token:{name}", token)
        await redis.sadd("tg_bots_list", name)
        
        await tg_manager.start_bot(name, token)

        await interaction.followup.send(f"✅ Telegram-бот `{name}` успешно зарегистрирован и запущен!")
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка регистрации Telegram-бота: {e}")


# ================= КОМАНДА: /btelegram (Связывание Discord -> TG) =================
@bot.tree.command(name="btelegram", description="Подключить группу Telegram к кросс-сети")
@app_commands.describe(
    name="Имя вашей кросс-сети",
    bot_name="Зарегистрированное имя Telegram-бота",
    chat_id="ID Telegram-чата (например, -1001234567890)"
)
async def btelegram(interaction: discord.Interaction, name: str, bot_name: str, chat_id: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ У вас должны быть права администратора!", ephemeral=True)
        return

    name = name.strip().lower()
    bot_name = bot_name.strip().lower()
    chat_id = chat_id.strip()

    await interaction.response.defer(ephemeral=True)

    try:
        meta_key = f"bridgemeta:{name}"
        mode_raw = await redis.get(meta_key)
        if not mode_raw or (mode_raw.decode('utf-8') if isinstance(mode_raw, bytes) else str(mode_raw)) != "cross":
            await interaction.followup.send(f"❌ Кросс-сеть `{name}` не найдена.")
            return

        token_bytes = await redis.get(f"tg_token:{bot_name}")
        if not token_bytes:
            await interaction.followup.send(f"❌ Зарегистрированный Telegram-бот `{bot_name}` не найден.")
            return
        token = token_bytes.decode('utf-8') if isinstance(token_bytes, bytes) else str(token_bytes)

        await redis.sadd(f"tg_links:{name}", f"{chat_id}:{bot_name}")
        await redis.set(f"tg_chat_net:{chat_id}", name)

        async with aiohttp.ClientSession() as session:
            test_msg = f"🎉 Бот успешно привязал этот чат к кросс-сети `{name}` через Discord!"
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            async with session.post(url, json={"chat_id": chat_id, "text": test_msg}) as resp:
                if resp.status == 200:
                    await interaction.followup.send(f"✅ Успешно! Группа Telegram (`{chat_id}`) связана с сетью `{name}`.")
                else:
                    await interaction.followup.send(f"⚠️ Связь сохранена, но бот не смог отправить сообщение в Telegram.")
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка связывания моста Telegram: {e}")


# ================= КОМАНДА: /brename =================
@bot.tree.command(name="brename", description="Переименовать существующую кросс-сеть")
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
        old_meta_key = f"bridgemeta:{old_name}"
        old_cross_key = f"crossnet:{old_name}"

        mode_raw = await redis.get(old_meta_key)
        if not mode_raw:
            await interaction.followup.send(f"❌ Кросс-сеть `{old_name}` не найдена.")
            return

        mode = mode_raw.decode('utf-8') if isinstance(mode_raw, bytes) else str(mode_raw)
        if mode != "cross":
            await interaction.followup.send("❌ Переименовать можно только сети в режиме `cross`!")
            return

        new_meta_key = f"bridgemeta:{new_name}"
        if await redis.exists(new_meta_key):
            await interaction.followup.send(f"❌ Имя `{new_name}` уже занято!")
            return

        new_cross_key = f"crossnet:{new_name}"
        
        await redis.rename(old_meta_key, new_meta_key)
        await redis.rename(old_cross_key, new_cross_key)

        if await redis.exists(f"tg_links:{old_name}"):
            await redis.rename(f"tg_links:{old_name}", f"tg_links:{new_name}")

        await interaction.followup.send(f"🎉 Кросс-сеть переименована из `{old_name}` в `{new_name}`!")

    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка переименования: {e}")


# ================= КОМАНДА: /bdelete =================
@bot.tree.command(name="bdelete", description="Удалить мост или отключить текущий канал от него")
async def bdelete(interaction: discord.Interaction, name: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ У вас должны быть права администратора!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        meta_key = f"bridgemeta:{name}"
        mode_raw = await redis.get(meta_key)

        if not mode_raw:
            await interaction.followup.send(f"❌ Сеть или мост `{name}` не найдены.")
            return

        mode = mode_raw.decode('utf-8') if isinstance(mode_raw, bytes) else str(mode_raw)
        channel_id_str = str(interaction.channel.id)

        if mode == "single":
            if name == channel_id_str:
                await redis.delete(f"bridge:{name}")
                await redis.delete(meta_key)
                await interaction.followup.send(f"🗑️ Single-мост `{name}` удален.")
            else:
                bridge_key = f"bridge:{name}"
                await redis.lrem(bridge_key, 0, channel_id_str)
                await interaction.followup.send(f"🔌 Канал отвязан от моста `{name}`.")

        elif mode == "cross":
            cross_key = f"crossnet:{name}"
            await redis.lrem(cross_key, 0, channel_id_str)
            
            remaining = await redis.llen(cross_key)
            if remaining == 0:
                await redis.delete(cross_key)
                await redis.delete(meta_key)
                await redis.delete(f"tg_links:{name}")
                await interaction.followup.send(f"🗑️ Кросс-сеть `{name}` полностью удалена.")
            else:
                await interaction.followup.send(f"🔌 Канал вышел из кросс-сети `{name}`.")

    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка удаления: {e}")


# ================= КОМАНДА: /blist =================
@bot.tree.command(name="blist", description="Показать список мостов этого сервера")
async def blist(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("❌ Ток на сервере!", ephemeral=True)
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Нужны права администратора!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        local_channel_ids = {str(ch.id) for ch in interaction.guild.text_channels}

        bridge_keys_raw = await redis.keys("bridge:*") or []
        bridge_keys = {k.decode('utf-8') if isinstance(k, bytes) else str(k) for k in bridge_keys_raw}

        meta_keys_raw = await redis.keys("bridgemeta:*") or []
        meta_keys = {k.decode('utf-8') if isinstance(k, bytes) else str(k) for k in meta_keys_raw}

        cross_keys_raw = await redis.keys("crossnet:*") or []
        cross_keys = {k.decode('utf-8') if isinstance(k, bytes) else str(k) for k in cross_keys_raw}

        embed = discord.Embed(
            title=f"🌐 Активные мосты сервера {interaction.guild.name}", 
            color=0x2ecc71
        )
        
        shown_count = 0

        async def resolve_channel_name(cid: str) -> str:
            if cid == "INIT_MARKER":
                return ""
            if cid in local_channel_ids:
                status_suffix = " 🔇 *(Muted)*" if await redis.exists(f"bridge_mute:{cid}") else ""
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

        async def format_channels(channel_ids_list):
            resolved = []
            for cid in channel_ids_list:
                name = await resolve_channel_name(cid)
                if name:
                    resolved.append(name)
            return "\n".join(resolved) if resolved else "*Нет подключенных каналов*"

        for bk in bridge_keys:
            source_id = bk.split(":")[-1]
            meta_key = f"bridgemeta:{source_id}"
            
            if meta_key in meta_keys:
                mode_raw = await redis.get(meta_key)
                mode = mode_raw.decode('utf-8') if isinstance(mode_raw, bytes) else str(mode_raw)
            else:
                mode = "single"

            if mode == "single":
                targets_raw = await redis.lrange(bk, 0, -1) or []
                targets = [t.decode('utf-8') if isinstance(t, bytes) else str(t) for t in targets_raw]
                
                is_local = (source_id in local_channel_ids) or any(t in local_channel_ids for t in targets)
                
                if is_local:
                    shown_count += 1
                    source_info = await resolve_channel_name(source_id)
                    targets_info = await format_channels(targets)
                    embed.add_field(
                        name=f"📢 Single-Мост ({source_info})",
                        value=f"➡️ Транслируется в:\n{targets_info}",
                        inline=False
                    )

        for ck in cross_keys:
            cross_name = ck.split(":")[-1]
            channels_raw = await redis.lrange(ck, 0, -1) or []
            channels = [c.decode('utf-8') if isinstance(c, bytes) else str(c) for c in channels_raw]
            
            is_local = any(c in local_channel_ids for c in channels)
            
            if is_local:
                shown_count += 1
                channels_info = await format_channels(channels)
                
                tg_links_raw = await redis.smembers(f"tg_links:{cross_name}") or []
                tg_links = [link.decode('utf-8') if isinstance(link, bytes) else str(link) for link in tg_links_raw]
                tg_info = ""
                if tg_links:
                    tg_info = "\n📱 **Telegram:**\n" + "\n".join([f"• Чат `{link.split(':')[0]}` (бот: `{link.split(':')[1]}`)" for link in tg_links])

                embed.add_field(
                    name=f"👑 Cross-сеть: `{cross_name}`",
                    value=f"🔗 Связанные каналы:\n{channels_info}{tg_info}",
                    inline=False
                )

        if shown_count == 0:
            await interaction.followup.send("📭 Активных мостов не найдено.")
        else:
            await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка получения списка: {e}")


# ================= АВТОЗАПУСК СЕРВЕРА С БОТОМ =================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=10000, reload=False)
