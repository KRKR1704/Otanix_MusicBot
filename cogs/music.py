import asyncio
import collections
import os
import random
import shutil
import subprocess
import urllib.parse

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


def _is_playlist_url(query):
    if not query.startswith("http"):
        return False
    try:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(query).query)
        return "list" in params
    except Exception:
        return False


class Track:
    def __init__(self, title, url, duration, requester, thumbnail=None, headers=None, source_url=None):
        self.title = title
        self.url = url          # resolved CDN stream URL; None if lazy
        self.source_url = source_url  # YouTube watch URL, for lazy resolution
        self.duration = duration
        self.requester = requester
        self.thumbnail = thumbnail
        self.headers = headers or {}

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
        self._ytdlp_proc = None

    def kill_ytdlp(self):
        if self._ytdlp_proc:
            try:
                self._ytdlp_proc.kill()
                self._ytdlp_proc.wait(timeout=2)
            except Exception:
                pass
            self._ytdlp_proc = None

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
            # Prefer m4a (ios client itag=140) — direct CDN URL, no IP lock, no HLS segments.
            # Fall through to other non-HLS audio, then best overall if nothing else found.
            "format": "bestaudio[ext=m4a]/bestaudio[protocol!=m3u8][protocol!=m3u8_native]/bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "remote_components": ["ejs:github"],
            # ios client: works with cookies, gives direct m4a streams (itag=140),
            # CDN URLs are not IP-locked unlike web client URLs.
            # android is skipped by yt-dlp when cookies are present, so omit it.
            "extractor_args": {"youtube": {"player_client": ["ios", "web"]}},
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
                fmt = None
                if info.get("url"):
                    fmt = info
                else:
                    fmt = next(
                        (f for f in info.get("formats", [])
                         if f.get("acodec") != "none" and f.get("url")), None
                    )
                if not fmt:
                    raise ValueError("yt-dlp returned no streamable URL")
                info["url"] = fmt["url"]
                info["resolved_headers"] = fmt.get("http_headers", {})
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
            headers=info.get("resolved_headers"),
            source_url=info.get("webpage_url") or info.get("original_url", ""),
        )

    def _start_ytdlp_pipe(self, url):
        """Start a yt-dlp subprocess that downloads audio to stdout for FFmpeg to pipe from."""
        cmd = [
            "yt-dlp",
            "--format", "bestaudio[protocol!=m3u8][protocol!=m3u8_native]/bestaudio",
            "--no-playlist",
            "--quiet",
            "--output", "-",
        ]
        if os.path.exists(COOKIES_FILE):
            cmd += ["--cookies", COOKIES_FILE]
        cmd.append(url)
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    async def _fetch_playlist(self, url):
        ydl_opts = {
            "extract_flat": "in_playlist",
            "quiet": True,
        }
        if os.path.exists(COOKIES_FILE):
            ydl_opts["cookiefile"] = COOKIES_FILE

        loop = asyncio.get_event_loop()

        def _extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if "entries" not in info:
                    return None, []
                entries = [e for e in info["entries"] if e and e.get("id")]
                return info.get("title", "Playlist"), entries

        return await loop.run_in_executor(None, _extract)

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
            state.kill_ytdlp()
            return
        track = state.queue.popleft()
        state.current = track
        vc = state.voice_client
        if vc is None:
            state.current = None
            state.kill_ytdlp()
            return

        # Kill previous yt-dlp download process before starting the next one
        state.kill_ytdlp()

        play_url = track.source_url
        if not play_url:
            print(f"[Music] No source URL for '{track.title}', skipping")
            await self._advance(guild_id, state)
            return

        try:
            # Pipe approach: yt-dlp downloads audio internally (handling all auth/cookies),
            # pipes raw audio bytes to FFmpeg — FFmpeg never makes a direct CDN request,
            # so IP-signed URLs and cookie requirements are not a problem.
            ytdlp_proc = self._start_ytdlp_pipe(play_url)
            state._ytdlp_proc = ytdlp_proc

            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(
                    ytdlp_proc.stdout,
                    pipe=True,
                    options=FFMPEG_OPTIONS,
                    executable="ffmpeg",
                ),
                volume=0.5,
            )
            vc.play(source, after=lambda e: self._after_play(guild_id, e))
        except Exception as e:
            print(f"[Music] ERROR starting playback: {e}")
            state.kill_ytdlp()

    def _now_playing_embed(self, track, title="Now Playing"):
        link = track.url or track.source_url or ""
        embed = discord.Embed(title=title, description=f"[{track.title}]({link})", color=EMBED_COLOR)
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

        if not await self._ensure_voice(ctx, state):
            return

        if _is_playlist_url(query):
            async with ctx.typing():
                playlist_title, entries = await self._fetch_playlist(query)

            if not entries:
                return await ctx.send("Could not load the playlist or it is empty.")

            for entry in entries:
                src = entry.get("url") or f"https://www.youtube.com/watch?v={entry['id']}"
                if not src.startswith("http"):
                    src = f"https://www.youtube.com/watch?v={src}"
                track = Track(
                    title=entry.get("title", "Unknown"),
                    url=None,
                    duration=entry.get("duration", 0),
                    requester=ctx.author,
                    thumbnail=entry.get("thumbnail"),
                    source_url=src,
                )
                state.queue.append(track)

            await ctx.send(f"Added **{len(entries)}** tracks from **{playlist_title}** to the queue.")
            if not state.is_playing() and not state.is_paused():
                await self._advance(ctx.guild.id, state)
            return

        searching_msg = await ctx.send(f"Searching for `{query}`...")
        try:
            async with ctx.typing():
                track = await self._build_track(query, ctx.author)
        except Exception as e:
            await searching_msg.edit(content=f"Could not find or load track: `{e}`")
            return

        if state.is_playing() or state.is_paused():
            state.queue.append(track)
            embed = self._now_playing_embed(track, title="Added to Queue")
            embed.set_footer(text=f"Position in queue: {len(state.queue)}")
            await searching_msg.edit(content=None, embed=embed)
        else:
            state.queue.append(track)
            await self._advance(ctx.guild.id, state)
            await searching_msg.edit(content=None, embed=self._now_playing_embed(track))

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
            cur = state.current
            cur_link = cur.url or cur.source_url or ""
            embed.add_field(
                name="Now Playing",
                value=f"[{cur.title}]({cur_link}) `{cur.duration_str}` — {cur.requester.mention}",
                inline=False,
            )

        queue_list = list(state.queue)[:10]
        if queue_list:
            lines = []
            for i, t in enumerate(queue_list, 1):
                t_link = t.url or t.source_url or ""
                lines.append(f"`{i}.` [{t.title}]({t_link}) `{t.duration_str}` — {t.requester.mention}")
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
        state.kill_ytdlp()
        if state.voice_client and (state.is_playing() or state.is_paused()):
            state.voice_client.stop()
        await ctx.send("Stopped playback and cleared the queue.")

    @commands.command(name="leave", aliases=["dc", "disconnect"])
    async def leave(self, ctx):
        state = self._get_state(ctx.guild.id)
        state.queue.clear()
        state.current = None
        state.kill_ytdlp()
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
