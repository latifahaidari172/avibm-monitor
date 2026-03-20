#!/usr/bin/env python3
"""
AVIBM Master Monitor — runs for ALL active customers in the database.
Fetches customers from Supabase, checks each vehicle, auto-books if earlier slot found.
"""

import csv, json, os, re, smtplib, sys, time
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# ── Constants ─────────────────────────────────────────────────────────────────

SUPABASE_URL     = os.environ["SUPABASE_URL"]
SUPABASE_KEY     = os.environ["SUPABASE_SERVICE_KEY"]   # service role key
TWOCAPTCHA_KEY   = os.environ["TWOCAPTCHA_API_KEY"]
GMAIL_ADDR       = os.environ["GMAIL_ADDRESS"]
GMAIL_PASS       = os.environ["GMAIL_APP_PASSWORD"]

QLD_BOOKING_URL  = "https://wovi.com.au/bookings/"
SA_BOOKING_URL   = "https://www.ecom.transport.sa.gov.au/et/rescheduleAVehicleInspectionBooking.do"
SA_HOME_URL      = "https://www.ecom.transport.sa.gov.au/et/welcome.jsp"

QLD_LOCATIONS    = ["Brisbane", "Bundaberg", "Burleigh Heads", "Cairns", "Mackay", "Narangba", "Rockhampton City", "Toowoomba", "Townsville", "Yatala"]
QLD_CAPTCHA_KEY  = "6LfAG_0pAAAAAFQzCmk7OQ4roYKXfgYFAPwsVo-5"

# Supabase requires both apikey and Authorization headers
HEADERS          = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def now_str():
    return datetime.now(timezone.utc).strftime("%d/%m/%Y %I:%M:%S %p UTC")

def log(msg, level="INFO"):
    print(f"[{now_str()}] [{level}] {msg}", flush=True)

def parse_date(s):
    for pat, fn in [
        (r'(\d{4})-(\d{2})-(\d{2})', lambda m: datetime(int(m[1]),int(m[2]),int(m[3]))),
        (r'(\d{1,2})/(\d{1,2})/(\d{4})', lambda m: datetime(int(m[3]),int(m[2]),int(m[1]))),
    ]:
        m = re.search(pat, s)
        if m:
            try: return fn(m)
            except: pass
    return None

def send_email(subject, body, to, html=None):
    from email.mime.multipart import MIMEMultipart
    if html:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = GMAIL_ADDR
        msg["To"]      = to
        msg.attach(MIMEText(body, "plain"))
        msg.attach(MIMEText(html, "html"))
    else:
        msg = MIMEText(body, "plain")
        msg["Subject"] = subject
        msg["From"]    = GMAIL_ADDR
        msg["To"]      = to
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_ADDR, GMAIL_PASS)
            s.sendmail(GMAIL_ADDR, to, msg.as_string())
        log(f"Email sent to {to}: {subject}")
    except Exception as e:
        log(f"Email failed: {e}", "ERROR")

# ── Supabase ──────────────────────────────────────────────────────────────────

def db_get(table, params=""):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}?{params}", headers=HEADERS)
    try:
        data = r.json()
        if r.status_code not in (200, 201):
            log(f"Supabase error {r.status_code}: {data}", "ERROR")
            return []
        return data
    except Exception:
        log(f"Supabase response error: {r.status_code} {r.text}", "ERROR")
        return []

def db_patch(table, match_key, match_val, data):
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/{table}?{match_key}=eq.{match_val}",
        headers=HEADERS, json=data
    )

def db_post(table, data):
    requests.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=HEADERS, json=data)

def log_result(customer_id, vehicle_id, state, location, result, detail=""):
    db_post("booking_logs", {
        "customer_id": customer_id,
        "vehicle_id":  vehicle_id,
        "state":       state,
        "location":    location or "",
        "result":      result,
        "detail":      detail,
    })

# ── Chrome ────────────────────────────────────────────────────────────────────

def make_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--log-level=3")
    d = webdriver.Chrome(options=opts)
    d.set_page_load_timeout(30)
    return d

# ── 2captcha ──────────────────────────────────────────────────────────────────

