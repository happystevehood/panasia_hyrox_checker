import os
import json
import time
import smtplib
import sys
import re
from urllib.parse import urlparse, urlunparse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, StaleElementReferenceException
from PIL import Image, ImageDraw, ImageFont
from datetime import datetime
import pytz

# Configuration file names
TICKET_DETAILS_CONFIG = "config.json"
ON_SALE_CONFIG = "onsale_config.json"
MATRIX_STATE_FILE = "matrix_last_state.json"
MATRIX_CHANGES_FILE = "matrix_intermediate_changes.json" # NEW: Stores changes between matrix runs
MATRIX_OUTPUT_FILE = "availability_matrix.png"

# Global Matrix Categories (Used for logging changes mapping)
DISPLAY_CATEGORIES = [
    "HYROX PRO WOMEN", "HYROX PRO MEN", "HYROX WOMEN", "HYROX MEN",
    "HYROX PRO DOUBLES WOMEN", "HYROX PRO DOUBLES MEN",
    "HYROX DOUBLES WOMEN", "HYROX DOUBLES MIXED", "HYROX DOUBLES MEN",
    "HYROX WOMENS RELAY", "HYROX MENS RELAY", "HYROX MIXED RELAY"
]
# Sorted by length to ensure "HYROX PRO MEN" matches before "HYROX MEN"
MATCHING_CATEGORIES = sorted(DISPLAY_CATEGORIES, key=len, reverse=True)

# --- HELPER FUNCTIONS ---
def setup_driver(headless=True):
    print(f"DEBUG: Headless mode is {headless}")
    chrome_options = Options()
    
    if headless:
        chrome_options.add_argument("--headless=new")
        
        # 1. Mask the bot identity with a real User-Agent
        chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
        # 2. Set a large window size so the "Buy Tickets" buttons aren't hidden by mobile layouts
        chrome_options.add_argument("--window-size=1920,1080")
        
    else:
        chrome_options.add_argument("--start-maximized")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)

    # --- ADD THESE TWO LINES HERE ---
    chrome_options.add_argument("--disable-webrtc")
    chrome_options.add_experimental_option('excludeSwitches', ['enable-logging'])

    # --- STABILITY FLAGS FOR GITHUB ACTIONS ---
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage") # Overcomes limited resource problems
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--proxy-server='direct://'")
    chrome_options.add_argument("--proxy-bypass-list=*")
    
    # Disabling images can sometimes break layout-heavy ticket buttons.
    chrome_options.add_argument("--blink-settings=imagesEnabled=false") # Speed up by not loading images
    
    service = Service()
    driver = webdriver.Chrome(service=service, options=chrome_options)
    
    # --- TIMEOUTS ---
    # This prevents the "Read timed out" by throwing a Selenium error 
    # after 60s instead of letting the socket hang.
    driver.set_page_load_timeout(60) 
    driver.set_script_timeout(60)
    
    return driver

def send_email(subject, html_body, recipient_email, mail_username, mail_password, attachment_path=None):
    if not recipient_email or not mail_username: return 
    
    # 1. Convert the string "email1, email2" into a Python list
    recipient_list = [email.strip() for email in recipient_email.split(',')]

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = f"Hyrox Monitor Bot <{mail_username}>"
    
    # 2. Set a generic 'To' header so recipients see a clean "To" field
    msg['To'] = "Hyrox Subscriber" 

    msg.attach(MIMEText(html_body, 'html'))
    if attachment_path and os.path.exists(attachment_path):
        with open(attachment_path, 'rb') as f:
            img = MIMEImage(f.read())
            img.add_header('Content-ID', '<matrix_image>')
             # ADD THIS LINE:
            img.add_header('Content-Disposition', 'attachment', filename=os.path.basename(attachment_path))
            msg.attach(img)
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(mail_username, mail_password)
            # 3. Explicitly pass the recipient_list to the 'to_addrs' argument
            # This handles the BCC logic at the protocol level
            server.send_message(msg, from_addr=mail_username, to_addrs=recipient_list)
    except Exception as e: print(f"Error sending email: {e}")

def normalize_text(text):
    if not isinstance(text, str): return text
    text = re.sub(r'[^\x00-\x7F]+', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def _normalize_for_matrix(text):
    return text.upper().replace("'", "")

def set_github_output(name, value):
    github_output_path = os.getenv('GITHUB_OUTPUT')
    if github_output_path:
        with open(github_output_path, 'a') as f:
            f.write(f'{name}={value}\n')

def clean_checkout_url(url):
    if not url: return None
    try:
        parsed = urlparse(url)
        clean = urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))
        return clean
    except:
        return url

def reset_driver_state(driver):
    """Ensures the driver is focused on a single, clean window before the next site."""
    try:
        # 1. Close all windows/tabs except the first one
        while len(driver.window_handles) > 1:
            driver.switch_to.window(driver.window_handles[-1])
            driver.close()
        # 2. Switch back to the main window
        driver.switch_to.window(driver.window_handles[0])
        # 3. Exit any iframes or shadow DOM contexts
        driver.switch_to.default_content()
    except:
        pass

