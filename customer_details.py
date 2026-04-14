#!/usr/bin/env python3
"""
שליפת פירוט קניות הקפה לכל לקוח עם חוב
כולל לחיצה על + לכל שורה לשליפת פריטים
"""
import os, re, time, requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

LOGIN_URL           = "https://cellular.neworder.co.il/heb/direct.aspx?UserName=nChDORjeuASklAO4HRJVcQ==&StoreName=eL/mCT/S9JtKfrclQgpe2Q==&password=y5leGHLlRcO1YFjej3CeHQ=="
WORKER_URL          = os.environ.get("WEBHOOK_URL", "https://debt-worker.bnaya-av.workers.dev").rstrip("/")
REPORT_URL          = "https://cellular.neworder.co.il/heb/reports/reportgenerator.aspx"
CUSTOMER_REPORT_NAV = os.environ.get("CUSTOMER_REPORT_NAV", "ctl00$ctrlNavigationBar$ctl274")
UA                  = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
DAYS_BACK           = 365  # שלוף פריטים רק לתנועות מ-12 חודשים אחרונים

def login():
    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    print("[1] מתחבר...")
    r = session.get(LOGIN_URL, timeout=30)
    print(f"    ✅ {r.url.split('/')[-1].split('?')[0]}")
    return session

