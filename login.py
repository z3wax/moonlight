"""Optional command-line login (the admin panel can do the same in a browser).

    python login.py            # phone number + code (+ 2FA password)
    python login.py --qr       # scan a QR code from the Telegram app
    python login.py --show-string   # also print the string session (keep it secret!)
"""

import argparse
import asyncio
import getpass
import sys

from telethon import errors

from autoreply.config import Bootstrap, Settings
from autoreply.formatting import sender_label
from autoreply.session import SessionLock, build_client, save_string_backup
from autoreply.storage import Storage


def print_qr(url: str) -> None:
    try:
        import qrcode
    except ImportError:
        print("\n(QR rasm uchun: pip install qrcode)  Havola:\n" + url)
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.print_ascii(invert=True)


async def login_with_qr(client) -> None:
    qr = await client.qr_login()
    while True:
        print(
            "\nTelegram ilovasida: Sozlamalar -> Qurilmalar -> "
            "'Kompyuterni ulash' (Link Desktop Device) va QR kodni skanerlang:\n"
        )
        print_qr(qr.url)
        try:
            await qr.wait(timeout=60)
            return
        except asyncio.TimeoutError:
            print("QR muddati tugadi, yangisi yaratilmoqda...")
            await qr.recreate()
        except errors.SessionPasswordNeededError:
            password = getpass.getpass("Ikki bosqichli parol (2FA): ")
            await client.sign_in(password=password)
            return


async def login_with_phone(client, phone: str) -> None:
    await client.start(
        phone=lambda: phone or input("Telefon raqam (+998...): ").strip(),
        password=lambda: getpass.getpass("Ikki bosqichli parol (2FA): "),
        code_callback=lambda: input("Telegramga kelgan kodni kiriting: ").strip(),
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram sessiyasini yaratish")
    parser.add_argument("--qr", action="store_true", help="QR kod orqali kirish")
    parser.add_argument("--show-string", action="store_true",
                        help="string sessiyani ekranga chiqarish (server uchun)")
    args = parser.parse_args()

    boot = Bootstrap.from_env()
    store = Storage(boot.data_dir / "state.db")
    settings = Settings(boot, store)
    if not settings.has_telegram_credentials:
        print("API ID / API Hash kiritilmagan (.env yoki admin panel → Telegram).")
        return 1

    lock = SessionLock(settings.sessions_dir / f"{settings.session_name}.lock")
    lock.acquire()  # refuses to run while the bot uses this session
    client = build_client(settings)
    try:
        await client.connect()
        if await client.is_user_authorized():
            print("Sessiya allaqachon mavjud va ishlayapti.")
        elif args.qr:
            await login_with_qr(client)
        else:
            await login_with_phone(client, settings.phone)

        me = await client.get_me()
        string = save_string_backup(client, settings)
        client.session.save()
        print(f"\n✅ Kirildi: {sender_label(me)} (id={me.id})")
        print(f"Sessiya fayli:   {settings.session_path}")
        print(f"Sessiya zaxirasi: {settings.string_session_path}")
        if args.show_string:
            print("\nTELEGRAM_STRING_SESSION (HECH KIMGA BERMANG - bu akkauntingiz kaliti):")
            print(string)
        print("\nEndi botni ishga tushiring:  python main.py  (panel: http://localhost:7860)")
        return 0
    finally:
        await client.disconnect()
        store.close()
        lock.release()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(1)
