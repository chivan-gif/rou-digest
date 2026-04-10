import schedule
import time
import pytz
from datetime import datetime
from digest import main

BUCHAREST_TZ = pytz.timezone("Europe/Bucharest")

def run_digest():
    now = datetime.now(BUCHAREST_TZ)
    print(f"⏰ Running digest at {now.strftime('%Y-%m-%d %H:%M')} Bucharest time")
    try:
        main()
    except Exception as e:
        print(f"❌ Error running digest: {e}")

# Schedule for 8:00 AM Bucharest time every day
# Railway runs in UTC — Bucharest is UTC+3 (summer) / UTC+2 (winter)
# We check the Bucharest time ourselves to be timezone-safe
def check_and_run():
    now = datetime.now(BUCHAREST_TZ)
    if now.hour == 8 and now.minute == 0:
        run_digest()

schedule.every(1).minutes.do(check_and_run)

print("🚀 ROU Sales Digest scheduler started")
print(f"   Will run daily at 08:00 Bucharest time")
print(f"   Current Bucharest time: {datetime.now(BUCHAREST_TZ).strftime('%H:%M')}")

while True:
    schedule.run_pending()
    time.sleep(30)
