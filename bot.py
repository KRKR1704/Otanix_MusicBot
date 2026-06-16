import asyncio
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening, name="!play | Music Bot"
        )
    )


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        await ctx.send("Unknown command. Use `!musichelp` to see available commands.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing argument: `{error.param.name}`. Use `!musichelp` for usage.")
    else:
        raise error


async def main():
    async with bot:
        await bot.load_extension("cogs.music")
        await bot.start(os.getenv("DISCORD_TOKEN"))


asyncio.run(main())
