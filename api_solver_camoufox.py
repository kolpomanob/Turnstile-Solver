import os
import sys
import time
import uuid
import json
import random
import logging
import asyncio
import argparse
import re
from quart import Quart, request, jsonify
from patchright.async_api import async_playwright
from camoufox.async_api import AsyncCamoufox

# --- Monkey-patch Browser.new_context to strip kwargs Patchright 1.49 rejects ---
# Newer Camoufox versions inject fingerprint options (e.g. 'contrast') that
# Patchright 1.49's Browser.new_context() doesn't accept, causing
# TypeError: Browser.new_context() got an unexpected keyword argument 'contrast'.
# This patch removes them at the source so any caller (Camoufox, Playwright,
# or our own code) is safe.
def _strip_unsupported_kwargs(original_method):
    async def _wrapper(self, *args, **kwargs):
        for bad in ("contrast",):
            kwargs.pop(bad, None)
        return await original_method(self, *args, **kwargs)
    return _wrapper


for _module_name in ("patchright._impl._browser", "playwright._impl._browser"):
    try:
        _mod = __import__(_module_name, fromlist=["Browser"])
        _cls = getattr(_mod, "Browser", None)
        if _cls is not None and hasattr(_cls, "new_context"):
            _cls.new_context = _strip_unsupported_kwargs(_cls.new_context)
    except Exception:
        pass
# --------------------------------------------------------------------------------

# Each user gets their own Railway variable prefixed with SOLVER_AUTH_
# e.g. SOLVER_AUTH_ALICE=abc123, SOLVER_AUTH_BOB=def456
AUTH_TOKEN_PREFIX = "SOLVER_AUTH_"

AUTH_TOKENS = {
    value.strip(): key[len(AUTH_TOKEN_PREFIX):].lower()
    for key, value in os.environ.items()
    if key.startswith(AUTH_TOKEN_PREFIX) and value.strip()
}


COLORS = {
    'MAGENTA': '\033[35m',
    'BLUE': '\033[34m',
    'GREEN': '\033[32m',
    'YELLOW': '\033[33m',
    'RED': '\033[31m',
    'RESET': '\033[0m',
}


class CustomLogger(logging.Logger):
    @staticmethod
    def format_message(level, color, message):
        timestamp = time.strftime('%H:%M:%S')
        return f"[{timestamp}] [{COLORS.get(color)}{level}{COLORS.get('RESET')}] -> {message}"

    def debug(self, message, *args, **kwargs):
        super().debug(self.format_message('DEBUG', 'MAGENTA', message), *args, **kwargs)

    def info(self, message, *args, **kwargs):
        super().info(self.format_message('INFO', 'BLUE', message), *args, **kwargs)

    def success(self, message, *args, **kwargs):
        super().info(self.format_message('SUCCESS', 'GREEN', message), *args, **kwargs)

    def warning(self, message, *args, **kwargs):
        super().warning(self.format_message('WARNING', 'YELLOW', message), *args, **kwargs)

    def error(self, message, *args, **kwargs):
        super().error(self.format_message('ERROR', 'RED', message), *args, **kwargs)


logging.setLoggerClass(CustomLogger)
logger = logging.getLogger("TurnstileAPIServer")
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler(sys.stdout)
logger.addHandler(handler)

