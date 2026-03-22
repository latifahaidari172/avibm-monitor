#!/usr/bin/env python3
"""
AVIBM Master Monitor — Webhook server mode.
Railway runs this as a persistent web server.
A GET/POST to /run triggers the monitor.
cron-job.org pings /run every 1 minute.
"""

import csv, json, os, re, smtplib, sys, time, threading
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# ── Constants ─────────────────────────────────────────────────────────────────

SUPABASE_URL     = os.environ["SUPABASE_URL"]
SUPABASE_KEY     = os.environ["SUPABASE_SERVICE_KEY"]
TWOCAPTCHA_KEY   = os.environ["TWOCAPTCHA_API_KEY"]
GMAIL_ADDR       = os.environ["GMAIL_ADDRESS"]
GMAIL_PASS       = os.environ["GMAIL_APP_PASSWORD"]

QLD_BOOKING_URL  = "https://wovi.com.au/bookings/"
SA_BOOKING_URL   = "https://www.ecom.transport.sa.gov.au/et/rescheduleAVehicleInspectionBooking.do"
SA_HOME_URL      = "https://www.ecom.transport.sa.gov.au/et/welcome.jsp"

QLD_LOCATIONS    = ["Brisbane", "Bundaberg", "Burleigh Heads", "Cairns", "Mackay", "Narangba", "Rockhampton City", "Toowoomba", "Townsville", "Yatala"]
QLD_CAPTCHA_KEY  = "6LfAG_0pAAAAAFQzCmk7OQ4roYKXfgYFAPwsVo-5"

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

# Prevent overlapping runs if a previous one is still going
_run_lock = threading.Lock()

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
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--log-level=3")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    d = webdriver.Chrome(options=opts)
    # Override navigator.webdriver so reCAPTCHA can't detect automation
    d.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })
    d.set_page_load_timeout(30)
    return d

# ── 2captcha ──────────────────────────────────────────────────────────────────

