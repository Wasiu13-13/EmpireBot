import discord
from discord.ext import commands, tasks
from discord import app_commands

import sqlite3
from datetime import datetime

DB_NAME = "database.db"


class Bankier(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.sprawdz_harmonogramy.start()

    def cog_unload(self):
        self.sprawdz_harmonogramy.cancel()

    @tasks.loop(minutes=1)
    async def sprawdz_harmonogramy(self):

        teraz = datetime.now()
        aktualna_godzina = teraz.strftime("%H:%M")

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        cursor.execute("""
        SELECT id, guild_id, channel_id, message, hour
        FROM schedules
        WHERE hour=?
        """, (aktualna_godzina,))

        harmonogramy = cursor.fetchall()

        conn.close()

        for rekord in harmonogramy:

            schedule_id = rekord[0]
            guild_id = rekord[1]
            channel_id = rekord[2]
            message = rekord[3]
            hour = rekord[4]

            kanał = self.bot.get_channel(channel_id)

            if kanał is None:
                print(
                    f"Nie znaleziono kanału dla harmonogramu {schedule_id}"
                )
                continue

            try:
                await kanał.send(message)

                print(
                    f"Wysłano harmonogram {schedule_id} "
                    f"na serwerze {guild_id}"
                )

            except discord.Forbidden:
                print(
                    f"Brak uprawnień do wysłania wiadomości "
                    f"w kanale {channel_id}"
                )

            except discord.HTTPException as e:
                print(
                    f"Błąd Discord przy harmonogramie "
                    f"{schedule_id}: {e}"
                )

    @sprawdz_harmonogramy.before_loop
    async def przed_sprawdzaniem(self):
        await self.bot.wait_until_ready()

    bankier = app_commands.Group(
        name="bankier",
        description="Zarządzanie harmonogramami"
    )

    @bankier.command(
        name="dodaj",
        description="Dodaj harmonogram"
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def dodaj(
        self,
        interaction: discord.Interaction,
        kanał: discord.TextChannel,
        godzina: str,
        wiadomość: str
    ):

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        cursor.execute("""
        INSERT INTO schedules(
            guild_id,
            channel_id,
            message,
            hour
        )
        VALUES (?, ?, ?, ?)
        """, (
            interaction.guild.id,
            kanał.id,
            wiadomość,
            godzina
        ))

        conn.commit()
        conn.close()

        await interaction.response.send_message(
            "✅ Harmonogram został dodany.",
            ephemeral=True
        )

    @bankier.command(
        name="lista",
        description="Lista harmonogramów"
    )
    async def lista(
        self,
        interaction: discord.Interaction
    ):

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        cursor.execute("""
        SELECT id, channel_id, message, hour
        FROM schedules
        WHERE guild_id=?
        """, (interaction.guild.id,))

        dane = cursor.fetchall()

        conn.close()

        if not dane:
            await interaction.response.send_message(
                "Brak harmonogramów."
            )
            return

        tekst = ""

        for rekord in dane:

            tekst += (
                f"**ID:** {rekord[0]}\n"
                f"Kanał: <#{rekord[1]}>\n"
                f"Godzina: {rekord[3]}\n"
                f"Wiadomość: `{rekord[2]}`\n\n"
            )

        await interaction.response.send_message(tekst)

    @bankier.command(
        name="usuń",
        description="Usuń harmonogram"
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def usun(
        self,
        interaction: discord.Interaction,
        id: int
    ):

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        cursor.execute("""
        DELETE FROM schedules
        WHERE id=?
        AND guild_id=?
        """, (
            id,
            interaction.guild.id
        ))

        conn.commit()

        usunieto = cursor.rowcount

        conn.close()

        if usunieto == 0:
            await interaction.response.send_message(
                "❌ Nie znaleziono takiego harmonogramu.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "🗑 Harmonogram usunięty.",
            ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(Bankier(bot))