from bare_cdp import Browser, launch_chrome


def main():
    proc = launch_chrome(port=9222, headless=True)
    browser = Browser(port=9222)
    try:
        page = browser.connect()
        page.navigate("https://example.com")
        page.screenshot("example.png")
        print("wrote example.png")
    finally:
        browser.close()
        proc.terminate()


if __name__ == "__main__":
    main()
