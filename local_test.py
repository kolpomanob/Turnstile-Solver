import time
from patchright.sync_api import sync_playwright

# --- CONFIGURATION ---
TARGET_URL = "https://appointment.ivacbd.com/"  # Replace with your target URL
SITEKEY = "0x4AAAAAACghKkJHL1t7UkuZ"             # Replace with your sitekey
ACTION = None                                     # Replace if site uses data-action
CDATA = None                                      # Replace if site uses data-cdata
# ---------------------

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async></script>
</head>
<body>
    <!-- cf turnstile -->
</body>
</html>
"""

def get_token():
    with sync_playwright() as p:
        # Launch visible browser on local machine
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        # Build widget HTML
        turnstile_div = f'<div class="cf-turnstile" data-sitekey="{SITEKEY}"' + \
                        (f' data-action="{ACTION}"' if ACTION else '') + \
                        (f' data-cdata="{CDATA}"' if CDATA else '') + '></div>'
        page_data = HTML_TEMPLATE.replace("<!-- cf turnstile -->", turnstile_div)

        # Route setup
        url_with_slash = TARGET_URL + "/" if not TARGET_URL.endswith("/") else TARGET_URL
        page.route(url_with_slash, lambda route: route.fulfill(body=page_data, status=200))
        
        print(f"[*] Navigating to {TARGET_URL}...")
        page.goto(url_with_slash)

        # Get User-Agent string
        user_agent = page.evaluate("navigator.userAgent")

        print("[*] Waiting for Turnstile token...")
        token = None
        for _ in range(15):
            try:
                val = page.input_value("[name=cf-turnstile-response]")
                if val:
                    token = val
                    break
                page.click("//div[@class='cf-turnstile']", timeout=2000)
                time.sleep(1)
            except:
                time.sleep(1)

        browser.close()
        return token, user_agent

if __name__ == "__main__":
    token, user_agent = get_token()
    print("\n" + "="*60)
    if token:
        print(" SUCCESS! TOKEN GENERATED:")
        print(token)
        print("\n USER-AGENT USED BY BROWSER:")
        print(user_agent)
        print("\nIMPORTANT: Send this exact User-Agent header when making your API call!")
    else:
        print(" FAILED TO GET TOKEN")
    print("="*60)