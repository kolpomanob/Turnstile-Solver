import os
import sys
import time
import uuid
import json
import random
import logging
import asyncio
import argparse
import re                                    # NEW
from quart import Quart, request, jsonify
from patchright.async_api import async_playwright

# Each user gets their own Railway variable prefixed with SOLVER_AUTH_
# e.g. SOLVER_AUTH_ALICE=abc123, SOLVER_AUTH_BOB=def456
AUTH_TOKEN_PREFIX = "SOLVER_AUTH_"

AUTH_TOKENS = {
    value.strip(): key[len(AUTH_TOKEN_PREFIX):].lower()
    for key, value in os.environ.items()
    if key.startswith(AUTH_TOKEN_PREFIX) and value.strip()
}

# NEW — per-request UA validation. Prevents the solver from becoming an open
# UA-spoofer that anyone can use to mint tokens with arbitrary fake UAs.
# Accepts normal desktop Chrome/Edge/Firefox/Safari UA strings only.
UA_RE = re.compile(
    r'^Mozilla/5\.0 \([^)]+\) AppleWebKit/[0-9.]+ \(KHTML, like Gecko\) '
    r'.*?(Chrome|Firefox|Safari|Edg)/[0-9.]+',
    re.IGNORECASE
)

def _looks_like_browser_ua(ua: str) -> bool:
    if not ua or len(ua) > 512:
        return False
    return bool(UA_RE.match(ua))


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
        self.useragent = useragent        # CHANGED — kept only as a fallback default
        self.thread_count = thread
        self.proxy_support = proxy_support
        self.browser_pool = asyncio.Queue()

        # CHANGED — do NOT pass --user-agent= as a launch arg.
        # It's unreliable in Playwright; the correct mechanism is
        # browser.new_context(user_agent=...) per request.
        self.browser_args = []

        self._setup_routes()

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
                # Log every authenticated request so we can see the ua param arriving
                logger.info(f"[AUTH] Request from user: {AUTH_TOKENS[token]} path={request.path}")

    async def _startup(self) -> None:
        """Initialize the browser and page pool on startup."""
        logger.info("Starting browser initialization")
        try:
            await self._initialize_browser()
        except Exception as e:
            logger.error(f"Failed to initialize browser: {str(e)}")
            raise

    async def _initialize_browser(self) -> None:
        """Initialize the browser and create the page pool."""
        playwright = await async_playwright().start()

        for _ in range(self.thread_count):
            browser = await playwright.chromium.launch(
                channel=self.browser_type,
                headless=self.headless,
                args=self.browser_args
            )

            await self.browser_pool.put((_ + 1, browser))

            if self.debug:
                logger.success(f"Browser {_ + 1} initialized successfully")

        logger.success(f"Browser pool initialized with {self.browser_pool.qsize()} browsers")


    async def _solve_turnstile(self, task_id: str, url: str, sitekey: str,
                               action: str = None, cdata: str = None,
                               requested_ua: str = None):
        """Solve the Turnstile challenge."""
        proxy = None

        index, browser = await self.browser_pool.get()

        if self.proxy_support:
            proxy_file_path = os.path.join(os.getcwd(), "proxies.txt")

            if not os.path.exists(proxy_file_path):
                raise FileNotFoundError(
                    f"proxies.txt not found at {proxy_file_path}. "
                    f"Remove it from .dockerignore and rebuild the image."
                )

            with open(proxy_file_path) as proxy_file:
                proxies = [line.strip() for line in proxy_file if line.strip()]

            proxy = random.choice(proxies) if proxies else None

            if proxy:
                scheme = "http"
                rest = proxy
                if "://" in rest:
                    scheme, rest = rest.split("://", 1)

                parts = rest.split(":")

                if len(parts) == 2:
                    ip, port = parts
                    context = await browser.new_context(
                        proxy={"server": f"{scheme}://{ip}:{port}"},
                        user_agent=requested_ua or self.useragent or None,
                        viewport={"width": 1280, "height": 720},
                        screen={"width": 1280, "height": 720},
                        locale="en-US",
                        timezone_id="Asia/Dhaka",
                        device_scale_factor=1,
                        is_mobile=False,
                        has_touch=False,
                        java_script_enabled=True,
                    )

                elif len(parts) == 4:
                    ip, port, user, pw = parts
                    context = await browser.new_context(
                        proxy={
                            "server": f"{scheme}://{ip}:{port}",
                            "username": user,
                            "password": pw,
                        },
                        user_agent=requested_ua or self.useragent or None,
                        viewport={"width": 1280, "height": 720},
                        screen={"width": 1280, "height": 720},
                        locale="en-US",
                        timezone_id="Asia/Dhaka",
                        device_scale_factor=1,
                        is_mobile=False,
                        has_touch=False,
                        java_script_enabled=True,
                    )

                else:
                    raise ValueError(
                        f"Invalid proxy format ({len(parts)} fields): {proxy!r}. "
                        f"Expected host:port or host:port:user:pass, "
                        f"optionally prefixed with scheme://"
                    )

                if self.debug:
                    logger.debug(
                        f"Browser {index}: proxy route = {scheme}://{parts[0]}:{parts[1]}"
                    )
            else:
                context = await browser.new_context(
                    user_agent=requested_ua or self.useragent or None,
                    viewport={"width": 1280, "height": 720},
                    screen={"width": 1280, "height": 720},
                    locale="en-US",
                    timezone_id="Asia/Dhaka",
                    device_scale_factor=1,
                    is_mobile=False,
                    has_touch=False,
                    java_script_enabled=True,
                )
        else:
            # CHANGED — this is the path that matters for UA matching.
            context = await browser.new_context(
                user_agent=requested_ua or self.useragent or None,
                viewport={"width": 1280, "height": 720},
                screen={"width": 1280, "height": 720},
                locale="en-US",
                timezone_id="Asia/Dhaka",
                device_scale_factor=1,
                is_mobile=False,
                has_touch=False,
                java_script_enabled=True,
            )

        # Effective UA actually used for this solve. Kept separate so we can
        # return it in the result and the userscript can verify.
        effective_ua = requested_ua or self.useragent or None
        ua_source = "request" if requested_ua else ("fallback" if self.useragent else "default")

        page = await context.new_page()

        start_time = time.time()

        try:
            if self.debug:
                logger.debug(
                    f"Browser {index}: Starting Turnstile solve for URL: {url} "
                    f"with Sitekey: {sitekey} | Proxy: {proxy} | "
                    f"UA source: {ua_source} | UA: {effective_ua}"
                )
                logger.debug(f"Browser {index}: Setting up page data and route")

            url_with_slash = url + "/" if not url.endswith("/") else url
            turnstile_div = f'<div class="cf-turnstile" style="background: white;" data-sitekey="{sitekey}"' + (f' data-action="{action}"' if action else '') + (f' data-cdata="{cdata}"' if cdata else '') + '></div>'
            page_data = self.HTML_TEMPLATE.replace("<!-- cf turnstile -->", turnstile_div)

            await page.route(url_with_slash, lambda route: route.fulfill(body=page_data, status=200))
            await page.goto(url_with_slash)

            if self.debug:
                logger.debug(f"Browser {index}: Setting up Turnstile widget dimensions")

            await page.eval_on_selector("//div[@class='cf-turnstile']", "el => el.style.width = '70px'")

            if self.debug:
                logger.debug(f"Browser {index}: Starting Turnstile response retrieval loop")

            for _ in range(10):
                try:
                    turnstile_check = await page.input_value("[name=cf-turnstile-response]", timeout=2000)
                    if turnstile_check == "":
                        if self.debug:
                            logger.debug(f"Browser {index}: Attempt {_} - No Turnstile response yet")

                        await page.locator("//div[@class='cf-turnstile']").click(timeout=1000)
                        await asyncio.sleep(0.5)
                    else:
                        elapsed_time = round(time.time() - start_time, 3)

                        logger.success(
                            f"Browser {index}: Successfully solved captcha - "
                            f"{COLORS.get('MAGENTA')}{turnstile_check[:10]}{COLORS.get('RESET')} "
                            f"in {COLORS.get('GREEN')}{elapsed_time}{COLORS.get('RESET')} Seconds"
                        )

                        # CHANGED — include userAgent + uaSource in the result dict
                        self.results[task_id] = {
                            "value": turnstile_check,
                            "elapsed_time": elapsed_time,
                            "userAgent": effective_ua,
                            "uaSource": ua_source,
                        }
                        self._save_results()
                        break
                except:
                    pass

            if self.results.get(task_id) == "CAPTCHA_NOT_READY":
                elapsed_time = round(time.time() - start_time, 3)
                self.results[task_id] = {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed_time}
                if self.debug:
                    logger.error(f"Browser {index}: Error solving Turnstile in {COLORS.get('RED')}{elapsed_time}{COLORS.get('RESET')} Seconds")
        except Exception as e:
            elapsed_time = round(time.time() - start_time, 3)
            self.results[task_id] = {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed_time}
            if self.debug:
                logger.error(f"Browser {index}: Error solving Turnstile: {str(e)}")
        finally:
            if self.debug:
                logger.debug(f"Browser {index}: Clearing page state")

            await context.close()
            await self.browser_pool.put((index, browser))

    async def process_turnstile(self):
        """Handle the /turnstile endpoint requests."""
        url = request.args.get('url')
        sitekey = request.args.get('sitekey')
        action = request.args.get('action')
        cdata = request.args.get('cdata')
        requested_ua = request.args.get('ua')       # NEW

        if not url or not sitekey:
            return jsonify({
                "status": "error",
                "error": "Both 'url' and 'sitekey' are required"
            }), 400

        # NEW — validate the requested UA. Reject obviously fake/garbage UAs so
        # this public solver can't be abused as an arbitrary UA spoofer.
        if requested_ua and not _looks_like_browser_ua(requested_ua):
            logger.warning(f"[UA] Rejected invalid UA from caller: {requested_ua[:100]!r}")
            return jsonify({
                "status": "error",
                "error": "Invalid 'ua' parameter. Must be a well-formed browser User-Agent string."
            }), 400

        task_id = str(uuid.uuid4())
        self.results[task_id] = "CAPTCHA_NOT_READY"

        try:
            asyncio.create_task(self._solve_turnstile(
                task_id=task_id,
                url=url,
                sitekey=sitekey,
                action=action,
                cdata=cdata,
                requested_ua=requested_ua,     # NEW
            ))

            if self.debug:
                logger.debug(
                    f"Request completed with taskid {task_id}. "
                    f"UA source: {'request' if requested_ua else 'fallback/default'}"
                )
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
                        <li><strong>ua</strong> (optional): Browser User-Agent string to use for the solve</li>
                    </ul>

                    <div class="bg-gray-700 p-4 rounded-lg mb-6 border border-red-500">
                        <p class="font-semibold mb-2 text-red-400">Example usage:</p>
                        <code class="text-sm break-all text-red-300">/turnstile?url=https://example.com&amp;sitekey=sitekey&amp;ua=Mozilla%2F5.0...</code>
                    </div>
                </div>
            </body>
            </html>
        """


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Turnstile API Server")

    parser.add_argument('--headless', type=bool, default=False, help='Run the browser in headless mode, without opening a graphical interface. This option requires the --useragent argument to be set (default: False)')
    parser.add_argument('--useragent', type=str, default=None, help='Fallback User-Agent if the caller does not supply a ua query param. If not provided, the browser default is used')
    parser.add_argument('--debug', type=bool, default=False, help='Enable or disable debug mode for additional logging and troubleshooting information (default: False)')
    parser.add_argument('--browser_type', type=str, default='chromium', help='Specify the browser type for the solver. Supported options: chromium, chrome, msedge, camoufox (default: chromium)')
    parser.add_argument('--thread', type=int, default=1, help='Set the number of browser threads to use for multi-threaded mode. Increasing this will speed up execution but requires more resources (default: 1)')
    parser.add_argument('--proxy', type=bool, default=False, help='Enable proxy support for the solver (Default: False)')
    parser.add_argument('--host', type=str, default='127.0.0.1', help='Specify the IP address where the API solver runs. (Default: 127.0.0.1)')
    parser.add_argument('--port', type=str, default='5000', help='Set the port for the API solver to listen on. (Default: 5000)')
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