MOCK_HTML_STUB = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>IVAC Bangladesh</title>
</head>
<body style="background: #ffffff; margin: 0; padding: 20px;">
</body>
</html>"""

class TurnstileAPIServer:
    HTML_TEMPLATE = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Turnstile Solver</title>
        <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async></script>
        <script>
            async function fetchIP() {
                try {
                    const response = await fetch('https://api64.ipify.org?format=json');
                    const data = await response.json();
                    document.getElementById('ip-display').innerText = `Your IP: ${data.ip}`;
                } catch (error) {
                    console.error('Error fetching IP:', error);
                    document.getElementById('ip-display').innerText = 'Failed to fetch IP';
                }
            }
            window.onload = fetchIP;
        </script>
    </head>
    <body>
        <!-- cf turnstile -->
        <p id="ip-display">Fetching your IP...</p>
    </body>
    </html>
    """

    def __init__(self, headless: bool, useragent: str, debug: bool, browser_type: str, thread: int, proxy_support: bool):
        self.app = Quart(__name__)

        self.debug = debug
        self.results = self._load_results()
        self.browser_type = browser_type
        self.headless = headless
        self.useragent = useragent
        self.thread_count = thread
        self.proxy_support = proxy_support
        self.browser_pool = asyncio.Queue()
        self.page_pool = asyncio.Queue()  # Persistent pre-warmed pages
        self.browser_args = []
        self.target_url = "https://appointment.ivacbd.com/"
        if useragent:
            self.browser_args.append(f"--user-agent={useragent}")

        self._setup_routes()

    async def _setup_route_interception(self, context) -> None:
        """Intercept requests to appointment.ivacbd.com and fulfill with local HTML stub."""
        async def _handle_route(route):
            req_url = route.request.url
            # Let Cloudflare challenge requests through — we only intercept the origin
            if "challenges.cloudflare.com" in req_url or "cloudflare.com" in req_url:
                await route.continue_()
            else:
                await route.fulfill(
                    status=200,
                    headers={"content-type": "text/html; charset=utf-8"},
                    body=MOCK_HTML_STUB
                )

        await context.route(re.compile(r"appointment\.ivacbd\.com"), _handle_route)

    @staticmethod
    def _load_results():
        """Load previous results from results.json."""
        try:
            if os.path.exists("results.json"):
                with open("results.json", "r") as f:
                    return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Error loading results: {str(e)}. Starting with an empty results dictionary.")
        return {}

    def _save_results(self):
        """Save results to results.json."""
        try:
            with open("results.json", "w") as result_file:
                json.dump(self.results, result_file, indent=4)
        except IOError as e:
            logger.error(f"Error saving results to file: {str(e)}")

    def _setup_routes(self) -> None:
        """Set up the application routes and optional auth check."""
        self.app.before_serving(self._startup)
        self.app.route('/turnstile', methods=['GET'])(self.process_turnstile)
        self.app.route('/result', methods=['GET'])(self.get_result)
        self.app.route('/')(self.index)

        @self.app.before_request
        async def _check_auth():
            if AUTH_TOKENS:
                token = request.headers.get("X-Solver-Token")
                if token not in AUTH_TOKENS:
                    return jsonify({"error": "unauthorized"}), 401
                # Only log /turnstile requests, not every /result poll
                if request.path == "/turnstile":
                    logger.info(f"[AUTH] Request from user: {AUTH_TOKENS[token]}")

    async def _startup(self) -> None:
        """Initialize the browser and page pool on startup."""
        logger.info("Starting browser initialization")
        try:
            await self._initialize_browser()
        except Exception as e:
            logger.error(f"Failed to initialize browser: {str(e)}")
            raise

    async def _initialize_browser(self) -> None:
        """Initialize the browser and create persistent pre-warmed pages."""
        if self.browser_type == "camoufox":
            for i in range(self.thread_count):
                browser = await AsyncCamoufox(headless=self.headless).start()
                await self.browser_pool.put((i + 1, browser))
                if self.debug:
                    logger.success(f"Camoufox browser {i + 1} initialized successfully")
            logger.success(f"Browser pool initialized with {self.browser_pool.qsize()} browsers")
            # Now create persistent pages for each browser
            await self._create_page_pool()
            return

        playwright = await async_playwright().start()

        for i in range(self.thread_count):
            browser = await playwright.chromium.launch(
                channel=self.browser_type,
                headless=self.headless,
                args=self.browser_args
            )

            await self.browser_pool.put((i + 1, browser))

            if self.debug:
                logger.success(f"Browser {i + 1} initialized successfully")

        logger.success(f"Browser pool initialized with {self.browser_pool.qsize()} browsers")
        # Now create persistent pages for each browser
        await self._create_page_pool()


    async def _create_page_pool(self) -> None:
        """Create persistent browser contexts with pre-warmed pages at target URL."""
        proxy_file_path = os.path.join(os.getcwd(), "proxies.txt")
        proxy_list = []
        if os.path.exists(proxy_file_path):
            with open(proxy_file_path) as proxy_file:
                proxy_list = [line.strip() for line in proxy_file if line.strip()]

        for i in range(self.thread_count):
            _, browser = await self.browser_pool.get()
            context = None
            proxy = None
            page = None

            if self.proxy_support and proxy_list:
                random.shuffle(proxy_list)
                for candidate in proxy_list:
                    try:
                        scheme = "http"
                        rest = candidate
                        if "://" in rest:
                            scheme, rest = rest.split("://", 1)
                        parts = rest.split(":")
                        ctx_kwargs = {}
                        if len(parts) == 2:
                            ip, port = parts
                            ctx_kwargs = {"proxy": {"server": f"{scheme}://{ip}:{port}"}}
                        elif len(parts) == 4:
                            ip, port, u, pw = parts
                            ctx_kwargs = {"proxy": {"server": f"{scheme}://{ip}:{port}", "username": u, "password": pw}}
                        else:
                            continue
                        context = await browser.new_context(**ctx_kwargs)
                        proxy = candidate
                        break
                    except Exception:
                        continue

            # Fallback: create context without proxy
            if context is None:
                context = await browser.new_context()

            # Set up route interception for Option B (local HTML stub)
            await self._setup_route_interception(context)

            page = await context.new_page()
            try:
                await page.goto(self.target_url, wait_until="domcontentloaded")
            except Exception as warmup_err:
                logger.warning(f"Pre-warm failed for browser {i + 1}: {warmup_err}")
                await browser.close()
                # Put browser back so we can try again with a different proxy
                await self.browser_pool.put((i + 1, browser))
                continue

            page_title = await page.title()
            blocked = any(k in page_title.lower() for k in ("cloudflare", "error", "attention"))
            if blocked:
                logger.warning(f"Pre-warm returned Cloudflare error for browser {i + 1}: {page_title!r} (proxy={proxy})")
                await browser.close()
                await self.browser_pool.put((i + 1, browser))
                continue

            await self.page_pool.put((i + 1, {"browser": browser, "context": context, "page": page, "proxy": proxy}))
            logger.success(f"Page pool created: browser {i + 1}, pre-warmed at {self.target_url} via proxy={proxy}")

        # Put back any leftover browsers from this loop that weren't consumed
        while not self.browser_pool.empty():
            await self.browser_pool.put(await self.browser_pool.get())
        logger.success(f"Page pool initialized with {self.page_pool.qsize()} pages")


    async def _solve_turnstile(self, task_id: str, url: str, sitekey: str, action: str = None, cdata: str = None, useragent: str = None):
        """Solve the Turnstile challenge using a pre-warmed persistent page."""
        # Camoufox manages its own fingerprint (Firefox engine + native UA).
        # Forcing a Chrome UA onto it creates an inconsistent fingerprint
        # (Chrome UA string on a Firefox/Gecko stack) that Cloudflare flags,
        # producing low-confidence tokens IVAC rejects. Let it use its own UA.
        # (UA match between solver and submitter is NOT required — Capsolver
        #  solves on a different UA/IP and is still accepted.)
        if self.browser_type == "camoufox":
            useragent = None

        # Get a pre-warmed page from the pool
        index, page_data = await self.page_pool.get()
        browser = page_data["browser"]
        context = page_data["context"]
        page = page_data["page"]
        current_proxy = page_data.get("proxy")

        start_time = time.time()

        try:
            if self.debug:
                logger.debug(f"Browser {index}: Starting Turnstile solve for URL: {url} with Sitekey: {sitekey}")

            # Show proxy usage at INFO level for verification
            if current_proxy:
                logger.info(f"Browser {index}: Using pre-warmed page via proxy {current_proxy}")
            else:
                logger.info(f"Browser {index}: Using pre-warmed page (no proxy)")

            # Fresh local navigation per solve (instant via route interception)
            # Ensures Turnstile initializes on a clean document lifecycle
            await page.goto(self.target_url, wait_until="domcontentloaded")

            # Remove existing widget if any and clean up old iframes
            await page.evaluate("""
                const oldWidgets = document.querySelectorAll('.cf-turnstile, iframe[src*="challenges.cloudflare.com"]');
                oldWidgets.forEach(w => w.remove());
            """)

            # Load Turnstile API script if not already present
            await page.evaluate("""
                if (!document.querySelector('script[src*="challenges.cloudflare.com/turnstile"]')) {
                    const script = document.createElement('script');
                    script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
                    script.async = true;
                    document.head.appendChild(script);
                }
            """)

            # Inject widget into page and trigger render if api.js is already loaded
            turnstile_div = f'<div id="cf-turnstile-container" class="cf-turnstile" style="background: white; position: fixed; top: 10px; left: 10px; z-index: 999999;" data-sitekey="{sitekey}"' + (f' data-action="{action}"' if action else '') + (f' data-cdata="{cdata}"' if cdata else '') + '></div>'
            await page.evaluate(f"""
                const wrapper = document.createElement('div');
                wrapper.innerHTML = `{turnstile_div}`;
                const el = wrapper.firstChild;
                document.body.appendChild(el);
                if (window.turnstile && typeof window.turnstile.render === 'function') {{
                    try {{
                        window.turnstile.render(el);
                    }} catch (e) {{}}
                }}
            """)

            # Wait for Turnstile to initialize
            await asyncio.sleep(1.5)

            if self.debug:
                logger.debug(f"Browser {index}: Widget injected, waiting for Turnstile to render")

            if self.debug:
                logger.debug(f"Browser {index}: Starting Turnstile response retrieval loop")

            for attempt in range(10):
                try:
                    # Check for Cloudflare block page (proxy IP got banned mid-session)
                    page_title = await page.title()
                    if any(k in page_title.lower() for k in ("cloudflare", "error", "attention")):
                        logger.warning(f"Browser {index}: Page hit Cloudflare block (proxy={current_proxy}), title={page_title!r}")
                        # Recycle this page with a fresh proxy
                        await self._recycle_page_with_new_proxy(index, page_data)
                        elapsed_time = round(time.time() - start_time, 3)
                        self.results[task_id] = {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed_time}
                        logger.error(f"Browser {index}: Recycled page due to Cloudflare block in {elapsed_time}s")
                        return

                    turnstile_check = await page.input_value("[name=cf-turnstile-response]", timeout=2000)
                    if turnstile_check == "":
                        logger.info(f"Browser {index}: Attempt {attempt} - No Turnstile response yet")

                        # Check if widget exists
                        widget_count = await page.locator("//div[@class='cf-turnstile']").count()
                        if widget_count == 0:
                            logger.warning(f"Browser {index}: No Turnstile widget found on page!")
                        else:
                            await page.locator("//div[@class='cf-turnstile']").click(timeout=1000)
                        await asyncio.sleep(0.3)
                    else:
                        elapsed_time = round(time.time() - start_time, 3)

                        logger.success(f"Browser {index}: Successfully solved captcha - {COLORS.get('MAGENTA')}{turnstile_check[:10]}{COLORS.get('RESET')} in {COLORS.get('GREEN')}{elapsed_time}{COLORS.get('RESET')} Seconds")

                        # Echo the UA the page actually reported so callers can
                        # verify the useragent passthrough reached the browser.
                        reported_ua = ""
                        try:
                            reported_ua = await page.evaluate("navigator.userAgent") or ""
                        except Exception:
                            reported_ua = ""

                        self.results[task_id] = {"value": turnstile_check, "elapsed_time": elapsed_time, "userAgent": reported_ua}
                        self._save_results()
                        break
                except Exception as loop_err:
                    logger.warning(f"Browser {index}: Attempt {attempt} error: {loop_err}")

            if self.results.get(task_id) == "CAPTCHA_NOT_READY":
                elapsed_time = round(time.time() - start_time, 3)
                self.results[task_id] = {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed_time}
                logger.error(f"Browser {index}: Turnstile timed out after 10 attempts in {COLORS.get('RED')}{elapsed_time}{COLORS.get('RESET')} Seconds — recycling proxy")
                await self._recycle_page_with_new_proxy(index, page_data)
                return
        except Exception as e:
            elapsed_time = round(time.time() - start_time, 3)
            self.results[task_id] = {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed_time}
            logger.error(f"Browser {index}: Error solving Turnstile: {str(e)}")
        finally:
            if self.debug:
                logger.debug(f"Browser {index}: Returning page to pool")

            # Return the page to the pool for reuse
            await self.page_pool.put((index, page_data))

    async def _recycle_page_with_new_proxy(self, index: int, page_data: dict):
        """Close current context/page and create a fresh one with a new proxy."""
        browser = page_data["browser"]
        context = page_data["context"]
        old_proxy = page_data.get("proxy")

        # Close old context
        try:
            await context.close()
        except Exception:
            pass

        # Load proxy list
        proxy_file_path = os.path.join(os.getcwd(), "proxies.txt")
        proxy_list = []
        if os.path.exists(proxy_file_path):
            with open(proxy_file_path) as proxy_file:
                proxy_list = [line.strip() for line in proxy_file if line.strip()]

        if not proxy_list:
            logger.error(f"Browser {index}: No proxies available for recycle, re-adding original page")
            await self.page_pool.put((index, page_data))
            return

        random.shuffle(proxy_list)
        new_context = None
        new_proxy = None

        for candidate in proxy_list:
            if candidate == old_proxy:
                continue  # Skip the blocked proxy
            try:
                scheme = "http"
                rest = candidate
                if "://" in rest:
                    scheme, rest = rest.split("://", 1)
                parts = rest.split(":")
                ctx_kwargs = {}
                if len(parts) == 2:
                    ip, port = parts
                    ctx_kwargs = {"proxy": {"server": f"{scheme}://{ip}:{port}"}}
                elif len(parts) == 4:
                    ip, port, u, pw = parts
                    ctx_kwargs = {"proxy": {"server": f"{scheme}://{ip}:{port}", "username": u, "password": pw}}
                else:
                    continue
                new_context = await browser.new_context(**ctx_kwargs)
                new_proxy = candidate
                break
            except Exception:
                continue

        if new_context is None:
            # Fallback to no proxy
            new_context = await browser.new_context()
            new_proxy = None

        # Set up route interception for Option B (local HTML stub)
        await self._setup_route_interception(new_context)

        new_page = await new_context.new_page()
        try:
            await new_page.goto(self.target_url, wait_until="domcontentloaded")
        except Exception as e:
            logger.error(f"Browser {index}: Recycle navigation failed: {e}")
            await new_context.close()
            await self.page_pool.put((index, page_data))
            return

        page_title = await new_page.title()
        blocked = any(k in page_title.lower() for k in ("cloudflare", "error", "attention"))
        if blocked:
            logger.warning(f"Browser {index}: Recycled page via proxy {new_proxy} still blocked: {page_title!r}")
            await new_context.close()
            await self.page_pool.put((index, page_data))
            return

        new_page_data = {"browser": browser, "context": new_context, "page": new_page, "proxy": new_proxy}
        await self.page_pool.put((index, new_page_data))
        logger.success(f"Browser {index}: Page recycled with new proxy={new_proxy}")

    async def process_turnstile(self):
        """Handle the /turnstile endpoint requests."""
        url = request.args.get('url')
        sitekey = request.args.get('sitekey')
        action = request.args.get('action')
        cdata = request.args.get('cdata')
        useragent = request.args.get('useragent')

        if not url or not sitekey:
            return jsonify({
                "status": "error",
                "error": "Both 'url' and 'sitekey' are required"
            }), 400

        task_id = str(uuid.uuid4())
        self.results[task_id] = "CAPTCHA_NOT_READY"

        try:
            asyncio.create_task(self._solve_turnstile(task_id=task_id, url=url, sitekey=sitekey, action=action, cdata=cdata, useragent=useragent))

            if self.debug:
                logger.debug(f"Request completed with taskid {task_id}.")
            return jsonify({"task_id": task_id}), 202
        except Exception as e:
            logger.error(f"Unexpected error processing request: {str(e)}")
            return jsonify({
                "status": "error",
                "error": str(e)
            }), 500

    async def get_result(self):
        """Return solved data"""
        task_id = request.args.get('id')

        if not task_id or task_id not in self.results:
            return jsonify({"status": "error", "error": "Invalid task ID/Request parameter"}), 400

        result = self.results[task_id]
        status_code = 200

        if "CAPTCHA_FAIL" in result:
            status_code = 422

        return result, status_code

    @staticmethod
    async def index():
        """Serve the API documentation page."""
        return """
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Turnstile Solver API</title>
                <script src="https://cdn.tailwindcss.com"></script>
            </head>
            <body class="bg-gray-900 text-gray-200 min-h-screen flex items-center justify-center">
                <div class="bg-gray-800 p-8 rounded-lg shadow-md max-w-2xl w-full border border-red-500">
                    <h1 class="text-3xl font-bold mb-6 text-center text-red-500">Welcome to Turnstile Solver API</h1>

                    <p class="mb-4 text-gray-300">To use the turnstile service, send a GET request to 
                       <code class="bg-red-700 text-white px-2 py-1 rounded">/turnstile</code> with the following query parameters:</p>

                    <ul class="list-disc pl-6 mb-6 text-gray-300">
                        <li><strong>url</strong>: The URL where Turnstile is to be validated</li>
                        <li><strong>sitekey</strong>: The site key for Turnstile</li>
                    </ul>

                    <div class="bg-gray-700 p-4 rounded-lg mb-6 border border-red-500">
                        <p class="font-semibold mb-2 text-red-400">Example usage:</p>
                        <code class="text-sm break-all text-red-300">/turnstile?url=https://example.com&sitekey=sitekey</code>
                    </div>

                    <div class="bg-red-900 border-l-4 border-red-600 p-4 mb-6">
                        <p class="text-red-200 font-semibold">This project is inspired by 
                           <a href="https://github.com/Body-Alhoha/turnaround" class="text-red-300 hover:underline">Turnaround</a> 
                           and is currently maintained by 
                           <a href="https://github.com/Theyka" class="text-red-300 hover:underline">Theyka</a> 
                           and <a href="https://github.com/sexfrance" class="text-red-300 hover:underline">Sexfrance</a>.</p>
                    </div>
                </div>
            </body>
            </html>
        """


