"""Discord bot: slash commands, request messages edited as games report, approvals.

Connects out to Discord's gateway, so it needs no public URL. The logic lives in
app.commands, app.service and app.store; this module only adapts it to discord.py.
(No `from __future__ import annotations` here: discord.py reads parameter
annotations to build the slash-command options.)
"""

import asyncio
import io
import logging
import os

import discord
from discord import app_commands

from app import commands, store
from app.commands import Member
from app.rhythmverse import RhythmVerseError, Song
from app.service import Events, request_song
from app.state import AppState

log = logging.getLogger(__name__)

APPROVE_PREFIX = "band3-approve:"
# edits of one request's message at most this often (Discord rate-limits edits)
EDIT_EVERY_S = 3.0


def member_of(interaction: discord.Interaction) -> Member:
    user = interaction.user
    roles: list[str] = []
    owner = admin = False
    if isinstance(user, discord.Member):
        for role in user.roles:
            roles += [role.name, str(role.id)]
        owner = interaction.guild is not None and interaction.guild.owner_id == user.id
        admin = user.guild_permissions.administrator
    return Member(str(user.id), user.display_name, roles, owner, admin)


class BotEvents(Events):
    """The web app's hooks: post a page request's message, edit messages as games report."""

    def __init__(self, bot: "Band3Bot"):
        self.bot = bot
        self._pending: set[int] = set()

    def request_created(self, request_id: int) -> None:
        self._soon(self.bot.post_request(request_id))

    def request_changed(self, request_id: int) -> None:
        if request_id in self._pending:
            return
        self._pending.add(request_id)

        async def later():
            await asyncio.sleep(EDIT_EVERY_S)
            self._pending.discard(request_id)
            await self.bot.update_request(request_id)

        self._soon(later())

    def _soon(self, coro) -> None:
        loop = self.bot.loop_ref
        if loop is None or not self.bot.is_ready():
            coro.close()
            return
        asyncio.run_coroutine_threadsafe(coro, loop)


class ApproveButton(discord.ui.Button):
    def __init__(self, request_id: int):
        super().__init__(label="Approve", style=discord.ButtonStyle.success, emoji="✅",
                         custom_id=f"{APPROVE_PREFIX}{request_id}")


def approve_view(request_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ApproveButton(request_id))
    return view


class PickView(discord.ui.View):
    def __init__(self, bot: "Band3Bot", member: Member, songs: list[Song]):
        super().__init__(timeout=600)
        self.bot = bot
        self.member = member
        self.songs = {s.file_id: s for s in songs}
        select = discord.ui.Select(placeholder="Pick the song to request", min_values=1, max_values=1)
        for s in songs:
            label, desc, value = commands.match_option(s)
            select.add_option(label=label, description=desc or None, value=value)
        select.callback = self.picked
        self.add_item(select)

    async def picked(self, interaction: discord.Interaction):
        select: discord.ui.Select = self.children[0]  # type: ignore[assignment]
        song = self.songs.get(select.values[0])
        if song is None:
            return
        member = member_of(interaction)
        settings = self.bot.state.settings
        channel_id = str(interaction.channel_id or "")
        with self.bot.state.conn() as conn:
            outcome = request_song(conn, settings, song, member.user_id, member.name,
                                   can_request=commands.can_request(settings, member),
                                   approver=commands.is_approver(settings, member), channel_id=channel_id)
        await interaction.response.edit_message(content=outcome.message, embeds=[], attachments=[], view=None)
        if outcome.kind == "requested" and outcome.request_id is not None:
            await self.bot.post_request(outcome.request_id)