# --- NEW: INTERMEDIATE CHANGE LOGGING ---
def log_intermediate_changes(site_name, changed_tickets):
    """
    Records categories that changed into a persistent file.
    This allows the Matrix to show an 'X' even if status flipped back.
    """
    if not changed_tickets: return

    # Load existing log
    try:
        with open(MATRIX_CHANGES_FILE, 'r') as f:
            changes_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        changes_data = {}

    if site_name not in changes_data:
        changes_data[site_name] = []

    # Map ticket names (e.g. "HYROX MEN | FRIDAY") to Matrix Categories (e.g. "HYROX MEN")
    categories_to_flag = set()
    for ticket_name in changed_tickets:
        norm_name = _normalize_for_matrix(ticket_name)
        for cat in MATCHING_CATEGORIES:
            if _normalize_for_matrix(cat) in norm_name:
                categories_to_flag.add(cat)
                break
    
    # Update file
    updated = False
    for cat in categories_to_flag:
        if cat not in changes_data[site_name]:
            changes_data[site_name].append(cat)
            updated = True

    if updated:
        try:
            with open(MATRIX_CHANGES_FILE, 'w') as f:
                json.dump(changes_data, f, indent=2)
            print(f"  > Logged intermediate changes for matrix: {list(categories_to_flag)}")
        except Exception as e:
            print(f"  ! Failed to save intermediate changes: {e}")

