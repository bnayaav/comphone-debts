#!/usr/bin/env python3
"""
שליפת פירוט קניות הקפה לכל לקוח עם חוב
כולל לחיצת + לפירוט פריטים
"""
import os, requests, time, logging
from bs4 import BeautifulSoup
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
log = logging.getLogger(__name__)

LOGIN_URL   = "https://cellular.neworder.co.il/heb/direct.aspx?UserName=nChDORjeuASklAO4HRJVcQ==&StoreName=eL/mCT/S9JtKfrclQgpe2Q==&password=y5leGHLlRcO1YFjej3CeHQ=="
REPORT_URL  = "https://cellular.neworder.co.il/heb/reports/reportgenerator.aspx"
WORKER_URL  = os.environ.get("WEBHOOK_URL", "https://debt-worker.bnaya-av.workers.dev").rstrip("/")
NAV_CTRL    = os.environ.get("CUSTOMER_REPORT_NAV", "ctl00$ctrlNavigationBar$ctl274")

def login():
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    r = s.get(LOGIN_URL, timeout=30)
    log.info(f"התחברות: {r.status_code}")
    return s

def get_vs(soup):
    def v(n):
        el = soup.find("input", {"name": n})
        return el["value"] if el else ""
    return {
        "__VIEWSTATE":          v("__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": v("__VIEWSTATEGENERATOR") or "3B3F7A04",
        "__EVENTVALIDATION":    v("__EVENTVALIDATION"),
        "__EVENTTARGET":        "",
        "__EVENTARGUMENT":      "",
    }

def get_hidden_keys(soup):
    """שדות hdnPrimaryGridKeyValue — נדרשים לבקשות postback"""
    keys = {}
    for inp in soup.find_all("input", {"name": lambda n: n and "hdnPrimaryGridKeyValue" in n}):
        keys[inp["name"]] = inp.get("value", "")
    return keys

def parse_rows(soup):
    table = soup.find("table", {"id": "MainContent_gvReportData"})
    if not table:
        return []
    rows = []
    for tr in table.find_all("tr")[1:]:
        cells = tr.find_all("td")
        if len(cells) < 9:
            continue
        plus = tr.find("input", {"type": "image"})
        plus_name = plus["name"] if plus else None
        rows.append({
            "plusBtnName": plus_name,
            "invoice":     cells[1].get_text(strip=True),
            "docType":     cells[2].get_text(strip=True),
            "date":        cells[4].get_text(strip=True),
            "amount":      cells[8].get_text(strip=True) if len(cells) > 8 else "",
            "balance":     cells[9].get_text(strip=True) if len(cells) > 9 else "",
            "items":       [],
        })
    return rows

def parse_nested(soup):
    tables = soup.find_all("table", id=lambda x: x and "gvNested" in x)
    if not tables:
        return []
    table = tables[-1]
    items = []
    for tr in table.find_all("tr")[1:]:
        cells = tr.find_all("td")
        if len(cells) >= 4:
            items.append({
                "code":  cells[0].get_text(strip=True),
                "desc":  cells[1].get_text(strip=True),
                "qty":   cells[2].get_text(strip=True),
                "price": cells[3].get_text(strip=True),
            })
    return items

def is_recent(date_str):
    try:
        dt = datetime.strptime(date_str, "%d-%m-%Y")
        return dt >= datetime.now() - timedelta(days=365)
    except:
        return False

def get_customer_txns(session, code, name):
    # 1. טעינת דף
    r = session.get(REPORT_URL, timeout=30)
    soup = BeautifulSoup(r.text, "lxml")
    vs = get_vs(soup)

    # 2. ניווט לדוח תנועות הקפה ללקוח
    vs["__EVENTTARGET"] = NAV_CTRL
    r = session.post(REPORT_URL, data=vs, timeout=30)
    soup = BeautifulSoup(r.text, "lxml")
    vs = get_vs(soup)

    # 3. שלח דוח ללקוח
    payload = {
        **vs,
        "__EVENTTARGET": "",
        "ctl00$MainContent$rptWhereFieldsParameters$ctl00$ctrlReportField$hdnReportFieldID":              "200",
        "ctl00$MainContent$rptWhereFieldsParameters$ctl00$ctrlReportField$hdnIsInputControlParameter":    "False",
        "ctl00$MainContent$rptWhereFieldsParameters$ctl00$ctrlReportField$hdnReportFieldTypeID":          "7",
        "ctl00$MainContent$rptWhereFieldsParameters$ctl00$ctrlReportField$txtAutoCompleteField":          name,
        "ctl00$MainContent$rptWhereFieldsParameters$ctl00$ctrlReportField$hdnAutoCompleteSelectedValue":  str(code),
        "ctl00$MainContent$btnConfirm": "הצג דו''ח",
    }
    r = session.post(REPORT_URL, data=payload, timeout=60)
    soup = BeautifulSoup(r.text, "lxml")
    vs = get_vs(soup)

    # 4. פרסר שורות
    all_rows = parse_rows(soup)
    recent_rows = [row for row in all_rows if is_recent(row["date"])]
    log.info(f"    {len(recent_rows)} תנועות חדשות (מתוך {len(all_rows)})")

    # 5. לכל תנועה חדשה — לחץ + לפירוט פריטים
    for idx, row in enumerate(recent_rows):
        if not row["plusBtnName"]:
            continue

        log.info(f"    [{idx+1}/{len(recent_rows)}] לוחץ + תנועה {row['invoice']} | {row['date']}")

        hidden = get_hidden_keys(soup)
        payload_plus = {
            **vs,
            "__EVENTTARGET":  "",
            "__EVENTARGUMENT": "",
            row["plusBtnName"] + ".x": "1",
            row["plusBtnName"] + ".y": "1",
            "ctl00$MainContent$rptWhereFieldsParameters$ctl00$ctrlReportField$hdnReportFieldID":              "200",
            "ctl00$MainContent$rptWhereFieldsParameters$ctl00$ctrlReportField$hdnIsInputControlParameter":    "False",
            "ctl00$MainContent$rptWhereFieldsParameters$ctl00$ctrlReportField$hdnReportFieldTypeID":          "7",
            "ctl00$MainContent$rptWhereFieldsParameters$ctl00$ctrlReportField$txtAutoCompleteField":          name,
            "ctl00$MainContent$rptWhereFieldsParameters$ctl00$ctrlReportField$hdnAutoCompleteSelectedValue":  str(code),
            **hidden,
        }

        r2 = session.post(REPORT_URL, data=payload_plus, timeout=30)
        soup2 = BeautifulSoup(r2.text, "lxml")
        items = parse_nested(soup2)
        row["items"] = items
        log.info(f"      {len(items)} פריטים")

        # עדכן soup ו-vs לבקשה הבאה
        soup = soup2
        vs = get_vs(soup2)
        time.sleep(0.4)

    return recent_rows

def push_to_worker(code, transactions):
    try:
        res = requests.post(
            f"{WORKER_URL}/customer-detail",
            json={"code": str(code), "transactions": transactions},
            timeout=15,
        )
        return res.ok
    except Exception as e:
        log.error(f"Worker: {e}")
        return False

def get_debtors():
    try:
        r = requests.get(f"{WORKER_URL}/state", timeout=15)
        custs = r.json().get("custs", {})
        return [(c["code"], c["name"], c["balance"])
                for c in custs.values() if c.get("balance", 0) > 0]
    except Exception as e:
        log.error(e)
        return []

def main():
    log.info("=" * 50)
    log.info("  פירוט קניות הקפה לכל לקוח")
    log.info("=" * 50)

    session = login()
    debtors = get_debtors()
    log.info(f"מעבד {len(debtors)} לקוחות...")

    for i, (code, name, balance) in enumerate(debtors[:50]):
        log.info(f"[{i+1}/50] {name} ({code}) ₪{balance}")
        try:
            txns = get_customer_txns(session, code, name)
            if txns:
                push_to_worker(code, txns)
                log.info(f"  ✅ {len(txns)} תנועות → Worker")
            else:
                log.info("  ⚪ אין תנועות")
            time.sleep(1)
        except Exception as e:
            log.error(f"  ❌ {e}")

    log.info("✅ הסתיים")

if __name__ == "__main__":
    main()
