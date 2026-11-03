import sqlite3

DB_NAME = "database.db"


def connect():
    return sqlite3.connect(DB_NAME)


def create_tables():
    """Create the bot's tables without removing existing data."""
    with connect() as conn:
        cursor = conn.cursor()

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            channel_id INTEGER,
            message TEXT,
            hour TEXT
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS economy_accounts (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            wallet INTEGER NOT NULL DEFAULT 0,
            bank INTEGER NOT NULL DEFAULT 0,
            last_daily TEXT,
            last_collect TEXT,
            PRIMARY KEY (guild_id, user_id)
        )
        """)

        # Upgrade databases created before the collect feature was added.
        account_columns = {
            row[1] for row in cursor.execute(
                "PRAGMA table_info(economy_accounts)"
            ).fetchall()
        }
        if "last_collect" not in account_columns:
            cursor.execute(
                "ALTER TABLE economy_accounts ADD COLUMN last_collect TEXT"
            )

        # Older versions blocked negative balances at the database level.
        # Rebuild that table in-place while retaining balances and cooldowns.
        account_schema_row = cursor.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'economy_accounts'"
        ).fetchone()
        account_schema = "".join((account_schema_row[0] or "").lower().split())
        if "check(wallet>=0)" in account_schema or "check(bank>=0)" in account_schema:
            cursor.execute("DROP TABLE IF EXISTS economy_accounts_migration")
            cursor.execute("""
                CREATE TABLE economy_accounts_migration (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    wallet INTEGER NOT NULL DEFAULT 0,
                    bank INTEGER NOT NULL DEFAULT 0,
                    last_daily TEXT,
                    last_collect TEXT,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
            cursor.execute("""
                INSERT INTO economy_accounts_migration
                    (guild_id, user_id, wallet, bank, last_daily, last_collect)
                SELECT guild_id, user_id, wallet, bank, last_daily, last_collect
                FROM economy_accounts
            """)
            cursor.execute("DROP TABLE economy_accounts")
            cursor.execute(
                "ALTER TABLE economy_accounts_migration RENAME TO economy_accounts"
            )

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS economy_collect_roles (
            guild_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            daily_pay INTEGER NOT NULL CHECK (daily_pay >= 0),
            PRIMARY KEY (guild_id, role_id)
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS economy_shop_listings (
            listing_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            seller_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            price INTEGER NOT NULL CHECK (price > 0),
            created_at TEXT NOT NULL,
            UNIQUE (guild_id, seller_id, role_id)
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS economy_auctions (
            auction_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            creator_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            starting_price INTEGER NOT NULL CHECK (starting_price > 0),
            current_price INTEGER NOT NULL CHECK (current_price > 0),
            highest_bidder_id INTEGER,
            ends_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL
        )
        """)

        cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_auction_per_guild
        ON economy_auctions(guild_id)
        WHERE status = 'active'
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS economy_settings (
            guild_id INTEGER PRIMARY KEY,
            daily_min INTEGER NOT NULL DEFAULT 100,
            daily_max INTEGER NOT NULL DEFAULT 500,
            daily_cooldown_hours INTEGER NOT NULL DEFAULT 24,
            rob_chance INTEGER DEFAULT NULL,
            roulette_chance INTEGER DEFAULT NULL,
            CHECK (daily_min >= 0),
            CHECK (daily_max >= daily_min)
        )
        """)

        settings_columns = {
            row[1] for row in cursor.execute(
                "PRAGMA table_info(economy_settings)"
            ).fetchall()
        }
        if "rob_chance" not in settings_columns:
            cursor.execute(
                "ALTER TABLE economy_settings "
                "ADD COLUMN rob_chance INTEGER DEFAULT NULL"
            )
        if "daily_cooldown_hours" not in settings_columns:
            cursor.execute(
                "ALTER TABLE economy_settings "
                "ADD COLUMN daily_cooldown_hours INTEGER NOT NULL DEFAULT 24"
            )
        if "roulette_chance" not in settings_columns:
            cursor.execute(
                "ALTER TABLE economy_settings "
                "ADD COLUMN roulette_chance INTEGER DEFAULT NULL"
            )

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS economy_server_shop (
            listing_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            product_key TEXT NOT NULL,
            product_type TEXT NOT NULL CHECK (product_type IN ('item', 'role')),
            product_name TEXT NOT NULL,
            role_id INTEGER,
            price INTEGER NOT NULL CHECK (price > 0),
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE (guild_id, product_key)
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS economy_inventory (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            product_key TEXT NOT NULL,
            item_name TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
            PRIMARY KEY (guild_id, user_id, product_key)
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS economy_item_effects (
            guild_id INTEGER NOT NULL,
            product_key TEXT NOT NULL,
            effect_type TEXT NOT NULL,
            effect_value INTEGER NOT NULL CHECK (effect_value >= 0),
            PRIMARY KEY (guild_id, product_key, effect_type)
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS economy_boxes (
            guild_id INTEGER NOT NULL,
            box_key TEXT NOT NULL,
            box_name TEXT NOT NULL,
            price INTEGER NOT NULL CHECK (price > 0),
            listed INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (guild_id, box_key)
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS economy_box_rewards (
            reward_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            box_key TEXT NOT NULL,
            reward_type TEXT NOT NULL CHECK (reward_type IN ('item', 'role', 'money')),
            reward_key TEXT NOT NULL,
            reward_name TEXT NOT NULL,
            reward_amount INTEGER NOT NULL DEFAULT 0,
            chance REAL NOT NULL CHECK (chance >= 0),
            FOREIGN KEY (guild_id, box_key)
                REFERENCES economy_boxes(guild_id, box_key) ON DELETE CASCADE
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS economy_online_roulettes (
            game_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER,
            creator_id INTEGER NOT NULL,
            chosen_number INTEGER NOT NULL CHECK (chosen_number BETWEEN 1 AND 30),
            stake INTEGER NOT NULL CHECK (stake > 0),
            ends_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'waiting',
            created_at TEXT NOT NULL,
            result_text TEXT,
            announced INTEGER NOT NULL DEFAULT 0
        )
        """)

        cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS one_waiting_online_roulette_per_guild
        ON economy_online_roulettes(guild_id)
        WHERE status = 'waiting'
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS economy_online_roulette_bets (
            game_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            chosen_number INTEGER NOT NULL CHECK (chosen_number BETWEEN 1 AND 30),
            stake INTEGER NOT NULL CHECK (stake > 0),
            PRIMARY KEY (game_id, user_id),
            UNIQUE (game_id, chosen_number)
        )
        """)