# --- HTML GENERATOR ---
def generate_diff_html(site_config, prev_status, curr_status):
    url = site_config['url']
    name = site_config['name']
    
    prev_list = prev_status.get("General", {}).get("details", [])
    curr_list = curr_status.get("General", {}).get("details", [])
    
    prev_map = {t['name']: t['status'] for t in prev_list}
    curr_map = {t['name']: t['status'] for t in curr_list}
    
    all_ticket_names = sorted(list(set(prev_map.keys()) | set(curr_map.keys())))
    
    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif;">
        <h3>Status Update for <a href="{url}" target="_blank" rel="nofollow noopener noreferrer">{name}</a></h3>
        <p>The following tickets have been detected:</p>
        <table border="1" cellpadding="8" style="border-collapse: collapse; width: 100%; border: 1px solid #ddd;">
            <tr style="background-color: #f2f2f2; text-align: left;">
                <th>Ticket Name</th>
                <th>Current Status</th>
                <th>Previous Status</th>
            </tr>
    """
    
    changes_found = False
    changed_ticket_names = [] # List to track specifically what changed for logging
    
    for t_name in all_ticket_names:
        p_status = prev_map.get(t_name, "N/A")
        
        if t_name in curr_map:
            c_status = curr_map[t_name]
        else:
            c_status = "Sold out"
            
        row_style = ""
        status_style = ""
        
        if c_status != p_status:
            changes_found = True
            changed_ticket_names.append(t_name)
            
            if c_status.lower() == "available":
                row_style = "background-color: #d4edda;"
                status_style = "color: #155724; font-weight: bold;"
            elif c_status.lower() == "sold out":
                row_style = "background-color: #f8d7da;"
                status_style = "color: #721c24; font-weight: bold;"
            else:
                row_style = "background-color: #fff3cd;"
        else:
            if c_status.lower() == "sold out":
                 status_style = "color: #999;"
            
        html += f"""
        <tr style="{row_style}">
            <td>{t_name}</td>
            <td style="{status_style}">{c_status}</td>
            <td style="color: #666;">{p_status}</td>
        </tr>
        """
            
    html += """
        </table>
        <br>
        <p><small>Timestamp: {}</small></p>
    </body>
    </html>
    """.format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    
    # Return tuple: (HTML, List of changed names)
    return (html, changed_ticket_names) if changes_found else (None, [])

# --- COOKIE HANDLING ---
def handle_cookies(driver):
    end_time = time.time() + 2
    while time.time() < end_time:
        try:
            host = driver.find_elements(By.ID, "usercentrics-root")
            if host:
                shadow_root = driver.execute_script("return arguments[0].shadowRoot", host[0])
                if shadow_root:
                    accept_btn = shadow_root.find_element(By.CSS_SELECTOR, "button[data-testid='uc-accept-all-button']")
                    if accept_btn.is_displayed():
                        driver.execute_script("arguments[0].click();", accept_btn)
                        return
        except: pass

        selectors = [
            "//button[contains(@class, 'rcb-btn-accept-all')]", 
            "//button[normalize-space()='Accept all']",
            "//a[normalize-space()='Accept all']"
        ]
        for xpath in selectors:
            try:
                btns = driver.find_elements(By.XPATH, xpath)
                for btn in btns:
                    if btn.is_displayed():
                        driver.execute_script("arguments[0].click();", btn)
                        return 
            except: continue
        time.sleep(0.5)

# --- SHARED NAVIGATION ---
def click_back_button(driver):
    back_selectors = [
        "//button[contains(., 'Back to categories')]", # Hangzhou style
        "//button[.//div[text()='Back']]", # Delhi style
        "//button[.//svg[contains(@class, 'lucide-chevron-left')]]",
        "//button[.//svg[contains(@class, 'fa-chevron-left')]]",
        "//button[contains(., '返回类别')]",
        "//button[contains(., 'Back')]"
    ]
    
    for xpath in back_selectors:
        try:
            btns = driver.find_elements(By.XPATH, xpath)
            for btn in btns:
                if btn.is_enabled() and btn.is_displayed():
                    driver.execute_script("arguments[0].click();", btn)
                    return True
        except: continue
    return False


def click_back_button_china(driver):
    try:
        # Targeted selectors for the Shanghai/Modern UI
        selectors = [
            "//button[contains(., 'Back to categories')]",
            "//div[contains(text(), 'Back to categories')]",
            "//button[contains(., '返回类别')]", 
            "//div[contains(@class, 'vi-cursor-pointer')]//svg[contains(@class, 'lucide-chevron-left')]"
        ]
        for xpath in selectors:
            btns = driver.find_elements(By.XPATH, xpath)
            for btn in btns:
                if btn.is_displayed():
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                    driver.execute_script("arguments[0].click();", btn)
                    return True
    except: pass
    return False

def wait_for_view_restoration(driver, text_to_find):
    end_time = time.time() + 5
    while time.time() < end_time:
        try:
            elements = driver.find_elements(By.CLASS_NAME, "card-list-item") + \
                       driver.find_elements(By.XPATH, "//a[contains(@class, 'vi-rounded-lg')]")
            for el in elements:
                clean_el_text = normalize_text(el.text)
                clean_target = normalize_text(text_to_find)
                if clean_target in clean_el_text and el.is_displayed():
                    return True
        except: pass
        time.sleep(0.5)
    return False

def scrape_current_view(driver, exclude_prefixes):
    tickets = []
    # Check if there are any ticket containers
    rows = driver.find_elements(By.CLASS_NAME, "ticket-type")
    if not rows:
        return []

    for row in rows:
        try:
            name = ""
            for selector in [".vi-font-semibold", "p.vi-text", "div.vi-gap-2"]:
                try:
                    name_el = row.find_element(By.CSS_SELECTOR, selector)
                    name = name_el.text.strip()
                    if name: break
                except: continue
            
            if not name: name = row.text.split('\n')[0]
            name = normalize_text(name)
            
            if not name or any(name.lower().startswith(p.lower()) for p in exclude_prefixes):
                continue
            
            # Status Logic
            status = "Sold out"
            row_html = row.get_attribute("outerHTML").lower()
            # If the row doesn't have the 'sold-out' class and has a button
            if "sold-out" not in row.get_attribute("class"):
                add_btns = row.find_elements(By.CSS_SELECTOR, "button[aria-label^='Add']")
                # If the button is enabled and visible, in some cases the button is present but disabled
                if add_btns and add_btns[0].is_displayed() and add_btns[0].is_enabled():
                    status = "Available"
            
            tickets.append({"name": name, "status": status})
        except: continue
    return tickets
def traverse_menu(driver, exclude_prefixes, depth=0):
    found_tickets = []
    
    # 1. Scrape tickets at current level
    tickets_here = scrape_current_view(driver, exclude_prefixes)
    if tickets_here:
        found_tickets.extend(tickets_here)

    # 2. Identify Navigation Elements
    navigation_elements = driver.find_elements(By.CSS_SELECTOR, "a.vi-rounded-lg, button.card-list-item, a.no-decoration")
    
    option_map = [] # Store (cleaned_name, original_element)
    
    for el in navigation_elements:
        try:
            # Try to find the specific title div (Hangzhou style)
            # or the main text (Delhi style)
            title_el = None
            for selector in ["div.vi-text", "div.vi-font-medium", "span"]:
                try:
                    candidates = el.find_elements(By.CSS_SELECTOR, selector)
                    if candidates:
                        title_el = candidates[-1] # Usually the last one is the name
                        break
                except: continue
            
            raw_text = title_el.text if title_el else el.text
            
            # Scrub status labels from the string
            clean_name = raw_text.replace("Sold out", "").replace("Tickets available", "")
            clean_name = clean_name.replace("SOLD OUT", "").replace("TICKETS AVAILABLE", "").strip()
            
            if clean_name and clean_name not in ["Select", "Select…", "Back to categories"]:
                option_map.append(clean_name)
        except: continue
    
    option_list = list(dict.fromkeys(option_map))

    # 3. Process Options
    for opt_text in option_list:
        if any(normalize_text(opt_text).lower().startswith(p.lower()) for p in exclude_prefixes):
            continue

        print(f"    [Depth {depth}] Clicking: {opt_text}")
        
        # Robust XPath: Finds the link/button that contains the cleaned text
        xpath_selector = f"//*[(self::a or self::button) and (descendant-or-self::*[normalize-space()='{opt_text}'])]"
        
        try:
            target = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.XPATH, xpath_selector)))
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", target)
            time.sleep(0.5)
            driver.execute_script("arguments[0].click();", target)
            
            time.sleep(2.5) # Wait for swap
            found_tickets.extend(traverse_menu(driver, exclude_prefixes, depth + 1))
            
            print(f"    [Depth {depth}] Navigating back from {opt_text}...")
            if not click_back_button(driver):
                driver.execute_script("window.history.go(-1)")
            time.sleep(1.5)
            
        except Exception as e:
            print(f"    ! Error clicking {opt_text}")

    # Deduplicate
    unique = {t['name']: t for t in found_tickets}.values()
    return list(unique)

def traverse_menu_china(driver, exclude_prefixes, depth=0):
    """Specialized traversal for Shanghai/China using the 'Back to categories' flow."""
    print(f"    [China Depth {depth}] Checking view...")
    found_tickets = []
    
    # Wait for the view to load (either tickets or category links)
    try:
        WebDriverWait(driver, 10).until(
            lambda d: d.find_elements(By.CLASS_NAME, "ticket-type") or 
                      d.find_elements(By.CSS_SELECTOR, "a.vi-rounded-lg")
        )
    except TimeoutException:
        return []

    # 1. Scrape tickets at this level
    tickets_here = scrape_current_view(driver, exclude_prefixes)
    if tickets_here:
        print(f"    [China Depth {depth}] Found {len(tickets_here)} tickets.")
        found_tickets.extend(tickets_here)

    # 2. Handle Folders/Categories
    potential_folders = driver.find_elements(By.CSS_SELECTOR, "a.vi-rounded-lg")
    folder_names = []
    for f in potential_folders:
        try:
            # The folder name is inside a div with class vi-font-medium
            text_el = f.find_element(By.CLASS_NAME, "vi-font-medium")
            raw = text_el.text.strip()
            if raw and not any(x in raw.lower() for x in ["available", "select", "sold out"]):
                folder_names.append(raw)
        except: continue
            
    folder_names = list(dict.fromkeys(folder_names))

    if folder_names and depth == 0:
        print(f"    [China Depth 0] Folders to process: {folder_names}")

    for opt_text in folder_names:
        if any(normalize_text(opt_text).lower().startswith(p.lower()) for p in exclude_prefixes):
            continue

        print(f"    [China Depth {depth}] Clicking category: {opt_text}")
        
        try:
            # Click category
            target_xpath = f"//a[.//div[normalize-space()='{opt_text}']]"
            target = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.XPATH, target_xpath)))
            driver.execute_script("arguments[0].click();", target)
            time.sleep(2) 
            
            # Recurse
            found_tickets.extend(traverse_menu_china(driver, exclude_prefixes, depth + 1))
            
            # GO BACK using the CHINA SPECIFIC function
            print(f"    [China Depth {depth}] Navigating back from {opt_text}...")
            if not click_back_button_china(driver):
                driver.execute_script("window.history.go(-1)")
            
            # Wait for restoration
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "a.vi-rounded-lg"))
            )
            time.sleep(1.5)
            
        except Exception as e:
            print(f"    ! Error in {opt_text}: {e}")

    return found_tickets

def _process_hyrox_event_page_china(site_config, driver):
    print(f"  > [China Flow] Loading: {site_config['url']}")
    driver.get(site_config['url'])
    handle_cookies(driver)
    
    checkout_url = None

    try:
        # 1. Click main button (aria-label matches your HTML)
        buy_btn = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "button[aria-label='Buy Tickets here']"))
        )
        driver.execute_script("arguments[0].click();", buy_btn)
        print("    > Step 1: Clicked 'Buy Tickets here'")

        # 2. Click "Athlete Tickets" in the resulting popup
        # Note: Your HTML shows this as an <a> tag with span class "w-btn-label"
        athlete_btn = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, "//a[.//span[contains(text(), 'Athlete Tickets')]]"))
        )
        driver.execute_script("arguments[0].click();", athlete_btn)
        print("    > Step 2: Clicked 'Athlete Tickets'")

        # 3. Extract the URL from the <object id="sellmodal-anchor">
        # We loop because it takes a moment for the 'data' attribute to populate
        print("    > Step 3: Waiting for checkout anchor...")
        for _ in range(10):
            anchors = driver.find_elements(By.ID, "sellmodal-anchor")
            if anchors:
                raw_url = anchors[0].get_attribute("data")
                if raw_url and "checkout" in raw_url:
                    checkout_url = clean_checkout_url(raw_url)
                    break
            time.sleep(1)

    except Exception as e:
        print(f"    ! Extraction Error: {e}")

    if checkout_url:
        print(f"  > Processing China Checkout: {checkout_url[:60]}...")
        driver.get(checkout_url)
        handle_cookies(driver)
        # CRITICAL: Wait for the internal app to boot before starting traversal
        time.sleep(5) 
        
        all_tickets = traverse_menu_china(driver, site_config.get("exclude_prefixes", []))
        
        # Format results for the main script
        current_status = {"General": {"found": True, "details": []}}
        if all_tickets:
            unique = {t['name']:t for t in all_tickets}.values()
            current_status["General"]["details"] = sorted(list(unique), key=lambda x: x['name'])
        
        # Load prev and compare (matches your existing logic)
        status_file = site_config['status_file']
        try:
            with open(status_file, 'r', encoding='utf-8') as f: previous_status = json.load(f)
        except: previous_status = {}

        if previous_status != current_status:
            html_body, changed_tickets = generate_diff_html(site_config, previous_status, current_status)
            if changed_tickets:
                log_intermediate_changes(site_config['name'], changed_tickets)
            if html_body:
                with open(status_file, 'w', encoding='utf-8') as f: 
                    json.dump(current_status, f, indent=2, ensure_ascii=False)
                return {"change_detected": True, "site_config": site_config, "html_body": html_body}
        
        return {"change_detected": False}
    else:
        print("  ! Failed to extract China checkout URL.")
        return {"change_detected": False}

def execute_checkout_scraping(driver, checkout_url, site_config):
    print(f"  > Clean Checkout URL: {checkout_url[:60]}...")
    driver.get(checkout_url)
    handle_cookies(driver)
    
    print("  > Waiting for content...")
    is_page_valid = False
    
    try:
        WebDriverWait(driver, 15).until(
            lambda d: d.find_elements(By.CLASS_NAME, "card-list-item") or 
                      d.find_elements(By.CLASS_NAME, "ticket-type") or
                      d.find_elements(By.XPATH, "//a[contains(@class, 'vi-rounded-lg')]") or
                      d.find_elements(By.CLASS_NAME, "fallback-box")
        )
        is_page_valid = True
    except TimeoutException:
        print("  ! Checkout page did not load content (Timeout).")
        safe_name = site_config['name'].replace(' ', '_').replace("'", "")
        driver.save_screenshot(f"debug_failed_load_{safe_name}.png")
        return {"change_detected": False}

    # Load previous status early to compare
    status_file = site_config['status_file']
    try:
        with open(status_file, 'r', encoding='utf-8') as f: 
            previous_status = json.load(f)
    except: 
            previous_status = {}

    all_tickets = []
    sale_ended_elements = driver.find_elements(By.CLASS_NAME, "fallback-box")
    sale_ended_flag = False
    if sale_ended_elements:
         for box in sale_ended_elements:
             if box.is_displayed() and "sale has ended" in box.text.lower():
                 sale_ended_flag = True
                 break

    if sale_ended_flag:
        print("  > Detected 'Sale has ended'. Marking all tickets as Sold Out.")
        # If sale ended, we take previous tickets and mark them all sold out
        prev_details = previous_status.get("General", {}).get("details", [])
        for t in prev_details:
            all_tickets.append({**t, "status": "Sold Out"})
    else:
        all_tickets = traverse_menu(driver, site_config.get("exclude_prefixes", []))

    # --- NEW LOGIC: RECONCILE MISSING TICKETS ---
    # If the page loaded (is_page_valid) but a previously known ticket is missing from the scrape,
    # it means it was removed from the site (Sold Out).
    if is_page_valid and not sale_ended_flag:
        current_names = {t['name'] for t in all_tickets}
        prev_details = previous_status.get("General", {}).get("details", [])
        for prev_t in prev_details:
            if prev_t['name'] not in current_names:
                # Add it back to the list but mark as Sold Out
                sold_out_t = prev_t.copy()
                sold_out_t['status'] = 'Sold Out'
                all_tickets.append(sold_out_t)
    # --------------------------------------------

    current_status = {"General": {"found": is_page_valid, "details": []}}
    
    if all_tickets:
        unique = {t['name']:t for t in all_tickets}.values()
        current_status["General"]["details"] = sorted(list(unique), key=lambda x: x['name'])
        print(f"  > Success! Found {len(current_status['General']['details'])} unique tickets.")
    else:
        if not sale_ended_flag:
            print("  > No tickets found (All categories excluded).")

    if previous_status != current_status and current_status["General"]["found"]:
        # Generate HTML and get list of changed items
        html_body, changed_tickets = generate_diff_html(site_config, previous_status, current_status)
        
        # Log changes for the persistent matrix
        if changed_tickets:
            log_intermediate_changes(site_config['name'], changed_tickets)
        
        if html_body:
            print(f"  > CHANGE DETECTED!")
            with open(status_file, 'w', encoding='utf-8') as f: 
                json.dump(current_status, f, indent=2, ensure_ascii=False)
            return {
                "change_detected": True, 
                "site_config": site_config,
                "html_body": html_body
            }
        else:
             print("  > Syncing status file (No visible change).")
             with open(status_file, 'w', encoding='utf-8') as f: 
                json.dump(current_status, f, indent=2, ensure_ascii=False)
    
    return {"change_detected": False}

# --- PROCESSORS ---
def _process_hyrox_event_page(site_config, driver):
    print(f"  > [Standard Flow] Loading event page...")
    driver.get(site_config['url'])
    handle_cookies(driver)
    
    checkout_url = None
    try:
        buy_btn = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH, "//button[@aria-label='Buy Tickets here']"))
        )
        driver.execute_script("arguments[0].click();", buy_btn)
    except TimeoutException:
        print("    ! Could not find 'Buy Tickets here' button.")
        return {"change_detected": False}

    try:
        time.sleep(1) 
        athlete_link = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.XPATH, "//a[contains(., 'Athlete Tickets')]"))
        )
        driver.execute_script("arguments[0].click();", athlete_link)
        
        time.sleep(2)
        try:
            obj = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.ID, "sellmodal-anchor"))
            )
            raw_url = obj.get_attribute("data")
            checkout_url = clean_checkout_url(raw_url)
        except TimeoutException:
            objs = driver.find_elements(By.TAG_NAME, "object")
            for o in objs:
                if "checkout" in (o.get_attribute("data") or ""):
                    checkout_url = clean_checkout_url(o.get_attribute("data"))
                    break
    except Exception: pass
    
    if checkout_url:
        return execute_checkout_scraping(driver, checkout_url, site_config)
    else:
        print("  ! Failed to extract checkout URL.")
        return {"change_detected": False}

def _process_hyrox_event_page_india(site_config, driver):
    print(f"  > [India Flow] Loading event page...")
    driver.switch_to.default_content() # Ensure we aren't stuck in a Shanghai iframe
    driver.get(site_config['url'])
    handle_cookies(driver)
    time.sleep(3) # India site needs a bit more time for the 'Book Now' button to render
    
    checkout_url = None
    try:
        keywords = ["buy ticket", "register", "get ticket", "book now", "tickets"]
        target = None
        for kw in keywords:
            if target: break
            xpath = f"//*[(self::a or self::button or contains(@class, 'btn') or contains(@class, 'button')) and contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{kw}')]"
            elements = driver.find_elements(By.XPATH, xpath)
            for el in elements:
                if el.is_displayed():
                    target = el
                    break
        
        if target:
            if target.tag_name == 'a':
                href = target.get_attribute('href')
                if href and ("checkout" in href or "vivenu" in href):
                    checkout_url = clean_checkout_url(href)
            
            if not checkout_url:
                current_url = driver.current_url
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", target)
                time.sleep(0.5)
                try: target.click()
                except: driver.execute_script("arguments[0].click();", target)
                time.sleep(3) 
                
                if driver.current_url != current_url and ("checkout" in driver.current_url or "vivenu" in driver.current_url):
                    checkout_url = clean_checkout_url(driver.current_url)
                
                if not checkout_url and len(driver.window_handles) > 1:
                    driver.switch_to.window(driver.window_handles[-1])
                    if "checkout" in driver.current_url or "vivenu" in driver.current_url:
                        checkout_url = clean_checkout_url(driver.current_url)

                if not checkout_url:
                    objs = driver.find_elements(By.ID, "sellmodal-anchor")
                    if objs:
                        data = objs[0].get_attribute("data")
                        if data: checkout_url = clean_checkout_url(data)
                    
                    if not checkout_url:
                        frames = driver.find_elements(By.TAG_NAME, "iframe")
                        for f in frames:
                            src = f.get_attribute("src")
                            if src and ("checkout" in src or "vivenu" in src):
                                checkout_url = clean_checkout_url(src)
                                break
    except Exception as e: print(f"    ! Error in India flow: {e}")

    if checkout_url:
        return execute_checkout_scraping(driver, checkout_url, site_config)
    else:
        print("  ! Failed to extract India checkout URL.")
        return {"change_detected": False}

# --- ON SALE CHECKER ---
def process_on_sale_site(site_config, driver):
    name = site_config['name']
    url = site_config['url']
    if site_config.get('on_sale'): return {"change_detected": False}
    
    print(f"\n--- Checking On Sale: {name} ---")
    try:
        driver.get(url)
        handle_cookies(driver)
        
        src = driver.page_source.lower()
        if "buy tickets" in src or "register now" in src or "get tickets" in src:
            print("  > ON SALE DETECTED!")
            site_config['on_sale'] = True
            return {"change_detected": True, "site_config": site_config}
    except TimeoutException:
        print(f"  ! Timeout loading {name}. Skipping...")
    except Exception as e: 
        print(f"  ! Error checking OS {name}: {e}")
    
    return {"change_detected": False}

# --- MAIN ROUTER ---
def process_ticket_details_site(site_config, driver):
    name = site_config['name']
    site_type = site_config.get("site_type", "hyrox_event_page")
    
    reset_driver_state(driver)
    
    print(f"\n--- Processing: {name} (Type: {site_type}) ---")
    try:
        if site_type == "hyrox_event_page":
            return _process_hyrox_event_page(site_config, driver)
        elif site_type == "hyrox_event_page_india":
            return _process_hyrox_event_page_india(site_config, driver)
        elif site_type == "hyrox_event_page_china":  # <--- ADD THIS
            return _process_hyrox_event_page_china(site_config, driver)
        else:
            print(f"  ! Unknown site_type: {site_type}")
            return {"change_detected": False}
    except Exception as e:
        print(f"  ! Unexpected error: {e}")
        return {"change_detected": False}

# --- MATRIX GENERATION (UPDATED FOR INTERMEDIATE CHANGES) ---
def generate_availability_matrix():
    print("Generating matrix...")
    
    try:
        with open(TICKET_DETAILS_CONFIG, 'r') as f: config = json.load(f)
        sites = config.get("sites", [])
    except: return

    try:
        with open(MATRIX_STATE_FILE, 'r') as f: prev_matrix = json.load(f)
    except: prev_matrix = {}

    # Load Intermediate Changes
    try:
        with open(MATRIX_CHANGES_FILE, 'r') as f: intermediate_changes = json.load(f)
    except: intermediate_changes = {}

    site_names = [s['name'] for s in sites]
    curr_matrix = {n: {c: False for c in DISPLAY_CATEGORIES} for n in site_names}

    for site in sites:
        try:
            with open(site['status_file'], 'r') as f: data = json.load(f)
            tickets = []
            for k, v in data.items():
                if "details" in v: tickets.extend(v["details"])
            
            for t in tickets:
                if t.get("status") == "Available":
                    norm_name = _normalize_for_matrix(t.get("name", ""))
                    for cat in MATCHING_CATEGORIES:
                        if _normalize_for_matrix(cat) in norm_name:
                            curr_matrix[site['name']][cat] = True
                            break
        except: pass

    # DRAWING
    CELL_SIZE = 40; COL_HEADER_HEIGHT = 150; ROW_HEADER_WIDTH = 250; PADDING = 20
    FONT_SIZE = 14; AVAILABLE_COLOR = "#77DD77"; UNAVAILABLE_COLOR = "#FF6961"
    GRID_COLOR = "#D3D3D3"; TEXT_COLOR = "#000000"; BG_COLOR = "#FFFFFF"

    w = ROW_HEADER_WIDTH + (len(site_names) * CELL_SIZE) + PADDING * 2
    h = COL_HEADER_HEIGHT + (len(DISPLAY_CATEGORIES) * CELL_SIZE) + PADDING * 2
    
    try: font = ImageFont.truetype("arial.ttf", FONT_SIZE)
    except: font = ImageFont.load_default()
    cross_font = font # Simple fallback, could use larger if arial available
    try: cross_font = ImageFont.truetype("arialbd.ttf", FONT_SIZE + 4)
    except: pass

    img = Image.new('RGB', (w, h), BG_COLOR)
    draw = ImageDraw.Draw(img)

    for i, name in enumerate(site_names):
        x = ROW_HEADER_WIDTH + (i * CELL_SIZE) + (CELL_SIZE / 2) + PADDING
        y = COL_HEADER_HEIGHT - 10 + PADDING
        txt = Image.new('L', (COL_HEADER_HEIGHT, FONT_SIZE + 10))
        d = ImageDraw.Draw(txt)
        d.text((0, 0), name, font=font, fill=255)
        r_txt = txt.rotate(90, expand=1)
        img.paste(TEXT_COLOR, (int(x - r_txt.size[0]/2), int(y - r_txt.size[1])), r_txt)

    for i, cat in enumerate(DISPLAY_CATEGORIES):
        draw.text((PADDING, COL_HEADER_HEIGHT + (i * CELL_SIZE) + 20 + PADDING), cat, font=font, fill=TEXT_COLOR, anchor="lm")
        
    for r, cat in enumerate(DISPLAY_CATEGORIES):
        y1 = COL_HEADER_HEIGHT + (r * CELL_SIZE) + PADDING
        y2 = y1 + CELL_SIZE
        for c, name in enumerate(site_names):
            x1 = ROW_HEADER_WIDTH + (c * CELL_SIZE) + PADDING
            x2 = x1 + CELL_SIZE
            
            avail = curr_matrix.get(name, {}).get(cat, False)
            color = AVAILABLE_COLOR if avail else UNAVAILABLE_COLOR
            draw.rectangle([x1, y1, x2, y2], fill=color, outline=GRID_COLOR)
            
            # CHECK FOR CHANGES (vs Last Matrix OR Intermediate)
            prev_avail = prev_matrix.get(name, {}).get(cat, False)
            
            # Did it change since last matrix run?
            status_changed = avail != prev_avail
            
            # Was it flagged in intermediate runs?
            was_flagged = cat in intermediate_changes.get(name, [])
            
            if status_changed or was_flagged:
                draw.text((x1 + CELL_SIZE/2, y1 + CELL_SIZE/2), "X", font=cross_font, fill=TEXT_COLOR, anchor="mm")

    try:
        ts = datetime.now(pytz.timezone('Asia/Kuala_Lumpur')).strftime("%y:%m:%d %H:%M MST")
        draw.text((w - PADDING, PADDING), ts, font=font, fill=TEXT_COLOR, anchor="ra")
    except: pass
    
    img.save(MATRIX_OUTPUT_FILE)
    print(f"Matrix saved to {MATRIX_OUTPUT_FILE}")
    
    # Save new state
    if curr_matrix != prev_matrix:
        with open(MATRIX_STATE_FILE, 'w') as f: json.dump(curr_matrix, f, indent=2)
    
    # Clear intermediate changes after successful matrix generation
    try:
        with open(MATRIX_CHANGES_FILE, 'w') as f: json.dump({}, f)
        print("  > Intermediate changes cleared.")
    except: pass
    
    set_github_output('matrix_changed', 'true') # Always upload artifact

def email_matrix():
    mail_user = os.getenv('MAIL_USERNAME'); mail_pass = os.getenv('MAIL_PASSWORD')
    if not (mail_user and mail_pass): return
    try:
        with open(TICKET_DETAILS_CONFIG, 'r') as f: rcpt = json.load(f).get("matrix_email_to")
    except: return
    
    # Define time and 'now' locally so it's recognized
    mst = pytz.timezone('Asia/Kuala_Lumpur')
    now_dt = datetime.now(mst) 
    
    date_str = now_dt.strftime('%Y%m%d') # Example: 20240724
    pretty_name = f"HyroxMonitorMatrix{date_str}.png"
    
    sub = f"Hyrox Matrix - {now_dt.strftime('%Y-%m-%d')}"
    body = "<html><body><p>Attached is the latest availability matrix.</p><img src='cid:matrix_image'></body></html>"

    # Temporary rename for the attachment
    if os.path.exists(MATRIX_OUTPUT_FILE):
        os.rename(MATRIX_OUTPUT_FILE, pretty_name)
    
    try:
        send_email(sub, body, rcpt, mail_user, mail_pass, pretty_name)
    except Exception as e:
        print(f"Error in matrix email: {e}")
    finally:
        # Rename it back to the original name so the script stays consistent
        if os.path.exists(pretty_name):
            os.rename(pretty_name, MATRIX_OUTPUT_FILE)    
    
    #send_email(sub, body, rcpt, mail_user, mail_pass, MATRIX_OUTPUT_FILE)

# --- MAIN ---
def main(headless=True, target_priority=None): # Added target_priority argument):
    mail_user = os.getenv('MAIL_USERNAME'); mail_pass = os.getenv('MAIL_PASSWORD')
    change = False
    
    driver = setup_driver(headless)
    
    # 1. On Sale Checks
    try:
        with open(ON_SALE_CONFIG, 'r') as f: on_sale_sites = json.load(f)
        os_updated = False
        for s in on_sale_sites:
            
            # --- MINIMAL CHANGE: Filter by priority ---
            if target_priority and s.get('priority', 'low').lower() != target_priority.lower():
                continue
            
            try:
                res = process_on_sale_site(s, driver)
                if res.get("change_detected"):
                    change = True
                    os_updated = True
                    if mail_user and mail_pass and res['site_config'].get("email_to"):
                        subj = f"[{s['name']}] Tickets are ON SALE!"
                        body = f"<html><body><p>Go to: <a href='{s['url']}'>{s['url']}</a></p></body></html>"
                        send_email(subj, body, res['site_config']['email_to'], mail_user, mail_pass)
            except Exception as e: print(f"Error checking OS {s['name']}: {e}")
        
        if os_updated:
            with open(ON_SALE_CONFIG, 'w') as f: json.dump(on_sale_sites, f, indent=2)
    except: pass

    # 2. Ticket Checks
    try:
        with open(TICKET_DETAILS_CONFIG, 'r') as f: sites = json.load(f)["sites"]
        
        for s in sites:
            
            # --- MINIMAL CHANGE: Filter by priority ---
            if target_priority and s.get('priority', 'low').lower() != target_priority.lower():
                continue
            
            try:
                res = process_ticket_details_site(s, driver)
                if res.get("change_detected"):
                    change = True
                    if mail_user and mail_pass and res['site_config'].get("email_to"):
                        subject = f"[{s['name']}] Status Change Detected"
                        html_body = res.get("html_body", "No details")
                        send_email(subject, html_body, res['site_config']['email_to'], mail_user, mail_pass)
            except Exception as e:
                print(f"Error processing {s['name']}: {e}")
               
    except Exception as e: print(f"Fatal Error: {e}")
    
    finally:
        if not headless:
            input("Debug Mode: Press Enter to close the browser and exit...")
        driver.quit()
        
    if change: set_github_output('changes_detected', 'true')

if __name__ == "__main__":
    print(f"System Arguments received: {sys.argv}")
    is_headless = "--visible" not in sys.argv
    
    # --- MINIMAL CHANGE: Parse priority argument ---
    priority_val = None
    if "--priority" in sys.argv:
        idx = sys.argv.index("--priority")
        if idx + 1 < len(sys.argv):
            priority_val = sys.argv[idx + 1]

    if "--matrix" in sys.argv: 
        generate_availability_matrix()
    elif "--email-matrix" in sys.argv: 
        email_matrix()
    else: 
        main(headless=is_headless, target_priority=priority_val)