#!/usr/bin/env python3
"""
שליפת פירוט קניות הקפה לכל לקוח עם חוב
מריץ אחרי hekfe_daily.py — שולח פירוט ל-Worker
"""
import os, re, time, json, base64, requests
from bs4 import BeautifulSoup

# ── הגדרות ────────────────────────────────────────────────────
# אותן הגדרות כמו hekfe_daily.py
LOGIN_URL  = "https://cellular.neworder.co.il/heb/direct.aspx?UserName=nChDORjeuASklAO4HRJVcQ==&StoreName=eL/mCT/S9JtKfrclQgpe2Q==&password=y5leGHLlRcO1YFjej3CeHQ=="
WORKER_URL = os.environ.get("WEBHOOK_URL", "https://debt-worker.bnaya-av.workers.dev").rstrip("/")
REPORT_URL = "https://cellular.neworder.co.il/heb/reports/reportgenerator.aspx"
UA         = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# val של דוח "תנועות הקפה ללקוח" — יתמלא אוטומטית בגילוי
CUSTOMER_REPORT_NAV = os.environ.get("CUSTOMER_REPORT_NAV", "")

# ── כניסה ────────────────────────────────────────────────────
def login():
    session = requests.Session()
    print("[1] מתחבר לנeworder...")
    r = session.get(LOGIN_URL, headers={"User-Agent": UA}, timeout=30)
    if r.status_code == 200:
        print("    ✅ מחובר")
    else:
        raise Exception(f"כניסה נכשלה: {r.status_code}")
    return session

# ── קבלת ViewState ────────────────────────────────────────────
def get_viewstate(session, html=None):
    if html is None:
        r = session.get(REPORT_URL, timeout=30)
        html = r.text
    soup = BeautifulSoup(html, "lxml")
    vs  = soup.find("input", {"name": "__VIEWSTATE"})
    ev  = soup.find("input", {"name": "__EVENTVALIDATION"})
    vsg = soup.find("input", {"name": "__VIEWSTATEGENERATOR"})
    return {
        "__VIEWSTATE":          vs["value"]  if vs  else "",
        "__EVENTVALIDATION":    ev["value"]  if ev  else "",
        "__VIEWSTATEGENERATOR": vsg["value"] if vsg else "3B3F7A04",
    }

# ── גילוי ה-NAV של דוח לקוח ─────────────────────────────────
def find_customer_report_nav(session):
    global CUSTOMER_REPORT_NAV
    if CUSTOMER_REPORT_NAV:
        return CUSTOMER_REPORT_NAV
    
    print("[*] מחפש ה-NAV של דוח תנועות הקפה ללקוח...")
    r = session.get(REPORT_URL, timeout=30)
    soup = BeautifulSoup(r.text, "lxml")
    
    # חפש קישורים בתפריט הניווט עם "תנועות" או "ללקוח"
    nav = soup.find("div", {"id": re.compile("Navigation|nav", re.I)})
    links = soup.find_all("a", href=True)
    for link in links:
        text = link.get_text(strip=True)
        if ("תנועות" in text and "לקוח" in text) or "הקפה ללקוח" in text:
            onclick = link.get("onclick", "") or link.get("href", "")
            m = re.search(r'(ctl\d+\$ctrlNavigationBar\$ctl\d+)', onclick)
            if m:
                CUSTOMER_REPORT_NAV = m.group(1)
                print(f"    ✅ נמצא: {CUSTOMER_REPORT_NAV}")
                return CUSTOMER_REPORT_NAV
            # try href val
            m2 = re.search(r'val=(\d+)', onclick)
            if m2:
                print(f"    val נמצא: {m2.group(1)}")
    
    print("    ⚠️  לא נמצא אוטומטית — נסה להגדיר CUSTOMER_REPORT_NAV ידנית")
    return None

# ── בחירת דוח לקוח ───────────────────────────────────────────
def select_customer_report(session, nav_ctrl):
    vs = get_viewstate(session)
    payload = {
        **vs,
        "__EVENTTARGET":   nav_ctrl,
        "__EVENTARGUMENT": "",
    }
    r = session.post(REPORT_URL, data=payload, timeout=30)
    print(f"    ✅ דוח נבחר: {len(r.text):,} תווים")
    return r.text

# ── שליפת פירוט לקוח ─────────────────────────────────────────
def fetch_customer_detail(session, html_after_select, code, name):
    vs = get_viewstate(session, html_after_select)
    payload = {
        **vs,
        "__EVENTTARGET":   "",
        "__EVENTARGUMENT": "",
        "ctl00$MainContent$rptWhereFieldsParameters$ctl00$ctrlReportField$hdnReportFieldID":              "200",
        "ctl00$MainContent$rptWhereFieldsParameters$ctl00$ctrlReportField$hdnIsInputControlParameter":    "False",
        "ctl00$MainContent$rptWhereFieldsParameters$ctl00$ctrlReportField$hdnReportFieldTypeID":          "7",
        "ctl00$MainContent$rptWhereFieldsParameters$ctl00$ctrlReportField$txtAutoCompleteField":          name,
        "ctl00$MainContent$rptWhereFieldsParameters$ctl00$ctrlReportField$hdnAutoCompleteSelectedValue":  str(code),
        "ctl00$MainContent$btnConfirm": "הצג דו''ח",
    }
    r = session.post(REPORT_URL, data=payload, timeout=60)
    return parse_customer_transactions(r.text, code)

