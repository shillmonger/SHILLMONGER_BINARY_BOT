"""Generate a NEW Telethon StringSession for BINARY_TRADING_BOTS.

Run this ONCE, locally, in a real terminal. It sends ONE login-code
request, prints safe diagnostics about which delivery channel Telegram
chose, then waits for you to decide what to do next.

    cd BINARY_TRADING_BOTS/sidecar
    python tools/telegram_login.py

Never prints: API_HASH, session strings, phone number, phone_code_hash,
verification codes, tokens, or 2FA passwords.
"""

import asyncio
import getpass
import os
import re
import sys
import platform
import tempfile
from pathlib import Path

import qrcode

# Load env like the sidecar does: sidecar/.env first, then parent .env.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from dotenv import load_dotenv

    base = Path(__file__).resolve().parents[1]
    load_dotenv(base / ".env")
    load_dotenv(base.parent / ".env")
except ImportError:
    pass

import telethon  # noqa: E402
from telethon import TelegramClient  # noqa: E402
from telethon.errors import (  # noqa: E402
    PasswordHashInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession  # noqa: E402


def display_qr(url: str) -> None:
    """Render the short-lived login URL locally; do not print the token URL."""
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(out=sys.stdout, invert=True)


async def login_with_qr(client: TelegramClient) -> None:
    """Authorize using fresh QR tokens, refreshing the QR when it expires."""
    qr_login = await client.qr_login()
    while True:
        print("\nScan this QR with Telegram on your phone:")
        print("Telegram > Settings > Devices > Link Desktop Device")
        print("Do not share or screenshot this QR code.")
        display_qr(qr_login.url)
        print("\nWaiting for authorization...")
        try:
            await qr_login.wait()
        except asyncio.TimeoutError:
            print("QR expired; generating a fresh QR code.")
            await qr_login.recreate()
            continue
        except SessionPasswordNeededError:
            while True:
                password = getpass.getpass("Telegram two-step verification password: ")
                try:
                    await client.sign_in(password=password)
                    break
                except PasswordHashInvalidError:
                    print("That two-step verification password was not accepted. Try again.")
        return


def save_session_string(env_path: Path, session_string: str) -> None:
    """Store the authorized session in sidecar/.env without printing it."""
    if not session_string:
        raise ValueError("Telegram returned an empty session string")
    if not env_path.is_file():
        raise FileNotFoundError(f"Sidecar environment file not found: {env_path}")

    with env_path.open("r", encoding="utf-8", newline="") as env_file:
        original = env_file.read()
    lines = original.splitlines()
    matches = [
        index
        for index, line in enumerate(lines)
        if re.match(r"^\s*TELEGRAM_SESSION_STRING\s*=", line)
    ]
    if len(matches) > 1:
        raise ValueError("sidecar/.env contains multiple TELEGRAM_SESSION_STRING entries")
    if matches:
        index = matches[0]
        if lines[index].partition("=")[2].strip():
            raise RuntimeError(
                "sidecar/.env already has a Telegram session; refusing to overwrite it"
            )
        indent = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
        lines[index] = f"{indent}TELEGRAM_SESSION_STRING={session_string}"
    else:
        lines.append(f"TELEGRAM_SESSION_STRING={session_string}")

    newline = "\r\n" if "\r\n" in original else "\n"
    updated = newline.join(lines)
    if original.endswith(("\n", "\r")):
        updated += newline

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=env_path.parent,
            prefix=".telegram-session-",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(updated)
        os.replace(temporary_path, env_path)
        temporary_path = None
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def print_diagnostics(sent, client) -> None:
    """Print ONLY safe diagnostic fields — never secrets or codes."""
    print()
    print("=" * 64)
    print("TELEGRAM send_code_request DIAGNOSTICS")
    print("=" * 64)

    t = getattr(sent, "type", None)
    print(f"Delivery type (class):   {type(t).__name__ if t is not None else 'None'}")
    print(f"Delivery type (value):   {t!r}" if t is not None else "Delivery type (value):   None")

    next_type = getattr(sent, "next_type", None)
    print(f"Next type:               {next_type!r}" if next_type is not None else "Next type:               None")

    timeout = getattr(sent, "timeout", None)
    print(f"Timeout (s):             {timeout}" if timeout is not None else "Timeout (s):             None")

    print(f"phone_code_hash present: {bool(getattr(sent, 'phone_code_hash', None))}")
    print(f"Client connected:        {client.is_connected()}")
    print(f"Telethon version:        {getattr(telethon, '__version__', 'unknown')}")
    print(f"Python version:          {platform.python_version()}")
    print("=" * 64)
    print("NOTE: The verification code itself is never shown here.")
    print("Check the Telegram app's 'Telegram' service chat, SMS, or calls.")
    print("=" * 64)


async def main() -> None:
    api_id_raw = os.environ.get("API_ID", "")
    api_hash = os.environ.get("API_HASH", "")
    if not (api_id_raw and api_hash):
        print("Missing API_ID / API_HASH in .env")
        sys.exit(1)

    try:
        telegram_api_id = int(api_id_raw)
    except ValueError:
        print("API_ID must be an integer.")
        sys.exit(1)

    client = TelegramClient(StringSession(), telegram_api_id, api_hash)
    try:
        await client.connect()
        if await client.is_user_authorized():
            print("This session is already authorized — nothing to do.")
            return

        print("Connecting to Telegram...")
        await login_with_qr(client)
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram QR login completed but the session is not authorized")

        session_string = client.session.save()
        env_path = Path(__file__).resolve().parents[1] / ".env"
        save_session_string(env_path, session_string)
        print()
        print("QR login approved. Telegram session saved to sidecar/.env.")
        print("Restart the sidecar to connect. Keep sidecar/.env private.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted — exiting.")
