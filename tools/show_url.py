"""Print the bridge URL, tunnel password, and an ASCII QR code for the phone."""
import sys
from pathlib import Path
from urllib.request import urlopen

import qrcode


def public_ip() -> str:
    try:
        with urlopen("https://loca.lt/mytunnelpassword", timeout=10) as r:
            return r.read().decode().strip()
    except Exception:
        return "(не удалось получить — открой loca.lt/mytunnelpassword)"


def access_token() -> str:
    try:
        p = Path(__file__).resolve().parent.parent / ".bridge-token"
        return p.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    url = sys.argv[1] if len(sys.argv) > 1 else ""
    if not url:
        print("no url")
        return
    pw = public_ip()
    token = access_token()
    # Bake the token into the QR link so scanning auto-logs in.
    qr_url = f"{url}/?key={token}" if token else url
    print("\n" + "=" * 52)
    print("  ClaudeBridge готов")
    print("=" * 52)
    print(f"\n  URL:        {url}")
    print(f"  Код доступа: {token}")
    print(f"  Tunnel-пароль (localtunnel): {pw}\n")

    qr = qrcode.QRCode(border=1)
    qr.add_data(qr_url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)
    print("\n  Наведи камеру телефона на QR ↑ — код доступа зайдёт сам.")
    print("  Либо открой URL и введи код доступа вручную.")
    print("=" * 52 + "\n")


if __name__ == "__main__":
    main()
