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
from fastapi import FastAPI, Request, Response
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

    async def _get_avatar_url(self, session: aiohttp.ClientSession, user_id: int) -> str:
        """Получение аватарки пользователя Telegram для отображения в Discord вебхуке"""
        try:
            async with session.get(f"https://api.telegram.org/bot{self.token}/getUserProfilePhotos?user_id={user_id}&limit=1") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("ok") and data["result"]["total_count"] > 0:
                        file_id = data["result"]["photos"][0][0]["file_id"]
                        async with session.get(f"https://api.telegram.org/bot{self.token}/getFile?file_id={file_id}") as file_resp:
                            if file_resp.status == 200:
                                file_data = await file_resp.json()
                                if file_data.get("ok"):
                                    file_path = file_data["result"]["file_path"]
                                    return f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        except Exception as e:
            print(f"[TG {self.bot_name}] Не удалось получить аватар для user_id {user_id}: {e}")
        return "https://cdn.discordapp.com/embed/avatars/0.png"

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
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    print(f"[TG {self.bot_name}] Ошибка в цикле обновлений: {e}")
                    await asyncio.sleep(5)

    async def _handle_message(self, session: aiohttp.ClientSession, message: dict):
        chat_id = str(message["chat"]["id"])
        text = message.get("text", "").strip()
        user = message.get("from", {})
        topic_id = str(message.get("message_thread_id", ""))
        
        if not text or text.startswith("/"):
            return  # Игнорируем команды и пустые системные сообщения

        # А. Проверяем, является ли сообщение частью Topic-моста (прямая связь Discord -> Топик TG)
        if topic_id:
            tg_key = f"tg_topic_tg_set:{chat_id}:{topic_id}"
            discord_channels_raw = await redis.smembers(tg_key) or []
            discord_channels = [c.decode('utf-8') if isinstance(c, bytes) else str(c) for c in discord_channels_raw]
            
            if discord_channels:
                first_name = user.get("first_name", "")
                last_name = user.get("last_name", "")
                full_name = f"{first_name} {last_name}".strip() or "Telegram User"
                avatar_url = await self._get_avatar_url(session, user.get("id", 0))
                
                for cid in discord_channels:
                    chan = bot.get_channel(int(cid))
                    if chan:
                        try:
                            wh = await get_or_create_webhook(chan)
                            await wh.send(
                                content=text,
                                username=f"[TG | Тема] {full_name}",
                                avatar_url=avatar_url
                            )
                        except Exception as e:
                            print(f"Ошибка трансляции TG Topic -> Discord в канал {cid}: {e}")
                return  # Завершаем, чтобы не дублировать сообщения в другие мосты

        # Б. Проверяем стандартные Cross-сети
        network_name_bytes = await redis.get(f"tg_chat_net:{chat_id}")
        if not network_name_bytes:
            return  # Чат не подключен к классическим мостам

        network_name = network_name_bytes.decode('utf-8') if isinstance(network_name_bytes, bytes) else str(network_name_bytes)
        
        # Получаем каналы Discord, входящие в эту кросс-сеть
        cross_key = f"crossnet:{network_name}"
        channels_raw = await redis.lrange(cross_key, 0, -1) or []
        channels = [c.decode('utf-8') if isinstance(c, bytes) else str(c) for c in channels_raw]

        first_name = user.get("first_name", "")
        last_name = user.get("last_name", "")
        full_name = f"{first_name} {last_name}".strip() or "Telegram User"
        avatar_url = await self._get_avatar_url(session, user.get("id", 0))

        # Транслируем сообщение во все Discord-каналы сети
        for cid in channels:
            chan = bot.get_channel(int(cid))
            if chan:
                try:
                    wh = await get_or_create_webhook(chan)
                    await wh.send(
                        content=text,
                        username=f"[TG] {full_name}",
                        avatar_url=avatar_url
                    )
                except Exception as e:
                    print(f"Ошибка трансляции TG -> Discord в канал {cid}: {e}")

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
    # Запускаем Discord бота
    asyncio.create_task(bot.start(TOKEN))
    print("🤖 Discord Bot (Redis) запущен в фоновом режиме!")
    # Инициализируем фоновые Telegram-боты
    await tg_manager.init_all_bots()
    yield
    print("🔌 Закрытие соединения с Discord, Telegram и Redis...")
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