class Band3Bot(discord.Client):
    def __init__(self, state: AppState):
        super().__init__(intents=discord.Intents.default())
        self.state = state
        self.tree = app_commands.CommandTree(self)
        self.loop_ref: asyncio.AbstractEventLoop | None = None
        self.last_error: str | None = None
        _register(self)

    async def setup_hook(self) -> None:
        self.loop_ref = asyncio.get_running_loop()

    async def on_ready(self) -> None:
        log.info("Discord: connected as %s in %d server(s)", self.user, len(self.guilds))
        gid = self.state.settings.discord_guild_id
        try:
            if gid:
                guild = discord.Object(gid)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                for g in self.guilds:
                    self.tree.copy_global_to(guild=g)
                    await self.tree.sync(guild=g)
        except discord.HTTPException:
            log.exception("Syncing slash commands failed")

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        custom_id = (interaction.data or {}).get("custom_id", "") if interaction.data else ""
        if interaction.type != discord.InteractionType.component or not custom_id.startswith(APPROVE_PREFIX):
            return
        try:
            request_id = int(custom_id[len(APPROVE_PREFIX):])
        except ValueError:
            return
        member = member_of(interaction)
        if not commands.is_approver(self.state.settings, member):
            await interaction.response.send_message("Only an approver can approve requests.", ephemeral=True)
            return
        with self.state.conn() as conn:
            ok = store.approve(conn, request_id, member.user_id)
            row = store.get_request(conn, request_id)
            content = commands.render_request(conn, row) if row else None
        if content is None:
            await interaction.response.send_message("That request is gone.", ephemeral=True)
            return
        await interaction.response.edit_message(content=content, view=None,
                                                allowed_mentions=discord.AllowedMentions.none())
        if not ok:
            await interaction.followup.send("It was approved or cancelled already.", ephemeral=True)

    async def _channel(self, channel_id: str):
        if not channel_id:
            return None
        try:
            return self.get_channel(int(channel_id)) or await self.fetch_channel(int(channel_id))
        except (discord.HTTPException, ValueError):
            return None

    async def post_request(self, request_id: int) -> None:
        with self.state.conn() as conn:
            row = store.get_request(conn, request_id)
            if row is None or row["message_id"]:
                return
            content = commands.render_request(conn, row)
        channel = await self._channel(row["channel_id"])
        if channel is None:
            return
        extra = {} if row["approved"] else {"view": approve_view(request_id)}
        try:
            msg = await channel.send(content, allowed_mentions=discord.AllowedMentions.none(), **extra)
        except discord.HTTPException:
            log.exception("Posting request %s failed", request_id)
            return
        with self.state.conn() as conn:
            store.set_message(conn, request_id, str(channel.id), str(msg.id))

    async def update_request(self, request_id: int) -> None:
        with self.state.conn() as conn:
            row = store.get_request(conn, request_id)
            if row is None or not row["message_id"]:
                return
            content = commands.render_request(conn, row)
        channel = await self._channel(row["channel_id"])
        if channel is None:
            return
        try:
            msg = channel.get_partial_message(int(row["message_id"]))
            await msg.edit(content=content, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("Editing request %s's message failed", request_id)


async def _art_files(bot: Band3Bot, songs: list[Song]) -> dict[str, discord.File]:
    rv = bot.state.rv
    if rv is None:
        return {}
    paths = await asyncio.gather(*(rv.art(s) for s in songs), return_exceptions=True)
    files = {}
    for s, path in zip(songs, paths, strict=False):
        if isinstance(path, str) and os.path.isfile(path):
            with open(path, "rb") as f:
                files[s.file_id] = discord.File(io.BytesIO(f.read()), filename=f"{s.file_id}.jpg")
    return files


def _register(bot: Band3Bot) -> None:
    tree = bot.tree
    state = bot.state
    settings = state.settings

    async def say(interaction: discord.Interaction, text: str, ephemeral: bool = True) -> None:
        kwargs = {"ephemeral": ephemeral, "allowed_mentions": discord.AllowedMentions.none()}
        if interaction.response.is_done():
            await interaction.followup.send(text, **kwargs)
        else:
            await interaction.response.send_message(text, **kwargs)

    @tree.command(name="request", description="Request a custom song from RhythmVerse for the band's games")
    @app_commands.describe(text="Title, artist or both")
    async def request_cmd(interaction: discord.Interaction, text: str):
        member = member_of(interaction)
        if not commands.can_request(settings, member):
            await say(interaction, f"Requests need the {settings.requester_role} role.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if state.rv is None:
            await say(interaction, "RhythmVerse isn't set up.")
            return
        try:
            result = await state.rv.search(commands.search_options(text))
        except RhythmVerseError as exc:
            await say(interaction, f"Couldn't search RhythmVerse: {exc}")
            return
        songs = result.songs[:commands.MAX_MATCHES]
        if not songs:
            await say(interaction, "RhythmVerse has nothing band3 can download for that. "
                                   "Its search doesn't match title and artist together: try just one.")
            return
        with state.conn() as conn:
            store.remember_songs(conn, songs)
            having = {s.file_id: store.games_having(conn, s) for s in songs}
        files = await _art_files(bot, songs)
        embeds = [discord.Embed.from_dict(commands.song_embed(s, having[s.file_id], s.file_id in files))
                  for s in songs]
        await interaction.followup.send("Pick one:", embeds=embeds, files=list(files.values()),
                                        view=PickView(bot, member, songs), ephemeral=True)

    @tree.command(name="browse", description="A link to the band's song library (just for you)")
    async def browse_cmd(interaction: discord.Interaction):
        await say(interaction, commands.browse_link(state.secret, settings, member_of(interaction),
                                                    str(interaction.channel_id or "")))

    @tree.command(name="queue", description="Requests not every game has yet")
    async def queue_cmd(interaction: discord.Interaction):
        with state.conn() as conn:
            text = commands.queue_text(conn)
        await say(interaction, text, ephemeral=False)

    @tree.command(name="cancel", description="Cancel a request no game has started")
    @app_commands.describe(request="The request")
    async def cancel_cmd(interaction: discord.Interaction, request: str):
        with state.conn() as conn:
            text, cancelled = commands.cancel(conn, settings, member_of(interaction), request)
        await say(interaction, text)
        if cancelled is not None:
            await bot.update_request(cancelled)

    @cancel_cmd.autocomplete("request")
    async def cancel_ac(interaction: discord.Interaction, current: str):
        with state.conn() as conn:
            pairs = commands.cancel_choices(conn, settings, member_of(interaction), current)
        return [app_commands.Choice(name=n, value=v) for n, v in pairs]

    @tree.command(name="pair", description="Pair the band3 showing this code (Settings > Online)")
    @app_commands.describe(code="The 4-digit code band3 shows")
    async def pair_cmd(interaction: discord.Interaction, code: str):
        with state.conn() as conn:
            text = commands.pair(conn, settings, member_of(interaction), code)
        await say(interaction, text)

    @tree.command(name="games", description="Paired games and when each was last online")
    async def games_cmd(interaction: discord.Interaction):
        with state.conn() as conn:
            text = commands.games_text(conn)
        await say(interaction, text)

    @tree.command(name="unpair", description="Stop sending requests to a game")
    @app_commands.describe(game="The game")
    async def unpair_cmd(interaction: discord.Interaction, game: str):
        with state.conn() as conn:
            text = commands.unpair(conn, settings, member_of(interaction), game)
        await say(interaction, text)

    @unpair_cmd.autocomplete("game")
    async def unpair_ac(interaction: discord.Interaction, current: str):
        with state.conn() as conn:
            pairs = commands.game_choices(conn, current)
        return [app_commands.Choice(name=n, value=v) for n, v in pairs]


async def run(bot: Band3Bot, token: str) -> None:
    """Keep the bot connected; record a login failure instead of crashing the web app."""
    try:
        await bot.start(token)
    except discord.LoginFailure as exc:
        bot.last_error = f"Login failed: {exc}"
        log.error("Discord login failed: check DISCORD_TOKEN")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        bot.last_error = f"{type(exc).__name__}: {exc}"
        log.exception("Discord bot stopped")
