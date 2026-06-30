"""
One-shot script: generate June 2026 monthly PDF and send to Telegram.
Run from project root (where .env lives):

    python send_monthly_report.py
    python send_monthly_report.py 2026-05   # any past month
"""
import sys
import asyncio
from datetime import date

# ── optional: pick month from CLI arg ────────────────────────────────────────
if len(sys.argv) > 1:
    try:
        y, m = sys.argv[1].split("-")
        target = date(int(y), int(m), 1)
    except ValueError:
        print("Usage: python send_monthly_report.py [YYYY-MM]")
        sys.exit(1)
else:
    target = date(2026, 6, 30)   # June 2026

print(f"Generating monthly report for {target.strftime('%B %Y')} ...")

from uptox.services import monthly_report, telegram_service

try:
    pdf_path = monthly_report.generate(target)
    print(f"PDF generated: {pdf_path}")
except Exception as e:
    print(f"ERROR generating PDF: {e}")
    raise

caption = f"Addy's Algo Trading — Monthly Report {target.strftime('%B %Y')}"

async def _send():
    from uptox.services.telegram_service import _send_document_async
    await _send_document_async(str(pdf_path), caption=caption, delete_after=False)

asyncio.run(_send())
print("Done — check Telegram.")
