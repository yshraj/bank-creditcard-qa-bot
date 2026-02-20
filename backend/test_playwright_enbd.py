from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

URL = "https://www.emiratesnbd.com/en/cards/credit-cards"

REAL_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            slow_mo=100
        )

        context = browser.new_context(
            user_agent=REAL_UA,
            locale="en-US",
            timezone_id="Asia/Dubai",
            viewport={"width": 1920, "height": 1080}
        )

        page = context.new_page()

        print(f"Opening {URL} ...")
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)

        # Handle cookie consent if present
        try:
            print("Checking for cookie consent button...")
            accept_btn = page.get_by_role("button", name="Accept All")
            accept_btn.wait_for(timeout=8000)
            accept_btn.click()
            print("Cookie consent accepted.")
            page.wait_for_timeout(2000)
        except PlaywrightTimeoutError:
            print("No cookie consent button found.")

        # Wait for dynamic content (text length heuristic)
        try:
            page.wait_for_function(
                "document.body && document.body.innerText.length > 1500",
                timeout=30000
            )
            print("Main content loaded.")
        except PlaywrightTimeoutError:
            print("Timed out waiting for large body text.")

        # Scroll to trigger lazy loading
        for _ in range(3):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(4000)

        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(2000)

        # Extract text
        body_len = page.evaluate("() => document.body ? document.body.innerText.length : 0")
        text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""

        print(f"\nbody.innerText.length = {body_len}")
        print("\nPreview (first 500 chars):\n")
        print(text[:500])

        # Save HTML for inspection
        html = page.content()
        with open("enbd_credit_cards_debug.html", "w", encoding="utf-8") as f:
            f.write(html)

        print("\nSaved HTML to enbd_credit_cards_debug.html")

        input("\nPress Enter to close browser...")
        browser.close()


if __name__ == "__main__":
    main()