def solve_captcha(site_key, page_url, retries=3):
    for attempt in range(retries):
        try:
            if attempt > 0:
                log(f"  CAPTCHA retry {attempt}/{retries-1}...")
                time.sleep(5)
            r = requests.post("http://2captcha.com/in.php", data={
                "key": TWOCAPTCHA_KEY, "method": "userrecaptcha",
                "googlekey": site_key, "pageurl": page_url, "json": 1,
            }, timeout=30)
            if not r.text.strip(): continue
            r_json = r.json()
            if r_json.get("status") != 1:
                log(f"  CAPTCHA submit failed: {r_json.get('request')}", "WARN")
                continue
            cid = r_json["request"]
            log(f"  CAPTCHA submitted (ID: {cid})")
            for _ in range(30):
                time.sleep(5)
                try:
                    resp = requests.get("http://2captcha.com/res.php", params={
                        "key": TWOCAPTCHA_KEY, "action": "get", "id": cid, "json": 1
                    }, timeout=10)
                    if not resp.text.strip(): continue
                    try:
                        p = resp.json()
                        if p.get("status") == 1:
                            log("  CAPTCHA solved!")
                            return p["request"]
                        req = p.get("request","")
                        if req not in ("CAPCHA_NOT_READY", ""): break
                    except:
                        t = resp.text.strip()
                        if t.startswith("OK|"): return t[3:]
                        if t and t != "CAPCHA_NOT_READY": break
                except Exception as e:
                    log(f"  CAPTCHA poll error: {e}", "WARN")
                    continue
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
    phrases = ["submit my booking request","submit booking request","submit my booking","next"]
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
        log(f"  [BOOK] Selecting location: {location}")
        sel = wait.until(EC.presence_of_element_located((By.XPATH,
            "//select[.//option[contains(text(),'Brisbane')]]")))
        for opt in Select(sel).options:
            if location.lower() in opt.text.lower():
                Select(sel).select_by_visible_text(opt.text); break
        time.sleep(3)
        log(f"  [BOOK] Waiting for calendar...")
        try:
            WebDriverWait(driver, 8).until(lambda d: len(
                d.find_elements(By.XPATH, "//div[@ng-click='setDateValue(day)']")) > 0)
        except TimeoutException:
            log(f"  [BOOK] Calendar did not load", "WARN")
            return (False, "")
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
            time_btns = driver.find_elements(By.XPATH,
                "//button[contains(@data-ng-repeat,'Slot in bookingSlots') or "
                "contains(@data-ng-click,'selectedBookingSlotId')]")
            if not time_btns:
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
                        if (timeSlots.length > 0) { timeSlots[0].click(); return timeSlots[0].textContent.trim(); }
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
        log(f"  [BOOK] Filling vehicle details...")
        vtype = vehicle.get("vehicle_type","Car")
        try:
            driver.execute_script("arguments[0].click();", driver.find_element(By.XPATH, f"//label[contains(normalize-space(.),'{vtype}')]"))
        except: pass
        fill(driver, vehicle["vin"],            "vin","chassis","VIN","vinChassis")
        fill(driver, vehicle["make"],           "make","vehicleMake")
        fill(driver, vehicle["model"],          "model","vehicleModel")
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
        log(f"  [BOOK] Filling customer details...")
        try:
            crn_el = driver.find_element(By.XPATH, "//input[@name='qldCRN']")
            driver.execute_script("""
                var el = arguments[0]; var val = arguments[1];
                el.focus();
                var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(el, val);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur', {bubbles: true}));
                try {
                    var scope = angular.element(el).scope();
                    if (scope && scope.vm && scope.vm.forms && scope.vm.forms.customerDetails) {
                        scope.vm.forms.customerDetails.qldCRN = val; scope.$apply();
                    }
                } catch(e) {}
            """, crn_el, str(customer["crn"]))
            time.sleep(0.5)
        except Exception as e:
            log(f"  [BOOK] CRN error: {e}", "WARN")
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
        try:
            paperwork_btn = driver.find_element(By.XPATH,
                "//button[@id='Paperwork' or @name='allPaperwork' or "
                "contains(@data-ng-click,'checkDuplicateBooking')]")
            driver.execute_script("arguments[0].click();", paperwork_btn)
            log(f"  [BOOK] Clicked paperwork button")
            time.sleep(2)
        except Exception as e:
            log(f"  [BOOK] Paperwork button not found: {e}", "WARN")
        log(f"  [BOOK] Waiting for reCAPTCHA to render...")
        for _ in range(10):
            has_captcha = driver.execute_script(
                "return document.querySelector('.g-recaptcha, #g-recaptcha-response') !== null || "
                "document.querySelector('iframe[src*=recaptcha]') !== null"
            )
            if has_captcha:
                log(f"  [BOOK] reCAPTCHA widget detected"); break
            time.sleep(1)
        else:
            log(f"  [BOOK] reCAPTCHA widget not found — proceeding anyway", "WARN")
        log(f"  [BOOK] Solving CAPTCHA...")
        token = solve_captcha(QLD_CAPTCHA_KEY, QLD_BOOKING_URL)
        if not token: return (False, "")
        # Wait for iframe to fully load before injecting
        time.sleep(3)
        driver.execute_script("""
            var token = arguments[0];
            var el1 = document.getElementById('g-recaptcha-response');
            if (el1) { el1.innerHTML = token; el1.value = token; el1.style.display = 'block'; }
            document.querySelectorAll('[name="g-recaptcha-response"]').forEach(function(el) {
                el.innerHTML = token; el.value = token;
            });
            try {
                var cfg = window.___grecaptcha_cfg;
                if (cfg && cfg.clients) {
                    Object.keys(cfg.clients).forEach(function(key) {
                        var client = cfg.clients[key];
                        Object.keys(client).forEach(function(k) {
                            var obj = client[k];
                            if (obj && typeof obj.callback === 'function') {
                                try { obj.callback(token); } catch(e) {}
                            }
                            if (obj && obj.l && typeof obj.l === 'function') {
                                try { obj.l(token); } catch(e) {}
                            }
                        });
                    });
                }
            } catch(e) {}
            try { angular.element(document.body).scope().$apply(); } catch(e) {}
        """, token)
        time.sleep(1)
        # Re-inject right before submit
        driver.execute_script("""
            var token=arguments[0];
            var el1=document.getElementById('g-recaptcha-response');
            if(el1){el1.innerHTML=token;el1.value=token;}
            document.querySelectorAll('[name="g-recaptcha-response"]').forEach(function(el){el.innerHTML=token;el.value=token;});
            try {
                var cfg=window.___grecaptcha_cfg;
                if(cfg&&cfg.clients){
                    Object.keys(cfg.clients).forEach(function(key){
                        var client=cfg.clients[key];
                        Object.keys(client).forEach(function(k){
                            var obj=client[k];
                            if(obj&&typeof obj.callback==='function'){try{obj.callback(token);}catch(e){}}
                            if(obj&&obj.l&&typeof obj.l==='function'){try{obj.l(token);}catch(e){}}
                        });
                    });
                }
            } catch(e){}
            try{angular.element(document.body).scope().$apply();}catch(e){}
        """, token)
        time.sleep(1)
        log(f"  [BOOK] Submitting booking...")
        click_next(driver, wait)
        time.sleep(4)
        log(f"  [BOOK] Page after submit: {driver.title}")
        clicked_popup = False
        log(f"  Waiting for Update Booking popup (up to 30s)...")

        # Dump full page source so we can find the real confirmation text
        time.sleep(3)
        full_source = driver.page_source
        full_lower = full_source.lower()
        log(f"  [DEBUG] Page title after submit: {driver.title}")
        log(f"  [DEBUG] Page URL after submit: {driver.current_url}")
        log(f"  [DEBUG] Full page source ({len(full_source)} chars) — printing in chunks:")
        for i in range(0, min(len(full_source), 6000), 600):
            log(f"  [DEBUG] [{i}] {full_source[i:i+600]}")

        # Strategy 1 — check if booking already confirmed without a popup
        already_confirmed = any(w in full_lower for w in [
            "booking has been secured", "your booking reference",
            "inspection has been booked", "booking confirmed",
            "successfully booked", "thank you for your booking"
        ])
        if already_confirmed:
            log("  ✅ Booking confirmed directly (no popup needed)!")
            return (True, selected_time)

        # Strategy 2 — wait for popup with broader search terms
        popup_found = False
        for _ in range(30):  # poll every 1s for 30s
            try:
                src = driver.page_source.lower()
                if any(w in src for w in ["update booking", "move booking", "earlier booking",
                                           "would you like", "movebooking", "updatbooking"]):
                    popup_found = True
                    break
                # Also check if confirmation already appeared while waiting
                if any(w in src for w in ["booking has been secured", "your booking reference",
                                           "successfully booked", "thank you for your booking"]):
                    log("  ✅ Booking confirmed while waiting for popup!")
                    return (True, selected_time)
            except: pass
            time.sleep(1)

        if popup_found:
            log("  Popup detected — trying all click strategies...")
            time.sleep(1)

            # Try multiple ways to click the confirm button
            result = driver.execute_script("""
                try {
                    // Strategy A — ng-click attribute
                    var btn = document.querySelector('[ng-click="vm.dialog.moveBooking()"]');
                    if (btn) { angular.element(btn).triggerHandler('click'); return 'A:triggered'; }

                    // Strategy B — any button containing 'update' or 'move' or 'yes'
                    var btns = document.querySelectorAll('button, a, input[type=button], input[type=submit]');
                    for (var i = 0; i < btns.length; i++) {
                        var t = (btns[i].textContent || btns[i].value || '').toLowerCase();
                        if (t.includes('update') || t.includes('move') || t.includes('yes') || t.includes('confirm')) {
                            btns[i].click();
                            return 'B:clicked:' + t.trim();
                        }
                    }

                    // Strategy C — Angular scope method directly
                    try {
                        var scope = angular.element(document.body).scope();
                        if (scope && scope.vm && scope.vm.dialog && scope.vm.dialog.moveBooking) {
                            scope.vm.dialog.moveBooking();
                            scope.$apply();
                            return 'C:scope-called';
                        }
                    } catch(e) {}

                    return 'no-button-found';
                } catch(e) { return 'error:' + e.toString(); }
            """)
            log(f"  Popup click result: {result}")
            if result and result not in ['no-button-found'] and not result.startswith('error'):
                clicked_popup = True
                time.sleep(6)
        else:
            log(f"  Popup not found after 30s — checking if booking went through anyway", "WARN")

        # Final confirmation check
        time.sleep(3)
        page_lower = driver.page_source.lower()
        log(f"  [DEBUG] Final page title: {driver.title}")
        log(f"  [DEBUG] Final URL: {driver.current_url}")

        confirmed = (clicked_popup or not popup_found) and any(w in page_lower for w in [
            "booking has been secured", "your booking reference",
            "inspection has been booked", "booking confirmed",
            "successfully booked", "thank you for your booking",
            "confirmed", "success", "thank you"
        ])
        if confirmed:
            log(f"  ✅ Booking confirmed!")
        else:
            log(f"  ❌ Booking NOT confirmed — page title: {driver.title}", "WARN")
            log(f"  [DEBUG] Page source sample: {page_lower[:300]}", "WARN")
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
            "clientNumber": customer["licence_number"],
            "clientSurnameOrgName": customer["last_name"],
            "clientDOB": dob,
        }, timeout=15)
        r2 = sess.post(SA_BOOKING_URL, data={"preferredDate": preferred}, timeout=15)
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

