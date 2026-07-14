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
import io
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

    if mode == "single":
        if not source:
            await interaction.followup.send("❌ Для режима single необходимо указать source (ID канала-источника)!")
            return
        try:
            source_id = extract_id(source)
        except ValueError:
            await interaction.followup.send("❌ Укажите корректный ID канала-источника.")
            return

        source_id_str = str(source_id)
        bridge_key = f"bridge:{source_id_str}"
        meta_key = f"bridgemeta:{source_id_str}"

        try:
            exists = await redis.exists(meta_key)
            if exists:
                await interaction.followup.send(f"⚠️ Мост для источника `{source_id_str}` уже инициализирован!")
                return

            await redis.set(meta_key, "single")
            if not await redis.exists(bridge_key):
                await redis.rpush(bridge_key, "INIT_MARKER")

            await interaction.followup.send(f"📈 Мост трансляции создан! Используйте `/bconnect bname:{source_id_str}` для привязки целевых каналов.")
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка Redis: {e}")

    elif mode == "cross":
        if not name:
            await interaction.followup.send("❌ Для режима cross необходимо указать параметр name (имя моста)!")
            return
        
        safe_name = re.sub(r'[^a-zA-Z0-9_-]', '', name).lower()
        meta_key = f"bridgemeta:{safe_name}"

        try:
            exists = await redis.exists(meta_key)
            if exists:
                await interaction.followup.send(f"⚠️ Кросс-мост с именем `{safe_name}` уже существует!")
                return

            await redis.set(meta_key, "cross")
            net_key = f"crossnet:{safe_name}"
            await redis.rpush(net_key, "INIT_MARKER")
            await interaction.followup.send(f"👑 Кросс-мост чатов `{safe_name}` создан! Используйте `/bconnect bname:{safe_name}` для привязки каналов.")
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка Redis: {e}")