def _str_to_bool(val):
    if isinstance(val, bool):
        return val
    return str(val).lower() in ("true", "1", "t", "yes", "y")


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Turnstile API Server")

    parser.add_argument('--headless', type=_str_to_bool, default=False, help='Run the browser in headless mode')
    parser.add_argument('--useragent', type=str, default=None, help='Specify a custom User-Agent string')
    parser.add_argument('--debug', type=_str_to_bool, default=False, help='Enable or disable debug mode')
    parser.add_argument('--browser_type', type=str, default='chromium', help='Browser type (chromium, chrome, msedge, camoufox)')
    parser.add_argument('--thread', type=int, default=1, help='Number of browser threads')
    parser.add_argument('--proxy', type=_str_to_bool, default=False, help='Enable proxy support')
    parser.add_argument('--host', type=str, default='127.0.0.1', help='Host IP address')
    parser.add_argument('--port', type=str, default='5000', help='Port number')
    return parser.parse_args()


def create_app(headless: bool, useragent: str, debug: bool, browser_type: str, thread: int, proxy_support: bool) -> Quart:
    server = TurnstileAPIServer(headless=headless, useragent=useragent, debug=debug, browser_type=browser_type, thread=thread, proxy_support=proxy_support)
    return server.app


if __name__ == '__main__':
    args = parse_args()
    browser_types = [
        'chromium',
        'chrome',
        'msedge',
        'camoufox',
    ]
    if args.browser_type not in browser_types:
        logger.error(f"Unknown browser type: {COLORS.get('RED')}{args.browser_type}{COLORS.get('RESET')} Available browser types: {browser_types}")
    elif args.headless is True and args.useragent is None and "camoufox" not in args.browser_type:
        logger.error(f"You must specify a {COLORS.get('YELLOW')}User-Agent{COLORS.get('RESET')} for Turnstile Solver or use {COLORS.get('GREEN')}camoufox{COLORS.get('RESET')} without useragent")
    else:
        app = create_app(headless=args.headless, debug=args.debug, useragent=args.useragent, browser_type=args.browser_type, thread=args.thread, proxy_support=args.proxy)
        app.run(host=args.host, port=int(args.port))