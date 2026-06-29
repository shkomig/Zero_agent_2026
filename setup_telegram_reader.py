"""One-time interactive setup for the Telethon MTProto reader.

Run this ONCE to authenticate with your personal Telegram account:

    python setup_telegram_reader.py

You will be asked for:
  1. Your phone number (international format, e.g. +972501234567)
  2. The verification code Telegram sends you (SMS or app notification)
  3. Your 2FA password (if enabled on your account)

After successful auth, a session file is saved to:
    data/telethon/zero_agent.session

All subsequent calls from telegram_reader.py will reuse this session
automatically — no further interaction required.
"""

import asyncio
import os
import sys
from pathlib import Path

# Load .env first so ZERO_AGENT_TG_API_ID etc. are available
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

try:
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
except ImportError:
    print("ERROR: telethon is not installed.")
    print("Run:  pip install telethon>=1.36")
    sys.exit(1)

API_ID_RAW = os.getenv("ZERO_AGENT_TG_API_ID", "").strip()
API_HASH = os.getenv("ZERO_AGENT_TG_API_HASH", "").strip()
SESSION_DIR = Path(__file__).parent / "data" / "telethon"
SESSION_PATH = str(SESSION_DIR / "zero_agent")


async def main() -> None:
    if not API_ID_RAW or not API_HASH:
        print("ERROR: ZERO_AGENT_TG_API_ID or ZERO_AGENT_TG_API_HASH not set in .env")
        sys.exit(1)

    api_id = int(API_ID_RAW)
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print("  Zero Agent — Telegram Reader Setup")
    print(f"{'='*60}")
    print(f"  API ID : {api_id}")
    print(f"  Session: {SESSION_PATH}.session")
    print()

    client = TelegramClient(SESSION_PATH, api_id, API_HASH)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"✅ Already authenticated as: {me.first_name} {getattr(me, 'last_name', '') or ''} (@{me.username})")
        print("   Session file is valid — no action needed.")
        await client.disconnect()
        return

    print("You are not authenticated yet. Starting interactive login...")
    phone = input("\nEnter your phone number (e.g. +972501234567): ").strip()

    await client.send_code_request(phone)
    code = input("Enter the verification code Telegram sent you: ").strip()

    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        password = input("Two-factor authentication enabled. Enter your 2FA password: ").strip()
        await client.sign_in(password=password)

    me = await client.get_me()
    print(f"\n✅ Authenticated as: {me.first_name} {getattr(me, 'last_name', '') or ''} (@{me.username})")
    print(f"   Session saved to: {SESSION_PATH}.session")
    print("\n   You can now use read_telegram_channels() in Zero Agent.")
    print("   Run Zero Agent normally — no further setup needed.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
