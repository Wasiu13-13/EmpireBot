import discord
from discord import app_commands
from discord.ext import commands


class InfoView(discord.ui.View):
    def __init__(self, pages: list[discord.Embed]):
        super().__init__(timeout=300)
        self.pages = pages
        self.page_index = 0
        for number, page in enumerate(self.pages, start=1):
            page.set_footer(text=f"Strona {number}/{len(self.pages)} • Użyj przycisków, aby zmienić stronę")
        for button in self.children:
            button.disabled = len(pages) <= 1

    @discord.ui.button(label="◀ Poprzednia", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page_index = (self.page_index - 1) % len(self.pages)
        await interaction.response.edit_message(embed=self.pages[self.page_index], view=self)

    @discord.ui.button(label="Następna ▶", style=discord.ButtonStyle.primary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page_index = (self.page_index + 1) % len(self.pages)
        await interaction.response.edit_message(embed=self.pages[self.page_index], view=self)


class Info(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="info", description="Informacje o EMPIRE BOT i jego modułach.")
    async def info(self, interaction: discord.Interaction):
        pages = [
            discord.Embed(
                title="🤖 EMPIRE BOT",
                description=(
                    "EMPIRE BOT składa się z czterech modułów opisanych na kolejnych stronach:\n\n"
                    "• **Bankier** — harmonogramy wiadomości.\n"
                    "• **Work** — prace, XP i role za aktywność.\n"
                    "• **Ekonomia** — portfel, bank, wypłaty, sklepy, aukcje i kradzieże.\n"
                    "• **Firmy** — inwestycje, produkcja, sklepy firmowe i rynek.\n\n"
                    "Przejdź przez instrukcję przyciskami poniżej."
                ),
                color=discord.Color.blue(),
            ),
            discord.Embed(
                title="🗓️ Bankier — harmonogramy",
                description=(
                    "Bankier wysyła wybraną wiadomość na wskazany kanał o ustawionej godzinie. "
                    "Harmonogramy są konfigurowane osobno dla każdego serwera."
                ),
                color=discord.Color.teal(),
            ).add_field(
                name="Komendy",
                value=(
                    "/bankier dodaj kanał godzina wiadomość — dodaje harmonogram. Podaj godzinę w formacie HH:MM, np. 18:30.\n"
                    "/bankier lista — pokazuje harmonogramy serwera i ich ID.\n"
                    "/bankier usuń id — usuwa harmonogram o podanym ID."
                ),
                inline=False,
            ).add_field(
                name="Uprawnienia i działanie",
                value=(
                    "Dodawanie i usuwanie harmonogramów jest dostępne dla administratorów. Bot musi mieć "
                    "uprawnienia do wysyłania wiadomości na wybranym kanale. Harmonogram jest sprawdzany co minutę."
                ),
                inline=False,
            ),
            discord.Embed(
                title="👷 Work — prace i XP",
                description=(
                    "Prace są powiązane z rolami i kanałami. Za wiadomości na kanałach przypisanych do pracy "
                    "zdobywasz XP. Po osiągnięciu wymaganego progu bot nadaje Ci rolę tej pracy."
                ),
                color=discord.Color.green(),
            ).add_field(
                name="Komendy gracza",
                value=(
                    "/all work — pokazuje dostępne prace, wymagane XP i kanały.\n"
                    "/xp work rola — sprawdza postęp w wybranej pracy.\n"
                    "/top work — pokazuje ranking XP pracowników."
                ),
                inline=False,
            ).add_field(
                name="Konfiguracja i reset",
                value=(
                    "Administrator ustawia XP za wiadomość komendą /xp ustaw i tworzy prace przez /create new work. "
                    "Praca może mieć dzienną wypłatę za rolę, którą odbierzesz przez /collect lub !collect. XP i role "
                    "prac resetują się w niedzielę o 23:00. Administrator może użyć /reset xp i ustawić kanały "
                    "powiadomień przez /create info new_work oraz /create info reset."
                ),
                inline=False,
            ),
            discord.Embed(
                title="💰 Ekonomia — pieniądze, sklepy i gry",
                description=(
                    "Saldo składa się z portfela i banku. Dzienną wypłatę za role pracy odbierzesz przez /collect lub !collect."
                ),
                color=discord.Color.gold(),
            ).add_field(
                name="Portfel, bank i sklepy",
                value=(
                    "/bal lub !bal — sprawdza saldo. /dep all lub !dep all — wpłaca cały portfel do banku.\n"
                    "/withdraw kwota lub !withdraw kwota — wypłaca pieniądze z banku. /daily lub !daily — odbiera dzienny zarobek; "
                    "administrator ustawia zakres przez /create daily minimum maksimum.\n"
                    "/shop lub !shop — otwiera sklep serwerowy. Podaj gracza, aby zobaczyć jego prywatny sklep, "
                    "albo nazwę firmy, aby otworzyć sklep firmowy.\n"
                    "/inventory lub !inventory — pokazuje Twój ekwipunek; podaj nazwę firmy, aby zobaczyć jej magazyn. "
                    "Administrator dodaje przedmiot komendą /create shop item cena opis. Zwykłe przedmioty trafiają do "
                    "ekwipunku. Administrator może ustawić pasywne efekty itemów i ról przez "
                    "/create item; dostępne są m.in. zniżka w systemie firm i premia do zarobków z firm.\n"
                    "/sell rola cena lub !sell @rola cena — wystawia rolę na sprzedaż."
                ),
                inline=False,
            ).add_field(
                name="Kradzieże, aukcje i administracja",
                value=(
                    "/rob gracz kwota lub !rob @gracz kwota — próbuje ukraść pieniądze. Administrator ustawia szansę przez "
                    "/create rob procent. Po nieudanej próbie złodziej traci obstawioną kwotę.\n"
                    "/create auction rola cena-początkowa lub !auction @rola cena-początkowa — rozpoczyna aukcję. "
                    "/bump kwota lub !bump kwota — składa wyższą ofertę.\n"
                    "Komendy administracyjne obejmują /add money, /remove money, /reset money i /delete collect rola."
                ),
                inline=False,
            ),
            discord.Embed(
                title="🏭 Firmy — inwestycje i rynek",
                description=(
                    "Każdy gracz może założyć firmę i inwestować w jej rozwój. Do produkcji produktu firma musi mieć "
                    "wymagane surowce w magazynie. Nie musi posiadać inwestycji, która je wytwarza, jeśli kupiła je "
                    "od innej firmy."
                ),
                color=discord.Color.orange(),
            ).add_field(
                name="Firma i inwestycje",
                value=(
                    "/create company nazwa lub !create company nazwa — tworzy firmę.\n"
                    "/start company lub !start company — administrator ustawia ceny startowe wszystkich inwestycji i produktów z katalogu.\n"
                    "/invest firma inwestycja lub !invest firma inwestycja — kupuje inwestycję.\n"
                    "Utrzymanie inwestycji: co 7 dni bot pobiera z portfela 5% zapłaconej ceny. Gdy brakuje środków, opłata nie jest pobierana.\n"
                    "/investments lub !investments — pokazuje katalog inwestycji, wymagania, produkty i bazowy popyt."
                ),
                inline=False,
            ).add_field(
                name="Produkcja i sklep firmy",
                value=(
                    "/produce firma produkt ilość lub !produce firma produkt ilość — produkuje towar. Bazowy koszt wynosi 90% ceny katalogowej i może zostać obniżony efektem firmowym.\n"
                    "/company inventory firma lub !company inventory firma — pokazuje magazyn.\n"
                    "/company shop firma lub !company shop firma — pokazuje oferty firmy.\n"
                    "/company sell firma produkt ilość cena lub !company sell firma produkt ilość cena — wystawia towar na sprzedaż.\n"
                    "/company share firma lub /share firma — pokazuje aktualne udziały rynkowe. /top companies — ranking firm według wartości."
                ),
                inline=False,
            ).add_field(
                name="Rynek i zakupy",
                value=(
                    "Po dziennym przydziale popytu o 17:00 czasu polskiego bot na bieżąco kupuje wystawione towary, "
                    "aż do wykorzystania dziennego limitu firmy. Popyt bazowy jest wspólną pulą dla firm produkujących "
                    "dany produkt, więc konkurują one o te same zakupy. Bot pomija oferty droższe o 67% lub więcej "
                    "od ceny katalogowej. "
                    "Zmiany cen, zaufania i udziałów uwzględnia dopiero w następnym dziennym przydziale. Zakupy są "
                    "ogłaszane na kanale ustawionym przez /create info events."
                ),
                inline=False,
            ).add_field(
                name="Administracja i ruchy specjalne",
                value=(
                    "Na kanale ustawionym przez /create info events bot raportuje każde zmniejszenie portfela, w tym opłaty firmowe.\n"
                    "Administrator może ponownie ustawić ceny startowe przez /start company, a pojedyncze ceny zmieniać przez /create investment i /create catalog-price. "
                    "Kanał raportów ustawia przez /create info events. /create special move dodaje ruch specjalny do sklepu. "
                    "/reset company po potwierdzeniu usuwa dane firm, ale zachowuje portfele graczy i ustawienia administracyjne. Reklama ma 50% szans powodzenia i po sukcesie zwiększa "
                    "popyt firmy o 25% na 7 dni. Kupiony ruch aktywujesz komendą /open special."
                ),
                inline=False,
            ),
        ]

        view = InfoView(pages)
        await interaction.response.send_message(embed=pages[0], view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(Info(bot))
