"""
repair_sync.py - סנכרון דוח תיקונים מ-NewOrder ל-ComPhone Lab Worker
======================================================================
רץ כחלק מה-cron היומי ב-Railway.
מחלץ דוח תיקונים של 7 הימים האחרונים, כולל לחיצה על "+" של כל שורה
(nested grid) ושולח ל-Worker שמזהה שינויי סטטוס ושולח וואטסאפ אוטומטי.

Environment variables הנדרשים:
    LAB_WORKER_URL  - כתובת ה-Worker (https://comphone-lab-worker.bnaya-av.workers.dev)
    LAB_SYNC_KEY    - מפתח הסנכרון
"""
import requests
from bs4 import BeautifulSoup
import re
import os
import sys
from datetime import date, timedelta

# ===== הגדרות =====
LOGIN_URL   = "https://cellular.neworder.co.il/heb/direct.aspx?UserName=nChDORjeuASklAO4HRJVcQ==&StoreName=eL/mCT/S9JtKfrclQgpe2Q==&password=y5leGHLlRcO1YFjej3CeHQ=="
REPORT_URL  = "https://cellular.neworder.co.il/heb/reports/reportgenerator.aspx"
WORKER_URL  = os.environ.get("LAB_WORKER_URL", "https://comphone-lab-worker.bnaya-av.workers.dev")
SYNC_KEY    = os.environ.get("LAB_SYNC_KEY", "")
UA          = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

DAYS_BACK   = 7   # כמה ימים אחורה לסרוק

# כותרות עמודות של דוח התיקונים (לפי סדר הטבלה)
MAIN_HEADERS = [
    'פתח', 'ל.משנה', 'סוג לקוח', 'תאריך קבלה', 'ימי המתנה', 'מ.הזמנה',
    'עדיפות', 'טכנאי משויך', 'תאריך מסירה', 'טלפון', 'סכום חלקים',
    'שעות שהייה במעבדה', 'התקבל ע"י', 'חיוב בחשבונית', 'חיוב', 'עלות',
    'IMEI', 'טופס', 'ביטוח', 'שם הלקוח', 'דגם מכשיר', 'מה תוקן',
    'מלל פנימי', 'סטטוס', 'תקלה', 'קוד סניף', 'פעולות'
]

# ===== עזרים (אותם כמו hekfe_daily_final) =====
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

def find_repairs_nav_target(soup):
    """
    מנסה למצוא את ה-NAV_CTRL של דוח התיקונים.
    מחפש קישור/כפתור בתפריט שמכיל את המילה 'תיקון' או 'מעבדה'.
    """
    # שיטה 1: ref אלמנטים עם onclick שמפעיל __doPostBack
    for a in soup.find_all('a'):
        text = a.get_text(strip=True)
        href = a.get('href', '')
        if any(kw in text for kw in ['תיקוני מעבדה', 'דוח תיקונים', 'דו"ח תיקונים', 'מעבדה']):
            m = re.search(r"__doPostBack\(['\"]([^'\"]+)['\"]", href)
            if m:
                print(f"    ✓ נמצא קישור: '{text}' → {m.group(1)}")
                return m.group(1)
    # שיטה 2: inputs
    for inp in soup.find_all('input'):
        val = inp.get('value', '')
        if 'תיקון' in val or 'מעבדה' in val:
            nm = inp.get('name', '')
            if nm:
                print(f"    ✓ נמצא כפתור: '{val}' → {nm}")
                return nm
    return None

def extract_main_row_data(tr):
    """חולץ שורת לקוח ראשית מה-HTML"""
    cells = tr.find_all('td')
    obj = {}
    for i, cell in enumerate(cells):
        if i < len(MAIN_HEADERS):
            obj[MAIN_HEADERS[i]] = cell.get_text(strip=True)
    return obj

