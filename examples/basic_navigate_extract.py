from zero_cdp import Browser


def main():
    browser = Browser(port=9222)
    page = browser.connect()
    try:
        page.navigate("https://example.com")
        print(page.extract_text())
    finally:
        browser.close()


if __name__ == "__main__":
    main()
