import os
import asyncio
import re
from contextlib import asynccontextmanager
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from upstash_redis.asyncio import Redis

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

# ================= СОВМЕСТНЫЙ ЗАПУСК (LIFESPAN) =================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Код при старте веб-сайта: запускаем бота в фоне
    asyncio.create_task(bot.start(TOKEN))
    print("🤖 Discord Bot (Redis) запущен в фоновом режиме!")
    yield
    # Код при закрытии веб-сайта
    print("🔌 Закрытие соединения с Discord и Redis...")
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
    print(f"✅ Вошли как {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"🔄 Синхронизировано {len(synced)} слэш-команд глобально.")
    except Exception as e:
        print(f"❌ Ошибка синхронизации команд: {e}")

@bot.event
async def on_message(message: discord.Message):
    # Игнорируем сообщения самого бота, но пропускаем Вики-Бота
    if message.author.id == bot.user.id:
        return

    # Защита от бесконечного цикла отправки вебхуков
    if message.webhook_id:
        try:
            if message.author.name == "Bridge Webhook" or (message.author.discriminator == "0000" and "Bridge Webhook" in message.author.name):
                return
        except Exception:
            pass

    # Проверяем, не приглушен ли канал-источник
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
            for target_id in channels:
                if target_id == channel_id_str:
                    continue  # не пересылаем обратно себе
                
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


# ================= КОМАНДА: /brename (Эксклюзивно для Cross-сетей в Redis) =================
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

        # Проверяем, принадлежит ли первый (создавший) канал текущему серверу
        creator_channel_id = int(channels[0])
        creator_channel_exists = interaction.guild.get_channel(creator_channel_id)

        if not creator_channel_exists:
            await interaction.followup.send("❌ Переименовать сеть может только администратор сервера, который изначально её создал!")
            return

        # 3. Проверяем, не занято ли новое имя
        new_meta_key = f"bridgemeta:{new_name}"
        if await redis.exists(new_meta_key):
            await interaction.followup.send(f"❌ Имя `{new_name}` уже занято другой сетью или мостом!")
            return

        # 4. Выполняем атомарный перенос ключей в Redis
        new_cross_key = f"crossnet:{new_name}"
        
        await redis.rename(old_meta_key, new_meta_key)
        await redis.rename(old_cross_key, new_cross_key)

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
                embed.add_field(
                    name=f"👑 Cross-сеть: `{cross_name}`",
                    value=f"🔗 Связанные каналы:\n{channels_info}",
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
    uvicorn.run("bot_redis:app", host="0.0.0.0", port=10000, reload=False)
