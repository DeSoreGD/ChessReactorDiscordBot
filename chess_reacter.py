# Final updated bot: interactive leaderboard with counts and nicer profile
# - Top (`!top`) shows per-user counts for brilliant, blunder and criminal under each entry
# - Only author or server admins can use pagination buttons
# - Profile (`!profile`) shows emoji next to each category for readability
# - Modernized embeds for `!top` and `!profile`
# - Fixes: Now correctly shows only members from the server where the command was called.

import os
import time
import random
import logging
import sqlite3
import math
from typing import Optional
from dotenv import load_dotenv
import discord
from discord.ext import commands
from discord import Embed
from discord.ui import View, button
import re

load_dotenv()

# ---------- Config ----------
TOKEN = os.getenv("BOT_TOKEN")
REACTION_PROBABILITY = float(os.getenv("REACTION_PROBABILITY", "0.02"))
CHANNEL_COOLDOWN_SECONDS = int(os.getenv("CHANNEL_COOLDOWN_SECONDS", "30"))
DB_PATH = os.getenv("DB_PATH", "chess_stats.db")
PER_PAGE = int(os.getenv("PER_PAGE", "10"))
PAGINATION_TIMEOUT = int(os.getenv("PAGINATION_TIMEOUT", "120"))
INITIAL_SCORE = int(os.getenv("INITIAL_SCORE", "0"))

# raw emoji strings -> category
# ----------------------------------------------------------------------------------  CHANGE EMOJI ID HERE
EMOJI_MAP = {
    "<:best:1408362375433945138>": "best",
    "<:blunder:1408362359919214592>": "blunder",
    "<:book:1408362344593227867>": "book",
    "<:brilliant:1408362078024110151>": "brilliant",
    "<:correct:1408362111771607100>": "correct",
    "<:excellent:1408362398531977327>": "excellent",
    "<:forced:1408362408682061924>": "forced",
    "<:good:1408362441611415582>": "good",
    "<:great:1408362430870061087>": "great",
    "<:inaccuracy:1408362419696439359>": "inaccuracy",
    "<:miss:1408362387752353833>": "miss",
    "<:mistake:1408362168478466180>": "mistake",
    "<:criminal:1408360614547427418>": "criminal",
}

# mapping category -> display emoji (raw) and readable name
# ----------------------------------------------------------------------------------  CHANGE EMOJI ID HERE
CATEGORY_DISPLAY = {
    "best": ("<:best:1408362375433945138>", "Best"),
    "brilliant": ("<:brilliant:1408362078024110151>", "Brilliant"),
    "excellent": ("<:excellent:1408362398531977327>", "Excellent"),
    "great": ("<:great:1408362430870061087>", "Great"),
    "good": ("<:good:1408362441611415582>", "Good"),
    "correct": ("<:correct:1408362111771607100>", "Correct"),
    "book": ("<:book:1408362344593227867>", "Book"),
    "forced": ("<:forced:1408362408682061924>", "Forced"),
    "inaccuracy": ("<:inaccuracy:1408362419696439359>", "Inaccuracy"),
    "mistake": ("<:mistake:1408362168478466180>", "Mistake"),
    "miss": ("<:miss:1408362387752353833>", "Miss"),
    "blunder": ("<:blunder:1408362359919214592>", "Blunder"),
    "criminal": ("<:criminal:1408360614547427418>", "Criminal"),
}

SCORE_WEIGHTS = {
    "best": 50,
    "brilliant": 35,
    "excellent": 20,
    "great": 15,
    "good": 10,
    "correct": 5,
    "book": 5,
    "forced": 8,
    "inaccuracy": -5,
    "mistake": -15,
    "miss": -25,
    "blunder": -40,
    "criminal": -60,
}

CATEGORY_COLUMNS = list(SCORE_WEIGHTS.keys())

# optional channels to ignore
IGNORE_CHANNELS = set()
if os.getenv("IGNORE_CHANNELS"):
    IGNORE_CHANNELS = {int(x.strip()) for x in os.getenv("IGNORE_CHANNELS").split(",") if x.strip()}

# ---------- Logging & Bot ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("chess_leaderboard")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True # Необходим для получения display_name
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

_last_reaction_at = {}

# ---------- Database ----------
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()