def solve_captcha(site_key, page_url):
    try:
        r = requests.post("http://2captcha.com/in.php", data={
            "key": TWOCAPTCHA_KEY, "method": "userrecaptcha",
            "googlekey": site_key, "pageurl": page_url, "json": 1,
        }, timeout=30).json()
        if r.get("status") != 1: return None
        cid = r["request"]
        for _ in range(24):
            time.sleep(5)
            try:
                resp = requests.get("http://2captcha.com/res.php", params={
                    "key": TWOCAPTCHA_KEY, "action": "get", "id": cid, "json": 1
                }, timeout=10)
                try:
                    p = resp.json()
                    if p.get("status") == 1: return p["request"]
                    if p.get("request") != "CAPCHA_NOT_READY": return None
                except:
                    t = resp.text.strip()
                    if t.startswith("OK|"): return t[3:]
                    if t != "CAPCHA_NOT_READY": return None
            except: continue
        return None
    except: return None

# ── Form helpers ──────────────────────────────────────────────────────────────

def fill(driver, value, *names):
    for name in names:
        for attr in ["name", "id", "ng-model"]:
            try:
                el = driver.find_element(By.XPATH, f"//input[@{attr}='{name}']")
                driver.execute_script(
                    "arguments[0].value=arguments[1];"
                    "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
                    "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
                    el, value)
                return True
            except NoSuchElementException: pass
    return False

def sel_by(driver, value, *xpaths):
    for xpath in xpaths:
        try:
            el = driver.find_element(By.XPATH, xpath)
            try: Select(el).select_by_value(value)
            except: Select(el).select_by_visible_text(value)
            return True
        except: pass
    return False

def click_next(driver, wait):
    phrases = [
        "submit my booking request",
        "submit booking request",
        "submit my booking",
        "next",
    ]
    for phrase in phrases:
        try:
            btns = driver.find_elements(By.XPATH,
                f"//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{phrase}')] | "
                f"//input[@type='submit'][contains(translate(@value,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{phrase}')]"
            )
            for btn in btns:
                if btn.is_displayed():
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                    time.sleep(0.5)
                    driver.execute_script("arguments[0].click();", btn)
                    time.sleep(2)
                    return True
        except Exception:
            continue
    # Fallback to any submit button
    try:
        btn = driver.find_element(By.XPATH, "//button[@type='submit'] | //input[@type='submit']")
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.5)
        driver.execute_script("arguments[0].click();", btn)
        time.sleep(2)
        return True
    except Exception:
        return False

# ── QLD Monitor ───────────────────────────────────────────────────────────────

def qld_find_slots(driver, cutoff, label, locations=None):
    wait = WebDriverWait(driver, 20)
    slots = []
    check_locations = locations if locations else QLD_LOCATIONS
    for location in check_locations:
        try:
            sel = wait.until(EC.presence_of_element_located((By.XPATH,
                "//select[.//option[contains(text(),'Brisbane')]]"
            )))
            for opt in Select(sel).options:
                if location.lower() in opt.text.lower():
                    Select(sel).select_by_visible_text(opt.text); break
            else: continue

            try:
                WebDriverWait(driver, 8).until(lambda d: len(
                    d.find_elements(By.XPATH, "//div[@ng-click='setDateValue(day)']")) > 0)
            except TimeoutException:
                log(f"  {label} / {location}: calendar timeout"); continue

            for item in driver.find_elements(By.XPATH, "//div[@ng-click='setDateValue(day)']"):
                try:
                    d = driver.execute_script(
                        "try{var s=angular.element(arguments[0]).scope();"
                        "if(!s||!s.day||!s.day.available||!s.day.thisMonth) return null;"
                        "return s.day.value;}catch(e){return null;}", item)
                    if not d: continue
                    dt = parse_date(d)
                    if dt and dt < cutoff: slots.append((dt, d, location))
                except: continue

            found = sum(1 for s in slots if s[2] == location)
            log(f"  {label} / {location}: {found} slot(s) before cutoff")
        except Exception as e:
            log(f"  {label} / {location}: error — {e}", "WARN")

    slots.sort(key=lambda x: x[0])
    return slots

