# These are the default values for the simulation.
# You can override them by setting the environment variables.
# Or you can change the values directly in the code.
DEFAULTS = {
    "agents": 1000,
    "rounds": 50,
    "seed": 42,
    "scenario": "balanced",
    "initial_agent_usdc": 1000.0,
    "pool_usdc": 10000.0,
    "pool_tokens": 10000000.0,
    "fee_bps": 30.0,
    "save_path": "results.json",
    "log_every": 1,
    "gemini_model": "gemini-2.5-flash-lite",
    "max_concurrent": 50,
    "api_retries": 2,
    "api_timeout_s": 20,
}


PERSONA_PROMPTS = {
    "Degen": "You ape hard on hype, dump fast on fear, and only sometimes wait.",
    "Sniper": "You trade momentum quickly with fast profit-taking and tight loss control.",
    "Paper_hands": "You react emotionally, panic on red candles, and chase green candles.",
    "Stonks": "You keep size smaller and wait for clearer signals.",
    "Flipper": "You rotate in and out quickly and avoid dead choppy ranges.",
    "Diamond": "You buy dips and hold conviction unless fear is extreme.",
}

PERSONAS = sorted(PERSONA_PROMPTS.keys())

BASE_EVENT_MIX = [
    ("SIDEWAYS", 0.10),
    ("KOL_TRENDING", 0.16),
    ("CT_X_NEWS_BULL", 0.12),
    ("TG_CALLS_PUMP", 0.10),
    ("INFLUENCER_SHILL", 0.08),
    ("PAID_KOL_THREAD", 0.08),
    ("RAID_ON_X", 0.08),
    ("VIRAL_MEME", 0.10),
    ("PRICE_SURGE", 0.10),
    ("CT_X_NEWS_BEAR", 0.08),
    ("TG_PANIC_SELL", 0.06),
    ("BUNDLED_SCAM", 0.06),
    ("CALL_GROUP_EXIT", 0.04),
    ("FUD", 0.07),
    ("WHALE_EXIT", 0.04),
    ("DUMP", 0.03),
]

SCENARIOS = {
    "balanced": {
        "personas": {"Degen": 0.28, "Flipper": 0.22, "Paper_hands": 0.18, "Sniper": 0.12, "Stonks": 0.12, "Diamond": 0.08},
        "events": None,
        "news": [
            "CT account starts shilling this token aggressively.",
            "Whale transfer screenshot spreads in Telegram chats.",
            "Space host says this is normal shakeout volatility.",
            "No big catalyst today; fakeouts and chop continue.",
            "Mid-tier influencer posts a paid-looking shill thread.",
            "Raid group coordinates replies under major CT accounts.",
        ],
    },
    "hype_season": {
        "personas": {"Degen": 0.42, "Flipper": 0.26, "Paper_hands": 0.12, "Sniper": 0.08, "Stonks": 0.08, "Diamond": 0.04},
        "events": [
            ("PRICE_SURGE", 0.16),
            ("KOL_TRENDING", 0.14),
            ("CT_X_NEWS_BULL", 0.12),
            ("TG_CALLS_PUMP", 0.14),
            ("INFLUENCER_SHILL", 0.10),
            ("PAID_KOL_THREAD", 0.10),
            ("RAID_ON_X", 0.08),
            ("VIRAL_MEME", 0.08),
            ("SIDEWAYS", 0.04),
            ("CT_X_NEWS_BEAR", 0.02),
            ("TG_PANIC_SELL", 0.01),
            ("FUD", 0.005),
            ("CALL_GROUP_EXIT", 0.005),
            ("DUMP", 0.005),
        ],
        "news": [
            "Large CT thread pushes breakout targets.",
            "Alpha channels coordinate a fresh buy wave.",
            "Listing rumor spreads quickly.",
            "Comments flood with rocket and moon memes.",
            "Influencer posts entry and target ladder to millions of followers.",
            "Paid KOL thread gets mass retweets from meme accounts.",
        ],
    },
    "fear_pit": {
        "personas": {"Paper_hands": 0.38, "Flipper": 0.22, "Stonks": 0.14, "Sniper": 0.12, "Degen": 0.10, "Diamond": 0.04},
        "events": [
            ("FUD", 0.12),
            ("CT_X_NEWS_BEAR", 0.12),
            ("TG_PANIC_SELL", 0.16),
            ("DEV_WALLET_FUD", 0.14),
            ("CALL_GROUP_EXIT", 0.12),
            ("WHALE_EXIT", 0.10),
            ("DUMP", 0.12),
            ("SIDEWAYS", 0.06),
            ("PRICE_SURGE", 0.02),
            ("KOL_TRENDING", 0.01),
            ("CT_X_NEWS_BULL", 0.01),
            ("TG_CALLS_PUMP", 0.01),
            ("INFLUENCER_SHILL", 0.005),
            ("PAID_KOL_THREAD", 0.005),
        ],
        "news": [
            "Bearish thread claims insiders are unloading.",
            "Heavy red candle triggers panic messages.",
            "Exit-liquidity posts trend in group chats.",
            "Dead-cat bounce appears, then fades fast.",
            "Dev wallet tracker alerts spark instant panic.",
            "Call channel admin says he is fully out.",
        ],
    },
}
