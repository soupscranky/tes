import os
import csv
import random
import time
import base64
import requests
from seleniumbase import sb_cdp

# Configuration
TARGET_URL = os.environ.get("SYNC_URL") 
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
DATA_CSV_B64 = os.environ.get("DATA_CSV_B64")
NEXT_ROW = int(os.environ.get("NEXT_ROW", 0))
PAT_TOKEN = os.environ.get("PAT_TOKEN")
REPO = os.environ.get("GITHUB_REPOSITORY") # Format: OWNER/REPO

# Mappings (US and CA provinces)
US_STATES = [
    "Alaska", "Alabama", "Arkansas", "American Samoa", "Arizona", "California",
    "Colorado", "Connecticut", "District of Columbia", "Delaware", "Florida",
    "Georgia", "Guam", "Hawaii", "Iowa", "Idaho", "Illinois", "Indiana",
    "Kansas", "Kentucky", "Louisiana", "Massachusetts", "Maryland", "Maine",
    "Michigan", "Minnesota", "Missouri", "Northern Mariana Islands", "Mississippi",
    "Montana", "North Carolina", "North Dakota", "Nebraska", "New Hampshire",
    "New Jersey", "New Mexico", "Nevada", "New York", "Ohio", "Oklahoma",
    "Oregon", "Pennsylvania", "Puerto Rico", "Rhode Island", "South Carolina",
    "South Dakota", "Tennessee", "Texas", "United States Minor Outlying Islands",
    "Utah", "Virginia", "Virgin Islands", "Vermont", "Washington", "Wisconsin",
    "West Virginia", "Wyoming"
]

CA_PROVINCES = [
    "Alberta", "British Columbia", "Manitoba", "New Brunswick", "Newfoundland and Labrador",
    "Nova Scotia", "Northwest Territories", "Nunavut", "Ontario", "Prince Edward Island",
    "Quebec", "Saskatchewan", "Yukon Territory"
]

COUNTRIES = [
    {"value": "US", "label": "United States", "states": US_STATES},
    {"value": "CA", "label": "Canada", "states": CA_PROVINCES},
]

def sync_progress(new_val):
    """Updates the progress variable via API."""
    if not PAT_TOKEN or not REPO:
        print("Skipping progress update (Required tokens not set).")
        return
    
    url = f"https://api.github.com/repos/{REPO}/actions/variables/NEXT_ROW"
    headers = {
        "Authorization": f"token {PAT_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        r = requests.patch(url, headers=headers, json={"name": "NEXT_ROW", "value": str(new_val)})
        if r.status_code == 204:
            print(f"Successfully updated progress to {new_val}")
        else:
            print(f"Failed to update progress: {r.status_code}")
    except Exception as e:
        print(f"Error syncing: {e}")

def send_discord_notification(message):
    if DISCORD_WEBHOOK_URL:
        try:
            requests.post(DISCORD_WEBHOOK_URL, json={"content": message})
        except Exception as e:
            print(f"Error sending Discord notification: {e}")

def execute_sync():
    # 1. Decode CSV Data
    if not DATA_CSV_B64:
        print("Error: DATA_CSV_B64 secret not found")
        return
        
    try:
        csv_data = base64.b64decode(DATA_CSV_B64).decode('utf-8')
        lines = [line for line in csv_data.splitlines() if line.strip()]
        reader = csv.DictReader(lines)
        accounts = list(reader)
    except Exception as e:
        print(f"Error decoding CSV: {e}")
        return

    if NEXT_ROW >= len(accounts):
        print(f"Finished: NEXT_ROW ({NEXT_ROW}) >= total accounts ({len(accounts)})")
        return

    account = accounts[NEXT_ROW]
    email_addr = account["email"]
    first_name = account["first_name"]
    print(f"Syncing item {NEXT_ROW}: {email_addr}")
    
    # Update progress immediately
    sync_progress(NEXT_ROW + 1)
    
    is_gh = os.environ.get("GITHUB_ACTIONS") == "true"
    sb = sb_cdp.Chrome(TARGET_URL, headless=is_gh)
    
    try:
        # Wait for form
        sb.wait_for_element("#field_email_address")
        
        # Email and First Name
        sb.type("#field_email_address", email_addr)
        sb.type("#field_first_name", first_name)
        
        # Select random country
        country = random.choice(COUNTRIES)
        sb.click('span[aria-labelledby="select2-field_country_region-container"]')
        sb.sleep(1)
        sb.select_option_by_value("#field_country_region", country["value"])
        # Use Enter to confirm and close the dropdown
        sb.press_keys("body", "\n") 
        sb.sleep(2) # Wait for AJAX
        
        if country["states"]:
            state_val = random.choice(country["states"])
            sb.click('span[aria-labelledby="select2-field_state-container"]')
            sb.sleep(1)
            
            # Resilient selection with Enter
            selected = False
            for _ in range(3):
                try:
                    sb.select_option_by_value("#field_state", state_val)
                    sb.press_keys("body", "\n")
                    selected = True
                    break
                except Exception:
                    sb.sleep(2)
            
            if not selected:
                print(f"Warning: Could not select state {state_val}")

        # Final check and Submit
        sb.click("#custom_checkbox_4_0")
        sb.sleep(1)
        
        sb.click("#form-submit")
        sb.sleep(5) # Wait for submission response
        
        send_discord_notification(f"✅ Item {NEXT_ROW} processed for: {email_addr}")
        print(f"Finished item {email_addr}")
            
    finally:
        sb.driver.stop()

if __name__ == "__main__":
    execute_sync()