# ================= ВЕБ-САЙТ: МАРШРУТЫ (ROUTES) =================

@app.head("/")
async def home_head():
    """Ответ на HEAD-запросы от UptimeRobot для предотвращения засыпания и 405 ошибок"""
    return Response(status_code=200)

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    try:
        bridge_keys = await redis.keys("bridge:*") or []
        cross_keys = await redis.keys("crossnet:*") or []
        topic_keys = await redis.keys("topicnet:*") or []
        total_bridges = len(bridge_keys) + len(cross_keys) + len(topic_keys)
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

@app.head("/ping")
async def ping_head():
    """Дополнительный пинг-эндпоинт для HEAD методов"""
    return Response(status_code=200)

@app.get("/ping")
async def ping():
    return {"status": "ok", "bot_ready": bot.is_ready()}


# ================= Вспомогательные функции для вебхуков =================

def clean_discord_message(message: discord.Message) -> str:
    """Очищает разметку Discord (упоминания, каналы, роли, эмодзи) для читаемого отображения в Telegram"""
    content = message.content or ""
    if not content:
        return content

    # 1. Расшифровываем кастомные эмодзи <:pepe:123456789> -> :pepe:
    content = re.sub(r'<a?:([a-zA-Z0-9_]+):[0-9]+>', r':\1:', content)

    # 2. Расшифровываем упоминания пользователей <@123456789> -> @Имя
    def replace_user(match):
        user_id = int(match.group(1))
        member = message.guild.get_member(user_id) if message.guild else None
        if member:
            return f"@{member.display_name}"
        user = bot.get_user(user_id)
        if user:
            return f"@{user.name}"
        return "@Пользователь"

    content = re.sub(r'<@!?([0-9]+)>', replace_user, content)

    # 3. Расшифровываем упоминания ролей <@&123456789> -> @Роль
    def replace_role(match):
        role_id = int(match.group(1))
        role = message.guild.get_role(role_id) if message.guild else None
        return f"@{role.name}" if role else "@Роль"

    content = re.sub(r'<@&([0-9]+)>', replace_role, content)

    # 4. Расшифровываем упоминания каналов <#123456789> -> #имя-канала
    def replace_channel(match):
        chan_id = int(match.group(1))
        chan = bot.get_channel(chan_id)
        return f"#{chan.name}" if chan else "#канал"

    content = re.sub(r'<#([0-9]+)>', replace_channel, content)

    return content


async def get_or_create_webhook(channel: discord.TextChannel) -> discord.Webhook:
    webhooks = await channel.webhooks()
    for wh in webhooks:
        if wh.name == "Bridge Webhook":
            return wh
    return await channel.create_webhook(name="Bridge Webhook")


