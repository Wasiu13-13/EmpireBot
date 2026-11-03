import os
import asyncio

import discord
from discord.ext import commands
from dotenv import load_dotenv

from database import create_tables


create_tables()
load_dotenv()
TOKEN = os.getenv("TOKEN")

if not TOKEN:
    raise RuntimeError("Nie znaleziono TOKEN w pliku .env")


intents = discord.Intents.default()
intents.message_content = True
intents.members = True


class Bot(commands.Bot):
    async def setup_hook(self):
        extensions = (
            "cogs.bankier",
            "cogs.work",
            "cogs.info",
            "cogs.ekonomia",
            "cogs.firmy",
        )
        for extension in extensions:
            if extension in self.extensions:
                print(f"⏭️ {extension} jest już załadowany — pomijam.")
                continue
            print(f"🔵 Ładowanie {extension}...")
            await self.load_extension(extension)
            print(f"✅ Załadowano {extension}")


bot = Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print("🔄 Start synchronizacji...")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Zalogowano jako {bot.user}")
        print(f"✅ Zsynchronizowano {len(synced)} komend.")
    except Exception as error:
        print("❌ Błąd synchronizacji:")
        print(error)


async def main():
    async with bot:
        await bot.start(TOKEN)


asyncio.run(main())
