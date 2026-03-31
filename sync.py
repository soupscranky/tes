import base64
import csv
import io
import os
import sys
import time
from datetime import datetime

import requests
from seleniumbase import SB


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
FORM_URL = os.environ.get(
    "FORM_URL",
    "https://signup.ticketmaster.fr/paris-la-defense-arena",
)
REPO = os.environ.get("GITHUB_REPOSITORY", "OWNER/REPO")
PAT_TOKEN = os.environ.get("PAT_TOKEN", "")
NEXT_ROW = int(os.environ.get("NEXT_ROW", "1"))
DATA_CSV_B64 = os.environ.get("DATA_CSV_B64", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
GITHUB_API = "https://api.github.com"

# Form field selectors
FIELD_FIRST = "#form_container_name_0"
FIELD_LAST = "#form_container_name_1"
FIELD_EMAIL = "#form_container_email_0"
FIELD_CONF = "#form_container_email_1"
FIELD_PHONE = "#form_container_phone"
FIELD_AGREE = "#form_container_agree"
FIELD_AGREE_LABEL = "label[for='form_container_agree']"
FIELD_SUBMIT = "button.form_submit"

# Optional country selector candidates
COUNTRY_SELECT_CANDIDATES = [
    "select",
    "select.form_control",
    "select[name*='country']",
    "select[id*='country']",
]

# Success phrases
SUCCESS_PHRASES = [
    "vous êtes inscrit",
    "merci",
    "nous allons vous contacter",
    "prochainement",
    "lundi 6 avril 2026",
    "6 avril 2026",
    "confirmation",
    "inscription confirmée",
]

# Blocked phrases
FAIL_FORM_STILL_OPEN = [
    ("vous ne pouvez pas", "inscri"),
    ("already registered", None),
    ("déjà inscrit", None),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def driver_alive(sb: SB) -> bool:
    try:
        _ = sb.driver.current_url
        return True
    except Exception as e:
        log(f"Driver not alive: {e}")
        return False


def save_debug_artifacts(sb: SB, prefix: str) -> None:
    try:
        html = sb.get_page_source()
        html_name = f"{prefix}.html"
        with open(html_name, "w", encoding="utf-8") as f:
            f.write(html)
        log(f"Saved page source: {html_name}")
    except Exception as e:
        log(f"Could not save page source ({prefix}): {e}")

    try:
        png_name = f"{prefix}.png"
        sb.save_screenshot(png_name)
        log(f"Saved screenshot: {png_name}")
    except Exception as e:
        log(f"Could not save screenshot ({prefix}): {e}")


def discord_notify(status: str, message: str, row: int) -> None:
    if not DISCORD_WEBHOOK_URL:
        log("Discord webhook not configured — skipping.")
        return

    color = 3066993 if status == "SUCCESS" else 15158332
    payload = {
        "embeds": [{
            "title": f"Signup #{row} — {status}",
            "description": message,
            "color": color,
            "footer": {"text": "Paris La Défense Arena • Céline Dion 2026"},
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }]
    }

    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        r.raise_for_status()
        log("Discord notification sent.")
    except Exception as e:
        log(f"Discord notification failed: {e}")


def github_increment_row() -> None:
    if not PAT_TOKEN or not REPO:
        log("PAT_TOKEN/GITHUB_REPOSITORY not set — skip row increment.")
        return

    headers = {
        "Authorization": f"Bearer {PAT_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    resp = requests.get(
        f"{GITHUB_API}/repos/{REPO}/actions/variables/NEXT_ROW",
        headers=headers,
        timeout=10,
    )

    if resp.status_code == 200:
        current = resp.json().get("value", str(NEXT_ROW))
        new_val = str(int(current) + 1)
    else:
        new_val = str(NEXT_ROW + 1)

    requests.patch(
        f"{GITHUB_API}/repos/{REPO}/actions/variables/NEXT_ROW",
        headers=headers,
        json={"name": "NEXT_ROW", "value": new_val},
        timeout=10,
    )
    log(f"NEXT_ROW incremented to {new_val}.")


def parse_csv() -> dict:
    if not DATA_CSV_B64:
        raise ValueError("DATA_CSV_B64 secret is not set.")

    decoded = base64.b64decode(DATA_CSV_B64).decode("utf-8")
    reader = csv.DictReader(io.StringIO(decoded), delimiter=",", quotechar='"')
    rows = list(reader)

    if NEXT_ROW < 1 or NEXT_ROW > len(rows):
        raise ValueError(f"NEXT_ROW={NEXT_ROW} out of range (1–{len(rows)}).")

    return rows[NEXT_ROW - 1]


def body_preview(sb: SB, max_len: int = 1200) -> str:
    try:
        text = sb.get_text("body")
        return (text or "")[:max_len]
    except Exception as e:
        return f"[body unavailable: {e}]"


# ---------------------------------------------------------------------------
# Page handling
# ---------------------------------------------------------------------------

def dismiss_cookies(sb: SB) -> None:
    """
    Cookie banner may or may not appear on the direct signup page.
    Try a few common selectors, but do not fail if none are present.
    """
    selectors = [
        "#onetrust-reject-all-handler",
        "#onetrust-accept-btn-handler",
        "button#didomi-notice-agree-button",
        "button[aria-label*='Accept']",
        "button[aria-label*='Reject']",
    ]

    for selector in selectors:
        try:
            if sb.is_element_visible(selector):
                try:
                    sb.click(selector)
                except Exception:
                    el = sb.find_element(selector, timeout=2)
                    sb.driver.execute_script("arguments[0].click();", el)
                sb.sleep(1.0)
                log(f"Cookie action clicked: {selector}")
                return
        except Exception:
            pass

    log("No cookie action needed.")


def wait_for_form_page(sb: SB, row: int, timeout: int = 25) -> tuple[bool, str]:
    """
    Wait for the actual signup form fields to appear on the direct page.
    """
    log("Waiting for signup form to appear…")
    end = time.time() + timeout
    last_url = None

    while time.time() < end:
        if not driver_alive(sb):
            save_debug_artifacts(sb, f"driver_dead_row_{row}")
            return False, "WebDriver session died while waiting for form"

        try:
            current_url = sb.get_current_url()
            if current_url != last_url:
                log(f"Current URL: {current_url}")
                last_url = current_url
        except Exception:
            pass

        try:
            if sb.is_element_present(FIELD_FIRST):
                log("First-name field is present.")
                return True, "OK"
        except Exception:
            pass

        sb.sleep(0.5)

    save_debug_artifacts(sb, f"form_not_found_row_{row}")
    preview = body_preview(sb)
    return False, f"Signup form did not appear. Body preview: {preview}"


# ---------------------------------------------------------------------------
# Form filling
# ---------------------------------------------------------------------------

def fill_input(sb: SB, selector: str, value: str, name: str) -> bool:
    try:
        el = sb.find_element(selector, timeout=10)
        el.clear()
        el.send_keys(value)
        sb.sleep(0.2)
        stored = el.get_attribute("value") or ""
        log(f"Filled {name}: '{stored}'")
        return stored.strip() == value.strip()
    except Exception as e:
        log(f"fill_input error ({name} / {selector}): {e}")
        return False


def select_country_if_present(sb: SB, country: str) -> bool:
    if not country:
        return True

    for selector in COUNTRY_SELECT_CANDIDATES:
        try:
            if sb.is_element_present(selector):
                sb.select_option_by_text(selector, country)
                sb.sleep(0.3)
                log(f"Selected country '{country}' using {selector}")
                return True
        except Exception:
            pass

    log("No usable country select found; continuing.")
    return True


def fill_form(sb: SB, data: dict) -> dict:
    results = {}

    fn = data.get("first_name", "").strip()
    ln = data.get("last_name", "").strip()
    email = data.get("email", "").strip()
    phone = data.get("phone", "").strip()
    country = data.get("country", "").strip()

    results["first_name"] = fill_input(sb, FIELD_FIRST, fn, "first_name")
    results["last_name"] = fill_input(sb, FIELD_LAST, ln, "last_name")
    results["email"] = fill_input(sb, FIELD_EMAIL, email, "email")
    results["confirm"] = fill_input(sb, FIELD_CONF, email, "confirm_email")

    if phone:
        results["phone"] = fill_input(sb, FIELD_PHONE, phone, "phone")
    else:
        results["phone"] = True

    results["country"] = select_country_if_present(sb, country)

    return results


def click_agree(sb: SB) -> bool:
    try:
        if sb.is_element_present(FIELD_AGREE):
            checked = sb.get_attribute(FIELD_AGREE, "checked")
            if checked:
                log("Agree checkbox already checked.")
                return True
    except Exception:
        pass

    try:
        sb.slow_click(FIELD_AGREE_LABEL)
        sb.sleep(0.5)
    except Exception:
        try:
            label_el = sb.find_element(FIELD_AGREE_LABEL, timeout=5)
            sb.driver.execute_script("arguments[0].click();", label_el)
            sb.sleep(0.5)
        except Exception as e:
            log(f"click_agree label click error: {e}")
            return False

    try:
        checked = sb.driver.execute_script("""
            var el = document.querySelector(arguments[0]);
            return !!(el && el.checked);
        """, FIELD_AGREE)
        log(f"Agree checkbox: checked={checked}")
        return bool(checked)
    except Exception as e:
        log(f"click_agree verify error: {e}")
        return False


def click_submit(sb: SB) -> str:
    try:
        sb.slow_click(FIELD_SUBMIT)
        log("Submit clicked via slow_click")
        return "OK"
    except Exception:
        try:
            btn = sb.find_element(FIELD_SUBMIT, timeout=5)
            sb.driver.execute_script("arguments[0].click();", btn)
            log("Submit clicked via JS")
            return "OK"
        except Exception as e:
            log(f"click_submit error: {e}")
            return f"ERROR: {e}"


# ---------------------------------------------------------------------------
# Result detection
# ---------------------------------------------------------------------------

def detect_success(sb: SB) -> tuple[bool, str]:
    try:
        body_text = sb.get_text("body") or ""
        body_lower = body_text.lower()
    except Exception:
        body_text = ""
        body_lower = ""

    matched_ok = [p for p in SUCCESS_PHRASES if p.lower() in body_lower]

    matched_blocked = []
    for phrase1, phrase2 in FAIL_FORM_STILL_OPEN:
        if phrase1.lower() in body_lower:
            if phrase2 is None or phrase2.lower() in body_lower:
                matched_blocked.append(f"{phrase1} (+ {phrase2 or 'inscri'})")

    form_gone = not sb.is_element_present(FIELD_FIRST)

    log(
        f"detect_success: matched_ok={matched_ok}, "
        f"form_gone={form_gone}, blocked={matched_blocked}"
    )

    if matched_blocked:
        return False, f"ALREADY REGISTERED / BLOCKED: {matched_blocked}. Body: {body_text[:200]}"

    if len(matched_ok) >= 2 or (form_gone and len(matched_ok) >= 1):
        return True, f"SUCCESS — phrases={matched_ok}, form_gone={form_gone}"

    return False, (
        f"No success. ok={matched_ok}, form_gone={form_gone}, "
        f"body: {body_text[:200]}"
    )


def wait_for_result(sb: SB, timeout: int = 20) -> tuple[bool, str]:
    log(f"Waiting for result (timeout={timeout}s)…")

    for i in range(timeout):
        sb.sleep(1)

        if not driver_alive(sb):
            return False, "WebDriver session died after submit"

        try:
            blocked = sb.driver.execute_script("""
                var t = (document.body ? (document.body.innerText || document.body.textContent || '') : '').toLowerCase();
                return (
                    (t.indexOf('vous ne pouvez pas') !== -1 && t.indexOf('inscri') !== -1) ||
                    t.indexOf('already registered') !== -1 ||
                    (t.indexOf('déjà') !== -1 && t.indexOf('inscrit') !== -1)
                );
            """)
            if blocked:
                return False, "ALREADY REGISTERED / BLOCKED"
        except Exception:
            pass

        is_success, msg = detect_success(sb)
        if is_success:
            return True, msg

        log(f"  Poll {i + 1}/{timeout}: {msg[:100]}")

    return False, "Timeout — no confirmation detected"


# ---------------------------------------------------------------------------
# Main signup flow
# ---------------------------------------------------------------------------

def run_signup(data: dict, row: int) -> tuple[bool, str]:
    log(f"=== Starting signup row {row} — {data.get('email', 'N/A')} ===")

    with SB(
        browser="chrome",
        headless=True,
        locale="fr-FR",
        incognito=True,
    ) as sb:
        sb.open(FORM_URL)
        log(f"Page URL: {sb.get_current_url()}")
        log(f"Title: {sb.get_title()}")
        sb.sleep(3)

        save_debug_artifacts(sb, f"page_loaded_row_{row}")

        dismiss_cookies(sb)
        sb.sleep(1)

        ok, msg = wait_for_form_page(sb, row, timeout=25)
        if not ok:
            return False, msg

        fill_results = fill_form(sb, data)
        log(f"Fill results: {fill_results}")

        if not (
            fill_results.get("first_name")
            and fill_results.get("last_name")
            and fill_results.get("email")
            and fill_results.get("confirm")
        ):
            save_debug_artifacts(sb, f"error_fill_failed_row_{row}")
            return False, f"Fields not filled. results={fill_results}"

        if not click_agree(sb):
            save_debug_artifacts(sb, f"error_agree_row_{row}")
            return False, "Agree checkbox not checked"

        sb.sleep(0.5)

        submit_result = click_submit(sb)
        if "ERROR" in submit_result:
            save_debug_artifacts(sb, f"error_submit_row_{row}")
            return False, submit_result

        sb.sleep(2)

        is_success, result_msg = wait_for_result(sb, timeout=20)

        save_debug_artifacts(sb, f"result_row_{row}")

        return is_success, result_msg


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    log("=== Ticketmaster Signup Script Started ===")
    log(f"NEXT_ROW = {NEXT_ROW}")

    test_mode = not DATA_CSV_B64

    if test_mode:
        is_kevin = (NEXT_ROW % 2 == 1)
        if is_kevin:
            email = "kevinhubertus2002@gmail.com"
            first = "Kevin"
            last = "Hubertus"
        else:
            email = "cashews_jogger.2y@icloud.com"
            first = "Sophie"
            last = "Jogger"

        log(f"[TEST MODE] Using inline test data: {email}")
        row_data = {
            "first_name": first,
            "last_name": last,
            "email": email,
            "country": "France",
            "phone": "+33600000000",
            "newsletter": "yes",
            "terms": "yes",
        }
    else:
        try:
            row_data = parse_csv()
            log(f"Loaded row {NEXT_ROW}: {row_data.get('email', 'N/A')}")
        except Exception as e:
            log(f"CSV parse error: {e}")
            discord_notify("ERROR", f"CSV parse error: {e}", NEXT_ROW)
            return 1

    try:
        success, msg = run_signup(row_data, NEXT_ROW)
    except Exception as e:
        import traceback
        traceback.print_exc()
        success = False
        msg = f"EXCEPTION: {e}"

    if success:
        log(f">>> SUCCESS row {NEXT_ROW} <<<")
        if not test_mode:
            try:
                github_increment_row()
            except Exception as e:
                log(f"Row increment (non-fatal): {e}")

        discord_notify(
            "SUCCESS",
            f"Row {NEXT_ROW} ({row_data.get('email')}) signed up.\n{msg}",
            NEXT_ROW,
        )
    else:
        log(f">>> FAILURE row {NEXT_ROW}: {msg} <<<")
        discord_notify(
            "FAILURE",
            f"Row {NEXT_ROW} ({row_data.get('email')}) failed.\n{msg}",
            NEXT_ROW,
        )

    log("=== Done ===")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
