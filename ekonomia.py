import random
import asyncio
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from database import connect, create_tables


SHOP_PAGE_SIZE = 6


def ensure_account(guild_id: int, user_id: int):
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO economy_accounts (guild_id, user_id) VALUES (?, ?)",
            (guild_id, user_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO economy_settings (guild_id) VALUES (?)",
            (guild_id,),
        )


def format_money(amount: int) -> str:
    return f"{amount:,}".replace(",", " ")


def normalize_key(value: str) -> str:
    return " ".join(value.casefold().split())


def parse_box_rewards(raw: str, guild: discord.Guild):
    """Parse `item:name=chance; role:name=chance; money:amount=chance`."""
    entries = []
    chance_total = 0
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            reward_spec, chance_text = part.rsplit("=", 1)
            chance_text = chance_text.strip().removesuffix("%")
            try:
                chance = int(chance_text)
            except ValueError as error:
                raise ValueError(f"Nieprawidłowa szansa w `{part}`.") from error
            if not 0 <= chance <= 100:
                raise ValueError("Każda szansa musi być z zakresu 0–100%.")
        else:
            reward_spec, chance = part, 0

        reward_type, separator, reward_value = reward_spec.partition(":")
        reward_type = reward_type.strip().casefold()
        reward_value = reward_value.strip()
        if not separator or not reward_value:
            raise ValueError(
                f"Nieprawidłowa nagroda `{part}`. Użyj `item:nazwa=szansa`, "
                "`role:nazwa=szansa` albo `money:kwota=szansa`."
            )

        if reward_type in {"money", "pieniądze", "pieniadze", "kasa"}:
            try:
                amount = int(reward_value.replace(" ", ""))
            except ValueError as error:
                raise ValueError(f"Kwota nagrody `{reward_value}` musi być liczbą.") from error
            if amount < 1:
                raise ValueError("Kwota nagrody pieniężnej musi być większa od zera.")
            entry = {
                "type": "money",
                "key": f"money:{amount}",
                "name": f"{format_money(amount)}$",
                "amount": amount,
                "chance": chance,
            }
        elif reward_type in {"role", "rola"}:
            match = re.fullmatch(r"<@&(\d+)>|([0-9]{15,22})", reward_value)
            role = (
                guild.get_role(int(match.group(1) or match.group(2)))
                if match else discord.utils.get(guild.roles, name=reward_value)
            )
            if role is None:
                raise ValueError(f"Nie znaleziono roli `{reward_value}`.")
            if role == guild.default_role or role.managed:
                raise ValueError(f"Rola {role.name} nie może być nagrodą.")
            bot_member = guild.me
            if (
                bot_member is None
                or not bot_member.guild_permissions.manage_roles
                or role.position >= bot_member.top_role.position
            ):
                raise ValueError(
                    f"Bot nie może przyznawać roli {role.name}; przenieś jego rolę wyżej."
                )
            entry = {
                "type": "role",
                "key": f"role:{role.id}",
                "name": role.name,
                "amount": 0,
                "chance": chance,
            }
        elif reward_type in {"item", "przedmiot"}:
            item_name = " ".join(reward_value.split())
            if len(item_name) > 100:
                raise ValueError("Nazwa nagrody typu item może mieć maksymalnie 100 znaków.")
            entry = {
                "type": "item",
                "key": f"item:{normalize_key(item_name)}",
                "name": item_name,
                "amount": 0,
                "chance": chance,
            }
        else:
            raise ValueError(
                f"Nieznany typ nagrody `{reward_type}`. Użyj item, role albo money."
            )

        entries.append(entry)
        chance_total += chance

    if not entries:
        raise ValueError("Dodaj przynajmniej jedną nagrodę.")
    if chance_total > 100:
        raise ValueError("Suma wpisanych szans nie może przekraczać 100%.")

    remainder_share = (100 - chance_total) / len(entries)
    for entry in entries:
        entry["chance"] += remainder_share
    return entries


EFFECT_NAMES = {
    "rob_defense": "Ochrona przed kradzieżą",
    "rob_bonus": "Premia do szansy kradzieży",
    "income_bonus": "Premia do zarobków",
    "shop_discount": "Zniżka w sklepach",
    "company_discount": "Zniżka w systemie firm",
    "company_income_bonus": "Premia do zarobków z firm",
}


def effects_for_user(
    conn, guild_id: int, user_id: int, role_ids: tuple[int, ...] | list[int] = ()
) -> dict[str, int]:
    """Sum passive effects from a user's inventory and owned roles."""
    active_products = {
        f"role:{role_id}": 1
        for role_id in role_ids
    }
    inventory = conn.execute(
        "SELECT product_key, quantity FROM economy_inventory "
        "WHERE guild_id = ? AND user_id = ? AND quantity > 0",
        (guild_id, user_id),
    ).fetchall()
    for product_key, quantity in inventory:
        active_products[product_key] = active_products.get(product_key, 0) + quantity

    totals = {effect_type: 0 for effect_type in EFFECT_NAMES}
    if not active_products:
        return totals

    for product_key, effect_type, value in conn.execute(
        "SELECT product_key, effect_type, effect_value "
        "FROM economy_item_effects WHERE guild_id = ?",
        (guild_id,),
    ).fetchall():
        if product_key in active_products and effect_type in totals:
            totals[effect_type] += value * active_products[product_key]

    totals["rob_defense"] = min(100, totals["rob_defense"])
    totals["rob_bonus"] = min(100, totals["rob_bonus"])
    totals["income_bonus"] = min(500, totals["income_bonus"])
    totals["shop_discount"] = min(100, totals["shop_discount"])
    totals["company_discount"] = min(100, totals["company_discount"])
    totals["company_income_bonus"] = min(500, totals["company_income_bonus"])
    return totals


def active_effects(conn, guild_id: int, member: discord.Member) -> dict[str, int]:
    """Sum passive effects from a member's inventory items and owned roles."""
    return effects_for_user(
        conn, guild_id, member.id, tuple(role.id for role in member.roles)
    )


def apply_income_bonus(amount: int, effects: dict[str, int]) -> int:
    return amount * (100 + effects["income_bonus"]) // 100


def discounted_price(price: int, effects: dict[str, int]) -> int:
    return price * (100 - effects["shop_discount"]) // 100


def discounted_company_price(price: int, effects: dict[str, int]) -> int:
    return price * (100 - effects["company_discount"]) // 100


def apply_company_income_bonus(amount: int, effects: dict[str, int]) -> int:
    return amount * (100 + effects["company_income_bonus"]) // 100


def cooldown_message(
    last_claim: str,
    now: datetime,
    cooldown_hours: int = 24,
) -> str | None:
    next_claim = datetime.fromisoformat(last_claim) + timedelta(hours=cooldown_hours)
    if now >= next_claim:
        return None

    remaining = next_claim - now
    hours, remainder = divmod(int(remaining.total_seconds()), 3600)
    minutes = (remainder + 59) // 60
    return f"⏳ Możesz odebrać wypłatę ponownie za **{hours} godz. {minutes} min**."


