import os
import sqlite3
import asyncio
import io
from pathlib import Path
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# ———————————————––

# Setup

# ———————————————––

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
_data_dir = Path("/data") if Path("/data").exists() else Path(".")
DB_PATH = _data_dir / "bot.db"
print("DB PATH:", DB_PATH)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ———————————————––

# Database

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
            welcome_text TEXT
        )
        """
    )

    for col in ["welcome_text", "welcome_banner_url", "welcome_theme", "verify_role_id",
                "verify_message_id", "verify_channel_id", "verify_button_label",
                "verify_button_emoji", "verify_title", "verify_description",
                "verify_color", "verify_image_url", "verify_thumbnail_url",
                "verify_success_message", "verify_already_message"]:
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
        CREATE TABLE IF NOT EXISTS sticky_messages (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            PRIMARY KEY (guild_id, channel_id)
        )
        """
    )

    try:
        cur.execute("ALTER TABLE sticky_messages ADD COLUMN last_message_id INTEGER")
    except sqlite3.OperationalError:
        pass

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS autoresponders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            trigger TEXT NOT NULL,
            message TEXT NOT NULL,
            ping_roles TEXT,
            UNIQUE(guild_id, trigger)
        )
        """
    )

    conn.commit()
    conn.close()


def upsert_settings(guild_id: int, **kwargs) -> None:
    allowed = {
        "welcome_channel_id",
        "welcome_banner_url",
        "welcome_theme",
        "welcome_text",
        "verify_role_id",
        "verify_message_id",
        "verify_channel_id",
        "verify_button_label",
        "verify_button_emoji",
        "verify_title",
        "verify_description",
        "verify_color",
        "verify_image_url",
        "verify_thumbnail_url",
        "verify_success_message",
        "verify_already_message",
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


THEMES = {
    "pink": 0xF7CFE3,
    "blue": 0xCFEFFF,
    "mint": 0xD8F5E3,
    "lavender": 0xE7D9FF,
    "white": 0xF2F2F2,
    "peach": 0xFFD9C7,
}


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
    title: str | None,
    description: str | None,
    theme: str = "pink",
    image: str | None = None,
    thumbnail: str | None = None,
    footer: str | None = None,
    user_avatar_url: str | None = None,
) -> discord.Embed:
    safe_desc = description or None
    if not title and not safe_desc and not image:
        safe_desc = "\u200b"
    embed = discord.Embed(
        title=title or None,
        description=safe_desc,
        color=get_theme_color(theme),
    )
    if image:
        embed.set_image(url=image)
    if user_avatar_url:
        embed.set_thumbnail(url=user_avatar_url)
    elif thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if footer:
        embed.set_footer(text=footer)
    return embed


def guild_only(interaction: discord.Interaction) -> discord.Guild:
    if interaction.guild is None:
        raise app_commands.CheckFailure("This command only works in a server.")
    return interaction.guild


def get_settings(guild_id: int) -> sqlite3.Row | None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM settings WHERE guild_id = ?", (guild_id,))
    row = cur.fetchone()
    conn.close()
    return row


class EmbedModal(discord.ui.Modal, title="Create Embed"):
    embed_title = discord.ui.TextInput(
        label="Title",
        required=False,
        max_length=256,
        placeholder="e.g.  ✨ server rules   |   leave blank for no title",
    )
    description = discord.ui.TextInput(
        label="Description",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=4000,
        placeholder="e.g.  welcome to cwtie ugc! please read the rules below ♡",
    )
    theme = discord.ui.TextInput(
        label="Color — theme name or hex",
        required=False,
        max_length=20,
        default="pink",
        placeholder="pink / blue / mint / lavender / white / peach / #f7cfe3",
    )
    image = discord.ui.TextInput(
        label="Big image URL (bottom of embed)",
        required=False,
        max_length=1000,
        placeholder="e.g.  https://i.imgur.com/abc123.png",
    )
    thumbnail = discord.ui.TextInput(
        label="Small image URL (top-right corner)",
        required=False,
        max_length=1000,
        placeholder="e.g.  https://i.imgur.com/xyz456.png   |   ignored if use_avatar is on",
    )

    def __init__(
        self,
        use_avatar: bool,
        save_name: str | None = None,
        post_here: bool = False,
        is_edit: bool = False,
        prefill: dict | None = None,
    ):
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
            cur.execute(
                "SELECT id FROM saved_embeds WHERE guild_id = ? AND name = ?",
                (guild_id, self.save_name),
            )
            existing = cur.fetchone()
            conn.close()

            if existing and not self.is_edit:
                await interaction.response.send_message(
                    f"❌ An embed named **{self.save_name}** already exists!\n"
                    f"Use `/embededit {self.save_name}` to edit it, or choose a different name.",
                    ephemeral=True,
                )
                return

            conn = get_db()
            cur = conn.cursor()
            if existing:
                cur.execute(
                    """UPDATE saved_embeds SET
                         embed_title=?, description=?, theme=?, image_url=?,
                         thumbnail_url=?, use_avatar=?, updated_at=?
                       WHERE guild_id=? AND name=?""",
                    (
                        title_val,
                        desc_val,
                        theme_val,
                        image_val,
                        thumb_val,
                        int(self.use_avatar),
                        now,
                        guild_id,
                        self.save_name,
                    ),
                )
            else:
                cur.execute(
                    """INSERT INTO saved_embeds
                       (guild_id, name, embed_title, description, theme, image_url, thumbnail_url, use_avatar, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        guild_id,
                        self.save_name,
                        title_val,
                        desc_val,
                        theme_val,
                        image_val,
                        thumb_val,
                        int(self.use_avatar),
                        now,
                        now,
                    ),
                )
            conn.commit()
            conn.close()
            if self.post_here:
                await interaction.response.send_message(embed=embed)
                await interaction.followup.send(
                    f"✅ Also saved as **{self.save_name}**. Use `/embedsend {self.save_name}` anytime to repost it.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    f"✅ Saved as **{self.save_name}**! Use `/embedsend {self.save_name}` to post it in any channel.",
                    embed=embed,
                    ephemeral=True,
                )
        else:
            await interaction.response.send_message(embed=embed)


