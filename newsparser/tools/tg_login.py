"""Interactive Telethon login helper with prompt injection support.

Reads phone/code/password from TG_LOGIN_PHONE / TG_LOGIN_CODE / TG_LOGIN_PASSWORD
env vars if set (non-interactive for CI), otherwise prompts.
"""

import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv(".env")

PHONE = os.environ.get("TG_LOGIN_PHONE")


async def phone_provider() -> str:
    return PHONE or input("Please enter your phone (or bot token): ")


async def code_provider() -> str:
    """Read code from TG_LOGIN_CODE env or from .tg_code file (written after start)."""
    code = os.environ.get("TG_LOGIN_CODE")
    if code:
        print("[login] using code from env")
        return code
    code_file = ".tg_code"
    print(f"[login] waiting for code in file {code_file}...", flush=True)
    for _ in range(300):  # up to 5 minutes
        if os.path.exists(code_file):
            code = open(code_file).read().strip()
            os.remove(code_file)
            if code:
                print(f"[login] code read from file ({len(code)} digits)")
                return code
        await asyncio.sleep(1)
    raise TimeoutError("no code in .tg_code within 5 minutes")


async def password_provider() -> str:
    pwd = os.environ.get("TG_LOGIN_PASSWORD") or input("Please enter your password: ")
    print("[login] password entered")
    return pwd


async def main() -> None:
    client = TelegramClient(
        "newsparser",
        int(os.environ["api_id"]),
        os.environ["api_hash"],
        device_model="newsparser",
        proxy={"proxy_type": "socks5", "addr": "127.0.0.1", "port": 10808},
    )
    await client.start(
        phone=phone_provider,
        code_callback=code_provider,
        password=password_provider,
    )
    me = await client.get_me()
    print("AUTHORIZED_AS:", me.username or f"{me.first_name} (id {me.id})")
    await client.disconnect()
    # StringSession for production: put into TELETHON_SESSION env var
    session_string = client.session.save() or ""  # type: ignore[union-attr]
    print("TELETHON_SESSION:", session_string)
    print("SESSION_SAVED (file newsparser.session + StringSession above)")


asyncio.run(main())
