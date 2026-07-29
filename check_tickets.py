import json
import os
import re
from pathlib import Path

import requests
import smtplib
from email.message import EmailMessage
from datetime import datetime
from playwright.sync_api import sync_playwright

URL = (
    "https://mpv.tickets.com/schedule/"
    "?agency=SETH_SNG_MPV&orgid=51529"
    "#/?view=list&includePackages=true"
)
API_URL = (
    "https://mpv.tickets.com/api/pvodc/v1/eventschedule/"
    "?orgId=51529&agency=SETH_SNG_MPV"
)
STATE_FILE = Path("ticket_state.json")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
EMAIL_USERNAME = os.environ.get("EMAIL_USERNAME", "")
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")

TARGET_MOVIE = "THE ODYSSEY"
TARGET_VENUES = [
    "AIRBUS IMAX THEATER, CHANTILLY, VA",
    "LOCKHEED IMAX THEATER, WASHINGTON, DC",
]

TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN",
    "",
)

TELEGRAM_CHAT_IDS = os.environ.get(
    "TELEGRAM_CHAT_IDS",
    "",
)
def send_telegram_notification(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        print("Telegram secrets are not fully configured.")
        return

    chat_ids = [
        chat_id.strip()
        for chat_id in TELEGRAM_CHAT_IDS.split(",")
        if chat_id.strip()
    ]

    api_url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    for chat_id in chat_ids:
        response = requests.post(
            api_url,
            json={
                "chat_id": chat_id,
                "text": message,
                "disable_web_page_preview": False,
            },
            timeout=30,
        )

        response.raise_for_status()

    print(
        f"Telegram alert sent to "
        f"{len(chat_ids)} chat(s)."
    )
    
def clean_text(text):
    return re.sub(r"\s+", " ", text).strip()
def get_schedule_data():
    """
    Open the Tickets.com page in Firefox and capture the complete
    structured eventschedule API response.
    """

    last_error = None

    with sync_playwright() as playwright:
        for attempt in range(1, 4):
            browser = None

            try:
                print(
                    f"Loading schedule through Firefox, "
                    f"attempt {attempt} of 3..."
                )

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
                        "AppleWebKit/537.36 "
                        "Firefox/128.0"
                    ),
                )

                with page.expect_response(
                    lambda response: (
                        "/api/pvodc/v1/eventschedule/" in response.url
                        and response.status == 200
                    ),
                    timeout=120000,
                ) as response_info:
                    page.goto(
                        URL,
                        wait_until="commit",
                        timeout=60000,
                    )

                response = response_info.value

                print("Schedule API response received:")
                print(response.url)

                # Playwright parses the JSON response directly.
                schedule_data = response.json()

                events = (
                    schedule_data
                    .get("eventSchedule", {})
                    .get("events", [])
                )

                if not isinstance(events, list):
                    raise RuntimeError(
                        "The schedule API events field is not a list."
                    )

                if not events:
                    raise RuntimeError(
                        "The schedule API returned no events."
                    )

                print(
                    f"Schedule API returned "
                    f"{len(events)} total events."
                )

                browser.close()
                return schedule_data

            except Exception as error:
                last_error = error

                print(
                    f"Firefox API attempt {attempt} failed: "
                    f"{error}"
                )

                if browser is not None:
                    browser.close()

                if attempt < 3:
                    import time
                    time.sleep(5)

    raise RuntimeError(
        "Could not capture the Tickets.com schedule API "
        f"after three attempts. Last error: {last_error}"
    )

