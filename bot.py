import os
import asyncio
import random
from datetime import datetime, timezone
from aiohttp import web

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

DB_PATH = "bot.db"
LEADERBOARD_SIZE = 5

# ---------- RECOVERY WEB SERVER ----------

async def _health(request):
    service_id = os.getenv("RENDER_SERVICE_ID", "unknown")
    region = os.getenv("RENDER_REGION", "unknown")
    external = os.getenv("RENDER_EXTERNAL_URL", "unknown")

    return web.Response(
        text=(
            "BOTM recovery server ✅\n"
            f"service_id={service_id}\n"
            f"region={region}\n"
            f"external_url={external}\n"
            "Try /download-db to fetch bot.db\n"
        )
    )

async def download_db(request):
    headers = {"Content-Disposition": "attachment; filename=bot.db"}
    return web.FileResponse("bot.db", headers=headers)

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/download-db", download_db)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

# ---------- time helpers ----------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------- database ----------

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                user_id INTEGER PRIMARY KEY,
                amount INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # Optional audit table (we are not exposing /history, but keeping it for future-proofing)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ticket_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                actor_id INTEGER NOT NULL,
                target_id INTEGER,
                delta INTEGER NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL
            )
        """)

        await db.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('last_reset_utc', ?)",
            (now_utc().isoformat(),)
        )

        await db.commit()


async def get_last_reset() -> datetime:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM meta WHERE key='last_reset_utc'") as cur:
            row = await cur.fetchone()
            return datetime.fromisoformat(row[0]) if row else now_utc()


async def set_last_reset(dt: datetime):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE meta SET value=? WHERE key='last_reset_utc'", (dt.isoformat(),))
        await db.commit()


async def add_tickets(user_id: int, delta: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO tickets(user_id, amount, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              amount = MAX(0, amount + excluded.amount),
              updated_at = excluded.updated_at
        """, (user_id, delta, now_utc().isoformat()))
        await db.commit()


async def set_all_tickets_zero():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tickets SET amount=0, updated_at=?", (now_utc().isoformat(),))
        await db.commit()


