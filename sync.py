import base64
import csv
import io
import os
import sys
import time
import requests
from datetime import datetime

from seleniumbase import SB

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SIGNUP_URL = os.environ.get("SYNC_URL", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "OWNER/REPO")
PAT_TOKEN = os.environ.get("PAT_TOKEN", "")
NEXT_ROW = int(os.environ.get("NEXT_ROW", "1"))
DATA_CSV_B64 = os.environ.get("DATA_CSV_B64", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
GITHUB_API = "https://api.github.com"

# Form field IDs (confirmed by user)
FIELD_FIRST = "form_container_name_0"
FIELD_LAST = "form_container_name_1"
FIELD_EMAIL = "form_container_email_0"
FIELD_CONF = "form_container_email_1"
FIELD_PHONE = "form_container_phone"
FIELD_AGREE = "form_container_agree"
IFRAME_ID = "n2cFnX"

# Success phrases (must appear in iframe body after successful signup)
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

# Blocked phrases — email already registered, form stays open
# Must check with word boundaries to avoid false positives like "d'accéder"
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
            "timestamp": datetime.utcnow().isoformat() + "Z"
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
        "Accept": "application/vnd.github+json"
    }
    resp = requests.get(
        f"{GITHUB_API}/repos/{REPO}/actions/variables/NEXT_ROW",
        headers=headers, timeout=10
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
        timeout=10
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


def dump_iframes(sb: SB, context: str) -> None:
    """Debug helper: print all top-level iframes."""
    try:
        frames = sb.driver.execute_script("""
        return Array.from(document.querySelectorAll('iframe')).map((f, i) => ({
            index: i,
            id: f.id,
            name: f.name,
            src: f.src,
            title: f.title,
            loading: f.getAttribute('loading'),
            visible: !!(f.offsetWidth || f.offsetHeight || f.getClientRects().length)
        }));
        """)
        log(f"Iframes ({context}): {frames}")
    except Exception as e:
        log(f"dump_iframes error ({context}): {e}")


# ---------------------------------------------------------------------------
# Cookie banner — top-level page
# ---------------------------------------------------------------------------

def dismiss_cookies(sb: SB) -> None:
    """Click OneTrust reject-all button at top-level page."""
    script = (
        "var btn = document.querySelector('#onetrust-reject-all-handler');"
        "if (btn && btn.offsetParent !== null) { btn.click(); }"
    )
    try:
        sb.evaluate(script)
        sb.sleep(0.5)
        log("Cookie dismiss: ok")
    except Exception as e:
        log(f"Cookie dismiss error: {e}")


# ---------------------------------------------------------------------------
# Iframe handling
# ---------------------------------------------------------------------------

def wait_for_iframe_ready(sb: SB, iframe_id: str, timeout: int = 20) -> bool:
    """
    Wait until the iframe exists in DOM and its contentDocument.readyState
    is complete or interactive.
    """
    log(f"Waiting for iframe #{iframe_id} to exist in DOM…")
    end = time.time() + timeout

    while time.time() < end:
        try:
            exists = sb.driver.execute_script(
                f"return !!document.querySelector('#{iframe_id}');"
            )
            if exists:
                log(f"Iframe #{iframe_id} exists in DOM")
                break
        except Exception:
            pass
        sb.sleep(0.5)
    else:
        return False

    log(f"Waiting for iframe #{iframe_id} contentDocument to be ready…")
    end = time.time() + timeout
    last_state = None

    while time.time() < end:
        try:
            state = sb.driver.execute_script(f"""
                var f = document.querySelector('#{iframe_id}');
                if (!f) return "missing";
                try {{
                    if (!f.contentWindow || !f.contentDocument) return "no-document";
                    return f.contentDocument.readyState || "unknown";
                }} catch(e) {{
                    return "exception:" + e.message;
                }}
            """)
            if state != last_state:
                log(f"Iframe readyState: {state}")
                last_state = state

            if state in ("interactive", "complete"):
                return True
        except Exception as e:
            log(f"wait_for_iframe_ready poll error: {e}")

        sb.sleep(0.5)

    return False


def switch_to_signup_iframe(sb: SB, row: int) -> tuple[bool, str]:
    """
    Robustly switch into the signup iframe using WebElement-based switching.
    This avoids flaky index-based switching in UC headless mode.
    """
    dump_iframes(sb, "before iframe prep")

    # Try to disable lazy loading to reduce flakiness in CI
    try:
        sb.driver.execute_script(f"""
            var f = document.querySelector('#{IFRAME_ID}');
            if (f) {{
                f.loading = 'eager';
                f.scrollIntoView({{block: 'center'}});
            }}
        """)
        log(f"Set iframe #{IFRAME_ID} loading='eager' and scrolled into view")
    except Exception as e:
        log(f"Could not tweak iframe loading behavior: {e}")

    if not wait_for_iframe_ready(sb, IFRAME_ID, timeout=20):
        dump_iframes(sb, "iframe not ready")
        sb.save_screenshot(f"error_no_iframe_row_{row}.png")
        return False, f"Iframe #{IFRAME_ID} not ready"

    try:
        iframe_el = sb.find_element(f"#{IFRAME_ID}", timeout=10)
        sb.driver.switch_to.frame(iframe_el)
        log(f"Switched to iframe #{IFRAME_ID} via WebElement")
        return True, "OK"
    except Exception as e:
        sb.save_screenshot(f"error_no_iframe_row_{row}.png")
        return False, f"Could not switch to iframe #{IFRAME_ID}: {e}"


# ---------------------------------------------------------------------------
# Form filling — inside iframe context
# ---------------------------------------------------------------------------

def fill_form(sb: SB, data: dict) -> dict:
    """
    Fill all text fields inside the iframe using WebDriver send_keys.
    IMPORTANT: Find each element once, use it immediately, store value
    via element.get_attribute() — avoid stale element references.
    """
    results = {}
    fn = data.get("first_name", "").strip()
    ln = data.get("last_name", "").strip()
    email = data.get("email", "").strip()
    phone = data.get("phone", "").strip()
    country = data.get("country", "").strip()

    def fill_field(selector: str, value: str, name: str) -> bool:
        try:
            el = sb.find_element(selector, timeout=5)
            el.clear()
            el.send_keys(value)
            sb.sleep(0.15)
            stored_value = el.get_attribute("value")
            log(f"Filled {name}: '{stored_value}'")
            return bool(stored_value)
        except Exception as e:
            log(f"fill_field error ({name} / {selector}): {e}")
            return False

    results["first_name"] = fill_field(f"#{FIELD_FIRST}", fn, "first_name")
    results["last_name"] = fill_field(f"#{FIELD_LAST}", ln, "last_name")
    results["email"] = fill_field(f"#{FIELD_EMAIL}", email, "email")
    results["confirm"] = fill_field(f"#{FIELD_CONF}", email, "confirm_email")

    if country:
        try:
            sb.select_option_by_text("select", country)
            sb.sleep(0.2)
            log(f"Selected country: {country}")
            results["country"] = True
        except Exception as e:
            log(f"country select error: {e}")
            results["country"] = False

    if phone:
        results["phone"] = fill_field(f"#{FIELD_PHONE}", phone, "phone")

    return results


def click_agree(sb: SB) -> bool:
    """
    The checkbox #form_container_agree has display:none.
    Its <label for="form_container_agree"> is visible — click that instead.
    slow_click handles CDP scroll + click (works inside iframe context).
    Verification uses sb.driver.execute_script.
    """
    try:
        sb.slow_click("label[for='form_container_agree']")
        sb.sleep(0.3)
        sb.driver.execute_script(
            "var el = document.querySelector('#form_container_agree'); "
            "if (el) { window._agree_checked = el.checked; } "
            "else { window._agree_checked = null; }"
        )
        checked = sb.driver.execute_script("return window._agree_checked;")
        log(f"Agree checkbox: checked={checked}")
        return bool(checked)
    except Exception as e:
        log(f"click_agree error: {e}")
        return False


def click_submit(sb: SB) -> str:
    """
    Click the S'INSCRIRE / SIGN UP button.
    slow_click handles CDP scroll-into-view + click.
    """
    try:
        sb.slow_click("button.form_submit")
        log("Submit clicked via slow_click")
        return "OK"
    except Exception as e:
        log(f"click_submit error: {e}")
        return f"ERROR: {e}"


# ---------------------------------------------------------------------------
# Success detection
# ---------------------------------------------------------------------------

def detect_success(sb: SB) -> tuple[bool, str]:
    """
    Detect signup success inside the iframe.
    """
    try:
        body_el = sb.find_element("body")
        body_text = body_el.text or ""
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

    form_gone = False
    try:
        sb.find_element(f"#{FIELD_FIRST}", timeout=1)
        form_gone = False
    except Exception:
        form_gone = True

    is_success = len(matched_ok) >= 2 or (form_gone and len(matched_ok) >= 1)
    fail_blocked = len(matched_blocked) > 0

    log(f"detect_success: matched_ok={matched_ok}, form_gone={form_gone}, blocked={matched_blocked}")

    if fail_blocked:
        return False, f"ALREADY REGISTERED / BLOCKED: {matched_blocked}. Body: {body_text[:200]}"
    if is_success:
        return True, f"SUCCESS — phrases={matched_ok}, form_gone={form_gone}"
    return False, (
        f"No success. ok={matched_ok}, form_gone={form_gone}, "
        f"body: {body_text[:200]}"
    )


def wait_for_result(sb: SB, timeout: int = 15) -> tuple[bool, str]:
    """Poll for success/failure inside the iframe."""
    log(f"Waiting for result (timeout={timeout}s)…")
    for i in range(timeout):
        sb.sleep(1)
        try:
            sb.driver.execute_script(
                "var t = (document.body ? (document.body.innerText||document.body.textContent||'') : '').toLowerCase();"
                "window._blocked = (t.indexOf('vous ne pouvez pas') !== -1 && t.indexOf('inscri') !== -1) || "
                "                  t.indexOf('already registered') !== -1 || "
                "                  (t.indexOf('déjà') !== -1 && t.indexOf('inscrit') !== -1);"
            )
            blocked = sb.driver.execute_script("return window._blocked;")
            if blocked:
                return False, "ALREADY REGISTERED / BLOCKED"
        except Exception:
            pass

        is_success, msg = detect_success(sb)
        if is_success:
            return True, msg
        log(f"  Poll {i+1}/{timeout}: {msg[:80]}")
    return False, "Timeout — no confirmation detected"


# ---------------------------------------------------------------------------
# Main signup flow
# ---------------------------------------------------------------------------

def run_signup(data: dict, row: int) -> tuple[bool, str]:
    log(f"=== Starting signup row {row} — {data.get('email', 'N/A')} ===")

    with SB(
        uc=True,
        test=True,
        locale="fr-FR",
        browser="chrome",
        headless=True,
    ) as sb:
        # ── Open page ─────────────────────────────────────────────────────
        sb.open(SIGNUP_URL)
        log(f"Page URL: {sb.get_current_url()}")
        sb.sleep(6)

        dump_iframes(sb, "after page load")

        # ── Dismiss top-level cookie banner ─────────────────────────────
        dismiss_cookies(sb)
        sb.sleep(1.5)

        dump_iframes(sb, "after cookie dismiss")

        # ── Switch into signup iframe ───────────────────────────────────
        ok, msg = switch_to_signup_iframe(sb, row)
        if not ok:
            return False, msg

        # ── Wait for dynamically-loaded form fields to appear ───────────
        log("Waiting for form fields to load…")
        try:
            sb.find_element(f"#{FIELD_FIRST}", timeout=15)
            sb.sleep(0.5)
            log("Form fields are present in DOM")
        except Exception:
            sb.save_screenshot(f"error_form_fields_row_{row}.png")
            return False, "Form fields did not appear in iframe"

        # ── Fill form ───────────────────────────────────────────────────
        fill_results = fill_form(sb, data)
        log(f"Fill results: {fill_results}")

        fn_ok = fill_results.get("first_name", False)
        em_ok = fill_results.get("email", False)
        cf_ok = fill_results.get("confirm", False)

        if not fn_ok or not em_ok or not cf_ok:
            sb.save_screenshot(f"error_fill_failed_row_{row}.png")
            return False, f"Fields not filled. results={fill_results}"

        sb.sleep(0.3)

        # ── Click agree checkbox (via visible label) ────────────────────
        agree_ok = click_agree(sb)
        if not agree_ok:
            sb.save_screenshot(f"error_agree_row_{row}.png")
            return False, "Agree checkbox not checked"

        sb.sleep(0.3)

        # ── Submit ──────────────────────────────────────────────────────
        submit_result = click_submit(sb)
        if "ERROR" in str(submit_result):
            sb.save_screenshot(f"error_submit_row_{row}.png")

        sb.sleep(2)

        # ── Wait for result ─────────────────────────────────────────────
        is_success, result_msg = wait_for_result(sb, timeout=15)

        # ── Screenshot ──────────────────────────────────────────────────
        ss_name = f"result_row_{row}.png"
        try:
            sb.save_screenshot(ss_name)
            log(f"Screenshot saved: {ss_name}")
        except Exception as e:
            log(f"Screenshot failed: {e}")

        # ── Return to main page ────────────────────────────────────────
        try:
            sb.switch_to_default_content()
            log("Returned to default content.")
        except Exception as e:
            log(f"switch_to_default_content (non-fatal): {e}")

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
            NEXT_ROW
        )
    else:
        log(f">>> FAILURE row {NEXT_ROW}: {msg} <<<")
        discord_notify(
            "FAILURE",
            f"Row {NEXT_ROW} ({row_data.get('email')}) failed.\n{msg}",
            NEXT_ROW
        )

    log("=== Done ===")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
