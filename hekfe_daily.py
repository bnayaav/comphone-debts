import requests
from bs4 import BeautifulSoup
import json, re, os, sys
from datetime import date

# ===== הגדרות — נקראות מ-Environment Variables של Railway =====
LOGIN_URL   = "https://cellular.neworder.co.il/heb/direct.aspx?UserName=nChDORjeuASklAO4HRJVcQ==&StoreName=eL/mCT/S9JtKfrclQgpe2Q==&password=y5leGHLlRcO1YFjej3CeHQ=="
REPORT_URL  = "https://cellular.neworder.co.il/heb/reports/reportgenerator.aspx"
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://debt-worker.bnaya-av.workers.dev")   # כתובת ה-Worker
NAV_CTRL    = "ctl00$ctrlNavigationBar$ctl286"     # val=385 ריכוז קניות הקפה
UA          = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# ===== עזרים =====
def parse_num(s):
    try:
        return float(str(s or "").replace(",", "").replace(" ", ""))
    except:
        return 0

def get_vs(soup):
    vs  = soup.find("input", {"name": "__VIEWSTATE"})
    ev  = soup.find("input", {"name": "__EVENTVALIDATION"})
    vsg = soup.find("input", {"name": "__VIEWSTATEGENERATOR"})
    d = {
        "__VIEWSTATE":          vs["value"]  if vs  else "",
        "__VIEWSTATEGENERATOR": vsg["value"] if vsg else "",
        "__EVENTVALIDATION":    ev["value"]  if ev  else "",
        "__EVENTTARGET":   "",
        "__EVENTARGUMENT": "",
    }
    for p in soup.find_all("input", {"name": lambda n: n and "hdnPrimaryGridKeyValue" in n}):
        d[p["name"]] = ""
    return d

def find_field(soup, keyword):
    for inp in soup.find_all("input"):
        nm = inp.get("name", "")
        if keyword in nm:
            return nm
    return None

def http_post(session, soup, extra):
    data = {**get_vs(soup), **extra}
    return session.post(
        REPORT_URL, data=data,
        headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded", "Referer": REPORT_URL},
        timeout=60,
    )

def extract_rows(soup):
    """
    חולץ לקוחות עם חוב פתוח מטבלת הדוח.
    כללי סינון:
      - שורת סיכום (קוד ריק / לא מספרי) — מדולגת
      - יתרת זכות (שלילית) — מדולגת
      - יתרה 0 — מדולגת
    """
    rows = []
    for t in soup.find_all("table"):
        ths = [th.get_text(strip=True) for th in t.find_all("th")]
        if "שם הלקוח" not in ths and "קוד לקוח" not in ths:
            continue

        for tr in t.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            if not cells:
                continue

            # detect offset: עמודה ראשונה ריקה בפורמט neworder
            offset = 1 if (len(cells) > 6 and cells[0] == "" and cells[1].lstrip("-").isdigit()) else 0
            code   = cells[offset] if len(cells) > offset else ""

            # דלג שורת כותרת ושורת סיכום (קוד לא מספרי)
            if not re.match(r"^\d{4,6}$", code):
                continue

            bal = parse_num(cells[offset + 5]) if len(cells) > offset + 5 else 0
            inv = parse_num(cells[offset + 3]) if len(cells) > offset + 3 else 0
            rec = parse_num(cells[offset + 4]) if len(cells) > offset + 4 else 0

            if bal <= 0:   # 0 = שולם, שלילי = זכות — שניהם מדולגים
                continue

            rows.append({
                "code":          code,
                "name":          cells[offset + 1].strip() if len(cells) > offset + 1 else "",
                "deliveryNotes": parse_num(cells[offset + 2]) if len(cells) > offset + 2 else 0,
                "invoices":      inv,
                "receipts":      rec,
                "balance":       bal,
            })
        break   # נמצאה הטבלה — יצא מהלולאה

    return rows

