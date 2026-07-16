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
import base64
import time
from contextlib import asynccontextmanager
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from upstash_redis.asyncio import Redis
import aiohttp

# Импорты для веб-сайта
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
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

# Буфер для хранения дефолтной аватарки в оперативной памяти (решает проблему редиректов Discord)
DEFAULT_AVATAR_BYTES = b""

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
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    print(f"[TG {self.bot_name}] Ошибка в цикле обновлений: {e}")
                    await asyncio.sleep(5)

    async def _handle_message(self, session: aiohttp.ClientSession, message: dict):
        chat_id = str(message["chat"]["id"])
        text = message.get("text", "").strip()
        user = message.get("from", {})
        
        if not text or text.startswith("/"):
            return  # Игнорируем команды и пустые системные сообщения

        # Проверяем, привязана ли эта группа к какой-либо кросс-сети
        network_name_bytes = await redis.get(f"tg_chat_net:{chat_id}")
        if not network_name_bytes:
            return  # Чат не подключен к мостам

        network_name = network_name_bytes.decode('utf-8') if isinstance(network_name_bytes, bytes) else str(network_name_bytes)
        
        # Получаем каналы Discord, входящие в эту кросс-сеть
        cross_key = f"crossnet:{network_name}"
        channels_raw = await redis.lrange(cross_key, 0, -1) or []
        channels = [c.decode('utf-8') if isinstance(c, bytes) else str(c) for c in channels_raw]

        first_name = user.get("first_name", "")
        last_name = user.get("last_name", "")
        full_name = f"{first_name} {last_name}".strip() or "Telegram User"

        # Формируем БЕЗОПАСНЫЙ URL аватарки через наш прокси на FastAPI с часовым кэш-бастером
        render_url = os.getenv("RENDER_EXTERNAL_URL", "http://localhost:10000").rstrip('/')
        current_hour = time.strftime("%Y%m%d%H")
        avatar_url = f"{render_url}/tg_avatar/{self.bot_name}/{user.get('id', 0)}?v={current_hour}"

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
    # Загружаем дефолтный аватар Discord напрямую в память во избежание редиректов
    global DEFAULT_AVATAR_BYTES
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get("https://cdn.discordapp.com/embed/avatars/0.png") as resp:
                if resp.status == 200:
                    DEFAULT_AVATAR_BYTES = await resp.read()
                    print("✅ Дефолтный аватар Discord успешно загружен в буфер памяти.")
        except Exception as e:
            print(f"⚠️ Не удалось загрузить дефолтную аватарку: {e}")

    # Резервный буфер на случай, если сеть во время инициализации упала (прозрачный 1x1 PNG)
    if not DEFAULT_AVATAR_BYTES:
        DEFAULT_AVATAR_BYTES = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc`\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'

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
@app.get("/", response_class=HTMLResponse)
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
async def ping():
    return {"status": "ok", "bot_ready": bot.is_ready()}

# ================= БЕЗОПАСНЫЙ ПРОКСИ ДЛЯ АВАТАРОК TELEGRAM =================
@app.get("/tg_avatar/{bot_name}/{user_id}")
async def get_tg_avatar(bot_name: str, user_id: int):
    """Эндпоинт для безопасного проксирования аватарок из TG в Discord вебхуки с кэшированием в Redis"""
    cache_key = f"tg_avatar_cache:{user_id}"
    
    # 1. Сначала пытаемся получить аватарку из быстрого кэша Redis
    try:
        cached_b64 = await redis.get(cache_key)
        if cached_b64:
            cached_str = cached_b64.decode('utf-8') if isinstance(cached_b64, bytes) else str(cached_b64)
            img_bytes = base64.b64decode(cached_str)
            return Response(content=img_bytes, media_type="image/jpeg")
    except Exception as e:
        print(f"[FASTAPI AVATAR PROXY] Ошибка чтения кэша Redis: {e}")

    # 2. Если в кэше пусто, загружаем токен из базы данных
    token_bytes = await redis.get(f"tg_token:{bot_name}")
    if not token_bytes:
        return Response(content=DEFAULT_AVATAR_BYTES, media_type="image/png")
    
    token = token_bytes.decode('utf-8') if isinstance(token_bytes, bytes) else str(token_bytes)
    
    async with aiohttp.ClientSession() as session:
        try:
            # А. Запрашиваем информацию об аватарках пользователя
            async with session.get(f"https://api.telegram.org/bot{token}/getUserProfilePhotos?user_id={user_id}&limit=1") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("ok") and data["result"]["total_count"] > 0:
                        # Получаем список размеров для первой (актуальной) аватарки
                        photos = data["result"]["photos"][0]
                        # Выбираем оптимальный размер (индекс 1 (обычно 320x320) если доступно, иначе 0 (160x160))
                        photo_index = 1 if len(photos) > 1 else 0
                        file_id = photos[photo_index]["file_id"]
                        
                        # Б. Получаем внутренний путь к файлу в Telegram
                        async with session.get(f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}") as file_resp:
                            if file_resp.status == 200:
                                file_data = await file_resp.json()
                                if file_data.get("ok"):
                                    file_path = file_data["result"]["file_path"]
                                    file_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
                                    
                                    # В. Скачиваем аватарку
                                    async with session.get(file_url) as img_resp:
                                        if img_resp.status == 200:
                                            img_bytes = await img_resp.read()
                                            
                                            # Записываем байты в кэш Redis на 6 часов (21600 сек) в формате Base64
                                            try:
                                                encoded_str = base64.b64encode(img_bytes).decode('utf-8')
                                                await redis.set(cache_key, encoded_str, ex=21600)
                                            except Exception as cache_err:
                                                print(f"[FASTAPI AVATAR PROXY] Ошибка записи в кэш: {cache_err}")
                                                
                                            return Response(content=img_bytes, media_type="image/jpeg")
        except Exception as e:
            print(f"[FASTAPI AVATAR PROXY] Ошибка получения аватара для {user_id}: {e}")
            
    # Если у пользователя нет аватарки или произошла ошибка — кэшируем дефолтный аватар на 1 час,
    # чтобы не слать частые бесполезные запросы в Telegram API на каждый клик Discord'а.
    try:
        encoded_str = base64.b64encode(DEFAULT_AVATAR_BYTES).decode('utf-8')
        await redis.set(cache_key, encoded_str, ex=3600)
    except Exception as cache_err:
        print(f"[FASTAPI AVATAR PROXY] Ошибка сохранения дефолтного кэша: {cache_err}")

    # Отдаем напрямую дефолтные байты
    return Response(content=DEFAULT_AVATAR_BYTES, media_type="image/png")


# ================= Вспомогательные функции для вебхуков =================
async def get_or_create_webhook(channel: discord.TextChannel) -> discord.Webhook:
    webhooks = await channel.webhooks()
    for wh in webhooks:
        if wh.name == "Bridge Webhook":
            # Сохраняем ID нашего вебхука в Redis для защиты от зацикливания
            await redis.sadd("our_webhooks", str(wh.id))
            return wh
    wh = await channel.create_webhook(name="Bridge Webhook")
    await redis.sadd("our_webhooks", str(wh.id))
    return wh

async def cache_all_existing_webhooks():
    """Сбор и кэширование ID всех существующих вебхуков нашего бота на серверах"""
    print("🔍 Кэширование ID наших вебхуков для защиты от зацикливания...")
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
    print(f"✅ Успешно закэшировано {count} вебхуков в Redis.")


# ================= СОБЫТИЯ И ИВЕНТЫ БОТА =================
@bot.event
async def on_ready():
    print(f"✅ Вошли как {bot.user} (ID: {bot.user.id})")
    
    # Кэшируем наши вебхуки, чтобы не блокировать чужие
    await cache_all_existing_webhooks()

    # Полезная диагностика для локальной разработки
    ext_url = os.getenv("RENDER_EXTERNAL_URL")
    if not ext_url or "localhost" in ext_url:
        print("ℹ️ Внимание: RENDER_EXTERNAL_URL отсутствует или указывает на localhost.")
        print("   При тестировании на локальном компьютере Discord не сможет получить аватарки Telegram.")
        print("   Для тестирования аватарок локально используйте Ngrok и укажите его HTTPS адрес в RENDER_EXTERNAL_URL.")
    
    try:
        synced = await bot.tree.sync()
        print(f"🔄 Синхронизировано {len(synced)} слэш-команд глобально.")
    except Exception as e:
        print(f"❌ Ошибка синхронизации команд: {e}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.id == bot.user.id:
        return

    # Защита от зацикливания: проверяем, отправлено ли сообщение НАШИМ вебхуком
    if message.webhook_id is not None:
        is_our_webhook = await redis.sismember("our_webhooks", str(message.webhook_id))
        if is_our_webhook:
            return  # Игнорируем только свои вебхуки. Чужие (Вики-Бот) будут работать!

    is_muted = await redis.exists(f"bridge_mute:{message.channel.id}")
    if is_muted:
        return

    channel_id_str = str(message.channel.id)

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
                    # Специфицированный формат отправки сообщений в Telegram
                    payload_text = f"[{message.author.display_name} | {message.guild.name}]:\n{message.content}"
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

    # 2. Обработка Single-мостов
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
async def bcreate(interaction: discord.Interaction, name: str, mode: str):
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
@bot.tree.command(name="bconnect", description="Подключить текущий канал к существующей сети")
async def bconnect(interaction: discord.Interaction, name: str):
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
            bridge_key = f"bridge:{name}"
            targets_raw = await redis.lrange(bridge_key, 0, -1) or []
            targets = [t.decode('utf-8') if isinstance(t, bytes) else str(t) for t in targets_raw]

            if channel_id_str in targets:
                await interaction.followup.send("❌ Этот канал уже привязан к этому мосту!")
                return

            await redis.rpush(bridge_key, channel_id_str)
            await interaction.followup.send(f"🔗 Канал успешно присоединен к трансляции моста `{name}`!")

        elif mode == "cross":
            cross_key = f"crossnet:{name}"
            channels_raw = await redis.lrange(cross_key, 0, -1) or []
            channels = [c.decode('utf-8') if isinstance(c, bytes) else str(c) for c in channels_raw]

            if channel_id_str in channels:
                await interaction.followup.send("❌ Этот канал уже находится в этой кросс-сети!")
                return

            await redis.rpush(cross_key, channel_id_str)
            await interaction.followup.send(f"🔗 Канал успешно подключен к кросс-сети `{name}`!")

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