class ShopSelect(discord.ui.Select):
    def __init__(self, view):
        self.shop_view = view
        start = view.page * SHOP_PAGE_SIZE
        page_listings = view.listings[start : start + SHOP_PAGE_SIZE]
        options = [
            discord.SelectOption(
                label=item["role_name"][:100],
                value=str(item["listing_id"]),
                description=f"Cena: {format_money(item['price'])}$"[:100],
            )
            for item in page_listings
        ]
        super().__init__(
            placeholder="Wybierz rolę, którą chcesz kupić",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        self.shop_view.selected_by_user[interaction.user.id] = int(self.values[0])
        await interaction.response.send_message(
            "Oferta wybrana. Kliknij **Kup wybraną rolę**.",
            ephemeral=True,
        )


class ShopBuyButton(discord.ui.Button):
    def __init__(self, view):
        super().__init__(
            label="Kup wybraną rolę",
            style=discord.ButtonStyle.success,
            row=1,
        )
        self.shop_view = view

    async def callback(self, interaction: discord.Interaction):
        listing_id = self.shop_view.selected_by_user.get(interaction.user.id)
        if listing_id is None:
            return await interaction.response.send_message(
                "Najpierw wybierz ofertę z listy.", ephemeral=True
            )
        await self.shop_view.cog._buy_listing(interaction, listing_id)


class ShopPageButton(discord.ui.Button):
    def __init__(self, view, direction: int):
        self.shop_view = view
        self.direction = direction
        label = "◀ Poprzednia" if direction < 0 else "Następna ▶"
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=2)

    async def callback(self, interaction: discord.Interaction):
        page_count = max(1, (len(self.shop_view.listings) + SHOP_PAGE_SIZE - 1) // SHOP_PAGE_SIZE)
        self.shop_view.page = (self.shop_view.page + self.direction) % page_count
        self.shop_view.selected_by_user.clear()
        self.shop_view.refresh_items()
        await interaction.response.edit_message(
            embed=self.shop_view.make_embed(), view=self.shop_view
        )


class ShopView(discord.ui.View):
    def __init__(self, cog, guild: discord.Guild, seller: discord.Member, listings):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.seller = seller
        self.listings = listings
        self.page = 0
        self.selected_by_user: dict[int, int] = {}
        self.refresh_items()

    def refresh_items(self):
        self.clear_items()
        self.add_item(ShopSelect(self))
        self.add_item(ShopBuyButton(self))

        page_count = max(1, (len(self.listings) + SHOP_PAGE_SIZE - 1) // SHOP_PAGE_SIZE)
        if page_count > 1:
            self.add_item(ShopPageButton(self, -1))
            self.add_item(ShopPageButton(self, 1))

    def make_embed(self):
        start = self.page * SHOP_PAGE_SIZE
        page_listings = self.listings[start : start + SHOP_PAGE_SIZE]
        embed = discord.Embed(
            title=f"🛍️ Sklep gracza • {self.seller.display_name}",
            description=(
                "Wybierz ofertę z listy, a następnie kliknij **Kup wybraną rolę**.\n"
                "Płatność zostanie pobrana z portfela."
            ),
            color=discord.Color.blurple(),
        )
        for item in page_listings:
            embed.add_field(
                name=f"🎭 {item['role_name']}  ·  {format_money(item['price'])}$",
                value=f"Sprzedawca: {self.seller.mention}",
                inline=False,
            )
        embed.set_footer(
            text=f"Oferta {start + 1}–{start + len(page_listings)} z {len(self.listings)}"
        )
        return embed


class ServerShopSelect(discord.ui.Select):
    def __init__(self, view):
        self.shop_view = view
        start = view.page * SHOP_PAGE_SIZE
        page_listings = view.listings[start : start + SHOP_PAGE_SIZE]
        options = [
            discord.SelectOption(
                label=(
                    f"🎁 {item['product_name']}"
                    if item["product_key"].startswith("box:")
                    else item["product_name"]
                )[:100],
                value=str(item["listing_id"]),
                description=(
                    f"{format_money(item['price'])}$ • "
                    f"{item['description']}"
                )[:100],
            )
            for item in page_listings
        ]
        super().__init__(
            placeholder="Wybierz produkt ze sklepu serwerowego",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        self.shop_view.selected_by_user[interaction.user.id] = int(self.values[0])
        await interaction.response.send_message(
            "Produkt wybrany. Kliknij **Kup wybrany produkt**.", ephemeral=True
        )


class ServerShopBuyButton(discord.ui.Button):
    def __init__(self, view):
        super().__init__(
            label="Kup wybrany produkt",
            style=discord.ButtonStyle.success,
            row=1,
        )
        self.shop_view = view

    async def callback(self, interaction: discord.Interaction):
        listing_id = self.shop_view.selected_by_user.get(interaction.user.id)
        if listing_id is None:
            return await interaction.response.send_message(
                "Najpierw wybierz produkt z listy.", ephemeral=True
            )
        await self.shop_view.cog._buy_server_product(interaction, listing_id)


class ServerShopView(discord.ui.View):
    def __init__(self, cog, guild: discord.Guild, listings):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.listings = listings
        self.page = 0
        self.selected_by_user: dict[int, int] = {}
        self.refresh_items()

    def refresh_items(self):
        self.clear_items()
        self.add_item(ServerShopSelect(self))
        self.add_item(ServerShopBuyButton(self))
        page_count = max(1, (len(self.listings) + SHOP_PAGE_SIZE - 1) // SHOP_PAGE_SIZE)
        if page_count > 1:
            self.add_item(ShopPageButton(self, -1))
            self.add_item(ShopPageButton(self, 1))

    def make_embed(self):
        start = self.page * SHOP_PAGE_SIZE
        page_listings = self.listings[start : start + SHOP_PAGE_SIZE]
        embed = discord.Embed(
            title=f"🛍️ Sklep serwerowy • {self.guild.name}",
            description=(
                "Kup role i przedmioty za pieniądze z portfela.\n"
                "Wybierz produkt z listy poniżej i naciśnij **Kup produkt**."
            ),
            color=discord.Color.gold(),
        )
        for item in page_listings:
            kind = item["product_name"]
            if item["product_type"] == "role":
                role = self.guild.get_role(item["role_id"])
                kind = role.name if role is not None else item["product_name"]
                icon = "🎭"
            else:
                icon = "📦"
            if item["product_key"].startswith("box:"):
                icon = "🎁 Skrzynka"
            embed.add_field(
                name=f"{icon} {kind}  ·  {format_money(item['price'])}$",
                value=item["description"] or "Brak dodatkowego opisu.",
                inline=False,
            )
        embed.set_footer(
            text=(
                f"Strona {self.page + 1}/{max(1, (len(self.listings) + SHOP_PAGE_SIZE - 1) // SHOP_PAGE_SIZE)}"
                f"  •  {len(self.listings)} produktów"
            )
        )
        return embed


class ResetMoneyView(discord.ui.View):
    def __init__(self, cog, guild_id: int, admin_id: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.guild_id = guild_id
        self.admin_id = admin_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "Tylko administrator, który uruchomił reset, może go potwierdzić.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Potwierdź reset ekonomii",
        style=discord.ButtonStyle.danger,
    )
    async def confirm_reset(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                "Reset może potwierdzić tylko administrator.", ephemeral=True
            )

        balances_reset, listings_deleted, auctions_cancelled = (
            self.cog.reset_guild_economy(self.guild_id)
        )
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.edit_message(
            content=(
                "✅ Reset ekonomii zakończony.\n"
                f"Wyzerowano salda kont: **{balances_reset}**.\n"
                f"Usunięto ofert z prywatnych sklepów: **{listings_deleted}**.\n"
                f"Anulowano aktywne aukcje: **{auctions_cancelled}**.\n"
                "Sklep serwerowy nie został zmieniony."
            ),
            view=self,
        )

    @discord.ui.button(
        label="Anuluj",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel_reset(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.edit_message(
            content="Reset ekonomii anulowany.",
            view=self,
        )


class Ekonomia(commands.Cog):
    dep_group = app_commands.Group(name="dep", description="Wpłaty do banku.")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.attached_admin_groups: dict[str, app_commands.Group] = {}
        self.added_admin_subcommands: dict[str, list[str]] = {}
        self.created_admin_groups: set[str] = set()
        self.purchase_lock = asyncio.Lock()
        self.box_lock = asyncio.Lock()
        self.roulette_lock = asyncio.Lock()
        self.online_roulette_lock = asyncio.Lock()
        self.auction_lock = asyncio.Lock()
        self.auction_task: asyncio.Task | None = None
        self.online_roulette_task: asyncio.Task | None = None
        create_tables()

    async def cog_load(self):
        self.auction_task = asyncio.create_task(self._auction_watcher())
        self.online_roulette_task = asyncio.create_task(self._online_roulette_watcher())

    def cog_unload(self):
        if self.auction_task is not None:
            self.auction_task.cancel()
        if self.online_roulette_task is not None:
            self.online_roulette_task.cancel()
        for group_name, group in self.attached_admin_groups.items():
            for subcommand in self.added_admin_subcommands.get(group_name, []):
                if group.get_command(subcommand) is not None:
                    group.remove_command(subcommand)
            if group_name in self.created_admin_groups:
                self.bot.tree.remove_command(group_name)

    async def _auction_watcher(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                now = datetime.now(timezone.utc)
                with connect() as conn:
                    due_auctions = conn.execute(
                        "SELECT auction_id FROM economy_auctions "
                        "WHERE status = 'active' AND ends_at <= ?",
                        (now.isoformat(),),
                    ).fetchall()
                for (auction_id,) in due_auctions:
                    await self._finish_auction(auction_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"Błąd sprawdzania aukcji: {error!r}")
            await asyncio.sleep(3)

    @app_commands.command(name="bal", description="Sprawdź stan portfela i banku.")
    @app_commands.describe(member="Opcjonalnie: sprawdź saldo innego gracza")
    async def balance_slash(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                "Ekonomia działa tylko na serwerze.", ephemeral=True
            )

        account_member = member or interaction.user
        ensure_account(interaction.guild_id, account_member.id)
        with connect() as conn:
            row = conn.execute(
                "SELECT wallet, bank FROM economy_accounts WHERE guild_id = ? AND user_id = ?",
                (interaction.guild_id, account_member.id),
            ).fetchone()

        await interaction.response.send_message(
            f"💰 **Stan konta {account_member.display_name}**\n"
            f"Portfel: **{format_money(row[0])}**\n"
            f"Bank: **{format_money(row[1])}**"
        )

    @commands.command(name="bal", aliases=["balance"])
    @commands.guild_only()
    async def balance_prefix(
        self,
        ctx: commands.Context,
        member: discord.Member | None = None,
    ):
        account_member = member or ctx.author
        ensure_account(ctx.guild.id, account_member.id)
        with connect() as conn:
            row = conn.execute(
                "SELECT wallet, bank FROM economy_accounts WHERE guild_id = ? AND user_id = ?",
                (ctx.guild.id, account_member.id),
            ).fetchone()
        await ctx.reply(
            f"💰 **Stan konta {account_member.display_name}**\n"
            f"Portfel: **{format_money(row[0])}**\n"
            f"Bank: **{format_money(row[1])}**",
            mention_author=False,
        )

    async def top_economy_slash(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                "Ranking działa tylko na serwerze.", ephemeral=True
            )
        await self._show_economy_top(interaction, interaction.guild)

    @commands.group(name="top", invoke_without_command=True)
    @commands.guild_only()
    async def top_prefix(self, ctx: commands.Context):
        await ctx.reply(
            "Użyj `!top ekonomy`, aby zobaczyć ranking ekonomii.",
            mention_author=False,
        )

    @top_prefix.command(name="ekonomy", aliases=["economy", "ekonomia"])
    @commands.guild_only()
    async def top_economy_prefix(self, ctx: commands.Context):
        await self._show_economy_top(ctx, ctx.guild)

    async def _show_economy_top(self, target, guild: discord.Guild):
        with connect() as conn:
            rows = conn.execute(
                "SELECT user_id, wallet, bank, wallet + bank AS total "
                "FROM economy_accounts WHERE guild_id = ? "
                "ORDER BY total DESC, user_id ASC LIMIT 10",
                (guild.id,),
            ).fetchall()

        if not rows:
            embed = discord.Embed(
                title=f"🏆 Ranking ekonomii • {guild.name}",
                description="Nie ma jeszcze żadnych kont w rankingu.",
                color=discord.Color.gold(),
            )
        else:
            medals = ["🥇", "🥈", "🥉"]
            lines = []
            for position, (user_id, wallet, bank, total) in enumerate(rows, start=1):
                marker = medals[position - 1] if position <= len(medals) else f"`{position}.`"
                lines.append(
                    f"{marker} <@{user_id}>  **{format_money(total)}$**\n"
                    f"　Portfel {format_money(wallet)}$ · Bank {format_money(bank)}$"
                )
            embed = discord.Embed(
                title=f"🏆 Ranking ekonomii • {guild.name}",
                description="Łączny majątek gracza: portfel + bank.\n\n" + "\n\n".join(lines),
                color=discord.Color.gold(),
            )
        embed.set_footer(text="Ranking obejmuje 10 graczy z największym majątkiem.")
        if isinstance(target, discord.Interaction):
            await target.response.send_message(embed=embed)
        else:
            await target.reply(embed=embed, mention_author=False)

    @app_commands.command(name="inventory", description="Pokaż swój ekwipunek lub magazyn firmy.")
    @app_commands.describe(firm="Opcjonalnie: nazwa Twojej firmy, której magazyn chcesz zobaczyć")
    async def inventory_slash(
        self,
        interaction: discord.Interaction,
        firm: str | None = None,
    ):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                "Ekwipunek działa tylko na serwerze.", ephemeral=True
            )
        if firm is not None:
            firm_cog = self.bot.get_cog("Firmy")
            if firm_cog is None:
                return await interaction.response.send_message(
                    "Moduł firm jest obecnie niedostępny.", ephemeral=True
                )
            return await firm_cog.inventory(interaction, firm)
        await self._show_inventory(interaction, interaction.guild_id, interaction.user.id)

    @commands.command(name="inventory", aliases=["inv"])
    @commands.guild_only()
    async def inventory_prefix(
        self,
        ctx: commands.Context,
        *,
        firm: str | None = None,
    ):
        if firm is not None:
            firm_cog = self.bot.get_cog("Firmy")
            if firm_cog is None:
                return await ctx.reply("Moduł firm jest obecnie niedostępny.", mention_author=False)
            return await firm_cog._prefix_call(ctx, firm_cog.inventory, firm)
        await self._show_inventory(ctx, ctx.guild.id, ctx.author.id)

    async def _show_inventory(self, target, guild_id: int, user_id: int):
        with connect() as conn:
            rows = conn.execute(
                "SELECT item_name, quantity FROM economy_inventory "
                "WHERE guild_id = ? AND user_id = ? AND quantity > 0 "
                "ORDER BY item_name COLLATE NOCASE LIMIT 30",
                (guild_id, user_id),
            ).fetchall()
        if not rows:
            return await self._reply(target, "🎒 Twój ekwipunek jest pusty.", ephemeral=True)

        lines = [f"• **{name}** × {quantity}" for name, quantity in rows]
        embed = discord.Embed(
            title="🎒 Twój ekwipunek",
            description="\n".join(lines),
            color=discord.Color.green(),
        )
        embed.set_footer(text="Pokazano maksymalnie 30 rodzajów itemów.")
        if isinstance(target, discord.Interaction):
            await target.response.send_message(embed=embed, ephemeral=True)
        else:
            await target.reply(embed=embed, mention_author=False)

    @app_commands.describe(
        box_name="Nazwa skrzynki, gdy masz ich kilka rodzajów",
        quantity="Liczba skrzynek do otwarcia (maksymalnie 100)",
    )
    async def open_box_slash(
        self,
        interaction: discord.Interaction,
        box_name: str | None = None,
        quantity: app_commands.Range[int, 1, 100] = 1,
    ):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                "Skrzynki można otwierać tylko na serwerze.", ephemeral=True
            )
        await self._open_box(
            interaction, interaction.guild, interaction.user, box_name, quantity
        )

    @commands.group(name="open", invoke_without_command=True)
    @commands.guild_only()
    async def open_prefix(self, ctx: commands.Context):
        await ctx.reply(
            "Użyj `!open box [nazwa skrzynki] [ilość]`, aby otworzyć skrzynkę.",
            mention_author=False,
        )

    @open_prefix.command(name="box")
    @commands.guild_only()
    async def open_box_prefix(
        self,
        ctx: commands.Context,
        *,
        arguments: str | None = None,
    ):
        box_name = None
        quantity = 1
        if arguments:
            if arguments.isdigit():
                quantity = int(arguments)
            else:
                parts = arguments.rsplit(maxsplit=1)
                if len(parts) == 2 and parts[1].isdigit():
                    box_name, quantity = parts[0], int(parts[1])
                else:
                    box_name = arguments
        await self._open_box(ctx, ctx.guild, ctx.author, box_name, quantity)

    async def _open_box(
        self,
        target,
        guild: discord.Guild,
        member: discord.Member,
        requested_box: str | None = None,
        quantity: int = 1,
    ):
        if not 1 <= quantity <= 100:
            return await self._reply(
                target,
                "Liczba skrzynek musi wynosić od 1 do 100.",
                ephemeral=True,
            )
        await self.box_lock.acquire()
        try:
            ensure_account(guild.id, member.id)
            with connect() as conn:
                owned_boxes = conn.execute(
                    "SELECT i.product_key, i.item_name, i.quantity, b.box_name "
                    "FROM economy_inventory i JOIN economy_boxes b "
                    "ON b.guild_id = i.guild_id AND b.box_key = i.product_key "
                    "WHERE i.guild_id = ? AND i.user_id = ? "
                    "AND i.quantity > 0 AND i.product_key LIKE 'box:%' "
                    "ORDER BY i.item_name COLLATE NOCASE",
                    (guild.id, member.id),
                ).fetchall()
                if not owned_boxes:
                    return await self._reply(
                        target,
                        "Nie masz żadnej skrzynki do otwarcia. Kup ją w `/shop`.",
                        ephemeral=True,
                    )
                if requested_box:
                    requested_key = f"box:{normalize_key(requested_box)}"
                    box = next((row for row in owned_boxes if row[0] == requested_key), None)
                    if box is None:
                        names = ", ".join(f"**{row[3]}** × {row[2]}" for row in owned_boxes)
                        return await self._reply(
                            target,
                            f"Nie masz skrzynki **{requested_box}**. Twoje skrzynki: {names}",
                            ephemeral=True,
                        )
                elif len(owned_boxes) == 1:
                    box = owned_boxes[0]
                else:
                    names = ", ".join(f"**{row[3]}** × {row[2]}" for row in owned_boxes)
                    return await self._reply(
                        target,
                        "Masz kilka rodzajów skrzynek. Podaj nazwę po komendzie, np. "
                        f"`/open box box_name:{owned_boxes[0][3]}`. Masz: {names}",
                        ephemeral=True,
                    )
                box_key = box[0]
                if box[2] < quantity:
                    return await self._reply(
                        target,
                        f"Masz **{box[2]}** skrzynek **{box[3]}**, "
                        f"a próbujesz otworzyć **{quantity}**.",
                        ephemeral=True,
                    )
                rewards = conn.execute(
                    "SELECT reward_type, reward_key, reward_name, reward_amount, chance "
                    "FROM economy_box_rewards WHERE guild_id = ? AND box_key = ?",
                    (guild.id, box_key),
                ).fetchall()

            if not rewards:
                return await self._reply(
                    target,
                    f"Skrzynka **{box[3]}** nie ma skonfigurowanych nagród. "
                    "Poproś administratora o ponowne ustawienie jej zawartości.",
                    ephemeral=True,
                )

            selected_rewards = [
                random.choices(rewards, weights=[row[4] for row in rewards], k=1)[0]
                for _ in range(quantity)
            ]
            roles_to_add = {}
            for reward in selected_rewards:
                if reward[0] != "role":
                    continue
                role_id = int(reward[1].split(":", 1)[1])
                role = guild.get_role(role_id)
                bot_member = guild.me
                if role is None or (
                    role not in member.roles
                    and (
                        bot_member is None
                        or not bot_member.guild_permissions.manage_roles
                        or role.position >= bot_member.top_role.position
                    )
                ):
                    return await self._reply(
                        target,
                        "Wylosowana rola nie jest już dostępna albo bot nie może jej "
                        "przyznać. Administrator musi poprawić ustawienia skrzynki.",
                        ephemeral=True,
                    )
                roles_to_add[role.id] = role
            added_roles = []

            if isinstance(target, discord.Interaction):
                await target.response.defer(thinking=True)
                message = await target.followup.send(
                    f"🎁 Otwieranie skrzynki **{box[3]}**…",
                    wait=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                message = await target.reply(
                    f"🎁 Otwieranie skrzynki **{box[3]}**…",
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            for _ in range(8):
                preview = random.choice(selected_rewards)[2]
                await message.edit(
                    content=f"🎰 Losowanie… **{preview}**",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                await asyncio.sleep(0.45)

            try:
                for role in roles_to_add.values():
                    if role not in member.roles:
                        await member.add_roles(
                            role, reason=f"Nagroda ze skrzynki {box[3]}"
                        )
                        added_roles.append(role)

                conn = connect()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    owned = conn.execute(
                        "SELECT quantity FROM economy_inventory "
                        "WHERE guild_id = ? AND user_id = ? AND product_key = ?",
                        (guild.id, member.id, box_key),
                    ).fetchone()
                    if owned is None or owned[0] < quantity:
                        raise ValueError("Nie masz już tylu skrzynek w ekwipunku.")
                    conn.execute(
                        "UPDATE economy_inventory SET quantity = quantity - ? "
                        "WHERE guild_id = ? AND user_id = ? AND product_key = ?",
                        (quantity, guild.id, member.id, box_key),
                    )
                    money_rewards = [row for row in selected_rewards if row[0] == "money"]
                    item_rewards = [row for row in selected_rewards if row[0] == "item"]
                    role_rewards = [row for row in selected_rewards if row[0] == "role"]
                    receipts = []

                    if money_rewards:
                        effects = active_effects(conn, guild.id, member)
                        payout = sum(
                            apply_income_bonus(row[3], effects)
                            for row in money_rewards
                        )
                        conn.execute(
                            "UPDATE economy_accounts SET wallet = wallet + ? "
                            "WHERE guild_id = ? AND user_id = ?",
                            (payout, guild.id, member.id),
                        )
                        cash_receipt = (
                            f"Pieniądze: **{format_money(payout)}$** trafiło do portfela"
                        )
                        if effects["income_bonus"]:
                            cash_receipt += f" (premia +{effects['income_bonus']}%)"
                        receipts.append(cash_receipt + ".")

                    item_counts = {}
                    for reward in item_rewards:
                        item_counts[reward[1]] = item_counts.get(reward[1], [reward[2], 0])
                        item_counts[reward[1]][1] += 1
                    for reward_key, (reward_name, count) in item_counts.items():
                        conn.execute(
                            "INSERT INTO economy_inventory "
                            "(guild_id, user_id, product_key, item_name, quantity) "
                            "VALUES (?, ?, ?, ?, ?) "
                            "ON CONFLICT(guild_id, user_id, product_key) "
                            "DO UPDATE SET quantity = quantity + excluded.quantity",
                            (guild.id, member.id, reward_key, reward_name, count),
                        )
                        receipts.append(f"**{reward_name}** × {count} dodano do ekwipunku.")
                    for role in roles_to_add.values():
                        role_count = sum(
                            1 for reward in role_rewards
                            if int(reward[1].split(":", 1)[1]) == role.id
                        )
                        if role_count:
                            receipts.append(f"Rola **{role.name}** × {role_count}.")

                    if len(receipts) > 15:
                        omitted = len(receipts) - 15
                        receipts = receipts[:15] + [
                            f"… oraz {omitted} innych pozycji nagród."
                        ]
                    receipt = "\n".join(receipts) or "Nie wypadła żadna nagroda."
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
            except Exception as error:
                for role in added_roles:
                    try:
                        await member.remove_roles(
                            role, reason="Cofnięcie nieudanego otwarcia skrzynki"
                        )
                    except discord.HTTPException:
                        pass
                await message.edit(
                    content=f"❌ Nie udało się otworzyć skrzynki: {error}",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            await message.edit(
                content=f"🎉 **{member.display_name}** otwiera **{quantity}× {box[3]}**!\n"
                f"Nagrody:\n{receipt}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        finally:
            self.box_lock.release()

    @app_commands.command(name="roullete", description="Postaw pieniądze na numer od 1 do 30.")
    @app_commands.describe(number="Numer, na który stawiasz", amount="Kwota zakładu z portfela")
    async def roulette_slash(
        self,
        interaction: discord.Interaction,
        number: app_commands.Range[int, 1, 30],
        amount: app_commands.Range[int, 1],
    ):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                "Ruletka działa tylko na serwerze.", ephemeral=True
            )
        await self._play_roulette(
            interaction.guild_id, interaction.user, interaction, number, amount
        )

    async def roulette_online_slash(
        self,
        interaction: discord.Interaction,
        number: app_commands.Range[int, 1, 30],
        amount: app_commands.Range[int, 1],
    ):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                "Ruletka online działa tylko na serwerze.", ephemeral=True
            )
        await self._start_online_roulette(
            interaction.guild,
            interaction.channel,
            interaction.user,
            interaction,
            number,
            amount,
        )

    @commands.group(name="roulette", invoke_without_command=True)
    @commands.guild_only()
    async def roulette_group(
        self,
        ctx: commands.Context,
        number: int | None = None,
        amount: int | None = None,
    ):
        if number is None or amount is None:
            return await ctx.reply(
                "Użyj `!roulette <numer 1–30> <stawka>` albo "
                "`!roulette online <numer 1–30> <stawka>`. ",
                mention_author=False,
            )
        await self._play_roulette(ctx.guild.id, ctx.author, ctx, number, amount)

    @roulette_group.command(name="online")
    @commands.guild_only()
    async def roulette_online_prefix(
        self,
        ctx: commands.Context,
        number: int,
        amount: int,
    ):
        await self._start_online_roulette(
            ctx.guild,
            ctx.channel,
            ctx.author,
            ctx,
            number,
            amount,
        )

    @commands.group(name="join", invoke_without_command=True)
    @commands.guild_only()
    async def join_group(self, ctx: commands.Context):
        await ctx.reply(
            "Użyj `!join roullete <twórca> <numer> <stawka>`, aby dołączyć.",
            mention_author=False,
        )

    @join_group.command(name="roullete")
    @commands.guild_only()
    async def join_roulette_prefix(
        self,
        ctx: commands.Context,
        creator: discord.Member,
        number: int,
        amount: int,
    ):
        await self._join_online_roulette(
            ctx.guild, creator, ctx.author, ctx, number, amount
        )

    @commands.command(name="roullete")
    @commands.guild_only()
    async def roulette_prefix(
        self,
        ctx: commands.Context,
        number: int,
        amount: int,
    ):
        await self._play_roulette(ctx.guild.id, ctx.author, ctx, number, amount)

    @app_commands.describe(
        creator="Gracz, który rozpoczął ruletkę",
        number="Wolny numer od 1 do 30",
        amount="Kwota stawki z portfela",
    )
    async def join_roulette_slash(
        self,
        interaction: discord.Interaction,
        creator: discord.Member,
        number: app_commands.Range[int, 1, 30],
        amount: app_commands.Range[int, 1],
    ):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                "Dołączenie działa tylko na serwerze.", ephemeral=True
            )
        await self._join_online_roulette(
            interaction.guild, creator, interaction.user, interaction, number, amount
        )

    async def _start_online_roulette(
        self,
        guild: discord.Guild,
        channel,
        creator: discord.Member,
        target,
        number: int,
        amount: int,
    ):
        if channel is None or not hasattr(channel, "id"):
            return await self._reply(target, "Nie udało się znaleźć tego kanału.", ephemeral=True)
        if not 1 <= number <= 30 or amount < 1:
            return await self._reply(
                target,
                "Podaj numer od **1 do 30** i stawkę większą od zera.",
                ephemeral=True,
            )

        ensure_account(guild.id, creator.id)
        now = datetime.now(timezone.utc)
        ends_at = now + timedelta(minutes=1)
        async with self.online_roulette_lock:
            conn = connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                settings = conn.execute(
                    "SELECT roulette_chance FROM economy_settings WHERE guild_id = ?",
                    (guild.id,),
                ).fetchone()
                if settings is None or settings[0] is None:
                    conn.rollback()
                    return await self._reply(
                        target,
                        "Administrator nie ustawił jeszcze szansy ruletki. "
                        "Użyj `/create roullete`.",
                        ephemeral=True,
                    )
                active = conn.execute(
                    "SELECT 1 FROM economy_online_roulettes "
                    "WHERE guild_id = ? AND status = 'waiting'",
                    (guild.id,),
                ).fetchone()
                if active:
                    conn.rollback()
                    return await self._reply(
                        target,
                        "Na tym serwerze trwa już ruletka online. Dołącz do niej albo "
                        "poczekaj, aż się zakończy.",
                        ephemeral=True,
                    )
                wallet = conn.execute(
                    "SELECT wallet FROM economy_accounts WHERE guild_id = ? AND user_id = ?",
                    (guild.id, creator.id),
                ).fetchone()[0]
                if wallet < amount:
                    conn.rollback()
                    return await self._reply(
                        target,
                        f"Masz **{format_money(wallet)}$**, a stawiasz "
                        f"**{format_money(amount)}$**.",
                        ephemeral=True,
                    )
                cursor = conn.execute(
                    "INSERT INTO economy_online_roulettes "
                    "(guild_id, channel_id, creator_id, chosen_number, stake, ends_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        guild.id,
                        channel.id,
                        creator.id,
                        number,
                        amount,
                        ends_at.isoformat(),
                        now.isoformat(),
                    ),
                )
                game_id = cursor.lastrowid
                conn.execute(
                    "INSERT INTO economy_online_roulette_bets "
                    "(game_id, user_id, chosen_number, stake) VALUES (?, ?, ?, ?)",
                    (game_id, creator.id, number, amount),
                )
                conn.execute(
                    "UPDATE economy_accounts SET wallet = wallet - ? "
                    "WHERE guild_id = ? AND user_id = ?",
                    (amount, guild.id, creator.id),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()
                return await self._reply(
                    target,
                    "Nie udało się utworzyć ruletki. Sprawdź, czy na serwerze nie ma już aktywnej gry.",
                    ephemeral=True,
                )
            finally:
                conn.close()

            try:
                content = await self._online_lobby_text(game_id)
                if isinstance(target, discord.Interaction):
                    await target.response.send_message(
                        content,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    message = await target.original_response()
                else:
                    message = await target.reply(
                        content,
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                with connect() as conn:
                    conn.execute(
                        "UPDATE economy_online_roulettes SET message_id = ? WHERE game_id = ?",
                        (message.id, game_id),
                    )
            except Exception as error:
                print(f"Nie udało się opublikować ruletki online: {error!r}")
                with connect() as conn:
                    conn.execute(
                        "UPDATE economy_online_roulettes SET status = 'cancelled' "
                        "WHERE game_id = ? AND status = 'waiting'",
                        (game_id,),
                    )
                    conn.execute(
                        "UPDATE economy_accounts SET wallet = wallet + ? "
                        "WHERE guild_id = ? AND user_id = ?",
                        (amount, guild.id, creator.id),
                    )

    async def _join_online_roulette(
        self,
        guild: discord.Guild,
        creator: discord.Member,
        player: discord.Member,
        target,
        number: int,
        amount: int,
    ):
        if not 1 <= number <= 30 or amount < 1:
            return await self._reply(
                target,
                "Podaj numer od **1 do 30** i stawkę większą od zera.",
                ephemeral=True,
            )
        if creator.guild.id != guild.id or player.guild.id != guild.id:
            return await self._reply(target, "Wybierz graczy z tego serwera.", ephemeral=True)
        ensure_account(guild.id, player.id)
        now = datetime.now(timezone.utc)

        async with self.online_roulette_lock:
            conn = connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                game = conn.execute(
                    "SELECT game_id, ends_at FROM economy_online_roulettes "
                    "WHERE guild_id = ? AND creator_id = ? AND status = 'waiting'",
                    (guild.id, creator.id),
                ).fetchone()
                if game is None or datetime.fromisoformat(game[1]) <= now:
                    conn.rollback()
                    return await self._reply(
                        target,
                        "Nie ma już otwartej ruletki tego gracza. Dołączanie trwa tylko minutę.",
                        ephemeral=True,
                    )
                game_id = game[0]
                if conn.execute(
                    "SELECT 1 FROM economy_online_roulette_bets "
                    "WHERE game_id = ? AND user_id = ?",
                    (game_id, player.id),
                ).fetchone():
                    conn.rollback()
                    return await self._reply(
                        target, "Już bierzesz udział w tej ruletce.", ephemeral=True
                    )
                occupied = conn.execute(
                    "SELECT user_id FROM economy_online_roulette_bets "
                    "WHERE game_id = ? AND chosen_number = ?",
                    (game_id, number),
                ).fetchone()
                if occupied:
                    conn.rollback()
                    return await self._reply(
                        target,
                        "Ten numer jest już obstawiony. Wybierz inny numer od 1 do 30.",
                        ephemeral=True,
                    )
                wallet = conn.execute(
                    "SELECT wallet FROM economy_accounts WHERE guild_id = ? AND user_id = ?",
                    (guild.id, player.id),
                ).fetchone()[0]
                if wallet < amount:
                    conn.rollback()
                    return await self._reply(
                        target,
                        f"Masz **{format_money(wallet)}$**, a stawiasz "
                        f"**{format_money(amount)}$**.",
                        ephemeral=True,
                    )
                conn.execute(
                    "INSERT INTO economy_online_roulette_bets "
                    "(game_id, user_id, chosen_number, stake) VALUES (?, ?, ?, ?)",
                    (game_id, player.id, number, amount),
                )
                conn.execute(
                    "UPDATE economy_accounts SET wallet = wallet - ? "
                    "WHERE guild_id = ? AND user_id = ?",
                    (amount, guild.id, player.id),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()
                return await self._reply(
                    target,
                    "Ten numer został już obstawiony. Spróbuj z innym.",
                    ephemeral=True,
                )
            finally:
                conn.close()

        await self._reply(
            target,
            f"✅ Dołączono do ruletki **{creator.display_name}**. "
            f"Twój numer: **{number}**, stawka: **{format_money(amount)}$**.",
            ephemeral=isinstance(target, discord.Interaction),
        )
        await self._refresh_online_roulette_message(game_id)

    async def _online_lobby_text(self, game_id: int) -> str:
        with connect() as conn:
            game = conn.execute(
                "SELECT guild_id, creator_id, chosen_number, stake, ends_at "
                "FROM economy_online_roulettes WHERE game_id = ?",
                (game_id,),
            ).fetchone()
            bets = conn.execute(
                "SELECT user_id, chosen_number, stake FROM economy_online_roulette_bets "
                "WHERE game_id = ? ORDER BY chosen_number",
                (game_id,),
            ).fetchall()
        if game is None:
            return "Ruletka online nie jest już dostępna."
        guild_id, creator_id, _number, _stake, ends_at = game
        guild = self.bot.get_guild(guild_id)
        lines = []
        for user_id, number, stake in bets:
            member = guild.get_member(user_id) if guild else None
            display_name = member.display_name if member else f"Gracz {user_id}"
            lines.append(
                f"• **{display_name}** — numer **{number}**, "
                f"stawka **{format_money(stake)}$**"
            )
        pot = sum(row[2] for row in bets)
        end_time = datetime.fromisoformat(ends_at)
        remaining = max(0, int((end_time - datetime.now(timezone.utc)).total_seconds()))
        creator = guild.get_member(creator_id) if guild else None
        creator_name = creator.display_name if creator else f"Gracz {creator_id}"
        return (
            f"🎰 **Ruletka online — {creator_name}**\n"
            f"Dołączanie kończy się za **{remaining} s**.\n"
            "Każdy wybiera inny numer. Zwycięzca zgarnia całą pulę; jeśli nikt nie trafi, "
            "pula przepada.\n\n"
            + "\n".join(lines)
            + f"\n\n💰 **Pula: {format_money(pot)}$**"
        )

    async def _refresh_online_roulette_message(self, game_id: int):
        with connect() as conn:
            details = conn.execute(
                "SELECT channel_id, message_id, status, result_text "
                "FROM economy_online_roulettes WHERE game_id = ?",
                (game_id,),
            ).fetchone()
        if not details or details[1] is None:
            return
        channel = self.bot.get_channel(details[0])
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(details[0])
            except discord.HTTPException:
                return
        try:
            message = await channel.fetch_message(details[1])
            content = (
                details[3]
                if details[2] == "finished" and details[3]
                else await self._online_lobby_text(game_id)
            )
            await message.edit(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            return

    async def _online_roulette_watcher(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                now = datetime.now(timezone.utc).isoformat()
                with connect() as conn:
                    due_games = conn.execute(
                        "SELECT game_id FROM economy_online_roulettes "
                        "WHERE status = 'waiting' AND ends_at <= ?",
                        (now,),
                    ).fetchall()
                    unannounced = conn.execute(
                        "SELECT game_id FROM economy_online_roulettes "
                        "WHERE status = 'finished' AND announced = 0"
                    ).fetchall()
                for (game_id,) in due_games:
                    await self._finish_online_roulette(game_id)
                for (game_id,) in unannounced:
                    await self._publish_online_roulette_result(game_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"Błąd obsługi ruletki online: {error!r}")
            await asyncio.sleep(2)

    async def _finish_online_roulette(self, game_id: int):
        async with self.online_roulette_lock:
            conn = connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                game = conn.execute(
                    "SELECT guild_id, creator_id, ends_at, status "
                    "FROM economy_online_roulettes WHERE game_id = ?",
                    (game_id,),
                ).fetchone()
                if (
                    game is None
                    or game[3] != "waiting"
                    or datetime.fromisoformat(game[2]) > datetime.now(timezone.utc)
                ):
                    conn.rollback()
                    return
                guild_id, creator_id = game[0], game[1]
                bets = conn.execute(
                    "SELECT user_id, chosen_number, stake "
                    "FROM economy_online_roulette_bets WHERE game_id = ?",
                    (game_id,),
                ).fetchall()
                settings = conn.execute(
                    "SELECT roulette_chance FROM economy_settings WHERE guild_id = ?",
                    (guild_id,),
                ).fetchone()
                chance = settings[0] if settings and settings[0] is not None else 0
                pot = sum(row[2] for row in bets)
                guild = self.bot.get_guild(guild_id)
                result_number = None
                winner_id = None

                if len(bets) == 1:
                    user_id, chosen_number, stake = bets[0]
                    if random.random() * 100 < chance:
                        result_number = chosen_number
                        winner_id = user_id
                        payout = stake * 2
                        member = guild.get_member(user_id) if guild else None
                        if member is not None:
                            payout = apply_income_bonus(
                                payout, active_effects(conn, guild_id, member)
                            )
                        conn.execute(
                            "UPDATE economy_accounts SET wallet = wallet + ? "
                            "WHERE guild_id = ? AND user_id = ?",
                            (payout, guild_id, user_id),
                        )
                        result_text = (
                            f"🎉 Nie dołączył nikt, więc **{member.display_name if member else f'gracz {user_id}'}** "
                            f"zagrał sam. Wypadło **{result_number}** i wygrał "
                            f"**{format_money(payout)}$** (2× stawki)."
                        )
                    else:
                        result_number = random.choice(
                            [value for value in range(1, 31) if value != chosen_number]
                        )
                        result_text = (
                            f"🎲 Nie dołączył nikt, więc twórca zagrał zwykłą ruletkę. "
                            f"Wypadło **{result_number}** — stawka **{format_money(stake)}$** przepada."
                        )
                else:
                    if bets and random.random() * 100 < chance:
                        winner = random.choice(bets)
                        winner_id, result_number = winner[0], winner[1]
                        conn.execute(
                            "UPDATE economy_accounts SET wallet = wallet + ? "
                            "WHERE guild_id = ? AND user_id = ?",
                            (pot, guild_id, winner_id),
                        )
                        winner_member = guild.get_member(winner_id) if guild else None
                        winner_name = winner_member.display_name if winner_member else f"gracz {winner_id}"
                        result_text = (
                            f"🎉 Wypadł numer **{result_number}**! **{winner_name}** zgarnia "
                            f"całą pulę: **{format_money(pot)}$**."
                        )
                    else:
                        chosen_numbers = {row[1] for row in bets}
                        remaining_numbers = [
                            value for value in range(1, 31) if value not in chosen_numbers
                        ]
                        result_number = random.choice(remaining_numbers) if remaining_numbers else None
                        roll_text = (
                            f"Wypadł numer **{result_number}**"
                            if result_number is not None
                            else "Żaden obstawiony numer nie wygrał"
                        )
                        result_text = (
                            f"🎲 {roll_text}. Nikt nie trafił — pula **{format_money(pot)}$** przepada."
                        )

                conn.execute(
                    "UPDATE economy_online_roulettes SET status = 'finished', "
                    "result_text = ?, announced = 0 WHERE game_id = ?",
                    (result_text, game_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        await self._animate_online_roulette(game_id)
        await self._publish_online_roulette_result(game_id)

    async def _animate_online_roulette(self, game_id: int):
        with connect() as conn:
            details = conn.execute(
                "SELECT channel_id, message_id FROM economy_online_roulettes "
                "WHERE game_id = ?",
                (game_id,),
            ).fetchone()
        if not details or details[1] is None:
            return
        channel = self.bot.get_channel(details[0])
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(details[0])
            except discord.HTTPException:
                return
        try:
            message = await channel.fetch_message(details[1])
            for _ in range(8):
                await message.edit(
                    content=f"🎰 Ruletka online losuje… **{random.randint(1, 30)}**",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                await asyncio.sleep(0.3)
        except discord.HTTPException:
            return

    async def _publish_online_roulette_result(self, game_id: int):
        await self._refresh_online_roulette_message(game_id)
        with connect() as conn:
            conn.execute(
                "UPDATE economy_online_roulettes SET announced = 1 "
                "WHERE game_id = ? AND status = 'finished'",
                (game_id,),
            )

    async def _play_roulette(self, guild_id: int, user, target, number: int, amount: int):
        if not 1 <= number <= 30 or amount < 1:
            return await self._reply(
                target,
                "Podaj numer od **1 do 30** i stawkę większą od zera.",
                ephemeral=True,
            )
        ensure_account(guild_id, user.id)

        async with self.roulette_lock:
            with connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                settings = conn.execute(
                    "SELECT roulette_chance FROM economy_settings WHERE guild_id = ?",
                    (guild_id,),
                ).fetchone()
                if settings is None or settings[0] is None:
                    conn.rollback()
                    return await self._reply(
                        target,
                        "Administrator nie ustawił jeszcze szansy ruletki. "
                        "Użyj `/create roullete`.",
                        ephemeral=True,
                    )
                account = conn.execute(
                    "SELECT wallet FROM economy_accounts WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user.id),
                ).fetchone()
                wallet = account[0] if account else 0
                if wallet < amount:
                    conn.rollback()
                    return await self._reply(
                        target,
                        f"Masz **{format_money(wallet)}$** w portfelu, "
                        f"a stawiasz **{format_money(amount)}$**.",
                        ephemeral=True,
                    )

                chance = settings[0]
                won = random.random() * 100 < chance
                result_number = number if won else random.choice(
                    [candidate for candidate in range(1, 31) if candidate != number]
                )
                payout = 0
                bonus = 0
                if won:
                    base_payout = amount * 2
                    if isinstance(user, discord.Member):
                        effects = active_effects(conn, guild_id, user)
                        payout = apply_income_bonus(base_payout, effects)
                        bonus = effects["income_bonus"]
                    else:
                        payout = base_payout
                conn.execute(
                    "UPDATE economy_accounts SET wallet = wallet - ? "
                    "WHERE guild_id = ? AND user_id = ?",
                    (amount, guild_id, user.id),
                )
                if payout:
                    conn.execute(
                        "UPDATE economy_accounts SET wallet = wallet + ? "
                        "WHERE guild_id = ? AND user_id = ?",
                        (payout, guild_id, user.id),
                    )
                conn.commit()

            if isinstance(target, discord.Interaction):
                await target.response.defer(thinking=True)
                message = await target.followup.send(
                    "🎰 Ruletka kręci się…", wait=True
                )
            else:
                message = await target.reply("🎰 Ruletka kręci się…", mention_author=False)

            for _ in range(8):
                await message.edit(content=f"🎰 Ruletka kręci się… **{random.randint(1, 30)}**")
                await asyncio.sleep(0.3)

            if won:
                result = (
                    f"🎉 Wypadło **{result_number}** — wygrywasz! Wypłata: "
                    f"**{format_money(payout)}$** (2× stawki)"
                    + (f" z premią +{bonus}%" if bonus else "")
                    + "."
                )
            else:
                result = (
                    f"🎲 Wypadło **{result_number}**. Nie tym razem — "
                    f"tracisz stawkę **{format_money(amount)}$**."
                )
            await message.edit(content=result)

    @app_commands.command(name="shop", description="Zobacz sklep serwerowy, gracza lub firmy.")
    @app_commands.describe(
        member="Opcjonalnie: gracz, którego prywatny sklep chcesz zobaczyć",
        firm="Opcjonalnie: nazwa firmy, której sklep chcesz zobaczyć",
    )
    async def shop_slash(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        firm: str | None = None,
    ):
        if interaction.guild is None:
            return await interaction.response.send_message(
                "Sklepy działają tylko na serwerze.", ephemeral=True
            )
        if firm is not None:
            if member is not None:
                return await interaction.response.send_message(
                    "Podaj albo gracza, albo nazwę firmy — nie oba naraz.", ephemeral=True
                )
            firm_cog = self.bot.get_cog("Firmy")
            if firm_cog is None:
                return await interaction.response.send_message(
                    "Moduł firm jest obecnie niedostępny.", ephemeral=True
                )
            return await firm_cog.make_shop(interaction, firm)
        if member is None:
            return await self._show_server_shop(interaction, interaction.guild)
        await self._show_shop(interaction, interaction.guild, member)

    @commands.command(name="shop")
    @commands.guild_only()
    async def shop_prefix(
        self,
        ctx: commands.Context,
        *,
        target: str | None = None,
    ):
        if target is None:
            return await self._show_server_shop(ctx, ctx.guild)
        try:
            member = await commands.MemberConverter().convert(ctx, target)
        except commands.BadArgument:
            member = None
        if member is not None:
            return await self._show_shop(ctx, ctx.guild, member)
        firm_cog = self.bot.get_cog("Firmy")
        if firm_cog is None:
            return await ctx.reply("Moduł firm jest obecnie niedostępny.", mention_author=False)
        await firm_cog._prefix_call(ctx, firm_cog.make_shop, target)

    async def _show_shop(self, target, guild: discord.Guild, seller: discord.Member):
        with connect() as conn:
            rows = conn.execute(
                "SELECT listing_id, role_id, price FROM economy_shop_listings "
                "WHERE guild_id = ? AND seller_id = ? ORDER BY created_at, listing_id",
                (guild.id, seller.id),
            ).fetchall()

        listings = []
        for listing_id, role_id, price in rows:
            role = guild.get_role(role_id)
            if role is None:
                continue
            listings.append({
                "listing_id": listing_id,
                "role_id": role_id,
                "role_name": role.name,
                "role_mention": role.mention,
                "price": price,
            })

        if not listings:
            return await self._reply(
                target,
                f"🛒 Sklep gracza **{seller.display_name}** jest pusty.",
                ephemeral=True,
            )

        view = ShopView(self, guild, seller, listings)
        if isinstance(target, discord.Interaction):
            await target.response.send_message(embed=view.make_embed(), view=view)
        else:
            await target.reply(embed=view.make_embed(), view=view, mention_author=False)

    async def _show_server_shop(self, target, guild: discord.Guild):
        with connect() as conn:
            rows = conn.execute(
                "SELECT listing_id, product_key, product_type, product_name, role_id, "
                "price, description FROM economy_server_shop "
                "WHERE guild_id = ? "
                "ORDER BY CASE WHEN product_key LIKE 'box:%' THEN 1 ELSE 0 END, "
                "product_name COLLATE NOCASE, listing_id",
                (guild.id,),
            ).fetchall()
            effect_rows = conn.execute(
                "SELECT product_key, effect_type, effect_value "
                "FROM economy_item_effects WHERE guild_id = ?",
                (guild.id,),
            ).fetchall()

        effects_by_product = {}
        for product_key, effect_type, value in effect_rows:
            if effect_type == "rob_defense":
                label = f"Ochrona przed kradzieżą: -{value} pp"
            elif effect_type == "rob_bonus":
                label = f"Szansa kradzieży: +{value} pp"
            elif effect_type == "income_bonus":
                label = f"Zarobki: +{value}%"
            elif effect_type == "shop_discount":
                label = f"Zniżka: {value}%"
            elif effect_type == "company_discount":
                label = f"Zniżka w firmach: {value}%"
            elif effect_type == "company_income_bonus":
                label = f"Zarobki z firm: +{value}%"
            else:
                continue
            effects_by_product.setdefault(product_key, []).append(label)

        listings = []
        for listing_id, product_key, product_type, product_name, role_id, price, description in rows:
            if product_type == "role" and guild.get_role(role_id) is None:
                continue
            product_description = description or "Brak opisu."
            product_effects = effects_by_product.get(product_key, [])
            if product_effects:
                product_description += "\n✨ Efekt: " + ", ".join(product_effects)
            listings.append({
                "listing_id": listing_id,
                "product_key": product_key,
                "product_type": product_type,
                "product_name": product_name,
                "role_id": role_id,
                "price": price,
                "description": product_description,
            })

        if not listings:
            return await self._reply(
                target,
                "🛒 Sklep serwerowy jest pusty. Administrator może dodać produkty "
                "komendą `/create shop`.",
                ephemeral=True,
            )

        view = ServerShopView(self, guild, listings)
        if isinstance(target, discord.Interaction):
            await target.response.send_message(embed=view.make_embed(), view=view)
        else:
            await target.reply(embed=view.make_embed(), view=view, mention_author=False)

    @app_commands.command(name="sell", description="Wystaw posiadaną rolę w swoim sklepie.")
    @app_commands.describe(role="Rola, którą posiadasz", price="Cena roli")
    async def sell_slash(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        price: app_commands.Range[int, 1],
    ):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                "Tej komendy można użyć tylko na serwerze.", ephemeral=True
            )
        await self._sell_role(interaction, interaction.guild, interaction.user, role, price)

    @commands.command(name="sell")
    @commands.guild_only()
    async def sell_prefix(
        self,
        ctx: commands.Context,
        role: discord.Role,
        price: int,
    ):
        await self._sell_role(ctx, ctx.guild, ctx.author, role, price)

    async def _sell_role(self, target, guild, seller, role, price: int):
        if price <= 0:
            return await self._reply(target, "Cena musi być większa od zera.", ephemeral=True)
        if role.guild.id != guild.id or role == guild.default_role or role.managed:
            return await self._reply(target, "Nie można wystawić tej roli.", ephemeral=True)
        if role not in seller.roles:
            return await self._reply(
                target,
                "Możesz wystawić tylko rolę, którą posiadasz.",
                ephemeral=True,
            )

        bot_member = guild.me
        if (
            bot_member is None
            or not bot_member.guild_permissions.manage_roles
            or role.position >= bot_member.top_role.position
        ):
            return await self._reply(
                target,
                "Bot potrzebuje uprawnienia **Zarządzanie rolami**, a jego najwyższa "
                "rola musi być wyżej od wystawianej roli.",
                ephemeral=True,
            )

        now = datetime.now(timezone.utc).isoformat()
        with connect() as conn:
            active_auction = conn.execute(
                "SELECT 1 FROM economy_auctions "
                "WHERE guild_id = ? AND role_id = ? AND status = 'active'",
                (guild.id, role.id),
            ).fetchone()
            if active_auction is not None:
                return await self._reply(
                    target,
                    "Ta rola jest już wystawiona na aktywnej aukcji.",
                    ephemeral=True,
                )
            conn.execute(
                "INSERT INTO economy_shop_listings "
                "(guild_id, seller_id, role_id, price, created_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, seller_id, role_id) DO UPDATE SET "
                "price = excluded.price, created_at = excluded.created_at",
                (guild.id, seller.id, role.id, price, now),
            )

        await self._reply(
            target,
            f"✅ Wystawiono {role.mention} w Twoim sklepie za **{format_money(price)}$**.",
        )

    async def create_auction_slash(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        starting_price: app_commands.Range[int, 1],
    ):
        """Parametry: role — sprzedawana rola; starting_price — cena początkowa."""
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                "Aukcje działają tylko na serwerze.", ephemeral=True
            )
        await self._create_auction(
            interaction,
            interaction.guild,
            interaction.user,
            role,
            starting_price,
            interaction.channel_id,
        )

    @commands.command(name="auction", aliases=["createauction"])
    @commands.guild_only()
    async def create_auction_prefix(
        self,
        ctx: commands.Context,
        role: discord.Role,
        starting_price: int,
    ):
        await self._create_auction(
            ctx,
            ctx.guild,
            ctx.author,
            role,
            starting_price,
            ctx.channel.id,
        )

    async def _create_auction(
        self,
        target,
        guild: discord.Guild,
        creator: discord.Member,
        role: discord.Role,
        starting_price: int,
        channel_id: int,
    ):
        if starting_price <= 0:
            return await self._reply(
                target, "Cena początkowa musi być większa od zera.", ephemeral=True
            )
        if role.guild.id != guild.id or role == guild.default_role or role.managed:
            return await self._reply(target, "Nie można wystawić tej roli.", ephemeral=True)
        if role not in creator.roles:
            return await self._reply(
                target,
                "Możesz wystawić tylko rolę, którą posiadasz.",
                ephemeral=True,
            )

        bot_member = guild.me
        if (
            bot_member is None
            or not bot_member.guild_permissions.manage_roles
            or role.position >= bot_member.top_role.position
        ):
            return await self._reply(
                target,
                "Bot potrzebuje uprawnienia **Zarządzanie rolami**, a jego najwyższa "
                "rola musi być wyżej od roli wystawianej na aukcji.",
                ephemeral=True,
            )

        now = datetime.now(timezone.utc)
        ends_at = now + timedelta(minutes=1)
        ensure_account(guild.id, creator.id)

        try:
            with connect() as conn:
                active = conn.execute(
                    "SELECT auction_id FROM economy_auctions "
                    "WHERE guild_id = ? AND status = 'active'",
                    (guild.id,),
                ).fetchone()
                if active is not None:
                    return await self._reply(
                        target,
                        "Na tym serwerze trwa już aukcja. Poczekaj na jej zakończenie.",
                        ephemeral=True,
                    )

                conn.execute(
                    "INSERT INTO economy_auctions "
                    "(guild_id, channel_id, creator_id, role_id, starting_price, "
                    "current_price, highest_bidder_id, ends_at, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 'active', ?)",
                    (
                        guild.id,
                        channel_id,
                        creator.id,
                        role.id,
                        starting_price,
                        starting_price,
                        ends_at.isoformat(),
                        now.isoformat(),
                    ),
                )
                # A role cannot be listed for sale and auction at the same time.
                conn.execute(
                    "DELETE FROM economy_shop_listings "
                    "WHERE guild_id = ? AND role_id = ?",
                    (guild.id, role.id),
                )
        except sqlite3.IntegrityError:
            return await self._reply(
                target,
                "Na tym serwerze trwa już aukcja. Poczekaj na jej zakończenie.",
                ephemeral=True,
            )

        end_timestamp = int(ends_at.timestamp())
        await self._reply(
            target,
            f"🔨 **Rozpoczęła się aukcja roli {role.mention}!**\n"
            f"Twórca: {creator.mention}\n"
            f"Cena początkowa: **{format_money(starting_price)}$**\n"
            f"Cena aktualna: **{format_money(starting_price)}$**\n"
            "Licytuj komendą `/bump` podając nową łączną kwotę. "
            f"Koniec aukcji: <t:{end_timestamp}:R>. Każde podbicie przedłuża aukcję o minutę.",
        )

    @app_commands.command(name="bump", description="Podbij aktualną aukcję nową ofertą.")
    @app_commands.describe(amount="Nowa łączna kwota oferty")
    async def bump_slash(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1],
    ):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                "Licytować można tylko na serwerze.", ephemeral=True
            )
        await self._place_bid(interaction, interaction.guild, interaction.user, amount)

    @commands.command(name="bump")
    @commands.guild_only()
    async def bump_prefix(self, ctx: commands.Context, amount: int):
        await self._place_bid(ctx, ctx.guild, ctx.author, amount)

    async def _place_bid(self, target, guild, bidder, amount: int):
        if amount <= 0:
            return await self._reply(target, "Oferta musi być większa od zera.", ephemeral=True)

        ensure_account(guild.id, bidder.id)
        failure = None
        end_time = None
        previous_bidder_id = None
        starting_price = None
        role_id = None
        async with self.auction_lock:
            conn = connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                auction = conn.execute(
                    "SELECT auction_id, creator_id, role_id, starting_price, current_price, "
                    "highest_bidder_id, ends_at FROM economy_auctions "
                    "WHERE guild_id = ? AND status = 'active'",
                    (guild.id,),
                ).fetchone()

                if auction is None:
                    failure = "Na tym serwerze nie ma teraz aktywnej aukcji."
                else:
                    (
                        auction_id,
                        creator_id,
                        role_id,
                        starting_price,
                        current_price,
                        previous_bidder_id,
                        ends_at,
                    ) = auction
                    now = datetime.now(timezone.utc)

                    if now >= datetime.fromisoformat(ends_at):
                        failure = "Czas aukcji minął. Poczekaj na jej rozstrzygnięcie."
                    elif bidder.id == creator_id:
                        failure = "Nie możesz licytować własnej aukcji."
                    elif previous_bidder_id == bidder.id and amount <= current_price:
                        failure = "Twoja nowa oferta musi być wyższa od obecnej."
                    elif (
                        previous_bidder_id is None and amount < starting_price
                    ) or (
                        previous_bidder_id is not None
                        and previous_bidder_id != bidder.id
                        and amount <= current_price
                    ):
                        minimum = starting_price if previous_bidder_id is None else current_price + 1
                        failure = f"Nowa oferta musi wynosić co najmniej **{format_money(minimum)}$**."
                    else:
                        previous_price = current_price if previous_bidder_id is not None else 0
                        charge = amount - current_price if previous_bidder_id == bidder.id else amount
                        wallet_row = conn.execute(
                            "SELECT wallet FROM economy_accounts "
                            "WHERE guild_id = ? AND user_id = ?",
                            (guild.id, bidder.id),
                        ).fetchone()
                        wallet = wallet_row[0] if wallet_row else 0

                        if wallet < charge:
                            failure = (
                                f"Masz **{format_money(wallet)}$** w portfelu, "
                                f"a do złożenia tej oferty potrzebujesz **{format_money(charge)}$**."
                            )
                        else:
                            debit = conn.execute(
                                "UPDATE economy_accounts SET wallet = wallet - ? "
                                "WHERE guild_id = ? AND user_id = ? AND wallet >= ?",
                                (charge, guild.id, bidder.id, charge),
                            )
                            if debit.rowcount != 1:
                                failure = "Saldo zmieniło się podczas licytacji. Spróbuj ponownie."
                            else:
                                if previous_bidder_id is not None and previous_bidder_id != bidder.id:
                                    conn.execute(
                                        "UPDATE economy_accounts SET wallet = wallet + ? "
                                        "WHERE guild_id = ? AND user_id = ?",
                                        (previous_price, guild.id, previous_bidder_id),
                                    )
                                end_time = now + timedelta(minutes=1)
                                conn.execute(
                                    "UPDATE economy_auctions SET current_price = ?, "
                                    "highest_bidder_id = ?, ends_at = ? WHERE auction_id = ?",
                                    (amount, bidder.id, end_time.isoformat(), auction_id),
                                )

                if failure:
                    conn.rollback()
                else:
                    conn.commit()
            except Exception as error:
                conn.rollback()
                print(f"Błąd przy składaniu oferty: {error!r}")
                failure = "Nie udało się zapisać oferty. Spróbuj ponownie."
            finally:
                conn.close()

        if failure:
            return await self._reply(target, failure, ephemeral=True)

        end_timestamp = int(end_time.timestamp())
        role = guild.get_role(role_id)
        role_text = role.mention if role is not None else "sprzedawanej roli"
        await self._reply(
            target,
            f"🔨 **Aktualizacja aukcji {role_text}**\n"
            f"Cena początkowa: **{format_money(starting_price)}$**\n"
            f"Cena aktualna: **{format_money(amount)}$**\n"
            f"Najwyższa oferta: {bidder.mention}\n"
            f"Nowy koniec aukcji: <t:{end_timestamp}:R>.",
        )

    async def _finish_auction(self, auction_id: int):
        async with self.auction_lock:
            with connect() as conn:
                auction = conn.execute(
                    "SELECT guild_id, channel_id, creator_id, role_id, current_price, "
                    "highest_bidder_id, ends_at FROM economy_auctions "
                    "WHERE auction_id = ? AND status = 'active'",
                    (auction_id,),
                ).fetchone()

            if auction is None:
                return

            guild_id, channel_id, creator_id, role_id, price, winner_id, ends_at = auction
            if datetime.now(timezone.utc) < datetime.fromisoformat(ends_at):
                return

            if winner_id is None:
                with connect() as conn:
                    conn.execute(
                        "UPDATE economy_auctions SET status = 'ended' "
                        "WHERE auction_id = ? AND status = 'active'",
                        (auction_id,),
                    )
                await self._announce_auction(
                    guild_id,
                    channel_id,
                    "⌛ Aukcja zakończyła się bez ofert. Rola pozostaje u twórcy.",
                )
                return

            guild = self.bot.get_guild(guild_id)
            if guild is None:
                await self._cancel_auction(
                    auction_id,
                    auction,
                    "Bot nie jest już na serwerze. Wpłata zwycięzcy została zwrócona.",
                )
                return

            role = guild.get_role(role_id)
            seller = guild.get_member(creator_id)
            winner = guild.get_member(winner_id)
            bot_member = guild.me

            if role is None or seller is None or winner is None or bot_member is None:
                await self._cancel_auction(
                    auction_id, auction, "Nie udało się znaleźć roli lub gracza."
                )
                return
            if (
                role not in seller.roles
                or role in winner.roles
                or role.managed
                or role == guild.default_role
                or not bot_member.guild_permissions.manage_roles
                or role.position >= bot_member.top_role.position
            ):
                await self._cancel_auction(
                    auction_id,
                    auction,
                    "Roli nie można było bezpiecznie przekazać. Wpłata zwycięzcy została zwrócona.",
                )
                return

            seller_removed = False
            winner_added = False
            try:
                await seller.remove_roles(role, reason="Zakończenie aukcji ekonomii")
                seller_removed = True
                await winner.add_roles(role, reason="Wygrana aukcji ekonomii")
                winner_added = True

                with connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    seller_effects = active_effects(conn, guild_id, seller)
                    seller_income = apply_income_bonus(price, seller_effects)
                    conn.execute(
                        "INSERT OR IGNORE INTO economy_accounts (guild_id, user_id) VALUES (?, ?)",
                        (guild_id, creator_id),
                    )
                    conn.execute(
                        "UPDATE economy_accounts SET wallet = wallet + ? "
                        "WHERE guild_id = ? AND user_id = ?",
                        (seller_income, guild_id, creator_id),
                    )
                    updated = conn.execute(
                        "UPDATE economy_auctions SET status = 'ended' "
                        "WHERE auction_id = ? AND status = 'active'",
                        (auction_id,),
                    )
                    if updated.rowcount != 1:
                        raise RuntimeError("Aukcja nie jest już aktywna.")
            except Exception as error:
                print(f"Błąd przy kończeniu aukcji {auction_id}: {error!r}")
                if winner_added:
                    try:
                        await winner.remove_roles(role, reason="Cofnięcie nieudanej aukcji")
                    except discord.HTTPException:
                        pass
                if seller_removed:
                    try:
                        await seller.add_roles(role, reason="Cofnięcie nieudanej aukcji")
                    except discord.HTTPException:
                        pass
                await self._cancel_auction(
                    auction_id,
                    auction,
                    "Nie udało się przekazać roli. Wpłata zwycięzcy została zwrócona.",
                )
                return

            await self._announce_auction(
                guild_id,
                channel_id,
                f"🏆 Aukcję wygrywa <@{winner_id}>! Rola {role.mention} "
                f"została przeniesiona od <@{creator_id}>. "
                f"Sprzedający otrzymuje **{format_money(seller_income)}$**.",
            )

    async def _cancel_auction(self, auction_id: int, auction, message: str):
        guild_id, channel_id, creator_id, role_id, price, winner_id, _ = auction
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cancelled = conn.execute(
                "UPDATE economy_auctions SET status = 'cancelled' "
                "WHERE auction_id = ? AND status = 'active'",
                (auction_id,),
            )
            if cancelled.rowcount == 1 and winner_id is not None:
                conn.execute(
                    "UPDATE economy_accounts SET wallet = wallet + ? "
                    "WHERE guild_id = ? AND user_id = ?",
                    (price, guild_id, winner_id),
                )
        if cancelled.rowcount == 1:
            await self._announce_auction(guild_id, channel_id, f"⚠️ Aukcja anulowana. {message}")

    async def _announce_auction(self, guild_id: int, channel_id: int, message: str):
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        try:
            await channel.send(message)
        except (discord.Forbidden, discord.HTTPException):
            print(f"Brak możliwości ogłoszenia wyniku aukcji w kanale {channel_id}.")

    async def _buy_listing(self, interaction: discord.Interaction, listing_id: int):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        buyer = interaction.user
        if guild is None or not isinstance(buyer, discord.Member):
            return await interaction.followup.send(
                "Zakupy działają tylko na serwerze.", ephemeral=True
            )

        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            return await interaction.followup.send(
                "Bot potrzebuje uprawnienia **Zarządzanie rolami**.", ephemeral=True
            )

        ensure_account(guild.id, buyer.id)

        async with self.purchase_lock:
            conn = connect()
            seller_role_removed = False
            buyer_role_added = False
            seller_member = None
            role = None

            try:
                conn.execute("BEGIN IMMEDIATE")
                listing = conn.execute(
                    "SELECT seller_id, role_id, price FROM economy_shop_listings "
                    "WHERE guild_id = ? AND listing_id = ?",
                    (guild.id, listing_id),
                ).fetchone()

                if listing is None:
                    conn.rollback()
                    return await interaction.followup.send(
                        "Ta oferta nie jest już dostępna.", ephemeral=True
                    )

                seller_id, role_id, price = listing
                if seller_id == buyer.id:
                    conn.rollback()
                    return await interaction.followup.send(
                        "Nie możesz kupić własnej oferty.", ephemeral=True
                    )

                conn.execute(
                    "INSERT OR IGNORE INTO economy_accounts (guild_id, user_id) VALUES (?, ?)",
                    (guild.id, seller_id),
                )

                buyer_wallet = conn.execute(
                    "SELECT wallet FROM economy_accounts WHERE guild_id = ? AND user_id = ?",
                    (guild.id, buyer.id),
                ).fetchone()[0]
                seller_wallet = conn.execute(
                    "SELECT wallet FROM economy_accounts WHERE guild_id = ? AND user_id = ?",
                    (guild.id, seller_id),
                ).fetchone()

                buyer_effects = active_effects(conn, guild.id, buyer)
                buyer_charge = discounted_price(price, buyer_effects)
                if buyer_wallet < buyer_charge:
                    conn.rollback()
                    return await interaction.followup.send(
                        f"Masz **{format_money(buyer_wallet)}$** w portfelu, "
                        f"a oferta po zniżce kosztuje **{format_money(buyer_charge)}$**.",
                        ephemeral=True,
                    )
                if seller_wallet is None:
                    conn.rollback()
                    return await interaction.followup.send(
                        "Konto sprzedającego nie istnieje.", ephemeral=True
                    )

                role = guild.get_role(role_id)
                if role is None or role.managed or role == guild.default_role:
                    conn.rollback()
                    return await interaction.followup.send(
                        "Ta rola nie jest już dostępna do kupienia.", ephemeral=True
                    )
                if role in buyer.roles:
                    conn.rollback()
                    return await interaction.followup.send(
                        "Masz już tę rolę.", ephemeral=True
                    )
                if role.position >= bot_member.top_role.position:
                    conn.rollback()
                    return await interaction.followup.send(
                        "Bot nie może zarządzać tą rolą. Administrator musi "
                        "przenieść rolę bota wyżej.",
                        ephemeral=True,
                    )

                seller_member = guild.get_member(seller_id)
                if seller_member is None:
                    try:
                        seller_member = await guild.fetch_member(seller_id)
                    except discord.NotFound:
                        conn.rollback()
                        return await interaction.followup.send(
                            "Sprzedający opuścił serwer, więc oferta jest niedostępna.",
                            ephemeral=True,
                        )

                if role not in seller_member.roles:
                    conn.rollback()
                    return await interaction.followup.send(
                        "Sprzedający nie ma już tej roli. Oferta jest nieaktualna.",
                        ephemeral=True,
                    )

                seller_effects = active_effects(conn, guild.id, seller_member)
                seller_income = apply_income_bonus(buyer_charge, seller_effects)

                # Role transfer happens before money changes; any failure rolls back.
                await seller_member.remove_roles(role, reason="Sprzedaż roli w sklepie ekonomii")
                seller_role_removed = True
                await buyer.add_roles(role, reason="Zakup roli w sklepie ekonomii")
                buyer_role_added = True

                if buyer_charge > 0:
                    debit = conn.execute(
                        "UPDATE economy_accounts SET wallet = wallet - ? "
                        "WHERE guild_id = ? AND user_id = ? AND wallet >= ?",
                        (buyer_charge, guild.id, buyer.id, buyer_charge),
                    )
                    if debit.rowcount != 1:
                        raise RuntimeError("Saldo kupującego zmieniło się podczas zakupu.")

                conn.execute(
                    "UPDATE economy_accounts SET wallet = wallet + ? "
                    "WHERE guild_id = ? AND user_id = ?",
                    (seller_income, guild.id, seller_id),
                )
                removed = conn.execute(
                    "DELETE FROM economy_shop_listings "
                    "WHERE guild_id = ? AND listing_id = ?",
                    (guild.id, listing_id),
                )
                if removed.rowcount != 1:
                    raise RuntimeError("Oferta została już kupiona.")

                conn.commit()
            except Exception as error:
                conn.rollback()
                if buyer_role_added and role is not None:
                    try:
                        await buyer.remove_roles(role, reason="Cofnięcie nieudanego zakupu")
                    except discord.HTTPException:
                        pass
                if seller_role_removed and role is not None and seller_member is not None:
                    try:
                        await seller_member.add_roles(role, reason="Cofnięcie nieudanego zakupu")
                    except discord.HTTPException:
                        pass
                print(f"Błąd zakupu w sklepie: {error!r}")
                return await interaction.followup.send(
                    "Nie udało się sfinalizować zakupu. Sprawdź uprawnienia bota i spróbuj ponownie.",
                    ephemeral=True,
                )
            finally:
                conn.close()

        await interaction.followup.send(
            f"✅ Kupiono {role.mention} za **{format_money(buyer_charge)}$**. "
            f"Rola została przeniesiona od <@{seller_id}> do Ciebie.",
            ephemeral=True,
        )

    async def _buy_server_product(self, interaction: discord.Interaction, listing_id: int):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        buyer = interaction.user
        if guild is None or not isinstance(buyer, discord.Member):
            return await interaction.followup.send(
                "Zakupy działają tylko na serwerze.", ephemeral=True
            )

        ensure_account(guild.id, buyer.id)
        role_to_add = None
        role_added = False
        conn = connect()
        async with self.purchase_lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                product = conn.execute(
                    "SELECT product_key, product_type, product_name, role_id, price "
                    "FROM economy_server_shop WHERE guild_id = ? AND listing_id = ?",
                    (guild.id, listing_id),
                ).fetchone()
                if product is None:
                    conn.rollback()
                    return await interaction.followup.send(
                        "Ten produkt nie jest już dostępny.", ephemeral=True
                    )

                product_key, product_type, product_name, role_id, price = product
                account = conn.execute(
                    "SELECT wallet FROM economy_accounts WHERE guild_id = ? AND user_id = ?",
                    (guild.id, buyer.id),
                ).fetchone()
                wallet = account[0] if account else 0
                buyer_effects = active_effects(conn, guild.id, buyer)
                buyer_charge = discounted_price(price, buyer_effects)
                if product_key.startswith("firmmove:"):
                    buyer_charge = discounted_company_price(buyer_charge, buyer_effects)
                if wallet < buyer_charge:
                    conn.rollback()
                    return await interaction.followup.send(
                        f"Masz **{format_money(wallet)}$** w portfelu, "
                        f"a produkt po zniżce kosztuje **{format_money(buyer_charge)}$**.",
                        ephemeral=True,
                    )

                if product_type == "role":
                    role_to_add = guild.get_role(role_id)
                    bot_member = guild.me
                    if role_to_add is None or role_to_add.managed or role_to_add == guild.default_role:
                        conn.rollback()
                        return await interaction.followup.send(
                            "Ta rola nie jest już dostępna.", ephemeral=True
                        )
                    if role_to_add in buyer.roles:
                        conn.rollback()
                        return await interaction.followup.send(
                            "Masz już tę rolę.", ephemeral=True
                        )
                    if (
                        bot_member is None
                        or not bot_member.guild_permissions.manage_roles
                        or role_to_add.position >= bot_member.top_role.position
                    ):
                        conn.rollback()
                        return await interaction.followup.send(
                            "Bot nie może zarządzać tą rolą. Administrator musi "
                            "przenieść jego rolę wyżej.", ephemeral=True
                        )
                    await buyer.add_roles(
                        role_to_add, reason="Zakup produktu w sklepie serwerowym"
                    )
                    role_added = True
                else:
                    conn.execute(
                        "INSERT INTO economy_inventory "
                        "(guild_id, user_id, product_key, item_name, quantity) "
                        "VALUES (?, ?, ?, ?, 1) "
                        "ON CONFLICT(guild_id, user_id, product_key) DO UPDATE SET "
                        "item_name = excluded.item_name, quantity = quantity + 1",
                        (guild.id, buyer.id, product_key, product_name),
                    )

                if buyer_charge > 0:
                    debit = conn.execute(
                        "UPDATE economy_accounts SET wallet = wallet - ? "
                        "WHERE guild_id = ? AND user_id = ? AND wallet >= ?",
                        (buyer_charge, guild.id, buyer.id, buyer_charge),
                    )
                    if debit.rowcount != 1:
                        raise RuntimeError("Saldo kupującego zmieniło się podczas zakupu.")
                conn.commit()
            except Exception as error:
                conn.rollback()
                if role_added and role_to_add is not None:
                    try:
                        await buyer.remove_roles(
                            role_to_add, reason="Cofnięcie nieudanego zakupu"
                        )
                    except discord.HTTPException:
                        pass
                print(f"Błąd zakupu w sklepie serwerowym: {error!r}")
                return await interaction.followup.send(
                    "Nie udało się sfinalizować zakupu. Spróbuj ponownie.",
                    ephemeral=True,
                )
            finally:
                conn.close()

        if product_type == "role":
            result = f"Kupiono {role_to_add.mention}; rola została Ci przyznana."
        else:
            result = f"Dodano **{product_name}** do Twojego ekwipunku."
        await interaction.followup.send(
            f"✅ {result} Cena po zniżce: **{format_money(buyer_charge)}$**.",
            ephemeral=True,
        )

    @dep_group.command(name="all", description="Wpłać cały portfel do banku.")
    async def deposit_slash(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                "Ekonomia działa tylko na serwerze.", ephemeral=True
            )
        ensure_account(interaction.guild_id, interaction.user.id)
        with connect() as conn:
            row = conn.execute(
                "SELECT wallet FROM economy_accounts WHERE guild_id = ? AND user_id = ?",
                (interaction.guild_id, interaction.user.id),
            ).fetchone()
            amount = row[0]
            if amount > 0:
                conn.execute(
                    "UPDATE economy_accounts SET wallet = 0, bank = bank + ? "
                    "WHERE guild_id = ? AND user_id = ?",
                    (amount, interaction.guild_id, interaction.user.id),
                )
        await interaction.response.send_message(
            f"🏦 Wpłacono **{format_money(amount)}** do banku."
            if amount > 0 else (
                "Twój portfel jest poniżej zera. Nie możesz wpłacić ujemnej kwoty."
                if amount < 0 else "Twój portfel jest pusty."
            )
        )

    @commands.command(name="dep", aliases=["deposit"])
    @commands.guild_only()
    async def deposit_prefix(self, ctx: commands.Context, what: str = ""):
        if what.lower() != "all":
            return await ctx.reply("Użycie: `!dep all`", mention_author=False)
        ensure_account(ctx.guild.id, ctx.author.id)
        with connect() as conn:
            row = conn.execute(
                "SELECT wallet FROM economy_accounts WHERE guild_id = ? AND user_id = ?",
                (ctx.guild.id, ctx.author.id),
            ).fetchone()
            amount = row[0]
            if amount > 0:
                conn.execute(
                    "UPDATE economy_accounts SET wallet = 0, bank = bank + ? "
                    "WHERE guild_id = ? AND user_id = ?",
                    (amount, ctx.guild.id, ctx.author.id),
                )
        await ctx.reply(
            f"🏦 Wpłacono **{format_money(amount)}** do banku."
            if amount > 0 else (
                "Twój portfel jest poniżej zera. Nie możesz wpłacić ujemnej kwoty."
                if amount < 0 else "Twój portfel jest pusty."
            ),
            mention_author=False,
        )

    @app_commands.command(name="withdraw", description="Wypłać pieniądze z banku do portfela.")
    @app_commands.describe(amount="Kwota do wypłaty")
    async def withdraw_slash(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1],
    ):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                "Ekonomia działa tylko na serwerze.", ephemeral=True
            )
        ensure_account(interaction.guild_id, interaction.user.id)
        with connect() as conn:
            row = conn.execute(
                "SELECT bank FROM economy_accounts WHERE guild_id = ? AND user_id = ?",
                (interaction.guild_id, interaction.user.id),
            ).fetchone()
            if row[0] < amount:
                return await interaction.response.send_message(
                    f"Masz tylko **{format_money(row[0])}** w banku.", ephemeral=True
                )
            conn.execute(
                "UPDATE economy_accounts SET bank = bank - ?, wallet = wallet + ? "
                "WHERE guild_id = ? AND user_id = ?",
                (amount, amount, interaction.guild_id, interaction.user.id),
            )
        await interaction.response.send_message(
            f"💵 Wypłacono **{format_money(amount)}** z banku do portfela."
        )

    @commands.command(name="withdraw", aliases=["with"])
    @commands.guild_only()
    async def withdraw_prefix(self, ctx: commands.Context, amount: int):
        if amount <= 0:
            return await ctx.reply("Kwota musi być większa od zera.", mention_author=False)
        ensure_account(ctx.guild.id, ctx.author.id)
        with connect() as conn:
            row = conn.execute(
                "SELECT bank FROM economy_accounts WHERE guild_id = ? AND user_id = ?",
                (ctx.guild.id, ctx.author.id),
            ).fetchone()
            if row[0] < amount:
                return await ctx.reply(
                    f"Masz tylko **{format_money(row[0])}** w banku.", mention_author=False
                )
            conn.execute(
                "UPDATE economy_accounts SET bank = bank - ?, wallet = wallet + ? "
                "WHERE guild_id = ? AND user_id = ?",
                (amount, amount, ctx.guild.id, ctx.author.id),
            )
        await ctx.reply(
            f"💵 Wypłacono **{format_money(amount)}** z banku do portfela.",
            mention_author=False,
        )

    @app_commands.command(name="daily", description="Odbierz zarobek daily.")
    async def daily_slash(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                "Ekonomia działa tylko na serwerze.", ephemeral=True
            )
        await self._claim_daily(interaction.guild_id, interaction.user.id, interaction)

    @commands.command(name="daily")
    @commands.guild_only()
    async def daily_prefix(self, ctx: commands.Context):
        await self._claim_daily(ctx.guild.id, ctx.author.id, ctx)

    async def _claim_daily(self, guild_id: int, user_id: int, target):
        ensure_account(guild_id, user_id)
        now = datetime.now(timezone.utc)
        member = target.user if isinstance(target, discord.Interaction) else target.author
        with connect() as conn:
            row = conn.execute(
                "SELECT a.last_daily, s.daily_min, s.daily_max, s.daily_cooldown_hours "
                "FROM economy_accounts a JOIN economy_settings s USING (guild_id) "
                "WHERE a.guild_id = ? AND a.user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            if row[0]:
                message = cooldown_message(row[0], now, row[3])
                if message:
                    return await self._reply(target, message, ephemeral=True)

            base_amount = random.randint(row[1], row[2])
            effects = active_effects(conn, guild_id, member)
            amount = apply_income_bonus(base_amount, effects)
            conn.execute(
                "UPDATE economy_accounts SET wallet = wallet + ?, last_daily = ? "
                "WHERE guild_id = ? AND user_id = ?",
                (amount, now.isoformat(), guild_id, user_id),
            )

        await self._reply(
            target,
            f"🎁 Otrzymujesz **{format_money(amount)}$** z daily do portfela!"
            + (
                f" (z premią itemów +{effects['income_bonus']}%)"
                if effects["income_bonus"] else ""
            ),
        )

    @app_commands.command(name="collect", description="Odbierz dzienny zarobek za swoje role.")
    async def collect_slash(self, interaction: discord.Interaction):
        if interaction.guild_id is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                "Tej komendy można użyć tylko na serwerze.", ephemeral=True
            )
        await self._collect(interaction.guild_id, interaction.user, interaction)

    @commands.command(name="collect")
    @commands.guild_only()
    async def collect_prefix(self, ctx: commands.Context):
        await self._collect(ctx.guild.id, ctx.author, ctx)

    async def _collect(self, guild_id: int, member: discord.Member, target):
        ensure_account(guild_id, member.id)
        now = datetime.now(timezone.utc)
        member_role_ids = {role.id for role in member.roles}

        with connect() as conn:
            account = conn.execute(
                "SELECT last_collect FROM economy_accounts WHERE guild_id = ? AND user_id = ?",
                (guild_id, member.id),
            ).fetchone()

            configured_roles = conn.execute(
                "SELECT role_id, daily_pay FROM economy_collect_roles WHERE guild_id = ?",
                (guild_id,),
            ).fetchall()

            # A job may define its daily salary directly in /create new work.
            # Job salary takes precedence if the same role also has a legacy
            # /create collect setting, so the role is never paid twice.
            role_payments = {role_id: pay for role_id, pay in configured_roles}
            try:
                work_role_payments = conn.execute(
                    "SELECT role_id, daily_pay FROM work_roles "
                    "WHERE guild_id = ? AND daily_pay > 0",
                    (guild_id,),
                ).fetchall()
            except sqlite3.OperationalError:
                # Keep economy usable if the work cog has not created its table.
                work_role_payments = []
            role_payments.update(work_role_payments)

            eligible = [
                (role_id, pay)
                for role_id, pay in role_payments.items()
                if role_id in member_role_ids
            ]

            if not eligible:
                return await self._reply(
                    target,
                    "Nie masz żadnej roli uprawniającej do odbioru collect.",
                    ephemeral=True,
                )

            if account[0]:
                message = cooldown_message(account[0], now)
                if message:
                    return await self._reply(target, message, ephemeral=True)

            base_amount = sum(pay for _, pay in eligible)
            effects = active_effects(conn, guild_id, member)
            amount = apply_income_bonus(base_amount, effects)
            role_lines = []
            for role_id, pay in eligible:
                role = member.guild.get_role(role_id)
                role_label = (
                    discord.utils.escape_markdown(role.name)
                    if role else f"usunięta rola ({role_id})"
                )
                role_lines.append(f"{role_label} — {format_money(pay)}$")

            conn.execute(
                "UPDATE economy_accounts SET wallet = wallet + ?, last_collect = ? "
                "WHERE guild_id = ? AND user_id = ?",
                (amount, now.isoformat(), guild_id, member.id),
            )

        await self._reply(
            target,
            "💼 **Dzisiejsza wypłata za role:**\n"
            + "\n".join(role_lines)
            + f"\n**Razem: {format_money(amount)}$**"
            + (
                f" (z premią itemów +{effects['income_bonus']}%)"
                if effects["income_bonus"] else ""
            )
            + "\n"
            + "Pieniądze trafiły do portfela.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.describe(
        minimum="Minimalna wypłata",
        maximum="Maksymalna wypłata",
        frequency="Jak często można odebrać daily",
    )
    @app_commands.choices(
        frequency=[
            app_commands.Choice(name="Raz na godzinę", value="1"),
            app_commands.Choice(name="Raz na 24 godziny", value="24"),
        ]
    )
    async def create_daily_slash(
        self,
        interaction: discord.Interaction,
        minimum: app_commands.Range[int, 0],
        maximum: app_commands.Range[int, 0],
        frequency: app_commands.Choice[str],
    ):
        """Ustaw zakres wypłaty i częstotliwość odbierania daily."""
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                "Tej komendy można użyć tylko na serwerze.", ephemeral=True
            )
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                "Tę komendę może wykonać tylko administrator.", ephemeral=True
            )
        if maximum < minimum:
            return await interaction.response.send_message(
                "Maksymalna kwota nie może być mniejsza od minimalnej.", ephemeral=True
            )

        with connect() as conn:
            conn.execute(
                "INSERT INTO economy_settings "
                "(guild_id, daily_min, daily_max, daily_cooldown_hours) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET daily_min = excluded.daily_min, "
                "daily_max = excluded.daily_max, "
                "daily_cooldown_hours = excluded.daily_cooldown_hours",
                (interaction.guild_id, minimum, maximum, int(frequency.value)),
            )
        cadence_text = "raz na godzinę" if frequency.value == "1" else "raz na 24 godziny"
        await interaction.response.send_message(
            f"✅ Daily ustawione na **{format_money(minimum)}–{format_money(maximum)}$**, "
            f"odbiór możliwy **{cadence_text}**."
        )

    async def create_collect_slash(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        amount: app_commands.Range[int, 1],
    ):
        """Parametry: role — rola do wypłaty; amount — zarobek za dzień."""
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                "Tej komendy można użyć tylko na serwerze.", ephemeral=True
            )
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                "Tę komendę może wykonać tylko administrator.", ephemeral=True
            )

        with connect() as conn:
            conn.execute(
                "INSERT INTO economy_collect_roles (guild_id, role_id, daily_pay) "
                "VALUES (?, ?, ?) ON CONFLICT(guild_id, role_id) "
                "DO UPDATE SET daily_pay = excluded.daily_pay",
                (interaction.guild_id, role.id, amount),
            )

        await interaction.response.send_message(
            f"✅ Rola {role.mention} daje **{format_money(amount)}$** "
            "za każdy odbiór collect (raz na 24 godziny)."
        )

    @app_commands.describe(
        price="Cena skrzynki w sklepie serwerowym",
        name="Nazwa skrzynki",
        rewards="Np. item:Diament=30; role:VIP=20; money:500=10. Reszta rozdzielana równo.",
    )
    async def create_box_slash(
        self,
        interaction: discord.Interaction,
        price: app_commands.Range[int, 1],
        name: str,
        rewards: str,
    ):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message(
                "Skrzynki można tworzyć tylko na serwerze.", ephemeral=True
            )
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                "Tę komendę może wykonać tylko administrator.", ephemeral=True
            )

        name = " ".join(name.split())
        if not name or len(name) > 80:
            return await interaction.response.send_message(
                "Nazwa skrzynki musi mieć od 1 do 80 znaków.", ephemeral=True
            )
        try:
            reward_entries = parse_box_rewards(rewards, guild)
        except ValueError as error:
            return await interaction.response.send_message(str(error), ephemeral=True)

        box_key = f"box:{normalize_key(name)}"
        description = "Skrzynka z losową nagrodą. Użyj `/open box`, aby ją otworzyć."
        now = datetime.now(timezone.utc).isoformat()
        with connect() as conn:
            conn.execute(
                "INSERT INTO economy_boxes (guild_id, box_key, box_name, price, listed) "
                "VALUES (?, ?, ?, ?, 1) ON CONFLICT(guild_id, box_key) DO UPDATE SET "
                "box_name = excluded.box_name, price = excluded.price, listed = 1",
                (guild.id, box_key, name, price),
            )
            conn.execute(
                "DELETE FROM economy_box_rewards WHERE guild_id = ? AND box_key = ?",
                (guild.id, box_key),
            )
            conn.executemany(
                "INSERT INTO economy_box_rewards "
                "(guild_id, box_key, reward_type, reward_key, reward_name, reward_amount, chance) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        guild.id,
                        box_key,
                        entry["type"],
                        entry["key"],
                        entry["name"],
                        entry["amount"],
                        entry["chance"],
                    )
                    for entry in reward_entries
                ],
            )
            conn.execute(
                "INSERT INTO economy_server_shop "
                "(guild_id, product_key, product_type, product_name, role_id, price, description, created_at) "
                "VALUES (?, ?, 'item', ?, NULL, ?, ?, ?) "
                "ON CONFLICT(guild_id, product_key) DO UPDATE SET "
                "product_name = excluded.product_name, price = excluded.price, "
                "description = excluded.description, created_at = excluded.created_at",
                (guild.id, box_key, name, price, description, now),
            )

        shown_rewards = reward_entries[:12]
        reward_summary = "\n".join(
            f"• {entry['name'][:80]} — **{entry['chance']:.2f}%**"
            for entry in shown_rewards
        )
        if len(reward_entries) > len(shown_rewards):
            reward_summary += f"\n… i {len(reward_entries) - len(shown_rewards)} kolejnych nagród."
        await interaction.response.send_message(
            f"✅ Utworzono skrzynkę **{name}** za **{format_money(price)}$** "
            "i dodano ją do sklepu serwerowego.\n"
            f"Szanse po równym rozdaniu reszty:\n{reward_summary}"
        )

    @app_commands.describe(chance="Szansa trafienia numeru obstawionego przez gracza")
    async def create_roulette_slash(
        self,
        interaction: discord.Interaction,
        chance: app_commands.Range[int, 0, 100],
    ):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                "Ruletkę można skonfigurować tylko na serwerze.", ephemeral=True
            )
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                "Tę komendę może wykonać tylko administrator.", ephemeral=True
            )
        with connect() as conn:
            conn.execute(
                "INSERT INTO economy_settings (guild_id, roulette_chance) VALUES (?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET "
                "roulette_chance = excluded.roulette_chance",
                (interaction.guild_id, chance),
            )
        await interaction.response.send_message(
            f"✅ Szansa trafienia obstawionego numeru ustawiona na **{chance}%**. "
            "W grze solo reszta przypada równo na pozostałe 29 numerów; "
            "w grze online jest to szansa, że wygra jeden z uczestników, "
            "dzielona równo między obstawiających. W solo wygrana płaci 2× stawki; "
            "w online zwycięzca bierze całą pulę."
        )

    @app_commands.describe(name="Nazwa skrzynki do usunięcia ze sklepu")
    async def delete_box_slash(
        self,
        interaction: discord.Interaction,
        name: str,
    ):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message(
                "Skrzynki można usuwać tylko na serwerze.", ephemeral=True
            )
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                "Tę komendę może wykonać tylko administrator.", ephemeral=True
            )

        box_key = f"box:{normalize_key(name)}"
        with connect() as conn:
            changed = conn.execute(
                "UPDATE economy_boxes SET listed = 0 "
                "WHERE guild_id = ? AND box_key = ? AND listed = 1",
                (guild.id, box_key),
            ).rowcount
            conn.execute(
                "DELETE FROM economy_server_shop WHERE guild_id = ? AND product_key = ?",
                (guild.id, box_key),
            )
        if changed == 0:
            return await interaction.response.send_message(
                "Nie znaleziono takiej skrzynki w sklepie.", ephemeral=True
            )
        await interaction.response.send_message(
            f"✅ Usunięto skrzynkę **{name}** ze sklepu. Gracze nadal mogą otworzyć "
            "egzemplarze, które wcześniej kupili."
        )

    @app_commands.describe(
        product="Nazwa itemu albo nazwa, wzmianka lub ID roli",
        price="Cena produktu w portfelu",
        description="Opcjonalny opis produktu",
    )
    async def create_shop_slash(
        self,
        interaction: discord.Interaction,
        product: str,
        price: app_commands.Range[int, 1],
        description: str | None = None,
    ):
        """Dodaj rolę lub nielimitowany item do sklepu serwerowego."""
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message(
                "Sklep serwerowy działa tylko na serwerze.", ephemeral=True
            )
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                "Tę komendę może wykonać tylko administrator.", ephemeral=True
            )

        product = product.strip()
        description = (description or "").strip()
        if not product:
            return await interaction.response.send_message(
                "Nazwa produktu nie może być pusta.", ephemeral=True
            )
        if len(product) > 100 or len(description) > 100:
            return await interaction.response.send_message(
                "Nazwa produktu i opis mogą mieć maksymalnie 100 znaków.",
                ephemeral=True,
            )

        role = None
        role_mention = re.fullmatch(r"<@&(\d+)>|([0-9]{15,22})", product)
        if role_mention:
            role_id = int(role_mention.group(1) or role_mention.group(2))
            role = guild.get_role(role_id)
            if role is None:
                return await interaction.response.send_message(
                    "Nie znaleziono takiej roli na tym serwerze.", ephemeral=True
                )
        else:
            role = discord.utils.get(guild.roles, name=product)

        if role is not None:
            if role == guild.default_role or role.managed:
                return await interaction.response.send_message(
                    "Nie można sprzedawać tej roli.", ephemeral=True
                )
            bot_member = guild.me
            if (
                bot_member is None
                or not bot_member.guild_permissions.manage_roles
                or role.position >= bot_member.top_role.position
            ):
                return await interaction.response.send_message(
                    "Bot potrzebuje uprawnienia **Zarządzanie rolami**, a jego "
                    "najwyższa rola musi być wyżej od sprzedawanej roli.",
                    ephemeral=True,
                )
            product_type = "role"
            product_name = role.name
            product_key = f"role:{role.id}"
            role_id = role.id
        else:
            product_type = "item"
            product_name = product
            product_key = f"item:{' '.join(product.casefold().split())}"
            role_id = None

        now = datetime.now(timezone.utc).isoformat()
        with connect() as conn:
            conn.execute(
                "INSERT INTO economy_server_shop "
                "(guild_id, product_key, product_type, product_name, role_id, price, "
                "description, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, product_key) DO UPDATE SET "
                "product_type = excluded.product_type, "
                "product_name = excluded.product_name, role_id = excluded.role_id, "
                "price = excluded.price, description = excluded.description, "
                "created_at = excluded.created_at",
                (
                    guild.id,
                    product_key,
                    product_type,
                    product_name,
                    role_id,
                    price,
                    description,
                    now,
                ),
            )

        kind_label = "rolę" if product_type == "role" else "item"
        await interaction.response.send_message(
            f"✅ Dodano **{product_name}** jako {kind_label} do sklepu serwerowego "
            f"za **{format_money(price)}$**. Zapas jest nieograniczony."
        )

    @app_commands.describe(
        product="Nazwa itemu albo nazwa, wzmianka lub ID roli",
        effect="Pasywny efekt produktu",
        value="Wartość procentowa efektu",
    )
    @app_commands.choices(
        effect=[
            app_commands.Choice(name="Ochrona przed kradzieżą", value="rob_defense"),
            app_commands.Choice(name="Większa szansa kradzieży", value="rob_bonus"),
            app_commands.Choice(name="Premia do zarobków", value="income_bonus"),
            app_commands.Choice(name="Zniżka w sklepach", value="shop_discount"),
            app_commands.Choice(name="Zniżka w systemie firm", value="company_discount"),
            app_commands.Choice(name="Premia do zarobków z firm", value="company_income_bonus"),
        ]
    )
    async def create_item_slash(
        self,
        interaction: discord.Interaction,
        product: str,
        effect: app_commands.Choice[str],
        value: app_commands.Range[int, 0, 500],
    ):
        """Ustaw lub zmień jeden pasywny efekt dla itemu albo roli."""
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message(
                "Tej komendy można użyć tylko na serwerze.", ephemeral=True
            )
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                "Tę komendę może wykonać tylko administrator.", ephemeral=True
            )

        product = product.strip()
        if not product:
            return await interaction.response.send_message(
                "Nazwa produktu nie może być pusta.", ephemeral=True
            )

        role_match = re.fullmatch(r"<@&(\d+)>|([0-9]{15,22})", product)
        role = None
        if role_match:
            role = guild.get_role(int(role_match.group(1) or role_match.group(2)))
            if role is None:
                return await interaction.response.send_message(
                    "Nie znaleziono takiej roli na tym serwerze.", ephemeral=True
                )
        else:
            role = discord.utils.get(guild.roles, name=product)

        if role is not None:
            if role == guild.default_role or role.managed:
                return await interaction.response.send_message(
                    "Nie można ustawić efektu dla tej roli.", ephemeral=True
                )
            product_key = f"role:{role.id}"
            product_name = role.name
        else:
            product_key = f"item:{' '.join(product.casefold().split())}"
            product_name = product

        effect_type = effect.value
        if effect_type not in {"income_bonus", "company_income_bonus"} and value > 100:
            return await interaction.response.send_message(
                "Ten efekt może mieć wartość od 0% do 100%. "
                "Premie do zarobków mogą mieć do 500%.",
                ephemeral=True,
            )

        with connect() as conn:
            conn.execute(
                "INSERT INTO economy_item_effects "
                "(guild_id, product_key, effect_type, effect_value) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, product_key, effect_type) "
                "DO UPDATE SET effect_value = excluded.effect_value",
                (guild.id, product_key, effect_type, value),
            )

        if effect_type == "rob_defense":
            effect_text = f"zmniejsza szansę okradzenia właściciela o {value} punktów procentowych"
        elif effect_type == "rob_bonus":
            effect_text = f"zwiększa szansę udanej kradzieży o {value} punktów procentowych"
        elif effect_type == "income_bonus":
            effect_text = f"zwiększa zarobki o {value}%"
        elif effect_type == "company_discount":
            effect_text = f"obniża koszty w systemie firm o {value}%"
        elif effect_type == "company_income_bonus":
            effect_text = f"zwiększa zarobek ze sprzedaży firmowej o {value}%"
        else:
            effect_text = f"obniża ceny w sklepach o {value}%"

        await interaction.response.send_message(
            f"✅ Ustawiono efekt dla **{product_name}**: {effect_text}. "
            "Efekt działa pasywnie, gdy item jest w ekwipunku albo gracz ma tę rolę."
        )

    async def delete_shop_slash(
        self,
        interaction: discord.Interaction,
        product: str,
    ):
        """Usuń produkt z katalogu sklepu serwerowego."""
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message(
                "Tej komendy można użyć tylko na serwerze.", ephemeral=True
            )
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                "Tę komendę może wykonać tylko administrator.", ephemeral=True
            )

        product = product.strip()
        if not product:
            return await interaction.response.send_message(
                "Podaj nazwę itemu, nazwę/wzmiankę/ID roli.", ephemeral=True
            )

        role_match = re.fullmatch(r"<@&(\d+)>|([0-9]{15,22})", product)
        if role_match:
            product_key = f"role:{int(role_match.group(1) or role_match.group(2))}"
        else:
            role = discord.utils.get(guild.roles, name=product)
            if role is not None:
                product_key = f"role:{role.id}"
            else:
                product_key = f"item:{' '.join(product.casefold().split())}"

        with connect() as conn:
            deleted = conn.execute(
                "DELETE FROM economy_server_shop "
                "WHERE guild_id = ? AND product_key = ?",
                (guild.id, product_key),
            ).rowcount

        if deleted == 0:
            return await interaction.response.send_message(
                "Nie znaleziono tego produktu w sklepie serwerowym.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            f"✅ Usunięto **{product}** ze sklepu serwerowego. "
            "Przedmioty już kupione przez graczy pozostają w ich ekwipunkach."
        )

    @app_commands.describe(chance="Szansa powodzenia od 0 do 100 procent")
    async def create_rob_slash(
        self,
        interaction: discord.Interaction,
        chance: app_commands.Range[int, 0, 100],
    ):
        """Ustaw serwerową szansę powodzenia kradzieży w procentach."""
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                "Tej komendy można użyć tylko na serwerze.", ephemeral=True
            )
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                "Tę komendę może wykonać tylko administrator.", ephemeral=True
            )

        with connect() as conn:
            conn.execute(
                "INSERT INTO economy_settings (guild_id, rob_chance) VALUES (?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET rob_chance = excluded.rob_chance",
                (interaction.guild_id, chance),
            )
        await interaction.response.send_message(
            f"✅ Ustawiono szansę udanej kradzieży na **{chance}%**."
        )

    @app_commands.command(name="rob", description="Spróbuj ukraść pieniądze innemu graczowi.")
    @app_commands.describe(member="Gracz, którego chcesz okraść", amount="Kwota próby kradzieży")
    async def rob_slash(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1],
    ):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                "Tej komendy można użyć tylko na serwerze.", ephemeral=True
            )
        await self._rob(interaction, interaction.guild, interaction.user, member, amount)

    @commands.command(name="rob")
    @commands.guild_only()
    async def rob_prefix(
        self,
        ctx: commands.Context,
        member: discord.Member,
        amount: int,
    ):
        await self._rob(ctx, ctx.guild, ctx.author, member, amount)

    async def _rob(self, target, guild, thief, victim, amount: int):
        if amount <= 0:
            return await self._reply(target, "Kwota kradzieży musi być większa od zera.", ephemeral=True)
        if victim.guild.id != guild.id:
            return await self._reply(target, "Wybierz gracza z tego serwera.", ephemeral=True)
        if thief.id == victim.id:
            return await self._reply(target, "Nie możesz okraść samego siebie.", ephemeral=True)
        if victim.bot:
            return await self._reply(target, "Nie można okradać botów.", ephemeral=True)

        ensure_account(guild.id, thief.id)
        ensure_account(guild.id, victim.id)
        result_message = None

        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            settings = conn.execute(
                "SELECT rob_chance FROM economy_settings WHERE guild_id = ?",
                (guild.id,),
            ).fetchone()
            chance = settings[0] if settings else None

            if chance is None:
                conn.rollback()
                return await self._reply(
                    target,
                    "Administrator nie ustawił jeszcze szansy kradzieży. "
                    "Użyj `/create rob` z wartością od 0 do 100.",
                    ephemeral=True,
                )

            thief_effects = active_effects(conn, guild.id, thief)
            victim_effects = active_effects(conn, guild.id, victim)
            effective_chance = max(
                0,
                min(
                    100,
                    chance
                    + thief_effects["rob_bonus"]
                    - victim_effects["rob_defense"],
                ),
            )
            success = random.randint(1, 100) <= effective_chance
            if success:
                stolen_amount = apply_income_bonus(amount, thief_effects)
                conn.execute(
                    "UPDATE economy_accounts SET wallet = wallet - ? "
                    "WHERE guild_id = ? AND user_id = ?",
                    (amount, guild.id, victim.id),
                )
                conn.execute(
                    "UPDATE economy_accounts SET wallet = wallet + ? "
                    "WHERE guild_id = ? AND user_id = ?",
                    (stolen_amount, guild.id, thief.id),
                )
                result_message = (
                    f"🦹 Kradzież udana! Zabrałeś **{format_money(amount)}$** "
                    f"graczowi {victim.mention} i otrzymujesz "
                    f"**{format_money(stolen_amount)}$**"
                    + (
                        f" (z premią do zarobków +{thief_effects['income_bonus']}%)."
                        if thief_effects["income_bonus"] else "."
                    )
                )
            else:
                conn.execute(
                    "UPDATE economy_accounts SET wallet = wallet - ? "
                    "WHERE guild_id = ? AND user_id = ?",
                    (amount, guild.id, thief.id),
                )
                new_wallet = conn.execute(
                    "SELECT wallet FROM economy_accounts WHERE guild_id = ? AND user_id = ?",
                    (guild.id, thief.id),
                ).fetchone()[0]
                result_message = (
                    f"🚨 Kradzież nieudana! Straciłeś **{format_money(amount)}$**. "
                    f"Twój portfel wynosi teraz **{format_money(new_wallet)}$**."
                )

        await self._reply(target, result_message)

    async def reset_money_slash(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                "Resetu można użyć tylko na serwerze.", ephemeral=True
            )
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                "Tę komendę może wykonać tylko administrator.", ephemeral=True
            )

        view = ResetMoneyView(self, interaction.guild_id, interaction.user.id)
        await interaction.response.send_message(
            "⚠️ Potwierdź reset ekonomii tego serwera. Wyzeruje on portfele i banki "
            "wszystkich graczy, usunie oferty z ich prywatnych sklepów "
            "i anuluje aktywne aukcje. "
            "Sklep serwerowy pozostanie bez zmian.",
            view=view,
            ephemeral=True,
        )

    def reset_guild_economy(self, guild_id: int) -> tuple[int, int, int]:
        with connect() as conn:
            balances_reset = conn.execute(
                "UPDATE economy_accounts SET wallet = 0, bank = 0 WHERE guild_id = ?",
                (guild_id,),
            ).rowcount
            listings_deleted = conn.execute(
                "DELETE FROM economy_shop_listings WHERE guild_id = ?",
                (guild_id,),
            ).rowcount
            auctions_cancelled = conn.execute(
                "UPDATE economy_auctions SET status = 'cancelled' "
                "WHERE guild_id = ? AND status = 'active'",
                (guild_id,),
            ).rowcount
        return balances_reset, listings_deleted, auctions_cancelled

    async def add_money_slash(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1],
    ):
        """Parameters: member — player; amount — money to add to their wallet."""
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                "Tej komendy można użyć tylko na serwerze.", ephemeral=True
            )
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                "Tę komendę może wykonać tylko administrator.", ephemeral=True
            )

        ensure_account(interaction.guild_id, member.id)
        with connect() as conn:
            conn.execute(
                "UPDATE economy_accounts SET wallet = wallet + ? "
                "WHERE guild_id = ? AND user_id = ?",
                (amount, interaction.guild_id, member.id),
            )
        await interaction.response.send_message(
            f"✅ Dodano **{format_money(amount)}$** do portfela gracza {member.mention}."
        )

    async def remove_money_slash(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1],
    ):
        """Parameters: member — player; amount — money to remove from their wallet."""
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                "Tej komendy można użyć tylko na serwerze.", ephemeral=True
            )
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                "Tę komendę może wykonać tylko administrator.", ephemeral=True
            )

        if member.guild.id != interaction.guild_id:
            return await interaction.response.send_message(
                "Wybierz gracza z tego serwera.", ephemeral=True
            )

        ensure_account(interaction.guild_id, member.id)
        with connect() as conn:
            conn.execute(
                "UPDATE economy_accounts SET wallet = wallet - ? "
                "WHERE guild_id = ? AND user_id = ?",
                (amount, interaction.guild_id, member.id),
            )
            new_wallet = conn.execute(
                "SELECT wallet FROM economy_accounts WHERE guild_id = ? AND user_id = ?",
                (interaction.guild_id, member.id),
            ).fetchone()[0]
        await interaction.response.send_message(
            f"✅ Odjęto **{format_money(amount)}$** z portfela gracza {member.mention}.\n"
            f"Nowy stan portfela: **{format_money(new_wallet)}$**."
        )

    async def delete_collect_slash(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
    ):
        """Usuń dzienną wypłatę collect przypisaną do roli."""
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                "Tej komendy można użyć tylko na serwerze.", ephemeral=True
            )
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                "Tę komendę może wykonać tylko administrator.", ephemeral=True
            )
        if role.guild.id != interaction.guild_id:
            return await interaction.response.send_message(
                "Wybierz rolę z tego serwera.", ephemeral=True
            )

        with connect() as conn:
            legacy_deleted = conn.execute(
                "DELETE FROM economy_collect_roles WHERE guild_id = ? AND role_id = ?",
                (interaction.guild_id, role.id),
            ).rowcount

            work_salary_removed = 0
            try:
                work_salary_removed = conn.execute(
                    "UPDATE work_roles SET daily_pay = 0 "
                    "WHERE guild_id = ? AND role_id = ? AND daily_pay != 0",
                    (interaction.guild_id, role.id),
                ).rowcount
            except sqlite3.OperationalError:
                # The work cog may not have created its table yet.
                pass

        if legacy_deleted == 0 and work_salary_removed == 0:
            return await interaction.response.send_message(
                f"Rola {role.mention} nie ma ustawionego zarobku collect.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            f"✅ Usunięto dzienny zarobek collect dla roli {role.mention}. "
            "Sama praca i jej XP pozostały bez zmian."
        )

    async def _reply(
        self,
        target,
        message: str,
        ephemeral: bool = False,
        allowed_mentions: discord.AllowedMentions | None = None,
    ):
        if isinstance(target, discord.Interaction):
            await target.response.send_message(
                message,
                ephemeral=ephemeral,
                allowed_mentions=allowed_mentions,
            )
        else:
            await target.reply(
                message,
                mention_author=False,
                allowed_mentions=allowed_mentions,
            )


