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
    # Remove --headless so Angular behaves the same as local debug
    # GitHub Actions uses Xvfb virtual display so this works fine
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--log-level=3")
    opts.add_argument("--display=:99")
    d = webdriver.Chrome(options=opts)
    d.set_page_load_timeout(30)
    return d

# ── 2captcha ──────────────────────────────────────────────────────────────────

def solve_captcha(site_key, page_url, retries=2):
    """Solve reCAPTCHA via 2captcha with retries on failure."""
    for attempt in range(retries):
        try:
            if attempt > 0:
                log(f"  CAPTCHA retry {attempt}/{retries-1}...")
            r = requests.post("http://2captcha.com/in.php", data={
                "key": TWOCAPTCHA_KEY, "method": "userrecaptcha",
                "googlekey": site_key, "pageurl": page_url, "json": 1,
            }, timeout=30).json()
            if r.get("status") != 1:
                log(f"  CAPTCHA submit failed: {r.get('request')}", "WARN")
                continue
            cid = r["request"]
            log(f"  CAPTCHA submitted (ID: {cid})")
            for _ in range(30):  # up to 150 seconds
                time.sleep(5)
                try:
                    resp = requests.get("http://2captcha.com/res.php", params={
                        "key": TWOCAPTCHA_KEY, "action": "get", "id": cid, "json": 1
                    }, timeout=10)
                    try:
                        p = resp.json()
                        if p.get("status") == 1:
                            log("  CAPTCHA solved!")
                            return p["request"]
                        if p.get("request") != "CAPCHA_NOT_READY":
                            log(f"  CAPTCHA error: {p.get('request')}", "WARN")
                            break  # try again
                    except:
                        t = resp.text.strip()
                        if t.startswith("OK|"): return t[3:]
                        if t != "CAPCHA_NOT_READY": break
                except: continue
        except Exception as e:
            log(f"  CAPTCHA exception: {e}", "WARN")
    log("  CAPTCHA failed after all retries", "WARN")
    return None

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

            # Wait for Angular to populate availability data
            time.sleep(3)
            cells = driver.find_elements(By.XPATH, "//div[@ng-click='setDateValue(day)']")
            for item in cells:
                try:
                    data = driver.execute_script("""
                        try {
                            var el = arguments[0];
                            var s = angular.element(el).scope();
                            var av = s && s.day ? s.day.available : null;
                            var val = s && s.day ? s.day.value : null;
                            var inMonth = s && s.day ? s.day.thisMonth : null;
                            var cls = el.className || '';
                            var cssAvail = cls.includes('available') && !cls.includes('unavailable') && !cls.includes('disabled');
                            var cssInMonth = !cls.includes('other-month') && !cls.includes('prev-month') && !cls.includes('next-month');
                            return {av: av, val: val, inMonth: inMonth, cssAvail: cssAvail, cssInMonth: cssInMonth};
                        } catch(e) { return null; }
                    """, item)
                    if not data or not data.get('val'): continue
                    val = data['val']
                    av = data.get('av')
                    inMonth = data.get('inMonth')
                    cssAvail = data.get('cssAvail')
                    cssInMonth = data.get('cssInMonth')
                    # Use Angular if available, fallback to CSS
                    use_av = av if av is not None else cssAvail
                    use_in = inMonth if inMonth is not None else cssInMonth
                    if use_av and use_in:
                        dt = parse_date(val)
                        if dt and dt < cutoff: slots.append((dt, val, location))
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
        selected_time = ""
        time.sleep(3)
        try:
            # Try button-based time slots first (data-ng-repeat)
            time_btns = driver.find_elements(By.XPATH,
                "//button[contains(@data-ng-repeat,'Slot in bookingSlots') or "
                "contains(@data-ng-click,'selectedBookingSlotId')]")
            if not time_btns:
                # Wait a bit more and retry
                time.sleep(3)
                time_btns = driver.find_elements(By.XPATH,
                    "//button[contains(@data-ng-repeat,'Slot in bookingSlots') or "
                    "contains(@data-ng-click,'selectedBookingSlotId')]")
            if time_btns:
                def parse_12hr(btn):
                    try: return datetime.strptime(btn.text.strip().upper().replace(" ",""), "%I:%M%p")
                    except:
                        try: return datetime.strptime(btn.text.strip().upper().replace(" ",""), "%I%p")
                        except: return datetime.max
                earliest_btn = min(time_btns, key=parse_12hr)
                driver.execute_script("arguments[0].click();", earliest_btn)
                selected_time = earliest_btn.text.strip()
                log(f"  [BOOK] Selected time: {selected_time}")
            else:
                # Fallback: try via Angular scope
                result = driver.execute_script("""
                    try {
                        var btns = document.querySelectorAll('button');
                        var timeSlots = [];
                        for (var i=0; i<btns.length; i++) {
                            var ngr = btns[i].getAttribute('data-ng-repeat') || btns[i].getAttribute('ng-repeat') || '';
                            if (ngr.indexOf('Slot') !== -1 || ngr.indexOf('bookingSlot') !== -1) {
                                timeSlots.push(btns[i]);
                            }
                        }
                        if (timeSlots.length > 0) {
                            timeSlots[0].click();
                            return timeSlots[0].textContent.trim();
                        }
                        return null;
                    } catch(e) { return null; }
                """)
                if result:
                    selected_time = result
                    log(f"  [BOOK] Selected time via JS: {selected_time}")
                else:
                    log(f"  [BOOK] No time slots found — proceeding without time selection", "WARN")
        except Exception as e:
            log(f"  [BOOK] Time slot error: {e}", "WARN")

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
        # CRN needs Angular-aware fill
        try:
            crn_el = driver.find_element(By.XPATH, "//input[@name='qldCRN' or @data-ng-model='vm.forms.customerDetails.qldCRN']")
            crn_el.clear()
            for char in str(customer["crn"]):
                crn_el.send_keys(char)
                time.sleep(0.05)
            driver.execute_script(
                "var el=arguments[0];"
                "el.dispatchEvent(new Event('input',{bubbles:true}));"
                "el.dispatchEvent(new Event('change',{bubbles:true}));", crn_el)
        except Exception:
            fill(driver, customer["crn"], "qldCRN","crn","CRN","licenceNumber","crnLicence")
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

        # Click paperwork button (id="Paperwork", triggers checkDuplicateBooking)
        try:
            paperwork_btn = driver.find_element(By.XPATH,
                "//button[@id='Paperwork' or @name='allPaperwork' or "
                "contains(@data-ng-click,'checkDuplicateBooking')]")
            driver.execute_script("arguments[0].click();", paperwork_btn)
            log(f"  [BOOK] Clicked paperwork button")
            time.sleep(1)
        except Exception as e:
            log(f"  [BOOK] Paperwork button not found: {e}", "WARN")

        # CAPTCHA
        log(f"  [BOOK] Solving CAPTCHA...")
        token = solve_captcha(QLD_CAPTCHA_KEY, QLD_BOOKING_URL)
        if not token: return False

        driver.execute_script("""
            var token = arguments[0];
            var el1 = document.getElementById('g-recaptcha-response');
            if (el1) { el1.innerHTML = token; el1.value = token; }
            var els = document.querySelectorAll('[name="g-recaptcha-response"]');
            els.forEach(function(el) { el.innerHTML = token; el.value = token; });
            try {
                var cfg = window.___grecaptcha_cfg;
                if (cfg && cfg.clients) {
                    Object.keys(cfg.clients).forEach(function(key) {
                        var client = cfg.clients[key];
                        Object.keys(client).forEach(function(k) {
                            var obj = client[k];
                            if (obj && obj.callback) { try { obj.callback(token); } catch(e) {} }
                        });
                    });
                }
            } catch(e) {}
        """, token)
        time.sleep(2)

        log(f"  [BOOK] Clicking Submit My Booking Request...")
        click_next(driver, wait)
        time.sleep(4)

        log(f"  [BOOK] Page after submit: {driver.title}")

        # Handle "Would you like to update your booking?" popup
        clicked_popup = False
        log(f"  Waiting for Update Booking popup (up to 20s)...")
        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.XPATH,
                    "//*[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'update booking')]"
                ))
            )
            log("  Popup detected — triggering Update Booking via Angular")
            time.sleep(2)

            result = driver.execute_script("""
                try {
                    var btn = document.querySelector('[ng-click="vm.dialog.moveBooking()"]');
                    if (btn) { angular.element(btn).triggerHandler('click'); return 'triggered'; }
                    return 'not found';
                } catch(e) { return 'error: ' + e.toString(); }
            """)
            log(f"  triggerHandler result: {result}")
            if result == 'triggered':
                log("  ✓ Update Booking triggered via Angular")
                clicked_popup = True
                time.sleep(5)
            else:
                log(f"  triggerHandler failed: {result}", "WARN")

        except TimeoutException:
            log(f"  Popup not found after 20s — page title: {driver.title}", "WARN")
            log(f"  Page URL: {driver.current_url}", "WARN")
            # Log snippet to understand what page we're on
            log(f"  Page source start: {driver.page_source[:200]}", "WARN")

        # Wait for confirmation page
        time.sleep(3)
        page_source = driver.page_source
        page_lower  = page_source.lower()
        log(f"  Page after final step: {driver.title}")
        log(f"  URL: {driver.current_url}")

        # Only confirm if Update Booking was actually clicked
        confirmed = clicked_popup and any(w in page_lower for w in [
            "booking has been secured", "your booking reference",
            "inspection has been booked", "confirmed", "success", "thank you"
        ])

        if confirmed:
            log(f"  ✅ Booking confirmed!")
        else:
            log(f"  ❌ Booking NOT confirmed — Update Booking popup not clicked or confirmation not found", "WARN")

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

    # Safety reset — clear any stuck booking_in_progress flags at start of each run
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/vehicles?booking_in_progress=eq.true",
            headers=HEADERS, json={"booking_in_progress": False}
        )
        log("Reset any stuck booking_in_progress flags")
    except Exception as e:
        log(f"Could not reset booking_in_progress flags: {e}", "WARN")

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

                    # Skip if booking already in progress from a previous run
                    if vehicle.get("booking_in_progress"):
                        log(f"  Skipping {label} — booking already in progress")
                        continue

                    if slots:
                        priority_locs = vehicle.get("priority_locations") or []
                        chosen = None

                        if priority_locs:
                            # Find earliest date across ALL locations
                            earliest_dt = slots[0][0]
                            # Check if any priority location has a slot on that earliest date
                            priority_slots = [s for s in slots if s[2] in priority_locs and s[0] == earliest_dt]
                            if priority_slots:
                                chosen = priority_slots[0]
                                log(f"  → Priority slot: {chosen[1]} at {chosen[2]}")
                            else:
                                # Priority locations don't have earliest date — book earliest anywhere
                                chosen = slots[0]
                                log(f"  → No priority slot available — using earliest: {chosen[1]} at {chosen[2]}")
                        else:
                            # No priority set — just book earliest slot
                            chosen = slots[0]

                        dt, ds, loc = chosen
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
            # Mark as in progress so concurrent runs don't try the same booking
            db_patch("vehicles", "id", vehicle["id"], {"booking_in_progress": True})
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
                    "booking_in_progress": False,
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
                db_patch("vehicles", "id", vehicle["id"], {"booking_in_progress": False})

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