# ———————————————––

# Welcome Edit Modal

# ———————————————––


class WelcomeEditModal(discord.ui.Modal, title="Edit Welcome Settings"):
    welcome_text = discord.ui.TextInput(
        label="Welcome message text",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=2000,
        placeholder="e.g.  🐇 welcome {mention} to cwtie ugc! enjoy your stay ♡",
    )
    theme = discord.ui.TextInput(
        label="Color — theme name or hex",
        required=False,
        max_length=20,
        placeholder="pink / blue / mint / lavender / white / peach / #f7cfe3",
    )
    banner_url = discord.ui.TextInput(
        label="Banner image URL (big image at bottom)",
        required=False,
        max_length=1000,
        placeholder="e.g.  https://i.imgur.com/abc123.gif",
    )

    def __init__(self, prefill: dict | None = None):
        super().__init__()
        if prefill:
            if prefill.get("welcome_text"):
                self.welcome_text.default = prefill["welcome_text"]
            if prefill.get("welcome_theme"):
                self.theme.default = prefill["welcome_theme"]
            if prefill.get("welcome_banner_url"):
                self.banner_url.default = prefill["welcome_banner_url"]

    async def on_submit(self, interaction: discord.Interaction):
        guild = guild_only(interaction)
        kwargs: dict = {"welcome_text": str(self.welcome_text)}
        if str(self.theme).strip():
            kwargs["welcome_theme"] = str(self.theme).strip()
        if str(self.banner_url).strip():
            kwargs["welcome_banner_url"] = str(self.banner_url).strip()
        upsert_settings(guild.id, **kwargs)

        preview = build_embed(
            title=None,
            description=str(self.welcome_text).replace("{mention}", interaction.user.mention),
            theme=str(self.theme) or "pink",
            image=str(self.banner_url) or None,
            thumbnail=None,
            user_avatar_url=interaction.user.display_avatar.url,
        )
        await interaction.response.send_message(
            "✅ Welcome settings updated! Here's a preview:",
            embed=preview,
            ephemeral=True,
        )


# ———————————————––

# Events

# ———————————————––


@bot.event
async def on_ready():
    init_db()
    bot.add_view(VerifyView())  # re-register persistent verify button
    print(f"Bot user: {bot.user}")

    synced = await bot.tree.sync()
    print(f"Synced {len(synced)} global command(s)")
    if GUILD_ID:
        try:
            guild_obj = discord.Object(id=int(GUILD_ID.strip()))
            guild_synced = await bot.tree.sync(guild=guild_obj)
            print(f"Also synced {len(guild_synced)} guild command(s) — instant update!")
        except Exception as e:
            print(f"Guild sync skipped: {e}")


