from bare_cdp import Browser


def main():
    browser = Browser(port=9222)
    page = browser.connect()
    try:
        page.navigate("https://www.google.com/search?q=")
        page.wait_for_selector("textarea[name=q], input[name=q]")
        # Pick the first matching input from JavaScript if the exact selector differs.
        page.evaluate("""
        (function(){
          const el = document.querySelector('textarea[name=q]') || document.querySelector('input[name=q]');
          if (el) el.setAttribute('data-bare-cdp-target', 'q');
        })()
        """)
        page.input_text("[data-bare-cdp-target=q]", "Chrome DevTools Protocol", press_enter=True)
        print(page.extract_text())
    finally:
        browser.close()


if __name__ == "__main__":
    main()
