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
        try:
            if os.path.exists("results.json"):
                with open("results.json", "r") as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"Error loading results: {str(e)}.")
        return {}

    def _save_results(self):
        try:
            with open("results.json", "w") as result_file:
                json.dump(self.results, result_file, indent=4)
        except Exception as e:
            logger.error(f"Error saving results: {str(e)}")

    def _setup_routes(self) -> None:
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
        logger.info("Starting browser initialization")
        try:
            await self._initialize_browser()
        except Exception as e:
            logger.error(f"Failed to initialize browser: {str(e)}")
            raise

    async def _initialize_browser(self) -> None:
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
        proxy_str = None
        index, browser = await self.browser_pool.get()
        start_time = time.time()

        try:
            if self.proxy_support:
                proxy_file_path = os.path.join(os.getcwd(), "proxies.txt")
                if os.path.exists(proxy_file_path):
                    with open(proxy_file_path) as proxy_file:
                        proxies = [line.strip() for line in proxy_file if line.strip()]
                    proxy_str = random.choice(proxies) if proxies else None

            context_kwargs = {
                "user_agent": self.useragent,
                "viewport": {"width": 1920, "height": 1080},
                "ignore_https_errors": True
            }

            if proxy_str:
                clean_proxy = proxy_str.replace("http://", "").replace("https://", "")
                parts = clean_proxy.split(':')

                if len(parts) == 4:
                    p_host, p_port, p_user, p_pass = parts
                    context_kwargs["proxy"] = {
                        "server": f"http://{p_host}:{p_port}",
                        "username": p_user,
                        "password": p_pass
                    }
                    if self.debug:
                        logger.info(f"Browser {index}: Formatted Auth Proxy -> http://{p_host}:{p_port} | User: {p_user}")
                elif len(parts) == 2:
                    p_host, p_port = parts
                    context_kwargs["proxy"] = {
                        "server": f"http://{p_host}:{p_port}"
                    }
                else:
                    context_kwargs["proxy"] = {"server": proxy_str}

            context = await browser.new_context(**context_kwargs)
            page = await context.new_page()

            if self.debug:
                logger.debug(f"Browser {index}: Task {task_id} navigating to {url}")

            # Ensure page actually loads DOM before proceeding
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            except Exception as nav_err:
                if self.debug:
                    logger.warning(f"Browser {index}: Page navigation notice: {nav_err}")

            # Wait briefly to confirm DOM context is ready
            await asyncio.sleep(2)

            # Inject Turnstile script
            await page.evaluate("""
                if (!document.querySelector('script[src*="challenges.cloudflare.com/turnstile"]')) {
                    const script = document.createElement('script');
                    script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
                    script.async = true;
                    document.head.appendChild(script);
                }
            """)

            # Inject widget node
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

            if self.debug:
                logger.debug(f"Browser {index}: Widget injected. Starting solve loop.")

            solved = False
            for attempt in range(30):
                try:
                    # Retrieve Turnstile token value
                    turnstile_check = await page.evaluate("() => { const el = document.querySelector('[name=cf-turnstile-response]'); return el ? el.value : ''; }")
                    if turnstile_check and len(turnstile_check) > 20:
                        elapsed_time = round(time.time() - start_time, 3)
                        logger.success(f"Browser {index}: Solved Token -> {turnstile_check[:12]}... in {elapsed_time}s")
                        self.results[task_id] = {
                            "value": turnstile_check,
                            "elapsed_time": elapsed_time,
                            "userAgent": self.useragent
                        }
                        self._save_results()
                        solved = True
                        break

                    # Click Turnstile iframe target if available
                    for frame in page.frames:
                        if "challenges.cloudflare.com" in frame.url:
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
            elapsed_time = round(time.time() - start_time, 3)
            self.results[task_id] = {"value": "CAPTCHA_FAIL", "elapsed_time": elapsed_time}
            logger.error(f"Browser {index}: Error during execution: {str(e)}")

        finally:
            await self.browser_pool.put((index, browser))

    async def process_turnstile(self):
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
        return "Turnstile Solver API"

def parse_args():
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