async def get_tickets(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT amount FROM tickets WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0


async def top_tickets(limit: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT user_id, amount
            FROM tickets
            WHERE amount > 0
            ORDER BY amount DESC, updated_at DESC
            LIMIT ?
        """, (limit,)) as cur:
            return await cur.fetchall()


async def all_positive_tickets():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, amount FROM tickets WHERE amount > 0") as cur:
            return await cur.fetchall()


async def total_entries() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COALESCE(SUM(amount), 0) FROM tickets WHERE amount > 0") as cur:
            row = await cur.fetchone()
            return int(row[0] or 0)


async def total_participants() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM tickets WHERE amount > 0") as cur:
            row = await cur.fetchone()
            return int(row[0] or 0)


async def log_ticket_action(guild_id: int, actor_id: int, target_id: int | None, delta: int, reason: str):
    # Stored for future-proofing (optional). Public command outputs are your main “ledger.”
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO ticket_log (guild_id, actor_id, target_id, delta, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (guild_id, actor_id, target_id, delta, reason, now_utc().isoformat()))
        await db.commit()


# ---------- permission checks ----------

def staff_or_admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.user or not isinstance(interaction.user, discord.Member):
            return False
        perms = interaction.user.guild_permissions
        return perms.manage_guild or perms.administrator or perms.manage_roles
    return app_commands.check(predicate)


def admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.user or not isinstance(interaction.user, discord.Member):
            return False
        return interaction.user.guild_permissions.administrator
    return app_commands.check(predicate)


# ---------- bot setup ----------

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
GUILD_OBJ = discord.Object(id=GUILD_ID) if GUILD_ID else None


@bot.event
async def on_ready():
    await init_db()

    # Fast sync to your server (good for testing / single-server usage)
    if GUILD_OBJ:
        bot.tree.copy_global_to(guild=GUILD_OBJ)
        await bot.tree.sync(guild=GUILD_OBJ)
    else:
        await bot.tree.sync()

    print(f"Logged in as {bot.user} (ready)")


# ---------- member commands (ephemeral) ----------

@bot.tree.command(name="entries", description="Check how many BOTM entries you have.")
async def entries(interaction: discord.Interaction):
    amt = await get_tickets(interaction.user.id)
    await interaction.response.send_message(
        f"🎟️ You currently have **{amt}** BOTM entries.",
        ephemeral=True
    )


@bot.tree.command(name="odds", description="See your current BOTM odds based on your entries in the pool.")
async def odds(interaction: discord.Interaction):
    mine = await get_tickets(interaction.user.id)
    total = await total_entries()

    if mine <= 0:
        await interaction.response.send_message(
            "🎟️ You currently have **0** entries — if you think that’s wrong, please open a ticket or ask **Angel**!",
            ephemeral=True
        )
        return

    if total <= 0:
        await interaction.response.send_message(
            "🎟️ The BOTM pool is empty right now — no entries yet 👀",
            ephemeral=True
        )
        return

    pct = (mine / total) * 100

    await interaction.response.send_message(
        f"🎯 You have **{mine}** entries out of **{total}** total.\n"
        f"✨ Your current odds are about **{pct:.2f}%**.",
        ephemeral=True
    )


@bot.tree.command(name="total_entries", description="See how many total BOTM entries are currently in the pool.")
async def total_entries_cmd(interaction: discord.Interaction):
    total = await total_entries()
    participants = await total_participants()

    if total == 0:
        await interaction.response.send_message(
            "🎟️ The BOTM pool is empty right now — no entries yet 👀",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"🎟️ There are currently **{total}** total BOTM entries in the pool across **{participants}** baddies.",
        ephemeral=True
    )


@bot.tree.command(name="leaderboard", description="Show the top 5 members with the most BOTM entries.")
async def leaderboard(interaction: discord.Interaction):
    rows = await top_tickets(LEADERBOARD_SIZE)
    if not rows:
        await interaction.response.send_message("No entries yet 👀")
        return

    lines = [f"**{i}.** <@{uid}> — **{amt}**" for i, (uid, amt) in enumerate(rows, start=1)]
    await interaction.response.send_message("🏆 **BOTM Entries Leaderboard**\n" + "\n".join(lines))


@bot.tree.command(name="month", description="Show last reset date/time.")
async def month(interaction: discord.Interaction):
    last = await get_last_reset()
    await interaction.response.send_message(
        f"📅 Last reset (UTC): **{last.strftime('%Y-%m-%d %H:%M')}**\nUse `/reset_entries` when you’re ready to start a new month.",
        ephemeral=True
    )


# ---------- staff/admin commands (PUBLIC, single message) ----------

@bot.tree.command(name="give", description="Give BOTM entries to a member.")
@staff_or_admin_only()
@app_commands.describe(user="Who to give entries to", amount="How many entries to add", reason="Optional reason/note")
async def give(interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, 1000], reason: str = ""):
    await add_tickets(user.id, amount)
    new_amt = await get_tickets(user.id)
    await log_ticket_action(interaction.guild_id or 0, interaction.user.id, user.id, amount, reason)

    note = f'\n📝 Reason: "{reason}"' if reason else ""
    msg = (
        f"➕ **Entries Added**\n"
        f"Mod: {interaction.user.mention}\n"
        f"Member: {user.mention}\n"
        f"Amount: **+{amount}**\n"
        f"New total: **{new_amt}**"
        f"{note}"
    )
    await interaction.response.send_message(msg, ephemeral=False)


@bot.tree.command(name="remove", description="Remove BOTM entries from a member.")
@staff_or_admin_only()
@app_commands.describe(user="Who to remove entries from", amount="How many entries to remove", reason="Optional reason/note")
async def remove(interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, 1000], reason: str = ""):
    await add_tickets(user.id, -amount)
    new_amt = await get_tickets(user.id)
    await log_ticket_action(interaction.guild_id or 0, interaction.user.id, user.id, -amount, reason)

    note = f'\n📝 Reason: "{reason}"' if reason else ""
    msg = (
        f"➖ **Entries Removed**\n"
        f"Mod: {interaction.user.mention}\n"
        f"Member: {user.mention}\n"
        f"Amount: **-{amount}**\n"
        f"New total: **{new_amt}**"
        f"{note}"
    )
    await interaction.response.send_message(msg, ephemeral=False)


@bot.tree.command(name="give_bulk", description="Give BOTM entries to multiple members at once.")
@staff_or_admin_only()
@app_commands.describe(
    amount="How many entries to add to each person",
    reason="Optional reason/note",
    user1="Member 1",
    user2="Member 2 (optional)",
    user3="Member 3 (optional)",
    user4="Member 4 (optional)",
    user5="Member 5 (optional)",
    user6="Member 6 (optional)",
    user7="Member 7 (optional)",
    user8="Member 8 (optional)",
    user9="Member 9 (optional)",
    user10="Member 10 (optional)",
)
async def give_bulk(
    interaction: discord.Interaction,
    amount: app_commands.Range[int, 1, 1000],
    user1: discord.Member,
    reason: str = "",
    user2: discord.Member | None = None,
    user3: discord.Member | None = None,
    user4: discord.Member | None = None,
    user5: discord.Member | None = None,
    user6: discord.Member | None = None,
    user7: discord.Member | None = None,
    user8: discord.Member | None = None,
    user9: discord.Member | None = None,
    user10: discord.Member | None = None,
):
    users = [u for u in [user1, user2, user3, user4, user5, user6, user7, user8, user9, user10] if u is not None]

    unique_users = []
    seen = set()
    for u in users:
        if u.id not in seen:
            unique_users.append(u)
            seen.add(u.id)

    for u in unique_users:
        await add_tickets(u.id, amount)
        await log_ticket_action(interaction.guild_id or 0, interaction.user.id, u.id, amount, reason)

    mentions = ", ".join(u.mention for u in unique_users)
    note = f'\n📝 Reason: "{reason}"' if reason else ""
    msg = (
        f"➕ **Bulk Entries Added**\n"
        f"Mod: {interaction.user.mention}\n"
        f"Members: {mentions}\n"
        f"Amount each: **+{amount}**"
        f"{note}"
    )
    await interaction.response.send_message(msg, ephemeral=False)


@bot.tree.command(name="remove_bulk", description="Remove BOTM entries from multiple members at once.")
@staff_or_admin_only()
@app_commands.describe(
    amount="How many entries to remove from each person",
    reason="Optional reason/note",
    user1="Member 1",
    user2="Member 2 (optional)",
    user3="Member 3 (optional)",
    user4="Member 4 (optional)",
    user5="Member 5 (optional)",
    user6="Member 6 (optional)",
    user7="Member 7 (optional)",
    user8="Member 8 (optional)",
    user9="Member 9 (optional)",
    user10="Member 10 (optional)",
)
async def remove_bulk(
    interaction: discord.Interaction,
    amount: app_commands.Range[int, 1, 1000],
    user1: discord.Member,
    reason: str = "",
    user2: discord.Member | None = None,
    user3: discord.Member | None = None,
    user4: discord.Member | None = None,
    user5: discord.Member | None = None,
    user6: discord.Member | None = None,
    user7: discord.Member | None = None,
    user8: discord.Member | None = None,
    user9: discord.Member | None = None,
    user10: discord.Member | None = None,
):
    users = [u for u in [user1, user2, user3, user4, user5, user6, user7, user8, user9, user10] if u is not None]

    unique_users = []
    seen = set()
    for u in users:
        if u.id not in seen:
            unique_users.append(u)
            seen.add(u.id)

    for u in unique_users:
        await add_tickets(u.id, -amount)
        await log_ticket_action(interaction.guild_id or 0, interaction.user.id, u.id, -amount, reason)

    mentions = ", ".join(u.mention for u in unique_users)
    note = f'\n📝 Reason: "{reason}"' if reason else ""
    msg = (
        f"➖ **Bulk Entries Removed**\n"
        f"Mod: {interaction.user.mention}\n"
        f"Members: {mentions}\n"
        f"Amount each: **-{amount}**"
        f"{note}"
    )
    await interaction.response.send_message(msg, ephemeral=False)


@bot.tree.command(name="give_role", description="Give BOTM entries to everyone with a specific role.")
@staff_or_admin_only()
@app_commands.describe(role="Role to award", amount="Entries to give each member", reason="Optional reason/note")
async def give_role(interaction: discord.Interaction, role: discord.Role, amount: app_commands.Range[int, 1, 1000], reason: str = ""):
    if not interaction.guild:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    awarded = 0
    async for member in interaction.guild.fetch_members(limit=None):
        if member.bot:
            continue
        if role in member.roles:
            await add_tickets(member.id, amount)
            await log_ticket_action(interaction.guild_id or 0, interaction.user.id, member.id, amount, reason)
            awarded += 1

    note = f'\n📝 Reason: "{reason}"' if reason else ""
    msg = (
        f"➕ **Role Award**\n"
        f"Mod: {interaction.user.mention}\n"
        f"Role: {role.mention}\n"
        f"Amount each: **+{amount}**\n"
        f"Awarded: **{awarded}** members"
        f"{note}"
    )
    await interaction.followup.send(msg, ephemeral=False)


@bot.tree.command(name="remove_role", description="Remove BOTM entries from everyone with a specific role.")
@staff_or_admin_only()
@app_commands.describe(role="Role to affect", amount="Entries to remove from each member", reason="Optional reason/note")
async def remove_role(interaction: discord.Interaction, role: discord.Role, amount: app_commands.Range[int, 1, 1000], reason: str = ""):
    if not interaction.guild:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    removed = 0
    async for member in interaction.guild.fetch_members(limit=None):
        if member.bot:
            continue
        if role in member.roles:
            await add_tickets(member.id, -amount)
            await log_ticket_action(interaction.guild_id or 0, interaction.user.id, member.id, -amount, reason)
            removed += 1

    note = f'\n📝 Reason: "{reason}"' if reason else ""
    msg = (
        f"➖ **Role Removal**\n"
        f"Mod: {interaction.user.mention}\n"
        f"Role: {role.mention}\n"
        f"Amount each: **-{amount}**\n"
        f"Updated: **{removed}** members"
        f"{note}"
    )
    await interaction.followup.send(msg, ephemeral=False)


@bot.tree.command(name="reset_entries", description="Reset all BOTM entries (admin only).")
@admin_only()
async def reset_entries(interaction: discord.Interaction):
    await set_all_tickets_zero()
    await set_last_reset(now_utc())

    msg = f"🧼 **Monthly Reset** — {interaction.user.mention} reset all BOTM entries to **0**."
    await interaction.response.send_message(msg, ephemeral=False)


@bot.tree.command(name="roll_winner", description="Spin the BOTM wheel (weighted by entries).")
@staff_or_admin_only()
async def roll_winner(interaction: discord.Interaction):
    rows = await all_positive_tickets()
    if not rows:
        await interaction.response.send_message("No one has entries yet — can’t roll a winner.", ephemeral=True)
        return

    population = []
    for uid, amt in rows:
        population.extend([uid] * int(amt))

    winner_id = random.choice(population)

    await interaction.response.send_message("🎡 Spinning the BOTM wheel...")
    msg = await interaction.original_response()

    for _ in range(8):
        candidate = random.choice(population)
        await asyncio.sleep(0.6)
        await msg.edit(content=f"🎡 Spinning the BOTM wheel...\n➡️ <@{candidate}>")

    await asyncio.sleep(0.8)
    await msg.edit(
        content=(
            "🎉✨ **BADDIE OF THE MONTH** ✨🎉\n"
            f"Congratulations <@{winner_id}> — you’ve been crowned our new Baddie of the Month! 💅📚\n\n"
            "If you haven't filled out the form already, Angel will reach out soon with a quick questionnaire so we can hype you up properly 🫶"
        )
    )


# ---------- tiny web server so Render sees an open port ----------

async def _health(request):
    return web.Response(text="BOTM bot is running ✅")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", _health)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))  # Render provides PORT automatically
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

# ---------- run LAST ----------

async def main():
    print("RECOVERY MODE: starting web server only (no Discord).")
    await start_web_server()
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())

