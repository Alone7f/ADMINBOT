#!/usr/bin/env python3
"""
Hukm — private Discord admin bot. Ek file. Render Background Worker par chalao.

Sirf OWNER_IDS wale log commands chala sakte hain.
- Slash menu mein koi command nahi (doosron ko kuch show nahi hota).
- Koi aur prefix likhe to bot bilkul chup rehta hai.
- Aapka command message server se mit jata hai.
- Jawab aapke DM mein aata hai.

RENDER
  New → Background Worker → Python
  Build:  pip install -r requirements.txt
  Start:  python bot.py
  Env:    DISCORD_TOKEN = bot token

LOCAL
  pip install -r requirements.txt
  token.txt mein sirf token likho (ek line), phir:  python bot.py
  Ya:  DISCORD_TOKEN=... python bot.py

Discord Developer Portal → Bot → dono ON hon:
  Server Members Intent
  Message Content Intent

Invite (Administrator — members, roles, channels, server):
  https://discord.com/oauth2/authorize?client_id=APP_ID&permissions=8&scope=bot

Bot ka role, Server Settings → Roles mein upar rakho.
Warna wo upar walon par action nahi kar payega.

Mass-ban, sab channels delete, sabko kick — jaan bujh kar nahi hain.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import threading
from datetime import timedelta
from typing import Optional

import discord
from discord.ext import commands


# Sirf ye IDs bot use kar sakti hain. Aur ID chahiye to yahan add karo.
OWNER_IDS = {
    1340670338996113533,
}

PREFIX = os.getenv("BOT_PREFIX", "!").strip() or "!"
ACCENT = 0xE07A3D

_extra = os.getenv("EXTRA_OWNER_IDS", "")
for _part in _extra.split(","):
    _part = _part.strip()
    if _part.isdigit():
        OWNER_IDS.add(int(_part))

# guild_id -> {channel_id: pehle wali send_messages value}
LOCKDOWN: dict[int, dict[int, Optional[bool]]] = {}

MENTION_RE = re.compile(r"<#(\d+)>")
HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def load_token() -> str:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if token:
        return token
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.txt")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8-sig") as handle:
            return handle.read().strip()
    return ""


def start_keepalive() -> None:
    """Render Web Service PORT bind karti hai. Worker par PORT nahi hota — skip."""
    raw = os.getenv("PORT", "").strip()
    if not raw:
        return
    port = int(raw)

    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[health] 0.0.0.0:{port}", flush=True)


def split_chunks(text: str, limit: int = 1800) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf = ""
    for line in text.split("\n"):
        piece = line if len(line) <= limit else line[:limit]
        if buf and len(buf) + 1 + len(piece) > limit:
            parts.append(buf)
            buf = piece
        else:
            buf = f"{buf}\n{piece}" if buf else piece
    if buf:
        parts.append(buf)
    return parts or [text[:limit]]


def audit(ctx: commands.Context, extra: Optional[str] = None) -> str:
    base = f"Hukm {ctx.author} ({ctx.author.id})"
    if extra:
        base = f"{base}: {extra}"
    return base[:512]


def snowflake(text: str) -> Optional[int]:
    raw = text.strip()
    if raw.startswith("<@") and raw.endswith(">"):
        raw = raw[2:-1].lstrip("!").lstrip("&")
    if raw.isdigit():
        return int(raw)
    return None


def parse_duration(text: str) -> Optional[int]:
    match = re.fullmatch(r"(\d+)([smhd])", text.strip().lower())
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    scale = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return amount * scale


def parse_flag(text: str) -> Optional[bool]:
    value = text.strip().lower()
    if value in {"on", "yes", "true", "1", "enable", "enabled"}:
        return True
    if value in {"off", "no", "false", "0", "disable", "disabled", "none"}:
        return False
    return None


def hierarchy_block(ctx: commands.Context, member: discord.Member) -> Optional[str]:
    if member.id == ctx.author.id:
        return "Khud par ye command nahi chalegi."
    if member.id == ctx.guild.owner_id:
        return "Server owner par ye action nahi ho sakti."
    if member.id == ctx.guild.me.id:
        return "Bot khud par ye action nahi karega."
    me = ctx.guild.me
    if member.top_role >= me.top_role:
        return "Bot ka role is member ke role se upar rakho (Server Settings → Roles)."
    if ctx.author.id != ctx.guild.owner_id and member.top_role >= ctx.author.top_role:
        return "Ye member aap ke role se upar ya barabar hai."
    return None


def role_block(ctx: commands.Context, role: discord.Role) -> Optional[str]:
    if role.is_default():
        return "@everyone ko edit / delete nahi kar sakte."
    if role.managed:
        return "Ye managed role hai (booster ya integration). Discord ise lock rakhta hai."
    if role >= ctx.guild.me.top_role:
        return "Ye role bot ke role se upar hai. Bot ka role upar move karo."
    return None


def take_channel(ctx: commands.Context, args: str) -> tuple[Optional[discord.abc.GuildChannel], str]:
    args = args.strip()
    match = MENTION_RE.search(args)
    if not match:
        return None, args
    channel = ctx.guild.get_channel(int(match.group(1)))
    rest = (args[: match.start()] + args[match.end() :]).strip()
    return channel, rest


def split_category(guild: discord.Guild, args: str) -> tuple[str, Optional[discord.CategoryChannel], Optional[str]]:
    if "|" not in args:
        return args.strip(), None, None
    name, cat_name = [part.strip() for part in args.split("|", 1)]
    if not cat_name:
        return name, None, None
    category = discord.utils.find(lambda item: item.name.lower() == cat_name.lower(), guild.categories)
    if category is None:
        return name, None, cat_name
    return name, category, None


async def delete_later(message: discord.Message, delay: int = 15) -> None:
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except discord.HTTPException:
        pass


async def owner_msg(ctx: commands.Context, text: str) -> None:
    parts = split_chunks(text.strip())
    if not parts or not parts[0]:
        return
    if ctx.guild is None:
        for part in parts:
            await ctx.send(part)
        return
    try:
        for part in parts:
            await ctx.author.send(part)
    except discord.HTTPException:
        for part in parts:
            sent = await ctx.send(part)
            asyncio.create_task(delete_later(sent))


async def hide_message(ctx: commands.Context) -> None:
    if ctx.guild is None or ctx.message is None:
        return
    try:
        await ctx.message.delete()
    except (discord.HTTPException, discord.NotFound):
        pass


HELP: dict[str, dict[str, str]] = {
    "help": {"cat": "general", "use": "!help [members|ban]", "about": "Commands ki list, ya ek command ki detail."},
    "ping": {"cat": "general", "use": "!ping", "about": "Bot online hai ya nahi, latency ke sath."},
    "whoami": {"cat": "general", "use": "!whoami", "about": "Aapki user ID confirm karta hai."},
    "invite": {"cat": "general", "use": "!invite", "about": "Administrator wala invite link."},
    "servers": {"cat": "general", "use": "!servers", "about": "Bot kin servers mein hai."},
    "ban": {"cat": "members", "use": "!ban @user [0-7] [wajah]", "about": "Ban. 0-7 un dino ke messages bhi mita sakte ho."},
    "unban": {"cat": "members", "use": "!unban USER_ID [wajah]", "about": "User ID se unban."},
    "kick": {"cat": "members", "use": "!kick @user [wajah]", "about": "Server se nikaalo, ban nahi."},
    "timeout": {"cat": "members", "use": "!timeout @user 10m [wajah]", "about": "Mute. Time: 30s, 10m, 2h, 1d. Max 28d. Alias: mute."},
    "untimeout": {"cat": "members", "use": "!untimeout @user", "about": "Timeout hatao. Alias: unmute."},
    "nick": {"cat": "members", "use": "!nick @user naya naam", "about": "Nickname set karo."},
    "clearnick": {"cat": "members", "use": "!clearnick @user", "about": "Nickname hatao."},
    "userinfo": {"cat": "members", "use": "!userinfo [@user]", "about": "Member ki ID, roles, join date."},
    "members": {"cat": "members", "use": "!members naam", "about": "Naam ya ID se members dhoondo."},
    "dm": {"cat": "members", "use": "!dm @user message", "about": "Ek member ko bot se DM."},
    "move": {"cat": "members", "use": "!move @user Voice Name", "about": "Member ko voice channel mein move karo."},
    "disconnect": {"cat": "members", "use": "!disconnect @user", "about": "Voice se nikaalo."},
    "vcmute": {"cat": "members", "use": "!vcmute @user", "about": "Server mute (voice)."},
    "vcunmute": {"cat": "members", "use": "!vcunmute @user", "about": "Server mute hatao."},
    "deafen": {"cat": "members", "use": "!deafen @user", "about": "Server deafen."},
    "undeafen": {"cat": "members", "use": "!undeafen @user", "about": "Server deafen hatao."},
    "roles": {"cat": "roles", "use": "!roles", "about": "Saari roles ki list."},
    "createrole": {"cat": "roles", "use": "!createrole Name #ff8800", "about": "Role banao. Color optional."},
    "deleterole": {"cat": "roles", "use": "!deleterole Role Name", "about": "Role delete."},
    "editrole": {"cat": "roles", "use": "!editrole \"Role\" color #ffffff", "about": "Fields: name, color, hoist, mention, admin."},
    "addrole": {"cat": "roles", "use": "!addrole @user Role Name", "about": "Member ko role do."},
    "removerole": {"cat": "roles", "use": "!removerole @user Role Name", "about": "Member se role hatao."},
    "roleall": {"cat": "roles", "use": "!roleall Role Name yes", "about": "Har human member ko role. Ek dafa 250. `yes` zaroori."},
    "channels": {"cat": "channels", "use": "!channels", "about": "Channels ki list."},
    "createtext": {"cat": "channels", "use": "!createtext naam | Category", "about": "Text channel. Category `|` ke baad, optional."},
    "createvoice": {"cat": "channels", "use": "!createvoice naam | Category", "about": "Voice channel."},
    "createcategory": {"cat": "channels", "use": "!createcategory naam", "about": "Category banao."},
    "createforum": {"cat": "channels", "use": "!createforum naam | Category", "about": "Forum channel."},
    "createstage": {"cat": "channels", "use": "!createstage naam | Category", "about": "Stage channel."},
    "deletechannel": {"cat": "channels", "use": "!deletechannel #channel", "about": "Sirf mention ki hui channel delete hoti hai."},
    "rename": {"cat": "channels", "use": "!rename naya-naam", "about": "Current channel, ya `!rename #channel naya-naam`."},
    "slowmode": {"cat": "channels", "use": "!slowmode 10", "about": "Seconds. 0 band. `#channel` mention ho sakta hai."},
    "lock": {"cat": "channels", "use": "!lock [#channel]", "about": "@everyone type nahi kar sakta."},
    "unlock": {"cat": "channels", "use": "!unlock [#channel]", "about": "Lock hatao."},
    "hide": {"cat": "channels", "use": "!hide [#channel]", "about": "@everyone channel nahi dekh sakta."},
    "unhide": {"cat": "channels", "use": "!unhide [#channel]", "about": "Channel wapas visible."},
    "clone": {"cat": "channels", "use": "!clone [#channel]", "about": "Channel ki copy."},
    "topic": {"cat": "channels", "use": "!topic text yahan", "about": "Topic. `#channel` se shuru karo agar doosri channel ho."},
    "nsfw": {"cat": "channels", "use": "!nsfw on", "about": "`on` ya `off`. Optional `#channel`."},
    "purge": {"cat": "channels", "use": "!purge 50", "about": "1-100 messages. Pinned rehte hain. Alias: clear."},
    "del": {"cat": "channels", "use": "!del", "about": "Message par reply karke `!del`, ya `!del MESSAGE_ID`."},
    "pin": {"cat": "channels", "use": "!pin", "about": "Reply karke pin, ya `!pin MESSAGE_ID`."},
    "unpin": {"cat": "channels", "use": "!unpin", "about": "Reply karke unpin, ya `!unpin MESSAGE_ID`."},
    "serverinfo": {"cat": "server", "use": "!serverinfo", "about": "Server ki detail."},
    "servername": {"cat": "server", "use": "!servername Naya Naam", "about": "Server ka naam badlo."},
    "seticon": {"cat": "server", "use": "!seticon", "about": "Image attach karke ye command likho."},
    "nickbot": {"cat": "server", "use": "!nickbot Naam", "about": "Is server par bot ka nickname."},
    "clearnickbot": {"cat": "server", "use": "!clearnickbot", "about": "Bot ka nickname hatao."},
    "bans": {"cat": "server", "use": "!bans", "about": "Ban list (20 tak)."},
    "audit": {"cat": "server", "use": "!audit [20]", "about": "Audit log. Max 30."},
    "lockdown": {"cat": "server", "use": "!lockdown yes", "about": "Saare text channels par @everyone ka type band."},
    "unlockdown": {"cat": "server", "use": "!unlockdown", "about": "Is session ka lockdown wapas."},
    "invitehere": {"cat": "server", "use": "!invitehere [uses] [hours]", "about": "Invite. 0 = unlimited / kabhi expire nahi. Max 100 uses, 168 hours."},
    "say": {"cat": "server", "use": "!say #channel message", "about": "Bot message bhejta hai. @everyone ping nahi hota."},
    "embed": {"cat": "server", "use": "!embed Title | text", "about": "Embed. `#channel` se shuru kar sakte ho."},
    "announce": {"cat": "server", "use": "!announce #channel message", "about": "Jaise say, lekin @everyone aur @here ping ho sakte hain."},
    "prune": {"cat": "server", "use": "!prune 7 yes", "about": "Inactive members. Bina yes sirf count. Days 1-30."},
    "leave": {"cat": "server", "use": "!leave yes", "about": "Bot is server ko chhor deta hai."},
}

CATEGORIES = ("general", "members", "roles", "channels", "server")


intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix=PREFIX,
    intents=intents,
    help_command=None,
    case_insensitive=True,
    strip_after_prefix=True,
    allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False),
)


@bot.check
async def only_owner(ctx: commands.Context) -> bool:
    return ctx.author.id in OWNER_IDS


@bot.before_invoke
async def log_command(ctx: commands.Context) -> None:
    guild = ctx.guild.id if ctx.guild else "dm"
    print(f"[cmd] {ctx.command} guild={guild} user={ctx.author.id}", flush=True)


@bot.after_invoke
async def hide_after(ctx: commands.Context) -> None:
    await hide_message(ctx)


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or message.author.id not in OWNER_IDS:
        return
    await bot.process_commands(message)


@bot.event
async def on_ready() -> None:
    if getattr(bot, "_announced", False):
        return
    bot._announced = True  # type: ignore[attr-defined]
    user = bot.user
    print(f"Logged in as {user} ({user.id})", flush=True)
    print("Owners: " + ", ".join(str(item) for item in sorted(OWNER_IDS)), flush=True)
    print(f"Prefix: {PREFIX}", flush=True)
    print(
        f"Invite: https://discord.com/oauth2/authorize?client_id={user.id}&permissions=8&scope=bot",
        flush=True,
    )


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    me = guild.me
    admin = bool(me and me.guild_permissions.administrator)
    print(f"[join] {guild.name} ({guild.id}) admin={admin}", flush=True)
    text = (
        f"Bot **{guild.name}** mein add ho gaya.\n"
        f"Administrator: {'haan' if admin else 'nahi — invite dubara Administrator ke sath karo (`!invite`).'}\n"
        f"Commands sirf owner IDs chala sakti hain. Prefix `{PREFIX}`.\n"
        f"Server channel mein `{PREFIX}help` likho. Jawab DM mein aayega.\n"
        "Bot ka role list mein upar rakho."
    )
    for owner_id in OWNER_IDS:
        try:
            user = await bot.fetch_user(owner_id)
            await user.send(text)
        except discord.HTTPException:
            pass


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if ctx.author.id not in OWNER_IDS:
        return
    await hide_message(ctx)
    if isinstance(error, commands.CommandNotFound):
        await owner_msg(ctx, f"Aisi command nahi. `{PREFIX}help` likho.")
        return
    if isinstance(error, commands.NoPrivateMessage):
        await owner_msg(ctx, "Ye command server ke channel mein chalao, DM mein nahi.")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await owner_msg(ctx, f"Argument kam hai. `{PREFIX}help {ctx.command}` dekho.")
        return
    if isinstance(error, (commands.MemberNotFound, commands.UserNotFound)):
        await owner_msg(ctx, "Member nahi mila. @mention ya ID use karo.")
        return
    if isinstance(error, commands.RoleNotFound):
        await owner_msg(ctx, "Role nahi mili. Poora naam likho.")
        return
    if isinstance(error, commands.ChannelNotFound):
        await owner_msg(ctx, "Channel nahi mili. #mention use karo.")
        return
    if isinstance(error, commands.BadArgument):
        await owner_msg(ctx, f"Argument galat hai. `{PREFIX}help {ctx.command}` dekho.")
        return
    if isinstance(error, commands.CheckFailure):
        return
    original = getattr(error, "original", error)
    print(f"[error] {ctx.command}: {original}", flush=True)
    if isinstance(original, discord.Forbidden):
        await owner_msg(ctx, "Discord ne permission se mana kar diya. Bot Administrator ho aur us ka role upar ho.")
        return
    if isinstance(original, discord.HTTPException):
        await owner_msg(ctx, f"Discord ne reject kiya: {original}"[:500])
        return
    await owner_msg(ctx, f"Error: {original}"[:500])


def render_help(query: str) -> str:
    aliases = {
        "mute": "timeout",
        "unmute": "untimeout",
        "clear": "purge",
        "delete": "del",
        "commands": "help",
        "id": "whoami",
    }
    query = aliases.get(query.strip().lower(), query.strip().lower())
    if not query:
        lines = [
            f"Prefix `{PREFIX}`. Sirf aap chala sakte ho. Jawab DM mein aata hai.",
            "Detail: `!help ban`  |  group: `!help members`",
            "Groups: general, members, roles, channels, server",
            "",
        ]
        for cat in CATEGORIES:
            names = [f"`{PREFIX}{name}`" for name, row in HELP.items() if row["cat"] == cat]
            lines.append(f"**{cat}**")
            lines.append(" ".join(names))
            lines.append("")
        return "\n".join(lines).strip()
    if query in CATEGORIES:
        lines = [f"**{query}**", ""]
        for name, row in HELP.items():
            if row["cat"] == query:
                lines.append(f"`{row['use']}` — {row['about']}")
        return "\n".join(lines)
    row = HELP.get(query)
    if row is None:
        return f"`{query}` koi command nahi. `{PREFIX}help` likho."
    return f"`{row['use']}`\n{row['about']}\nGroup: {row['cat']}"


async def make_channel(
    ctx: commands.Context,
    kind: str,
    args: str,
) -> None:
    name, category, missing = split_category(ctx.guild, args)
    if not name:
        await owner_msg(ctx, "Channel ka naam do.")
        return
    if missing:
        await owner_msg(ctx, f"Category nahi mili: **{missing}**")
        return
    reason = audit(ctx)
    if kind == "text":
        channel = await ctx.guild.create_text_channel(name, category=category, reason=reason)
    elif kind == "voice":
        channel = await ctx.guild.create_voice_channel(name, category=category, reason=reason)
    elif kind == "category":
        channel = await ctx.guild.create_category(name, reason=reason)
    elif kind == "forum":
        channel = await ctx.guild.create_forum(name, category=category, reason=reason)
    elif kind == "stage":
        channel = await ctx.guild.create_stage_channel(name, category=category, reason=reason)
    else:
        await owner_msg(ctx, "Channel type galat hai.")
        return
    parent = f" in {category.name}" if category else ""
    await owner_msg(ctx, f"Ban gayi: **{channel.name}** ({kind}{parent})")


async def set_everyone(ctx: commands.Context, args: str, changes: dict[str, Optional[bool]], label: str) -> None:
    mentioned, _rest = take_channel(ctx, args)
    channel = mentioned or ctx.channel
    if not isinstance(channel, discord.TextChannel):
        await owner_msg(ctx, "Ye sirf text channel par chalta hai. #mention do.")
        return
    overwrite = channel.overwrites_for(ctx.guild.default_role)
    for key, value in changes.items():
        setattr(overwrite, key, value)
    await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=audit(ctx, label))
    await owner_msg(ctx, f"**{channel.name}**: {label}.")


async def voice_action(ctx: commands.Context, member: discord.Member, **kwargs: bool) -> Optional[str]:
    blocked = hierarchy_block(ctx, member)
    if blocked:
        return blocked
    if member.voice is None:
        return "Ye member kisi voice channel mein nahi."
    await member.edit(reason=audit(ctx), **kwargs)
    return None


async def fetch_referenced(ctx: commands.Context, message_id: Optional[str]) -> Optional[discord.Message]:
    target: Optional[int] = None
    if ctx.message.reference and ctx.message.reference.message_id:
        target = ctx.message.reference.message_id
    elif message_id:
        found = snowflake(message_id)
        if found:
            target = found
    if target is None or not isinstance(ctx.channel, discord.abc.Messageable):
        return None
    try:
        return await ctx.channel.fetch_message(target)
    except discord.HTTPException:
        return None


@bot.command(name="help", aliases=["commands"])
async def help_cmd(ctx: commands.Context, *, query: str = "") -> None:
    await owner_msg(ctx, render_help(query))


@bot.command(name="ping")
async def ping_cmd(ctx: commands.Context) -> None:
    await owner_msg(ctx, f"Online. Latency {round(bot.latency * 1000)} ms.")


@bot.command(name="whoami", aliases=["id"])
async def whoami_cmd(ctx: commands.Context) -> None:
    await owner_msg(ctx, f"Aap owner ho.\nID: `{ctx.author.id}`\nNaam: {ctx.author}")


@bot.command(name="invite")
async def invite_cmd(ctx: commands.Context) -> None:
    app_id = bot.user.id if bot.user else "APP_ID"
    await owner_msg(
        ctx,
        "Is link se bot Administrator ban kar server mein aata hai:\n"
        f"https://discord.com/oauth2/authorize?client_id={app_id}&permissions=8&scope=bot\n"
        "Add karne ke baad bot ka role upar move karna.",
    )


@bot.command(name="servers")
async def servers_cmd(ctx: commands.Context) -> None:
    lines = [f"Servers: {len(bot.guilds)}"]
    for guild in bot.guilds[:40]:
        admin = guild.me.guild_permissions.administrator if guild.me else False
        lines.append(f"- {guild.name} `{guild.id}` members={guild.member_count} admin={'yes' if admin else 'no'}")
    if len(bot.guilds) > 40:
        lines.append("… aur bhi")
    await owner_msg(ctx, "\n".join(lines))


@bot.command(name="ban")
@commands.guild_only()
async def ban_cmd(ctx: commands.Context, member: discord.Member, *, rest: str = "") -> None:
    blocked = hierarchy_block(ctx, member)
    if blocked:
        await owner_msg(ctx, blocked)
        return
    delete_days = 0
    reason = "No reason"
    rest = rest.strip()
    if rest:
        parts = rest.split(maxsplit=1)
        if parts[0].isdigit() and 0 <= int(parts[0]) <= 7:
            delete_days = int(parts[0])
            reason = parts[1] if len(parts) > 1 else "No reason"
        else:
            reason = rest
    why = audit(ctx, reason)
    try:
        await member.ban(delete_message_seconds=delete_days * 86400, reason=why)
    except TypeError:
        await member.ban(delete_message_days=delete_days, reason=why)
    await owner_msg(ctx, f"Ban ho gaya: **{member}** (`{member.id}`)\nMessages: {delete_days} din\nWajah: {reason}")


@bot.command(name="unban")
@commands.guild_only()
async def unban_cmd(ctx: commands.Context, user_id: str, *, reason: str = "No reason") -> None:
    found = snowflake(user_id)
    if found is None:
        await owner_msg(ctx, "User ID do. Example: `!unban 123456789012345678`")
        return
    user = discord.Object(id=found)
    await ctx.guild.unban(user, reason=audit(ctx, reason))
    await owner_msg(ctx, f"Unban ho gaya: `{found}`\nWajah: {reason}")


@bot.command(name="kick")
@commands.guild_only()
async def kick_cmd(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason") -> None:
    blocked = hierarchy_block(ctx, member)
    if blocked:
        await owner_msg(ctx, blocked)
        return
    await member.kick(reason=audit(ctx, reason))
    await owner_msg(ctx, f"Kick ho gaya: **{member}** (`{member.id}`)\nWajah: {reason}")


@bot.command(name="timeout", aliases=["mute"])
@commands.guild_only()
async def timeout_cmd(ctx: commands.Context, member: discord.Member, duration: str, *, reason: str = "No reason") -> None:
    blocked = hierarchy_block(ctx, member)
    if blocked:
        await owner_msg(ctx, blocked)
        return
    seconds = parse_duration(duration)
    if seconds is None or seconds < 1 or seconds > 28 * 86400:
        await owner_msg(ctx, "Time aisa do: `30s` `10m` `2h` `1d` (max 28d).")
        return
    await member.timeout(timedelta(seconds=seconds), reason=audit(ctx, reason))
    await owner_msg(ctx, f"Timeout: **{member}** for {duration}\nWajah: {reason}")


@bot.command(name="untimeout", aliases=["unmute"])
@commands.guild_only()
async def untimeout_cmd(ctx: commands.Context, member: discord.Member) -> None:
    blocked = hierarchy_block(ctx, member)
    if blocked:
        await owner_msg(ctx, blocked)
        return
    await member.timeout(None, reason=audit(ctx, "remove timeout"))
    await owner_msg(ctx, f"Timeout hat gaya: **{member}**")


@bot.command(name="nick")
@commands.guild_only()
async def nick_cmd(ctx: commands.Context, member: discord.Member, *, name: str) -> None:
    blocked = hierarchy_block(ctx, member)
    if blocked:
        await owner_msg(ctx, blocked)
        return
    if len(name) > 32:
        await owner_msg(ctx, "Nickname 32 characters se chhota hona chahiye.")
        return
    await member.edit(nick=name, reason=audit(ctx))
    await owner_msg(ctx, f"Nickname set: **{member}** → {name}")


@bot.command(name="clearnick")
@commands.guild_only()
async def clearnick_cmd(ctx: commands.Context, member: discord.Member) -> None:
    blocked = hierarchy_block(ctx, member)
    if blocked:
        await owner_msg(ctx, blocked)
        return
    await member.edit(nick=None, reason=audit(ctx))
    await owner_msg(ctx, f"Nickname hat gaya: **{member}**")


@bot.command(name="userinfo")
@commands.guild_only()
async def userinfo_cmd(ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
    target = member or ctx.author
    if not isinstance(target, discord.Member):
        await owner_msg(ctx, "Member server mein nahi hai.")
        return
    roles = [role.name for role in target.roles if not role.is_default()]
    role_text = ", ".join(roles[:20]) if roles else "koi role nahi"
    timeout = target.timed_out_until
    lines = [
        f"**{target}**",
        f"ID: `{target.id}`",
        f"Nickname: {target.nick or '—'}",
        f"Bot: {'haan' if target.bot else 'nahi'}",
        f"Join: {target.joined_at:%Y-%m-%d %H:%M UTC}" if target.joined_at else "Join: —",
        f"Account: {target.created_at:%Y-%m-%d %H:%M UTC}",
        f"Top role: {target.top_role.name}",
        f"Timeout: {timeout:%Y-%m-%d %H:%M UTC}" if timeout else "Timeout: nahi",
        f"Roles: {role_text}",
    ]
    await owner_msg(ctx, "\n".join(lines))


@bot.command(name="members")
@commands.guild_only()
async def members_cmd(ctx: commands.Context, *, query: str) -> None:
    if not ctx.guild.chunked:
        await ctx.guild.chunk()
    needle = query.lower().strip()
    found = []
    for member in ctx.guild.members:
        blob = " ".join(
            part
            for part in (member.name, member.nick or "", member.display_name, str(member.id), str(member))
            if part
        ).lower()
        if needle in blob:
            found.append(member)
        if len(found) >= 20:
            break
    if not found:
        await owner_msg(ctx, "Koi member nahi mila.")
        return
    lines = [f"Results for `{query}`:"]
    for member in found:
        lines.append(f"- {member} `{member.id}` nick={member.nick or '—'}")
    await owner_msg(ctx, "\n".join(lines))


@bot.command(name="dm")
@commands.guild_only()
async def dm_cmd(ctx: commands.Context, member: discord.Member, *, message: str) -> None:
    if len(message) > 2000:
        await owner_msg(ctx, "DM 2000 characters se chhota hona chahiye.")
        return
    try:
        await member.send(message)
    except discord.Forbidden:
        await owner_msg(ctx, "Is member ke DMs band hain.")
        return
    await owner_msg(ctx, f"DM chala gaya: **{member}**")


@bot.command(name="move")
@commands.guild_only()
async def move_cmd(ctx: commands.Context, member: discord.Member, *, channel: discord.VoiceChannel) -> None:
    blocked = hierarchy_block(ctx, member)
    if blocked:
        await owner_msg(ctx, blocked)
        return
    if member.voice is None:
        await owner_msg(ctx, "Ye member voice mein nahi hai.")
        return
    await member.move_to(channel, reason=audit(ctx))
    await owner_msg(ctx, f"**{member}** ko **{channel.name}** mein move kar diya.")


@bot.command(name="disconnect")
@commands.guild_only()
async def disconnect_cmd(ctx: commands.Context, member: discord.Member) -> None:
    blocked = hierarchy_block(ctx, member)
    if blocked:
        await owner_msg(ctx, blocked)
        return
    if member.voice is None:
        await owner_msg(ctx, "Ye member voice mein nahi hai.")
        return
    await member.move_to(None, reason=audit(ctx))
    await owner_msg(ctx, f"**{member}** voice se nikal gaya.")


@bot.command(name="vcmute")
@commands.guild_only()
async def vcmute_cmd(ctx: commands.Context, member: discord.Member) -> None:
    problem = await voice_action(ctx, member, mute=True)
    await owner_msg(ctx, problem or f"Server mute: **{member}**")


@bot.command(name="vcunmute")
@commands.guild_only()
async def vcunmute_cmd(ctx: commands.Context, member: discord.Member) -> None:
    problem = await voice_action(ctx, member, mute=False)
    await owner_msg(ctx, problem or f"Server unmute: **{member}**")


@bot.command(name="deafen")
@commands.guild_only()
async def deafen_cmd(ctx: commands.Context, member: discord.Member) -> None:
    problem = await voice_action(ctx, member, deafen=True)
    await owner_msg(ctx, problem or f"Server deafen: **{member}**")


@bot.command(name="undeafen")
@commands.guild_only()
async def undeafen_cmd(ctx: commands.Context, member: discord.Member) -> None:
    problem = await voice_action(ctx, member, deafen=False)
    await owner_msg(ctx, problem or f"Server undeafen: **{member}**")


@bot.command(name="roles")
@commands.guild_only()
async def roles_cmd(ctx: commands.Context) -> None:
    ordered = sorted(ctx.guild.roles, key=lambda role: role.position, reverse=True)
    lines = [f"Roles: {len(ordered)}"]
    for role in ordered:
        if role.is_default():
            continue
        lines.append(f"- {role.position:>2}  {role.name}  `{role.id}`")
        if len(lines) >= 42:
            lines.append("… list kaat di. Baaqi Discord settings mein hain.")
            break
    await owner_msg(ctx, "\n".join(lines))


@bot.command(name="createrole")
@commands.guild_only()
async def createrole_cmd(ctx: commands.Context, *, args: str) -> None:
    color = discord.Color.default()
    name = args.strip()
    parts = name.rsplit(maxsplit=1)
    if len(parts) == 2:
        match = HEX_RE.fullmatch(parts[1])
        if match:
            name = parts[0]
            color = discord.Color(int(match.group(1), 16))
    if not name or len(name) > 100:
        await owner_msg(ctx, "Role ka naam 1-100 characters hona chahiye.")
        return
    role = await ctx.guild.create_role(name=name, color=color, reason=audit(ctx))
    await owner_msg(ctx, f"Role ban gayi: **{role.name}** `{role.id}`")


@bot.command(name="deleterole")
@commands.guild_only()
async def deleterole_cmd(ctx: commands.Context, *, role: discord.Role) -> None:
    blocked = role_block(ctx, role)
    if blocked:
        await owner_msg(ctx, blocked)
        return
    name = role.name
    await role.delete(reason=audit(ctx))
    await owner_msg(ctx, f"Role delete ho gayi: **{name}**")


@bot.command(name="editrole")
@commands.guild_only()
async def editrole_cmd(ctx: commands.Context, role: discord.Role, field: str, *, value: str) -> None:
    blocked = role_block(ctx, role)
    if blocked:
        await owner_msg(ctx, blocked)
        return
    field = field.lower()
    why = audit(ctx, f"edit {field}")
    if field == "name":
        if not value or len(value) > 100:
            await owner_msg(ctx, "Naam 1-100 characters.")
            return
        await role.edit(name=value, reason=why)
    elif field == "color":
        if value.lower() in {"none", "clear", "off"}:
            await role.edit(color=discord.Color.default(), reason=why)
        else:
            match = HEX_RE.fullmatch(value.strip())
            if not match:
                await owner_msg(ctx, "Color `#rrggbb` do, ya `none`.")
                return
            await role.edit(color=discord.Color(int(match.group(1), 16)), reason=why)
    elif field == "hoist":
        flag = parse_flag(value)
        if flag is None:
            await owner_msg(ctx, "`on` ya `off` likho.")
            return
        await role.edit(hoist=flag, reason=why)
    elif field in {"mention", "mentionable"}:
        flag = parse_flag(value)
        if flag is None:
            await owner_msg(ctx, "`on` ya `off` likho.")
            return
        await role.edit(mentionable=flag, reason=why)
    elif field == "admin":
        flag = parse_flag(value)
        if flag is None:
            await owner_msg(ctx, "`on` ya `off` likho.")
            return
        perms = role.permissions
        perms.administrator = flag
        await role.edit(permissions=perms, reason=why)
    else:
        await owner_msg(ctx, "Field: `name` `color` `hoist` `mention` `admin`.")
        return
    await owner_msg(ctx, f"**{role.name}** update ho gayi. {field} = {value}")


@bot.command(name="addrole")
@commands.guild_only()
async def addrole_cmd(ctx: commands.Context, member: discord.Member, *, role: discord.Role) -> None:
    blocked = hierarchy_block(ctx, member) or role_block(ctx, role)
    if blocked:
        await owner_msg(ctx, blocked)
        return
    await member.add_roles(role, reason=audit(ctx))
    await owner_msg(ctx, f"**{member}** ko **{role.name}** mil gayi.")


@bot.command(name="removerole")
@commands.guild_only()
async def removerole_cmd(ctx: commands.Context, member: discord.Member, *, role: discord.Role) -> None:
    blocked = hierarchy_block(ctx, member) or role_block(ctx, role)
    if blocked:
        await owner_msg(ctx, blocked)
        return
    await member.remove_roles(role, reason=audit(ctx))
    await owner_msg(ctx, f"**{member}** se **{role.name}** hat gayi.")


@bot.command(name="roleall")
@commands.guild_only()
async def roleall_cmd(ctx: commands.Context, *, args: str) -> None:
    if not args.lower().endswith(" yes"):
        await owner_msg(ctx, "Confirm chahiye. Example: `!roleall Members yes`")
        return
    role_name = args[: -4].strip()
    try:
        role = await commands.RoleConverter().convert(ctx, role_name)
    except commands.RoleNotFound:
        await owner_msg(ctx, "Role nahi mili.")
        return
    blocked = role_block(ctx, role)
    if blocked:
        await owner_msg(ctx, blocked)
        return
    await hide_message(ctx)
    if not ctx.guild.chunked:
        await ctx.guild.chunk()
    me = ctx.guild.me
    pending = [
        member
        for member in ctx.guild.members
        if not member.bot and role not in member.roles and member.top_role < me.top_role
    ]
    batch = pending[:250]
    await owner_msg(ctx, f"**{role.name}** de raha hoon: {len(batch)} members. Baaqi {max(0, len(pending) - 250)}.")
    ok = 0
    failed = 0
    for member in batch:
        try:
            await member.add_roles(role, reason=audit(ctx, "roleall"))
            ok += 1
        except discord.HTTPException:
            failed += 1
    await owner_msg(ctx, f"Roleall khatam. **{role.name}** — diye {ok}, fail {failed}.")


@bot.command(name="channels")
@commands.guild_only()
async def channels_cmd(ctx: commands.Context) -> None:
    lines = [f"Channels: {len(ctx.guild.channels)}"]
    loose = [channel for channel in ctx.guild.channels if channel.category is None and not isinstance(channel, discord.CategoryChannel)]
    for channel in loose:
        lines.append(f"- {channel.name}")
    for category in ctx.guild.categories:
        lines.append(f"[{category.name}]")
        for channel in category.channels:
            lines.append(f"  {channel.name}")
        if len(lines) >= 60:
            lines.append("… list kaat di.")
            break
    await owner_msg(ctx, "\n".join(lines[:80]))


@bot.command(name="createtext")
@commands.guild_only()
async def createtext_cmd(ctx: commands.Context, *, args: str) -> None:
    await make_channel(ctx, "text", args)


@bot.command(name="createvoice")
@commands.guild_only()
async def createvoice_cmd(ctx: commands.Context, *, args: str) -> None:
    await make_channel(ctx, "voice", args)


@bot.command(name="createcategory")
@commands.guild_only()
async def createcategory_cmd(ctx: commands.Context, *, name: str) -> None:
    await make_channel(ctx, "category", name)


@bot.command(name="createforum")
@commands.guild_only()
async def createforum_cmd(ctx: commands.Context, *, args: str) -> None:
    await make_channel(ctx, "forum", args)


@bot.command(name="createstage")
@commands.guild_only()
async def createstage_cmd(ctx: commands.Context, *, args: str) -> None:
    await make_channel(ctx, "stage", args)


@bot.command(name="deletechannel")
@commands.guild_only()
async def deletechannel_cmd(ctx: commands.Context, *, args: str) -> None:
    channel, _rest = take_channel(ctx, args)
    if channel is None:
        await owner_msg(ctx, "Channel mention karo. Example: `!deletechannel #general` — current channel khud delete nahi hoti.")
        return
    name = channel.name
    await owner_msg(ctx, f"**{name}** delete ho rahi hai.")
    await channel.delete(reason=audit(ctx))


@bot.command(name="rename")
@commands.guild_only()
async def rename_cmd(ctx: commands.Context, *, args: str) -> None:
    channel, rest = take_channel(ctx, args)
    if channel is None:
        channel = ctx.channel
        rest = args.strip()
    if not rest:
        await owner_msg(ctx, "Naya naam do. `!rename naya-naam`")
        return
    if not isinstance(channel, discord.abc.GuildChannel):
        await owner_msg(ctx, "Ye channel rename nahi ho sakti.")
        return
    await channel.edit(name=rest, reason=audit(ctx))
    await owner_msg(ctx, f"Naam badal gaya: **{rest}**")


@bot.command(name="slowmode")
@commands.guild_only()
async def slowmode_cmd(ctx: commands.Context, *, args: str = "") -> None:
    channel, rest = take_channel(ctx, args)
    target = channel or ctx.channel
    if not rest.isdigit():
        await owner_msg(ctx, "Seconds do. `!slowmode 10` ya `!slowmode #channel 0` (0 = band). Max 21600.")
        return
    if not isinstance(target, discord.TextChannel):
        await owner_msg(ctx, "Slowmode text channel par hi chalta hai.")
        return
    seconds = int(rest)
    if seconds > 21600:
        await owner_msg(ctx, "Max 21600 seconds (6 hours).")
        return
    await target.edit(slowmode_delay=seconds, reason=audit(ctx))
    await owner_msg(ctx, f"**{target.name}** slowmode: {seconds}s")


@bot.command(name="lock")
@commands.guild_only()
async def lock_cmd(ctx: commands.Context, *, args: str = "") -> None:
    await set_everyone(ctx, args, {"send_messages": False}, "locked")


@bot.command(name="unlock")
@commands.guild_only()
async def unlock_cmd(ctx: commands.Context, *, args: str = "") -> None:
    await set_everyone(ctx, args, {"send_messages": None}, "unlocked")


@bot.command(name="hide")
@commands.guild_only()
async def hide_cmd(ctx: commands.Context, *, args: str = "") -> None:
    await set_everyone(ctx, args, {"view_channel": False}, "hidden")


@bot.command(name="unhide")
@commands.guild_only()
async def unhide_cmd(ctx: commands.Context, *, args: str = "") -> None:
    await set_everyone(ctx, args, {"view_channel": None}, "visible")


@bot.command(name="clone")
@commands.guild_only()
async def clone_cmd(ctx: commands.Context, *, args: str = "") -> None:
    channel, _rest = take_channel(ctx, args)
    target = channel or ctx.channel
    if not hasattr(target, "clone"):
        await owner_msg(ctx, "Is channel type ki copy nahi ban sakti.")
        return
    copied = await target.clone(reason=audit(ctx))
    await owner_msg(ctx, f"Copy ban gayi: **{copied.name}**")


@bot.command(name="topic")
@commands.guild_only()
async def topic_cmd(ctx: commands.Context, *, args: str) -> None:
    channel, rest = take_channel(ctx, args)
    target = channel or ctx.channel
    text = rest if channel else args.strip()
    if not text:
        await owner_msg(ctx, "Topic likho.")
        return
    if not isinstance(target, (discord.TextChannel, discord.ForumChannel)):
        await owner_msg(ctx, "Topic text ya forum channel par hi lagta hai.")
        return
    await target.edit(topic=text[:1024], reason=audit(ctx))
    await owner_msg(ctx, f"**{target.name}** ka topic set ho gaya.")


@bot.command(name="nsfw")
@commands.guild_only()
async def nsfw_cmd(ctx: commands.Context, *, args: str) -> None:
    channel, rest = take_channel(ctx, args)
    target = channel or ctx.channel
    flag = parse_flag(rest if channel else args)
    if flag is None:
        await owner_msg(ctx, "`!nsfw on` ya `!nsfw off`. Optional `#channel`.")
        return
    if not isinstance(target, (discord.TextChannel, discord.ForumChannel, discord.VoiceChannel, discord.StageChannel)):
        await owner_msg(ctx, "Is channel par NSFW flag nahi lagta.")
        return
    await target.edit(nsfw=flag, reason=audit(ctx))
    await owner_msg(ctx, f"**{target.name}** NSFW: {'on' if flag else 'off'}")


@bot.command(name="purge", aliases=["clear"])
@commands.guild_only()
async def purge_cmd(ctx: commands.Context, amount: int) -> None:
    if not isinstance(ctx.channel, (discord.TextChannel, discord.Thread)):
        await owner_msg(ctx, "Purge text channel ya thread mein chalao.")
        return
    if amount < 1 or amount > 100:
        await owner_msg(ctx, "1 se 100 ke beech number do.")
        return
    deleted = await ctx.channel.purge(limit=amount, check=lambda msg: not msg.pinned, before=ctx.message)
    await owner_msg(ctx, f"{len(deleted)} messages delete ho gaye. Pinned rehte hain.")


@bot.command(name="del", aliases=["delete"])
@commands.guild_only()
async def del_cmd(ctx: commands.Context, message_id: Optional[str] = None) -> None:
    message = await fetch_referenced(ctx, message_id)
    if message is None:
        await owner_msg(ctx, "Message par reply karke `!del` likho, ya `!del MESSAGE_ID`.")
        return
    await message.delete()
    await owner_msg(ctx, "Message delete ho gaya.")


@bot.command(name="pin")
@commands.guild_only()
async def pin_cmd(ctx: commands.Context, message_id: Optional[str] = None) -> None:
    message = await fetch_referenced(ctx, message_id)
    if message is None:
        await owner_msg(ctx, "Message par reply karke `!pin` likho.")
        return
    await message.pin(reason=audit(ctx))
    await owner_msg(ctx, "Pin ho gaya.")


@bot.command(name="unpin")
@commands.guild_only()
async def unpin_cmd(ctx: commands.Context, message_id: Optional[str] = None) -> None:
    message = await fetch_referenced(ctx, message_id)
    if message is None:
        await owner_msg(ctx, "Message par reply karke `!unpin` likho.")
        return
    await message.unpin(reason=audit(ctx))
    await owner_msg(ctx, "Unpin ho gaya.")


@bot.command(name="serverinfo")
@commands.guild_only()
async def serverinfo_cmd(ctx: commands.Context) -> None:
    guild = ctx.guild
    lines = [
        f"**{guild.name}**",
        f"ID: `{guild.id}`",
        f"Owner: {guild.owner} `{guild.owner_id}`",
        f"Members: {guild.member_count}",
        f"Roles: {len(guild.roles)}",
        f"Channels: {len(guild.channels)}",
        f"Boosts: {guild.premium_subscription_count}",
        f"Verification: {guild.verification_level}",
        f"Created: {guild.created_at:%Y-%m-%d}",
        f"Bot admin: {'haan' if guild.me and guild.me.guild_permissions.administrator else 'nahi'}",
    ]
    await owner_msg(ctx, "\n".join(lines))


@bot.command(name="servername")
@commands.guild_only()
async def servername_cmd(ctx: commands.Context, *, name: str) -> None:
    if len(name) < 2 or len(name) > 100:
        await owner_msg(ctx, "Server naam 2-100 characters.")
        return
    await ctx.guild.edit(name=name, reason=audit(ctx))
    await owner_msg(ctx, f"Server ka naam ab **{name}** hai.")


@bot.command(name="seticon")
@commands.guild_only()
async def seticon_cmd(ctx: commands.Context) -> None:
    if not ctx.message.attachments:
        await owner_msg(ctx, "Image attach karo aur caption mein `!seticon` likho.")
        return
    attachment = ctx.message.attachments[0]
    if attachment.content_type and not attachment.content_type.startswith("image/"):
        await owner_msg(ctx, "Sirf image file chalega.")
        return
    if attachment.size > 8 * 1024 * 1024:
        await owner_msg(ctx, "Image 8 MB se chhoti hona chahiye.")
        return
    data = await attachment.read()
    await ctx.guild.edit(icon=data, reason=audit(ctx))
    await owner_msg(ctx, "Server icon update ho gaya.")


@bot.command(name="nickbot")
@commands.guild_only()
async def nickbot_cmd(ctx: commands.Context, *, name: str) -> None:
    if len(name) > 32:
        await owner_msg(ctx, "Nickname 32 characters se chhota hona chahiye.")
        return
    await ctx.guild.me.edit(nick=name, reason=audit(ctx))
    await owner_msg(ctx, f"Bot ka nickname: **{name}**")


@bot.command(name="clearnickbot")
@commands.guild_only()
async def clearnickbot_cmd(ctx: commands.Context) -> None:
    await ctx.guild.me.edit(nick=None, reason=audit(ctx))
    await owner_msg(ctx, "Bot ka nickname hat gaya.")


@bot.command(name="bans")
@commands.guild_only()
async def bans_cmd(ctx: commands.Context) -> None:
    lines = ["Bans:"]
    count = 0
    async for entry in ctx.guild.bans(limit=20):
        count += 1
        why = entry.reason or "—"
        lines.append(f"- {entry.user} `{entry.user.id}` — {why}")
    if count == 0:
        lines.append("Koi ban nahi.")
    await owner_msg(ctx, "\n".join(lines))


@bot.command(name="audit")
@commands.guild_only()
async def audit_cmd(ctx: commands.Context, limit: int = 10) -> None:
    if limit < 1 or limit > 30:
        await owner_msg(ctx, "1 se 30 ke beech number do.")
        return
    lines = [f"Audit log ({limit}):"]
    async for entry in ctx.guild.audit_logs(limit=limit):
        actor = entry.user or "—"
        action = str(entry.action).split(".")[-1]
        target = entry.target or "—"
        lines.append(f"- {entry.created_at:%m-%d %H:%M} {actor} {action} {target}")
    await owner_msg(ctx, "\n".join(lines))


@bot.command(name="lockdown")
@commands.guild_only()
async def lockdown_cmd(ctx: commands.Context, confirm: Optional[str] = None) -> None:
    if confirm != "yes":
        await owner_msg(ctx, "Saare text channels lock hon ge. Confirm: `!lockdown yes`")
        return
    if ctx.guild.id in LOCKDOWN:
        await owner_msg(ctx, "Lockdown pehle se chal raha hai. Pehle `!unlockdown` karo.")
        return
    await hide_message(ctx)
    saved: dict[int, Optional[bool]] = {}
    changed = 0
    for channel in ctx.guild.text_channels:
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        saved[channel.id] = overwrite.send_messages
        if overwrite.send_messages is False:
            continue
        overwrite.send_messages = False
        await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=audit(ctx, "lockdown"))
        changed += 1
    LOCKDOWN[ctx.guild.id] = saved
    await owner_msg(ctx, f"Lockdown. {changed} channels lock hue. Wapas: `!unlockdown`")


@bot.command(name="unlockdown")
@commands.guild_only()
async def unlockdown_cmd(ctx: commands.Context) -> None:
    saved = LOCKDOWN.get(ctx.guild.id)
    if not saved:
        await owner_msg(ctx, "Is run mein lockdown save nahi. `!unlock #channel` se ek ek kholo.")
        return
    restored = 0
    for channel_id, previous in saved.items():
        channel = ctx.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            continue
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = previous
        await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=audit(ctx, "unlockdown"))
        restored += 1
    LOCKDOWN.pop(ctx.guild.id, None)
    await owner_msg(ctx, f"Unlockdown. {restored} channels wapas.")


@bot.command(name="invitehere")
@commands.guild_only()
async def invitehere_cmd(ctx: commands.Context, uses: Optional[int] = None, hours: Optional[int] = None) -> None:
    max_uses = 0 if uses is None else uses
    max_hours = 0 if hours is None else hours
    if max_uses < 0 or max_uses > 100 or max_hours < 0 or max_hours > 168:
        await owner_msg(ctx, "Uses 0-100, hours 0-168. 0 ka matlab unlimited / no expiry.")
        return
    if not isinstance(ctx.channel, discord.abc.GuildChannel):
        await owner_msg(ctx, "Is channel par invite nahi ban sakta.")
        return
    invite = await ctx.channel.create_invite(
        max_uses=max_uses,
        max_age=max_hours * 3600,
        unique=True,
        reason=audit(ctx),
    )
    await owner_msg(ctx, f"Invite: {invite.url}\nUses: {max_uses or 'unlimited'}  Hours: {max_hours or 'no expiry'}")


@bot.command(name="say")
@commands.guild_only()
async def say_cmd(ctx: commands.Context, *, args: str) -> None:
    channel, text = take_channel(ctx, args)
    target = channel or ctx.channel
    body = text if channel else args.strip()
    if not body:
        await owner_msg(ctx, "Message likho. `!say #general hello`")
        return
    if len(body) > 2000:
        await owner_msg(ctx, "Message 2000 characters se chhota hona chahiye.")
        return
    if not isinstance(target, discord.abc.Messageable):
        await owner_msg(ctx, "Is channel mein message nahi ja sakta.")
        return
    await target.send(body)
    await owner_msg(ctx, f"Bhej diya: **{getattr(target, 'name', 'channel')}**")


@bot.command(name="embed")
@commands.guild_only()
async def embed_cmd(ctx: commands.Context, *, args: str) -> None:
    channel, text = take_channel(ctx, args)
    target = channel or ctx.channel
    body = text if channel else args.strip()
    if "|" not in body:
        await owner_msg(ctx, "Format: `!embed Title | description`")
        return
    title, description = [part.strip() for part in body.split("|", 1)]
    if not title or not description:
        await owner_msg(ctx, "Title aur description dono chahiye.")
        return
    if not isinstance(target, discord.abc.Messageable):
        await owner_msg(ctx, "Is channel mein embed nahi ja sakta.")
        return
    embed = discord.Embed(title=title[:256], description=description[:4096], color=ACCENT)
    await target.send(embed=embed)
    await owner_msg(ctx, f"Embed bhej diya: **{getattr(target, 'name', 'channel')}**")


@bot.command(name="announce")
@commands.guild_only()
async def announce_cmd(ctx: commands.Context, *, args: str) -> None:
    channel, text = take_channel(ctx, args)
    target = channel or ctx.channel
    body = text if channel else args.strip()
    if not body:
        await owner_msg(ctx, "Message likho. @everyone tabhi ping hoga jab aap likho ge.")
        return
    if len(body) > 2000:
        await owner_msg(ctx, "Message 2000 characters se chhota hona chahiye.")
        return
    if not isinstance(target, discord.abc.Messageable):
        await owner_msg(ctx, "Is channel mein announce nahi ho sakta.")
        return
    mentions = discord.AllowedMentions(everyone=True, roles=True, users=True)
    await target.send(body, allowed_mentions=mentions)
    await owner_msg(ctx, f"Announce ho gaya: **{getattr(target, 'name', 'channel')}**")


@bot.command(name="prune")
@commands.guild_only()
async def prune_cmd(ctx: commands.Context, days: int, confirm: Optional[str] = None) -> None:
    if days < 1 or days > 30:
        await owner_msg(ctx, "Days 1 se 30. Example: `!prune 7` phir `!prune 7 yes`")
        return
    estimate = await ctx.guild.estimate_pruned_members(days=days)
    if confirm != "yes":
        await owner_msg(ctx, f"{days} din se inactive members (estimate): **{estimate}**.\nNikalne ke liye: `!prune {days} yes`")
        return
    pruned = await ctx.guild.prune_members(days=days, reason=audit(ctx, f"prune {days}d"))
    await owner_msg(ctx, f"Prune ho gaya. Nikale gaye: **{pruned if pruned is not None else estimate}**")


@bot.command(name="leave")
@commands.guild_only()
async def leave_cmd(ctx: commands.Context, confirm: Optional[str] = None) -> None:
    if confirm != "yes":
        await owner_msg(ctx, f"**{ctx.guild.name}** chhorna hai? `!leave yes`")
        return
    await owner_msg(ctx, f"**{ctx.guild.name}** chhor raha hoon.")
    await ctx.guild.leave()


def main() -> None:
    if sys.version_info < (3, 10):
        print("Python 3.10+ chahiye.", flush=True)
        sys.exit(1)
    if not PREFIX or any(ch.isspace() for ch in PREFIX):
        print("BOT_PREFIX khali ya space wala nahi ho sakta.", flush=True)
        sys.exit(1)
    token = load_token()
    if not token:
        print("DISCORD_TOKEN set nahi. Render env var lagao, ya bot.py ke folder mein token.txt banao.", flush=True)
        sys.exit(1)
    start_keepalive()
    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        print("Token galat hai. Developer Portal → Bot → Reset Token, phir DISCORD_TOKEN update karo.", flush=True)
        sys.exit(1)
    except discord.PrivilegedIntentsRequired:
        print("Portal → Bot → Server Members Intent AUR Message Content Intent ON karo.", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
