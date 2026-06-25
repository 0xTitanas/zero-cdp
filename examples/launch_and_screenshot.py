from zero_cdp import Browser, launch_chrome, terminate_chrome


def main():
    launch = launch_chrome(headless=True)
    browser = Browser(port=launch.port)
    try:
        page = browser.connect()
        page.navigate("https://example.com")
        page.screenshot("example.png")
        print("wrote example.png")
    finally:
        browser.close()
        terminate_chrome(launch)


if __name__ == "__main__":
    main()