def init_db():
    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER NOT NULL,
        guild_id INTEGER NOT NULL,
        username TEXT,
        score INTEGER DEFAULT {INITIAL_SCORE},
        best INTEGER DEFAULT 0,
        blunder INTEGER DEFAULT 0,
        book INTEGER DEFAULT 0,
        brilliant INTEGER DEFAULT 0,
        correct INTEGER DEFAULT 0,
        excellent INTEGER DEFAULT 0,
        forced INTEGER DEFAULT 0,
        good INTEGER DEFAULT 0,
        great INTEGER DEFAULT 0,
        inaccuracy INTEGER DEFAULT 0,
        miss INTEGER DEFAULT 0,
        mistake INTEGER DEFAULT 0,
        criminal INTEGER DEFAULT 0,
        PRIMARY KEY (user_id, guild_id)
    )
    """)
    try:
        cur.execute("ALTER TABLE users ADD COLUMN username TEXT;")
    except sqlite3.OperationalError:
        pass
    conn.commit()

init_db()


def ensure_user(user_id: int, guild_id: int, username: str):
    cur.execute("SELECT user_id FROM users WHERE user_id = ? AND guild_id = ?", (user_id, guild_id))
    if cur.fetchone() is None:
        cur.execute("INSERT INTO users (user_id, guild_id, username, score) VALUES (?, ?, ?, ?)", (user_id, guild_id, username, INITIAL_SCORE))
    else:
        cur.execute("UPDATE users SET username = ? WHERE user_id = ? AND guild_id = ?", (username, user_id, guild_id))
    conn.commit()


def record_reaction(user_id: int, guild_id: int, username: str, category: str):
    ensure_user(user_id, guild_id, username)
    if category not in CATEGORY_COLUMNS:
        return 0
    cur.execute(f"UPDATE users SET {category} = {category} + 1 WHERE user_id = ? AND guild_id = ?", (user_id, guild_id))
    delta = SCORE_WEIGHTS.get(category, 0)
    cur.execute("UPDATE users SET score = score + ? WHERE user_id = ? AND guild_id = ?", (delta, user_id, guild_id))
    cur.execute("UPDATE users SET username = ? WHERE user_id = ? AND guild_id = ?", (username, user_id, guild_id))
    conn.commit()
    return delta


def get_total_users_count(guild_id: int):
    cur.execute("SELECT COUNT(*) FROM users WHERE guild_id = ?", (guild_id,))
    return cur.fetchone()[0] or 0


def get_top_by_score(guild_id: int, limit: int = PER_PAGE, offset: int = 0):
    cur.execute(
        "SELECT user_id, score, brilliant, blunder, criminal FROM users "
        "WHERE guild_id = ? ORDER BY score DESC LIMIT ? OFFSET ?",
        (guild_id, limit, offset),
    )
    return cur.fetchall()


def get_top_by_category(guild_id: int, category: str, limit: int = PER_PAGE, offset: int = 0):
    if category not in CATEGORY_COLUMNS:
        return []
    cur.execute(
        f"SELECT user_id, {category}, brilliant, blunder, criminal FROM users "
        f"WHERE guild_id = ? AND {category} > 0 ORDER BY {category} DESC LIMIT ? OFFSET ?",
        (guild_id, limit, offset),
    )
    return cur.fetchall()


def get_user_stats(user_id: int, guild_id: int):
    cur.execute("SELECT * FROM users WHERE user_id = ? AND guild_id = ?", (user_id, guild_id))
    return cur.fetchone()


def reset_stats_db():
    cur.execute("DELETE FROM users")
    conn.commit()

# ---------- Emoji resolver ----------
EMOJI_ID_RE = re.compile(r":(\d+)>$")

def resolve_emoji(obj, raw: str):
    m = EMOJI_ID_RE.search(raw)
    if not m:
        return raw
    try:
        emoji_id = int(m.group(1))
    except Exception:
        return raw
    emo = obj.get_emoji(emoji_id)
    return emo if emo is not None else raw

EMOJI_STRINGS = list(EMOJI_MAP.keys())

# ---------- Bot events ----------
@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (id: {bot.user.id})")


@bot.event
async def on_message(message: discord.Message):
    global REACTION_PROBABILITY
    if message.author.bot or message.guild is None:
        return
    if message.channel.id in IGNORE_CHANNELS:
        return
    if not message.content:
        return

    now = time.time()
    last = _last_reaction_at.get(message.channel.id, 0)
    if now - last < CHANNEL_COOLDOWN_SECONDS:
        await bot.process_commands(message)
        return

    if random.random() <= REACTION_PROBABILITY:
        emoji_raw = random.choice(EMOJI_STRINGS)
        emoji = resolve_emoji(bot, emoji_raw)
        try:
            await message.add_reaction(emoji)
            _last_reaction_at[message.channel.id] = now
            logger.info(f"Reacted to {message.id} with {emoji_raw}")
            category = EMOJI_MAP.get(emoji_raw)
            if category:
                delta = record_reaction(message.author.id, message.guild.id, message.author.display_name, category)
                logger.info(f"Recorded {category} for {message.author} (delta {delta}) on guild {message.guild.id}")
        except discord.Forbidden:
            logger.warning(f"Forbidden: missing Add Reactions permission in channel {message.channel.id}")
        except discord.HTTPException as e:
            logger.exception(f"HTTPException while adding reaction: {e}")

    await bot.process_commands(message)

# ---------- Pagination view ----------
class LeaderboardView(View):
    def __init__(self, ctx, category: Optional[str], total_items: int, per_page: int = PER_PAGE):
        super().__init__(timeout=PAGINATION_TIMEOUT)
        self.ctx = ctx
        self.guild_id = ctx.guild.id
        self.category = category
        self.per_page = per_page
        self.total_items = total_items
        self.total_pages = max(1, math.ceil(total_items / per_page))
        self.current_page = 1
        self.message = None
        self.update_buttons()

    def update_buttons(self):
        self.first.disabled = self.current_page == 1
        self.prev.disabled = self.current_page == 1
        self.next.disabled = self.current_page == self.total_pages
        self.last.disabled = self.current_page == self.total_pages

    def make_embed(self):
        offset = (self.current_page - 1) * self.per_page
        if self.category is None:
            rows = get_top_by_score(self.guild_id, limit=self.per_page, offset=offset)
            title = "Топ по рейтингу"
        else:
            rows = get_top_by_category(self.guild_id, self.category, limit=self.per_page, offset=offset)
            title = f"Топ по `{self.category}`"

        emb = discord.Embed(title=title, color=0x2DD4BF)
        start_rank = offset + 1
        if not rows:
            emb.description = "Нет данных для отображения."
        else:
            lines = []
            for i, row in enumerate(rows, start=start_rank):
                uid = row[0]
                val = row[1]
                brilliant_cnt = row[2]
                blunder_cnt = row[3]
                criminal_cnt = row[4]

                member = self.ctx.guild.get_member(uid)
                username = member.display_name if member else "Неизвестный пользователь"

                medal = ""
                if i == 1:
                    medal = "🥇 "
                elif i == 2:
                    medal = "🥈 "
                elif i == 3:
                    medal = "🥉 "

                b_emo = CATEGORY_DISPLAY["brilliant"][0]
                bl_emo = CATEGORY_DISPLAY["blunder"][0]
                cr_emo = CATEGORY_DISPLAY["criminal"][0]
                counts_line = f"   {b_emo} {brilliant_cnt}  {bl_emo} {blunder_cnt}  {cr_emo} {criminal_cnt}"

                lines.append(f"**{i}.** {medal}{username} — **{val}**\n{counts_line}")
            emb.description = "\n\n".join(lines)

        if self.ctx.guild and self.ctx.guild.icon:
            emb.set_thumbnail(url=self.ctx.guild.icon.url)

        emb.set_footer(text=f"Страница {self.current_page}/{self.total_pages} • Всего пользователей: {self.total_items}")
        return emb

    def _is_allowed(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.ctx.author.id:
            return True
        if interaction.user.guild is None:
            return False
        if interaction.user.guild_permissions.administrator:
            return True
        return False

    async def update_message(self, interaction: discord.Interaction):
        if self.category is None:
            self.total_items = get_total_users_count(self.guild_id)
        else:
            cur.execute(f"SELECT COUNT(*) FROM users WHERE guild_id = ? AND {self.category} > 0", (self.guild_id,))
            self.total_items = cur.fetchone()[0] or 0
        self.total_pages = max(1, math.ceil(self.total_items / self.per_page))
        if self.current_page > self.total_pages:
            self.current_page = self.total_pages
            
        self.update_buttons()
        embed = self.make_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    @button(label="⏮", style=discord.ButtonStyle.secondary)
    async def first(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_allowed(interaction):
            await interaction.response.send_message("Только автор команды или админ может листать топ.", ephemeral=True)
            return
        self.current_page = 1
        await self.update_message(interaction)

    @button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_allowed(interaction):
            await interaction.response.send_message("Только автор команды или админ может листать топ.", ephemeral=True)
            return
        if self.current_page > 1:
            self.current_page -= 1
        await self.update_message(interaction)

    @button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_allowed(interaction):
            await interaction.response.send_message("Только автор команды или админ может листать топ.", ephemeral=True)
            return
        if self.current_page < self.total_pages:
            self.current_page += 1
        await self.update_message(interaction)

    @button(label="⏭", style=discord.ButtonStyle.secondary)
    async def last(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_allowed(interaction):
            await interaction.response.send_message("Только автор команды или админ может листать топ.", ephemeral=True)
            return
        self.current_page = self.total_pages
        await self.update_message(interaction)

    @button(label="❌", style=discord.ButtonStyle.danger)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_allowed(interaction):
            await interaction.response.send_message("Только автор команды или админ может закрыть интерфейс.", ephemeral=True)
            return
        
        await interaction.response.defer()
        await interaction.message.delete()
        self.stop()

# ---------- Commands ----------
@bot.command(name="top")
async def top(ctx, arg: Optional[str] = None):
    """!top -> top by score; !top <category> -> top by category"""
    if ctx.guild is None:
        await ctx.send("Эту команду можно использовать только на сервере.")
        return
    category = None
    if arg is not None:
        if arg.lower() not in CATEGORY_COLUMNS:
            await ctx.send(f"Неизвестная категория `{arg}`. Допустимые: {', '.join(CATEGORY_COLUMNS)}")
            return
        category = arg.lower()

    if category is None:
        total = get_total_users_count(ctx.guild.id)
    else:
        cur.execute(f"SELECT COUNT(*) FROM users WHERE guild_id = ? AND {category} > 0", (ctx.guild.id,))
        total = cur.fetchone()[0] or 0
    
    view = LeaderboardView(ctx, category, total, per_page=PER_PAGE)
    embed = view.make_embed()
    
    msg = await ctx.send(embed=embed, view=view)
    view.message = msg

@bot.command(name="profile")
async def profile(ctx, member: Optional[discord.Member] = None):
    """!profile -> your stats; !profile @user -> that user's stats"""
    if ctx.guild is None:
        await ctx.send("Эту команду можно использовать только на сервере.")
        return
    if member is None:
        member = ctx.author
    stats = get_user_stats(member.id, ctx.guild.id)
    if not stats:
        await ctx.send(f"У {member.display_name} нет статистики пока.")
        return

    cols_info = cur.execute('PRAGMA table_info(users)').fetchall()
    cols = [c[1] for c in cols_info]
    colvals = dict(zip(cols, stats))

    emb = Embed(title=f"Статистика {member.display_name}", color=0xFFD166)
    emb.add_field(name="Рейтинг", value=str(colvals.get('score', INITIAL_SCORE)), inline=False)

    cat_lines = []
    for cat in CATEGORY_COLUMNS:
        emo = CATEGORY_DISPLAY.get(cat, (None, cat))[0]
        pretty = CATEGORY_DISPLAY.get(cat, (None, cat.capitalize()))[1]
        count = colvals.get(cat, 0)
        cat_lines.append(f"{emo} **{pretty}**: {count}")

    emb.add_field(name="Категории", value="\n".join(cat_lines), inline=False)
    try:
        if member.display_avatar:
            emb.set_thumbnail(url=member.display_avatar.url)
    except Exception:
        pass

    await ctx.send(embed=emb)

