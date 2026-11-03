"""System firm for EmpireBot. Uses database.connect and economy_accounts.wallet."""
from __future__ import annotations

import json
import asyncio
import math
import random
import re
from datetime import datetime, timezone
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import connect
from cogs.ekonomia import (
    apply_company_income_bonus,
    discounted_company_price,
    effects_for_user,
)

DATA_FILE = Path(__file__).resolve().parent.parent / "firmy_katalog.json"
with DATA_FILE.open("r", encoding="utf-8-sig") as file:
    CATALOG = json.load(file)
EVENT_FILE = Path(__file__).resolve().parent.parent / "firmy_zdarzenia.json"
with EVENT_FILE.open("r", encoding="utf-8-sig") as file:
    EVENTS = json.load(file)
BY_ID = {int(row["id"]): row for row in CATALOG}
BY_PRODUCT = {row["product"].casefold(): row for row in CATALOG}
BY_EVENT = {int(row["id"]): row for row in EVENTS}


def money(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def tree_for(investment_id: int) -> str:
    if investment_id <= 25 or investment_id in (131, 133):
        return "Wydobycie"
    if 26 <= investment_id <= 59 or investment_id in (132, 134, 135, 136, 139, 140, 141):
        return "Przetwarzanie"
    if 61 <= investment_id <= 100 or investment_id == 137:
        return "Komponenty"
    return "Gotowe produkty"


def init_firm_tables():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS firms(
          firm_id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
          owner_id INTEGER NOT NULL, name TEXT NOT NULL, founded_at TEXT NOT NULL,
          UNIQUE(guild_id, name));
        CREATE TABLE IF NOT EXISTS firm_investment_prices(
          guild_id INTEGER NOT NULL, investment_id INTEGER NOT NULL, price INTEGER NOT NULL,
          PRIMARY KEY(guild_id, investment_id));
        CREATE TABLE IF NOT EXISTS firm_investments(
          firm_id INTEGER NOT NULL, investment_id INTEGER NOT NULL, bought_at TEXT NOT NULL,
          purchase_price INTEGER NOT NULL,
          PRIMARY KEY(firm_id, investment_id));
        CREATE TABLE IF NOT EXISTS firm_product_prices(
          guild_id INTEGER NOT NULL, product_id INTEGER NOT NULL, catalog_price INTEGER NOT NULL,
          PRIMARY KEY(guild_id, product_id));
        CREATE TABLE IF NOT EXISTS firm_inventory(
          firm_id INTEGER NOT NULL, product_id INTEGER NOT NULL, quantity INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(firm_id, product_id));
        CREATE TABLE IF NOT EXISTS firm_listings(
          listing_id INTEGER PRIMARY KEY AUTOINCREMENT, firm_id INTEGER NOT NULL,
          product_id INTEGER NOT NULL, quantity INTEGER NOT NULL, price INTEGER NOT NULL,
          created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS firm_activity(
          firm_id INTEGER NOT NULL, product_id INTEGER NOT NULL, last_produced TEXT NOT NULL,
          last_sold TEXT NOT NULL, stable_price_since TEXT NOT NULL, stable_price INTEGER NOT NULL,
          PRIMARY KEY(firm_id, product_id));
        CREATE TABLE IF NOT EXISTS firm_settings(
          guild_id INTEGER PRIMARY KEY, event_channel_id INTEGER, last_buy_date TEXT);
        CREATE TABLE IF NOT EXISTS firm_active_events(
          guild_id INTEGER NOT NULL, event_id INTEGER NOT NULL, starts_at TEXT NOT NULL,
          ends_at TEXT NOT NULL, PRIMARY KEY(guild_id,event_id));
        CREATE TABLE IF NOT EXISTS firm_blocks(
          firm_id INTEGER PRIMARY KEY, tree_name TEXT NOT NULL, ends_at TEXT NOT NULL,
          created_by INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS firm_ads(
          firm_id INTEGER PRIMARY KEY, bonus_percent INTEGER NOT NULL, ends_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS firm_moves(
          guild_id INTEGER NOT NULL, move_key TEXT NOT NULL, move_name TEXT NOT NULL,
          price INTEGER NOT NULL, PRIMARY KEY(guild_id,move_key));
        CREATE TABLE IF NOT EXISTS firm_sales_days(
          firm_id INTEGER NOT NULL, product_id INTEGER NOT NULL, sale_day TEXT NOT NULL,
          PRIMARY KEY(firm_id,product_id,sale_day));
        CREATE TABLE IF NOT EXISTS firm_market_daily(
          guild_id INTEGER NOT NULL, market_date TEXT NOT NULL, product_id INTEGER NOT NULL,
          firm_id INTEGER NOT NULL, allocated INTEGER NOT NULL, purchased INTEGER NOT NULL DEFAULT 0,
          base_allocated INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(guild_id,market_date,product_id,firm_id));
        CREATE TABLE IF NOT EXISTS firm_market_shares(
          guild_id INTEGER NOT NULL, market_date TEXT NOT NULL, product_id INTEGER NOT NULL,
          firm_id INTEGER NOT NULL, share REAL NOT NULL,
          PRIMARY KEY(guild_id,market_date,product_id,firm_id));
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_firms_owner ON firms(guild_id, owner_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_firm_listings ON firm_listings(firm_id, product_id)")
        investment_columns={row[1] for row in db.execute("PRAGMA table_info(firm_investments)").fetchall()}
        if "purchase_price" not in investment_columns:
            db.execute("ALTER TABLE firm_investments ADD COLUMN purchase_price INTEGER NOT NULL DEFAULT 0")
        market_columns={row[1] for row in db.execute("PRAGMA table_info(firm_market_daily)").fetchall()}
        if "base_allocated" not in market_columns:
            db.execute("ALTER TABLE firm_market_daily ADD COLUMN base_allocated INTEGER NOT NULL DEFAULT 0")
            db.execute("UPDATE firm_market_daily SET base_allocated=allocated")


def recipe_for(row):
    """Return alternative ingredient groups; one unit is needed from each group."""
    recipe = row.get("recipe", "")
    if not recipe:
        return []
    groups = []
    for chunk in recipe.split("+"):
        ids=[int(value) for value in re.findall(r"#(\d+)",chunk) if int(value) in BY_ID]
        if not ids:
            key=chunk.strip().casefold()
            if key in BY_PRODUCT: ids=[int(BY_PRODUCT[key]["id"])]
        if ids: groups.append(list(dict.fromkeys(ids)))
    return groups


class Firmy(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.attached: list[tuple[app_commands.Group, str]] = []
        self.groups_created: list[str] = []
        self.prefix_groups_created: list[str] = []
        self.prefix_attached: list[tuple[commands.Group, str]] = []
        init_firm_tables()
        self.daily_market_buy.start()

    def _effects_for_user(self, db, guild_id: int, user_id: int) -> dict[str, int]:
        guild=self.bot.get_guild(guild_id)
        member=guild.get_member(user_id) if guild else None
        role_ids=tuple(role.id for role in member.roles) if member else ()
        return effects_for_user(db,guild_id,user_id,role_ids)

    def cog_unload(self):
        self.daily_market_buy.cancel()
        for group, name in self.prefix_attached:
            if group.get_command(name):
                group.remove_command(name)
        for name in self.prefix_groups_created:
            self.bot.remove_command(name)
        for group, name in self.attached:
            if group.get_command(name):
                group.remove_command(name)
        for name in self.groups_created:
            self.bot.tree.remove_command(name)

    @tasks.loop(minutes=1)
    async def daily_market_buy(self):
        """Create one daily share snapshot at 17:00, then buy new offers as they appear."""
        now = datetime.now(ZoneInfo("Europe/Warsaw"))
        today = now.date().isoformat()
        with connect() as db:
            guilds = db.execute("SELECT DISTINCT guild_id FROM firms").fetchall()
        messages=[]
        for (guild_id,) in guilds:
            setting = None
            with connect() as db:
                setting = db.execute("SELECT last_buy_date FROM firm_settings WHERE guild_id=?", (guild_id,)).fetchone()
                if now.hour >= 17 and (not setting or setting[0] != today):
                    messages.extend(self._create_market_snapshot(db, guild_id, now, today))
            purchase_log = self._process_live_market(guild_id, today)
            if purchase_log:
                with connect() as db:
                    row = db.execute("SELECT event_channel_id FROM firm_settings WHERE guild_id=?", (guild_id,)).fetchone()
                if row and row[0]:
                    messages.append((row[0], purchase_log))
        for channel_id,text in messages:
            channel=self.bot.get_channel(channel_id)
            if channel is None:
                try: channel=await self.bot.fetch_channel(channel_id)
                except (discord.NotFound,discord.Forbidden,discord.HTTPException): channel=None
            if channel:
                try:
                    for start in range(0, len(text), 1800):
                        await channel.send(text[start:start+1800])
                except discord.HTTPException: pass

    def _refresh_supplemental_demand(self, db, guild_id, today):
        """Recalculate simulated input demand from company stock once per market tick."""
        base={int(row["id"]):int(row.get("demand",0)) for row in CATALOG}
        total=base.copy()
        available={int(row[0]) for row in db.execute("""
            SELECT i.product_id FROM firm_inventory i JOIN firms f USING(firm_id)
            WHERE f.guild_id=? AND i.quantity>0
            UNION
            SELECT l.product_id FROM firm_listings l JOIN firms f USING(firm_id)
            WHERE f.guild_id=? AND l.quantity>0
        """,(guild_id,guild_id)).fetchall()}

        # Process higher-numbered products first. If no company has any of that
        # product in its inventory or shop, the bot simulates making it and
        # creates demand for each required input.
        for row in sorted(CATALOG,key=lambda item:int(item["id"]),reverse=True):
            product_id=int(row["id"])
            quantity=total[product_id]
            if quantity<=0 or product_id in available:
                continue
            for group in recipe_for(row):
                for ingredient_id in group:
                    total[ingredient_id]=total.get(ingredient_id,0)+quantity

        shares_by_product={}
        for product_id,firm_id,share in db.execute("""
            SELECT product_id,firm_id,share FROM firm_market_shares
            WHERE guild_id=? AND market_date=?
        """,(guild_id,today)).fetchall():
            shares_by_product.setdefault(int(product_id),[]).append((int(firm_id),float(share)))

        # Upgrade an already-created daily snapshot without changing its firm
        # shares: recover those shares from the existing allocations and reset
        # its base quota to the new direct-demand values.
        if not shares_by_product:
            legacy=db.execute("""SELECT product_id,firm_id,allocated,purchased
                FROM firm_market_daily WHERE guild_id=? AND market_date=?""",(guild_id,today)).fetchall()
            grouped={}
            for product_id,firm_id,allocated,purchased in legacy:
                grouped.setdefault(int(product_id),[]).append((int(firm_id),int(allocated),int(purchased)))
            for product_id,items in grouped.items():
                allocation_total=sum(max(0,item[1]) for item in items)
                equal_share=1/len(items) if items else 0
                firms=[(fid,(max(0,allocated)/allocation_total if allocation_total else equal_share))
                       for fid,allocated,_ in items]
                shares_by_product[product_id]=firms
                for fid,share in firms:
                    db.execute("INSERT OR IGNORE INTO firm_market_shares VALUES(?,?,?,?,?)",
                               (guild_id,today,product_id,fid,share))
                demand=self._event_adjusted_demand(db,guild_id,product_id,base.get(product_id,0))
                allocations={fid:int(demand*share) for fid,share in firms}
                remainder=demand-sum(allocations.values())
                for fid,_ in sorted(firms,key=lambda item:demand*item[1]-allocations[item[0]],reverse=True)[:remainder]:
                    allocations[fid]+=1
                bonuses={}
                for fid,_ in firms:
                    ad=db.execute("SELECT bonus_percent FROM firm_ads WHERE firm_id=? AND ends_at>?",
                                  (fid,datetime.now(timezone.utc).isoformat())).fetchone()
                    sales_trust,price_trust=self._trust_bonus(db,fid,datetime.fromisoformat(today).date())
                    bonuses[fid]=base.get(product_id,0)*((int(ad[0]) if ad else 0)+sales_trust+price_trust)/100
                whole={fid:math.floor(value) for fid,value in bonuses.items()}
                remainder=max(0,math.floor(sum(bonuses.values())+0.5)-sum(whole.values()))
                for fid in sorted(firms,key=lambda item:bonuses[item[0]]-whole[item[0]],reverse=True)[:remainder]:
                    whole[fid]+=1
                for fid,_,purchased in items:
                    base_allocation=allocations[fid]+whole[fid]
                    db.execute("""UPDATE firm_market_daily SET base_allocated=?,allocated=?
                        WHERE guild_id=? AND market_date=? AND product_id=? AND firm_id=?""",
                        (base_allocation,max(purchased,base_allocation),guild_id,today,product_id,fid))

        supplement={}
        for product_id,firms in shares_by_product.items():
            base_after_event=self._event_adjusted_demand(db,guild_id,product_id,base.get(product_id,0))
            total_after_event=self._event_adjusted_demand(db,guild_id,product_id,total.get(product_id,0))
            amount=max(0,total_after_event-base_after_event)
            allocations={fid:int(amount*share) for fid,share in firms}
            remainder=amount-sum(allocations.values())
            for fid,_ in sorted(firms,key=lambda item:amount*item[1]-allocations[item[0]],reverse=True)[:remainder]:
                allocations[fid]+=1
            for fid,value in allocations.items():
                supplement[(product_id,fid)]=value

        market_rows=db.execute("""
            SELECT product_id,firm_id,purchased,base_allocated FROM firm_market_daily
            WHERE guild_id=? AND market_date=?
        """,(guild_id,today)).fetchall()
        for product_id,firm_id,purchased,base_allocated in market_rows:
            target=int(base_allocated)+supplement.get((int(product_id),int(firm_id)),0)
            db.execute("""UPDATE firm_market_daily SET allocated=?
                WHERE guild_id=? AND market_date=? AND product_id=? AND firm_id=?""",
                (max(int(purchased),target),guild_id,today,product_id,firm_id))

    def _create_market_snapshot(self, db, guild_id, now, today):
        """Freeze firm shares daily; simulated input demand stays dynamic during the day."""
        db.execute("INSERT INTO firm_settings(guild_id,last_buy_date) VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET last_buy_date=excluded.last_buy_date", (guild_id, today))
        announcements=self._roll_daily_event(db,guild_id,now)
        db.execute("DELETE FROM firm_active_events WHERE ends_at<=?",(datetime.now(timezone.utc).isoformat(),))
        db.execute("DELETE FROM firm_market_daily WHERE guild_id=?",(guild_id,))
        db.execute("DELETE FROM firm_market_shares WHERE guild_id=?",(guild_id,))
        cutoff=datetime.now(timezone.utc).timestamp()-7*86400
        active_rows=db.execute("""SELECT f.firm_id,f.owner_id,a.product_id,a.last_produced
            FROM firms f JOIN firm_activity a USING(firm_id) WHERE f.guild_id=?""",(guild_id,)).fetchall()
        active={}
        for firm_id,owner_id,pid,produced in active_rows:
            try: produced_at=datetime.fromisoformat(produced).timestamp()
            except (ValueError,TypeError): continue
            if produced_at>=cutoff: active.setdefault(pid,{})[firm_id]=owner_id
        for pid,firms in active.items():
            prices={}
            for fid in firms:
                offer=db.execute("SELECT MIN(price) FROM firm_listings WHERE firm_id=? AND product_id=? AND quantity>0",(fid,pid)).fetchone()[0]
                catalog=db.execute("SELECT catalog_price FROM firm_product_prices WHERE guild_id=? AND product_id=?",(guild_id,pid)).fetchone()
                prices[fid]=(float(catalog[0])-float(offer))/float(catalog[0])*100 if offer and catalog and catalog[0]>0 else 0.0
            raw={fid:max(0.0,100/len(firms)+prices[fid]-(sum(prices[o] for o in firms if o!=fid)/max(1,len(firms)-1))) for fid in firms}
            total_raw=sum(raw.values())
            shares={fid:(raw[fid]/total_raw if total_raw else 1/len(firms)) for fid in firms}
            for fid,share in shares.items():
                db.execute("INSERT INTO firm_market_shares VALUES(?,?,?,?,?)",(guild_id,today,pid,fid,share))

            demand=int(BY_ID.get(pid,{}).get("demand",0))
            demand=self._event_adjusted_demand(db,guild_id,pid,demand)
            allocations={fid:int(demand*shares[fid]) for fid in firms}
            remainder=demand-sum(allocations.values())
            for fid in sorted(firms,key=lambda x:demand*shares[x]-allocations[x],reverse=True)[:remainder]: allocations[fid]+=1
            bonuses={}
            for fid in firms:
                ad=db.execute("SELECT bonus_percent FROM firm_ads WHERE firm_id=? AND ends_at>?",(fid,datetime.now(timezone.utc).isoformat())).fetchone()
                sales_trust,price_trust=self._trust_bonus(db,fid,pid,now.date())
                bonuses[fid]=int(BY_ID[pid].get("demand",0))*((int(ad[0]) if ad else 0)+sales_trust+price_trust)/100
            whole={fid:math.floor(value) for fid,value in bonuses.items()}
            extra=max(0,math.floor(sum(bonuses.values())+0.5)-sum(whole.values()))
            for fid in sorted(firms,key=lambda x:bonuses[x]-whole[x],reverse=True)[:extra]: whole[fid]+=1
            for fid in firms:
                base_allocation=allocations[fid]+whole[fid]
                db.execute("""INSERT INTO firm_market_daily
                    (guild_id,market_date,product_id,firm_id,allocated,purchased,base_allocated)
                    VALUES(?,?,?,?,?,0,?)""",
                    (guild_id,today,pid,fid,base_allocation,base_allocation))
        self._refresh_supplemental_demand(db,guild_id,today)
        return announcements

    def _process_live_market(self, guild_id, today):
        """Buy currently listed products against today's frozen quotas."""
        bought=[]
        now=datetime.now(timezone.utc).isoformat()
        with connect() as db:
            self._refresh_supplemental_demand(db,guild_id,today)
            quotas=db.execute("""SELECT m.product_id,m.firm_id,m.allocated-m.purchased,f.owner_id,f.name
                FROM firm_market_daily m JOIN firms f USING(firm_id)
                WHERE m.guild_id=? AND m.market_date=? AND m.allocated>m.purchased
                ORDER BY m.product_id,f.firm_id""",(guild_id,today)).fetchall()
            for pid,fid,left,owner_id,firm_name in quotas:
                catalog=db.execute("SELECT catalog_price FROM firm_product_prices WHERE guild_id=? AND product_id=?",(guild_id,pid)).fetchone()
                if not catalog or int(catalog[0])<=0:
                    continue
                catalog_price=int(catalog[0])
                listings=db.execute("SELECT listing_id,quantity,price FROM firm_listings WHERE firm_id=? AND product_id=? AND quantity>0 ORDER BY price,listing_id",(fid,pid)).fetchall()
                total_qty=0; total_paid=0
                for listing_id,stock,price in listings:
                    # Reject offers that are 67% or more above the catalog price.
                    # Integer arithmetic avoids rounding errors at the threshold.
                    if price*100>=catalog_price*167:
                        continue
                    take=min(left,stock)
                    if take<=0: break
                    db.execute("UPDATE firm_listings SET quantity=quantity-? WHERE listing_id=?",(take,listing_id))
                    db.execute("INSERT OR IGNORE INTO economy_accounts(guild_id,user_id) VALUES(?,?)",(guild_id,owner_id))
                    seller_effects=self._effects_for_user(db,guild_id,owner_id)
                    payout=apply_company_income_bonus(take*price,seller_effects)
                    db.execute("UPDATE economy_accounts SET wallet=wallet+? WHERE guild_id=? AND user_id=?",(payout,guild_id,owner_id))
                    db.execute("UPDATE firm_market_daily SET purchased=purchased+? WHERE guild_id=? AND market_date=? AND product_id=? AND firm_id=?",(take,guild_id,today,pid,fid))
                    db.execute("UPDATE firm_activity SET last_sold=? WHERE firm_id=? AND product_id=?",(now,fid,pid))
                    db.execute("INSERT OR IGNORE INTO firm_sales_days VALUES(?,?,?)",(fid,pid,datetime.now(ZoneInfo("Europe/Warsaw")).date().isoformat()))
                    db.execute("DELETE FROM firm_listings WHERE listing_id=? AND quantity<=0",(listing_id,))
                    total_qty+=take; total_paid+=payout; left-=take
                if total_qty:
                    bought.append(f"• **{firm_name}** sold **{total_qty} × {BY_ID[pid]['product']}** for **{money(total_paid)}$**.")
        return "🛒 **Company market purchases**\n"+"\n".join(bought) if bought else ""

    async def _publish_market_log(self, guild_id, text):
        if not text:
            return
        with connect() as db:
            row=db.execute("SELECT event_channel_id FROM firm_settings WHERE guild_id=?",(guild_id,)).fetchone()
        channel=self.bot.get_channel(row[0]) if row and row[0] else None
        if channel is None and row and row[0]:
            try: channel=await self.bot.fetch_channel(row[0])
            except (discord.NotFound,discord.Forbidden,discord.HTTPException): channel=None
        if channel:
            for start in range(0,len(text),1800):
                try: await channel.send(text[start:start+1800])
                except discord.HTTPException: break

    def clear_firms(self, guild_id):
        """Delete server company data while preserving wallets and admin configuration."""
        with connect() as db:
            firm_ids=[row[0] for row in db.execute("SELECT firm_id FROM firms WHERE guild_id=?",(guild_id,)).fetchall()]
            count=len(firm_ids)
            if firm_ids:
                marks=','.join('?' for _ in firm_ids)
                for table in ("firm_inventory","firm_listings","firm_investments","firm_activity","firm_sales_days","firm_blocks","firm_ads"):
                    db.execute(f"DELETE FROM {table} WHERE firm_id IN ({marks})",firm_ids)
                db.execute(f"DELETE FROM firm_market_daily WHERE firm_id IN ({marks})",firm_ids)
                db.execute("DELETE FROM firms WHERE guild_id=?",(guild_id,))
            db.execute("DELETE FROM firm_market_daily WHERE guild_id=?",(guild_id,))
            db.execute("DELETE FROM firm_market_shares WHERE guild_id=?",(guild_id,))
            db.execute("DELETE FROM economy_inventory WHERE guild_id=? AND product_key LIKE 'firmmove:%'",(guild_id,))
        return count

    async def reset_firms_slash(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user,discord.Member) or not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("Only server administrators can reset companies.",ephemeral=True)
        view=FirmResetConfirmationView(self,interaction.guild_id,interaction.user.id)
        await interaction.response.send_message("⚠️ This will permanently delete every company, investment, listing, inventory, trust record, and unused company move on this server. Player wallets and admin settings will be kept. Confirm the reset?",view=view,ephemeral=True)

    async def reset_firms_prefix(self, ctx):
        if not ctx.guild or not ctx.author.guild_permissions.administrator:
            return await ctx.send("Only server administrators can reset companies.")
        view=FirmResetConfirmationView(self,ctx.guild.id,ctx.author.id)
        await ctx.send("⚠️ This will permanently delete every company, investment, listing, inventory, trust record, and unused company move on this server. Player wallets and admin settings will be kept. Confirm the reset?",view=view)

    def investment_catalog_pages(self):
        items=[]
        for row in CATALOG:
            recipe=row.get("recipe","") or "Raw material extraction"
            demand=int(row.get("demand",0))
            items.append(f"**#{row['id']} — {row['investment']}**\nProduct: {row['product']} • Inputs: {recipe} • Daily base demand: {demand}")
        chunks=[items[index:index+12] for index in range(0,len(items),12)] or [["No investments are configured."]]
        pages=[discord.Embed(title="🏭 Company investment catalog",description="\n\n".join(chunk),color=discord.Color.gold()) for chunk in chunks]
        for number,page in enumerate(pages,1):
            page.set_footer(text=f"Page {number}/{len(pages)} • Use the buttons to browse")
        return pages

    async def investments_slash(self, interaction: discord.Interaction):
        pages=self.investment_catalog_pages()
        view=FirmInvestmentsView(pages,interaction.user.id)
        await interaction.response.send_message(embed=pages[0],view=view)

    async def investments_prefix(self, ctx):
        pages=self.investment_catalog_pages()
        view=FirmInvestmentsView(pages,ctx.author.id)
        await ctx.send(embed=pages[0],view=view)

    def _roll_daily_event(self, db, guild_id, local_now):
        """One weighted event is rolled daily; active event effects overlap additively."""
        active=db.execute("SELECT event_id FROM firm_active_events WHERE guild_id=? AND ends_at>?",(guild_id,datetime.now(timezone.utc).isoformat())).fetchall()
        base={int(e["id"]):float(e["base_chance"]) for e in EVENTS}
        for (active_id,) in active:
            match=re.search(r"(.+?)\s+([+\-−])\s*(\d+)\s*pp",BY_EVENT[int(active_id)]["next"],re.I)
            if not match: continue
            target_name=match.group(1).strip().casefold(); delta=int(match.group(3))*(1 if match.group(2)=="+" else -1)
            target=next((e for e in EVENTS if e["name"].casefold()==target_name),None)
            if target:
                target_id=int(target["id"]); old=base[target_id]
                new=max(0.0,min(100.0,old+delta)); others=[i for i in base if i!=target_id]
                remainder=sum(base[i] for i in others)
                if remainder:
                    factor=(100.0-new)/remainder
                    for other in others: base[other]*=factor
                elif others:
                    equal=(100.0-new)/len(others)
                    for other in others: base[other]=equal
                base[target_id]=new
        event_id=random.choices(list(base),weights=list(base.values()),k=1)[0]
        duration=random.randint(1,7)
        started=datetime.now(timezone.utc); ends=started+timedelta(days=duration)
        db.execute("INSERT OR REPLACE INTO firm_active_events VALUES(?,?,?,?)",(guild_id,event_id,started.isoformat(),ends.isoformat()))
        setting=db.execute("SELECT event_channel_id FROM firm_settings WHERE guild_id=?",(guild_id,)).fetchone()
        if setting and setting[0]:
            event=BY_EVENT[event_id]
            return [(setting[0],f"📢 **Wydarzenie gospodarcze: {event['name']}**\n{event['effects']}\nCzas trwania: **{duration} dni**.")]
        return []

    def _trust_bonus(self, db, firm_id, product_id, today):
        days={r[0] for r in db.execute("SELECT sale_day FROM firm_sales_days WHERE firm_id=? AND product_id=? ORDER BY sale_day DESC LIMIT 7",(firm_id,product_id)).fetchall()}
        sales_streak=0
        for offset in range(7):
            if (today-timedelta(days=offset)).isoformat() in days: sales_streak+=1
            else: break
        row=db.execute("SELECT stable_price_since FROM firm_activity WHERE firm_id=? AND product_id=?",(firm_id,product_id)).fetchone()
        price_trust=0
        if row and row[0]:
            try: price_trust=int((datetime.now(timezone.utc)-datetime.fromisoformat(row[0])).total_seconds()>=7*86400)
            except ValueError: pass
        return (1 if sales_streak>=7 else 0), (1 if price_trust else 0)

    def _event_adjusted_demand(self, db, guild_id, product_id, demand):
        if demand<=0: return 0
        rows=db.execute("SELECT event_id FROM firm_active_events WHERE guild_id=? AND ends_at>?",(guild_id,datetime.now(timezone.utc).isoformat())).fetchall()
        change=0
        product=BY_ID[product_id]["product"].casefold()
        aliases={
          "pojazdy": [101,102,103,104,105,106,107],
          "sprzęt elektroniczny": [108,109,110,111,112,117,118,119,126],
          "sprzęt agd": [113,114,115,129],
          "maszyny budowlane": [106], "maszyny przemysłowe": [124,130],
          "sprzęt medyczny": [122], "aparatura laboratoryjna": [123],
          "wszystkie skupowane towary": list(BY_ID.keys()),
        }
        for (event_id,) in rows:
            text=BY_EVENT[int(event_id)]["effects"]
            import re
            for label,sign,num in re.findall(r"([^,]+?)\s+([+−-])\s*(\d+)%",text):
                label=label.strip().casefold(); amount=int(num)*(1 if sign=="+" else -1)
                targets=aliases.get(label)
                if targets is None:
                    targets=[int(x["id"]) for x in CATALOG if x["product"].casefold()==label]
                    if not targets and label=="stal": targets=[27,54,55,56]
                    if not targets and label=="aluminium": targets=[29,53]
                    if not targets and label=="miedź": targets=[28]
                if not targets and "/" in label:
                    for part in label.split("/"):
                        targets += [int(x["id"]) for x in CATALOG if part.strip() in x["product"].casefold()]
                if product_id in targets: change+=amount
        return max(0,round(demand*(100+change)/100))

    @daily_market_buy.before_loop
    async def before_daily_market_buy(self):
        await self.bot.wait_until_ready()

    def _group(self, name, description):
        group = self.bot.tree.get_command(name)
        if group is None:
            group = app_commands.Group(name=name, description=description)
            self.bot.tree.add_command(group)
            self.groups_created.append(name)
        if not isinstance(group, app_commands.Group):
            raise RuntimeError(f"/{name} jest już zajęte i nie jest grupą.")
        return group

    def _add(self, group, command):
        if group.get_command(command.name):
            raise RuntimeError(f"/{group.name} {command.name} jest już zajęte.")
        group.add_command(command)
        self.attached.append((group, command.name))

    def _firm(self, guild_id, owner_id, name):
        with connect() as db:
            if name:
                row = db.execute("SELECT firm_id,name FROM firms WHERE guild_id=? AND owner_id=? AND name=? COLLATE NOCASE", (guild_id, owner_id, name)).fetchone()
            else:
                row = db.execute("SELECT firm_id,name FROM firms WHERE guild_id=? AND owner_id=? ORDER BY firm_id LIMIT 1", (guild_id, owner_id)).fetchone()
        return row

    def _product(self, name):
        return next((x for x in CATALOG if x["product"].casefold() == name.strip().casefold()), None)

    def _parse_recipe(self, row):
        # Tree references resolve to investment numbers, whose product is the required stock.
        ids = recipe_for(row)
        # Ingredients written without a #number are represented by their exact product names.
        return ids

    async def create_firm(self, interaction, name: str):
        name = " ".join(name.split())
        if not name or len(name) > 80:
            return await interaction.response.send_message("Nazwa firmy musi mieć 1–80 znaków.", ephemeral=True)
        with connect() as db:
            try:
                db.execute("INSERT INTO firms(guild_id,owner_id,name,founded_at) VALUES(?,?,?,?)", (interaction.guild_id, interaction.user.id, name, datetime.now(timezone.utc).isoformat()))
            except Exception as exc:
                if "UNIQUE" in str(exc).upper():
                    return await interaction.response.send_message("Firma o tej nazwie już istnieje na serwerze.", ephemeral=True)
                raise
        await interaction.response.send_message(f"🏭 Utworzono firmę **{discord.utils.escape_markdown(name)}**.")

    async def invest(self, interaction, firm_name: str, investment_name: str):
        row = next((x for x in CATALOG if x["investment"].casefold() == investment_name.casefold()), None)
        if row is None:
            try: row = BY_ID.get(int(investment_name))
            except ValueError: pass
        firm = self._firm(interaction.guild_id, interaction.user.id, firm_name)
        if not firm: return await interaction.response.send_message("Nie znaleziono Twojej firmy o tej nazwie.", ephemeral=True)
        if not row: return await interaction.response.send_message("Nie znaleziono takiej inwestycji. Podaj jej pełną nazwę albo numer.", ephemeral=True)
        iid = int(row["id"])
        with connect() as db:
            blocked=db.execute("SELECT tree_name FROM firm_blocks WHERE firm_id=? AND ends_at>?",(firm[0],datetime.now(timezone.utc).isoformat())).fetchone()
            if blocked and tree_for(iid)==blocked[0]: return await interaction.response.send_message(f"Ta gałąź technologii firmy jest zablokowana: **{blocked[0]}**.",ephemeral=True)
            price_row = db.execute("SELECT price FROM firm_investment_prices WHERE guild_id=? AND investment_id=?", (interaction.guild_id, iid)).fetchone()
            if not price_row: return await interaction.response.send_message("Administracja nie ustawiła jeszcze ceny tej inwestycji.", ephemeral=True)
            price = int(price_row[0])
            db.execute("INSERT OR IGNORE INTO economy_accounts(guild_id,user_id) VALUES(?,?)", (interaction.guild_id, interaction.user.id))
            bal = db.execute("SELECT wallet FROM economy_accounts WHERE guild_id=? AND user_id=?", (interaction.guild_id, interaction.user.id)).fetchone()[0]
            effects=self._effects_for_user(db,interaction.guild_id,interaction.user.id)
            charged=discounted_company_price(price,effects)
            if db.execute("SELECT 1 FROM firm_investments WHERE firm_id=? AND investment_id=?", (firm[0],iid)).fetchone():
                return await interaction.response.send_message("Ta firma ma już tę inwestycję.", ephemeral=True)
            if bal < charged: return await interaction.response.send_message(f"Brakuje środków. Portfel: **{money(bal)}$**, cena po zniżce: **{money(charged)}$**.", ephemeral=True)
            db.execute("UPDATE economy_accounts SET wallet=wallet-? WHERE guild_id=? AND user_id=? AND wallet>=?", (charged,interaction.guild_id,interaction.user.id,charged))
            db.execute("INSERT INTO firm_investments VALUES(?,?,?,?)", (firm[0],iid,datetime.now(timezone.utc).isoformat(),charged))
        await interaction.response.send_message(f"✅ **{row['investment']}** kupione dla firmy **{firm[1]}** za **{money(charged)}$**.")

    async def produce(self, interaction, firm_name: str, product_name: str, quantity: int):
        firm=self._firm(interaction.guild_id,interaction.user.id,firm_name); row=self._product(product_name)
        if not firm: return await interaction.response.send_message("Nie znaleziono Twojej firmy.",ephemeral=True)
        if not row: return await interaction.response.send_message("Nie znaleziono produktu z katalogu.",ephemeral=True)
        if quantity < 1 or quantity > 10000: return await interaction.response.send_message("Ilość musi wynosić 1–10 000.",ephemeral=True)
        pid=int(row['id']); ingredient_groups=self._parse_recipe(row)
        with connect() as db:
            blocked=db.execute("SELECT tree_name FROM firm_blocks WHERE firm_id=? AND ends_at>?",(firm[0],datetime.now(timezone.utc).isoformat())).fetchone()
            if blocked and tree_for(pid)==blocked[0]: return await interaction.response.send_message(f"Produkcja w zablokowanej gałęzi **{blocked[0]}** jest wstrzymana.",ephemeral=True)
            price=db.execute("SELECT catalog_price FROM firm_product_prices WHERE guild_id=? AND product_id=?",(interaction.guild_id,pid)).fetchone()
            if not price: return await interaction.response.send_message("Nie ustawiono ceny katalogowej tego produktu.",ephemeral=True)
            if not ingredient_groups:
                owns_source=db.execute("SELECT 1 FROM firm_investments WHERE firm_id=? AND investment_id=?",(firm[0],pid)).fetchone()
                if not owns_source:
                    return await interaction.response.send_message("Ten surowiec nie ma składników do przetworzenia. Kup najpierw odpowiadającą mu inwestycję wydobywczą.",ephemeral=True)
            base_cost=math.ceil(int(price[0])*0.9)*quantity
            effects=self._effects_for_user(db,interaction.guild_id,interaction.user.id)
            cost=discounted_company_price(base_cost,effects)
            db.execute("INSERT OR IGNORE INTO economy_accounts(guild_id,user_id) VALUES(?,?)",(interaction.guild_id,interaction.user.id))
            bal=db.execute("SELECT wallet FROM economy_accounts WHERE guild_id=? AND user_id=?",(interaction.guild_id,interaction.user.id)).fetchone()[0]
            if bal<cost: return await interaction.response.send_message(f"Produkcja kosztuje **{money(cost)}$**, a portfel ma **{money(bal)}$**.",ephemeral=True)
            ingredients=[]
            for alternatives in ingredient_groups:
                selected=None
                for ingredient in alternatives:
                    available=db.execute("SELECT quantity FROM firm_inventory WHERE firm_id=? AND product_id=?",(firm[0],ingredient)).fetchone()
                    if available and available[0]>=quantity:
                        selected=ingredient; break
                if selected is None:
                    labels=" lub ".join(BY_ID[i]['product'] for i in alternatives)
                    return await interaction.response.send_message(f"Brakuje składnika **{labels}**: potrzeba {quantity} szt. jednej z opcji.",ephemeral=True)
                ingredients.append(selected)
            db.execute("UPDATE economy_accounts SET wallet=wallet-? WHERE guild_id=? AND user_id=? AND wallet>=?",(cost,interaction.guild_id,interaction.user.id,cost))
            for ingredient in ingredients: db.execute("UPDATE firm_inventory SET quantity=quantity-? WHERE firm_id=? AND product_id=?",(quantity,firm[0],ingredient))
            db.execute("INSERT INTO firm_inventory VALUES(?,?,?) ON CONFLICT(firm_id,product_id) DO UPDATE SET quantity=quantity+excluded.quantity",(firm[0],pid,quantity))
            now=datetime.now(timezone.utc).isoformat()
            db.execute("INSERT INTO firm_activity VALUES(?,?,?,?,?,?) ON CONFLICT(firm_id,product_id) DO UPDATE SET last_produced=excluded.last_produced",(firm[0],pid,now,"",now,0))
        await interaction.response.send_message(f"🏭 Wyprodukowano **{quantity} × {row['product']}**. Koszt: **{money(cost)}$**.")

    async def admin_price(self, interaction, kind: str, item: str, price: int):
        if not interaction.user.guild_permissions.administrator: return await interaction.response.send_message("Tylko administracja może zmieniać ceny.",ephemeral=True)
        row=next((x for x in CATALOG if x['investment'].casefold()==item.casefold() or x['product'].casefold()==item.casefold()),None)
        if row is None:
            try: row=BY_ID.get(int(item))
            except ValueError: pass
        if not row or price<1: return await interaction.response.send_message("Podaj poprawną nazwę/numer i cenę większą od zera.",ephemeral=True)
        table='firm_investment_prices' if kind=='investment' else 'firm_product_prices'
        col='investment_id' if kind=='investment' else 'product_id'; val='price' if kind=='investment' else 'catalog_price'
        with connect() as db: db.execute(f"INSERT INTO {table}(guild_id,{col},{val}) VALUES(?,?,?) ON CONFLICT(guild_id,{col}) DO UPDATE SET {val}=excluded.{val}",(interaction.guild_id,row['id'],price))
        await interaction.response.send_message(f"✅ Ustawiono cenę **{row['investment'] if kind=='investment' else row['product']}**: **{money(price)}$**.",ephemeral=True)

    async def buy_listing(self, interaction, listing_id: int, buyer_firm_name: str):
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            listing=db.execute("SELECT l.firm_id,l.product_id,l.quantity,l.price,f.owner_id,f.name FROM firm_listings l JOIN firms f USING(firm_id) WHERE l.listing_id=? AND f.guild_id=?",(listing_id,interaction.guild_id)).fetchone()
            if not listing: return await interaction.response.send_message("Oferta już nie istnieje.",ephemeral=True)
            fid,pid,qty,price,seller,name=listing
            if seller==interaction.user.id: return await interaction.response.send_message("Nie możesz kupić własnej oferty.",ephemeral=True)
            buyer_firm=db.execute("SELECT firm_id FROM firms WHERE guild_id=? AND owner_id=? AND name=? COLLATE NOCASE",(interaction.guild_id,interaction.user.id,buyer_firm_name)).fetchone()
            if not buyer_firm: return await interaction.response.send_message("Nie znaleziono Twojej firmy o podanej nazwie.",ephemeral=True)
            db.execute("INSERT OR IGNORE INTO economy_accounts(guild_id,user_id) VALUES(?,?)",(interaction.guild_id,interaction.user.id))
            bal=db.execute("SELECT wallet FROM economy_accounts WHERE guild_id=? AND user_id=?",(interaction.guild_id,interaction.user.id)).fetchone()[0]
            buyer_effects=self._effects_for_user(db,interaction.guild_id,interaction.user.id)
            charged=discounted_company_price(price,buyer_effects)
            seller_effects=self._effects_for_user(db,interaction.guild_id,seller)
            seller_income=apply_company_income_bonus(charged,seller_effects)
            if qty<1 or bal<charged: return await interaction.response.send_message("Brak towaru albo środków w portfelu.",ephemeral=True)
            db.execute("UPDATE economy_accounts SET wallet=wallet-? WHERE guild_id=? AND user_id=? AND wallet>=?",(charged,interaction.guild_id,interaction.user.id,charged))
            db.execute("UPDATE economy_accounts SET wallet=wallet+? WHERE guild_id=? AND user_id=?",(seller_income,interaction.guild_id,seller))
            db.execute("UPDATE firm_listings SET quantity=quantity-1 WHERE listing_id=?",(listing_id,))
            db.execute("INSERT INTO firm_inventory(firm_id,product_id,quantity) VALUES(?,?,1) ON CONFLICT(firm_id,product_id) DO UPDATE SET quantity=quantity+1",(buyer_firm[0],pid))
            now=datetime.now(timezone.utc).isoformat()
            db.execute("INSERT INTO firm_activity VALUES(?,?,?,?,?,?) ON CONFLICT(firm_id,product_id) DO UPDATE SET last_sold=excluded.last_sold",(fid,pid,"",now,now,0))
            db.execute("INSERT OR IGNORE INTO firm_sales_days VALUES(?,?,?)",(fid,pid,datetime.now(ZoneInfo("Europe/Warsaw")).date().isoformat()))
            db.execute("DELETE FROM firm_listings WHERE listing_id=? AND quantity<=0",(listing_id,))
        await interaction.response.send_message(f"✅ Kupiono **{BY_ID[pid]['product']}** od firmy **{name}** za **{money(charged)}$**.")

    async def make_shop(self, interaction, firm_name: str):
        with connect() as db:
            firm=db.execute("SELECT firm_id,name,owner_id,founded_at FROM firms WHERE guild_id=? AND name=? COLLATE NOCASE",(interaction.guild_id,firm_name)).fetchone()
            if not firm: return await interaction.response.send_message("Nie znaleziono firmy o tej nazwie.",ephemeral=True)
            offers=db.execute("SELECT listing_id,product_id,quantity,price FROM firm_listings WHERE firm_id=? AND quantity>0 ORDER BY listing_id",(firm[0],)).fetchall()
            inv=db.execute("SELECT product_id,quantity FROM firm_inventory WHERE firm_id=? AND quantity>0",(firm[0],)).fetchall()
            value=db.execute("SELECT COALESCE(SUM(purchase_price),0) FROM firm_investments WHERE firm_id=?",(firm[0],)).fetchone()[0]
            investments=db.execute("SELECT investment_id FROM firm_investments WHERE firm_id=? ORDER BY investment_id",(firm[0],)).fetchall()
        embed=discord.Embed(title=f"🏭 {firm[1]}",description=f"Właściciel: <@{firm[2]}>\nZałożono: {firm[3][:10]}\nWartość inwestycji: {money(value)}$\nMagazyn: {len(inv)} rodzajów towarów",color=discord.Color.gold())
        embed.add_field(name="Technologie i inwestycje",value=(", ".join(BY_ID[iid]["investment"] for (iid,) in investments)[:1024] if investments else "Brak"),inline=False)
        view=FirmShopView(self,offers) if offers else None
        for listing_id,pid,qty,price in offers[:20]: embed.add_field(name=BY_ID[pid]['product'],value=f"{qty} szt. • {money(price)}$ za szt. • oferta `{listing_id}`",inline=False)
        await interaction.response.send_message(embed=embed,view=view)

    async def sell_listing(self, interaction, firm_name: str, product_name: str, quantity: int, price: int):
        firm=self._firm(interaction.guild_id,interaction.user.id,firm_name); row=self._product(product_name)
        if not firm or not row: return await interaction.response.send_message("Nie znaleziono Twojej firmy albo produktu.",ephemeral=True)
        if quantity<1 or price<1: return await interaction.response.send_message("Ilość i cena muszą być większe od zera.",ephemeral=True)
        pid=int(row['id'])
        with connect() as db:
            stock=db.execute("SELECT quantity FROM firm_inventory WHERE firm_id=? AND product_id=?",(firm[0],pid)).fetchone()
            if not stock or stock[0]<quantity: return await interaction.response.send_message("Firma nie ma takiej ilości w magazynie.",ephemeral=True)
            db.execute("UPDATE firm_inventory SET quantity=quantity-? WHERE firm_id=? AND product_id=?",(quantity,firm[0],pid))
            db.execute("INSERT INTO firm_listings(firm_id,product_id,quantity,price,created_at) VALUES(?,?,?,?,?)",(firm[0],pid,quantity,price,datetime.now(timezone.utc).isoformat()))
            now=datetime.now(timezone.utc).isoformat()
            activity=db.execute("SELECT stable_price,stable_price_since FROM firm_activity WHERE firm_id=? AND product_id=?",(firm[0],pid)).fetchone()
            stable_since=activity[1] if activity and activity[0]==price else now
            db.execute("INSERT INTO firm_activity VALUES(?,?,?,?,?,?) ON CONFLICT(firm_id,product_id) DO UPDATE SET stable_price=excluded.stable_price,stable_price_since=excluded.stable_price_since",(firm[0],pid,"",now,stable_since,price))
        await interaction.response.send_message(f"Offer listed: **{quantity} × {row['product']}** at **{money(price)}$** each. The company market will buy available demand automatically.")
        today=datetime.now(ZoneInfo("Europe/Warsaw")).date().isoformat()
        await self._publish_market_log(interaction.guild_id,self._process_live_market(interaction.guild_id,today))

    async def inventory(self, interaction, firm_name: str):
        firm=self._firm(interaction.guild_id,interaction.user.id,firm_name)
        if not firm: return await interaction.response.send_message("Nie znaleziono Twojej firmy.",ephemeral=True)
        with connect() as db: rows=db.execute("SELECT product_id,quantity FROM firm_inventory WHERE firm_id=? AND quantity>0 ORDER BY product_id",(firm[0],)).fetchall()
        await interaction.response.send_message("📦 Magazyn **"+firm[1]+"**:\n"+("\n".join(f"• {BY_ID[p]['product']}: {q}" for p,q in rows) if rows else "Pusty."),ephemeral=True)

    async def share(self, interaction, firm_name: str):
        firm=self._firm(interaction.guild_id,interaction.user.id,firm_name)
        if not firm: return await interaction.response.send_message("Nie znaleziono Twojej firmy.",ephemeral=True)
        with connect() as db:
            rows=db.execute("SELECT DISTINCT l.product_id,f.firm_id FROM firm_listings l JOIN firms f USING(firm_id) WHERE f.guild_id=? AND l.quantity>0 ORDER BY l.product_id",(interaction.guild_id,)).fetchall()
            price_rows=db.execute("SELECT l.product_id,l.firm_id,MIN(l.price) FROM firm_listings l JOIN firms f USING(firm_id) WHERE f.guild_id=? AND l.quantity>0 GROUP BY l.product_id,l.firm_id",(interaction.guild_id,)).fetchall()
        groups={}; prices={}
        for pid,fid in rows: groups.setdefault(pid,set()).add(fid)
        for pid,fid,price in price_rows: prices[(pid,fid)]=price
        vals=[]
        for pid,firms in groups.items():
            if firm[0] not in firms: continue
            catalog_row=next((x for x in CATALOG if int(x['id'])==pid),None)
            with connect() as db: catalog=db.execute("SELECT catalog_price FROM firm_product_prices WHERE guild_id=? AND product_id=?",(interaction.guild_id,pid)).fetchone()
            catalog_price=catalog[0] if catalog else 0
            bonuses={fid:((catalog_price-prices.get((pid,fid),catalog_price))/catalog_price*100 if catalog_price else 0) for fid in firms}
            raw={fid:max(0,100/len(firms)+bonuses[fid]-(sum(bonuses[o] for o in firms if o!=fid)/max(1,len(firms)-1))) for fid in firms}
            weight=sum(raw.values()); share=raw[firm[0]]/weight*100 if weight else 100/len(firms)
            vals.append(f"• {BY_ID[pid]['product']}: **{share:.1f}%** ({len(firms)} firm)")
        await interaction.response.send_message("📊 Udziały **"+firm[1]+"**:\n"+("\n".join(vals) if vals else "Firma nie ma jeszcze wystawionych produktów."),ephemeral=True)

    async def top_firms(self, interaction):
        with connect() as db:
            rows=db.execute("SELECT f.firm_id,f.name,f.owner_id,COALESCE(SUM(i.purchase_price),0) value FROM firms f LEFT JOIN firm_investments i USING(firm_id) WHERE f.guild_id=? GROUP BY f.firm_id ORDER BY value DESC LIMIT 10",(interaction.guild_id,)).fetchall()
        await interaction.response.send_message("🏆 **Najbogatsze firmy**\n"+("\n".join(f"{i}. **{n}** — {money(v)}$ (<@{o}>)" for i,(_,n,o,v) in enumerate(rows,1)) if rows else "Nie utworzono jeszcze firm."))

    async def event_channel_slash(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("Tylko administrator może ustawić kanał wydarzeń.",ephemeral=True)
        with connect() as db:
            db.execute("INSERT INTO firm_settings(guild_id,event_channel_id) VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET event_channel_id=excluded.event_channel_id",(interaction.guild_id,channel.id))
        await interaction.response.send_message(f"✅ Wydarzenia firm będą ogłaszane na {channel.mention}.",ephemeral=True)

    async def special_move_slash(self, interaction: discord.Interaction, move_name: str, price: app_commands.Range[int,1]):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("Tylko administrator może ustawić ruch specjalny.",ephemeral=True)
        allowed={"blokada technologiczna":"blokada","reklama":"reklama"}
        key=allowed.get(move_name.casefold())
        if not key: return await interaction.response.send_message("Ruch to **Blokada technologiczna** albo **Reklama**.",ephemeral=True)
        with connect() as db:
            db.execute("INSERT INTO firm_moves VALUES(?,?,?,?) ON CONFLICT(guild_id,move_key) DO UPDATE SET move_name=excluded.move_name,price=excluded.price",(interaction.guild_id,key,move_name,price))
            db.execute("INSERT INTO economy_server_shop(guild_id,product_key,product_type,product_name,role_id,price,description,created_at) VALUES(?,?,'item',?,NULL,?,?,?) ON CONFLICT(guild_id,product_key) DO UPDATE SET product_name=excluded.product_name,price=excluded.price,description=excluded.description",(interaction.guild_id,f"firmmove:{key}",move_name,price,"Ruch specjalny firmy. Kup tutaj, a następnie użyj /open ruch.",datetime.now(timezone.utc).isoformat()))
        await interaction.response.send_message(f"✅ **{move_name}** dodano do sklepu serwerowego za **{money(price)}$**.",ephemeral=True)

    async def open_move_slash(self, interaction: discord.Interaction, move_name: str, firm_name: str, tree_name: str = "Przetwarzanie", duration: str = "tydzień"):
        if not isinstance(interaction.user,discord.Member): return await interaction.response.send_message("Użyj tej komendy na serwerze.",ephemeral=True)
        key="blokada" if move_name.casefold().startswith("blokada") else "reklama" if move_name.casefold().startswith("reklama") else None
        if not key: return await interaction.response.send_message("Nieznany ruch specjalny.",ephemeral=True)
        with connect() as db:
            move=db.execute("SELECT 1 FROM firm_moves WHERE guild_id=? AND move_key=?",(interaction.guild_id,key)).fetchone()
            if not move: return await interaction.response.send_message("Tego ruchu nie ma w sklepie serwerowym.",ephemeral=True)
            item=db.execute("SELECT quantity FROM economy_inventory WHERE guild_id=? AND user_id=? AND product_key=?",(interaction.guild_id,interaction.user.id,f"firmmove:{key}")).fetchone()
            if not item or item[0]<1: return await interaction.response.send_message("Nie masz tego ruchu w ekwipunku. Kup go w sklepie serwerowym.",ephemeral=True)
            if key=="blokada":
                target=db.execute("SELECT firm_id,owner_id FROM firms WHERE guild_id=? AND name=? COLLATE NOCASE",(interaction.guild_id,firm_name)).fetchone()
                if not target: return await interaction.response.send_message("Nie znaleziono firmy docelowej.",ephemeral=True)
                if target[1]==interaction.user.id: return await interaction.response.send_message("Blokady użyj na firmie innego gracza.",ephemeral=True)
                if tree_name not in {"Wydobycie","Przetwarzanie","Komponenty","Gotowe produkty"}:
                    return await interaction.response.send_message("Wybierz drzewko: Wydobycie, Przetwarzanie, Komponenty albo Gotowe produkty.",ephemeral=True)
                existing=db.execute("SELECT 1 FROM firm_blocks WHERE firm_id=? AND ends_at>?",(target[0],datetime.now(timezone.utc).isoformat())).fetchone()
                if existing: return await interaction.response.send_message("Ta firma ma już aktywną blokadę technologiczną.",ephemeral=True)
                days=30 if duration.casefold()=="miesiąc" else 7
                ends=(datetime.now(timezone.utc)+timedelta(days=days)).isoformat()
                db.execute("INSERT OR REPLACE INTO firm_blocks VALUES(?,?,?,?)",(target[0],tree_name,ends,interaction.user.id))
                result=f"🔒 Firma **{firm_name}** ma zablokowaną gałąź **{tree_name}** na {days} dni."
            else:
                own=db.execute("SELECT firm_id FROM firms WHERE guild_id=? AND owner_id=? AND name=? COLLATE NOCASE",(interaction.guild_id,interaction.user.id,firm_name)).fetchone()
                if not own: return await interaction.response.send_message("Reklamować możesz tylko własną firmę.",ephemeral=True)
                success=random.random()<0.5
                if success:
                    ends=(datetime.now(timezone.utc)+timedelta(days=7)).isoformat()
                    db.execute("INSERT OR REPLACE INTO firm_ads VALUES(?,?,?)",(own[0],25,ends))
                    result=f"📣 Reklama firmy **{firm_name}** się udała! Popyt wzrasta o **25%** przez 7 dni."
                else: result=f"📣 Reklama firmy **{firm_name}** nie przyniosła efektu."
            db.execute("UPDATE economy_inventory SET quantity=quantity-1 WHERE guild_id=? AND user_id=? AND product_key=?",(interaction.guild_id,interaction.user.id,f"firmmove:{key}"))
        if isinstance(interaction,discord.Interaction):
            await interaction.response.defer(ephemeral=True,thinking=True)
            message=await interaction.followup.send("🎲 Losowanie ruchu specjalnego…",ephemeral=True,wait=True)
        else:
            message=await interaction.followup.send("🎲 Losowanie ruchu specjalnego…")
        for frame in ("🎲 ◉ ○ ○", "🎲 ○ ◉ ○", "🎲 ○ ○ ◉", "🎲 ◉ ◉ ○"):
            await message.edit(content=frame)
            await asyncio.sleep(0.35)
        await message.edit(content=result)

    async def create_firm_slash(self, interaction: discord.Interaction, name: str): await self.create_firm(interaction,name)
    async def invest_slash(self, interaction: discord.Interaction, firm: str, investment: str): await self.invest(interaction,firm,investment)
    async def produce_slash(self, interaction: discord.Interaction, firm: str, product: str, quantity: app_commands.Range[int,1,10000]): await self.produce(interaction,firm,product,quantity)
    async def investment_price_slash(self, interaction: discord.Interaction, investment: str, price: app_commands.Range[int,1]): await self.admin_price(interaction,'investment',investment,price)
    async def catalog_price_slash(self, interaction: discord.Interaction, product: str, price: app_commands.Range[int,1]): await self.admin_price(interaction,'product',product,price)
    async def shop_slash(self, interaction: discord.Interaction, firm: str): await self.make_shop(interaction,firm)
    async def sell_slash(self, interaction: discord.Interaction, firm: str, product: str, quantity: app_commands.Range[int,1], price: app_commands.Range[int,1]): await self.sell_listing(interaction,firm,product,quantity,price)
    async def inventory_slash(self, interaction: discord.Interaction, firm: str): await self.inventory(interaction,firm)
    async def share_slash(self, interaction: discord.Interaction, firm: str): await self.share(interaction,firm)
    async def top_slash(self, interaction: discord.Interaction): await self.top_firms(interaction)
    async def event_channel_command(self, interaction: discord.Interaction, channel: discord.TextChannel): await self.event_channel_slash(interaction,channel)
    async def special_move_command(self, interaction: discord.Interaction, name: str, price: app_commands.Range[int,1]): await self.special_move_slash(interaction,name,price)
    async def open_move_command(self, interaction: discord.Interaction, move: str, firm: str, tree: str = "Przetwarzanie", duration: str = "tydzień"): await self.open_move_slash(interaction,move,firm,tree,duration)
    async def reset_firms_command(self, interaction: discord.Interaction): await self.reset_firms_slash(interaction)

    @commands.command(name="invest")
    async def invest_prefix(self,ctx,firm_name:str,*,investment:str):
        await self._prefix_call(ctx,self.invest,firm_name,investment)
    @commands.command(name="produce")
    async def produce_prefix(self,ctx,firm_name:str,product:str,quantity:int):
        await self._prefix_call(ctx,self.produce,firm_name,product,quantity)
    @commands.command(name="share")
    async def share_prefix(self,ctx,firm_name:str): await self._prefix_call(ctx,self.share,firm_name)
    @commands.command(name="topcompanies")
    async def top_prefix(self,ctx): await self._prefix_call(ctx,self.top_firms)
    @commands.command(name="companyshop")
    async def shop_prefix(self,ctx,*,firm_name:str): await self._prefix_call(ctx,self.make_shop,firm_name)
    @commands.command(name="companysell")
    async def sell_prefix(self,ctx,firm_name:str,product:str,quantity:int,price:int): await self._prefix_call(ctx,self.sell_listing,firm_name,product,quantity,price)
    @commands.command(name="companyinventory")
    async def inventory_prefix(self,ctx,*,firm_name:str): await self._prefix_call(ctx,self.inventory,firm_name)
    @commands.command(name="investments")
    async def investments_prefix_command(self,ctx): await self.investments_prefix(ctx)
    async def _prefix_call(self,ctx,func,*args):
        class Reply:
            guild_id=ctx.guild.id; user=ctx.author
            async def response_send(_,content=None,**kwargs): await ctx.send(content,**kwargs)
        # Adapt Discord's interaction response for the shared handlers.
        proxy=Reply()
        class Response:
            async def send_message(_,content=None,**kwargs):
                kwargs.pop("ephemeral",None)
                await ctx.send(content,**kwargs)
        class Followup:
            async def send(_,content=None,**kwargs):
                kwargs.pop("ephemeral",None)
                await ctx.send(content,**kwargs)
        proxy.response=Response()
        proxy.followup=Followup()
        await func(proxy,*args)

    @commands.group(name="create",invoke_without_command=True)
    async def create_prefix(self,ctx):
        await ctx.send("Usage: !create company <name> | !create investment <name or ID> <price> | !create catalog-price <product or ID> <price>")

    @create_prefix.command(name="company")
    async def create_firm_prefix(self,ctx,*,name:str):
        await self._prefix_call(ctx,self.create_firm,name)

    @create_prefix.command(name="investment")
    async def investment_price_prefix(self,ctx,item:str,price:int):
        await self._prefix_call(ctx,self.admin_price,"investment",item,price)

    @create_prefix.command(name="catalog-price")
    async def catalog_price_prefix(self,ctx,item:str,price:int):
        await self._prefix_call(ctx,self.admin_price,"product",item,price)

    @create_prefix.group(name="info",invoke_without_command=True)
    async def create_info_prefix(self,ctx): await ctx.send("Usage: !create info events <channel>")

    @create_info_prefix.command(name="events")
    async def event_channel_prefix(self,ctx,channel:discord.TextChannel): await self._prefix_call(ctx,self.event_channel_slash,channel)

    @create_prefix.group(name="special",invoke_without_command=True)
    async def create_move_prefix(self,ctx): await ctx.send("Usage: !create special move <Technology blockade/Advertising> <price>")

    @create_move_prefix.command(name="move")
    async def special_move_prefix(self,ctx,*,arguments:str):
        try: name,price=arguments.rsplit(" ",1); price=int(price)
        except ValueError: return await ctx.send("Provide a move name and price, e.g. !create special move Advertising 5000")
        await self._prefix_call(ctx,self.special_move_slash,name,price)

    @commands.command(name="usemove")
    async def open_move_prefix(self,ctx,move:str,firm_name:str,tree_name:str="Przetwarzanie",duration:str="tydzień"):
        await self._prefix_call(ctx,self.open_move_slash,move,firm_name,tree_name,duration)

    @commands.group(name="company",invoke_without_command=True)
    async def company_prefix(self,ctx): await ctx.send("Use !company create <name>, !company shop <name>, !company sell <company> <product> <quantity> <price>, or !company inventory <name>.")

    @company_prefix.command(name="create")
    async def company_create_prefix(self,ctx,*,name:str): await self._prefix_call(ctx,self.create_firm,name)

    @company_prefix.command(name="shop")
    async def company_shop_prefix(self,ctx,*,firm_name:str): await self._prefix_call(ctx,self.make_shop,firm_name)

    @company_prefix.command(name="sell")
    async def company_sell_prefix(self,ctx,firm_name:str,product:str,quantity:int,price:int): await self._prefix_call(ctx,self.sell_listing,firm_name,product,quantity,price)

    @company_prefix.command(name="inventory")
    async def company_inventory_prefix(self,ctx,*,firm_name:str): await self._prefix_call(ctx,self.inventory,firm_name)

class FirmShopView(discord.ui.View):
    def __init__(self,cog,offers):
        super().__init__(timeout=300); self.cog=cog; self.offers=offers[:25]
        options=[discord.SelectOption(label=BY_ID[pid]['product'][:100],description=f"{qty} szt. • {money(price)}$"[:100],value=str(lid)) for lid,pid,qty,price in self.offers]
        self.add_item(FirmShopSelect(self,options))


class FirmResetConfirmationView(discord.ui.View):
    def __init__(self,cog,guild_id,admin_id):
        super().__init__(timeout=60)
        self.cog=cog; self.guild_id=guild_id; self.admin_id=admin_id

    async def interaction_check(self,interaction):
        if interaction.user.id!=self.admin_id:
            await interaction.response.send_message("Only the administrator who started this reset can confirm it.",ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm reset",style=discord.ButtonStyle.danger)
    async def confirm_reset(self,interaction: discord.Interaction,button: discord.ui.Button):
        count=self.cog.clear_firms(self.guild_id)
        for child in self.children: child.disabled=True
        await interaction.response.edit_message(content=f"✅ Company data reset complete. Deleted **{count}** companies. Player wallets and admin settings were kept.",view=self)

    @discord.ui.button(label="Cancel",style=discord.ButtonStyle.secondary)
    async def cancel_reset(self,interaction: discord.Interaction,button: discord.ui.Button):
        for child in self.children: child.disabled=True
        await interaction.response.edit_message(content="Company reset cancelled.",view=self)


class FirmInvestmentsView(discord.ui.View):
    def __init__(self,pages,owner_id):
        super().__init__(timeout=180)
        self.pages=pages; self.page_index=0; self.owner_id=owner_id
        for button in self.children:
            button.disabled=len(self.pages)<=1

    async def interaction_check(self,interaction):
        if interaction.user.id!=self.owner_id:
            await interaction.response.send_message("Only the person who opened this catalog can change its page.",ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Previous",style=discord.ButtonStyle.secondary)
    async def previous(self,interaction: discord.Interaction,button: discord.ui.Button):
        self.page_index=(self.page_index-1)%len(self.pages)
        await interaction.response.edit_message(embed=self.pages[self.page_index],view=self)

    @discord.ui.button(label="Next",style=discord.ButtonStyle.primary)
    async def next(self,interaction: discord.Interaction,button: discord.ui.Button):
        self.page_index=(self.page_index+1)%len(self.pages)
        await interaction.response.edit_message(embed=self.pages[self.page_index],view=self)


class FirmBuyModal(discord.ui.Modal, title="Zakup do firmy"):
    firm_name=discord.ui.TextInput(label="Nazwa Twojej firmy",max_length=80)
    def __init__(self,cog,listing_id): super().__init__(); self.cog=cog; self.listing_id=listing_id
    async def on_submit(self,interaction): await self.cog.buy_listing(interaction,self.listing_id,str(self.firm_name))


class FirmShopSelect(discord.ui.Select):
    def __init__(self,view,options): self.shop=view; super().__init__(placeholder="Wybierz ofertę do kupienia",min_values=1,max_values=1,options=options)
    async def callback(self,interaction): await interaction.response.send_modal(FirmBuyModal(self.shop.cog,int(self.values[0])))


async def setup(bot: commands.Bot):
    init_firm_tables()
    cog=Firmy(bot); await bot.add_cog(cog)
    create=cog._group('create','Create companies and configure the company system.')
    top=cog._group('top','Company rankings.')
    cog._add(create,app_commands.Command(name='company',description='Create a company.',callback=cog.create_firm_slash))
    cog._add(create,app_commands.Command(name='investment',description='Set an investment price (admin).',callback=cog.investment_price_slash))
    cog._add(create,app_commands.Command(name='catalog-price',description='Set a product catalog price (admin).',callback=cog.catalog_price_slash))
    info=create.get_command('info')
    if info is None:
        info=app_commands.Group(name='info',description='Company event announcements.')
        create.add_command(info); cog.attached.append((create,'info'))
    if not isinstance(info,app_commands.Group):
        raise RuntimeError("/create info exists but is not a command group; company event settings cannot be added.")
    cog._add(info,app_commands.Command(name='events',description='Set the event announcement channel.',callback=cog.event_channel_command))
    special=app_commands.Group(name='special',description='Company special moves.')
    create.add_command(special); cog.attached.append((create,'special'))
    cog._add(special,app_commands.Command(name='move',description='Add a company move to the server shop.',callback=cog.special_move_command))
    cog._add(top,app_commands.Command(name='companies',description='Company value ranking.',callback=cog.top_slash))
    company=app_commands.Group(name='company',description='Company shop and inventory.')
    bot.tree.add_command(company); cog.groups_created.append('company')
    cog._add(company,app_commands.Command(name='shop',description='View a company shop.',callback=cog.shop_slash))
    cog._add(company,app_commands.Command(name='sell',description='List company inventory for sale.',callback=cog.sell_slash))
    cog._add(company,app_commands.Command(name='inventory',description='View company inventory.',callback=cog.inventory_slash))
    cog._add(company,app_commands.Command(name='share',description='Show company market shares.',callback=cog.share_slash))
    cog._add(company,app_commands.Command(name='create',description='Create a company.',callback=cog.create_firm_slash))
    cog._add(bot.tree,app_commands.Command(name='invest',description='Buy an investment for your company.',callback=cog.invest_slash))
    cog._add(bot.tree,app_commands.Command(name='investments',description='Browse all company investments and products.',callback=cog.investments_slash))
    cog._add(bot.tree,app_commands.Command(name='produce',description='Produce a product for your company.',callback=cog.produce_slash))
    cog._add(bot.tree,app_commands.Command(name='share',description='Show your company market shares.',callback=cog.share_slash))
    open_group=cog._group('open','Use a purchased company special move.')
    cog._add(open_group,app_commands.Command(name='special',description='Use a purchased company move.',callback=cog.open_move_command))
    reset_root=bot.tree.get_command('reset')
    if reset_root is None or isinstance(reset_root,app_commands.Group):
        reset_group=cog._group('reset','Administrative reset tools.')
        if reset_group.get_command('firms') is None:
            cog._add(reset_group,app_commands.Command(name='firms',description='Delete all company data on this server (admin).',callback=cog.reset_firms_command))
        else:
            cog._add(bot.tree,app_commands.Command(name='reset-firms',description='Delete all company data on this server (admin).',callback=cog.reset_firms_command))
    else:
        cog._add(bot.tree,app_commands.Command(name='reset-firms',description='Delete all company data on this server (admin).',callback=cog.reset_firms_command))
    async def reset_firms_prefix_callback(ctx):
        await cog.reset_firms_prefix(ctx)

    reset_prefix=bot.get_command('reset')
    if reset_prefix is None:
        async def reset_prefix_help(ctx):
            await ctx.send('Usage: !reset firms')
        reset_prefix=commands.Group(reset_prefix_help,name='reset',invoke_without_command=True)
        bot.add_command(reset_prefix); cog.prefix_groups_created.append('reset')
    if isinstance(reset_prefix,commands.Group) and reset_prefix.get_command('firms') is None:
        reset_prefix.add_command(commands.Command(reset_firms_prefix_callback,name='firms'))
        cog.prefix_attached.append((reset_prefix,'firms'))
    else:
        if bot.get_command('resetfirms') is None:
            bot.add_command(commands.Command(reset_firms_prefix_callback,name='resetfirms'))
            cog.prefix_groups_created.append('resetfirms')