# ===== לוגיקה ראשית =====
def run():
    today     = date.today()
    today_str = today.strftime("%d/%m/%Y")
    today_iso = today.strftime("%Y-%m-%d")

    print("=" * 55)
    print(f"  ריכוז קניות הקפה | {today_str}")
    print("=" * 55)

    if not WEBHOOK_URL:
        print("❌  WEBHOOK_URL לא מוגדר — הגדר ב-Railway Environment Variables")
        sys.exit(1)

    # ── 1. כניסה ────────────────────────────────────────────
    print("\n[1] כניסה לאתר...")
    session = requests.Session()
    r1 = session.get(LOGIN_URL, headers={"User-Agent": UA}, timeout=20)
    if r1.status_code != 200:
        raise Exception(f"כניסה נכשלה: {r1.status_code}")
    print(f"    ✅ {r1.url.split('/')[-1]}")

    # ── 2. דף הדוחות ────────────────────────────────────────
    print("\n[2] טוען דף דוחות...")
    r2    = session.get(REPORT_URL, headers={"User-Agent": UA}, timeout=20)
    soup2 = BeautifulSoup(r2.text, "lxml")
    print(f"    ✅ {len(r2.text):,} תווים")

    # ── 3. בחירת דוח הקפה ───────────────────────────────────
    print("\n[3] בוחר דוח 'ריכוז קניות הקפה'...")
    r3    = http_post(session, soup2, {"__EVENTTARGET": NAV_CTRL, "__EVENTARGUMENT": ""})
    soup3 = BeautifulSoup(r3.text, "lxml")
    print(f"    ✅ {r3.status_code} | {len(r3.text):,} תווים")

    # ── 4. שדות תאריך ───────────────────────────────────────
    from_field = find_field(soup3, "txtFromDate")
    to_field   = find_field(soup3, "txtToDate")
    btn_field  = find_field(soup3, "btnConfirm") or find_field(soup3, "btnShowReport")
    if not from_field or not to_field:
        raise Exception(f"שדות תאריך לא נמצאו: from={from_field} to={to_field}")
    print(f"\n[4] שדות: FROM={from_field.split('$')[-1]} | TO={to_field.split('$')[-1]} | BTN={btn_field and btn_field.split('$')[-1]}")

    # ── 5. שליחת תאריך ──────────────────────────────────────
    print(f"\n[5] שולח דוח ל-{today_str}...")
    extra = {from_field: today_str, to_field: today_str}
    if btn_field:
        extra[btn_field] = "הצג דו''ח"
    r4    = http_post(session, soup3, extra)
    soup4 = BeautifulSoup(r4.text, "lxml")
    print(f"    ✅ {r4.status_code} | {len(r4.text):,} תווים")

    # ── 6. חילוץ נתונים ─────────────────────────────────────
    print("\n[6] מחלץ נתונים...")
    print(f"    גודל HTML: {len(r4.text):,} תווים")
    rows = extract_rows(soup4)
    del soup4  # שחרר זיכרון מיד אחרי חילוץ

    if not rows:
        print("    ℹ️  אין עסקאות הקפה להיום — לא נשלח דבר")
        print(f"\n{'='*55}\n  ✅ הסתיים — אין נתונים להיום\n{'='*55}")
        return

    total = sum(r["balance"] for r in rows)
    print(f"    ✅ {len(rows)} לקוחות | סה\"כ ₪{total:,.0f}")
    for r in rows:
        print(f"    [{r['code']}] {r['name'][:25]:25s} → ₪{r['balance']:.0f}")

    # ── 7. שליחה ל-Worker ────────────────────────────────────
    print(f"\n[7] שולח ל-Worker ({WEBHOOK_URL.split('/')[2]})...")
    payload = {
        "date":      today_iso,
        "source":    "railway_cron",
        "customers": rows,
    }
    resp = requests.post(
        f"{WEBHOOK_URL}/import",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    if resp.status_code == 200:
        print(f"    ✅ נשלחו {len(rows)} לקוחות בהצלחה")
        result = resp.json() if resp.text else {}
        print(f"    Worker: {result}")
    else:
        print(f"    ❌ Worker החזיר {resp.status_code}: {resp.text[:200]}")
        sys.exit(1)

    print(f"\n{'='*55}")
    print(f"  ✅ הסתיים | {len(rows)} לקוחות | ₪{total:,.0f}")
    print(f"{'='*55}\n")

if __name__ == "__main__":
    run()


# הפעל פירוט לקוחות
try:
    import customer_details
    customer_details.main()
except Exception as e:
    print(f'⚠️  פירוט לקוחות נכשל: {e}')