@bot.command(name="reset_stats")
@commands.has_permissions(administrator=True)
async def _reset_stats(ctx):
    reset_stats_db()
    await ctx.send("✅ Статистика сброшена.")

@bot.command(name="setprob")
@commands.has_permissions(administrator=True)
async def setprob(ctx, prob: float):
    global REACTION_PROBABILITY
    if prob < 0 or prob > 1:
        await ctx.send("Probability must be between 0.0 and 1.0")
        return
    REACTION_PROBABILITY = prob
    await ctx.send(f"✅ REACTION_PROBABILITY set to {REACTION_PROBABILITY}")

@bot.command(name="helpme")
async def helpme(ctx):
    emb = Embed(title="Команды бота", color=0x89CFF0)
    emb.add_field(name="!top", value="Показывает топ по рейтингу. `!top <category>` — топ по категории.", inline=False)
    emb.add_field(name="!profile [@user]", value="Показывает статистику пользователя с эмодзи для каждой категории.", inline=False)
    emb.add_field(name="!setprob <0.0-1.0>", value="(Админ) Установить вероятность реакции.", inline=False)
    emb.add_field(name="!reset_stats", value="(Админ) Сбросить статистику.", inline=False)
    await ctx.send(embed=emb)

# ---------- Run ----------
if __name__ == "__main__":
    if not TOKEN:
        logger.error("BOT_TOKEN missing in .env")
        raise SystemExit(1)
    bot.run(TOKEN)