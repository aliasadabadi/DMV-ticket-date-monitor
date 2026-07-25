import json
import os
import re
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright
import smtplib
from email.message import EmailMessage
from datetime import datetime

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
    "LOCKHEED MARTIN IMAX THEATER, WASHINGTON, DC",
]

def clean_text(text):
    return re.sub(r"\s+", " ", text).strip()
def get_schedule_data():
    """
    Download the complete structured schedule directly from
    the Tickets.com API.
    """

    headers = {
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
        ),
    }

    last_error = None

    for attempt in range(1, 4):
        try:
            print(
                f"Downloading schedule API, attempt "
                f"{attempt} of 3..."
            )

            response = requests.get(
                API_URL,
                headers=headers,
                timeout=90,
            )

            response.raise_for_status()

            data = response.json()

            events = (
                data
                .get("eventSchedule", {})
                .get("events", [])
            )

            if not isinstance(events, list):
                raise RuntimeError(
                    "The API events field was not a list."
                )

            if not events:
                raise RuntimeError(
                    "The API returned no schedule events."
                )

            print(
                f"Schedule API returned "
                f"{len(events)} total events."
            )

            return data

        except Exception as error:
            last_error = error

            print(
                f"Schedule API attempt {attempt} failed: "
                f"{error}"
            )

            if attempt < 3:
                import time
                time.sleep(5)

    raise RuntimeError(
        "Could not download the schedule API after "
        f"three attempts. Last error: {last_error}"
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
