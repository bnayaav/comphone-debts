# updated 21/04/2026
import subprocess
import sys

# 1. דוח הקפה (חובות)
try:
    subprocess.run(["python", "hekfe_daily_final.py"], check=True)
except subprocess.CalledProcessError as e:
    print(f"⚠️  hekfe_daily_final נכשל: {e}")

# 2. פירוט לקוחות
try:
    subprocess.run(["python", "customer_details.py"], check=True)
except subprocess.CalledProcessError as e:
    print(f"⚠️  customer_details נכשל: {e}")

# 3. דוח תיקוני מעבדה (חדש)
try:
    subprocess.run(["python", "repair_sync.py"], check=True)
except subprocess.CalledProcessError as e:
    print(f"⚠️  repair_sync נכשל: {e}")

print("✅ run_all הסתיים")