# ── Main run logic ────────────────────────────────────────────────────────────

def run():
    # Prevent two runs overlapping
    if not _run_lock.acquire(blocking=False):
        log("Previous run still in progress — skipping this trigger")
        return "skipped"

    try:
        log("=" * 60)
        log("AVIBM Master Monitor — checking all active customers")
        log("=" * 60)

        customers = db_get("customers", "active=eq.true&select=*,vehicles(*)")
        if isinstance(customers, dict) and customers.get("error"):
            log(f"Failed to fetch customers: {customers}", "ERROR")
            return "error"
        if not isinstance(customers, list):
            log(f"Unexpected Supabase response: {customers}", "ERROR")
            return "error"

        active_customers = [c for c in customers if isinstance(c, dict) and c.get("active")]
        log(f"Found {len(active_customers)} active customer(s)")

        if not active_customers:
            log("No active customers — nothing to do.")
            return "ok"

        qld_customers = [c for c in active_customers if c.get("state") == "QLD"]
        sa_customers  = [c for c in active_customers if c.get("state") == "SA"]

        TIER_ORDER = {"priority": 0, "standard": 1, "basic": 2}
        TIER_DELAY = {"priority": 0, "standard": 30, "basic": 60}
        TIER_LABEL = {"priority": "🥇 PRIORITY", "standard": "🥈 STANDARD", "basic": "🥉 BASIC"}

        if qld_customers:
            qld_customers.sort(key=lambda c: TIER_ORDER.get(c.get("tier","standard"), 1))
            log(f"Checking {len(qld_customers)} QLD customer(s)...")
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
                        now_naive = datetime.now()
                        if (now_naive - cutoff).total_seconds() > 86400:
                            log(f"  Auto-deactivating vehicle — cutoff passed by 24h")
                            db_patch("vehicles", "id", vehicle["id"], {"active": False})
                            continue
                        label = f"{customer['first_name']} {customer['last_name']} / {vehicle.get('label', vehicle.get('make','?'))}"
                        log(f"Checking [{TIER_LABEL[tier]}] {label} — cutoff {cutoff.strftime('%d/%m/%Y')}")
                        vehicle_locations = vehicle.get("locations") or QLD_LOCATIONS
                        slots = qld_find_slots(driver, cutoff, label, vehicle_locations)
                        log_result(customer["id"], vehicle["id"], "QLD", "All", "Checked", f"{len(slots)} slots found")
                        if vehicle.get("booking_in_progress"):
                            started_at = vehicle.get("booking_started_at")
                            if started_at:
                                try:
                                    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                                    age_mins = (datetime.now(timezone.utc) - started).total_seconds() / 60
                                    if age_mins < 5:
                                        log(f"  Skipping — booking in progress ({age_mins:.1f} min ago)")
                                        continue
                                    else:
                                        db_patch("vehicles", "id", vehicle["id"], {"booking_in_progress": False, "booking_started_at": None})
                                except Exception:
                                    continue
                            else:
                                continue
                        if slots:
                            priority_locs = vehicle.get("priority_locations") or []
                            if priority_locs:
                                earliest_dt = slots[0][0]
                                priority_slots = [s for s in slots if s[2] in priority_locs and s[0] == earliest_dt]
                                chosen = priority_slots[0] if priority_slots else slots[0]
                            else:
                                chosen = slots[0]
                            dt, ds, loc = chosen
                            log(f"  → Earlier slot: {ds} at {loc}")
                            booking_jobs.append((customer, vehicle, dt, ds, loc, tier))
            finally:
                try: driver.quit()
                except: pass

            def book_vehicle(customer, vehicle, dt, ds, loc, tier, delay=0):
                if delay > 0:
                    log(f"[{TIER_LABEL[tier]}] Waiting {delay}s before booking...")
                    time.sleep(delay)
                log(f"[{TIER_LABEL[tier]}] Booking {loc} on {ds} for {customer['first_name']} {customer['last_name']}...")
                claim_resp = requests.patch(
                    f"{SUPABASE_URL}/rest/v1/vehicles?id=eq.{vehicle['id']}&booking_in_progress=eq.false",
                    headers={**HEADERS, "Prefer": "return=representation"},
                    json={"booking_in_progress": True, "booking_started_at": datetime.now(timezone.utc).isoformat()}
                )
                claimed = claim_resp.json()
                if not claimed or len(claimed) == 0:
                    log(f"  Could not claim vehicle — skipping")
                    return
                result = qld_book_slot(loc, ds, customer, vehicle)
                confirmed, booked_time = result if isinstance(result, tuple) else (result, "")
                if confirmed:
                    old_cutoff = vehicle.get("cutoff_date", "")
                    db_patch("vehicles", "id", vehicle["id"], {
                        "booked_date": ds, "booked_time": booked_time,
                        "booked_location": loc, "previous_cutoff": old_cutoff,
                        "cutoff_date": ds, "booking_in_progress": False,
                    })
                    log_result(customer["id"], vehicle["id"], "QLD", loc, "BOOKED", ds)
                    booking_html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"/></head>
