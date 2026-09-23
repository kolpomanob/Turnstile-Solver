import os
import sys
import time
import uuid
import json
import random
import logging
import asyncio
import argparse
from quart import Quart, request, jsonify
from camoufox.async_api import AsyncCamoufox
from patchright.async_api import async_playwright

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
        self.useragent = useragent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        self.thread_count = thread
        self.proxy_support = proxy_support
        self.browser_pool = asyncio.Queue()
        
        # Anti-detection launch flags for containerized environments
        self.browser_args = [
            f"--user-agent={self.useragent}",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--use-gl=swiftshader",
            "--enable-webgl",
            "--window-size=1920,1080",
            "--disable-dev-shm-usage"
        ]

        self._setup_routes()

    @staticmethod
    def _load_results():
        """Load previous results from results.json."""
        try:
            if os.path.exists("results.json"):
                with open("results.json", "r") as f:
                    return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Error loading results: {str(e)}. Starting with empty dict.")
        return {}

    def _save_results(self):
        """Save results to results.json."""
        try:
            with open("results.json", "w") as result_file:
                json.dump(self.results, result_file, indent=4)
        except IOError as e:
            logger.error(f"Error saving results: {str(e)}")

    def _setup_routes(self) -> None:
        """Set up the application routes and auth check."""
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
                if request.path == "/turnstile":
                    logger.info(f"[AUTH] Request from user: {AUTH_TOKENS[token]}")

    async def _startup(self) -> None:
        """Initialize the browser pool on startup."""
        logger.info("Starting browser initialization")
        try:
            await self._initialize_browser()
        except Exception as e:
            logger.error(f"Failed to initialize browser: {str(e)}")
            raise

    async def _initialize_browser(self) -> None:
        """Initialize the browser instances."""
        if self.browser_type in ['chromium', 'chrome', 'msedge']:
            playwright = await async_playwright().start()
        elif self.browser_type == "camoufox":
            camoufox = AsyncCamoufox(headless=self.headless)

        for i in range(self.thread_count):
            if self.browser_type in ['chromium', 'chrome', 'msedge']:
                browser = await playwright.chromium.launch(
                    channel=self.browser_type if self.browser_type != 'chromium' else None,
                    headless=self.headless,
                    args=self.browser_args
                )
            elif self.browser_type == "camoufox":
                browser = await camoufox.start()

            await self.browser_pool.put((i + 1, browser))
            if self.debug:
                logger.success(f"Browser {i + 1} initialized successfully")

        logger.success(f"Browser pool initialized with {self.browser_pool.qsize()} browsers")

    async def _solve_turnstile(self, task_id: str, url: str, sitekey: str, action: str = None, cdata: str = None):
        """Solve the Turnstile challenge."""
        proxy = None
        index, browser = await self.browser_pool.get()

        try:
            if self.proxy_support:
                proxy_file_path = os.path.join(os.getcwd(), "proxies.txt")
                if os.path.exists(proxy_file_path):
                    with open(proxy_file_path) as proxy_file:
                        proxies = [line.strip() for line in proxy_file if line.strip()]
                    proxy = random.choice(proxies) if proxies else None

            # Setup Context
            context_kwargs = {
                "user_agent": self.useragent,
                "viewport": {"width": 1920, "height": 1080}
            }

            if proxy:
                clean_proxy = proxy.replace("http://", "").replace("https://", "")
                parts = clean_proxy.split(':')
                
                if len(parts) == 4:
                    # Format: host:port:username:password
                    p_host, p_port, p_user, p_pass = parts
                    context_kwargs["proxy"] = {
                        "server": f"http://{p_host}:{p_port}",
                        "username": p_user,
                        "password": p_pass
                    }
                elif len(parts) == 2:
                    # Format: host:port
                    p_host, p_port = parts
                    context_kwargs["proxy"] = {
                        "server": f"http://{p_host}:{p_port}"
                    }
                else:
                    context_kwargs["proxy"] = {"server": proxy}

            context = await browser.new_context(**context_kwargs)
            page = await context.new_page()
            start_time = time.time()

            if self.debug:
                logger.debug(f"Browser {index}: Task {task_id} solving for {url} | Proxy: {proxy}")

            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Check if Turnstile iframe appears natively
            try:
                await page.wait_for_selector("iframe[src*='challenges.cloudflare.com']", timeout=10000)
            except Exception:
                pass

            # Inject Turnstile API script
            await page.evaluate("""
                if (!document.querySelector('script[src*="challenges.cloudflare.com/turnstile"]')) {
                    const script = document.createElement('script');
                    script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
                    script.async = true;
                    document.head.appendChild(script);
                }
            """)

            # Inject widget DOM node
            action_str = f"div.setAttribute('data-action', '{action}');" if action else ""
            cdata_str = f"div.setAttribute('data-cdata', '{cdata}');" if cdata else ""
            inject_script = f"""
                const div = document.createElement('div');
                div.className = 'cf-turnstile';
                div.setAttribute('data-sitekey', '{sitekey}');
                {action_str}
                {cdata_str}
                document.body.appendChild(div);
            """
            await page.evaluate(inject_script)

            # Polling loop for response
            solved = False
            for attempt in range(20):
                try:
                    turnstile_check = await page.input_value("[name=cf-turnstile-response]", timeout=1500)
                    if turnstile_check and len(turnstile_check) > 20:
                        elapsed_time = round(time.time() - start_time, 3)
                        logger.success(f"Browser {index}: Solved -> {turnstile_check[:12]}... in {elapsed_time}s")
                        self.results[task_id] = {
                            "value": turnstile_check,
                            "elapsed_time": elapsed_time,
                            "userAgent": self.useragent
                        }
                        self._save_results()
                        solved = True
                        break
                    else:
                        # Try clicking directly inside the Turnstile iframe context
                        for frame in page.frames:
                            if "challenges.cloudflare.com" in frame.url:
                                try:
                                    await frame.click("input[type='checkbox']", timeout=1000)
                                except Exception:
                                    try:
                                        await frame.click("body", timeout=1000)
                                    except Exception:
                                        pass
                        await asyncio.sleep(1)
                except Exception:
                    await asyncio.sleep(0.5)

            if not solved:
                elapsed_time = round(time.time() - start_time, 3)
                self.results[task_id] = {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed_time}
                logger.error(f"Browser {index}: Solving failed or timed out after {elapsed_time}s")

            await context.close()

        except Exception as e:
            elapsed_time = round(time.time() - start_time, 3) if 'start_time' in locals() else 0
            self.results[task_id] = {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed_time}
            logger.error(f"Browser {index}: Error during execution: {str(e)}")

        finally:
            await self.browser_pool.put((index, browser))

    async def process_turnstile(self):
        """Handle the /turnstile endpoint requests."""
        url = request.args.get('url')
        sitekey = request.args.get('sitekey')
        action = request.args.get('action')
        cdata = request.args.get('cdata')

        if not url or not sitekey:
            return jsonify({
                "status": "error",
                "error": "Both 'url' and 'sitekey' are required"
            }), 400

        task_id = str(uuid.uuid4())
        self.results[task_id] = "CAPTCHA_NOT_READY"

        try:
            asyncio.create_task(self._solve_turnstile(task_id=task_id, url=url, sitekey=sitekey, action=action, cdata=cdata))
            if self.debug:
                logger.debug(f"Request enqueued with task_id {task_id}")
            return jsonify({"task_id": task_id}), 202
        except Exception as e:
            logger.error(f"Unexpected error processing request: {str(e)}")
            return jsonify({"status": "error", "error": str(e)}), 500

    async def get_result(self):
        """Return solved token or status."""
        task_id = request.args.get('id')

        if not task_id or task_id not in self.results:
            return jsonify({"status": "error", "error": "Invalid task ID/Request parameter"}), 400

        result = self.results[task_id]
        
        if result == "CAPTCHA_NOT_READY":
            return jsonify({"value": "CAPTCHA_NOT_READY"}), 200

        status_code = 200
        if isinstance(result, dict) and result.get("value") == "CAPTCHA_FAIL":
            status_code = 422

        return jsonify(result) if isinstance(result, dict) else jsonify({"value": result}), status_code

    @staticmethod
    async def index():
        """Serve API documentation page."""
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
                    <p class="mb-4 text-gray-300">Send a GET request to <code class="bg-red-700 text-white px-2 py-1 rounded">/turnstile</code> with parameters:</p>
                    <ul class="list-disc pl-6 mb-6 text-gray-300">
                        <li><strong>url</strong>: Page URL where Turnstile is embedded</li>
                        <li><strong>sitekey</strong>: Cloudflare site key</li>
                    </ul>
                </div>
            </body>
            </html>
        """

def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Turnstile API Server")
    parser.add_argument('--headless', type=bool, default=True, help='Run browser headless')
    parser.add_argument('--useragent', type=str, default=None, help='Custom User-Agent')
    parser.add_argument('--debug', type=bool, default=True, help='Enable debug logging')
    parser.add_argument('--browser_type', type=str, default='chromium', help='Browser type')
    parser.add_argument('--thread', type=int, default=1, help='Number of threads')
    parser.add_argument('--proxy', type=bool, default=True, help='Enable proxy support')
    parser.add_argument('--host', type=str, default='0.0.0.0', help='Host IP address')
    parser.add_argument('--port', type=str, default='8080', help='Server port')
    return parser.parse_args()

def create_app(headless: bool, useragent: str, debug: bool, browser_type: str, thread: int, proxy_support: bool) -> Quart:
    server = TurnstileAPIServer(headless=headless, useragent=useragent, debug=debug, browser_type=browser_type, thread=thread, proxy_support=proxy_support)
    return server.app

if __name__ == '__main__':
    args = parse_args()
    app = create_app(headless=args.headless, debug=args.debug, useragent=args.useragent, browser_type=args.browser_type, thread=args.thread, proxy_support=args.proxy)
    app.run(host=args.host, port=int(args.port))