@bot.event
async def on_member_join(member: discord.Member):
    await asyncio.sleep(1)  # small delay so Discord resolves the member properly
    settings = get_settings(member.guild.id)
    if not settings or not settings["welcome_channel_id"]:
        return
    channel = member.guild.get_channel(settings["welcome_channel_id"])
    if channel is None:
        return
    description = (settings["welcome_text"] or "Welcome {mention}!").replace("{mention}", member.mention)
    embed = build_embed(
        title=None,
        description=description,
        theme=settings["welcome_theme"] or "pink",
        image=settings["welcome_banner_url"],
        thumbnail=None,
        user_avatar_url=member.display_avatar.url,
    )
    await channel.send(embed=embed)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    await bot.process_commands(message)

    if message.guild is None:
        return

    # autoresponder check
    content = message.content.strip()
    if content.startswith("."):
        trigger = content[1:].split()[0].lower() if len(content) > 1 else ""
        if trigger:
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "SELECT message, ping_roles FROM autoresponders WHERE guild_id = ? AND trigger = ?",
                (message.guild.id, trigger),
            )
            ar = cur.fetchone()
            conn.close()
            if ar:
                ping_text = ""
                if ar["ping_roles"]:
                    role_ids = [r for r in ar["ping_roles"].split(",") if r]
                    ping_text = " ".join(f"<@&{rid}>" for rid in role_ids) + " "
                await message.channel.send(ping_text + ar["message"].replace("\\n", "\n"))

    if message.guild is None:
        return

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT message, last_message_id FROM sticky_messages WHERE guild_id = ? AND channel_id = ?",
        (message.guild.id, message.channel.id),
    )
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

    new_msg = await message.channel.send(row["message"].replace("\\n", "\n"))

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE sticky_messages SET last_message_id = ? WHERE guild_id = ? AND channel_id = ?",
        (new_msg.id, message.guild.id, message.channel.id),
    )
    conn.commit()
    conn.close()


# ———————————————––

# Commands — Embed

# ———————————————––


