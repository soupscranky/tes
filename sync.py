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

# Data Mappings
FR_DEPARTMENTS = [
    "Ain", "Aisne", "Allier", "Alpes-de-Haute-Provence", "Hautes-Alpes", "Alpes-Maritimes", "Ardèche", "Ardennes", 
    "Ariège", "Aube", "Aude", "Aveyron", "Bouches-du-Rhône", "Calvados", "Cantal", "Charente", "Charente-Maritime", 
    "Cher", "Corrèze", "Corse-du-Sud", "Haute-Corse", "Côte-d'Or", "Côtes-d'Armor", "Creuse", "Dordogne", "Doubs", 
    "Drôme", "Eure", "Eure-et-Loir", "Finistère", "Gard", "Haute-Garonne", "Gers", "Gironde", "Hérault", "Ille-et-Vilaine", 
    "Indre", "Indre-et-Loire", "Isère", "Jura", "Landes", "Loir-et-Cher", "Loire", "Haute-Loire", "Loire-Atlantique", 
    "Loiret", "Lot", "Lot-et-Garonne", "Lozère", "Maine-et-Loire", "Manche", "Marne", "Haute-Marne", "Mayenne", 
    "Meurthe-et-Moselle", "Meuse", "Morbihan", "Moselle", "Nièvre", "Nord", "Oise", "Orne", "Pas-de-Calais", 
    "Puy-de-Dôme", "Pyrénées-Atlantiques", "Hautes-Pyrénées", "Pyrénées-Orientales", "Bas-Rhin", "Haut-Rhin", "Rhône", 
    "Métropole de Lyon", "Haute-Saône", "Saône-et-Loire", "Sarthe", "Savoie", "Haute-Savoie", "Paris", "Seine-Maritime", 
    "Seine-et-Marne", "Yvelines", "Deux-Sèvres", "Somme", "Tarn", "Tarn-et-Garonne", "Var", "Vaucluse", "Vendée", 
    "Vienne", "Haute-Vienne", "Vosges", "Yonne", "Territoire de Belfort", "Essonne", "Hauts-de-Seine", "Seine-Saint-Denis", 
    "Val-de-Marne", "Val-d'Oise", "Guadeloupe", "Martinique", "Guyane", "La Réunion", "Mayotte"
]

CA_PROVINCES = [
    "Alberta", "British Columbia", "Manitoba", "New Brunswick", "Newfoundland and Labrador",
    "Nova Scotia", "Northwest Territories", "Nunavut", "Ontario", "Prince Edward Island",
    "Quebec", "Saskatchewan", "Yukon Territory"
]

COUNTRIES = [
    {"value": "CA", "label": "Canada", "states": CA_PROVINCES},
    {"value": "FR", "label": "France", "states": FR_DEPARTMENTS},
]

def sync_progress(new_val):
    if not PAT_TOKEN or not REPO:
        return
    url = f"https://api.github.com/repos/{REPO}/actions/variables/NEXT_ROW"
    headers = {
        "Authorization": f"token {PAT_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        requests.patch(url, headers=headers, json={"name": "NEXT_ROW", "value": str(new_val)})
    except Exception:
        pass

def send_discord_notification(message):
    if DISCORD_WEBHOOK_URL:
        try:
            requests.post(DISCORD_WEBHOOK_URL, json={"content": message})
        except Exception:
            pass

def select_select2(sb, container_id, search_text):
    """Reliably selects an option from a Select2 dropdown."""
    # 1. Click the dropdown to open it
    sb.click(f'span[aria-labelledby="{container_id}"]')
    sb.sleep(1.5)
    
    # 2. Type into the generated search field
    sb.type('input.select2-search__field', search_text)
    sb.sleep(1.5)
    
    # 3. Press Enter to select the top match
    sb.press_keys('input.select2-search__field', '\n')
    sb.sleep(1)

def execute_sync():
    if not DATA_CSV_B64:
        print("Error: DATA_CSV_B64 missing")
        return
        
    try:
        csv_data = base64.b64decode(DATA_CSV_B64).decode('utf-8')
        lines = [line for line in csv_data.splitlines() if line.strip()]
        reader = csv.DictReader(lines)
        accounts = list(reader)
    except Exception:
        return

    if NEXT_ROW >= len(accounts):
        print(f"Sync complete. (Index {NEXT_ROW})")
        return

    account = accounts[NEXT_ROW]
    email_addr = account["email"]
    first_name = account["first_name"]
    print(f"Processing Item {NEXT_ROW}: {email_addr}")
    
    # Update progress for next run
    sync_progress(NEXT_ROW + 1)
    
    is_gh = os.environ.get("GITHUB_ACTIONS") == "true"
    sb = sb_cdp.Chrome(TARGET_URL, headless=is_gh)
    
    try:
        # Wait for fields to load
        sb.wait_for_element("#field_email_address", timeout=20)
        
        # Identity
        sb.type("#field_email_address", email_addr)
        sb.type("#field_first_name", first_name)
        
        # Country Selection
        country = random.choice(COUNTRIES)
        print(f"Selecting Country: {country['label']}")
        select_select2(sb, "select2-field_country_region-container", country["label"])
        
        # Wait for dynamic province list to update
        sb.sleep(2)
        
        # Province/Department Selection
        state_val = random.choice(country["states"])
        print(f"Selecting Region: {state_val}")
        select_select2(sb, "select2-field_state-container", state_val)
        
        # Confirmation (Age Checkbox)
        sb.click("#custom_checkbox_4_0")
        sb.sleep(1)
        
        # Submit
        sb.click("#form-submit")
        sb.sleep(2)
        
        # Success Verification
        try:
            # Wait for common success indicators (Sony Music forms often use .thanks or .form-success)
            sb.wait_for_any_element(".campaign-success, .thanks, .form-success, :contains('Merci')", timeout=15)
            print(f"✅ Sync SUCCESS verified for {email_addr}")
            send_discord_notification(f"✅ Item #{NEXT_ROW} VERIFIED: {email_addr} ({country['label']})")
        except Exception:
            # If no success message found, it might still have worked but we can't be 100% sure
            print(f"⚠️ Item #{NEXT_ROW} submitted, but could not verify success message on page.")
            send_discord_notification(f"⚠️ Item #{NEXT_ROW} submitted: {email_addr} (Unverified)")
            
        print(f"Finished item {email_addr}")
            
    except Exception as e:
        print(f"Sync error: {e}")
        send_discord_notification(f"❌ Sync #{NEXT_ROW} failed: {email_addr}")
    finally:
        sb.driver.stop()

if __name__ == "__main__":
    execute_sync()
