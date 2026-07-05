import os
import sqlite3
import asyncio
import json
import io
import aiohttp
import re
from pathlib import Path
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
_data_dir = Path("/data") if Path("/data").exists() else Path(".")
DB_PATH = _data_dir / "bot.db"
WAITLIST_FILE = str(_data_dir / "waitlists.json")

print("DB PATH:", DB_PATH)
print("WAITLIST FILE:", WAITLIST_FILE)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ———————————————––
# Constants
# ———————————————––

CHECK = "<a:0000:1488556886918824068>"

THEMES = {
    "pink": 0xF7CFE3,
    "blue": 0xCFEFFF,
    "mint": 0xD8F5E3,
    "lavender": 0xE7D9FF,
    "white": 0xF2F2F2,
    "peach": 0xFFD9C7,
}

SETTINGS_COLUMNS = [
    "welcome_text", "welcome_title", "welcome_banner_url", "welcome_theme", "welcome_thumbnail_url", "welcome_banner2_url",
    "welcome_outside_text",
    "verify_role_id", "verify_message_id", "verify_channel_id", "verify_button_label", "verify_button_emoji",
    "verify_title", "verify_description", "verify_color", "verify_image_url", "verify_thumbnail_url",
    "verify_success_message", "verify_already_message",
    "boost_channel_id", "boost_text", "boost_outside_text", "boost_title", "boost_color", "boost_image_url", "boost_thumbnail_url", "boost_use_avatar",
    "vouch_channel_id", "vouch_reaction_emoji", "vouch_super_reaction",
]

# ———————————————––
# Database Helpers
# ———————————————––

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            guild_id INTEGER PRIMARY KEY,
            welcome_channel_id INTEGER,
            welcome_banner_url TEXT,
            welcome_theme TEXT DEFAULT 'pink',
            welcome_text TEXT,
            boost_channel_id INTEGER,
            boost_text TEXT,
            boost_title TEXT,
            boost_color TEXT,
            boost_image_url TEXT,
            boost_thumbnail_url TEXT
        )
        """
    )

    for col in SETTINGS_COLUMNS:
        try:
            cur.execute(f"ALTER TABLE settings ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS saved_embeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            embed_title TEXT,
            description TEXT,
            theme TEXT DEFAULT 'pink',
            image_url TEXT,
            thumbnail_url TEXT,
            use_avatar INTEGER DEFAULT 0,
            post_channel_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(guild_id, name)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS embed_collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            embed_names TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(guild_id, name)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sticky_messages (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            last_message_id INTEGER,
            PRIMARY KEY (guild_id, channel_id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS autoresponders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            trigger TEXT NOT NULL,
            message TEXT NOT NULL,
            ping_roles TEXT,
            match_type TEXT DEFAULT 'exact',
            UNIQUE(guild_id, trigger)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS image_responders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            trigger TEXT NOT NULL,
            image_url TEXT NOT NULL,
            caption TEXT,
            match_type TEXT DEFAULT 'exact',
            UNIQUE(guild_id, trigger)
        )
        """
    )

    conn.commit()
    conn.close()


def upsert_settings(guild_id: int, **kwargs) -> None:
    allowed = set(SETTINGS_COLUMNS) | {
        "welcome_channel_id", "boost_channel_id",
        "vouch_channel_id", "vouch_reaction_emoji", "vouch_super_reaction"
    }
    updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not updates:
        return
    
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO settings (guild_id) VALUES (?)", (guild_id,))
    for key, value in updates.items():
        cur.execute(f"UPDATE settings SET {key} = ? WHERE guild_id = ?", (value, guild_id))
    conn.commit()
    conn.close()


def get_settings(guild_id: int) -> sqlite3.Row | None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM settings WHERE guild_id = ?", (guild_id,))
    row = cur.fetchone()
    conn.close()
    return row


# ———————————————––
# Theme & Embed Helpers
# ———————————————––

def get_theme_color(name: str | None) -> int:
    if not name:
        return THEMES["pink"]
    value = name.strip().lower()
    if value in THEMES:
        return THEMES[value]
    if value.startswith("#"):
        value = value[1:]
    if len(value) == 6:
        try:
            return int(value, 16)
        except ValueError:
            pass
    return THEMES["pink"]


def build_embed(
    *,
    title: str | None = None,
    description: str | None = None,
    theme: str = "pink",
    image: str | None = None,
    thumbnail: str | None = None,
    footer: str | None = None,
    user_avatar_url: str | None = None,
) -> discord.Embed:
    safe_desc = description
    if not title and not safe_desc and not image:
        safe_desc = "\u200b"
    
    embed = discord.Embed(title=title, description=safe_desc, color=get_theme_color(theme))
    
    if image:
        embed.set_image(url=image)
    if user_avatar_url:
        embed.set_thumbnail(url=user_avatar_url)
    elif thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if footer:
        embed.set_footer(text=footer)
    
    return embed


# ———————————————––
# Utility Helpers
# ———————————————––

def guild_only(interaction: discord.Interaction) -> discord.Guild:
    if interaction.guild is None:
        raise app_commands.CheckFailure("This command only works in a server.")
    return interaction.guild


def clean_input(value):
    if value is None:
        return None
    return "" if isinstance(value, str) and value.lower() == "none" else value


CUSTOM_EMOJI_RE = re.compile(r'^<a?:\w+:\d+>$')


def _looks_like_emoji_token(token: str) -> bool:
    if CUSTOM_EMOJI_RE.match(token):
        return True
    # Short unicode emoji (not custom) — heuristic: short and not plain alphanumeric text
    return len(token) <= 3 and not token.isalnum()


def parse_button(button: str | None):
    if not button:
        return None, None
    parts = button.split(" ", 1)
    first = parts[0]

    if len(parts) > 1 and _looks_like_emoji_token(first):
        # "<emoji> label text" — split into emoji + label
        return parts[1], first

    if len(parts) == 1 and _looks_like_emoji_token(first):
        # Only an emoji was given, no label text — leave label untouched (None)
        # so the existing label isn't overwritten, and just update the emoji.
        return None, first

    # No emoji detected — return "" (not None) so upsert_settings
    # actually clears any previously-saved emoji instead of leaving it untouched.
    return button, ""


def parse_emoji(value: str | None):
    return value.strip() if value else None


def check_trigger(content_lower: str, trigger: str, match_type: str) -> bool:
    if match_type == "anywhere":
        return trigger in content_lower
    return content_lower == f".{trigger}"


# ———————————————––
# Waitlist Helpers
# ———————————————––