def extract_target_showings(schedule_data):
    """
    Extract all The Odyssey performances at the selected venues
    directly from the structured Tickets.com API response.
    """

    events = (
        schedule_data
        .get("eventSchedule", {})
        .get("events", [])
    )

    target_venues_upper = {
        venue.upper() for venue in TARGET_VENUES
    }

    showings = {}

    for event in events:
        title = str(
            event.get("description", "")
        ).strip()

        venue = str(
            event.get("venueDescription", "")
        ).strip()

        title_upper = title.upper()
        venue_upper = venue.upper()

        # Includes ordinary and open-caption Odyssey listings.
        if not title_upper.startswith(TARGET_MOVIE):
            continue

        if venue_upper not in target_venues_upper:
            continue

        date_details = event.get("dateDetails") or {}
        date_time_text = date_details.get("dateTime")

        if not date_time_text:
            print(
                "Skipping an Odyssey event with no dateTime:",
                event.get("id"),
            )
            continue

        try:
            event_datetime = datetime.fromisoformat(
                date_time_text
            )
        except ValueError:
            print(
                "Skipping an event with an invalid dateTime:",
                date_time_text,
            )
            continue

        timezone_abbreviation = (
            date_details.get("timeZoneAbbreviation")
            or ""
        ).upper()

        date_display = event_datetime.strftime(
            "%b %d"
        ).upper()

        weekday = event_datetime.strftime(
            "%A"
        ).upper()

        # %-I does not work on Windows, but GitHub runs Linux.
        # This alternative works everywhere.
        hour = event_datetime.strftime("%I").lstrip("0")
        minute = event_datetime.strftime("%M")
        am_pm = event_datetime.strftime("%p")

        time_display = f"{hour}:{minute}{am_pm}"

        if timezone_abbreviation:
            time_display += f" {timezone_abbreviation}"

        sold_out = bool(
            event.get("soldoutFlag", False)
        )

        on_sale = bool(
            event.get("onsaleFlag", False)
        )

        if sold_out:
            status = "sold_out"
        elif not on_sale:
            status = "not_on_sale"
        else:
            status = "available"

        # The API performance ID is the most reliable unique key.
        performance_id = str(
            event.get("id", "")
        ).strip()

        if performance_id:
            key = performance_id
        else:
            key = (
                f"{date_time_text}|"
                f"{title_upper}|"
                f"{venue_upper}"
            )

        showings[key] = {
            "performance_id": performance_id,
            "date": date_display,
            "date_time": date_time_text,
            "weekday": weekday,
            "time": time_display,
            "title": title_upper,
            "venue": venue_upper,
            "status": status,
            "onsale": on_sale,
            "sold_out": sold_out,
            "event_id": str(
                event.get("eventId", "")
            ),
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
    venue_names = {
        "AIRBUS IMAX THEATER, CHANTILLY, VA":
            "Airbus IMAX Theater, Chantilly, VA",

        "LOCKHEED IMAX THEATER, WASHINGTON, DC":
            "Lockheed IMAX Theater, Washington, DC",
    }

    venue = venue_names.get(
        showing["venue"],
        showing["venue"],
    )

    return (
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

    recipients = [
        address.strip()
        for address in EMAIL_TO.split(",")
        if address.strip()
    ]

    if not recipients:
        print("No valid email recipients were configured.")
        return

    email = EmailMessage()
    email["Subject"] = subject
    email["From"] = EMAIL_USERNAME
    email["To"] = ", ".join(recipients)
    email.set_content(message)

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
    ) as smtp:
        smtp.login(
            EMAIL_USERNAME,
            EMAIL_APP_PASSWORD,
        )
        smtp.send_message(
            email,
            to_addrs=recipients,
        )

    print(
        f"Email notification sent to "
        f"{len(recipients)} recipient(s)."
    )
    
def main():
    print(
        "Checking The Odyssey at Airbus "
        "and Lockheed IMAX..."
    )

    schedule_data = get_schedule_data()

    current_showings = extract_target_showings(
        schedule_data
    )

    print(
        f"Found {len(current_showings)} Odyssey "
        "showings across the target IMAX venues."
    )

    for showing in sorted(
        current_showings.values(),
        key=lambda item: item["date_time"],
    ):
        print(
            f"{showing['date']} "
            f"{showing['time']} "
            f"- {showing['venue']} "
            f"- {showing['status']}"
        )

    if not current_showings:
        raise RuntimeError(
            "No Odyssey showings were found at either "
            "target venue. The API format may have changed."
        )

    previous_state = load_state()

    # First run creates the baseline and sends no alert.
    if previous_state is None or "showings" not in previous_state:
        save_state(current_showings)

        print(
            "Targeted baseline created. "
            "No notification sent on this run."
        )
        return

    previous_showings = previous_state.get(
        "showings",
        {},
    )

    new_showing_alerts = []
    renewed_availability_alerts = []

    for key, current in current_showings.items():
        previous = previous_showings.get(key)

        # A brand-new performance was added and tickets are available.
        if (
            previous is None
            and current["status"] == "available"
        ):
            new_showing_alerts.append(current)
            continue

        # A performance already existed, but changed from
        # unavailable/sold out to available.
        if (
            previous is not None
            and previous.get("status") in {
                "sold_out",
                "not_on_sale",
            }
            and current["status"] == "available"
        ):
            renewed_availability_alerts.append(current)

    # Always save the latest state.
    save_state(current_showings)

    def build_email_message(intro, showings):
        showings.sort(
            key=lambda showing: showing["date_time"]
        )

        lines = [
            intro,
            "",
            f"{len(showings)} showing(s):",
            "",
        ]

        for index, showing in enumerate(showings, start=1):
            lines.extend(
                [
                    f"{index}. {format_showing(showing)}",
                    "",
                ]
            )

        lines.extend(
            [
                "Open the ticket page:",
                URL,
            ]
        )

        return "\n".join(lines)

    if new_showing_alerts:
        subject = (
            "The Odyssey: new showings added"
            if len(new_showing_alerts) > 1
            else "The Odyssey: new showing added"
        )

        message = build_email_message(
            "New Odyssey showings have been added "
            "and tickets are available.",
            new_showing_alerts,
        )

        try:
            send_email_notification(subject, message)
        except Exception as error:
            print(f"New-showing email failed: {error}")

        print(
            f"Sent new-showing email for "
            f"{len(new_showing_alerts)} showing(s)."
        )

    if renewed_availability_alerts:
        subject = (
            "The Odyssey: tickets available again"
        )

        message = build_email_message(
            "Tickets have become available again for "
            "existing Odyssey showings that were previously "
            "sold out or unavailable.",
            renewed_availability_alerts,
        )

        try:
            send_email_notification(subject, message)
        except Exception as error:
            print(
                f"Renewed-availability email failed: {error}"
            )

        print(
            f"Sent renewed-availability email for "
            f"{len(renewed_availability_alerts)} showing(s)."
        )

    if (
        not new_showing_alerts
        and not renewed_availability_alerts
    ):
        print(
            "No new showings or renewed availability detected."
        )


if __name__ == "__main__":
    main()
