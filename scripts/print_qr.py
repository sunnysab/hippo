#!/usr/bin/env python3
"""Decode a QR image and print it in the terminal."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image
from pyzbar.pyzbar import decode
import qrcode


def display_qr_in_shell(image_path: Path) -> None:
    if not image_path.exists():
        print(f"ERROR: QR image not found: {image_path}", file=sys.stderr)
        return

    try:
        img = Image.open(image_path)
        decoded_objects = decode(img)
        if not decoded_objects:
            print("ERROR: No QR code detected in the image.", file=sys.stderr)
            return

        obj = decoded_objects[0]
        qr_data = obj.data.decode("utf-8", errors="ignore")

        print(f"[ok] decoded data: {qr_data}")
        print("[ok] rendering QR in terminal...\n")

        qr = qrcode.QRCode()
        qr.add_data(qr_data)
        qr.make(fit=True)
        qr.print_ascii(tty=True)

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)


def main() -> int:
    default_path = Path.home() / ".local/share/hippo/login/qrcode.png"
    image_path = Path(sys.argv[1]) if len(sys.argv) > 1 else default_path
    display_qr_in_shell(image_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