def qld_book_slot(location, date_str, customer, vehicle):
    driver = make_driver()
    wait   = WebDriverWait(driver, 20)
    try:
        log(f"  [BOOK] Loading WOVI page...")
        driver.get(QLD_BOOKING_URL)
        time.sleep(3)

        # Select location
        log(f"  [BOOK] Selecting location: {location}")
        sel = wait.until(EC.presence_of_element_located((By.XPATH,
            "//select[.//option[contains(text(),'Brisbane')]]")))
        for opt in Select(sel).options:
            if location.lower() in opt.text.lower():
                Select(sel).select_by_visible_text(opt.text); break
        time.sleep(3)

        # Wait for calendar
        log(f"  [BOOK] Waiting for calendar...")
        try:
            WebDriverWait(driver, 8).until(lambda d: len(
                d.find_elements(By.XPATH, "//div[@ng-click='setDateValue(day)']")) > 0)
        except TimeoutException:
            log(f"  [BOOK] Calendar did not load", "WARN")
            return (False, "")

        # Click date
        log(f"  [BOOK] Clicking date: {date_str}")
        clicked = False
        for item in driver.find_elements(By.XPATH, "//div[@ng-click='setDateValue(day)']"):
            d = driver.execute_script(
                "try{var s=angular.element(arguments[0]).scope();"
                "if(!s||!s.day) return null;return s.day.value;}catch(e){return null;}", item)
            if d == date_str:
                driver.execute_script("arguments[0].click();", item)
                clicked = True; time.sleep(2); break
        if not clicked:
            log(f"  [BOOK] Could not find date {date_str} on calendar", "WARN")
            return (False, "")

        log(f"  [BOOK] Selecting time slot...")
        # Select earliest time and capture it
        selected_time = ""
        try:
            ts = driver.find_element(By.XPATH,
                "//select[contains(@ng-model,'time') or contains(@ng-change,'time')]")
            opts = sorted([o for o in Select(ts).options
                           if o.get_attribute("value") not in ("","null","undefined","0")],
                          key=lambda o: o.text)
            if opts:
                Select(ts).select_by_visible_text(opts[0].text)
                selected_time = opts[0].text
        except NoSuchElementException: pass

        time.sleep(1)
        log(f"  [BOOK] Clicking Next (after time)...")
        click_next(driver, wait)
        time.sleep(3)

        # Vehicle details
        log(f"  [BOOK] Filling vehicle details...")
        vtype = vehicle.get("vehicle_type","Car")
        try:
            driver.find_element(By.XPATH, f"//label[contains(normalize-space(.,'{vtype}')]")
            driver.execute_script("arguments[0].click();", driver.find_element(By.XPATH, f"//label[contains(normalize-space(.),'{vtype}')]"))
        except: pass

        fill(driver, vehicle["vin"],            "vin","chassis","VIN","vinChassis")
        fill(driver, vehicle["make"],           "make","vehicleMake")
        fill(driver, vehicle["model"],          "model","vehicleModel")
        # Year is a dropdown
        if not sel_by(driver, vehicle["year"],
            "//select[contains(@name,'year') or contains(@ng-model,'year') or "
            "contains(@name,'buildYear') or contains(@ng-model,'buildYear') or "
            "contains(@name,'buildDateYear') or contains(@ng-model,'buildDateYear')]"):
            fill(driver, vehicle["year"], "year","buildYear","buildDateYear")
        fill(driver, vehicle["colour"],         "colour","color","vehicleColour")
        fill(driver, vehicle["purchased_from"], "purchasedFrom","sellerName")
        sel_by(driver, vehicle.get("build_month",""),
               "//select[contains(@name,'buildDateMonth') or contains(@ng-model,'buildDateMonth')]")
        sel_by(driver, vehicle["damage"],
               "//select[contains(@ng-model,'damage') or contains(@name,'damage')]")
        sel_by(driver, vehicle["purchase_method"],
               "//select[contains(@ng-model,'purchase') or contains(@name,'purchase')]")

        log(f"  [BOOK] Clicking Next (after vehicle details)...")
        click_next(driver, wait)
        time.sleep(2)

        # Customer details
        log(f"  [BOOK] Filling customer details...")
        fill(driver, customer["crn"],        "crn","CRN","licenceNumber","crnLicence")
        fill(driver, customer["first_name"], "firstName","first_name","fname")
        fill(driver, customer["last_name"],  "lastName","last_name","surname")
        fill(driver, customer["address"],    "address","streetAddress","street")
        fill(driver, customer["suburb"],     "suburb","city")
        fill(driver, customer["postcode"],   "postcode","zipCode")
        fill(driver, customer["email"],      "email","emailAddress")
        fill(driver, customer["phone"],      "phone","mobile","mobileNumber")

        log(f"  [BOOK] Clicking Next (after customer details)...")
        click_next(driver, wait)
        time.sleep(2)

        # CAPTCHA
        log(f"  [BOOK] Solving CAPTCHA...")
        token = solve_captcha(QLD_CAPTCHA_KEY, QLD_BOOKING_URL)
        if not token: return False

        driver.execute_script("var el=document.getElementById('g-recaptcha-response');if(el) el.innerHTML=arguments[0];", token)
        driver.execute_script("var el=document.querySelector('[name=\"g-recaptcha-response\"]');if(el) el.value=arguments[0];", token)
        time.sleep(1)

        log(f"  [BOOK] Clicking Submit My Booking Request...")
        click_next(driver, wait)
        time.sleep(4)

        log(f"  [BOOK] Page after submit: {driver.title}")

        # Handle "Would you like to update your booking?" popup — click "Update Booking"
        try:
            WebDriverWait(driver, 12).until(
                EC.presence_of_element_located((By.XPATH,
                    "//*[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'update booking')]"
                ))
            )
            time.sleep(1)
            log("  Popup detected — looking for 'Update Booking' button")

            clicked_popup = False
            # Try every possible element containing "update booking"
            candidates = driver.find_elements(By.XPATH,
                "//*[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'update booking')]"
            )
            for el in candidates:
                try:
                    if el.is_displayed():
                        driver.execute_script("arguments[0].click();", el)
                        log("  Clicked 'Update Booking' on popup")
                        clicked_popup = True
                        time.sleep(4)
                        break
                except Exception:
                    continue

            if not clicked_popup:
                log("  Could not click 'Update Booking' — trying JS modal dismiss", "WARN")
                # Try clicking any visible modal button as fallback
                try:
                    modal_btns = driver.find_elements(By.XPATH,
                        "//div[contains(@class,'modal') or contains(@class,'dialog') or contains(@class,'popup')]//button"
                    )
                    for btn in modal_btns:
                        if btn.is_displayed() and 'update' in btn.text.lower():
                            driver.execute_script("arguments[0].click();", btn)
                            log("  Clicked modal button via fallback")
                            time.sleep(4)
                            break
                except Exception:
                    pass

        except TimeoutException:
            log("  No 'Update Booking' popup appeared — continuing")

        # After clicking Update Booking, wait for confirmation page
        time.sleep(4)

        page_source = driver.page_source
        page_lower  = page_source.lower()
        page_title  = driver.title
        page_url    = driver.current_url
        log(f"  Page after final step: {page_title}")
        log(f"  URL: {page_url}")

        # Only confirm if WOVI shows a REAL booking confirmation
        # Must contain "booking has been secured" OR a booking reference number
        confirmed = (
            "booking has been secured" in page_lower
            or "your booking reference" in page_lower
            or "inspection has been booked" in page_lower
        )

        # Log a snippet of the page to help debug
        snippet = page_source[:500].replace("\n", " ").replace("\r", "")
        log(f"  Page snippet: {snippet[:200]}")

        if confirmed:
            log(f"  ✅ Booking confirmed — WOVI confirmation phrase detected!")
        else:
            log(f"  ❌ Booking NOT confirmed — WOVI confirmation phrase NOT found", "WARN")
            log(f"  This may be a false positive — NOT marking as booked", "WARN")

        return (confirmed, selected_time)

    except Exception as e:
        log(f"QLD booking error: {e}", "ERROR"); return (False, "")
    finally:
        driver.quit()