def load_waitlists():
    try:
        with open(WAITLIST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_waitlists(data):
    with open(WAITLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_waitlist_key(guild_id: int):
    return str(guild_id)


def entry_id(entry) -> str:
    return entry["id"] if isinstance(entry, dict) else entry


def entry_label(entry) -> str | None:
    return entry.get("label") if isinstance(entry, dict) else None


def build_waitlist_embed(guild, title, entries, color="pink"):
    lines = []
    for i, entry in enumerate(entries, start=1):
        cid = entry_id(entry)
        label = entry_label(entry)
        channel = guild.get_channel(int(cid))
        if channel:
            line = f"{i}) <#{channel.id}>"
            if label:
                line += f" {label}"
            lines.append(line)
    
    description = "\n".join(lines) if lines else "*No orders in the waitlist yet.*"
    return discord.Embed(title=title, description=description, color=get_theme_color(color))


async def update_waitlist_message(bot, guild_id: int):
    data = load_waitlists()
    key = get_waitlist_key(guild_id)
    if key not in data:
        return
    
    entry = data[key]
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    
    channel = guild.get_channel(entry["channel_id"])
    if not channel:
        return
    
    try:
        message = await channel.fetch_message(entry["message_id"])
        embed = build_waitlist_embed(guild, entry["title"], entry["users"], entry.get("color", "pink"))
        await message.edit(embed=embed)
    except discord.NotFound:
        pass


# ———————————————––
# Modals
# ———————————————––

class EmbedModal(discord.ui.Modal, title="Create Embed"):
    embed_title = discord.ui.TextInput(label="Title", required=False, max_length=256, placeholder="e.g.  ✨ server rules")
    description = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, required=False, max_length=4000, placeholder="e.g.  welcome to cwtie ugc! ♡")
    theme = discord.ui.TextInput(label="Color — theme name or hex", required=False, max_length=20, default="pink", placeholder="pink / blue / mint / lavender / white / peach / #f7cfe3")
    image = discord.ui.TextInput(label="Big image URL (bottom of embed)", required=False, max_length=1000, placeholder="e.g.  https://i.imgur.com/abc123.png")
    thumbnail = discord.ui.TextInput(label="Small image URL (top-right corner)", required=False, max_length=1000, placeholder="e.g.  https://i.imgur.com/xyz456.png")

    def __init__(self, use_avatar: bool, save_name: str | None = None, post_here: bool = False, is_edit: bool = False, prefill: dict | None = None):
        super().__init__()
        self.use_avatar = use_avatar
        self.save_name = save_name
        self.post_here = post_here
        self.is_edit = is_edit
        if prefill:
            if prefill.get("embed_title"):
                self.embed_title.default = prefill["embed_title"]
            if prefill.get("description"):
                self.description.default = prefill["description"]
            if prefill.get("theme"):
                self.theme.default = prefill["theme"]
            if prefill.get("image_url"):
                self.image.default = prefill["image_url"]
            if prefill.get("thumbnail_url"):
                self.thumbnail.default = prefill["thumbnail_url"]

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        title_val = str(self.embed_title).strip() or None
        desc_val = str(self.description).strip() or None
        theme_val = str(self.theme).strip() or "pink"
        image_val = str(self.image).strip() or None
        thumb_val = str(self.thumbnail).strip() or None

        if not title_val and not desc_val and not image_val:
            desc_val = "\u200b"

        embed = build_embed(
            title=title_val,
            description=desc_val,
            theme=theme_val,
            image=image_val,
            thumbnail=None if self.use_avatar else thumb_val,
            user_avatar_url=interaction.user.display_avatar.url if self.use_avatar else None,
        )

        if self.save_name and guild_id:
            now = datetime.now(timezone.utc).isoformat()
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT id FROM saved_embeds WHERE guild_id = ? AND name = ?", (guild_id, self.save_name))
            existing = cur.fetchone()
            conn.close()

            if existing and not self.is_edit:
                await interaction.response.send_message(f"❌ An embed named **{self.save_name}** already exists!\nUse `/embededit {self.save_name}` to edit it.", ephemeral=True)
                return

            conn = get_db()
            cur = conn.cursor()
            if existing:
                cur.execute(
                    "UPDATE saved_embeds SET embed_title=?, description=?, theme=?, image_url=?, thumbnail_url=?, use_avatar=?, updated_at=? WHERE guild_id=? AND name=?",
                    (title_val, desc_val, theme_val, image_val, thumb_val, int(self.use_avatar), now, guild_id, self.save_name),
                )
            else:
                cur.execute(
                    "INSERT INTO saved_embeds (guild_id, name, embed_title, description, theme, image_url, thumbnail_url, use_avatar, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (guild_id, self.save_name, title_val, desc_val, theme_val, image_val, thumb_val, int(self.use_avatar), now, now),
                )
            conn.commit()
            conn.close()

            if self.post_here:
                await interaction.response.send_message(embed=embed)
                await interaction.followup.send(f"{CHECK} Also saved as **{self.save_name}**. Use `/embedpost {self.save_name}` anytime to repost it.", ephemeral=True)
            else:
                await interaction.response.send_message(f"{CHECK} Saved as **{self.save_name}**! Use `/embedpost {self.save_name}` to post it in any channel.", embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed)


class WelcomeEditModal(discord.ui.Modal, title="Edit Welcome Settings"):
    outside_text = discord.ui.TextInput(label="Text ABOVE the embed (optional)", required=False, max_length=2000, placeholder="e.g.  thank you {mention} !")
    welcome_title = discord.ui.TextInput(label="Embed title (optional)", required=False, max_length=256, placeholder="e.g.  🐇 welcome to cwtie ugc!")
    welcome_text = discord.ui.TextInput(label="Embed description text", style=discord.TextStyle.paragraph, required=True, max_length=2000, placeholder="e.g.  🐇 welcome {mention} to cwtie ugc! ♡")
    theme = discord.ui.TextInput(label="Color — theme name or hex", required=False, max_length=20, placeholder="pink / blue / mint / lavender / white / peach / #f7cfe3")
    banner_url = discord.ui.TextInput(label="Banner image URL (big image at bottom)", required=False, max_length=1000, placeholder="e.g.  https://i.imgur.com/abc123.gif")

    def __init__(self, prefill: dict | None = None):
        super().__init__()
        if prefill:
            if prefill.get("welcome_outside_text"):
                self.outside_text.default = prefill["welcome_outside_text"]
            if prefill.get("welcome_title"):
                self.welcome_title.default = prefill["welcome_title"]
            if prefill.get("welcome_text"):
                self.welcome_text.default = prefill["welcome_text"]
            if prefill.get("welcome_theme"):
                self.theme.default = prefill["welcome_theme"]
            if prefill.get("welcome_banner_url"):
                self.banner_url.default = prefill["welcome_banner_url"]

    async def on_submit(self, interaction: discord.Interaction):
        guild = guild_only(interaction)
        title_val = str(self.welcome_title).strip()
        outside_val = str(self.outside_text).strip()
        kwargs = {"welcome_text": str(self.welcome_text), "welcome_title": title_val, "welcome_outside_text": outside_val}
        if str(self.theme).strip():
            kwargs["welcome_theme"] = str(self.theme).strip()
        if str(self.banner_url).strip():
            kwargs["welcome_banner_url"] = str(self.banner_url).strip()
        
        upsert_settings(guild.id, **kwargs)
        preview = build_embed(
            title=title_val.replace("{mention}", interaction.user.mention) if title_val else None,
            description=str(self.welcome_text).replace("{mention}", interaction.user.mention),
            theme=str(self.theme) or "pink",
            image=str(self.banner_url) or None,
            user_avatar_url=interaction.user.display_avatar.url,
        )
        preview_content = outside_val.replace("{mention}", interaction.user.mention) if outside_val else None
        await interaction.response.send_message(f"{CHECK} Welcome settings updated! Here's a preview:", content=preview_content, embed=preview, ephemeral=True)


class BoostEditModal(discord.ui.Modal, title="Edit Boost Settings"):
    boost_title = discord.ui.TextInput(label="Title", required=False, max_length=256, placeholder="e.g.  💖 server boost!")
    boost_text = discord.ui.TextInput(label="Message", style=discord.TextStyle.paragraph, required=False, max_length=2000, placeholder="Use {mention}, {username}, {server} — e.g. thank you {mention} for boosting {server} ♡")
    outside_text = discord.ui.TextInput(label="Text ABOVE the embed (optional)", required=False, max_length=2000, placeholder="e.g.  thank you {mention} for boosting {server} ♡")
    image = discord.ui.TextInput(label="Big image URL (bottom of embed)", required=False, max_length=1000, placeholder="e.g.  https://i.imgur.com/abc123.gif")
    thumbnail = discord.ui.TextInput(label="Thumbnail URL (or 'avatar')", required=False, max_length=1000, placeholder="e.g. https://i.imgur.com/xyz456.png, or type: avatar")

    def __init__(self, prefill: dict | None = None):
        super().__init__()
        if prefill:
            if prefill.get("boost_title"):
                self.boost_title.default = prefill["boost_title"]
            if prefill.get("boost_text"):
                self.boost_text.default = prefill["boost_text"]
            if prefill.get("boost_outside_text"):
                self.outside_text.default = prefill["boost_outside_text"]
            if prefill.get("boost_image_url"):
                self.image.default = prefill["boost_image_url"]
            if prefill.get("boost_thumbnail_url"):
                self.thumbnail.default = prefill["boost_thumbnail_url"]
            if prefill.get("boost_use_avatar") == "1":
                self.thumbnail.default = "avatar"

    async def on_submit(self, interaction: discord.Interaction):
        guild = guild_only(interaction)
        updates = {}
        if str(self.boost_title).strip():
            updates["boost_title"] = str(self.boost_title).strip()
        if str(self.boost_text).strip():
            updates["boost_text"] = str(self.boost_text).strip()
        updates["boost_outside_text"] = str(self.outside_text).strip()
        # Keep existing color if user didn't provide a new one (modal no longer asks for color)
        if str(self.image).strip():
            updates["boost_image_url"] = str(self.image).strip()
        
        use_av = str(self.thumbnail).strip().lower() == "avatar"
        if use_av:
            updates["boost_use_avatar"] = "1"
            updates["boost_thumbnail_url"] = ""
        elif str(self.thumbnail).strip():
            updates["boost_use_avatar"] = "0"
            updates["boost_thumbnail_url"] = str(self.thumbnail).strip()
        
        if updates:
            upsert_settings(guild.id, **updates)
        
        thumb = None if use_av else (str(self.thumbnail).strip() or None)
        preview_text = (str(self.boost_text).strip() or "thank you {mention} for boosting! ♡").replace("{mention}", interaction.user.mention).replace("{username}", interaction.user.name).replace("{server}", guild.name)
        preview_content = (str(self.outside_text).strip() or None)
        if preview_content:
            preview_content = preview_content.replace("{mention}", interaction.user.mention).replace("{username}", interaction.user.name).replace("{server}", guild.name)
        # Use existing saved color (if any) or default to pink
        current_settings = get_settings(guild.id)
        color_val = (current_settings["boost_color"] if current_settings and current_settings["boost_color"] else "pink")
        embed = build_embed(
            title=str(self.boost_title).strip() or None,
            description=preview_text,
            theme=color_val,
            image=str(self.image).strip() or None,
            thumbnail=thumb,
            user_avatar_url=interaction.user.display_avatar.url if use_av else None
        )
        await interaction.response.send_message(f"{CHECK} Boost settings updated! Here's a preview:", content=preview_content, embed=embed, ephemeral=True)


# ———————————————––
# Verify Views
# ———————————————––

class VerifyButton(discord.ui.Button):
    def __init__(self, label: str = "Verify", emoji=None):
        super().__init__(label=label or "Verify", emoji=emoji, style=discord.ButtonStyle.secondary, custom_id="verify_button")

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        settings = get_settings(guild.id)
        role_id = settings["verify_role_id"] if settings else None
        
        if not role_id:
            return await interaction.response.send_message("⚠️ No verify role set.", ephemeral=True)
        
        role = guild.get_role(int(role_id))
        if not role:
            return await interaction.response.send_message("⚠️ Verify role not found.", ephemeral=True)
        
        if role in interaction.user.roles:
            msg = (settings["verify_already_message"] or f"{CHECK} You're already verified!")
            return await interaction.response.send_message(msg, ephemeral=True)
        
        try:
            await interaction.user.add_roles(role)
            msg = (settings["verify_success_message"] or f"{CHECK} You've been verified!")
            await interaction.response.send_message(msg, ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("⚠️ I need higher permissions to assign this role.", ephemeral=True)


class VerifyView(discord.ui.View):
    def __init__(self, button_label: str = "Verify", button_emoji=None):
        super().__init__(timeout=None)
        self.add_item(VerifyButton(label=button_label, emoji=button_emoji))


# ———————————————––
# Waitlist Views
# ———————————————––

class WaitlistRemoveSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="Pick a channel to remove...", options=options)

    async def callback(self, interaction: discord.Interaction):
        data = load_waitlists()
        key = get_waitlist_key(interaction.guild.id)
        cid = self.values[0]
        data[key]["users"] = [e for e in data[key]["users"] if entry_id(e) != cid]
        save_waitlists(data)
        await update_waitlist_message(bot, interaction.guild.id)
        await interaction.response.edit_message(content=f"{CHECK} Removed from the waitlist.", view=None)


class WaitlistRemoveView(discord.ui.View):
    def __init__(self, guild: discord.Guild, entries: list):
        super().__init__(timeout=60)
        options = []
        for e in entries:
            cid = entry_id(e)
            label = entry_label(e)
            channel = guild.get_channel(int(cid))
            display = f"#{channel.name}" if channel else f"unknown ({cid})"
            if label:
                display += f" — {label}"
            options.append(discord.SelectOption(label=display[:100], value=cid))
        self.add_item(WaitlistRemoveSelect(options))


class WaitlistMovePickSelect(discord.ui.Select):
    def __init__(self, entries: list, guild: discord.Guild):
        self.entries = entries
        self.guild = guild
        options = []
        for i, e in enumerate(entries, start=1):
            cid = entry_id(e)
            lbl = entry_label(e)
            ch = guild.get_channel(int(cid))
            ch_name = f"#{ch.name}" if ch else f"unknown ({cid})"
            suffix = f" — {lbl}" if lbl else ""
            options.append(discord.SelectOption(label=f"{i}) {ch_name}{suffix}"[:100], value=cid))
        super().__init__(placeholder="Pick an entry to move...", options=options)

    async def callback(self, interaction: discord.Interaction):
        cid = self.values[0]
        data = load_waitlists()
        key = get_waitlist_key(interaction.guild.id)
        entries = data[key]["users"]
        view = WaitlistMovePositionView(cid, entries, interaction.guild)
        ch = interaction.guild.get_channel(int(cid))
        ch_mention = ch.mention if ch else f"<#{cid}>"
        await interaction.response.edit_message(content=f"Where should {ch_mention} go?", view=view)


class WaitlistMovePickView(discord.ui.View):
    def __init__(self, entries: list, guild: discord.Guild):
        super().__init__(timeout=60)
        self.add_item(WaitlistMovePickSelect(entries, guild))


class WaitlistMovePositionSelect(discord.ui.Select):
    def __init__(self, channel_id: str, entries: list, guild: discord.Guild):
        self.channel_id = channel_id
        options = []
        for i in range(1, len(entries) + 1):
            e = entries[i - 1]
            cid = entry_id(e)
            lbl = entry_label(e)
            ch = guild.get_channel(int(cid))
            ch_name = f"#{ch.name}" if ch else f"unknown ({cid})"
            suffix = f" — {lbl}" if lbl else ""
            options.append(discord.SelectOption(label=f"Slot {i}", description=f"Currently: {ch_name}{suffix}"[:100], value=str(i)))
        super().__init__(placeholder="Pick the new position...", options=options)

    async def callback(self, interaction: discord.Interaction):
        data = load_waitlists()
        key = get_waitlist_key(interaction.guild.id)
        entries = data[key]["users"]

        target = None
        new_entries = []
        for e in entries:
            if entry_id(e) == self.channel_id:
                target = e
            else:
                new_entries.append(e)

        if target is None:
            await interaction.response.edit_message(content="❌ Entry not found.", view=None)
            return

        new_pos = max(0, min(int(self.values[0]) - 1, len(new_entries)))
        new_entries.insert(new_pos, target)
        data[key]["users"] = new_entries
        save_waitlists(data)
        await update_waitlist_message(bot, interaction.guild.id)
        ch = interaction.guild.get_channel(int(self.channel_id))
        ch_mention = ch.mention if ch else f"<#{self.channel_id}>"
        await interaction.response.edit_message(content=f"{CHECK} Moved {ch_mention} to slot {self.values[0]}.", view=None)


class WaitlistMovePositionView(discord.ui.View):
    def __init__(self, channel_id: str, entries: list, guild: discord.Guild):
        super().__init__(timeout=60)
        self.add_item(WaitlistMovePositionSelect(channel_id, entries, guild))


# ———————————————––
# Events
# ———————————————––

@bot.event
async def on_ready():
    init_db()
    bot.add_view(VerifyView())
    print(f"Bot user: {bot.user}")
    synced = await bot.tree.sync()
    print(f"Synced {len(synced)} global command(s)")
    if GUILD_ID:
        try:
            guild_obj = discord.Object(id=int(GUILD_ID.strip()))
            guild_synced = await bot.tree.sync(guild=guild_obj)
            print(f"Also synced {len(guild_synced)} guild command(s)")
        except Exception as e:
            print(f"Guild sync skipped: {e}")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Global handler so a bug in a command shows an error instead of a silent
    'The application did not respond' failure in Discord."""
    print(f"[COMMAND ERROR] /{interaction.command.name if interaction.command else '?'}: {error!r}")

    if isinstance(error, app_commands.CheckFailure):
        msg = str(error) or "⚠️ You don't have permission to use this command."
    else:
        msg = "⚠️ Something went wrong running that command. It's been logged."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


@bot.event
async def on_member_join(member: discord.Member):
    await asyncio.sleep(1)
    settings = get_settings(member.guild.id)
    if not settings or not settings["welcome_channel_id"]:
        return
    
    channel = member.guild.get_channel(settings["welcome_channel_id"])
    if not channel:
        return
    
    description = (settings["welcome_text"] or "Welcome {mention}!").replace("{mention}", member.mention).replace("\\n", "\n")
    title = (settings["welcome_title"] or None)
    if title:
        title = title.replace("{mention}", member.mention)
    thumb = settings["welcome_thumbnail_url"] if settings["welcome_thumbnail_url"] else None
    outside_text = settings["welcome_outside_text"] if settings["welcome_outside_text"] else None
    if outside_text:
        outside_text = outside_text.replace("{mention}", member.mention).replace("\\n", "\n")
    embed = build_embed(
        title=title,
        description=description,
        theme=settings["welcome_theme"] or "pink",
        image=settings["welcome_banner_url"],
        thumbnail=thumb,
        user_avatar_url=member.display_avatar.url if not thumb else None,
    )
    await channel.send(content=outside_text, embed=embed)
    
    if settings["welcome_banner2_url"]:
        embed2 = build_embed(title=None, description=None, theme=settings["welcome_theme"] or "pink", image=settings["welcome_banner2_url"])
        await channel.send(embed=embed2)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if before.premium_since is None and after.premium_since is not None:
        settings = get_settings(after.guild.id)
        if not settings or not settings["boost_channel_id"]:
            return
        
        channel = after.guild.get_channel(int(settings["boost_channel_id"]))
        if not channel:
            return
        
        text = (settings["boost_text"] or "thank you {mention} for boosting! ♡").replace("{mention}", after.mention).replace("{username}", after.name).replace("{server}", after.guild.name)
        outside_text = settings["boost_outside_text"] if settings["boost_outside_text"] else None
        if outside_text:
            outside_text = outside_text.replace("{mention}", after.mention).replace("{username}", after.name).replace("{server}", after.guild.name).replace("\\n", "\n")
        use_av = settings["boost_use_avatar"] == "1" if settings["boost_use_avatar"] else False
        thumb = settings["boost_thumbnail_url"] or None
        embed = build_embed(
            title=settings["boost_title"] or None,
            description=text,
            theme=settings["boost_color"] or "pink",
            image=settings["boost_image_url"] or None,
            thumbnail=None if use_av else thumb,
            user_avatar_url=after.display_avatar.url if use_av else None
        )
        await channel.send(content=outside_text, embed=embed)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    
    await bot.process_commands(message)
    
    if not message.guild:
        return

    # Vouch auto-react
    settings = get_settings(message.guild.id)
    if settings and settings["vouch_channel_id"]:
        if message.channel.id == int(settings["vouch_channel_id"]):
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT last_message_id FROM sticky_messages WHERE guild_id = ? AND channel_id = ?", (message.guild.id, message.channel.id))
            sticky_row = cur.fetchone()
            conn.close()
            
            is_sticky = sticky_row and sticky_row["last_message_id"] == message.id
            
            if not is_sticky:
                reaction_emojis = settings["vouch_reaction_emoji"]
                super_reaction = settings["vouch_super_reaction"] == "1"
                if reaction_emojis:
                    emoji_list = [e.strip() for e in reaction_emojis.split(",") if e.strip()]
                    for emoji in emoji_list:
                        try:
                            await message.add_reaction(emoji)
                            if super_reaction:
                                await asyncio.sleep(0.1)
                                await message.remove_reaction(emoji, bot.user)
                                await asyncio.sleep(0.1)
                                await message.add_reaction(emoji)
                        except Exception as e:
                            print(f"[DEBUG] Failed to auto-react with {emoji}: {e}")

    content = message.content.strip()
    content_lower = content.lower()

    # Autoresponders
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT trigger, message, ping_roles, match_type FROM autoresponders WHERE guild_id = ?", (message.guild.id,))
    all_ars = cur.fetchall()
    conn.close()

    for ar in all_ars:
        if check_trigger(content_lower, ar["trigger"], ar["match_type"] or "exact"):
            ping_text = ""
            if ar["ping_roles"]:
                role_ids = [r for r in ar["ping_roles"].split(",") if r]
                ping_text = " ".join(f"<@&{rid}>" for rid in role_ids) + " "
            await message.channel.send(ping_text + ar["message"].replace("\\n", "\n"))
            break

    # Image responders
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT trigger, image_url, caption, match_type FROM image_responders WHERE guild_id = ?", (message.guild.id,))
    all_imgs = cur.fetchall()
    conn.close()

    for img in all_imgs:
        if check_trigger(content_lower, img["trigger"], img["match_type"] or "exact"):
            embed = build_embed(title=None, description=img["caption"] or None, theme="pink", image=img["image_url"])
            await message.channel.send(embed=embed)
            break

    # Sticky messages
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT message, last_message_id FROM sticky_messages WHERE guild_id = ? AND channel_id = ?", (message.guild.id, message.channel.id))
    row = cur.fetchone()
    conn.close()

    if not row:
        return

    if row["last_message_id"]:
        try:
            old_msg = await message.channel.fetch_message(row["last_message_id"])
            await old_msg.delete()
        except Exception:
            pass

    new_msg = await message.channel.send(row["message"].replace("\\n", "\n").replace("{mention}", message.author.mention))

    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE sticky_messages SET last_message_id = ? WHERE guild_id = ? AND channel_id = ?", (new_msg.id, message.guild.id, message.channel.id))
    conn.commit()
    conn.close()


# ———————————————––
# Commands — Embeds
# ———————————————––

@bot.tree.command(name="embed", description="Create and post a custom embed in this channel")
@app_commands.describe(title="Embed title", description="Embed text", color="Theme or hex color", image="Big image URL", thumbnail="Small image URL", use_avatar="Use your avatar as thumbnail", save="Save as named embed")
async def embed_command(interaction: discord.Interaction, title: str | None = None, description: str | None = None, color: str = "pink", image: str | None = None, thumbnail: str | None = None, use_avatar: bool = False, save: str | None = None):
    guild_id = interaction.guild_id
    embed = build_embed(title=title, description=description, theme=color or "pink", image=image, thumbnail=None if use_avatar else thumbnail, user_avatar_url=interaction.user.display_avatar.url if use_avatar else None)
    
    if save and guild_id:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM saved_embeds WHERE guild_id = ? AND name = ?", (guild_id, save))
        existing = cur.fetchone()
        if existing:
            conn.close()
            await interaction.response.send_message(f"❌ An embed named **{save}** already exists! Use `/embededit {save}` to edit it.", ephemeral=True)
            return
        
        now = datetime.now(timezone.utc).isoformat()
        cur.execute(
            "INSERT INTO saved_embeds (guild_id, name, embed_title, description, theme, image_url, thumbnail_url, use_avatar, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, save, title, description, color, image, thumbnail, int(use_avatar), now, now)
        )
        conn.commit()
        conn.close()
        await interaction.response.send_message(embed=embed)
        await interaction.followup.send(f"{CHECK} Saved as **{save}**! Use `/embedpost {save}` to repost anytime.", ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed)


@bot.tree.command(name="embedlist", description="List all saved embeds")
async def embedlist(interaction: discord.Interaction):
    guild = guild_only(interaction)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT name, embed_title, post_channel_id, updated_at FROM saved_embeds WHERE guild_id = ? ORDER BY name", (guild.id,))
    rows = cur.fetchall()
    conn.close()
    
    if not rows:
        await interaction.response.send_message("No saved embeds yet. Use `/embed save_name:myname` to save one.", ephemeral=True)
        return
    
    # Discord embeds cap out at 25 fields each, so paginate into chunks of 25
    chunks = [rows[i:i + 25] for i in range(0, len(rows), 25)]
    embeds = []
    for idx, chunk in enumerate(chunks, start=1):
        page_title = f"📋 Saved Embeds (page {idx}/{len(chunks)})" if len(chunks) > 1 else "📋 Saved Embeds"
        embed = discord.Embed(title=page_title, color=get_theme_color("pink"))
        for row in chunk:
            ch = guild.get_channel(row["post_channel_id"]) if row["post_channel_id"] else None
            ch_text = ch.mention if ch else "*(no channel)*"
            t_text = f'"{row["embed_title"]}"' if row["embed_title"] else "*(no title)*"
            embed.add_field(name=f"• {row['name']}", value=f"Title: {t_text}\nChannel: {ch_text}\nUpdated: {row['updated_at'][:10]}", inline=False)
        embeds.append(embed)
    
    # Discord also caps messages at 10 embeds each, so batch those too
    first_batch = embeds[:10]
    await interaction.response.send_message(embeds=first_batch, ephemeral=True)
    for i in range(10, len(embeds), 10):
        await interaction.followup.send(embeds=embeds[i:i + 10], ephemeral=True)


@bot.tree.command(name="embedpost", description="Post a saved embed in the current channel")
@app_commands.describe(name="Name of the embed")
async def embedpost(interaction: discord.Interaction, name: str):
    guild = guild_only(interaction)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM saved_embeds WHERE guild_id = ? AND name = ?", (guild.id, name))
    row = cur.fetchone()
    conn.close()
    
    if not row:
        await interaction.response.send_message(f"No embed named **{name}**. Use `/embedlist`.", ephemeral=True)
        return
    
    embed = build_embed(title=row["embed_title"], description=row["description"] or "\u200b", theme=row["theme"] or "pink", image=row["image_url"], thumbnail=None if row["use_avatar"] else row["thumbnail_url"])
    await interaction.response.defer(ephemeral=True)
    await interaction.channel.send(embed=embed)


@bot.tree.command(name="embededit", description="Edit a saved embed")
@app_commands.describe(name="Name of the embed to edit")
async def embededit(interaction: discord.Interaction, name: str):
    guild = guild_only(interaction)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM saved_embeds WHERE guild_id = ? AND name = ?", (guild.id, name))
    row = cur.fetchone()
    conn.close()
    
    if not row:
        await interaction.response.send_message(f"No embed named **{name}**.", ephemeral=True)
        return
    
    prefill = {"embed_title": row["embed_title"], "description": row["description"], "theme": row["theme"], "image_url": row["image_url"], "thumbnail_url": row["thumbnail_url"]}
    await interaction.response.send_modal(EmbedModal(use_avatar=bool(row["use_avatar"]), save_name=name, is_edit=True, prefill=prefill))


@bot.tree.command(name="embedchannel", description="Set which channel a saved embed posts to")
@app_commands.describe(name="Name of the embed", channel="Channel to post it in")
async def embedchannel(interaction: discord.Interaction, name: str, channel: discord.TextChannel):
    guild = guild_only(interaction)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM saved_embeds WHERE guild_id = ? AND name = ?", (guild.id, name))
    row = cur.fetchone()
    
    if not row:
        conn.close()
        await interaction.response.send_message(f"No embed named **{name}**.", ephemeral=True)
        return
    
    cur.execute("UPDATE saved_embeds SET post_channel_id = ? WHERE guild_id = ? AND name = ?", (channel.id, guild.id, name))
    conn.commit()
    conn.close()
    
    await interaction.response.send_message(f"{CHECK} **{name}** will post to {channel.mention}.", ephemeral=True)


@bot.tree.command(name="embeddelete", description="Delete a saved embed")
@app_commands.describe(name="Name of the embed to delete")
async def embeddelete(interaction: discord.Interaction, name: str):
    guild = guild_only(interaction)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM saved_embeds WHERE guild_id = ? AND name = ?", (guild.id, name))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    
    msg = f"🗑️ Deleted **{name}**." if deleted else f"No embed named **{name}**."
    await interaction.response.send_message(msg, ephemeral=True)


# ———————————————––
# Commands — Embed Collections (post several saved embeds at once)
# ———————————————––

def _parse_embed_names(embeds: str) -> list[str]:
    return [e.strip() for e in embeds.split(",") if e.strip()]


async def _missing_embed_names(guild_id: int, names: list[str]) -> list[str]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(f"SELECT name FROM saved_embeds WHERE guild_id = ? AND name IN ({','.join('?' * len(names))})", (guild_id, *names))
    existing = {row["name"] for row in cur.fetchall()}
    conn.close()
    return [n for n in names if n not in existing]


embedcollection_group = app_commands.Group(name="embedcollection", description="Post several saved embeds together with one command")

@embedcollection_group.command(name="create", description="Create a collection of saved embeds to post together")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(name="Name for this collection", embeds="Saved embed names, comma-separated, in post order (e.g. banner, models, uploads)")
async def embedcollection_create(interaction: discord.Interaction, name: str, embeds: str):
    guild = guild_only(interaction)
    embed_names = _parse_embed_names(embeds)
    if not embed_names:
        await interaction.response.send_message("❌ Provide at least one saved embed name.", ephemeral=True)
        return

    missing = await _missing_embed_names(guild.id, embed_names)
    if missing:
        await interaction.response.send_message(f"❌ No saved embed(s) named: {', '.join(missing)}. Check `/embedlist`.", ephemeral=True)
        return

    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM embed_collections WHERE guild_id = ? AND name = ?", (guild.id, name))
    if cur.fetchone():
        conn.close()
        await interaction.response.send_message(f"❌ A collection named **{name}** already exists! Use `/embedcollection edit` to change it.", ephemeral=True)
        return

    cur.execute(
        "INSERT INTO embed_collections (guild_id, name, embed_names, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (guild.id, name, json.dumps(embed_names), now, now),
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"{CHECK} Collection **{name}** created with: {', '.join(embed_names)}\nUse `/embedcollection post name:{name}` to post them all.", ephemeral=True)


@embedcollection_group.command(name="post", description="Post every embed in a collection, in order")
@app_commands.describe(name="Name of the collection")
async def embedcollection_post(interaction: discord.Interaction, name: str):
    guild = guild_only(interaction)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT embed_names FROM embed_collections WHERE guild_id = ? AND name = ?", (guild.id, name))
    row = cur.fetchone()
    conn.close()

    if not row:
        await interaction.response.send_message(f"No collection named **{name}**. Use `/embedcollection list`.", ephemeral=True)
        return

    embed_names = json.loads(row["embed_names"])
    await interaction.response.defer(ephemeral=True)

    conn = get_db()
    cur = conn.cursor()
    posted, missing, built = [], [], []
    for embed_name in embed_names:
        cur.execute("SELECT * FROM saved_embeds WHERE guild_id = ? AND name = ?", (guild.id, embed_name))
        saved = cur.fetchone()
        if not saved:
            missing.append(embed_name)
            continue
        embed = build_embed(
            title=saved["embed_title"],
            description=saved["description"] or "\u200b",
            theme=saved["theme"] or "pink",
            image=saved["image_url"],
            thumbnail=None if saved["use_avatar"] else saved["thumbnail_url"],
        )
        built.append(embed)
        posted.append(embed_name)
    conn.close()

    # Discord allows up to 10 embeds per message, so batch them instead of
    # sending one message per embed (fewer API calls, no rate-limit risk,
    # and they land as one grouped post instead of a spammy chain).
    for i in range(0, len(built), 10):
        await interaction.channel.send(embeds=built[i:i + 10])

    msg = f"{CHECK} Posted: {', '.join(posted)}"
    if missing:
        msg += f"\n⚠️ Skipped (no longer saved): {', '.join(missing)}"
    await interaction.followup.send(msg, ephemeral=True)


@embedcollection_group.command(name="edit", description="Replace the embed list in a collection")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(name="Name of the collection to edit", embeds="New saved embed names, comma-separated, in post order")
async def embedcollection_edit(interaction: discord.Interaction, name: str, embeds: str):
    guild = guild_only(interaction)
    embed_names = _parse_embed_names(embeds)
    if not embed_names:
        await interaction.response.send_message("❌ Provide at least one saved embed name.", ephemeral=True)
        return

    missing = await _missing_embed_names(guild.id, embed_names)
    if missing:
        await interaction.response.send_message(f"❌ No saved embed(s) named: {', '.join(missing)}. Check `/embedlist`.", ephemeral=True)
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM embed_collections WHERE guild_id = ? AND name = ?", (guild.id, name))
    if not cur.fetchone():
        conn.close()
        await interaction.response.send_message(f"No collection named **{name}**.", ephemeral=True)
        return

    now = datetime.now(timezone.utc).isoformat()
    cur.execute("UPDATE embed_collections SET embed_names = ?, updated_at = ? WHERE guild_id = ? AND name = ?", (json.dumps(embed_names), now, guild.id, name))
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"{CHECK} Collection **{name}** updated to: {', '.join(embed_names)}", ephemeral=True)


@embedcollection_group.command(name="add", description="Add one saved embed to the end of a collection")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(name="Name of the collection", embed="Saved embed name to append")
async def embedcollection_add(interaction: discord.Interaction, name: str, embed: str):
    guild = guild_only(interaction)
    missing = await _missing_embed_names(guild.id, [embed])
    if missing:
        await interaction.response.send_message(f"❌ No saved embed named **{embed}**. Check `/embedlist`.", ephemeral=True)
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT embed_names FROM embed_collections WHERE guild_id = ? AND name = ?", (guild.id, name))
    row = cur.fetchone()
    if not row:
        conn.close()
        await interaction.response.send_message(f"No collection named **{name}**.", ephemeral=True)
        return

    embed_names = json.loads(row["embed_names"])
    if embed in embed_names:
        conn.close()
        await interaction.response.send_message(f"**{embed}** is already in **{name}**.", ephemeral=True)
        return

    embed_names.append(embed)
    now = datetime.now(timezone.utc).isoformat()
    cur.execute("UPDATE embed_collections SET embed_names = ?, updated_at = ? WHERE guild_id = ? AND name = ?", (json.dumps(embed_names), now, guild.id, name))
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"{CHECK} Added **{embed}** to **{name}**. Order now: {', '.join(embed_names)}", ephemeral=True)


@embedcollection_group.command(name="remove", description="Remove one saved embed from a collection")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(name="Name of the collection", embed="Saved embed name to remove")
async def embedcollection_remove(interaction: discord.Interaction, name: str, embed: str):
    guild = guild_only(interaction)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT embed_names FROM embed_collections WHERE guild_id = ? AND name = ?", (guild.id, name))
    row = cur.fetchone()
    if not row:
        conn.close()
        await interaction.response.send_message(f"No collection named **{name}**.", ephemeral=True)
        return

    embed_names = json.loads(row["embed_names"])
    if embed not in embed_names:
        conn.close()
        await interaction.response.send_message(f"**{embed}** isn't in **{name}**.", ephemeral=True)
        return

    embed_names.remove(embed)
    now = datetime.now(timezone.utc).isoformat()
    cur.execute("UPDATE embed_collections SET embed_names = ?, updated_at = ? WHERE guild_id = ? AND name = ?", (json.dumps(embed_names), now, guild.id, name))
    conn.commit()
    conn.close()
    remaining = ', '.join(embed_names) if embed_names else "*(empty)*"
    await interaction.response.send_message(f"{CHECK} Removed **{embed}** from **{name}**. Remaining: {remaining}", ephemeral=True)


@embedcollection_group.command(name="list", description="List all embed collections")
async def embedcollection_list(interaction: discord.Interaction):
    guild = guild_only(interaction)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT name, embed_names, updated_at FROM embed_collections WHERE guild_id = ? ORDER BY name", (guild.id,))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message("No collections yet. Use `/embedcollection create`.", ephemeral=True)
        return

    embed = discord.Embed(title="📚 Embed Collections", color=get_theme_color("pink"))
    for row in rows:
        names = json.loads(row["embed_names"])
        embed.add_field(name=f"• {row['name']}", value=f"{' → '.join(names)}\nUpdated: {row['updated_at'][:10]}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@embedcollection_group.command(name="delete", description="Delete a collection (the saved embeds themselves are untouched)")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(name="Name of the collection to delete")
async def embedcollection_delete(interaction: discord.Interaction, name: str):
    guild = guild_only(interaction)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM embed_collections WHERE guild_id = ? AND name = ?", (guild.id, name))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    msg = f"🗑️ Deleted collection **{name}**." if deleted else f"No collection named **{name}**."
    await interaction.response.send_message(msg, ephemeral=True)


bot.tree.add_command(embedcollection_group)


# ———————————————––
# Commands — Welcome
# ———————————————––

@bot.tree.command(name="welcome_setup", description="Set up the welcome message")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(welcome_channel="Channel for welcome messages", welcome_text="Embed description text (use {mention})", title="Embed title (optional, use {mention})", color="Theme or hex", banner_url="Big image URL", thumbnail_url="Small image URL", banner2_url="Second image URL", outside_text="Plain text ABOVE the embed (use {mention})")
async def welcome_setup(interaction: discord.Interaction, welcome_channel: discord.TextChannel, welcome_text: str, title: str | None = None, color: str = "pink", banner_url: str | None = None, thumbnail_url: str | None = None, banner2_url: str | None = None, outside_text: str | None = None):
    guild = guild_only(interaction)
    kwargs = dict(welcome_channel_id=welcome_channel.id, welcome_text=welcome_text.replace("\\n", "\n"), welcome_theme=color)
    if title is not None:
        kwargs["welcome_title"] = title
    if banner_url:
        kwargs["welcome_banner_url"] = banner_url
    if thumbnail_url is not None:
        kwargs["welcome_thumbnail_url"] = thumbnail_url
    if banner2_url is not None:
        kwargs["welcome_banner2_url"] = banner2_url
    if outside_text is not None:
        kwargs["welcome_outside_text"] = outside_text.replace("\\n", "\n")
    
    upsert_settings(guild.id, **kwargs)
    thumb = thumbnail_url or None
    preview_title = title.replace("{mention}", interaction.user.mention) if title else None
    preview = build_embed(title=preview_title, description=welcome_text.replace("{mention}", interaction.user.mention), theme=color, image=banner_url, thumbnail=thumb, user_avatar_url=interaction.user.display_avatar.url if not thumb else None)
    preview_content = outside_text.replace("{mention}", interaction.user.mention) if outside_text else None
    await interaction.response.send_message(f"{CHECK} Welcome message saved! Preview:", embed=preview, ephemeral=True)
    if preview_content:
        await interaction.followup.send(content=preview_content, ephemeral=True)
    if banner2_url:
        embed2 = build_embed(title=None, description=None, theme=color, image=banner2_url)
        await interaction.followup.send(embed=embed2, ephemeral=True)


@bot.tree.command(name="welcome_edit", description="Edit the welcome message")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(welcome_text="New embed description text", title="New embed title ('none' to remove, use {mention})", color="New color", banner_url="New banner URL", thumbnail_url="New thumbnail (or 'none')", banner2_url="New second image (or 'none')", outside_text="Plain text ABOVE the embed ('none' to remove, use {mention})")
async def welcome_edit(interaction: discord.Interaction, welcome_text: str | None = None, title: str | None = None, color: str | None = None, banner_url: str | None = None, thumbnail_url: str | None = None, banner2_url: str | None = None, outside_text: str | None = None):
    guild = guild_only(interaction)
    settings = get_settings(guild.id)
    if not settings or not settings["welcome_channel_id"]:
        await interaction.response.send_message("Run `/welcome_setup` first.", ephemeral=True)
        return
    
    kwargs = {}
    if welcome_text:
        kwargs["welcome_text"] = welcome_text
    if title is not None:
        kwargs["welcome_title"] = "" if title.lower() == "none" else title
    if color:
        kwargs["welcome_theme"] = color
    if banner_url:
        kwargs["welcome_banner_url"] = banner_url
    if thumbnail_url is not None:
        kwargs["welcome_thumbnail_url"] = "" if thumbnail_url.lower() == "none" else thumbnail_url
    if banner2_url is not None:
        kwargs["welcome_banner2_url"] = "" if banner2_url.lower() == "none" else banner2_url
    if outside_text is not None:
        kwargs["welcome_outside_text"] = "" if outside_text.lower() == "none" else outside_text.replace("\\n", "\n")
    
    if not kwargs:
        await interaction.response.send_message("Provide at least one field to update.", ephemeral=True)
        return
    
    upsert_settings(guild.id, **kwargs)
    updated = get_settings(guild.id)
    thumb = updated["welcome_thumbnail_url"] if updated["welcome_thumbnail_url"] else None
    updated_title = updated["welcome_title"] if updated["welcome_title"] else None
    preview = build_embed(
        title=updated_title.replace("{mention}", interaction.user.mention) if updated_title else None,
        description=(updated["welcome_text"] or "Welcome!").replace("{mention}", interaction.user.mention),
        theme=updated["welcome_theme"] or "pink",
        image=updated["welcome_banner_url"],
        thumbnail=thumb,
        user_avatar_url=interaction.user.display_avatar.url if not thumb else None,
    )
    updated_outside = updated["welcome_outside_text"] if updated["welcome_outside_text"] else None
    preview_content = updated_outside.replace("{mention}", interaction.user.mention) if updated_outside else None
    await interaction.response.send_message(f"{CHECK} Welcome message updated! Preview:", embed=preview, ephemeral=True)
    if preview_content:
        await interaction.followup.send(content=preview_content, ephemeral=True)
    if updated["welcome_banner2_url"]:
        embed2 = build_embed(title=None, description=None, theme=updated["welcome_theme"] or "pink", image=updated["welcome_banner2_url"])
        await interaction.followup.send(embed=embed2, ephemeral=True)


@bot.tree.command(name="welcome_test", description="Preview the welcome embed")
@app_commands.checks.has_permissions(manage_guild=True)
async def welcome_test(interaction: discord.Interaction):
    guild = guild_only(interaction)
    settings = get_settings(guild.id)
    if not settings or not settings["welcome_channel_id"]:
        await interaction.response.send_message("Run `/welcome_setup` first.", ephemeral=True)
        return
    
    description = (settings["welcome_text"] or "Welcome {mention}!").replace("{mention}", interaction.user.mention)
    title = settings["welcome_title"] if settings["welcome_title"] else None
    if title:
        title = title.replace("{mention}", interaction.user.mention)
    thumb = settings["welcome_thumbnail_url"] if settings["welcome_thumbnail_url"] else None
    outside_text = settings["welcome_outside_text"] if settings["welcome_outside_text"] else None
    if outside_text:
        outside_text = outside_text.replace("{mention}", interaction.user.mention)
    embed = build_embed(title=title, description=description, theme=settings["welcome_theme"] or "pink", image=settings["welcome_banner_url"], thumbnail=thumb, user_avatar_url=interaction.user.display_avatar.url if not thumb else None)
    await interaction.response.send_message(content=outside_text, embed=embed, ephemeral=True)
    if settings["welcome_banner2_url"]:
        embed2 = build_embed(title=None, description=None, theme=settings["welcome_theme"] or "pink", image=settings["welcome_banner2_url"])
        await interaction.followup.send(embed=embed2, ephemeral=True)


@bot.tree.command(name="themes", description="Show available embed colors")
async def themes(interaction: discord.Interaction):
    names = ", ".join(THEMES.keys())
    await interaction.response.send_message(f"**Embed colors:** {names}\nOr use any hex like `#f7cfe3`", ephemeral=True)


# ———————————————––
# Commands — Boost
# ———————————————––

@bot.tree.command(name="set_boost_channel", description="Set the channel for boost announcements")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(channel="Boost announcement channel")
async def set_boost_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    guild = guild_only(interaction)
    upsert_settings(guild.id, boost_channel_id=channel.id)
    await interaction.response.send_message(f"{CHECK} Boost announcements will go to {channel.mention}!", ephemeral=True)


@bot.tree.command(name="set_boost_message", description="Set the boost message")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(message="Message (use {mention}, {username}, {server})", outside_text="Text above the embed (use {mention}, {username}, {server})", title="Title", color="Color", image="Big image", thumbnail="Small image", use_avatar="Use booster's avatar")
async def set_boost_message(interaction: discord.Interaction, message: str, outside_text: str | None = None, title: str | None = None, color: str = "pink", image: str | None = None, thumbnail: str | None = None, use_avatar: bool = False):
    guild = guild_only(interaction)
    updates = dict(boost_text=message.replace("\\n", "\n"), boost_color=color, boost_use_avatar="1" if use_avatar else "0")
    if outside_text is not None:
        updates["boost_outside_text"] = outside_text.replace("\\n", "\n")
    if title:
        updates["boost_title"] = title
    if image:
        updates["boost_image_url"] = image
    if thumbnail:
        updates["boost_thumbnail_url"] = thumbnail
    
    upsert_settings(guild.id, **updates)
    thumb = thumbnail or None
    preview_text = message.replace("\\n", "\n").replace("{mention}", interaction.user.mention).replace("{username}", interaction.user.name).replace("{server}", guild.name)
    preview_content = None
    if outside_text is not None:
        preview_content = outside_text.replace("\\n", "\n").replace("{mention}", interaction.user.mention).replace("{username}", interaction.user.name).replace("{server}", guild.name)
    embed = build_embed(title=title, description=preview_text, theme=color, image=image, thumbnail=None if use_avatar else thumb, user_avatar_url=interaction.user.display_avatar.url if use_avatar else None)
    await interaction.response.send_message(f"{CHECK} Boost message saved! Preview:", content=preview_content, embed=embed, ephemeral=True)


@bot.tree.command(name="boost_edit", description="Edit boost settings")
@app_commands.checks.has_permissions(manage_guild=True)
async def boost_edit(interaction: discord.Interaction):
    guild = guild_only(interaction)
    settings = get_settings(guild.id)
    if not settings or not settings["boost_channel_id"]:
        await interaction.response.send_message("Run `/set_boost_channel` first.", ephemeral=True)
        return
    
    prefill = {
        "boost_title": settings["boost_title"],
        "boost_text": settings["boost_text"],
        "boost_outside_text": settings["boost_outside_text"],
        "boost_color": settings["boost_color"],
        "boost_image_url": settings["boost_image_url"],
        "boost_thumbnail_url": settings["boost_thumbnail_url"],
        "boost_use_avatar": settings["boost_use_avatar"],
    }
    await interaction.response.send_modal(BoostEditModal(prefill=prefill))


@bot.tree.command(name="test_boost", description="Preview the boost message")
@app_commands.checks.has_permissions(manage_guild=True)
async def test_boost(interaction: discord.Interaction):
    guild = guild_only(interaction)
    settings = get_settings(guild.id)
    if not settings or not settings["boost_channel_id"]:
        await interaction.response.send_message("Run `/set_boost_channel` first.", ephemeral=True)
        return
    
    text = (settings["boost_text"] or "thank you {mention} for boosting! ♡").replace("{mention}", interaction.user.mention).replace("{username}", interaction.user.name).replace("{server}", guild.name)
    outside_text = settings["boost_outside_text"] if settings["boost_outside_text"] else None
    if outside_text:
        outside_text = outside_text.replace("{mention}", interaction.user.mention).replace("{username}", interaction.user.name).replace("{server}", guild.name).replace("\\n", "\n")
    embed = build_embed(title=settings["boost_title"], description=text, theme=settings["boost_color"] or "pink", image=settings["boost_image_url"], thumbnail=settings["boost_thumbnail_url"], user_avatar_url=interaction.user.display_avatar.url if not settings["boost_thumbnail_url"] else None)
    await interaction.response.send_message(content=outside_text, embed=embed, ephemeral=True)


# ———————————————––
# Commands — Verify
# ———————————————––

@bot.tree.command(name="verify_message", description="Create or edit the verify embed")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(title="Title ('none' to remove)", description="Description", color="Color", image="Image URL ('none' to remove)", thumbnail="Thumbnail URL ('none' to remove)", button="Button text + emoji (e.g. 💖 verify)")
async def verify_message(interaction: discord.Interaction, title: str | None = None, description: str | None = None, color: str | None = None, image: str | None = None, thumbnail: str | None = None, button: str | None = None):
    guild = interaction.guild
    updates = {}
    title = clean_input(title)
    image = clean_input(image)
    thumbnail = clean_input(thumbnail)
    
    if title is not None:
        updates["verify_title"] = title
    if description is not None:
        updates["verify_description"] = description
    if color is not None:
        updates["verify_color"] = color
    if image is not None:
        updates["verify_image_url"] = image
    if thumbnail is not None:
        updates["verify_thumbnail_url"] = thumbnail
    if button is not None:
        label, emoji = parse_button(button)
        updates["verify_button_label"] = label
        updates["verify_button_emoji"] = emoji
    
    upsert_settings(guild.id, **updates)
    row = get_settings(guild.id)
    embed = build_embed(title=row["verify_title"], description=(row["verify_description"] or "Click the button below to verify!").replace("\\n", "\n"), theme=row["verify_color"] or "pink", image=row["verify_image_url"], thumbnail=row["verify_thumbnail_url"])
    view = VerifyView(button_label=row["verify_button_label"], button_emoji=parse_emoji(row["verify_button_emoji"]))
    channel_id = row["verify_channel_id"] or interaction.channel.id
    channel = guild.get_channel(int(channel_id))
    
    await interaction.response.defer(ephemeral=True)
    if row["verify_message_id"]:
        try:
            old_msg = await channel.fetch_message(int(row["verify_message_id"]))
            await old_msg.delete()
        except Exception:
            pass
    
    new_msg = await channel.send(embed=embed, view=view)
    upsert_settings(guild.id, verify_message_id=str(new_msg.id))
    await interaction.followup.send(f"{CHECK} Verify message updated in {channel.mention}", ephemeral=True)


@bot.tree.command(name="verify_settings", description="Set verify role and channel")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(role="Role to assign", channel="Channel for verify message")
async def verify_settings(interaction: discord.Interaction, role: discord.Role, channel: discord.TextChannel):
    guild = interaction.guild
    upsert_settings(guild.id, verify_role_id=str(role.id), verify_channel_id=channel.id)
    await interaction.response.send_message(f"{CHECK} Verify setup updated\nRole: {role.mention}\nChannel: {channel.mention}", ephemeral=True)


@bot.tree.command(name="verify_responses", description="Set verify response messages")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(success_message="Message after verifying", already_verified_message="Message if already verified")
async def verify_responses(interaction: discord.Interaction, success_message: str | None = None, already_verified_message: str | None = None):
    guild = interaction.guild
    updates = {}
    if success_message:
        updates["verify_success_message"] = success_message
    if already_verified_message:
        updates["verify_already_message"] = already_verified_message
    
    if not updates:
        await interaction.response.send_message("Provide at least one field.", ephemeral=True)
        return
    
    upsert_settings(guild.id, **updates)
    await interaction.response.send_message(f"{CHECK} Verify responses updated!", ephemeral=True)


# ———————————————––
# Commands — Vouch
# ———————————————––

@bot.tree.command(name="vouch_setup", description="Set up auto-reaction in a vouch channel")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(channel="Vouch channel", emojis="Emojis to react with (space-separated)", super_reaction="Enable super reaction mode")
async def vouch_setup(interaction: discord.Interaction, channel: discord.TextChannel, emojis: str, super_reaction: bool = False):
    guild = guild_only(interaction)
    emoji_list = emojis.split()
    upsert_settings(guild.id, vouch_channel_id=str(channel.id), vouch_reaction_emoji=",".join(emoji_list), vouch_super_reaction="1" if super_reaction else "0")
    emoji_display = " ".join(emoji_list)
    await interaction.response.send_message(f"{CHECK} Vouch channel set to {channel.mention}\n**Emojis:** {emoji_display}\n**Super reaction:** {'Enabled' if super_reaction else 'Disabled'}", ephemeral=True)


@bot.tree.command(name="vouch_clear", description="Disable vouch auto-reactions")
@app_commands.checks.has_permissions(manage_guild=True)
async def vouch_clear(interaction: discord.Interaction):
    guild = guild_only(interaction)
    upsert_settings(guild.id, vouch_channel_id="", vouch_reaction_emoji="", vouch_super_reaction="0")
    await interaction.response.send_message(f"{CHECK} Vouch auto-reaction disabled.", ephemeral=True)


# ———————————————––
# Commands — Sticky
# ———————————————––

@bot.tree.command(name="sticky_set", description="Set a sticky message for this channel")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(message="Message to pin at the bottom")
async def sticky_set(interaction: discord.Interaction, message: str):
    guild = guild_only(interaction)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO sticky_messages (guild_id, channel_id, message) VALUES (?, ?, ?)", (guild.id, interaction.channel_id, message))
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"{CHECK} Sticky message set for this channel!", ephemeral=True)


@bot.tree.command(name="sticky_clear", description="Remove the sticky message from this channel")
@app_commands.checks.has_permissions(manage_messages=True)
async def sticky_clear(interaction: discord.Interaction):
    guild = guild_only(interaction)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM sticky_messages WHERE guild_id = ? AND channel_id = ?", (guild.id, interaction.channel_id))
    conn.commit()
    conn.close()
    await interaction.response.send_message("🗑️ Sticky message cleared.", ephemeral=True)


@bot.tree.command(name="sticky_view", description="See the current sticky message for this channel")
async def sticky_view(interaction: discord.Interaction):
    guild = guild_only(interaction)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT message FROM sticky_messages WHERE guild_id = ? AND channel_id = ?", (guild.id, interaction.channel_id))
    row = cur.fetchone()
    conn.close()
    
    if not row:
        await interaction.response.send_message("No sticky message set for this channel.", ephemeral=True)
        return
    
    await interaction.response.send_message(row["message"].replace("\\n", "\n"), ephemeral=True)


# ———————————————––
# Commands — Autoresponder
# ———————————————––

@bot.tree.command(name="autoresponder_add", description="Create an autoresponder trigger")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(trigger="Trigger word", message="Response message", match_type="Match type: exact or anywhere")
@app_commands.choices(match_type=[app_commands.Choice(name="exact", value="exact"), app_commands.Choice(name="anywhere", value="anywhere")])
async def autoresponder_add(interaction: discord.Interaction, trigger: str, message: str, match_type: str = "exact"):
    guild = guild_only(interaction)
    trigger = trigger.lower().strip().lstrip(".")
    conn = get_db()
    cur = conn.cursor()
    
    try:
        cur.execute("INSERT INTO autoresponders (guild_id, trigger, message, match_type) VALUES (?, ?, ?, ?)", (guild.id, trigger, message, match_type))
        conn.commit()
        await interaction.response.send_message(f"{CHECK} Autoresponder `{trigger}` created! (match: {match_type})", ephemeral=True)
    except sqlite3.IntegrityError:
        await interaction.response.send_message(f"❌ Trigger `{trigger}` already exists.", ephemeral=True)
    finally:
        conn.close()


@bot.tree.command(name="autoresponder_edit", description="Edit an autoresponder trigger")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(trigger="Trigger to edit", message="New message", match_type="New match type")
@app_commands.choices(match_type=[app_commands.Choice(name="exact", value="exact"), app_commands.Choice(name="anywhere", value="anywhere")])
async def autoresponder_edit(interaction: discord.Interaction, trigger: str, message: str | None = None, match_type: str | None = None):
    guild = guild_only(interaction)
    trigger = trigger.lower().strip().lstrip(".")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM autoresponders WHERE guild_id = ? AND trigger = ?", (guild.id, trigger))
    row = cur.fetchone()
    
    if not row:
        conn.close()
        await interaction.response.send_message(f"No autoresponder `{trigger}` found.", ephemeral=True)
        return
    
    final_message = message if message is not None else row["message"]
    final_match = match_type if match_type is not None else (row["match_type"] or "exact")
    cur.execute("UPDATE autoresponders SET message = ?, match_type = ? WHERE guild_id = ? AND trigger = ?", (final_message, final_match, guild.id, trigger))
    conn.commit()
    conn.close()
    
    await interaction.response.send_message(f"{CHECK} Autoresponder `{trigger}` updated! (match: {final_match})", ephemeral=True)


@bot.tree.command(name="autoresponder_remove", description="Delete an autoresponder trigger")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(trigger="Trigger to delete")
async def autoresponder_remove(interaction: discord.Interaction, trigger: str):
    guild = guild_only(interaction)
    trigger = trigger.lower().strip().lstrip(".")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM autoresponders WHERE guild_id = ? AND trigger = ?", (guild.id, trigger))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    
    msg = f"🗑️ Autoresponder `{trigger}` deleted." if deleted else f"No autoresponder `{trigger}` found."
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="autoresponder_list", description="List all autoresponder triggers")
@app_commands.checks.has_permissions(manage_guild=True)
async def autoresponder_list(interaction: discord.Interaction):
    guild = guild_only(interaction)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT trigger, message, match_type FROM autoresponders WHERE guild_id = ? ORDER BY trigger", (guild.id,))
    rows = cur.fetchall()
    conn.close()
    
    if not rows:
        await interaction.response.send_message("No autoresponders set up yet.", ephemeral=True)
        return
    
    embed = discord.Embed(title="⚡ Autoresponders", color=get_theme_color("pink"))
    for row in rows:
        preview = row["message"][:60] + ("..." if len(row["message"]) > 60 else "")
        embed.add_field(name=f"`{row['trigger']}` — {row['match_type'] or 'exact'}", value=preview, inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ———————————————––
# Commands — Image Responder
# ———————————————––

@bot.tree.command(name="imageresponder_add", description="Post an image when a keyword is triggered")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(trigger="Keyword", image_url="Image URL", caption="Caption text", match_type="Match type")
@app_commands.choices(match_type=[app_commands.Choice(name="exact", value="exact"), app_commands.Choice(name="anywhere", value="anywhere")])
async def imageresponder_add(interaction: discord.Interaction, trigger: str, image_url: str, caption: str | None = None, match_type: str = "exact"):
    guild = guild_only(interaction)
    trigger = trigger.lower().strip()
    conn = get_db()
    cur = conn.cursor()
    
    try:
        cur.execute("INSERT INTO image_responders (guild_id, trigger, image_url, caption, match_type) VALUES (?, ?, ?, ?, ?)", (guild.id, trigger, image_url, caption, match_type))
        conn.commit()
        await interaction.response.send_message(f"{CHECK} Image responder `{trigger}` created! (match: {match_type})", ephemeral=True)
    except sqlite3.IntegrityError:
        await interaction.response.send_message(f"❌ Trigger `{trigger}` already exists.", ephemeral=True)
    finally:
        conn.close()


@bot.tree.command(name="imageresponder_edit", description="Edit an image responder")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(trigger="Trigger to edit", image_url="New image URL", caption="New caption", match_type="New match type")
@app_commands.choices(match_type=[app_commands.Choice(name="exact", value="exact"), app_commands.Choice(name="anywhere", value="anywhere")])
async def imageresponder_edit(interaction: discord.Interaction, trigger: str, image_url: str | None = None, caption: str | None = None, match_type: str | None = None):
    guild = guild_only(interaction)
    trigger = trigger.lower().strip()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM image_responders WHERE guild_id = ? AND trigger = ?", (guild.id, trigger))
    row = cur.fetchone()
    
    if not row:
        conn.close()
        await interaction.response.send_message(f"No image responder `{trigger}` found.", ephemeral=True)
        return
    
    final_image = image_url if image_url is not None else row["image_url"]
    final_caption = caption if caption is not None else row["caption"]
    final_match = match_type if match_type is not None else (row["match_type"] or "exact")
    cur.execute("UPDATE image_responders SET image_url = ?, caption = ?, match_type = ? WHERE guild_id = ? AND trigger = ?", (final_image, final_caption, final_match, guild.id, trigger))
    conn.commit()
    conn.close()
    
    await interaction.response.send_message(f"{CHECK} Image responder `{trigger}` updated! (match: {final_match})", ephemeral=True)


@bot.tree.command(name="imageresponder_remove", description="Delete an image responder")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(trigger="Keyword to delete")
async def imageresponder_remove(interaction: discord.Interaction, trigger: str):
    guild = guild_only(interaction)
    trigger = trigger.lower().strip()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM image_responders WHERE guild_id = ? AND trigger = ?", (guild.id, trigger))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    
    msg = f"🗑️ Image responder `{trigger}` deleted." if deleted else f"No image responder `{trigger}` found."
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="imageresponder_list", description="List all image responders")
@app_commands.checks.has_permissions(manage_guild=True)
async def imageresponder_list(interaction: discord.Interaction):
    guild = guild_only(interaction)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT trigger, image_url, caption, match_type FROM image_responders WHERE guild_id = ? ORDER BY trigger", (guild.id,))
    rows = cur.fetchall()
    conn.close()
    
    if not rows:
        await interaction.response.send_message("No image responders set up yet.", ephemeral=True)
        return
    
    embed = discord.Embed(title="🖼️ Image Responders", color=get_theme_color("pink"))
    for row in rows:
        caption_text = f"\nCaption: {row['caption']}" if row["caption"] else ""
        embed.add_field(name=f"`{row['trigger']}` — {row['match_type'] or 'exact'}", value=f"[image]({row['image_url']}){caption_text}", inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ———————————————––
# Commands — Waitlist
# ———————————————––

@bot.tree.command(name="waitlist_create", description="Create a waitlist")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(title="Waitlist title", color="Theme name or hex")
async def waitlist_create(interaction: discord.Interaction, title: str | None = None, color: str = "pink"):
    if not interaction.guild:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return
    
    data = load_waitlists()
    key = get_waitlist_key(interaction.guild.id)
    final_title = title or f"{interaction.guild.name}'s waitlist"
    embed = build_waitlist_embed(interaction.guild, final_title, [], color)
    await interaction.response.send_message(f"{CHECK} Waitlist created!", ephemeral=True)
    msg = await interaction.channel.send(embed=embed)
    data[key] = {"title": final_title, "color": color, "channel_id": interaction.channel.id, "message_id": msg.id, "users": []}
    save_waitlists(data)


@bot.tree.command(name="waitlist_add", description="Add a channel to the waitlist")
@app_commands.describe(channel="Order channel", label="Optional label")
async def waitlist_add(interaction: discord.Interaction, channel: discord.TextChannel, label: str | None = None):
    data = load_waitlists()
    key = get_waitlist_key(interaction.guild.id)
    if key not in data:
        await interaction.response.send_message("Run /waitlist_create first.", ephemeral=True)
        return
    
    cid = str(channel.id)
    if any(entry_id(e) == cid for e in data[key]["users"]):
        await interaction.response.send_message("That channel is already in the waitlist.", ephemeral=True)
        return
    
    entry = {"id": cid, "label": label} if label else cid
    data[key]["users"].append(entry)
    save_waitlists(data)
    await update_waitlist_message(bot, interaction.guild.id)
    suffix = f" — {label}" if label else ""
    await interaction.response.send_message(f"{CHECK} Added {channel.mention}{suffix}", ephemeral=True)


@bot.tree.command(name="waitlist_label", description="Set or update a waitlist entry label")
@app_commands.describe(channel="Order channel", label="New label (leave blank to clear)")
async def waitlist_label(interaction: discord.Interaction, channel: discord.TextChannel, label: str | None = None):
    data = load_waitlists()
    key = get_waitlist_key(interaction.guild.id)
    if key not in data:
        await interaction.response.send_message("No waitlist found. Run /waitlist_create first.", ephemeral=True)
        return
    
    cid = str(channel.id)
    entries = data[key]["users"]
    for i, e in enumerate(entries):
        if entry_id(e) == cid:
            entries[i] = {"id": cid, "label": label} if label else cid
            save_waitlists(data)
            await update_waitlist_message(bot, interaction.guild.id)
            msg = f"{CHECK} Updated label for {channel.mention} → **{label}**" if label else f"{CHECK} Cleared label for {channel.mention}"
            await interaction.response.send_message(msg, ephemeral=True)
            return
    
    await interaction.response.send_message(f"{channel.mention} isn't in the waitlist.", ephemeral=True)


@bot.tree.command(name="waitlist_remove", description="Remove a channel from the waitlist")
@app_commands.checks.has_permissions(manage_guild=True)
async def waitlist_remove(interaction: discord.Interaction):
    data = load_waitlists()
    key = get_waitlist_key(interaction.guild.id)
    if key not in data or not data[key]["users"]:
        await interaction.response.send_message("The waitlist is empty.", ephemeral=True)
        return
    
    view = WaitlistRemoveView(interaction.guild, data[key]["users"])
    await interaction.response.send_message("Select a channel to remove:", view=view, ephemeral=True)


@bot.tree.command(name="waitlist_move", description="Reorder an entry in the waitlist")
@app_commands.checks.has_permissions(manage_guild=True)
async def waitlist_move(interaction: discord.Interaction):
    data = load_waitlists()
    key = get_waitlist_key(interaction.guild.id)
    if key not in data or not data[key]["users"]:
        await interaction.response.send_message("The waitlist is empty.", ephemeral=True)
        return
    
    entries = data[key]["users"]
    if len(entries) < 2:
        await interaction.response.send_message("Only one entry — nothing to reorder.", ephemeral=True)
        return
    
    view = WaitlistMovePickView(entries, interaction.guild)
    await interaction.response.send_message("Pick an entry to move:", view=view, ephemeral=True)


# ———————————————––
# Commands — Role Management
# ———————————————––

role_group = app_commands.Group(name="role", description="Role management")

@role_group.command(name="add", description="Add a role to a user")
@app_commands.checks.has_permissions(manage_roles=True)
@app_commands.describe(user="User", role="Role")
async def role_add(interaction: discord.Interaction, user: discord.Member, role: discord.Role):
    guild_only(interaction)
    if role in user.roles:
        await interaction.response.send_message(f"❌ {user.mention} already has {role.mention}.", ephemeral=True)
        return
    
    try:
        await user.add_roles(role)
        await interaction.response.send_message(f"{CHECK} Added {role.mention} to {user.mention}.")
    except discord.Forbidden:
        await interaction.response.send_message("⚠️ I don't have permission to assign that role.", ephemeral=True)


@role_group.command(name="remove", description="Remove a role from a user")
@app_commands.checks.has_permissions(manage_roles=True)
@app_commands.describe(user="User", role="Role")
async def role_remove(interaction: discord.Interaction, user: discord.Member, role: discord.Role):
    guild_only(interaction)
    if role not in user.roles:
        await interaction.response.send_message(f"❌ {user.mention} doesn't have {role.mention}.", ephemeral=True)
        return
    
    try:
        await user.remove_roles(role)
        await interaction.response.send_message(f"{CHECK} Removed {role.mention} from {user.mention}.")
    except discord.Forbidden:
        await interaction.response.send_message("⚠️ I don't have permission to remove that role.", ephemeral=True)


bot.tree.add_command(role_group)


# ———————————————––
# Commands — Roblox
# ———————————————––

roblox_group = app_commands.Group(name="roblox", description="Roblox utilities")

@roblox_group.command(name="calctax", description="Calculate Roblox marketplace tax")
@app_commands.describe(amount="Robux amount", discount="Apply 10% discount")
async def roblox_calctax(interaction: discord.Interaction, amount: int, discount: bool = False):
    display_amount = round(amount * 0.90) if discount else amount
    after_tax = round(display_amount * 0.70)
    to_cover_tax = round(display_amount / 0.70)

    embed = discord.Embed(color=get_theme_color("pink"))
    if discount:
        embed.add_field(name="Original amount", value=f"{amount} <:001_roblox_DNS:1501267296511856712>", inline=False)
        embed.add_field(name="After 10% discount", value=f"{display_amount} <:001_roblox_DNS:1501267296511856712>", inline=False)
    else:
        embed.add_field(name="Initial amount", value=f"{amount} <:001_roblox_DNS:1501267296511856712>", inline=False)
    
    embed.add_field(name="After Roblox tax (30%)", value=f"{after_tax} <:001_roblox_DNS:1501267296511856712>", inline=False)
    embed.add_field(name="Total cost to cover tax", value=f"{to_cover_tax} <:001_roblox_DNS:1501267296511856712>", inline=False)

    await interaction.response.send_message(embed=embed)


bot.tree.add_command(roblox_group)


# ———————————————––
# Commands — Emoji/Sticker Stealing
# ———————————————––

emoji_group = app_commands.Group(name="emoji", description="Emoji management")

@emoji_group.command(name="steal", description="Copy an emoji from another server")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(emoji="Emoji to copy", name="Optional custom name")
async def emoji_steal(interaction: discord.Interaction, emoji: str, name: str | None = None):
    guild = guild_only(interaction)
    await interaction.response.defer(ephemeral=True)
    
    custom_match = re.match(r'<a?:(\w+):(\d+)>', emoji)
    if not custom_match:
        await interaction.followup.send("❌ Please use a custom emoji.", ephemeral=True)
        return
    
    emoji_name = name or custom_match.group(1)
    emoji_id = custom_match.group(2)
    emoji_url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{'gif' if emoji.startswith('<a:') else 'png'}"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(emoji_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    await interaction.followup.send("❌ Couldn't download the emoji.", ephemeral=True)
                    return
                emoji_data = await resp.read()
        
        created_emoji = await guild.create_custom_emoji(name=emoji_name, image=emoji_data)
        await interaction.followup.send(f"{CHECK} Emoji stolen! {created_emoji} `{created_emoji.name}`", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ I don't have permission to create emojis.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)[:100]}", ephemeral=True)


bot.tree.add_command(emoji_group)


# ———————————————––
# Commands — Sticker Stealing
# ———————————————––

sticker_group = app_commands.Group(name="sticker", description="Sticker management")

@sticker_group.command(name="steal", description="Copy a sticker from another server")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(sticker_id="Sticker ID", name="Optional custom name")
async def sticker_steal(interaction: discord.Interaction, sticker_id: str, name: str | None = None):
    guild = guild_only(interaction)
    await interaction.response.defer(ephemeral=True)
    
    try:
        sticker = await bot.fetch_sticker(int(sticker_id))
    except (ValueError, discord.NotFound):
        await interaction.followup.send("❌ Sticker not found.", ephemeral=True)
        return
    
    sticker_name = name or sticker.name
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(sticker.url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    await interaction.followup.send("❌ Couldn't download the sticker.", ephemeral=True)
                    return
                sticker_data = await resp.read()
        
        created_sticker = await guild.create_sticker(
            name=sticker_name,
            description=f"Stolen from {sticker.guild.name}" if sticker.guild else "Imported sticker",
            emoji="📌",
            file=discord.File(io.BytesIO(sticker_data), filename=f"{sticker_name}.png")
        )
        await interaction.followup.send(f"{CHECK} Sticker stolen! **{created_sticker.name}**", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ I don't have permission to create stickers.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)[:100]}", ephemeral=True)


bot.tree.add_command(sticker_group)


# ———————————————––
# Run
# ———————————————––

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is not set")

bot.run(TOKEN)
