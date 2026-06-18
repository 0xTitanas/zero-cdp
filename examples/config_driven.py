from bare_cdp import Browser


def main():
    browser = Browser.from_config("bare-cdp.example.json")
    page = browser.page()
    try:
        page.navigate("https://example.com")
        print(page.extract_text())
    finally:
        browser.close()


if __name__ == "__main__":
    main()