# ── SA Monitor ────────────────────────────────────────────────────────────────

def sa_check(customer, vehicle, cutoff):
    try:
        sess = requests.Session()
        sess.get(SA_HOME_URL, timeout=15)
        sess.get(SA_BOOKING_URL, timeout=15)

        dob = "".join(c for c in customer.get("date_of_birth","") if c.isdigit())
        preferred = (datetime.now() + timedelta(days=90)).strftime("%d%m%Y")

        r = sess.post(SA_BOOKING_URL, data={
            "clientNumber":       customer["licence_number"],
            "clientSurnameOrgName": customer["last_name"],
            "clientDOB":          dob,
        }, timeout=15)

        r2 = sess.post(SA_BOOKING_URL, data={"preferredDate": preferred}, timeout=15)

        # Parse available slots
        import re as _re
        slots_raw = _re.findall(r'<option[^>]*value="[^"]*"[^>]*>(From[^<]+)</option>', r2.text)
        slots_raw += _re.findall(r'From\s+\w+\s+\d{1,2}/\d{2}/\d{4}\s+\d{2}:\d{2}', r2.text)

        available = []
        for raw in slots_raw:
            clean = raw.replace('\xa0',' ').strip()
            m = _re.search(r'(\d{1,2}/\d{2}/\d{4})', clean)
            if m:
                dt = parse_date(m.group(1))
                if dt and dt < cutoff:
                    available.append((dt, clean))

        available.sort(key=lambda x: x[0])
        return available

    except Exception as e:
        log(f"SA check error: {e}", "ERROR")
        return []

# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    log("=" * 60)
    log("AVIBM Master Monitor — checking all active customers")
    log("=" * 60)

    # Fetch all active customers with their vehicles
    log(f"Connecting to Supabase: {SUPABASE_URL}")
    log(f"Key prefix: {SUPABASE_KEY[:20]}...")
    customers = db_get("customers", "active=eq.true&select=*,vehicles(*)")
    if isinstance(customers, dict) and customers.get("error"):
        log(f"Failed to fetch customers: {customers}", "ERROR")
        sys.exit(1)
    if not isinstance(customers, list):
        log(f"Unexpected response from Supabase: {customers}", "ERROR")
        sys.exit(1)

    active_customers = [c for c in customers if isinstance(c, dict) and c.get("active")]
    log(f"Found {len(active_customers)} active customer(s)")

    if not active_customers:
        log("No active customers — nothing to do.")
        return

    # Group QLD customers by whether they need Chrome
    qld_customers = [c for c in active_customers if c.get("state") == "QLD"]
    sa_customers  = [c for c in active_customers if c.get("state") == "SA"]

    # ── QLD: single browser session for all checking ──────────────────────────
    if qld_customers:
        # Sort customers by tier priority: priority first, then standard, then basic
        TIER_ORDER = {"priority": 0, "standard": 1, "basic": 2}
        TIER_DELAY = {"priority": 0, "standard": 30, "basic": 60}
        TIER_LABEL = {"priority": "🥇 PRIORITY", "standard": "🥈 STANDARD", "basic": "🥉 BASIC"}

        qld_customers.sort(key=lambda c: TIER_ORDER.get(c.get("tier","standard"), 1))

        log(f"Checking {len(qld_customers)} QLD customer(s) (sorted by tier)...")
        driver = make_driver()
        booking_jobs = []

        try:
            driver.get(QLD_BOOKING_URL)
            time.sleep(3)

            for customer in qld_customers:
                tier = customer.get("tier", "standard")
                vehicles = [v for v in (customer.get("vehicles") or []) if v.get("active")]
                for vehicle in vehicles:
                    raw_cutoff = vehicle.get("cutoff_date","")
                    cutoff = parse_date(raw_cutoff)
                    if not cutoff:
                        log(f"  Skipping {customer['first_name']} — invalid cutoff: {raw_cutoff}", "WARN")
                        continue

                    # Auto-deactivate vehicle if cutoff date was more than 24 hours ago
                    now_naive = datetime.now()
                    if (now_naive - cutoff).total_seconds() > 86400:
                        log(f"  Auto-deactivating vehicle — cutoff {cutoff.strftime('%d/%m/%Y')} has passed by more than 24 hours")
                        db_patch("vehicles", "id", vehicle["id"], {"active": False})
                        continue

                    label = f"{customer['first_name']} {customer['last_name']} / {vehicle.get('label', vehicle.get('make','?'))}"
                    log(f"Checking [{TIER_LABEL[tier]}] {label} — cutoff {cutoff.strftime('%d/%m/%Y')}")

                    vehicle_locations = vehicle.get("locations") or QLD_LOCATIONS
                    slots = qld_find_slots(driver, cutoff, label, vehicle_locations)
                    log_result(customer["id"], vehicle["id"], "QLD", "All", "Checked", f"{len(slots)} slots found")

                    if slots:
                        dt, ds, loc = slots[0]
                        log(f"  → Earlier slot: {ds} at {loc}")
                        booking_jobs.append((customer, vehicle, dt, ds, loc, tier))

        finally:
            try: driver.quit()
            except: pass

        # Book in tier order with delays between tiers
        # This ensures priority customers always get first attempt
        current_tier = None
        for customer, vehicle, dt, ds, loc, tier in booking_jobs:
            # Apply delay when tier changes (except for first booking)
            if current_tier is not None and tier != current_tier:
                delay = TIER_DELAY.get(tier, 0)
                if delay > 0:
                    log(f"Tier change to {TIER_LABEL[tier]} — waiting {delay}s before booking...")
                    time.sleep(delay)
            current_tier = tier

            log(f"[{TIER_LABEL[tier]}] Booking {loc} on {ds} for {customer['first_name']} {customer['last_name']}...")
            result = qld_book_slot(loc, ds, customer, vehicle)
            confirmed, booked_time = result if isinstance(result, tuple) else (result, "")
            if confirmed:
                old_cutoff = vehicle.get("cutoff_date", "")
                booking_slot = f"{ds} at {booked_time}" if booked_time else ds
                db_patch("vehicles", "id", vehicle["id"], {
                    "booked_date": ds,
                    "booked_time": booked_time,
                    "booked_location": loc,
                    "previous_cutoff": old_cutoff,
                    "cutoff_date": ds,
                })
                log_result(customer["id"], vehicle["id"], "QLD", loc, "BOOKED", ds)
                booking_html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"/></head>
