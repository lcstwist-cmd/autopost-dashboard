"""AutoPost package initialization.

Force UTF-8 encoding globally on Windows to prevent charmap errors
when printing Unicode characters (≈, →, etc.).
"""
import io
import sys

# Force UTF-8 output on Windows (run once when module is first imported)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