def get_vs(session, html=None):
    if html is None:
        r = session.get(REPORT_URL, timeout=30)
        html = r.text
    soup = BeautifulSoup(html, "lxml")
    def v(n): f = soup.find("input", {"name": n}); return f["value"] if f else ""
    return {
        "__VIEWSTATE":          v("__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": v("__VIEWSTATEGENERATOR") or "3B3F7A04",
        "__EVENTVALIDATION":    v("__EVENTVALIDATION"),
        "__EVENTTARGET":        "",
        "__EVENTARGUMENT":      "",
    }

def select_report(session):
    vs = get_vs(session)
    vs["__EVENTTARGET"] = CUSTOMER_REPORT_NAV
    r = session.post(REPORT_URL, data=vs, timeout=30)
    print(f"    ✅ דוח נבחר ({len(r.text):,} תווים)")
    return r.text

def fetch_customer(session, html_base, code, name):
    vs = get_vs(session, html_base)
    soup_base = BeautifulSoup(html_base, "lxml")

    payload = {
        **vs,
        "__EVENTTARGET": "",
        "ctl00$MainContent$rptWhereFieldsParameters$ctl00$ctrlReportField$hdnReportFieldID": "200",
        "ctl00$MainContent$rptWhereFieldsParameters$ctl00$ctrlReportField$hdnIsInputControlParameter": "False",
        "ctl00$MainContent$rptWhereFieldsParameters$ctl00$ctrlReportField$hdnReportFieldTypeID": "7",
        "ctl00$MainContent$rptWhereFieldsParameters$ctl00$ctrlReportField$txtAutoCompleteField": name,
        "ctl00$MainContent$rptWhereFieldsParameters$ctl00$ctrlReportField$hdnAutoCompleteSelectedValue": str(code),
        "ctl00$MainContent$btnConfirm": "הצג דו''ח",
    }
    # הגבל לשנה אחרונה
    date_from = (datetime.now() - timedelta(days=DAYS_BACK)).strftime("%d/%m/%Y")
    date_to   = datetime.now().strftime("%d/%m/%Y")
    for inp in soup_base.find_all("input"):
        nm = inp.get("name", "")
        if "txtFromDate" in nm: payload[nm] = date_from
        if "txtToDate"   in nm: payload[nm] = date_to

    r = session.post(REPORT_URL, data=payload, timeout=60)
    return parse_transactions(r.text, session)

def click_plus(session, html, row_idx):
    """לחץ על כפתור + בשורה מסוימת ושלוף items"""
    soup = BeautifulSoup(html, "lxml")
    vs   = get_vs(session, html)

    # DEBUG: הדפס את שמות ה-inputs בשורה
    table = soup.find("table", {"id": "MainContent_gvReportData"})
    if not table:
        return []
    rows = [r for r in table.find_all("tr") if r.find_all("td")]
    if row_idx >= len(rows):
        return []
    row = rows[row_idx]
    inputs = row.find_all("input")
    names  = [inp.get("name","") for inp in inputs]
    print(f"      DEBUG row {row_idx} inputs: {names}")

    # חפש input של כפתור +
    ctrl = None
    for nm in names:
        if "imgShowNestedGrid" in nm or "ShowNested" in nm or "btnPlus" in nm:
            ctrl = nm
            break
    # ניסיון נוסף — input type=image
    if not ctrl:
        for inp in inputs:
            if inp.get("type","").lower() == "image":
                ctrl = inp.get("name","")
                break

    if not ctrl:
        return []

    print(f"      → לוחץ {ctrl}")
    vs["__EVENTTARGET"]   = ctrl
    vs["__EVENTARGUMENT"] = ""
    r2 = session.post(REPORT_URL, data=vs, timeout=30)
    soup2 = BeautifulSoup(r2.text, "lxml")

    # חפש טבלת nested
    nested = soup2.find("table", {"id": re.compile(r"gvNested")})
    if not nested:
        nested = soup2.find("table", {"id": f"MainContent_gvReportData_gvNested_{row_idx}"})
    if not nested:
        return []

    items = []
    for nrow in nested.find_all("tr")[1:]:
        ncells = nrow.find_all("td")
        if len(ncells) >= 4:
            items.append({
                "code":  ncells[0].get_text(strip=True),
                "desc":  ncells[1].get_text(strip=True),
                "qty":   ncells[2].get_text(strip=True),
                "price": ncells[3].get_text(strip=True),
            })
    return items

def parse_transactions(html, session=None):
    soup  = BeautifulSoup(html, "lxml")
    table = soup.find("table", {"id": "MainContent_gvReportData"})
    if not table:
        return []

    cutoff = datetime.now() - timedelta(days=DAYS_BACK)
    transactions = []
    data_rows = [r for r in table.find_all("tr") if r.find_all("td")]

    for idx, row in enumerate(data_rows):
        cells = row.find_all("td")
        if len(cells) < 8:
            continue
        try:
            doc_type = cells[1].get_text(strip=True)
            date_str = cells[4].get_text(strip=True)
            amount   = cells[7].get_text(strip=True)
            balance  = cells[8].get_text(strip=True) if len(cells) > 8 else ""
            if not doc_type or not date_str:
                continue

            items = []
            try:
                dt = datetime.strptime(date_str, "%d-%m-%Y")
                if dt >= cutoff and session:
                    items = click_plus(session, html, idx)
                    if items:
                        print(f"        ✅ {len(items)} פריטים")
                    time.sleep(0.5)
            except Exception as e:
                print(f"        ⚠️ items error: {e}")

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

def push_to_worker(code, transactions):
    try:
        res = requests.post(
            f"{WORKER_URL}/customer-detail",
            json={"code": str(code), "transactions": transactions},
            timeout=15,
        )
        return res.ok
    except Exception as e:
        print(f"    ⚠️ Worker: {e}")
        return False

def get_debtors():
    try:
        r = requests.get(f"{WORKER_URL}/state", timeout=15)
        custs = r.json().get("custs", {})
        return [(c["code"], c["name"], c["balance"])
                for c in custs.values() if c.get("balance", 0) > 0]
    except Exception as e:
        print(f"⚠️ {e}")
        return []

def main():
    print("=" * 50)
    print("  פירוט קניות הקפה לכל לקוח")
    print("=" * 50)

    session = login()

    print("\n[2] בוחר דוח לקוח...")
    html_report = select_report(session)

    debtors = get_debtors()
    print(f"\n[3] מעבד {len(debtors)} לקוחות...")

    for i, (code, name, balance) in enumerate(debtors[:50]):
        print(f"  [{i+1}/50] {name} ({code}) — ₪{balance}")
        try:
            txns = fetch_customer(session, html_report, code, name)
            if txns:
                push_to_worker(code, txns)
                print(f"    ✅ {len(txns)} תנועות → Worker")
            else:
                print("    ⚪ אין תנועות")
            time.sleep(1)
        except Exception as e:
            print(f"    ❌ {e}")

    print("\n✅ הסתיים")

if __name__ == "__main__":
    main()