<body style="margin:0;padding:0;background:#0a0a0a;font-family:Helvetica,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#0a0a0a;padding:40px 20px;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">
<tr><td style="background:#111;border:1px solid #2a2a2a;border-radius:12px 12px 0 0;padding:32px 40px;text-align:center;">
  <div style="font-size:32px;font-weight:900;letter-spacing:0.2em;color:#C9A84C;font-family:Arial Black,Arial,sans-serif;">AVIBM</div>
  <div style="font-size:11px;letter-spacing:0.25em;color:#666;margin-top:4px;text-transform:uppercase;">Australian Vehicle Inspection Booking Monitor</div>
  <div style="width:60px;height:2px;background:#C9A84C;margin:16px auto 0;"></div>
</td></tr>
<tr><td style="background:#141414;border-left:1px solid #2a2a2a;border-right:1px solid #2a2a2a;padding:40px;">
  <div style="text-align:center;margin-bottom:28px;">
    <h1 style="margin:0 0 8px;font-size:28px;font-weight:900;color:#ffffff;">BOOKING CONFIRMED</h1>
    <p style="margin:0;font-size:15px;color:#C9A84C;">We found you an earlier slot!</p>
  </div>
  <p style="margin:0 0 24px;font-size:15px;color:#aaa;line-height:1.7;">Great news! We found an earlier inspection slot and have automatically rebooked your vehicle. Here are your new booking details:</p>
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#1a2a1a;border:1px solid #2a4a2a;border-radius:8px;margin-bottom:24px;">
  <tr><td style="padding:24px;">
    <div style="font-size:11px;letter-spacing:0.15em;color:#5adb5a;text-transform:uppercase;margin-bottom:16px;">New Booking Details</div>
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr><td style="padding:6px 0;font-size:13px;color:#4a7a4a;width:120px;">Location</td><td style="padding:6px 0;font-size:15px;color:#fff;font-weight:700;">{loc}</td></tr>
      <tr><td style="padding:6px 0;font-size:13px;color:#4a7a4a;">Date</td><td style="padding:6px 0;font-size:15px;color:#5adb5a;font-weight:700;">{ds}</td></tr>
    </table>
  </td></tr></table>
  <div style="padding:16px 20px;background:#1a1a0a;border:1px solid #3a3a00;border-radius:8px;margin-bottom:24px;">
    <p style="margin:0;font-size:13px;color:#C9A84C;line-height:1.6;">Please verify your booking at <a href="https://wovi.com.au" style="color:#C9A84C;">wovi.com.au</a> and contact Queensland Inspection Services on 1300 722 411 if you have any questions.</p>
  </div>
  <p style="margin:0;font-size:13px;color:#555;line-height:1.7;">Thank you for using AVIBM. Our system will continue monitoring in case an even earlier slot becomes available.</p>