# ================= СОБЫТИЯ И ИВЕНТЫ БОТА =================
@bot.event
async def on_ready():
    print(f"✅ Вошли как {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"🔄 Синхронизировано {len(synced)} слэш-команд глобально.")
    except Exception as e:
        print(f"❌ Ошибка синхронизации команд: {e}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.id == bot.user.id:
        return

    # Защита от зацикливания вебхуков
    if message.webhook_id:
        try:
            if message.author.name == "Bridge Webhook" or (message.author.discriminator == "0000" and "Bridge Webhook" in message.author.name):
                return
        except Exception:
            pass

    is_muted = await redis.exists(f"bridge_mute:{message.channel.id}")
    if is_muted:
        return

    channel_id_str = str(message.channel.id)

    # 1. Обработка Topic-мостов (прямая связь Discord канал -> Тема TG)
    topic_set_key = f"tg_topic_discord_set:{channel_id_str}"
    mapped_topics_raw = await redis.smembers(topic_set_key) or []
    mapped_topics = [t.decode('utf-8') if isinstance(t, bytes) else str(t) for t in mapped_topics_raw]

    if mapped_topics and message.content:
        cleaned_content = clean_discord_message(message)
        payload_text = f"[{message.author.display_name} | {message.guild.name}]:\n{cleaned_content}"
        
        async with aiohttp.ClientSession() as session:
            for item in mapped_topics:
                tg_chat_id, tg_topic_id, tg_bot_name = item.split(":")
                token_bytes = await redis.get(f"tg_token:{tg_bot_name}")
                if token_bytes:
                    token = token_bytes.decode('utf-8') if isinstance(token_bytes, bytes) else str(token_bytes)
                    url = f"https://api.telegram.org/bot{token}/sendMessage"
                    try:
                        await session.post(url, json={
                            "chat_id": tg_chat_id, 
                            "message_thread_id": int(tg_topic_id),
                            "text": payload_text
                        })
                    except Exception as e:
                        print(f"Ошибка трансляции Discord -> TG Topic ({tg_chat_id}:{tg_topic_id}): {e}")

    # 2. Обработка Cross-сетей
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
                        await wh.send(
                            content=message.content or "",
                            username=f"{message.author.display_name} ({message.guild.name})",
                            avatar_url=message.author.display_avatar.url,
                            embeds=message.embeds,
                            files=[await f.to_file() for f in message.attachments] if message.attachments else []
                        )
                    except Exception as e:
                        print(f"Ошибка пересылки crossnet в {target_id}: {e}")

            # Б. Трансляция в связанные группы Telegram
            tg_links_raw = await redis.smembers(f"tg_links:{network_name}") or []
            tg_links = [link.decode('utf-8') if isinstance(link, bytes) else str(link) for link in tg_links_raw]

            if tg_links and message.content:
                async with aiohttp.ClientSession() as session:
                    # Используем дешифратор для очистки текста от ID-мусора Discord
                    cleaned_content = clean_discord_message(message)
                    payload_text = f"[{message.author.display_name} | {message.guild.name}]:\n{cleaned_content}"
                    for link in tg_links:
                        chat_id, bot_name = link.split(":")
                        token_bytes = await redis.get(f"tg_token:{bot_name}")
                        if token_bytes:
                            token = token_bytes.decode('utf-8') if isinstance(token_bytes, bytes) else str(token_bytes)
                            url = f"https://api.telegram.org/bot{token}/sendMessage"
                            try:
                                await session.post(url, json={"chat_id": chat_id, "text": payload_text})
                            except Exception as e:
                                print(f"Ошибка трансляции Discord -> TG в чат {chat_id}: {e}")

    # 3. Обработка Single-мостов
    bridge_key = f"bridge:{channel_id_str}"
    if await redis.exists(bridge_key):
        targets_raw = await redis.lrange(bridge_key, 0, -1) or []
        targets = [t.decode('utf-8') if isinstance(t, bytes) else str(t) for t in targets_raw]

        for target_id in targets:
            if target_id == "INIT_MARKER":
                continue
            
            if await redis.exists(f"bridge_mute:{target_id}"):
                continue

            target_channel = bot.get_channel(int(target_id))
            if target_channel:
                try:
                    wh = await get_or_create_webhook(target_channel)
                    await wh.send(
                        content=message.content or "",
                        username=message.author.display_name,
                        avatar_url=message.author.display_avatar.url,
                        embeds=message.embeds,
                        files=[await f.to_file() for f in message.attachments] if message.attachments else []
                    )
                except Exception as e:
                    print(f"Ошибка пересылки single в {target_id}: {e}")

    await bot.process_commands(message)


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
        
        # Определяем целевой канал
        if channel:
            # Извлекаем только цифры из ввода (для поддержки упоминаний типа <#123456> или чистых ID)
            clean_id = re.sub(r'[^0-9]', '', channel)
            if not clean_id:
                await interaction.followup.send("❌ Указан неверный формат канала. Используйте упоминание (#канал) или цифровой ID!")
                return
            
            # Сначала ищем канал в локальном кэше бота
            target_channel = bot.get_channel(int(clean_id))
            if not target_channel:
                try:
                    # Если в кэше нет (например, на другом сервере), пробуем загрузить из API Discord
                    target_channel = await bot.fetch_channel(int(clean_id))
                except Exception:
                    pass
            
            if not target_channel:
                await interaction.followup.send("❌ Не удалось найти указанный канал. Убедитесь, что бот добавлен на тот сервер!")
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
            await interaction.followup.send(f"🔗 Канал {target_channel.mention} успешно присоединен к трансляции моста `{name}`!")

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
        
        # Запуск инстанса бота
        await tg_manager.start_bot(name, token)

        await interaction.followup.send(f"✅ Telegram-бот `{name}` успешно зарегистрирован и запущен!\nТеперь вы можете связать его с любой кросс-сетью через Discord-команду `/btelegram`.")
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
        # 1. Проверяем существование кросс-сети
        meta_key = f"bridgemeta:{name}"
        mode_raw = await redis.get(meta_key)
        if not mode_raw or (mode_raw.decode('utf-8') if isinstance(mode_raw, bytes) else str(mode_raw)) != "cross":
            await interaction.followup.send(f"❌ Кросс-сеть `{name}` не найдена.")
            return

        # 2. Проверяем существование бота
        token_bytes = await redis.get(f"tg_token:{bot_name}")
        if not token_bytes:
            await interaction.followup.send(f"❌ Зарегистрированный Telegram-бот `{bot_name}` не найден.")
            return
        token = token_bytes.decode('utf-8') if isinstance(token_bytes, bytes) else str(token_bytes)

        # 3. Сохраняем связи в Redis
        await redis.sadd(f"tg_links:{name}", f"{chat_id}:{bot_name}")
        await redis.set(f"tg_chat_net:{chat_id}", name)

        # 4. Отправляем тестовое сообщение в Telegram для подтверждения связки
        async with aiohttp.ClientSession() as session:
            test_msg = f"🎉 Бот успешно привязал этот чат к кросс-сети `{name}` через Discord!"
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            async with session.post(url, json={"chat_id": chat_id, "text": test_msg}) as resp:
                if resp.status == 200:
                    await interaction.followup.send(f"✅ Успешно! Группа Telegram (`{chat_id}`) связана с сетью `{name}`.")
                else:
                    await interaction.followup.send(f"⚠️ Связь сохранена, но бот не смог отправить приветственное сообщение в Telegram. Проверьте, добавлен ли бот в группу.")
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка связывания моста Telegram: {e}")


# ================= КОМАНДА: /btgtopic (Прямое связывание с топиками TG) =================
@bot.tree.command(name="btgtopic", description="Создать прямой мост между каналом Discord и темой (Topic) Telegram")
@app_commands.describe(
    name="Уникальное имя для этого моста (например, my-topic-bridge)",
    chat_id="ID Telegram-чата (например, -1004384337986)",
    topic_id="ID темы (Topic ID) внутри группы Telegram (например, 2)",
    bot_name="Имя Telegram-бота (необязательно, если зарегистрирован всего один бот)",
    channel="Канал Discord (если пусто, подключит текущий канал)"
)
async def btgtopic(
    interaction: discord.Interaction,
    name: str,
    chat_id: str,
    topic_id: str,
    bot_name: Optional[str] = None,
    channel: Optional[str] = None
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ У вас должны быть права администратора!", ephemeral=True)
        return

    clean_name = re.sub(r'[^a-zA-Z0-9_-]', '', name).strip().lower()
    if not clean_name:
        await interaction.response.send_message("❌ Недопустимое имя моста!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        # 1. Проверяем существование моста с таким именем
        meta_key = f"bridgemeta:{clean_name}"
        if await redis.exists(meta_key):
            await interaction.followup.send(f"❌ Мост или сеть с именем `{clean_name}` уже существует!")
            return

        # 2. Определяем имя Telegram-бота
        if not bot_name:
            bot_names_raw = await redis.smembers("tg_bots_list") or []
            bot_names = [b.decode('utf-8') if isinstance(b, bytes) else str(b) for b in bot_names_raw]
            if not bot_names:
                await interaction.followup.send("❌ У вас нет зарегистрированных Telegram-ботов! Запустите сначала `/btgbot`.")
                return
            if len(bot_names) == 1:
                bot_name = bot_names[0]
            else:
                await interaction.followup.send("❌ У вас зарегистрировано несколько ботов. Укажите имя нужного бота в параметре `bot_name`!")
                return
        else:
            bot_name = bot_name.strip().lower()
            if not await redis.exists(f"tg_token:{bot_name}"):
                await interaction.followup.send(f"❌ Зарегистрированный Telegram-бот `{bot_name}` не найден!")
                return

        # 3. Определяем канал Discord
        if channel:
            clean_cid = re.sub(r'[^0-9]', '', channel)
            if not clean_cid:
                await interaction.followup.send("❌ Указан неверный формат канала Discord!")
                return
            target_channel = bot.get_channel(int(clean_cid))
            if not target_channel:
                try:
                    target_channel = await bot.fetch_channel(int(clean_cid))
                except Exception:
                    pass
            if not target_channel:
                await interaction.followup.send("❌ Не удалось найти указанный канал Discord.")
                return
        else:
            target_channel = interaction.channel

        channel_id_str = str(target_channel.id)
        chat_id = chat_id.strip()
        topic_id = topic_id.strip()

        # 4. Сохраняем связи в Redis
        await redis.set(meta_key, "topic")
        await redis.set(f"topicnet:{clean_name}", f"{channel_id_str}:{chat_id}:{topic_id}:{bot_name}")
        await redis.sadd(f"tg_topic_discord_set:{channel_id_str}", f"{chat_id}:{topic_id}:{bot_name}")
        await redis.sadd(f"tg_topic_tg_set:{chat_id}:{topic_id}", channel_id_str)

        # 5. Проверяем бота и шлем сообщение для теста
        token_bytes = await redis.get(f"tg_token:{bot_name}")
        token = token_bytes.decode('utf-8') if isinstance(token_bytes, bytes) else str(token_bytes)

        async with aiohttp.ClientSession() as session:
            test_msg = f"🎉 Мост `{clean_name}` успешно связал эту тему с каналом Discord {target_channel.name}!"
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "message_thread_id": int(topic_id),
                "text": test_msg
            }
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    await interaction.followup.send(f"✅ Успешно создан мост `{clean_name}` между каналом {target_channel.mention} и темой `{topic_id}`!")
                else:
                    await interaction.followup.send(f"⚠️ Мост `{clean_name}` создан, но бот не смог отправить тестовое сообщение в Telegram. Убедитесь, что бот добавлен в группу как администратор.")
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка при создании топик-моста: {e}")


# ================= КОМАНДА: /brename =================
@bot.tree.command(name="brename", description="Переименовать существующую кросс-сеть")
@app_commands.describe(
    old_name="Текущее уникальное имя вашей кросс-сети",
    new_name="Новое имя для кросс-сети (только буквы, цифры и знаки дефиса)"
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
        old_meta_key = f"bridgemeta:{old_name}"
        old_cross_key = f"crossnet:{old_name}"

        # 1. Проверяем существование сети и её тип
        mode_raw = await redis.get(old_meta_key)
        if not mode_raw:
            await interaction.followup.send(f"❌ Кросс-сеть `{old_name}` не найдена.")
            return

        mode = mode_raw.decode('utf-8') if isinstance(mode_raw, bytes) else str(mode_raw)
        if mode != "cross":
            await interaction.followup.send("❌ Переименовать можно только сети в режиме `cross`!")
            return

        # 2. Проверяем владельца (первый подключенный канал в кросс-сети является создателем)
        channels_raw = await redis.lrange(old_cross_key, 0, -1) or []
        channels = [c.decode('utf-8') if isinstance(c, bytes) else str(c) for c in channels_raw]

        if not channels:
            await interaction.followup.send("❌ Ошибка структуры сети: в ней отсутствуют каналы.")
            return

        creator_channel_id = int(channels[0])
        creator_channel_exists = interaction.guild.get_channel(creator_channel_id)

        if not creator_channel_exists:
            await interaction.followup.send("❌ Переименовать сеть может только администратор сервера, который изначально её создал!")
            return

        # 3. Проверяем, не занато ли новое имя
        new_meta_key = f"bridgemeta:{new_name}"
        if await redis.exists(new_meta_key):
            await interaction.followup.send(f"❌ Имя `{new_name}` уже занято другой сетью или мостом!")
            return

        # 4. Выполняем атомарный перенос ключей в Redis
        new_cross_key = f"crossnet:{new_name}"
        
        await redis.rename(old_meta_key, new_meta_key)
        await redis.rename(old_cross_key, new_cross_key)

        # Переносим связи Telegram
        tg_links_exist = await redis.exists(f"tg_links:{old_name}")
        if tg_links_exist:
            await redis.rename(f"tg_links:{old_name}", f"tg_links:{new_name}")

        await interaction.followup.send(f"🎉 Кросс-сеть успешно переименована из `{old_name}` в `{new_name}`! Все участники продолжают общение.")

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
                await interaction.followup.send(f"🗑️ Single-мост `{name}` полностью удален из базы данных.")
            else:
                bridge_key = f"bridge:{name}"
                await redis.lrem(bridge_key, 0, channel_id_str)
                await interaction.followup.send(f"🔌 Канал успешно отвязан от моста `{name}`.")

        elif mode == "cross":
            cross_key = f"crossnet:{name}"
            await redis.lrem(cross_key, 0, channel_id_str)
            
            remaining = await redis.llen(cross_key)
            if remaining == 0:
                await redis.delete(cross_key)
                await redis.delete(meta_key)
                await redis.delete(f"tg_links:{name}")
                await interaction.followup.send(f"🗑️ Кросс-сеть `{name}` опустела и была полностью удалена.")
            else:
                await interaction.followup.send(f"🔌 Этот канал успешно вышел из кросс-сети `{name}`.")

        elif mode == "topic":
            # Безопасно очищаем прямые связки топика по названию моста
            data_raw = await redis.get(f"topicnet:{name}")
            if data_raw:
                data = data_raw.decode('utf-8') if isinstance(data_raw, bytes) else str(data_raw)
                cid, chat_id, topic_id, bot_name = data.split(":")
                await redis.srem(f"tg_topic_discord_set:{cid}", f"{chat_id}:{topic_id}:{bot_name}")
                await redis.srem(f"tg_topic_tg_set:{chat_id}:{topic_id}", cid)
            await redis.delete(f"topicnet:{name}")
            await redis.delete(meta_key)
            await interaction.followup.send(f"🗑️ Мост к теме Telegram `{name}` полностью удален из базы данных.")

    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка удаления/выхода: {e}")


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

        bridge_keys_raw = await redis.keys("bridge:*") or []
        bridge_keys = {k.decode('utf-8') if isinstance(k, bytes) else str(k) for k in bridge_keys_raw}

        meta_keys_raw = await redis.keys("bridgemeta:*") or []
        meta_keys = {k.decode('utf-8') if isinstance(k, bytes) else str(k) for k in meta_keys_raw}

        cross_keys_raw = await redis.keys("crossnet:*") or []
        cross_keys = {k.decode('utf-8') if isinstance(k, bytes) else str(k) for k in cross_keys_raw}

        topic_keys_raw = await redis.keys("topicnet:*") or []
        topic_keys = {k.decode('utf-8') if isinstance(k, bytes) else str(k) for k in topic_keys_raw}

        embed = discord.Embed(
            title=f"🌐 Активные связи мостов для сервера {interaction.guild.name}", 
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
                        name=f"📢 Single-Мост (Источник: {source_info})",
                        value=f"➡️ ... транслируется в:\n{targets_info}",
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
                
                # Читаем связанные TG группы
                tg_links_raw = await redis.smembers(f"tg_links:{cross_name}") or []
                tg_links = [link.decode('utf-8') if isinstance(link, bytes) else str(link) for link in tg_links_raw]
                tg_info = ""
                if tg_links:
                    tg_info = "\n📱 **Telegram мосты:**\n" + "\n".join([f"• Чат `{link.split(':')[0]}` (бот: `{link.split(':')[1]}`)" for link in tg_links])

                embed.add_field(
                    name=f"👑 Cross-сеть: `{cross_name}`",
                    value=f"🔗 Связанные каналы:\n{channels_info}{tg_info}",
                    inline=False
                )

        for tk in topic_keys:
            topic_name = tk.split(":")[-1]
            data_raw = await redis.get(tk)
            if data_raw:
                data = data_raw.decode('utf-8') if isinstance(data_raw, bytes) else str(data_raw)
                cid, chat_id, topic_id, bot_name = data.split(":")
                
                if cid in local_channel_ids:
                    shown_count += 1
                    chan_info = await resolve_channel_name(cid)
                    embed.add_field(
                        name=f"🎯 Мост к теме Telegram: `{topic_name}`",
                        value=f"📟 Канал: {chan_info}\n📱 Telegram: Чат `{chat_id}` (Тема `{topic_id}`) через бота `{bot_name}`",
                        inline=False
                    )

        if shown_count == 0:
            await interaction.followup.send("📭 На этом сервере не найдено активных мостов.")
        else:
            await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка при получении списка: {e}")


# ================= АВТОЗАПУСК СЕРВЕРА С БОТОМ =================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=10000, reload=False)