# ── פרסור טבלה ───────────────────────────────────────────────
def parse_customer_transactions(html, code, session=None):
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", {"id": "MainContent_gvReportData"})
    if not table:
        return []
    
    # חשב תאריך 12 חודשים אחרונים
    from datetime import datetime, timedelta
    cutoff = datetime.now() - timedelta(days=365)
    
    transactions = []
    rows = table.find_all("tr")
    
    for i, row in enumerate(rows):
        cells = row.find_all("td")
        if len(cells) < 8:
            continue
        
        try:
            doc_type  = cells[1].get_text(strip=True)
            date_str  = cells[4].get_text(strip=True)  # DD-MM-YYYY
            amount    = cells[7].get_text(strip=True)
            balance   = cells[8].get_text(strip=True) if len(cells) > 8 else ""
            
            if not doc_type or not date_str:
                continue
            
            # בדוק אם תנועה חדשה מספיק לפרסור items
            items = []
            try:
                dt = datetime.strptime(date_str, "%d-%m-%Y")
                is_recent = dt >= cutoff
            except:
                is_recent = False
            
            # שלוף items רק לתנועות חדשות עם כפתור +
            if is_recent and session:
                btn = row.find("input", {"type": "image"}) or row.find("img", {"src": lambda s: s and "plus" in s.lower()})
                # חפש כפתור postback
                ctrl_match = None
                for inp in row.find_all("input"):
                    nm = inp.get("name", "")
                    if "imgShowNestedGrid" in nm:
                        ctrl_match = nm
                        break
                
                if ctrl_match:
                    try:
                        vs = get_viewstate(session)
                        vs["__EVENTTARGET"] = ctrl_match
                        vs["__EVENTARGUMENT"] = ""
                        r2 = session.post(REPORT_URL, data=vs, timeout=30)
                        soup2 = BeautifulSoup(r2.text, "lxml")
                        nested = soup2.find("table", {"id": f"MainContent_gvReportData_gvNested_{i}"})
                        if nested:
                            for nrow in nested.find_all("tr")[1:]:
                                ncells = nrow.find_all("td")
                                if len(ncells) >= 4:
                                    items.append({
                                        "code":  ncells[0].get_text(strip=True),
                                        "desc":  ncells[1].get_text(strip=True),
                                        "qty":   ncells[2].get_text(strip=True),
                                        "price": ncells[3].get_text(strip=True),
                                    })
                        time.sleep(0.3)
                    except Exception as e:
                        pass
            
            transactions.append({
                "type":    doc_type,
                "date":    date_str,
                "amount":  amount,
                "balance": balance,
                "items":   items,
            })
        except Exception:
            continue
    
    return transactions

# ── שליחה ל-Worker ───────────────────────────────────────────
def push_to_worker(code, transactions):
    if not WORKER_URL:
        return
    try:
        res = requests.post(
            f"{WORKER_URL}/customer-detail",
            json={"code": str(code), "transactions": transactions},
            timeout=15,
        )
        return res.ok
    except Exception as e:
        print(f"    ⚠️  Worker שגיאה: {e}")
        return False

# ── קבלת לקוחות עם חוב מ-Worker ─────────────────────────────
def get_debtors():
    try:
        r = requests.get(f"{WORKER_URL}/state", timeout=15)
        state = r.json()
        custs = state.get("custs", {})
        return [(c["code"], c["name"], c["balance"]) 
                for c in custs.values() 
                if c.get("balance", 0) > 0]
    except Exception as e:
        print(f"⚠️  לא ניתן למשוך לקוחות: {e}")
        return []

# ── Main ─────────────────────────────────────────────────────
def main():
    print("=" * 50)
    print("  פירוט קניות הקפה לכל לקוח")
    print("=" * 50)
    
    if not LOGIN_URL:
        raise Exception("חסר NEWORDER_LOGIN_URL")
    
    session = login()
    
    # גלה ה-NAV של הדוח
    nav = find_customer_report_nav(session)
    if not nav:
        print("❌ לא ניתן למצוא דוח תנועות הקפה ללקוח")
        print("   הגדר CUSTOMER_REPORT_NAV=ctl00$ctrlNavigationBar$ctlXXX")
        return
    
    # בחר את הדוח פעם אחת
    print(f"\n[2] בוחר דוח לקוח...")
    html_report = select_customer_report(session, nav)
    
    # קבל רשימת חייבים
    debtors = get_debtors()
    print(f"\n[3] מעבד {len(debtors)} לקוחות עם חוב...")
    
    success = 0
    for i, (code, name, balance) in enumerate(debtors[:50]):  # מגביל ל-50 ראשונים
        print(f"  [{i+1}/{min(len(debtors),50)}] {name} ({code}) — ₪{balance}")
        try:
            txns = fetch_customer_detail(session, html_report, code, name)
            if txns:
                pushed = push_to_worker(code, txns)
                print(f"    ✅ {len(txns)} תנועות {'→ Worker' if pushed else ''}")
                success += 1
            else:
                print(f"    ⚪ אין תנועות")
            time.sleep(1)  # הגנה על שרת
        except Exception as e:
            print(f"    ❌ שגיאה: {e}")
    
    print(f"\n✅ הסתיים: {success}/{min(len(debtors),50)} לקוחות עובדו")

if __name__ == "__main__":
    main()
