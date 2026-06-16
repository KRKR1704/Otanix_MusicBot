import asyncio
import collections
import os
import random
import shutil

import discord
import yt_dlp
from discord.ext import commands

FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"
EMBED_COLOR = 0x1DB954

print(f"[Music] deno detected at: {shutil.which('deno')}")

COOKIES_FILE = "cookies.txt"
_cookies_env = os.getenv("YT_COOKIES")
if _cookies_env and not os.path.exists(COOKIES_FILE):
    with open(COOKIES_FILE, "w", encoding="utf-8") as f:
        f.write(_cookies_env)


class Track:
    def __init__(self, title, url, duration, requester, thumbnail=None):
        self.title = title
        self.url = url
        self.duration = duration
        self.requester = requester
        self.thumbnail = thumbnail

    @property
    def duration_str(self):
        seconds = int(self.duration or 0)
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"


class GuildMusicState:
    def __init__(self):
        self.queue = collections.deque()
        self.current = None
        self.voice_client = None
        self.loop = False

    def is_playing(self):
        return self.voice_client is not None and self.voice_client.is_playing()

    def is_paused(self):
        return self.voice_client is not None and self.voice_client.is_paused()


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._states = {}

    def _get_state(self, guild_id):
        if guild_id not in self._states:
            self._states[guild_id] = GuildMusicState()
        return self._states[guild_id]

    async def _fetch_yt(self, query):
        ydl_opts = {
            "format": "bestaudio*/bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "remote_components": ["ejs:github"],
        }
        if os.path.exists(COOKIES_FILE):
            ydl_opts["cookiefile"] = COOKIES_FILE
        if not query.startswith("http"):
            query = f"ytsearch1:{query}"

        loop = asyncio.get_event_loop()

        def _extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(query, download=False)
                if "entries" in info:
                    info = info["entries"][0]
                url = info.get("url") or next(
                    (f["url"] for f in info.get("formats", [])
                     if f.get("acodec") != "none" and f.get("url")), None
                )
                if not url:
                    raise ValueError("yt-dlp returned no streamable URL")
                info["url"] = url
                return info

        return await loop.run_in_executor(None, _extract)

    async def _build_track(self, query, requester):
        info = await self._fetch_yt(query)
        return Track(
            title=info.get("title", "Unknown"),
            url=info["url"],
            duration=info.get("duration", 0),
            requester=requester,
            thumbnail=info.get("thumbnail"),
        )

    def _after_play(self, guild_id, error):
        if error:
            print(f"[Music] Playback error: {error}")
        state = self._get_state(guild_id)
        if state.loop and state.current:
            state.queue.appendleft(state.current)
        coro = self._advance(guild_id, state)
        asyncio.run_coroutine_threadsafe(coro, self.bot.loop)

    async def _advance(self, guild_id, state):
        if not state.queue:
            state.current = None
            return
        track = state.queue.popleft()
        state.current = track
        vc = state.voice_client
        if vc is None:
            state.current = None
            return

        try:
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(
                    track.url,
                    before_options=FFMPEG_BEFORE_OPTIONS,
                    options=FFMPEG_OPTIONS,
                    executable="ffmpeg",
                ),
                volume=0.5,
            )
            vc.play(source, after=lambda e: self._after_play(guild_id, e))
        except Exception as e:
            print(f"[Music] ERROR starting playback: {e}")

    def _now_playing_embed(self, track, title="Now Playing"):
        embed = discord.Embed(title=title, description=f"[{track.title}]({track.url})", color=EMBED_COLOR)
        embed.add_field(name="Duration", value=track.duration_str)
        embed.add_field(name="Requested by", value=track.requester.mention)
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        embed.set_author(name=track.requester.display_name, icon_url=track.requester.display_avatar.url)
        return embed

    async def _ensure_voice(self, ctx, state):
        """Connect or reconnect to the user's voice channel. Returns False on failure."""
        channel = ctx.author.voice.channel
        existing = ctx.guild.voice_client

        # Already connected to the right channel and live
        if existing and existing.is_connected() and existing.channel == channel:
            state.voice_client = existing
            return True

        # Disconnect any stale connection first
        if existing:
            try:
                await existing.disconnect(force=True)
            except Exception:
                pass

        try:
            state.voice_client = await channel.connect(reconnect=True)
            for _ in range(20):
                if state.voice_client.is_connected():
                    break
                await asyncio.sleep(0.5)
            return True
        except discord.Forbidden:
            await ctx.send(f"I don't have permission to join **{channel.name}**.")
            return False
        except Exception as e:
            await ctx.send(f"Could not join voice channel: `{e}`")
            return False

    @commands.command(name="play", aliases=["p"])
    async def play(self, ctx, *, query):
        if not ctx.author.voice:
            return await ctx.send("You must be in a voice channel to use this command.")

        state = self._get_state(ctx.guild.id)

        # Join voice FIRST, then fetch — avoids Discord timing out the connection
        if not await self._ensure_voice(ctx, state):
            return

        try:
            async with ctx.typing():
                track = await self._build_track(query, ctx.author)
        except Exception as e:
            return await ctx.send(f"Could not find or load track: `{e}`")

        if state.is_playing() or state.is_paused():
            state.queue.append(track)
            embed = self._now_playing_embed(track, title="Added to Queue")
            embed.set_footer(text=f"Position in queue: {len(state.queue)}")
            await ctx.send(embed=embed)
        else:
            state.queue.append(track)
            await self._advance(ctx.guild.id, state)
            await ctx.send(embed=self._now_playing_embed(track))

    @commands.command(name="pause")
    async def pause(self, ctx):
        state = self._get_state(ctx.guild.id)
        if state.is_playing():
            state.voice_client.pause()
            await ctx.send("Paused.")
        else:
            await ctx.send("Nothing is playing.")

    @commands.command(name="resume", aliases=["r"])
    async def resume(self, ctx):
        state = self._get_state(ctx.guild.id)
        if state.is_paused():
            state.voice_client.resume()
            await ctx.send("Resumed.")
        else:
            await ctx.send("Nothing is paused.")

    @commands.command(name="skip", aliases=["next", "s"])
    async def skip(self, ctx):
        state = self._get_state(ctx.guild.id)
        if not state.is_playing() and not state.is_paused():
            return await ctx.send("Nothing is playing.")
        state.voice_client.stop()
        await ctx.send("Skipped.")

    @commands.command(name="queue", aliases=["q"])
    async def queue(self, ctx):
        state = self._get_state(ctx.guild.id)
        if not state.current and not state.queue:
            return await ctx.send("The queue is empty.")

        embed = discord.Embed(title="Music Queue", color=EMBED_COLOR)
        if state.current:
            embed.add_field(
                name="Now Playing",
                value=f"[{state.current.title}]({state.current.url}) `{state.current.duration_str}` — {state.current.requester.mention}",
                inline=False,
            )

        queue_list = list(state.queue)[:10]
        if queue_list:
            lines = []
            for i, t in enumerate(queue_list, 1):
                lines.append(f"`{i}.` [{t.title}]({t.url}) `{t.duration_str}` — {t.requester.mention}")
            embed.add_field(name="Up Next", value="\n".join(lines), inline=False)

        embed.set_footer(text=f"{len(state.queue)} track(s) in queue | Loop: {'On' if state.loop else 'Off'}")
        await ctx.send(embed=embed)

    @commands.command(name="nowplaying", aliases=["np"])
    async def nowplaying(self, ctx):
        state = self._get_state(ctx.guild.id)
        if not state.current:
            return await ctx.send("Nothing is playing.")
        await ctx.send(embed=self._now_playing_embed(state.current))

    @commands.command(name="stop")
    async def stop(self, ctx):
        state = self._get_state(ctx.guild.id)
        state.queue.clear()
        state.loop = False
        state.current = None
        if state.voice_client and (state.is_playing() or state.is_paused()):
            state.voice_client.stop()
        await ctx.send("Stopped playback and cleared the queue.")

    @commands.command(name="leave", aliases=["dc", "disconnect"])
    async def leave(self, ctx):
        state = self._get_state(ctx.guild.id)
        state.queue.clear()
        state.current = None
        if state.voice_client:
            await state.voice_client.disconnect()
            state.voice_client = None
        await ctx.send("Disconnected.")

    @commands.command(name="loop", aliases=["l"])
    async def loop(self, ctx):
        state = self._get_state(ctx.guild.id)
        state.loop = not state.loop
        await ctx.send(f"Loop is now **{'On' if state.loop else 'Off'}**.")

    @commands.command(name="remove")
    async def remove(self, ctx, position: int):
        state = self._get_state(ctx.guild.id)
        if position < 1 or position > len(state.queue):
            return await ctx.send(f"Invalid position. Queue has {len(state.queue)} track(s).")
        queue_list = list(state.queue)
        removed = queue_list.pop(position - 1)
        state.queue = collections.deque(queue_list)
        await ctx.send(f"Removed **{removed.title}** from the queue.")

    @commands.command(name="clear")
    async def clear(self, ctx):
        state = self._get_state(ctx.guild.id)
        state.queue.clear()
        await ctx.send("Queue cleared.")

    @commands.command(name="volume", aliases=["vol"])
    async def volume(self, ctx, vol: int):
        if not 0 <= vol <= 100:
            return await ctx.send("Volume must be between 0 and 100.")
        state = self._get_state(ctx.guild.id)
        if state.voice_client and state.voice_client.source:
            state.voice_client.source.volume = vol / 100
            await ctx.send(f"Volume set to **{vol}%**.")
        else:
            await ctx.send("Nothing is playing.")

    @commands.command(name="shuffle")
    async def shuffle(self, ctx):
        state = self._get_state(ctx.guild.id)
        if len(state.queue) < 2:
            return await ctx.send("Need at least 2 songs in the queue to shuffle.")
        queue_list = list(state.queue)
        random.shuffle(queue_list)
        state.queue = collections.deque(queue_list)
        await ctx.send("Queue shuffled.")

    @commands.command(name="musichelp", aliases=["mhelp"])
    async def musichelp(self, ctx):
        embed = discord.Embed(title="Music Bot Commands", color=EMBED_COLOR)
        commands_list = [
            ("!play <query>", "!p", "Play a song by name or YouTube URL"),
            ("!pause", "", "Pause the current track"),
            ("!resume", "!r", "Resume the paused track"),
            ("!skip", "!next, !s", "Skip the current track"),
            ("!queue", "!q", "Show the current queue"),
            ("!nowplaying", "!np", "Show what's currently playing"),
            ("!stop", "", "Stop playback and clear the queue"),
            ("!leave", "!dc, !disconnect", "Disconnect the bot from voice"),
            ("!loop", "!l", "Toggle loop mode"),
            ("!remove <pos>", "", "Remove a track from the queue by position"),
            ("!clear", "", "Clear the queue without stopping playback"),
            ("!volume <0-100>", "!vol", "Set the playback volume"),
            ("!shuffle", "", "Shuffle the queue"),
            ("!musichelp", "!mhelp", "Show this help message"),
        ]
        lines = []
        for cmd, aliases, desc in commands_list:
            alias_str = f" (aliases: {aliases})" if aliases else ""
            lines.append(f"`{cmd}`{alias_str}\n{desc}")
        embed.description = "\n\n".join(lines)
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Music(bot))