<body style="margin:0;padding:0;background:#0a0a0a;font-family:Helvetica,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#0a0a0a;padding:40px 20px;">
<tr><td align="center"><table width="600" cellpadding="0" cellspacing="0">
<tr><td style="background:#111;border:1px solid #2a2a2a;border-radius:12px 12px 0 0;padding:32px 40px;text-align:center;">
  <div style="font-size:32px;font-weight:900;letter-spacing:0.2em;color:#C9A84C;">AVIBM</div>
  <div style="font-size:11px;letter-spacing:0.25em;color:#666;margin-top:4px;text-transform:uppercase;">Australian Vehicle Inspection Booking Monitor</div>
</td></tr>
<tr><td style="background:#141414;border-left:1px solid #2a2a2a;border-right:1px solid #2a2a2a;padding:40px;">
  <h1 style="margin:0 0 8px;font-size:28px;color:#fff;">BOOKING CONFIRMED</h1>
  <p style="color:#C9A84C;">We found you an earlier slot!</p>
  <p style="color:#aaa;">Location: <strong style="color:#fff;">{loc}</strong></p>
  <p style="color:#aaa;">Date: <strong style="color:#5adb5a;">{ds}</strong></p>
  <p style="color:#aaa;">Please verify at <a href="https://wovi.com.au" style="color:#C9A84C;">wovi.com.au</a></p>