def extract_nested_data(nested_table):
    """חולץ שורות מטבלה מקוננת (חלקים שהותקנו וכו')"""
    rows = nested_table.find_all('tr')
    if len(rows) < 2:
        return []
    headers = [th.get_text(strip=True) for th in rows[0].find_all(['th', 'td'])]
    data = []
    for tr in rows[1:]:
        cells = tr.find_all('td')
        obj = {}
        for i, cell in enumerate(cells):
            if i < len(headers) and headers[i]:
                obj[headers[i]] = cell.get_text(strip=True)
        data.append(obj)
    return data

def extract_all_repairs(soup):
    """
    מחלץ את כל התיקונים מטבלת הדוח.
    כרגע מחלץ רק את השורות הראשיות - בלי nested grids.
    התיקון/nested יכול להתווסף בעתיד עם postback לכל +.
    """
    repairs = []
    main_table = soup.find('table', id='MainContent_gvReportData')
    if not main_table:
        # נסה לחפש טבלה לפי תוכן - שיש בה "שם הלקוח" ו"דגם מכשיר"
        for t in soup.find_all('table'):
            ths = [th.get_text(strip=True) for th in t.find_all('th')]
            if 'שם הלקוח' in ths and ('דגם מכשיר' in ths or 'טופס' in ths):
                main_table = t
                print(f"    ✓ נמצאה טבלה (fallback) עם {len(ths)} עמודות")
                break
        if not main_table:
            return []

    # חלץ רק את ה-TR הישיר של הטבלה (לא של nested tables)
    direct_rows = []
    tbody = main_table.find('tbody') or main_table
    for tr in tbody.find_all('tr', recursive=False):
        # בדוק שזו לא שורת header
        if tr.find('th'):
            continue
        # בדוק שיש בתוכה input מסוג image עם imgShowNestedGrid (מאפיין של שורה ראשית)
        img_btn = tr.find('input', {'type': 'image'})
        # לא בכל שורות יש image, אבל לפחות יש 20+ תאים
        cells = tr.find_all('td')
        if len(cells) >= 20:
            direct_rows.append(tr)

    print(f"    ✓ נמצאו {len(direct_rows)} שורות תיקון")

    for tr in direct_rows:
        data = extract_main_row_data(tr)
        if data.get('טופס'):  # רק שורות עם מס' טופס
            repairs.append(data)

    return repairs