async def setup(bot: commands.Bot):
    create_tables()
    cog = Ekonomia(bot)
    await bot.add_cog(cog)

    specifications = {
        "create": (
            "Komendy tworzenia i konfiguracji.",
            (
                ("daily", "Ustaw zakres i częstotliwość odbioru daily.", cog.create_daily_slash),
                ("collect", "Ustaw dzienny zarobek dla roli.", cog.create_collect_slash),
                ("box", "Utwórz skrzynkę i ustaw jej nagrody oraz szanse.", cog.create_box_slash),
                ("shop", "Dodaj lub zmień produkt sklepu serwerowego.", cog.create_shop_slash),
                ("item", "Ustaw pasywny efekt dla itemu lub roli.", cog.create_item_slash),
                ("rob", "Ustaw procentową szansę udanej kradzieży.", cog.create_rob_slash),
                ("roullete", "Ustaw szansę trafienia numeru w ruletce.", cog.create_roulette_slash),
                ("auction", "Rozpocznij minutową aukcję roli.", cog.create_auction_slash),
            ),
        ),
        "reset": (
            "Komendy resetowania.",
            (("money", "Wyzeruj ekonomię i usuń prywatne oferty.", cog.reset_money_slash),),
        ),
        "add": (
            "Komendy administratora do dodawania.",
            (("money", "Dodaj pieniądze graczowi.", cog.add_money_slash),),
        ),
        "remove": (
            "Komendy administratora do odejmowania.",
            (("money", "Odejmij pieniądze graczowi.", cog.remove_money_slash),),
        ),
        "delete": (
            "Usuwanie ustawień.",
            (
                ("collect", "Usuń dzienny zarobek collect dla roli.", cog.delete_collect_slash),
                ("shop", "Usuń produkt ze sklepu serwerowego.", cog.delete_shop_slash),
                ("box", "Usuń skrzynkę ze sklepu.", cog.delete_box_slash),
            ),
        ),
        "top": (
            "Rankingi ekonomii.",
            (("ekonomy", "Pokaż ranking majątku graczy.", cog.top_economy_slash),),
        ),
        "open": (
            "Otwieranie przedmiotów.",
            (("box", "Otwórz skrzynkę z ekwipunku.", cog.open_box_slash),),
        ),
        "roulette": (
            "Gry ruletki.",
            (("online", "Uruchom minutową ruletkę online.", cog.roulette_online_slash),),
        ),
        "join": (
            "Dołączanie do gier.",
            (("roullete", "Dołącz do ruletki online.", cog.join_roulette_slash),),
        ),
    }

    for group_name, (description, subcommands) in specifications.items():
        group = bot.tree.get_command(group_name)
        if group is None:
            group = app_commands.Group(name=group_name, description=description)
            bot.tree.add_command(group)
            cog.created_admin_groups.add(group_name)
        elif not isinstance(group, app_commands.Group):
            raise RuntimeError(
                f"Komenda /{group_name} istnieje, ale nie jest grupą slash."
            )

        cog.attached_admin_groups[group_name] = group
        cog.added_admin_subcommands[group_name] = []

        for subcommand_name, subcommand_description, callback in subcommands:
            if group.get_command(subcommand_name) is not None:
                raise RuntimeError(
                    f"Podkomenda /{group_name} {subcommand_name} "
                    "jest już zarejestrowana."
                )

            group.add_command(
                app_commands.Command(
                    name=subcommand_name,
                    description=subcommand_description,
                    callback=callback,
                )
            )
            cog.added_admin_subcommands[group_name].append(subcommand_name)
