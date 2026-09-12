import os
import shutil
import sqlite3
import subprocess
import secrets
import signal
import threading
import asyncio
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

# ==================== 設定 ====================
TOKEN = os.environ.get('DISCORD_TOKEN')
if not TOKEN:
    raise SystemExit("DISCORD_TOKEN not set")

OWNER_ID = 1392870568432373810
DB_PATH = "bot_data.db"
MAX_BOTS_PER_USER = 2

# ==================== 資料庫初始化 ====================
def get_db():
    return sqlite3.connect(DB_PATH)

conn = get_db()
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    discord_id INTEGER PRIMARY KEY,
    channel_id INTEGER,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id INTEGER,
    bot_name TEXT,
    process_pid INTEGER,
    bot_status TEXT DEFAULT 'offline',
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (discord_id) REFERENCES users(discord_id)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id INTEGER,
    bot_name TEXT,
    filename TEXT,
    file_path TEXT,
    uploaded_at TEXT DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (discord_id) REFERENCES users(discord_id)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS keys (
    key TEXT PRIMARY KEY,
    created_by INTEGER,
    used_by INTEGER,
    is_used INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    used_at TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS server_settings (
    guild_id INTEGER PRIMARY KEY,
    category_id INTEGER,
    panel_channel_id INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS bot_quota (
    discord_id INTEGER PRIMARY KEY,
    extra_quota INTEGER DEFAULT 0
)
""")
conn.commit()
conn.close()

# ==================== 輔助函式 ====================
def is_owner_id(discord_id):
    return discord_id == OWNER_ID

def get_extra_quota(discord_id):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT extra_quota FROM bot_quota WHERE discord_id = ?", (discord_id,))
        row = cursor.fetchone()
        return row[0] if row else 0
    finally:
        conn.close()

def get_user(discord_id):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM users WHERE discord_id = ?", (discord_id,))
        row = cursor.fetchone()
        if row:
            return {"discord_id": row[0], "channel_id": row[1], "created_at": row[2]}
        return None
    finally:
        conn.close()

def create_user(discord_id, channel_id):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT OR REPLACE INTO users (discord_id, channel_id) VALUES (?, ?)", (discord_id, channel_id))
        conn.commit()
    finally:
        conn.close()

def delete_user(discord_id):
    for bot_data in get_user_bots(discord_id):
        if bot_data["bot_status"] == "online":
            stop_bot_process(discord_id, bot_data["bot_name"])
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM files WHERE discord_id = ?", (discord_id,))
        cursor.execute("DELETE FROM bots WHERE discord_id = ?", (discord_id,))
        cursor.execute("DELETE FROM users WHERE discord_id = ?", (discord_id,))
        cursor.execute("DELETE FROM bot_quota WHERE discord_id = ?", (discord_id,))
        conn.commit()
    finally:
        conn.close()

def get_user_bots(discord_id):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM bots WHERE discord_id = ? ORDER BY created_at DESC", (discord_id,))
        rows = cursor.fetchall()
        return [{"id": r[0], "discord_id": r[1], "bot_name": r[2], "process_pid": r[3], "bot_status": r[4], "created_at": r[5]} for r in rows]
    finally:
        conn.close()

def get_bot(discord_id, bot_name):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM bots WHERE discord_id = ? AND bot_name = ?", (discord_id, bot_name))
        row = cursor.fetchone()
        if row:
            return {"id": row[0], "discord_id": row[1], "bot_name": row[2], "process_pid": row[3], "bot_status": row[4], "created_at": row[5]}
        return None
    finally:
        conn.close()

def create_bot(discord_id, bot_name):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO bots (discord_id, bot_name) VALUES (?, ?)", (discord_id, bot_name))
        conn.commit()
    finally:
        conn.close()

def delete_bot(discord_id, bot_name):
    bot_data = get_bot(discord_id, bot_name)
    if bot_data and bot_data["bot_status"] == "online":
        stop_bot_process(discord_id, bot_name)
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM files WHERE discord_id = ? AND bot_name = ?", (discord_id, bot_name))
        cursor.execute("DELETE FROM bots WHERE discord_id = ? AND bot_name = ?", (discord_id, bot_name))
        conn.commit()
    finally:
        conn.close()

def rename_bot(discord_id, old_name, new_name):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE bots SET bot_name = ? WHERE discord_id = ? AND bot_name = ?", (new_name, discord_id, old_name))
        cursor.execute("UPDATE files SET bot_name = ?, file_path = REPLACE(file_path, ?, ?) WHERE discord_id = ? AND bot_name = ?",
                       (new_name, old_name, new_name, discord_id, old_name))
        conn.commit()
    finally:
        conn.close()
    old_folder = f"./bots/{discord_id}/{old_name}"
    new_folder = f"./bots/{discord_id}/{new_name}"
    if os.path.exists(old_folder):
        os.rename(old_folder, new_folder)

def update_bot_status(discord_id, bot_name, process_pid=None, bot_status=None):
    conn = get_db()
    cursor = conn.cursor()
    try:
        if process_pid is not None and bot_status is not None:
            cursor.execute("UPDATE bots SET process_pid = ?, bot_status = ? WHERE discord_id = ? AND bot_name = ?",
                           (process_pid, bot_status, discord_id, bot_name))
        elif process_pid is not None:
            cursor.execute("UPDATE bots SET process_pid = ? WHERE discord_id = ? AND bot_name = ?", (process_pid, discord_id, bot_name))
        elif bot_status is not None:
            cursor.execute("UPDATE bots SET bot_status = ? WHERE discord_id = ? AND bot_name = ?", (bot_status, discord_id, bot_name))
        conn.commit()
    finally:
        conn.close()

def add_file(discord_id, bot_name, filename, file_path):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO files (discord_id, bot_name, filename, file_path) VALUES (?, ?, ?, ?)",
                       (discord_id, bot_name, filename, file_path))
        conn.commit()
    finally:
        conn.close()

def get_bot_files(discord_id, bot_name):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT filename, file_path FROM files WHERE discord_id = ? AND bot_name = ?", (discord_id, bot_name))
        return cursor.fetchall()
    finally:
        conn.close()

def get_server_settings(guild_id):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM server_settings WHERE guild_id = ?", (guild_id,))
        row = cursor.fetchone()
        if row:
            return {"guild_id": row[0], "category_id": row[1], "panel_channel_id": row[2]}
        return None
    finally:
        conn.close()

def set_server_settings(guild_id, category_id=None, panel_channel_id=None):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM server_settings WHERE guild_id = ?", (guild_id,))
        current = cursor.fetchone()
        if current:
            new_cat = category_id if category_id else current[1]
            new_panel = panel_channel_id if panel_channel_id else current[2]
            cursor.execute("UPDATE server_settings SET category_id = ?, panel_channel_id = ? WHERE guild_id = ?",
                           (new_cat, new_panel, guild_id))
        else:
            cursor.execute("INSERT INTO server_settings (guild_id, category_id, panel_channel_id) VALUES (?, ?, ?)",
                           (guild_id, category_id or 0, panel_channel_id or 0))
        conn.commit()
    finally:
        conn.close()

def generate_key():
    return f"DCBOT-{secrets.token_hex(4).upper()}"

def create_key_in_db(created_by):
    key = generate_key()
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO keys (key, created_by) VALUES (?, ?)", (key, created_by))
        conn.commit()
        return key
    finally:
        conn.close()

def use_key(key, used_by):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE keys SET is_used = 1, used_by = ?, used_at = datetime('now', 'localtime') WHERE key = ? AND is_used = 0",
                       (used_by, key))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()

def is_key_valid(key):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM keys WHERE key = ? AND is_used = 0", (key,))
        return cursor.fetchone() is not None
    finally:
        conn.close()

def get_all_unused_keys():
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT key, created_by, created_at FROM keys WHERE is_used = 0 ORDER BY created_at DESC")
        return cursor.fetchall()
    finally:
        conn.close()

# ==================== Bot 啟動/停止 ====================
running_processes = {}

async def send_dm_429(discord_id, bot_name):
    try:
        target_user = await bot.fetch_user(discord_id)
        if target_user:
            await target_user.send(f"⚠️ `{bot_name}` 偵測到 Discord 429 錯誤，已立即關閉！\n請檢查程式碼後再重新啟動。")
    except:
        pass

def monitor_429(discord_id, bot_name, process):
    try:
        for line in iter(process.stderr.readline, b''):
            if not line:
                continue
            try:
                line_str = line.decode('utf-8', errors='ignore')
            except:
                continue
            if '429' in line_str or 'rate limit' in line_str.lower() or 'too many requests' in line_str.lower():
                print(f"⚠️ 偵測到 429！立刻關閉 {discord_id} 的 {bot_name}")
                try:
                    process.kill()
                except:
                    pass
                update_bot_status(discord_id, bot_name, process_pid=None, bot_status="offline")
                user = get_user(discord_id)
                if user and user["channel_id"]:
                    channel = bot.get_channel(user["channel_id"])
                    if channel:
                        try:
                            asyncio.run_coroutine_threadsafe(
                                channel.send(f"⚠️ `{bot_name}` 偵測到 Discord 429 錯誤，已立即關閉！\n請檢查程式碼後再重新啟動。"),
                                bot.loop
                            )
                        except:
                            pass
                else:
                    try:
                        asyncio.run_coroutine_threadsafe(
                            send_dm_429(discord_id, bot_name),
                            bot.loop
                        )
                    except:
                        pass
                break
    except:
        pass
    finally:
        if process.poll() is not None:
            update_bot_status(discord_id, bot_name, process_pid=None, bot_status="offline")

def start_bot_process(discord_id, bot_name):
    bot_folder = os.path.abspath(f"./bots/{discord_id}/{bot_name}")
    files = get_bot_files(discord_id, bot_name)

    main_file = None
    is_js = False
    for filename, _ in files:
        if filename in ["main.py", "bot.py", "index.py", "app.py"]:
            main_file = filename
            break
        if filename in ["main.js", "bot.js", "index.js", "app.js"]:
            main_file = filename
            is_js = True
            break
    if not main_file and files:
        main_file = files[0][0]
        if main_file.endswith('.js'):
            is_js = True

    if not main_file:
        return None, "沒有可執行的檔案"

    main_path = os.path.join(bot_folder, main_file)
    if not os.path.exists(main_path):
        return None, f"找不到檔案：{main_file}"

    try:
        env_path = os.path.join(bot_folder, ".env")
        env_vars = os.environ.copy()
        if os.path.exists(env_path):
            with open(env_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        env_vars[key.strip()] = value.strip()

        if is_js:
            try:
                subprocess.run(["node", "--version"], capture_output=True, timeout=5, check=True)
            except Exception:
                return None, "找不到 Node.js，請先在伺服器安裝 Node.js"
            pkg_path = os.path.join(bot_folder, "package.json")
            if os.path.exists(pkg_path):
                result = subprocess.run(["npm", "install"], cwd=bot_folder, capture_output=True, text=True, timeout=120)
                if result.returncode != 0:
                    return None, f"npm install 失敗：\n{result.stderr[:800]}"
            cmd = ["node", main_path]
        else:
            req_path = os.path.join(bot_folder, "requirements.txt")
            if os.path.exists(req_path):
                subprocess.run(["pip", "install", "-r", req_path], cwd=bot_folder, capture_output=True, timeout=60)
            python_cmd = None
            for c in ["python3", "python"]:
                try:
                    subprocess.run([c, "--version"], capture_output=True, timeout=5)
                    python_cmd = c
                    break
                except FileNotFoundError:
                    continue
            if python_cmd is None:
                return None, "找不到 Python"
            cmd = [python_cmd, main_path]

        os.makedirs("./logs", exist_ok=True)
        log_file = open(f"./logs/{discord_id}_{bot_name}.log", "w")

        process = subprocess.Popen(
            cmd,
            cwd=bot_folder,
            env=env_vars,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True
        )
        running_processes[f"{discord_id}_{bot_name}"] = process

        monitor_thread = threading.Thread(target=monitor_429, args=(discord_id, bot_name, process), daemon=True)
        monitor_thread.start()

        return process.pid, None
    except Exception as e:
        return None, str(e)

def stop_bot_process(discord_id, bot_name):
    bot_data = get_bot(discord_id, bot_name)
    if bot_data and bot_data["process_pid"]:
        pid = bot_data["process_pid"]
        try:
            os.kill(pid, signal.SIGKILL)
        except:
            pass
    key = f"{discord_id}_{bot_name}"
    if key in running_processes:
        try:
            running_processes[key].kill()
        except:
            pass
        del running_processes[key]
    bot_folder = os.path.abspath(f"./bots/{discord_id}/{bot_name}")
    try:
        result = subprocess.run(["pgrep", "-f", bot_folder], capture_output=True, text=True)
        if result.stdout.strip():
            for pid_str in result.stdout.strip().split("\n"):
                if pid_str:
                    try:
                        os.kill(int(pid_str), signal.SIGKILL)
                    except:
                        pass
    except:
        pass
    try:
        subprocess.run(["pkill", "-9", "-f", bot_folder], capture_output=True)
    except:
        pass

def is_process_running(discord_id, bot_name):
    bot_data = get_bot(discord_id, bot_name)
    if not bot_data or not bot_data["process_pid"]:
        return False
    try:
        os.kill(bot_data["process_pid"], 0)
        return True
    except:
        return False

# ==================== Discord Bot ====================
class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
    async def setup_hook(self):
        await self.tree.sync()
        print("✅ 斜線指令已同步")

bot = MyBot()

def is_owner():
    async def predicate(interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("❌ 你不是 Bot 擁有者，無法使用此指令", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

# ==================== 面板與視窗 ====================
class KeyInputModal(discord.ui.Modal, title="輸入你的專屬 Key"):
    key_input = discord.ui.TextInput(label="請輸入你的專屬 Key", placeholder="例如：DCBOT-ABCD1234", required=True, min_length=5, max_length=50)

    async def on_submit(self, interaction: discord.Interaction):
        key = self.key_input.value.strip().upper()
        user_id = interaction.user.id
        guild = interaction.guild
        existing = get_user(user_id)
        if existing and existing["channel_id"]:
            channel = guild.get_channel(existing["channel_id"])
            if channel:
                await interaction.response.send_message(f"⚠️ 你已經有專屬頻道了：{channel.mention}", ephemeral=True)
                return
        if not is_key_valid(key):
            await interaction.response.send_message("❌ Key 無效或已被使用！", ephemeral=True)
            return
        use_key(key, user_id)
        await create_user_channel(interaction, user_id, guild)

async def create_user_channel(interaction, user_id, guild):
    settings = get_server_settings(guild.id)
    if not settings or not settings["category_id"]:
        await interaction.response.send_message("❌ 此伺服器尚未設定託管分類", ephemeral=True)
        return
    category = guild.get_channel(settings["category_id"])
    if not category:
        await interaction.response.send_message("❌ 找不到託管分類", ephemeral=True)
        return
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, read_message_history=True, attach_files=True, send_messages=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=True)
    }
    channel = await guild.create_text_channel(name=f"🤖-{interaction.user.name}-託管", overwrites=overwrites, category=category)
    create_user(user_id, channel.id)
    await interaction.response.send_message(f"✅ 已建立專屬頻道：{channel.mention}\n上傳 .py 或 .js 檔案後用 `/查看我的機器人` 管理！", ephemeral=True)
    await channel.send(f"🎉 歡迎 {interaction.user.mention}！\n\n📌 **直接上傳 .py 或 .js 檔案**\n📌 可選配 `.env`、`requirements.txt`、`package.json`、`.json`、`.db` **一起上傳**\n📌 使用 `/查看我的機器人` 來管理")

class MainPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="開始使用", style=discord.ButtonStyle.success, emoji="🚀", custom_id="panel_start_button")
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        guild = interaction.guild
        existing = get_user(user_id)
        if existing:
            if existing["channel_id"]:
                old_channel = guild.get_channel(existing["channel_id"])
                if old_channel:
                    await interaction.response.send_message(f"⚠️ 你的專屬頻道還在：{old_channel.mention}", ephemeral=True)
                    return
            await create_user_channel(interaction, user_id, guild)
        else:
            modal = KeyInputModal()
            await interaction.response.send_modal(modal)

class RenameModal(discord.ui.Modal, title="重新命名機器人"):
    new_name = discord.ui.TextInput(label="請輸入新名稱", placeholder="中英文、數字、底線", required=True, min_length=1, max_length=50)
    def __init__(self, user_id, old_name):
        super().__init__()
        self.user_id = user_id
        self.old_name = old_name
    async def on_submit(self, interaction: discord.Interaction):
        new_name = self.new_name.value.strip()
        if not new_name.replace("_", "").isalnum():
            await interaction.response.send_message("❌ 名稱只能包含中英文、數字、底線", ephemeral=True)
            return
        bot_data = get_bot(self.user_id, self.old_name)
        if bot_data is None:
            await interaction.response.send_message("❌ 找不到此機器人", ephemeral=True)
            return
        if bot_data["bot_status"] == "online":
            await interaction.response.send_message("⚠️ 請先關閉機器人再改名", ephemeral=True)
            return
        if get_bot(self.user_id, new_name):
            await interaction.response.send_message(f"❌ `{new_name}` 已經存在", ephemeral=True)
            return
        rename_bot(self.user_id, self.old_name, new_name)
        await interaction.response.send_message(f"✅ `{self.old_name}` → `{new_name}`", ephemeral=True)

class TerminalView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=600)
        self.user_id = user_id
        bots = get_user_bots(user_id)
        for b in bots[:25]:
            self.add_item(TerminalSelectButton(user_id, b["bot_name"]))

class TerminalSelectButton(discord.ui.Button):
    def __init__(self, user_id, bot_name):
        super().__init__(label=bot_name[:80], style=discord.ButtonStyle.primary, emoji="📋")
        self.user_id = user_id
        self.bot_name = bot_name
    async def callback(self, interaction: discord.Interaction):
        log_path = f"./logs/{self.user_id}_{self.bot_name}.log"
        if os.path.exists(log_path):
            with open(log_path, 'r') as f:
                lines = f.readlines()[-200:]
                log_text = "".join(lines)
        else:
            log_text = "（無記錄）"
        embed = discord.Embed(title=f"📋 {self.bot_name} 終端記錄", color=discord.Color.blue())
        embed.description = f"```{log_text[:1900]}```"
        await interaction.response.edit_message(embed=embed, view=None)

class BotManageView(discord.ui.View):
    def __init__(self, user_id, bot_name):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.bot_name = bot_name

    @discord.ui.button(label="啟動機器人", style=discord.ButtonStyle.green, emoji="▶️")
    async def start_bot(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot_data = get_bot(self.user_id, self.bot_name)
        if bot_data is None:
            await interaction.response.send_message("❌ 找不到此機器人", ephemeral=True)
            return
        if bot_data["bot_status"] == "online":
            await interaction.response.send_message("⚠️ 已經在運作中！", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        pid, error = start_bot_process(self.user_id, self.bot_name)
        if error:
            await interaction.followup.send(f"❌ 啟動失敗：\n```{error[:500]}```", ephemeral=True)
        else:
            update_bot_status(self.user_id, self.bot_name, process_pid=pid, bot_status="online")
            user = get_user(self.user_id)
            if user:
                channel = bot.get_channel(user["channel_id"])
                if channel:
                    try:
                        await channel.send(f"✅ `{self.bot_name}` 已啟動！PID：`{pid}`")
                    except:
                        pass
            await interaction.followup.send(f"✅ `{self.bot_name}` 已啟動！", ephemeral=True)

    @discord.ui.button(label="關閉機器人", style=discord.ButtonStyle.red, emoji="⏹️")
    async def stop_bot(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot_data = get_bot(self.user_id, self.bot_name)
        if bot_data is None:
            await interaction.response.send_message("❌ 找不到此機器人", ephemeral=True)
            return
        if bot_data["bot_status"] == "offline":
            await interaction.response.send_message("⚠️ 已經是關閉狀態", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        stop_bot_process(self.user_id, self.bot_name)
        update_bot_status(self.user_id, self.bot_name, process_pid=None, bot_status="offline")
        user = get_user(self.user_id)
        if user:
            channel = bot.get_channel(user["channel_id"])
            if channel:
                try:
                    await channel.send(f"⏹️ `{self.bot_name}` 已關閉！")
                except:
                    pass
        await interaction.followup.send(f"✅ `{self.bot_name}` 已關閉！", ephemeral=True)

    @discord.ui.button(label="查看狀態", style=discord.ButtonStyle.blurple, emoji="📊")
    async def bot_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot_data = get_bot(self.user_id, self.bot_name)
        if bot_data is None:
            await interaction.response.send_message("❌ 找不到此機器人", ephemeral=True)
            return
        files = get_bot_files(self.user_id, self.bot_name)
        file_list = "\n".join([f"• `{f[0]}`" for f in files]) if files else "無"
        actual_running = is_process_running(self.user_id, self.bot_name)
        status_text = "🟢 運作中" if (bot_data["bot_status"] == "online" and actual_running) else "🔴 離線"
        embed = discord.Embed(title=f"🤖 {self.bot_name} 狀態", color=discord.Color.blue())
        embed.add_field(name="狀態", value=status_text, inline=True)
        embed.add_field(name="PID", value=f"`{bot_data['process_pid']}`" if bot_data["process_pid"] else "無", inline=True)
        embed.add_field(name="檔案", value=file_list, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="查看所有檔案", style=discord.ButtonStyle.blurple, emoji="📁")
    async def view_files(self, interaction: discord.Interaction, button: discord.ui.Button):
        files = get_bot_files(self.user_id, self.bot_name)
        if not files:
            await interaction.response.send_message("📭 此機器人沒有任何檔案", ephemeral=True)
            return

        existing_files = []
        for filename, file_path in files:
            if os.path.exists(file_path):
                file_size = os.path.getsize(file_path) / 1024
                existing_files.append((filename, file_path, file_size))

        if not existing_files:
            await interaction.response.send_message("📭 此機器人沒有任何檔案", ephemeral=True)
            return

        batch_size = 10
        total_batches = (len(existing_files) + batch_size - 1) // batch_size

        await interaction.response.defer(ephemeral=True)

        for batch_num in range(total_batches):
            start = batch_num * batch_size
            end = start + batch_size
            batch = existing_files[start:end]

            file_list = []
            discord_files = []

            for filename, file_path, file_size in batch:
                file_list.append(f"• `{filename}` ({file_size:.1f} KB)")
                discord_file = discord.File(file_path, filename=filename)
                discord_files.append(discord_file)

            embed = discord.Embed(
                title=f"📁 {self.bot_name} 的所有檔案" + (f"（{batch_num + 1}/{total_batches}）" if total_batches > 1 else ""),
                description="\n".join(file_list),
                color=discord.Color.blue()
            )
            embed.set_footer(text=f"第 {batch_num + 1} 批，共 {len(batch)} 個檔案")

            await interaction.followup.send(embed=embed, files=discord_files, ephemeral=True)

    @discord.ui.button(label="改名", style=discord.ButtonStyle.gray, emoji="✏️")
    async def rename_bot_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot_data = get_bot(self.user_id, self.bot_name)
        if bot_data is None:
            await interaction.response.send_message("❌ 找不到此機器人", ephemeral=True)
            return
        if bot_data["bot_status"] == "online":
            await interaction.response.send_message("⚠️ 請先關閉機器人再改名", ephemeral=True)
            return
        modal = RenameModal(self.user_id, self.bot_name)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="刪除此機器人", style=discord.ButtonStyle.red, emoji="🗑️")
    async def delete_bot_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot_data = get_bot(self.user_id, self.bot_name)
        if bot_data is None:
            await interaction.response.send_message("❌ 找不到此機器人", ephemeral=True)
            return
        if bot_data["bot_status"] == "online":
            await interaction.response.send_message("⚠️ 請先關閉機器人再刪除", ephemeral=True)
            return
        await interaction.response.defer()
        delete_bot(self.user_id, self.bot_name)
        bots = get_user_bots(self.user_id)
        if not bots:
            embed = discord.Embed(title="🤖 你的機器人列表", description=f"✅ `{self.bot_name}` 已刪除！\n\n目前沒有任何機器人", color=discord.Color.green())
            await interaction.edit_original_response(embed=embed, view=None)
        else:
            embed = discord.Embed(title="🤖 你的機器人列表", description=f"✅ `{self.bot_name}` 已刪除！\n\n選擇一個機器人來管理：", color=discord.Color.green())
            for b in bots:
                status = "🟢" if b["bot_status"] == "online" else "🔴"
                embed.add_field(name=f"{status} {b['bot_name']}", value=f"PID: `{b['process_pid'] or '無'}`", inline=False)
            view = BotListView(self.user_id, bots)
            await interaction.edit_original_response(embed=embed, view=view)

    @discord.ui.button(label="回到機器人列表", style=discord.ButtonStyle.secondary, emoji="🔙")
    async def back_to_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_bot_list(interaction, self.user_id)

async def show_bot_list(interaction, user_id):
    bots = get_user_bots(user_id)
    if not bots:
        embed = discord.Embed(title="🤖 你的機器人列表", description="目前沒有任何機器人\n\n上傳 .py 或 .js 檔案來自動建立！", color=discord.Color.green())
        await interaction.response.edit_message(embed=embed, view=None)
        return
    embed = discord.Embed(title="🤖 你的機器人列表", description="選擇一個機器人來管理：", color=discord.Color.green())
    for b in bots:
        status = "🟢" if b["bot_status"] == "online" else "🔴"
        embed.add_field(name=f"{status} {b['bot_name']}", value=f"PID: `{b['process_pid'] or '無'}`", inline=False)
    view = BotListView(user_id, bots)
    await interaction.response.edit_message(embed=embed, view=view)

class BotListView(discord.ui.View):
    def __init__(self, user_id, bots):
        super().__init__(timeout=600)
        self.user_id = user_id
        for b in bots[:25]:
            status_emoji = "🟢 " if b["bot_status"] == "online" else ""
            self.add_item(BotSelectButton(user_id, b["bot_name"], f"{status_emoji}{b['bot_name']}"))

class BotSelectButton(discord.ui.Button):
    def __init__(self, user_id, bot_name, label):
        super().__init__(label=label[:80], style=discord.ButtonStyle.primary)
        self.user_id = user_id
        self.bot_name = bot_name
    async def callback(self, interaction: discord.Interaction):
        embed = discord.Embed(title=f"🤖 管理：{self.bot_name}", description="選擇操作：", color=discord.Color.blue())
        await interaction.response.edit_message(embed=embed, view=BotManageView(self.user_id, self.bot_name))

# ==================== 指令 ====================
@bot.tree.command(name="設定託管分類", description="【擁有者】設定此伺服器的託管分類")
@is_owner()
@app_commands.describe(category="選擇要當託管專區的分類")
async def set_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    await interaction.response.defer(ephemeral=True)
    set_server_settings(interaction.guild_id, category_id=category.id)
    await interaction.followup.send(f"✅ 已將託管分類設定為：{category.name}", ephemeral=True)

@bot.tree.command(name="設定面板頻道", description="【擁有者】設定此伺服器的面板頻道")
@is_owner()
@app_commands.describe(channel="選擇要放公開面板的頻道")
async def set_panel_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    set_server_settings(interaction.guild_id, panel_channel_id=channel.id)
    await interaction.followup.send(f"✅ 已將面板頻道設定為：{channel.mention}", ephemeral=True)

@bot.tree.command(name="啟用託管頻道", description="【擁有者】發送公開的託管申請面板")
@is_owner()
async def activate_panel(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    settings = get_server_settings(interaction.guild_id)
    if not settings or not settings["panel_channel_id"]:
        await interaction.followup.send("❌ 請先用 `/設定面板頻道` 設定面板要發送的頻道", ephemeral=True)
        return
    channel = bot.get_channel(settings["panel_channel_id"])
    if channel is None:
        await interaction.followup.send("❌ 找不到面板頻道", ephemeral=True)
        return
    embed = discord.Embed(title="🤖 機器人託管系統", description="歡迎使用機器人託管服務！\n\n**使用步驟：**\n1️⃣ 點擊下方按鈕\n2️⃣ 輸入你的專屬 Key\n3️⃣ 系統會自動為你建立專屬頻道\n4️⃣ 上傳你的 .py 或 .js 檔案即可開始託管\n\n⚠️ 沒有 Key？請聯繫管理員取得！", color=discord.Color.green())
    view = MainPanelView()
    await channel.send(embed=embed, view=view)
    await interaction.followup.send(f"✅ 已於 {channel.mention} 發送託管面板", ephemeral=True)

@bot.tree.command(name="設定key", description="【擁有者】產生新的託管 Key")
@is_owner()
async def set_key(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    key = create_key_in_db(interaction.user.id)
    embed = discord.Embed(title="🔑 新的託管 Key 已產生", description=f"```{key}```\n⚠️ 此 Key 只能使用一次。", color=discord.Color.gold())
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="取消資格", description="【擁有者】取消用戶的託管資格")
@is_owner()
@app_commands.describe(user="要取消資格的使用者")
async def revoke_access(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    target = get_user(user.id)
    if not target:
        await interaction.followup.send(f"❌ {user.mention} 沒有託管資料", ephemeral=True)
        return
    channel = interaction.guild.get_channel(target["channel_id"])
    if channel:
        try:
            await channel.delete(reason=f"擁有者 {interaction.user.name} 取消託管資格")
        except:
            pass
    delete_user(user.id)
    await interaction.followup.send(f"✅ 已取消 {user.mention} 的託管資格\n• 已停止所有機器人\n• 已刪除專屬頻道\n• 檔案已保留", ephemeral=True)
    try:
        await user.send("⚠️ 你的機器人託管資格已被取消，如有疑問請聯繫管理員。")
    except:
        pass

@bot.tree.command(name="查看我的機器人", description="查看你的機器人管理面板")
async def my_bot(interaction: discord.Interaction):
    user = get_user(interaction.user.id)
    if not user:
        await interaction.response.send_message("❌ 你還沒有啟用託管服務！\n請先在公開面板輸入 Key 來開通。", ephemeral=True)
        return
    bots = get_user_bots(interaction.user.id)
    if not bots:
        embed = discord.Embed(title="🤖 你的機器人列表", description="目前沒有任何機器人\n\n上傳 .py 或 .js 檔案來自動建立！", color=discord.Color.green())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    embed = discord.Embed(title="🤖 你的機器人列表", description="選擇一個機器人來管理：", color=discord.Color.green())
    for b in bots:
        status = "🟢" if b["bot_status"] == "online" else "🔴"
        embed.add_field(name=f"{status} {b['bot_name']}", value=f"PID: `{b['process_pid'] or '無'}`", inline=False)
    view = BotListView(interaction.user.id, bots)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="查看所有key", description="【擁有者】查看所有未使用的 Key")
@is_owner()
async def list_keys(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    keys = get_all_unused_keys()
    if not keys:
        await interaction.followup.send("📭 目前沒有未使用的 Key", ephemeral=True)
        return
    msg = "**🔑 未使用的 Key 列表：**\n\n"
    for key, created_by, created_at in keys:
        creator = await bot.fetch_user(created_by)
        creator_name = creator.name if creator else "未知"
        msg += f"• `{key}`\n  └ 建立者：{creator_name} | {created_at}\n\n"
    await interaction.followup.send(msg, ephemeral=True)

@bot.tree.command(name="清理幽靈頻道", description="【擁有者】清除頻道已不存在的用戶資料")
@is_owner()
async def clean_ghost_channels(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT discord_id, channel_id FROM users")
        all_users = cursor.fetchall()
    finally:
        conn.close()
    cleaned = 0
    for discord_id, channel_id in all_users:
        channel = interaction.guild.get_channel(channel_id)
        if channel is None:
            delete_user(discord_id)
            cleaned += 1
    if cleaned == 0:
        await interaction.followup.send("✅ 沒有發現幽靈頻道！", ephemeral=True)
    else:
        await interaction.followup.send(f"✅ 已清理 {cleaned} 個幽靈用戶！", ephemeral=True)

@bot.tree.command(name="查看終端機", description="查看你的機器人終端輸出")
async def view_terminal(interaction: discord.Interaction):
    user = get_user(interaction.user.id)
    if not user:
        await interaction.response.send_message("❌ 你還沒有啟用託管服務！", ephemeral=True)
        return
    bots = get_user_bots(interaction.user.id)
    if not bots:
        await interaction.response.send_message("❌ 你沒有任何機器人", ephemeral=True)
        return
    embed = discord.Embed(title="📋 選擇要查看的機器人", description="點擊按鈕查看終端記錄：", color=discord.Color.green())
    view = TerminalView(interaction.user.id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="查看別人的終端機", description="【擁有者】查看指定用戶的機器人終端輸出")
@is_owner()
@app_commands.describe(user="要查看的使用者")
async def view_other_terminal(interaction: discord.Interaction, user: discord.Member):
    bots = get_user_bots(user.id)
    if not bots:
        await interaction.response.send_message(f"❌ {user.name} 沒有機器人", ephemeral=True)
        return
    embed = discord.Embed(title=f"📋 選擇要查看 {user.name} 的機器人", description="點擊按鈕查看終端記錄：", color=discord.Color.green())
    view = TerminalView(user.id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="加購機器人數量", description="【擁有者】增加用戶的機器人上限")
@is_owner()
@app_commands.describe(user="要加購的使用者", amount="要增加的數量")
async def add_bot_quota(interaction: discord.Interaction, user: discord.Member, amount: int):
    await interaction.response.defer(ephemeral=True)
    if amount <= 0:
        await interaction.followup.send("❌ 數量必須大於 0", ephemeral=True)
        return
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM bot_quota WHERE discord_id = ?", (user.id,))
        current = cursor.fetchone()
        if current:
            new_quota = current[1] + amount
            cursor.execute("UPDATE bot_quota SET extra_quota = ? WHERE discord_id = ?", (new_quota, user.id))
        else:
            cursor.execute("INSERT INTO bot_quota (discord_id, extra_quota) VALUES (?, ?)", (user.id, amount))
            new_quota = amount
        conn.commit()
    finally:
        conn.close()
    total_limit = MAX_BOTS_PER_USER + new_quota
    await interaction.followup.send(f"✅ 已為 {user.mention} 增加 {amount} 台上限！\n目前額外配額：{new_quota} 台\n總上限：{total_limit} 台", ephemeral=True)

@bot.tree.command(name="減少機器人數量", description="【擁有者】減少用戶的機器人上限")
@is_owner()
@app_commands.describe(user="要減少的使用者", amount="要減少的數量")
async def remove_bot_quota(interaction: discord.Interaction, user: discord.Member, amount: int):
    await interaction.response.defer(ephemeral=True)
    if amount <= 0:
        await interaction.followup.send("❌ 數量必須大於 0", ephemeral=True)
        return
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT extra_quota FROM bot_quota WHERE discord_id = ?", (user.id,))
        current = cursor.fetchone()
        current_extra = current[0] if current else 0
        if current_extra < amount:
            await interaction.followup.send(f"❌ {user.mention} 的額外配額只有 {current_extra} 台，無法減少 {amount} 台", ephemeral=True)
            return
        new_quota = current_extra - amount
        if current:
            cursor.execute("UPDATE bot_quota SET extra_quota = ? WHERE discord_id = ?", (new_quota, user.id))
        else:
            cursor.execute("INSERT INTO bot_quota (discord_id, extra_quota) VALUES (?, 0)", (user.id,))
        conn.commit()
    finally:
        conn.close()
    total_limit = MAX_BOTS_PER_USER + new_quota
    await interaction.followup.send(f"✅ 已將 {user.mention} 的上限減少 {amount} 台！\n目前額外配額：{new_quota} 台\n總上限：{total_limit} 台", ephemeral=True)

# ==================== 檔案上傳 ====================
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    user = get_user(message.author.id)
    if not user or message.channel.id != user["channel_id"]:
        return

    if message.attachments:
        main_file = None
        has_env = False
        has_req = False
        has_pkg = False
        has_data = False

        for attachment in message.attachments:
            name = attachment.filename
            if name.endswith('.py') or name.endswith('.js'):
                main_file = name
            elif name == '.env':
                has_env = True
            elif name == 'requirements.txt':
                has_req = True
            elif name == 'package.json':
                has_pkg = True
            elif name.endswith('.json') or name.endswith('.db'):
                has_data = True

        if not main_file:
            if has_data:
                user_bots = get_user_bots(message.author.id)
                if user_bots:
                    latest_bot = user_bots[0]["bot_name"]
                    saved = []
                    for attachment in message.attachments:
                        if attachment.filename.endswith('.json') or attachment.filename.endswith('.db'):
                            save_path = f"./bots/{message.author.id}/{latest_bot}/{attachment.filename}"
                            os.makedirs(os.path.dirname(save_path), exist_ok=True)
                            await attachment.save(save_path)
                            add_file(message.author.id, latest_bot, attachment.filename, save_path)
                            saved.append(attachment.filename)
                    await message.channel.send(f"✅ 資料檔已更新到 `{latest_bot}`：{', '.join([f'`{f}`' for f in saved])}")
                else:
                    await message.channel.send("⚠️ 請先上傳主程式再傳資料檔！")
                return
            else:
                await message.channel.send("⚠️ 請將 `.env` / `requirements.txt` / `package.json` 跟 `.py` 或 `.js` **一起上傳**！")
                return

        bot_name = main_file[:-3] if main_file.endswith(('.py', '.js')) else main_file

        user_bots = get_user_bots(message.author.id)
        extra_quota = get_extra_quota(message.author.id)
        total_limit = MAX_BOTS_PER_USER + extra_quota

        if not is_owner_id(message.author.id) and len(user_bots) >= total_limit:
            await message.channel.send(f"❌ 你最多只能託管 {total_limit} 台機器人！\n如需更多請聯繫管理員。")
            return

        if not get_bot(message.author.id, bot_name):
            create_bot(message.author.id, bot_name)

        saved = []
        for attachment in message.attachments:
            save_path = f"./bots/{message.author.id}/{bot_name}/{attachment.filename}"
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            await attachment.save(save_path)
            add_file(message.author.id, bot_name, attachment.filename, save_path)
            saved.append(attachment.filename)

        msg_parts = []
        if main_file:
            msg_parts.append(f"主程式：`{main_file}`")
        if has_env:
            msg_parts.append(f"環境變數：`.env`")
        if has_req:
            msg_parts.append(f"Python 依賴：`requirements.txt`")
        if has_pkg:
            msg_parts.append(f"Node.js 依賴：`package.json`")
        if has_data:
            msg_parts.append(f"資料檔：`.json` / `.db`")

        await message.channel.send(f"✅ `{bot_name}` 已建立！\n" + "\n".join(msg_parts))

    await bot.process_commands(message)

# ==================== 啟動 ====================
@bot.event
async def on_ready():
    print(f"✅ {bot.user} 已上線")
    bot.add_view(MainPanelView())

    activity = discord.Streaming(
        name="託管運行中",
        url="https://discord.gg/VqjAfdrHsm"
    )
    await bot.change_presence(activity=activity)

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT discord_id, bot_name, process_pid FROM bots WHERE bot_status = 'online'")
        online_bots = cursor.fetchall()
    finally:
        conn.close()

    offline_bots = []
    for discord_id, bot_name, pid in online_bots:
        if pid:
            try:
                os.kill(pid, 0)
            except:
                offline_bots.append((discord_id, bot_name))
                update_bot_status(discord_id, bot_name, bot_status="offline")
                print(f"⚠️ {discord_id} 的 {bot_name} 已離線")

    if offline_bots:
        print(f"🔄 正在自動重啟 {len(offline_bots)} 個 Bot...")
        for discord_id, bot_name in offline_bots:
            pid, error = start_bot_process(discord_id, bot_name)
            if error:
                print(f"❌ {bot_name} 重啟失敗：{error}")
                user = get_user(discord_id)
                if user:
                    channel = bot.get_channel(user["channel_id"])
                    if channel:
                        try:
                            await channel.send(f"⚠️ `{bot_name}` 重啟失敗：{error}")
                        except:
                            pass
            else:
                update_bot_status(discord_id, bot_name, process_pid=pid, bot_status="online")
                print(f"✅ {bot_name} 已自動重啟！PID：{pid}")
                user = get_user(discord_id)
                if user:
                    channel = bot.get_channel(user["channel_id"])
                    if channel:
                        try:
                            await channel.send(f"🔄 系統重啟，`{bot_name}` 已自動恢復運作！")
                        except:
                            pass
        print("✅ 自動重啟完成")

bot.run(TOKEN)