@bot.tree.command(name="embed", description="Create and post a custom embed in this channel")
@app_commands.describe(
    title="Embed title — supports server emojis",
    description="Embed text — supports server emojis",
    color="Theme or hex color e.g. pink / #f7cfe3",
    image="Big image URL at the bottom",
    thumbnail="Small image URL top-right",
    use_avatar="Use your Discord avatar as the small top-right image",
    save="Name to save it for reuse e.g. rules",
)
async def embed_command(
    interaction: discord.Interaction,
    title: str | None = None,
    description: str | None = None,
    color: str = "pink",
    image: str | None = None,
    thumbnail: str | None = None,
    use_avatar: bool = False,
    save: str | None = None,
):
    guild_id = interaction.guild_id
    embed = build_embed(
        title=title or None,
        description=description or None,
        theme=color or "pink",
        image=image,
        thumbnail=None if use_avatar else thumbnail,
        user_avatar_url=interaction.user.display_avatar.url if use_avatar else None,
    )
    if save and guild_id:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM saved_embeds WHERE guild_id = ? AND name = ?", (guild_id, save))
        existing = cur.fetchone()
        if existing:
            conn.close()
            await interaction.response.send_message(
                f"❌ An embed named **{save}** already exists! Use `/embededit {save}` to edit it.",
                ephemeral=True,
            )
            return
        now = datetime.now(timezone.utc).isoformat()
        cur.execute(
            """INSERT INTO saved_embeds
            (guild_id, name, embed_title, description, theme, image_url, thumbnail_url, use_avatar, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, save, title, description, color, image, thumbnail, int(use_avatar), now, now),
        )
        conn.commit()
        conn.close()
        await interaction.response.send_message(embed=embed)
        await interaction.followup.send(f"✅ Saved as **{save}**! Use `/embedpost {save}` to repost anytime.", ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed)


@bot.tree.command(name="embedlist", description="List all saved embeds")
async def embedlist(interaction: discord.Interaction):
    guild = guild_only(interaction)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT name, embed_title, post_channel_id, updated_at FROM saved_embeds WHERE guild_id = ? ORDER BY name",
        (guild.id,),
    )
    rows = cur.fetchall()
    conn.close()
    if not rows:
        await interaction.response.send_message("No saved embeds yet. Use `/embed save_name:myname` to save one.", ephemeral=True)
        return
    embed = discord.Embed(title="📋 Saved Embeds", color=get_theme_color("pink"))
    for row in rows:
        ch = guild.get_channel(row["post_channel_id"]) if row["post_channel_id"] else None
        ch_text = ch.mention if ch else "*(no channel — use /embedchannel)*"
        t_text = f'"{row["embed_title"]}"' if row["embed_title"] else "*(no title)*"
        embed.add_field(
            name=f"• {row['name']}",
            value=f"Title: {t_text}\nPost to: {ch_text}\nUpdated: {row['updated_at'][:10]}",
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="embedpost", description="Post a saved embed in the current channel")
@app_commands.describe(name="Name of the saved embed to post here")
async def embedpost(interaction: discord.Interaction, name: str):
    guild = guild_only(interaction)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM saved_embeds WHERE guild_id = ? AND name = ?", (guild.id, name))
    row = cur.fetchone()
    conn.close()
    if row is None:
        await interaction.response.send_message(
            f"No embed named **{name}**. Use `/embedlist` to see all saved embeds.",
            ephemeral=True,
        )
        return
    embed = build_embed(
        title=row["embed_title"],
        description=row["description"] or "\u200b",
        theme=row["theme"] or "pink",
        image=row["image_url"],
        thumbnail=None if row["use_avatar"] else row["thumbnail_url"],
    )
    await interaction.response.defer(ephemeral=True)
    await interaction.channel.send(embed=embed)


@bot.tree.command(name="embededit", description="Edit a saved embed — opens form pre-filled with current values")
@app_commands.describe(name="Name of the embed to edit")
async def embededit(interaction: discord.Interaction, name: str):
    guild = guild_only(interaction)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM saved_embeds WHERE guild_id = ? AND name = ?", (guild.id, name))
    row = cur.fetchone()
    conn.close()
    if row is None:
        await interaction.response.send_message(f"No embed named **{name}**. Use `/embedlist`.", ephemeral=True)
        return
    prefill = {
        "embed_title": row["embed_title"],
        "description": row["description"],
        "theme": row["theme"],
        "image_url": row["image_url"],
        "thumbnail_url": row["thumbnail_url"],
    }
    await interaction.response.send_modal(
        EmbedModal(use_avatar=bool(row["use_avatar"]), save_name=name, is_edit=True, prefill=prefill)
    )


@bot.tree.command(name="embedchannel", description="Set which channel a saved embed posts to")
@app_commands.describe(name="Name of the saved embed", channel="Channel to post it in")
async def embedchannel(interaction: discord.Interaction, name: str, channel: discord.TextChannel):
    guild = guild_only(interaction)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM saved_embeds WHERE guild_id = ? AND name = ?", (guild.id, name))
    row = cur.fetchone()
    if row is None:
        conn.close()
        await interaction.response.send_message(f"No embed named **{name}**.", ephemeral=True)
        return
    cur.execute("UPDATE saved_embeds SET post_channel_id = ? WHERE guild_id = ? AND name = ?", (channel.id, guild.id, name))
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"✅ **{name}** will post to {channel.mention}.", ephemeral=True)


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
    if deleted:
        await interaction.response.send_message(f"🗑️ Deleted **{name}**.", ephemeral=True)
    else:
        await interaction.response.send_message(f"No embed named **{name}**.", ephemeral=True)


# ———————————————––

# Commands — Welcome

# ———————————————––


@bot.tree.command(name="welcome_setup", description="Set up the welcome message")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    welcome_channel="Channel where welcome messages will be sent",
    welcome_text="Welcome message — use {mention} to tag the new member, supports :cwutie: emojis",
    color="Theme name or hex like #f7cfe3",
    banner_url="Big image URL at the bottom of the welcome embed",
)
async def welcome_setup(
    interaction: discord.Interaction,
    welcome_channel: discord.TextChannel,
    welcome_text: str,
    color: str = "pink",
    banner_url: str | None = None,
):
    guild = guild_only(interaction)
    kwargs = dict(welcome_channel_id=welcome_channel.id, welcome_text=welcome_text.replace("\n", "\n"), welcome_theme=color)
    if banner_url:
        kwargs["welcome_banner_url"] = banner_url
    upsert_settings(guild.id, **kwargs)
    preview = build_embed(
        title=None,
        description=welcome_text.replace("{mention}", interaction.user.mention),
        theme=color,
        image=banner_url,
        thumbnail=None,
        user_avatar_url=interaction.user.display_avatar.url,
    )
    await interaction.response.send_message("✅ Welcome message saved! Preview:", embed=preview, ephemeral=True)


@bot.tree.command(name="welcome_edit", description="Edit the welcome message")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    welcome_text="New welcome text — use {mention} to tag new member, supports :cwutie: emojis",
    color="New color — theme name or hex (leave blank to keep current)",
    banner_url="New banner image URL (leave blank to keep current)",
)
async def welcome_edit(
    interaction: discord.Interaction,
    welcome_text: str | None = None,
    color: str | None = None,
    banner_url: str | None = None,
):
    guild = guild_only(interaction)
    settings = get_settings(guild.id)
    if not settings or not settings["welcome_channel_id"]:
        await interaction.response.send_message("Run `/welcome_setup` first to set a channel.", ephemeral=True)
        return
    kwargs = {}
    if welcome_text:
        kwargs["welcome_text"] = welcome_text.replace("\n", "\n")
    if color:
        kwargs["welcome_theme"] = color
    if banner_url:
        kwargs["welcome_banner_url"] = banner_url
    if not kwargs:
        await interaction.response.send_message("Please provide at least one field to update.", ephemeral=True)
        return
    upsert_settings(guild.id, **kwargs)
    updated = get_settings(guild.id)
    preview = build_embed(
        title=None,
        description=(updated["welcome_text"] or "Welcome!").replace("{mention}", interaction.user.mention),
        theme=updated["welcome_theme"] or "pink",
        image=updated["welcome_banner_url"],
        thumbnail=None,
        user_avatar_url=interaction.user.display_avatar.url,
    )
    await interaction.response.send_message("✅ Welcome message updated! Preview:", embed=preview, ephemeral=True)


@bot.tree.command(name="welcome_test", description="Preview the welcome embed (only visible to you)")
@app_commands.checks.has_permissions(manage_guild=True)
async def welcome_test(interaction: discord.Interaction):
    guild = guild_only(interaction)
    settings = get_settings(guild.id)
    if not settings or not settings["welcome_channel_id"]:
        await interaction.response.send_message("Run `/welcome_setup` first.", ephemeral=True)
        return
    description = (settings["welcome_text"] or "Welcome {mention}!").replace("{mention}", interaction.user.mention)
    embed = build_embed(
        title=None,
        description=description,
        theme=settings["welcome_theme"] or "pink",
        image=settings["welcome_banner_url"],
        thumbnail=None,
        user_avatar_url=interaction.user.display_avatar.url,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="themes", description="Show available embed colors and button styles")
async def themes(interaction: discord.Interaction):
    names = ", ".join(THEMES.keys())
    await interaction.response.send_message(
        f"**Embed colors:** {names}\nOr use any hex like `#f7cfe3`",
        ephemeral=True,
    )


# ———————————————––

# Verify Button

# ———————————————––


class VerifyButton(discord.ui.Button):
    def __init__(self, label: str = "Verify", emoji=None):
        super().__init__(
            label=label or "Verify",
            emoji=emoji,
            style=discord.ButtonStyle.secondary,
            custom_id="verify_button",
        )

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
            msg = (settings["verify_already_message"] or "✅ You're already verified!")
            return await interaction.response.send_message(msg, ephemeral=True)

        try:
            await interaction.user.add_roles(role)
            msg = (settings["verify_success_message"] or "✅ You've been verified!")
            await interaction.response.send_message(msg, ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("⚠️ I need higher permissions to assign this role.", ephemeral=True)

class VerifyView(discord.ui.View):
    def __init__(self, button_label: str = "Verify", button_emoji=None):
        super().__init__(timeout=None)
        self.add_item(VerifyButton(label=button_label, emoji=button_emoji))

@bot.tree.command(name="verify_edit", description="Edit the verify embed — deletes the old one and reposts with your changes")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    role="Role to assign on verify (leave blank to keep current)",
    channel="Channel to repost in (leave blank to keep current)",
    button_label="Text on the verify button (leave blank to keep current)",
    button_emoji="Emoji for the button (leave blank to keep current)",
    title="Embed title (leave blank to keep current)",
    clear_title="Set to True to remove the title entirely",
    description="Embed description — supports \\n for newlines (leave blank to keep current)",
    color="Theme name or hex color (leave blank to keep current)",
    image="Big image URL (leave blank to keep current)",
    clear_image="Set to True to remove the big image",
    thumbnail="Small image URL top right (leave blank to keep current)",
    clear_thumbnail="Set to True to remove the thumbnail",
    success_message="Message shown after verifying (leave blank to keep current)",
    already_verified_message="Message shown if already verified (leave blank to keep current)"
)
async def verify_edit(
    interaction: discord.Interaction,
    role: discord.Role = None,
    channel: discord.TextChannel = None,
    button_label: str = None,
    button_emoji: str = None,
    title: str = None,
    clear_title: bool = False,
    description: str = None,
    color: str = None,
    image: str = None,
    clear_image: bool = False,
    thumbnail: str = None,
    clear_thumbnail: bool = False,
    success_message: str = None,
    already_verified_message: str = None
):
    guild = interaction.guild
    updates = {}
    
    # Standard Updates
    if role: updates["verify_role_id"] = str(role.id)
    if channel: updates["verify_channel_id"] = channel.id
    if button_label is not None: updates["verify_button_label"] = button_label
    if button_emoji is not None: updates["verify_button_emoji"] = button_emoji
    if description is not None: updates["verify_description"] = description
    if color is not None: updates["verify_color"] = color
    if success_message is not None: updates["verify_success_message"] = success_message
    if already_verified_message is not None: updates["verify_already_message"] = already_verified_message

    # Clear Logic (To remove fields like title or images)
    if clear_title: updates["verify_title"] = "" 
    elif title is not None: updates["verify_title"] = title

    if clear_image: updates["verify_image_url"] = ""
    elif image is not None: updates["verify_image_url"] = image

    if clear_thumbnail: updates["verify_thumbnail_url"] = ""
    elif thumbnail is not None: updates["verify_thumbnail_url"] = thumbnail

    upsert_settings(guild.id, **updates)
    row = get_settings(guild.id)
    
    embed = build_embed(
        title=row["verify_title"] if row["verify_title"] else None,
        description=(row["verify_description"] or "Click the button below to verify!").replace("\\n", "\n"),
        theme=row["verify_color"] or "pink",
        image=row["verify_image_url"] if row["verify_image_url"] else None,
        thumbnail=row["verify_thumbnail_url"] if row["verify_thumbnail_url"] else None
    )
    
    view = VerifyView(
        button_label=row["verify_button_label"],
        button_emoji=parse_emoji(row["verify_button_emoji"])
    )

    target_channel_id = row["verify_channel_id"] or interaction.channel.id
    target_channel = guild.get_channel(int(target_channel_id))
    
    await interaction.response.defer(ephemeral=True)

    if row["verify_message_id"]:
        try:
            old_msg = await target_channel.fetch_message(int(row["verify_message_id"]))
            await old_msg.delete()
        except:
            pass

    new_msg = await target_channel.send(embed=embed, view=view)
    upsert_settings(guild.id, verify_message_id=str(new_msg.id))

    await interaction.followup.send(f"✅ Verification updated in {target_channel.mention}!", ephemeral=True)

# ———————————————––

# Commands — Sticky

# ———————————————––


@bot.tree.command(name="sticky_set", description="Set a sticky message for this channel")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(message="The message to pin at the bottom of this channel")
async def sticky_set(interaction: discord.Interaction, message: str):
    guild = guild_only(interaction)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO sticky_messages (guild_id, channel_id, message) VALUES (?, ?, ?)",
        (guild.id, interaction.channel_id, message),
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message("✅ Sticky message set for this channel!", ephemeral=True)


@bot.tree.command(name="sticky_clear", description="Remove the sticky message from this channel")
@app_commands.checks.has_permissions(manage_messages=True)
async def sticky_clear(interaction: discord.Interaction):
    guild = guild_only(interaction)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM sticky_messages WHERE guild_id = ? AND channel_id = ?",
        (guild.id, interaction.channel_id),
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message("🗑️ Sticky message cleared.", ephemeral=True)


@bot.tree.command(name="sticky_view", description="See the current sticky message for this channel")
async def sticky_view(interaction: discord.Interaction):
    guild = guild_only(interaction)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT message FROM sticky_messages WHERE guild_id = ? AND channel_id = ?",
        (guild.id, interaction.channel_id),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        await interaction.response.send_message("No sticky message set for this channel.", ephemeral=True)
        return
    await interaction.response.send_message(row["message"].replace("\\n", "\n"), ephemeral=True)


# ———————————————––

# Commands — Autoresponder

# ———————————————––


@bot.tree.command(name="autoresponder_add", description="Create a new autoresponder trigger")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    trigger="The trigger word after the dot — e.g. 'ask' for .ask",
    message="The message to send when triggered — supports \\n for newlines",
)
async def autoresponder_add(
    interaction: discord.Interaction,
    trigger: str,
    message: str,
):
    guild = guild_only(interaction)
    trigger = trigger.lower().strip().lstrip(".")

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO autoresponders (guild_id, trigger, message, ping_roles) VALUES (?, ?, ?, ?)",
            (guild.id, trigger, message, None),
        )
        conn.commit()
        await interaction.response.send_message(f"✅ Autoresponder `.{trigger}` created!", ephemeral=True)
    except sqlite3.IntegrityError:
        await interaction.response.send_message(
            f"❌ A trigger `.{trigger}` already exists. Use `/autoresponder_edit` to update it.", ephemeral=True
        )
    finally:
        conn.close()


@bot.tree.command(name="autoresponder_edit", description="Edit an existing autoresponder trigger")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    trigger="The trigger to edit — e.g. 'ask' for .ask",
    message="New message (leave blank to keep current)",
)
async def autoresponder_edit(
    interaction: discord.Interaction,
    trigger: str,
    message: str | None = None,
):
    guild = guild_only(interaction)
    trigger = trigger.lower().strip().lstrip(".")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM autoresponders WHERE guild_id = ? AND trigger = ?", (guild.id, trigger))
    row = cur.fetchone()
    if not row:
        conn.close()
        await interaction.response.send_message(f"No autoresponder `.{trigger}` found.", ephemeral=True)
        return

    final_message = message if message is not None else row["message"]

    cur.execute(
        "UPDATE autoresponders SET message = ? WHERE guild_id = ? AND trigger = ?",
        (final_message, guild.id, trigger),
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"✅ Autoresponder `.{trigger}` updated!", ephemeral=True)


@bot.tree.command(name="autoresponder_remove", description="Delete an autoresponder trigger")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(trigger="The trigger to delete — e.g. 'ask' for .ask")
async def autoresponder_remove(interaction: discord.Interaction, trigger: str):
    guild = guild_only(interaction)
    trigger = trigger.lower().strip().lstrip(".")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM autoresponders WHERE guild_id = ? AND trigger = ?", (guild.id, trigger))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    if deleted:
        await interaction.response.send_message(f"🗑️ Autoresponder `.{trigger}` deleted.", ephemeral=True)
    else:
        await interaction.response.send_message(f"No autoresponder `.{trigger}` found.", ephemeral=True)


@bot.tree.command(name="autoresponder_list", description="List all autoresponder triggers")
@app_commands.checks.has_permissions(manage_guild=True)
async def autoresponder_list(interaction: discord.Interaction):
    guild = guild_only(interaction)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT trigger, message, ping_roles FROM autoresponders WHERE guild_id = ? ORDER BY trigger", (guild.id,))
    rows = cur.fetchall()
    conn.close()
    if not rows:
        await interaction.response.send_message("No autoresponders set up yet.", ephemeral=True)
        return
    embed = discord.Embed(title="⚡ Autoresponders", color=get_theme_color("pink"))
    for row in rows:
        roles_text = ""
        if row["ping_roles"]:
            roles_text = "\nPings: " + " ".join(f"<@&{r}>" for r in row["ping_roles"].split(",") if r)
        preview = row["message"][:60] + ("..." if len(row["message"]) > 60 else "")
        embed.add_field(name=f"`.{row['trigger']}`", value=f"{preview}{roles_text}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is not set")

bot.run(TOKEN)
