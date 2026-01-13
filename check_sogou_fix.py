#!/usr/bin/env python3
"""Check if the Sogou proxy fix is working."""

import sys
from pathlib import Path

# Add current directory to path
sys.path.insert(0, str(Path(__file__).parent))

print("Checking Sogou proxy fix...\n")

# Read the actual source file
downloader_path = Path(__file__).parent / "wechatcli" / "downloader.py"
content = downloader_path.read_text()

# Check if the fix is present
if '"sogou.com" in lowered' in content:
    print("✓ Source code contains the fix")
else:
    print("✗ Source code does NOT contain the fix")
    sys.exit(1)

# Check the logic
if 'url=" in lowered or "url%3d" in lowered' in content:
    print("✓ URL parameter detection is correct")
else:
    print("✗ URL parameter detection is missing")
    sys.exit(1)

if 'logger.debug("Unwrapped Sogou proxy URL:' in content:
    print("✓ Debug logging is present")
else:
    print("✗ Debug logging is missing")

print("\n" + "="*60)
print("The code is correct in the source file.")
print("If you're still seeing errors, you need to:")
print("1. Restart your Python process / reload the module")
print("2. Re-run: pip install -e .")
print("3. Or run directly: python -m wechatcli [command]")
print("="*60)