</td></tr>
<tr><td style="background:#0f0f0f;border:1px solid #2a2a2a;border-radius:0 0 12px 12px;padding:24px 40px;text-align:center;">
  <div style="font-size:12px;color:#444;">AVIBM — <a href="https://avibm.vercel.app" style="color:#C9A84C;">avibm.vercel.app</a></div>
</td></tr></table></td></tr></table></body></html>"""
                    send_email(
                        f"AVIBM — Booking Confirmed: {loc} on {ds}" + (f" at {booked_time}" if booked_time else ""),
                        f"Great news! We found an earlier slot and rebooked your vehicle.\n\nLocation: {loc}\nDate: {ds}" + (f"\nTime: {booked_time}" if booked_time else "") + f"\n\nPlease verify at wovi.com.au\n— AVIBM",
                        customer["email"], html=booking_html,
                    )
                else:
                    log_result(customer["id"], vehicle["id"], "QLD", loc, "BOOKING FAILED", ds)
                    db_patch("vehicles", "id", vehicle["id"], {"booking_in_progress": False})

            threads = []
            for job in booking_jobs:
                customer, vehicle, dt, ds, loc, tier = job
                delay = TIER_DELAY.get(tier, 0) if tier != 'priority' else 0
                t = threading.Thread(target=book_vehicle, args=(customer, vehicle, dt, ds, loc, tier, delay), daemon=True)
                threads.append(t)
            for t in threads: t.start()
            for t in threads: t.join()
            log("All booking threads completed")

        if sa_customers:
            log(f"Checking {len(sa_customers)} SA customer(s)...")
            for customer in sa_customers:
                vehicles = [v for v in (customer.get("vehicles") or []) if v.get("active")]
                for vehicle in vehicles:
                    cutoff = parse_date(vehicle.get("cutoff_date",""))
                    if not cutoff: continue
                    label = f"{customer['first_name']} {customer['last_name']}"
                    log(f"Checking SA / {label} — cutoff {cutoff.strftime('%d/%m/%Y')}")
                    slots = sa_check(customer, vehicle, cutoff)
                    log_result(customer["id"], vehicle["id"], "SA", "Regency Park", "Checked", f"{len(slots)} slots found")
                    if slots:
                        dt, slot_text = slots[0]
                        log(f"  → Earlier SA slot: {slot_text}")
                        send_email(
                            f"SA Inspection — Earlier Slot Available: {slot_text}",
                            f"An earlier inspection slot is available for {label}.\n\nSlot: {slot_text}\n\nBook now at:\nhttps://www.ecom.transport.sa.gov.au/et/rescheduleAVehicleInspectionBooking.do\n\n— AVIBM",
                            customer["email"]
                        )
                        log_result(customer["id"], vehicle["id"], "SA", "Regency Park", "SLOT FOUND", slot_text)
                    else:
                        log(f"  {label}: no earlier SA slots.")

        log("All done.")

        try:
            from zoneinfo import ZoneInfo
            adelaide = ZoneInfo("Australia/Adelaide")
            status_data = {
                "id": "main",
                "last_run": datetime.now(adelaide).strftime("%d/%m/%Y %I:%M:%S %p ACST"),
                "active_customers": len(active_customers),
                "qld_count": len(qld_customers),
                "sa_count": len(sa_customers),
                "status": "running",
            }
            requests.delete(f"{SUPABASE_URL}/rest/v1/monitor_status?id=eq.main", headers=HEADERS)
            requests.post(f"{SUPABASE_URL}/rest/v1/monitor_status", json=status_data, headers=HEADERS)
        except Exception as e:
            log(f"Could not update monitor status: {e}", "WARN")

        return "ok"

    finally:
        _run_lock.release()

# ── Webhook server ────────────────────────────────────────────────────────────

class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default HTTP logs — our own log() handles output

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, "AVIBM is alive")
        elif self.path == "/run":
            self._respond(200, "Trigger received — running monitor in background")
            threading.Thread(target=run, daemon=True).start()
        else:
            self._respond(404, "Not found")

    def do_POST(self):
        if self.path == "/run":
            self._respond(200, "Trigger received — running monitor in background")
            threading.Thread(target=run, daemon=True).start()
        else:
            self._respond(404, "Not found")

    def _respond(self, code, message):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(message.encode())


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    log(f"AVIBM webhook server starting on port {port}")
    log(f"Trigger URL: http://0.0.0.0:{port}/run")
    log(f"Health check: http://0.0.0.0:{port}/health")
    server = HTTPServer(("0.0.0.0", port), WebhookHandler)
    server.serve_forever()