</td></tr>
<tr><td style="background:#0f0f0f;border:1px solid #2a2a2a;border-top:1px solid #1e1e1e;border-radius:0 0 12px 12px;padding:24px 40px;text-align:center;">
  <div style="font-size:12px;color:#444;line-height:1.8;">AVIBM — Australian Vehicle Inspection Booking Monitor<br/>
  <a href="https://avibm.vercel.app" style="color:#C9A84C;text-decoration:none;">avibm.vercel.app</a></div>
</td></tr>
</table></td></tr></table>
</body></html>"""
                send_email(
                    f"AVIBM — Booking Confirmed: {loc} on {ds}" + (f" at {booked_time}" if booked_time else ""),
                    f"Great news! We found an earlier slot and rebooked your vehicle.\n\nLocation: {loc}\nDate: {ds}" + (f"\nTime: {booked_time}" if booked_time else "") + f"\n\nPlease verify at wovi.com.au\n— AVIBM",
                    customer["email"],
                    html=booking_html,
                )
            else:
                log_result(customer["id"], vehicle["id"], "QLD", loc, "BOOKING FAILED", ds)
                log(f"  Booking failed for {customer['first_name']} {customer['last_name']}", "WARN")

    # ── SA: requests-based, no browser needed ────────────────────────────────
    if sa_customers:
        log(f"Checking {len(sa_customers)} SA customer(s)...")
        for customer in sa_customers:
            vehicles = [v for v in (customer.get("vehicles") or []) if v.get("active")]
            for vehicle in vehicles:
                cutoff = parse_date(vehicle.get("cutoff_date",""))
                if not cutoff: continue

                label = f"{customer['first_name']} {customer['last_name']}"
                log(f"Checking [🥇 PRIORITY] SA / {label} — cutoff {cutoff.strftime('%d/%m/%Y')}")

                slots = sa_check(customer, vehicle, cutoff)
                log_result(customer["id"], vehicle["id"], "SA", "Regency Park", "Checked", f"{len(slots)} slots found")

                if slots:
                    dt, slot_text = slots[0]
                    log(f"  → Earlier SA slot: {slot_text}")
                    # SA auto-booking would go here (currently sends alert email)
                    send_email(
                        f"SA Inspection — Earlier Slot Available: {slot_text}",
                        f"An earlier inspection slot is available for {label}.\n\n"
                        f"Slot: {slot_text}\n\n"
                        f"Book now at:\nhttps://www.ecom.transport.sa.gov.au/et/rescheduleAVehicleInspectionBooking.do\n\n"
                        f"— AVIBM Automated Booking Monitor",
                        customer["email"]
                    )
                    log_result(customer["id"], vehicle["id"], "SA", "Regency Park", "SLOT FOUND", slot_text)
                else:
                    log(f"  {label}: no earlier SA slots.")

    log("All done.")

    # Update monitor status in Supabase
    try:
        from zoneinfo import ZoneInfo
        adelaide = ZoneInfo("Australia/Adelaide")
        now_str = datetime.now(adelaide).strftime("%d/%m/%Y %I:%M:%S %p ACST")
        status_data = {
            "id": "main",
            "last_run": now_str,
            "active_customers": len(active_customers),
            "qld_count": len(qld_customers),
            "sa_count": len(sa_customers),
            "status": "running",
        }
        # Delete existing row first then insert fresh
        requests.delete(
            f"{SUPABASE_URL}/rest/v1/monitor_status?id=eq.main",
            headers=HEADERS,
        )
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/monitor_status",
            json=status_data,
            headers=HEADERS,
        )
        log(f"Monitor status update: {r.status_code} {r.text[:100]}")
    except Exception as e:
        log(f"Could not update monitor status: {e}", "WARN")


if __name__ == "__main__":
    run()