# ================= КОМАНДА: /bconnect =================
@bot.tree.command(name="bconnect", description="Подключить канал к мосту (текущий или указанный через аргумент channel)")
async def bconnect(interaction: discord.Interaction, bname: str, channel: str = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ У вас должны быть права администратора!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    safe_bname = re.sub(r'[^a-zA-Z0-9_-]', '', bname).lower()
    
    if channel:
        try:
            target_channel_id_str = str(extract_id(channel))
        except ValueError:
            await interaction.followup.send("❌ Укажите корректный ID канала в аргументе channel.")
            return
    else:
        target_channel_id_str = str(interaction.channel_id)
    
    try:
        mode_raw = await redis.get(f"bridgemeta:{safe_bname}")
        
        if not mode_raw and await redis.exists(f"bridge:{safe_bname}"):
            mode = "single"
            await redis.set(f"bridgemeta:{safe_bname}", "single")
        elif mode_raw:
            mode = mode_raw.decode('utf-8') if isinstance(mode_raw, bytes) else str(mode_raw)
        else:
            await interaction.followup.send(f"❌ Моста или источника со значением `{safe_bname}` не существует!")
            return

        if mode == "single":
            bridge_key = f"bridge:{safe_bname}"
            
            current_targets_raw = await redis.lrange(bridge_key, 0, -1) or []
            current_targets = [t.decode('utf-8') if isinstance(t, bytes) else str(t) for t in current_targets_raw]
            
            if target_channel_id_str in current_targets:
                await interaction.followup.send(f"⚠️ Канал <#{target_channel_id_str}> уже принимает трансляцию из источника `{safe_bname}`!")
                return
                
            await redis.rpush(bridge_key, target_channel_id_str)
            await interaction.followup.send(f"✅ Готово! Канал <#{target_channel_id_str}> теперь получает трансляцию из источника <#{safe_bname}>")

        elif mode == "cross":
            net_key = f"crossnet:{safe_bname}"
            current_channels_raw = await redis.lrange(net_key, 0, -1) or []
            current_channels = [c.decode('utf-8') if isinstance(c, bytes) else str(c) for c in current_channels_raw]
            
            if target_channel_id_str in current_channels:
                await interaction.followup.send(f"⚠️ Канал <#{target_channel_id_str}> уже подключен к кросс-мосту `{safe_bname}`!")
                return
                
            await redis.rpush(net_key, target_channel_id_str)
            await redis.sadd(f"crosschannels:{target_channel_id_str}", safe_bname)
            await interaction.followup.send(f"🔗 Канал <#{target_channel_id_str}> успешно подключен к глобальному кросс-мосту `{safe_bname}`!")

    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка подключения: {e}")

# ================= КОМАНДА: /blist =================
@bot.tree.command(name="blist", description="Показать список всех активных мостов и подключенных каналов")
async def blist(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ У вас должны быть права администратора!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        meta_keys_raw = await redis.keys("bridgemeta:*") or []
        meta_keys = [k.decode('utf-8') if isinstance(k, bytes) else str(k) for k in meta_keys_raw]

        if not meta_keys:
            # Резервный поиск для обратной совместимости по старым ключам bridge:*
            bridge_keys_raw = await redis.keys("bridge:*") or []
            bridge_keys = [k.decode('utf-8') if isinstance(k, bytes) else str(k) for k in bridge_keys_raw]
            if not bridge_keys:
                await interaction.followup.send("📭 Активных мостов не найдено.")
                return
            
            embed = discord.Embed(title="🌐 Список активных мостов (Совместимость)", color=0x3498db)
            for bk in bridge_keys:
                source_id = bk.split(":")[-1]
                targets_raw = await redis.lrange(bk, 0, -1) or []
                targets = [t.decode('utf-8') if isinstance(t, bytes) else str(t) for t in targets_raw if (t.decode('utf-8') if isinstance(t, bytes) else str(t)) != "INIT_MARKER"]
                targets_mention = ", ".join([f"<#{t}>" for t in targets]) if targets else "*Нет подключенных каналов*"
                embed.add_field(name=f"📢 Источник: <#{source_id}> (ID: {source_id})", value=f"➡️ Получатели: {targets_mention}", inline=False)
            await interaction.followup.send(embed=embed)
            return

        embed = discord.Embed(title="🌐 Список активных мостов", color=0x2ecc71)
        single_count = 0
        cross_count = 0

        for mk in meta_keys:
            name_or_id = mk.split(":")[-1]
            mode_raw = await redis.get(mk)
            mode = mode_raw.decode('utf-8') if isinstance(mode_raw, bytes) else str(mode_raw)

            if mode == "single":
                single_count += 1
                targets_raw = await redis.lrange(f"bridge:{name_or_id}", 0, -1) or []
                targets = [t.decode('utf-8') if isinstance(t, bytes) else str(t) for t in targets_raw if (t.decode('utf-8') if isinstance(t, bytes) else str(t)) != "INIT_MARKER"]
                targets_mention = ", ".join([f"<#{t}>" for t in targets]) if targets else "*Нет подключенных каналов*"
                embed.add_field(
                    name=f"📢 Single: <#{name_or_id}> (ID: {name_or_id})",
                    value=f"➡️ Трансляция в: {targets_mention}",
                    inline=False
                )

            elif mode == "cross":
                cross_count += 1
                channels_raw = await redis.lrange(f"crossnet:{name_or_id}", 0, -1) or []
                channels = [c.decode('utf-8') if isinstance(c, bytes) else str(c) for c in channels_raw if (c.decode('utf-8') if isinstance(c, bytes) else str(c)) != "INIT_MARKER"]
                channels_mention = ", ".join([f"<#{c}>" for c in channels]) if channels else "*Нет подключенных каналов*"
                embed.add_field(
                    name=f"👑 Cross-сеть: `{name_or_id}`",
                    value=f"🔗 Участники: {channels_mention}",
                    inline=False
                )

        if single_count == 0 and cross_count == 0:
            await interaction.followup.send("📭 Активных мостов не найдено.")
        else:
            await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка при получении списка: {e}")

# ================= КОМАНДА: /bdelete =================
@bot.tree.command(name="bdelete", description="Полностью удалить мост (введите имя кросс-моста или ID источника)")
async def bdelete(interaction: discord.Interaction, name: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ У вас должны быть права администратора!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '', name).lower()

    try:
        mode_raw = await redis.get(f"bridgemeta:{safe_name}")
        
        if not mode_raw and await redis.exists(f"bridge:{safe_name}"):
            mode = "single"
        elif mode_raw:
            mode = mode_raw.decode('utf-8') if isinstance(mode_raw, bytes) else str(mode_raw)
        else:
            await interaction.followup.send(f"❌ Моста или источника со значением `{safe_name}` не существует.")
            return

        if mode == "single":
            await redis.delete(f"bridge:{safe_name}")
            
        elif mode == "cross":
            net_key = f"crossnet:{safe_name}"
            channels_raw = await redis.lrange(net_key, 0, -1) or []
            for c_raw in channels_raw:
                c_str = c_raw.decode('utf-8') if isinstance(c_raw, bytes) else str(c_raw)
                if c_str != "INIT_MARKER":
                    await redis.srem(f"crosschannels:{c_str}", safe_name)
            await redis.delete(net_key)

        await redis.delete(f"bridgemeta:{safe_name}")
        await interaction.followup.send(f"🗑️ Мост `{safe_name}` (тип: {mode}) успешно удален из системы!")

    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка при удалении моста: {e}")

# ================= ОБРАБОТКА СООБЩЕНИЙ =================
@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    if message.webhook_id and message.webhook_id in [wh.id for wh in cached_webhooks.values()]:
        return

    current_channel_id_str = str(message.channel.id)
    targets_to_send = set()

    # --- ЛОГИКА 1: Проверяем классические мосты (Single) ---
    single_key = f"bridge:{current_channel_id_str}"
    single_targets_raw = await redis.lrange(single_key, 0, -1)
    if single_targets_raw:
        for t_raw in single_targets_raw:
            try:
                t_str = t_raw.decode('utf-8').strip("'\" ") if isinstance(t_raw, bytes) else str(t_raw).strip("'\" ")
                if t_str == "INIT_MARKER":
                    continue
                targets_to_send.add(int(t_str))
            except:
                continue

    # --- ЛОГИКА 2: Проверяем кросс-мосты (Cross) ---
    active_cross_bridges = await redis.smembers(f"crosschannels:{current_channel_id_str}")
    if active_cross_bridges:
        for b_bytes in active_cross_bridges:
            b_name = b_bytes.decode('utf-8') if isinstance(b_bytes, bytes) else str(b_bytes)
            if await redis.exists(f"bridgemeta:{b_name}"):
                cross_targets_raw = await redis.lrange(f"crossnet:{b_name}", 0, -1) or []
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
        # Скачиваем файлы и оборачиваем их заново с сохранением оригинальных свойств
        files = []
        for attachment in message.attachments:
            try:
                fp = io.BytesIO()
                await attachment.save(fp)
                discord_file = discord.File(fp, filename=attachment.filename, spoiler=attachment.is_spoiler())
                files.append(discord_file)
            except Exception as e:
                print(f"🔴 Ошибка подготовки файла {attachment.filename}: {e}")
        
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
                    # Сбрасываем указатель буфера в начало перед каждой отправкой
                    for f in files:
                        f.fp.seek(0)

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
