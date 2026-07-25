import hashlib
import json
import os
import re
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright


URL = (
    "https://mpv.tickets.com/schedule/"
    "?agency=SETH_SNG_MPV&orgid=51529"
    "#/?view=list&includePackages=true"
)

STATE_FILE = Path("ticket_state.json")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")


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
                            "The page loaded but contained too little text."
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
        "Could not load the Tickets.com schedule after trying "
        f"Chromium and Firefox. Last error: {last_error}"
    )


def fingerprint(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_state():
    if not STATE_FILE.exists():
        return None

    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_state(text):
    STATE_FILE.write_text(
        json.dumps(
            {
                "fingerprint": fingerprint(text),
                "text": text,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def find_new_text(old_text, new_text):
    old_parts = {
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+|\n+", old_text)
        if len(part.strip()) >= 12
    }

    new_parts = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+|\n+", new_text)
        if len(part.strip()) >= 12
    ]

    return [part for part in new_parts if part not in old_parts]


def notify(message):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC has not been configured.")
        return

    response = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": "New DMV ticket date detected",
            "Priority": "high",
            "Tags": "ticket,calendar",
            "Click": URL,
        },
        timeout=30,
    )

    response.raise_for_status()


def main():
    print("Checking DMV ticket schedule...")

    current_text = get_page_text()
    current_hash = fingerprint(current_text)
    previous = load_state()

    if previous is None:
        save_state(current_text)
        print("First check completed. Initial schedule saved.")
        return

    if previous.get("fingerprint") == current_hash:
        print("No changes detected.")
        return

    additions = find_new_text(previous.get("text", ""), current_text)
    save_state(current_text)

    if additions:
        summary = "\n\n".join(additions[:10])

        notify(
            "New information appeared on the DMV ticket schedule:\n\n"
            + summary
            + "\n\nOpen the schedule: "
            + URL
        )

        print("New content detected. Notification sent.")
    else:
        print("The page changed, but no clear new date was identified.")


if __name__ == "__main__":
    main()
