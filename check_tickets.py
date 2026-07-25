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
TARGET_VENUES = [
    "AIRBUS IMAX THEATER, CHANTILLY, VA",
    "LOCKHEED MARTIN IMAX THEATER, WASHINGTON, DC",
]

def clean_text(text):
    return re.sub(r"\s+", " ", text).strip()
def get_page_text():
    last_error = None

    with sync_playwright() as playwright:
        browser = None

        try:
            print("Launching Firefox...")

            browser = playwright.firefox.launch(
                headless=True,
            )

            page = browser.new_page(
                viewport={
                    "width": 1440,
                    "height": 1600,
                },
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Firefox/128.0"
                ),
            )

            for attempt in range(1, 4):
                try:
                    print(
                        f"Firefox navigation attempt "
                        f"{attempt} of 3..."
                    )

                    # Do not wait for domcontentloaded because this site
                    # sometimes never reports it cleanly.
                    page.goto(
                        URL,
                        wait_until="commit",
                        timeout=60000,
                    )

                    # Wait until schedule content begins appearing.
                    page.wait_for_function(
                        """
                        () => {
                            const text =
                                document.body?.innerText || "";
                            return (
                                text.includes("EVENTS") &&
                                text.includes("RESULTS")
                            );
                        }
                        """,
                        timeout=60000,
                    )

                    page.wait_for_timeout(10000)

                    captured_batches = []
                    seen_batches = set()

                    max_clicks = 40

                    for click_number in range(max_clicks + 1):
                        current_raw_text = (
                            page.locator("body").inner_text()
                        )
                        current_text = clean_text(current_raw_text)

                        if current_text not in seen_batches:
                            seen_batches.add(current_text)
                            captured_batches.append(current_text)

                            print(
                                f"Captured batch "
                                f"{len(captured_batches)}: "
                                f"{len(current_text):,} characters"
                            )

                            print(
                                "  Contains August:",
                                "AUG " in current_text.upper(),
                            )

                        page.evaluate(
                            """
                            window.scrollTo(
                                0,
                                document.body.scrollHeight
                            )
                            """
                        )
                        page.wait_for_timeout(1500)

                        view_more = page.get_by_role(
                            "button",
                            name=re.compile(
                                r"VIEW\s+MORE\s+RESULTS",
                                re.IGNORECASE,
                            ),
                        )

                        # Fallback in case the site does not expose
                        # the control with a button role.
                        if view_more.count() == 0:
                            view_more = page.get_by_text(
                                re.compile(
                                    r"VIEW\s+MORE\s+RESULTS",
                                    re.IGNORECASE,
                                ),
                                exact=False,
                            )

                        if view_more.count() == 0:
                            print(
                                "No View More Results control remains."
                            )
                            break

                        button = view_more.last

                        try:
                            if not button.is_visible():
                                print(
                                    "View More Results is no longer visible."
                                )
                                break

                            before_click_text = clean_text(
                                page.locator("body").inner_text()
                            )

                            button.scroll_into_view_if_needed()
                            page.wait_for_timeout(1000)

                            button.click(
                                timeout=20000,
                                force=True,
                            )

                            print(
                                "Clicked View More Results "
                                f"{click_number + 1} time(s)."
                            )

                            # Wait for the content itself to change.
                            # It does not have to become longer.
                            try:
                                page.wait_for_function(
                                    """
                                    previousText => {
                                        const currentText =
                                            document.body?.innerText || "";
                                        return currentText !== previousText;
                                    }
                                    """,
                                    arg=before_click_text,
                                    timeout=30000,
                                )
                            except Exception:
                                print(
                                    "Page text did not change within "
                                    "30 seconds after the click."
                                )

                            # Give any later rendering time to finish.
                            page.wait_for_timeout(5000)

                        except Exception as click_error:
                            print(
                                "Could not load another batch: "
                                f"{click_error}"
                            )
                            break

                    # Combine all captured states. This works whether
                    # the site appends results or replaces old results.
                    combined_text = clean_text(
                        " ".join(captured_batches)
                    )

                    if len(combined_text) < 50:
                        raise RuntimeError(
                            "The schedule contained too little text."
                        )

                    print(
                        f"Combined captured text: "
                        f"{len(combined_text):,} characters."
                    )
                    print(
                        "August present in combined text:",
                        "AUG " in combined_text.upper(),
                    )
                    print(
                        "Odyssey present in combined text:",
                        "THE ODYSSEY"
                        in combined_text.upper(),
                    )

                    return combined_text

                except Exception as error:
                    last_error = error

                    print(
                        f"Firefox attempt {attempt} failed: "
                        f"{error}"
                    )

                    if attempt < 3:
                        page.wait_for_timeout(5000)

            raise RuntimeError(
                "Firefox could not load the schedule after "
                f"three attempts. Last error: {last_error}"
            )

        finally:
            if browser is not None:
                browser.close()

def extract_target_showings(page_text):
    """
    Extract The Odyssey showings at all target IMAX venues.

    Each showing is uniquely identified by:
    date + time + title + venue.
    """

    month_pattern = (
        r"JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"
    )

    venue_pattern = "|".join(
        re.escape(venue) for venue in TARGET_VENUES
    )

    pattern = re.compile(
        rf"(?P<date>(?:{month_pattern}) \d{{1,2}}) "
        rf"(?P<title>THE ODYSSEY"
        rf"(?: - OPEN CAPTION \(ON-SCREEN ENGLISH SUBTITLES\))?) "
        rf"(?P<weekday>MONDAY|TUESDAY|WEDNESDAY|THURSDAY|"
        rf"FRIDAY|SATURDAY|SUNDAY) "
        rf"\| (?P<time>\d{{1,2}}:\d{{2}}[AP]M "
        rf"(?:EDT|EST)) "
        rf"(?P<venue>{venue_pattern})"
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
        venue = match.group("venue").upper()
        following_text = match.group("following").lower()

        if "currently sold out" in following_text:
            status = "sold_out"
        elif "not currently on sale" in following_text:
            status = "not_on_sale"
        else:
            status = "available"

        # Venue is included because both theaters could have
        # the same movie at the same date and time.
        key = f"{date}|{time}|{title}|{venue}"

        showings[key] = {
            "date": date,
            "weekday": weekday,
            "time": time,
            "title": title,
            "venue": venue,
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
        "venues": TARGET_VENUES,
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
    venue = showing["venue"].title()

    return (
        f"{title}\n"
        f"{showing['weekday'].title()}, "
        f"{showing['date'].title()} at {showing['time']}\n"
        f"{venue}"
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
    print("Checking The Odyssey at Airbus and Lockheed IMAX...")
    page_text = get_page_text()
    current_showings = extract_target_showings(page_text)

    print(
        f"Found {len(current_showings)} Odyssey "
        "showings across the target IMAX venues."
    )

    for showing in current_showings.values():
        print(
            f"{showing['date']} {showing['time']} "
            f"- {showing['status']}"
        )

    if not current_showings:
        raise RuntimeError(
            "No Odyssey showings were found at either target venue. "
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
