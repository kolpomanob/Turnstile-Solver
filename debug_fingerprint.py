import time
from camoufox.sync_api import Camoufox

SITEKEY = "0x4AAAAAACghKkJHL1t7UkuZ"
TARGET_URL = "https://appointment.ivacbd.com/"

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

FINGERPRINT_JS = """
() => {
    let gl = null, vendor = "", renderer = "";
    try {
        const c = document.createElement('canvas');
        const ctx = c.getContext('webgl') || c.getContext('experimental-webgl');
        if (ctx) {
            const dbg = ctx.getExtension('WEBGL_debug_renderer_info');
            vendor   = dbg ? ctx.getParameter(dbg.UNMASKED_VENDOR_WEBGL)   : '';
            renderer = dbg ? ctx.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : '';
            gl = { vendor, renderer };
        }
    } catch(e) { gl = { error: String(e) } }

    return {
        userAgent: navigator.userAgent,
        platform: navigator.platform,
        language: navigator.language,
        languages: navigator.languages,
        hardwareConcurrency: navigator.hardwareConcurrency,
        deviceMemory: navigator.deviceMemory,
        maxTouch: navigator.maxTouch,
        doNotTrack: navigator.doNotTrack,
        webdriver: navigator.webdriver,
        plugins: Array.from(navigator.plugins).map(p => p.name),
        mimeTypes: Array.from(navigator.mimeTypes).map(m => m.type),
        screen: { w: screen.width, h: screen.height, colorDepth: screen.colorDepth },
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        webgl: gl,
    };
}
"""

def main():
    with Camoufox(headless=False) as browser:
        context = browser.new_context()
        page = context.new_page()

        print("=== FINGERPRINT (BEFORE navigation) ===")
        fp = page.evaluate(FINGERPRINT_JS)
        for k, v in fp.items():
            print(f"  {k}: {v}")

        turnstile_div = f'<div class="cf-turnstile" style="background:white;" data-sitekey="{SITEKEY}"></div>'
        page_data = HTML_TEMPLATE.replace("<!-- cf turnstile -->", turnstile_div)
        url_with_slash = TARGET_URL if TARGET_URL.endswith("/") else TARGET_URL + "/"

        # Capture network traffic to challenges.cloudflare.com
        cf_requests = []
        def on_request(req):
            if "cloudflare" in req.url or "challenges" in req.url:
                cf_requests.append(req.url)
        page.on("request", on_request)

        page.route(url_with_slash, lambda route: route.fulfill(body=page_data, status=200))

        print("\n=== NAVIGATING ===")
        page.goto(url_with_slash)

        print("\n=== CF-RELATED REQUESTS ===")
        for r in cf_requests:
            print("  ", r)

        print("\n=== WAITING FOR TOKEN ===")
        token = None
        for i in range(20):
            try:
                val = page.input_value("[name=cf-turnstile-response]", timeout=2000)
                if val:
                    token = val
                    print(f"  Token obtained on attempt {i+1}")
                    break
                page.click("//div[@class='cf-turnstile']", timeout=2000)
                time.sleep(1)
            except Exception as e:
                time.sleep(1)

        print("\n=== FINGERPRINT (AFTER navigation) ===")
        fp2 = page.evaluate(FINGERPRINT_JS)
        for k, v in fp2.items():
            print(f"  {k}: {v}")

        context.close()
        print("\n=== TOKEN ===")
        print(token if token else "FAILED")

if __name__ == "__main__":
    main()