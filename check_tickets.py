import json
import os
import re
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright
import smtplib
from email.message import EmailMessage

URL = (
    "https://mpv.tickets.com/schedule/"
    "?agency=SETH_SNG_MPV&orgid=51529"
    "#/?view=list&includePackages=true"
)

STATE_FILE = Path("ticket_state.json")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
EMAIL_USERNAME = os.environ.get("EMAIL_USERNAME", "")
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")

TARGET_MOVIE = "THE ODYSSEY"
TARGET_VENUE = "AIRBUS IMAX THEATER, CHANTILLY, VA"


def clean_text(text):
    return re.sub(r"\s+", " ", text).strip()


def get_page_text():
    last_error = None

    with sync_playwright() as playwright:
        browser_options = [
            (
                "Chromium",
                lambda: playwright.chromium.launch(
                    headless=True,
                    args=["--disable-http2"],
                ),
            ),
            (
                "Firefox",
                lambda: playwright.firefox.launch(headless=True),
            ),
        ]

        for browser_name, launch_browser in browser_options:
            browser = None

            try:
                print(f"Trying {browser_name}...")
                browser = launch_browser()

                page = browser.new_page(
                    viewport={"width": 1440, "height": 1600},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0 Safari/537.36"
                    ),
                )

                for attempt in range(1, 4):
                    try:
                        print(
                            f"{browser_name} navigation attempt "
                            f"{attempt} of 3..."
                        )

                        page.goto(
                            URL,
                            wait_until="domcontentloaded",
                            timeout=90000,
                        )

                        page.wait_for_timeout(15000)

                        text = clean_text(
                            page.locator("body").inner_text()
                        )

                        if len(text) >= 50:
                            print(
                                f"Schedule loaded successfully "
                                f"with {browser_name}."
                            )
                            return text

                        raise RuntimeError(
                            "Page loaded but contained too little text."
                        )

                    except Exception as error:
                        last_error = error
                        print(
                            f"{browser_name} attempt {attempt} failed: "
                            f"{error}"
                        )

                        if attempt < 3:
                            page.wait_for_timeout(5000)

            except Exception as error:
                last_error = error
                print(f"{browser_name} failed: {error}")

            finally:
                if browser is not None:
                    browser.close()

    raise RuntimeError(
        "Could not load the Tickets.com schedule. "
        f"Last error: {last_error}"
    )


def extract_target_showings(page_text):
    """
    Extract only The Odyssey showings at Airbus IMAX.

    Each showing is stored using date, time, and title as its unique key.
    """

    month_pattern = (
        r"JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"
    )

    pattern = re.compile(
        rf"(?P<date>(?:{month_pattern}) \d{{1,2}}) "
        rf"(?P<title>THE ODYSSEY"
        rf"(?: - OPEN CAPTION \(ON-SCREEN ENGLISH SUBTITLES\))?) "
        rf"(?P<weekday>MONDAY|TUESDAY|WEDNESDAY|THURSDAY|"
        rf"FRIDAY|SATURDAY|SUNDAY) "
        rf"\| (?P<time>\d{{1,2}}:\d{{2}}[AP]M "
        rf"(?:EDT|EST)) "
        rf"AIRBUS IMAX THEATER, CHANTILLY, VA"
        rf"(?P<following>.*?)"
        rf"(?=(?:{month_pattern}) \d{{1,2}} |\Z)",
        re.IGNORECASE,
    )

    showings = {}

    for match in pattern.finditer(page_text):
        date = match.group("date").upper()
        title = match.group("title").upper()
        weekday = match.group("weekday").upper()
        time = match.group("time").upper()
        following_text = match.group("following").lower()

        if "currently sold out" in following_text:
            status = "sold_out"
        elif "not currently on sale" in following_text:
            status = "not_on_sale"
        else:
            status = "available"

        key = f"{date}|{time}|{title}"

        showings[key] = {
            "date": date,
            "weekday": weekday,
            "time": time,
            "title": title,
            "venue": TARGET_VENUE,
            "status": status,
        }

    return showings


def load_state():
    if not STATE_FILE.exists():
        return None

    try:
        return json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
    except Exception as error:
        print(f"Could not read previous state: {error}")
        return None


def save_state(showings):
    state = {
        "movie": TARGET_MOVIE,
        "venue": TARGET_VENUE,
        "showings": showings,
    }

    STATE_FILE.write_text(
        json.dumps(
            state,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def format_showing(showing):
    title = showing["title"].title()

    return (
        f"{title}\n"
        f"{showing['weekday'].title()}, "
        f"{showing['date'].title()} at {showing['time']}\n"
        f"Airbus IMAX Theater, Chantilly"
    )


def send_notification(title, message):
    if not NTFY_TOPIC:
        raise RuntimeError(
            "NTFY_TOPIC is not configured in GitHub Secrets."
        )

    response = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": "max",
            "Tags": "ticket,movie_camera,rotating_light",
            "Click": URL,
        },
        timeout=30,
    )

    response.raise_for_status()

def send_email_notification(subject, message):
    if not all([
        EMAIL_USERNAME,
        EMAIL_APP_PASSWORD,
        EMAIL_TO,
    ]):
        print("Email secrets are not fully configured.")
        return

    email = EmailMessage()
    email["Subject"] = subject
    email["From"] = EMAIL_USERNAME
    email["To"] = EMAIL_TO
    email.set_content(message)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(
            EMAIL_USERNAME,
            EMAIL_APP_PASSWORD,
        )
        smtp.send_message(email)

    print("Email notification sent successfully.")
    
def main():
    print("Checking The Odyssey at Airbus IMAX...")

    page_text = get_page_text()
    current_showings = extract_target_showings(page_text)

    print(
        f"Found {len(current_showings)} Odyssey "
        "showings at Airbus IMAX."
    )

    for showing in current_showings.values():
        print(
            f"{showing['date']} {showing['time']} "
            f"- {showing['status']}"
        )

    if not current_showings:
        raise RuntimeError(
            "No Odyssey showings at Airbus IMAX were found. "
            "The website format may have changed."
        )

    previous_state = load_state()

    # First run after installing this targeted version establishes
    # a clean baseline and intentionally sends no notification.
    if previous_state is None or "showings" not in previous_state:
        save_state(current_showings)
        print(
            "Targeted baseline created. "
            "No notification sent on this run."
        )
        return

    previous_showings = previous_state.get("showings", {})
    alerts = []

    for key, current in current_showings.items():
        previous = previous_showings.get(key)

        # A newly added showing is useful only when it appears available.
        if previous is None and current["status"] == "available":
            alerts.append(
                "NEW SHOWING AVAILABLE\n"
                + format_showing(current)
            )
            continue

        # An existing unavailable showing has become purchasable.
        if (
            previous is not None
            and previous.get("status") in {
                "sold_out",
                "not_on_sale",
            }
            and current["status"] == "available"
        ):
            alerts.append(
                "TICKETS NOW AVAILABLE\n"
                + format_showing(current)
            )

    save_state(current_showings)

    if alerts:
        message = (
            "\n\n--------------------\n\n".join(alerts)
            + "\n\nOpen the ticket page immediately:\n"
            + URL
        )

        subject = "The Odyssey Airbus IMAX tickets!"

        try:
            send_notification(subject, message)
        except Exception as error:
            print(f"ntfy notification failed: {error}")

        try:
            send_email_notification(subject, message)
        except Exception as error:
            print(f"Email notification failed: {error}")

        print(f"Notification sent with {len(alerts)} alert(s).")
    else:
        print("No new available Odyssey showings detected.")


if __name__ == "__main__":
    main()
