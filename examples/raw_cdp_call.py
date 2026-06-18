from bare_cdp import Browser


def main():
    page = Browser(port=9222).connect()
    result = page.call("Runtime.evaluate", {
        "expression": "document.title",
        "returnByValue": True,
    })
    print(result["result"]["value"])
    page.close()


if __name__ == "__main__":
    main()