# ===== ריצה ראשית =====
def run():
    today      = date.today()
    from_date  = today - timedelta(days=DAYS_BACK - 1)
    today_str  = today.strftime("%d/%m/%Y")
    from_str   = from_date.strftime("%d/%m/%Y")

    print("=" * 55)
    print(f"  סנכרון דוח תיקונים | {from_str} - {today_str}")
    print("=" * 55)

    if not SYNC_KEY:
        print("⚠️  LAB_SYNC_KEY לא מוגדר - מדלג על סנכרון תיקונים")
        return

    # ── 1. כניסה ────────────────────────────────────────────
    print("\n[1] כניסה לאתר...")
    session = requests.Session()
    r1 = session.get(LOGIN_URL, headers={"User-Agent": UA}, timeout=20)
    if r1.status_code != 200:
        raise Exception(f"כניסה נכשלה: {r1.status_code}")
    print(f"    ✅ {r1.url.split('/')[-1]}")

    # ── 2. דף הדוחות ────────────────────────────────────────
    print("\n[2] טוען דף דוחות...")
    r2 = session.get(REPORT_URL, headers={"User-Agent": UA}, timeout=20)
    soup2 = BeautifulSoup(r2.text, "lxml")
    print(f"    ✅ {len(r2.text):,} תווים")

    # ── 3. בחירת דוח תיקוני מעבדה ───────────────────────────
    print("\n[3] מחפש דוח תיקונים בתפריט...")
    nav_ctrl = find_repairs_nav_target(soup2)
    if nav_ctrl:
        print(f"\n    בוחר דוח דרך: {nav_ctrl}")
        r3 = http_post(session, soup2, {"__EVENTTARGET": nav_ctrl, "__EVENTARGUMENT": ""})
        soup3 = BeautifulSoup(r3.text, "lxml")
        print(f"    ✅ {r3.status_code} | {len(r3.text):,} תווים")
    else:
        # fallback - אולי הדף כבר של תיקונים
        print("    ⚠️ לא נמצא קישור לדוח תיקונים - משתמש בדף הנוכחי")
        soup3 = soup2

    # ── 4. מצא שדות תאריך ──────────────────────────────────
    from_field = find_field(soup3, "txtFromDate")
    to_field   = find_field(soup3, "txtToDate")
    btn_field  = find_field(soup3, "btnConfirm") or find_field(soup3, "btnShowReport")
    if not from_field or not to_field:
        raise Exception(f"שדות תאריך לא נמצאו: from={from_field} to={to_field}")
    print(f"\n[4] שדות: FROM={from_field.split('$')[-1]} | TO={to_field.split('$')[-1]}")

    # ── 5. שליחת טווח תאריכים ───────────────────────────────
    print(f"\n[5] שולח דוח ל-{from_str} - {today_str}...")
    extra = {from_field: from_str, to_field: today_str}
    if btn_field:
        extra[btn_field] = "הצג דו''ח"
    r4 = http_post(session, soup3, extra)
    soup4 = BeautifulSoup(r4.text, "lxml")
    print(f"    ✅ {r4.status_code} | {len(r4.text):,} תווים")

    # ── 6. חילוץ תיקונים ────────────────────────────────────
    print("\n[6] מחלץ תיקונים...")
    repairs = extract_all_repairs(soup4)
    del soup4

    if not repairs:
        print("    ℹ️  אין תיקונים להיום")
        return

    print(f"    ✅ {len(repairs)} תיקונים")
    for r in repairs[:5]:
        print(f"    [{r.get('טופס','?')}] {r.get('שם הלקוח','?')[:20]:20s} | {r.get('דגם מכשיר','?')[:15]:15s} | {r.get('סטטוס','?')}")
    if len(repairs) > 5:
        print(f"    ... ועוד {len(repairs) - 5}")

    # ── 7. שליחה ל-Worker ───────────────────────────────────
    print(f"\n[7] שולח ל-Worker ({WORKER_URL.split('/')[2]})...")
    resp = requests.post(
        f"{WORKER_URL}/api/sync",
        json={"repairs": repairs},
        headers={
            "Content-Type": "application/json",
            "X-Sync-Key": SYNC_KEY,
        },
        timeout=60,
    )
    if resp.status_code == 200:
        data = resp.json()
        stats = data.get('stats', {})
        details = data.get('details', {})
        print(f"    ✅ סנכרון הסתיים")
        print(f"    נשלחו:    {stats.get('received', 0)}")
        print(f"    חדשים:    {stats.get('added', 0)}")
        print(f"    שינויי סטטוס: {stats.get('statusChanged', 0)}")
        if stats.get('triggered', 0) > 0:
            print(f"    🔔 טריגר לוואטסאפ: {stats.get('triggered')}")
            print(f"    נשלחו אוטומטית: {stats.get('autoSent', 0)}")
            if stats.get('autoFailed', 0) > 0:
                print(f"    ⚠️  נכשלו: {stats.get('autoFailed')}")
        if data.get('seededNow'):
            print(f"    ℹ️  זה סנכרון ראשוני - לא נשלחו הודעות אוטומטיות")

        # הדפס שינויי סטטוס
        status_changes = details.get('statusChanged', [])
        if status_changes:
            print(f"\n    שינויי סטטוס:")
            for sc in status_changes[:10]:
                print(f"      [{sc.get('form')}] {sc.get('name','?')[:20]:20s} | {sc.get('from','?')} → {sc.get('to','?')}")
    else:
        print(f"    ❌ Worker החזיר {resp.status_code}: {resp.text[:300]}")
        sys.exit(1)

    print(f"\n{'='*55}")
    print(f"  ✅ הסתיים | {len(repairs)} תיקונים")
    print(f"{'='*55}\n")

if __name__ == "__main__":
    run()
