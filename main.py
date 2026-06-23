"""
cron: 0 */6 * * *
new Env("Linux.Do 签到")
"""

import os
import random
import time
import functools
from loguru import logger
from DrissionPage import ChromiumOptions, Chromium
from tabulate import tabulate
from curl_cffi import requests
from bs4 import BeautifulSoup
from notify import NotificationManager


def retry_decorator(retries=3, min_delay=5, max_delay=10):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == retries - 1:  # 最后一次尝试
                        logger.error(f"函数 {func.__name__} 最终执行失败: {str(e)}")
                    logger.warning(
                        f"函数 {func.__name__} 第 {attempt + 1}/{retries} 次尝试失败: {str(e)}"
                    )
                    if attempt < retries - 1:
                        sleep_s = random.uniform(min_delay, max_delay)
                        logger.info(
                            f"将在 {sleep_s:.2f}s 后重试 ({min_delay}-{max_delay}s 随机延迟)"
                        )
                        time.sleep(sleep_s)
            return None

        return wrapper

    return decorator


os.environ.pop("DISPLAY", None)
os.environ.pop("DYLD_LIBRARY_PATH", None)

USERNAME = os.environ.get("LINUXDO_USERNAME")
PASSWORD = os.environ.get("LINUXDO_PASSWORD")
COOKIES = os.environ.get("LINUXDO_COOKIES", "").strip()  # 手动设置的 Cookie 字符串，优先使用
BROWSE_ENABLED = os.environ.get("BROWSE_ENABLED", "true").strip().lower() not in [
    "false",
    "0",
    "off",
]
if not USERNAME:
    USERNAME = os.environ.get("USERNAME")
if not PASSWORD:
    PASSWORD = os.environ.get("PASSWORD")

HOME_URL = "https://linux.do/"
LOGIN_URL = "https://linux.do/login"
SESSION_URL = "https://linux.do/session"
CSRF_URL = "https://linux.do/session/csrf"


class LinuxDoBrowser:
    def __init__(self) -> None:
        from sys import platform

        if platform == "linux" or platform == "linux2":
            platformIdentifier = "X11; Linux x86_64"
        elif platform == "darwin":
            platformIdentifier = "Macintosh; Intel Mac OS X 10_15_7"
        elif platform == "win32":
            platformIdentifier = "Windows NT 10.0; Win64; x64"
        else:
            platformIdentifier = "X11; Linux x86_64"

        co = (
            ChromiumOptions()
            .headless(True)
            .incognito(True)
            .set_argument("--no-sandbox")
        )
        co.set_user_agent(
            f"Mozilla/5.0 ({platformIdentifier}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )
        self.browser = Chromium(co)
        self.page = self.browser.new_tab()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        # 初始化通知管理器
        self.notifier = NotificationManager()

    def _safe_ele(self, locator, timeout=1):
        """查找元素，找不到时返回 None，避免 DrissionPage 直接抛异常中断流程。"""
        try:
            return self.page.ele(locator, timeout=timeout)
        except Exception:
            return None

    def _page_html(self) -> str:
        try:
            return self.page.html or ""
        except Exception as e:
            logger.warning(f"读取页面 HTML 失败: {str(e)}")
            return ""

    def log_page_diagnostics(self, context: str):
        """输出当前页面的关键信息，用于定位登录、CF challenge 或页面结构变化。"""
        html = self._page_html()
        html_lower = html.lower()
        try:
            title = self.page.title
        except Exception:
            title = ""
        try:
            url = self.page.url
        except Exception:
            url = ""

        indicators = []
        if "just a moment" in html_lower or "cf-chl" in html_lower or "challenge-platform" in html_lower:
            indicators.append("Cloudflare challenge")
        if "login" in url.lower() or "/login" in html_lower:
            indicators.append("login page")
        if "id=\"list-area\"" in html_lower or "list-area" in html_lower:
            indicators.append("topic list present")
        if "id=\"current-user\"" in html_lower or "current-user" in html_lower:
            indicators.append("current user present")

        text_snippet = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)[:300]
        logger.warning(
            f"{context}: url={url}, title={title}, html_len={len(html)}, "
            f"indicators={', '.join(indicators) if indicators else 'none'}"
        )
        if text_snippet:
            logger.warning(f"{context}: 页面文本片段: {text_snippet}")

    def has_current_user(self) -> bool:
        if self._safe_ele("@id=current-user", timeout=1):
            return True
        html = self._page_html()
        return "id=\"current-user\"" in html or "current-user" in html

    def has_topic_list(self) -> bool:
        return self._safe_ele("@id=list-area", timeout=1) is not None

    def wait_home_ready(self, timeout=45) -> bool:
        """等待首页关键 DOM 或登录态出现，避免页面还没渲染完就开始找帖子。"""
        logger.info(f"等待 linux.do 首页加载完成，最多 {timeout}s...")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.has_current_user() or self.has_topic_list():
                return True

            html_lower = self._page_html().lower()
            if "just a moment" in html_lower or "cf-chl" in html_lower:
                logger.warning("检测到 Cloudflare challenge 页面，继续等待...")

            time.sleep(2)

        self.log_page_diagnostics("等待首页加载超时")
        return False

    @staticmethod
    def parse_cookie_string(cookie_str: str) -> list[dict]:
        """
        解析浏览器复制的 Cookie 字符串格式: "name1=value1; name2=value2"
        返回 DrissionPage 所需的 cookie 列表格式。
        """
        cookies = []
        for part in cookie_str.strip().split(";"):
            part = part.strip()
            if "=" in part:
                name, _, value = part.partition("=")
                cookies.append(
                    {
                        "name": name.strip(),
                        "value": value.strip(),
                        "domain": ".linux.do",
                        "path": "/",
                    }
                )
        return cookies

    def login_with_cookies(self, cookie_str: str) -> bool:
        """使用手动设置的 Cookie 直接登录，跳过账号密码流程"""
        logger.info("检测到手动 Cookie，尝试 Cookie 登录...")
        dp_cookies = self.parse_cookie_string(cookie_str)
        if not dp_cookies:
            logger.error("Cookie 解析失败或为空，无法使用 Cookie 登录")
            return False

        logger.info(f"成功解析 {len(dp_cookies)} 个 Cookie 条目")

        # 同步到 requests.Session，以便后续 API 请求（如 print_connect_info）使用
        for ck in dp_cookies:
            self.session.cookies.set(ck["name"], ck["value"], domain=".linux.do", path="/")

        # 同步到 DrissionPage
        self.page.set.cookies(dp_cookies)
        logger.info("Cookie 设置完成，导航至 linux.do...")
        self.page.get(HOME_URL)
        if not self.wait_home_ready():
            logger.error("Cookie 登录后首页未能加载到可浏览状态")
            return False

        # 验证登录状态
        if self.has_current_user():
            logger.info("Cookie 登录验证成功")
            return True

        # 有些页面结构变化会导致 current-user 不稳定，但只要主题列表可用，仍继续执行真实浏览任务。
        if self.has_topic_list():
            logger.warning("未确认 current-user，但主题列表已加载，将继续执行浏览任务")
            return True

        logger.error("Cookie 登录验证失败 (未找到 current-user)，Cookie 可能已过期")
        self.log_page_diagnostics("Cookie 登录失败")
        return False

    def login(self):
        logger.info("开始账号密码登录")
        # Step 1: Get CSRF Token
        logger.info("获取 CSRF token...")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": LOGIN_URL,
        }
        resp_csrf = self.session.get(CSRF_URL, headers=headers, impersonate="firefox135")
        if resp_csrf.status_code != 200:
            logger.error(f"获取 CSRF token 失败: {resp_csrf.status_code}")
            return False        
        csrf_data = resp_csrf.json()
        csrf_token = csrf_data.get("csrf")
        logger.info(f"CSRF Token obtained: {csrf_token[:10]}...")

        # Step 2: Login
        logger.info("正在登录...")
        headers.update(
            {
                "X-CSRF-Token": csrf_token,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": "https://linux.do",
            }
        )

        data = {
            "login": USERNAME,
            "password": PASSWORD,
            "second_factor_method": "1",
            "timezone": "Asia/Shanghai",
        }

        try:
            resp_login = self.session.post(
                SESSION_URL, data=data, impersonate="chrome136", headers=headers
            )

            if resp_login.status_code == 200:
                response_json = resp_login.json()
                if response_json.get("error"):
                    logger.error(f"登录失败: {response_json.get('error')}")
                    return False
                logger.info("登录成功!")
            else:
                logger.error(f"登录失败，状态码: {resp_login.status_code}")
                logger.error(resp_login.text)
                return False
        except Exception as e:
            logger.error(f"登录请求异常: {e}")
            return False

        # Step 3: Pass cookies to DrissionPage
        logger.info("同步 Cookie 到 DrissionPage...")

        cookies_dict = self.session.cookies.get_dict()

        dp_cookies = []
        for name, value in cookies_dict.items():
            dp_cookies.append(
                {
                    "name": name,
                    "value": value,
                    "domain": ".linux.do",
                    "path": "/",
                }
            )

        self.page.set.cookies(dp_cookies)

        logger.info("Cookie 设置完成，导航至 linux.do...")
        self.page.get(HOME_URL)

        if not self.wait_home_ready():
            logger.error("账号密码登录后首页未能加载到可浏览状态")
            return False

        if self.has_current_user():
            logger.info("登录验证成功")
            return True

        if self.has_topic_list():
            logger.warning("未确认 current-user，但主题列表已加载，将继续执行浏览任务")
            return True

        logger.error("登录验证失败 (未找到 current-user)")
        self.log_page_diagnostics("账号密码登录失败")
        return False

    def normalize_topic_url(self, href: str) -> str | None:
        if not href:
            return None
        href = href.strip()
        if href.startswith("http://") or href.startswith("https://"):
            return href
        if href.startswith("/"):
            return HOME_URL.rstrip("/") + href
        return None

    def collect_topic_urls_from_dom(self) -> list[str]:
        topic_urls = []
        list_area = self._safe_ele("@id=list-area", timeout=2)
        if list_area:
            try:
                topic_list = list_area.eles(".:title")
            except Exception as e:
                logger.warning(f"从 #list-area 读取主题失败: {str(e)}")
                topic_list = []

            for topic in topic_list:
                url = self.normalize_topic_url(topic.attr("href"))
                if url:
                    topic_urls.append(url)

        # DOM 结构变化时，用 HTML 兜底解析所有 /t/ 链接。
        if not topic_urls:
            soup = BeautifulSoup(self._page_html(), "html.parser")
            for link in soup.select('a[href^="/t/"], a[href*="linux.do/t/"]'):
                url = self.normalize_topic_url(link.get("href", ""))
                if url:
                    topic_urls.append(url)

        return list(dict.fromkeys(topic_urls))

    def collect_topic_urls_from_api(self) -> list[str]:
        """DOM 没有主题列表时，尝试用 Discourse latest.json 取主题，再用浏览器打开帖子。"""
        try:
            resp = self.session.get(
                HOME_URL.rstrip("/") + "/latest.json",
                headers={"Accept": "application/json"},
                impersonate="chrome136",
                timeout=20,
            )
        except Exception as e:
            logger.warning(f"请求 latest.json 异常: {str(e)}")
            return []

        if resp.status_code != 200:
            logger.warning(f"请求 latest.json 失败: HTTP {resp.status_code}")
            return []

        try:
            data = resp.json()
        except Exception as e:
            logger.warning(f"解析 latest.json 失败: {str(e)}")
            return []

        topic_urls = []
        for topic in data.get("topic_list", {}).get("topics", []):
            topic_id = topic.get("id")
            slug = topic.get("slug") or "topic"
            if topic_id:
                topic_urls.append(f"{HOME_URL.rstrip('/')}/t/{slug}/{topic_id}")
        return list(dict.fromkeys(topic_urls))

    def wait_topic_urls_from_dom(self, timeout=45) -> list[str]:
        """等待主题列表真实出现；登录头像先出现时，帖子列表可能仍在异步渲染。"""
        logger.info(f"等待主题列表加载完成，最多 {timeout}s...")
        deadline = time.time() + timeout
        while time.time() < deadline:
            topic_urls = self.collect_topic_urls_from_dom()
            if topic_urls:
                return topic_urls

            html_lower = self._page_html().lower()
            if "just a moment" in html_lower or "cf-chl" in html_lower:
                logger.warning("检测到 Cloudflare challenge 页面，继续等待主题列表...")

            time.sleep(2)

        return []

    def click_topic(self):
        if not self.wait_home_ready():
            logger.error("首页未加载完成，无法执行浏览任务")
            return False

        topic_urls = self.wait_topic_urls_from_dom()
        if not topic_urls:
            logger.warning("DOM 中未找到主题帖，尝试 latest.json 兜底获取主题")
            topic_urls = self.collect_topic_urls_from_api()

        if not topic_urls:
            logger.error("未找到主题帖")
            self.log_page_diagnostics("未找到主题帖")
            return False

        sample_count = min(10, len(topic_urls))
        logger.info(f"发现 {len(topic_urls)} 个主题帖，随机选择 {sample_count} 个")
        for topic_url in random.sample(topic_urls, sample_count):
            self.click_one_topic(topic_url)
        return True

    @retry_decorator()
    def click_one_topic(self, topic_url):
        new_page = self.browser.new_tab()
        try:
            new_page.get(topic_url)
            if random.random() < 0.3:  # 0.3 * 30 = 9
                self.click_like(new_page)
            self.browse_post(new_page)
        finally:
            try:
                new_page.close()
            except Exception:
                pass

    def browse_post(self, page):
        prev_url = None
        # 开始自动滚动，最多滚动10次
        for _ in range(10):
            # 随机滚动一段距离
            scroll_distance = random.randint(550, 650)  # 随机滚动 550-650 像素
            logger.info(f"向下滚动 {scroll_distance} 像素...")
            page.run_js(f"window.scrollBy(0, {scroll_distance})")
            logger.info(f"已加载页面: {page.url}")

            if random.random() < 0.03:  # 33 * 4 = 132
                logger.success("随机退出浏览")
                break

            # 检查是否到达页面底部
            at_bottom = page.run_js(
                "window.scrollY + window.innerHeight >= document.body.scrollHeight"
            )
            current_url = page.url
            if current_url != prev_url:
                prev_url = current_url
            elif at_bottom and prev_url == current_url:
                logger.success("已到达页面底部，退出浏览")
                break

            # 动态随机等待
            wait_time = random.uniform(2, 4)  # 随机等待 2-4 秒
            logger.info(f"等待 {wait_time:.2f} 秒...")
            time.sleep(wait_time)

    def run(self):
        try:
            # 优先使用手动 Cookie 登录，没有再使用账号密码
            if COOKIES:
                login_res = self.login_with_cookies(COOKIES)
                if not login_res:
                    logger.warning("Cookie 登录失败，尝试账号密码登录...")
                    login_res = self.login()
            else:
                login_res = self.login()
            if not login_res:  # 登录
                logger.warning("登录验证失败，程序终止")
                return

            if BROWSE_ENABLED:
                click_topic_res = self.click_topic()  # 点击主题
                if not click_topic_res:
                    logger.error("点击主题失败，程序终止")
                    return
                logger.info("完成浏览任务")
            self.print_connect_info()  # 打印连接信息
            self.send_notifications(BROWSE_ENABLED)  # 发送通知
        finally:
            try:
                self.page.close()
            except Exception:
                pass
            try:
                self.browser.quit()
            except Exception:
                pass

    def click_like(self, page):
        try:
            # 专门查找未点赞的按钮
            like_button = page.ele(".discourse-reactions-reaction-button")
            if like_button:
                logger.info("找到未点赞的帖子，准备点赞")
                like_button.click()
                logger.info("点赞成功")
                time.sleep(random.uniform(1, 2))
            else:
                logger.info("帖子可能已经点过赞了")
        except Exception as e:
            logger.error(f"点赞失败: {str(e)}")

    def print_connect_info(self):
        logger.info("获取连接信息")
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        }
        resp = self.session.get(
            "https://connect.linux.do/", headers=headers, impersonate="chrome136"
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("table tr")
        info = []

        for row in rows:
            cells = row.select("td")
            if len(cells) >= 3:
                project = cells[0].text.strip()
                current = cells[1].text.strip() if cells[1].text.strip() else "0"
                requirement = cells[2].text.strip() if cells[2].text.strip() else "0"
                info.append([project, current, requirement])

        logger.info("--------------Connect Info-----------------")
        logger.info("\n" + tabulate(info, headers=["项目", "当前", "要求"], tablefmt="pretty"))

    def send_notifications(self, browse_enabled):
        """发送签到通知"""
        status_msg = f"✅每日登录成功: {USERNAME}"
        if browse_enabled:
            status_msg += " + 浏览任务完成"
        
        # 使用通知管理器发送所有通知
        self.notifier.send_all("LINUX DO", status_msg)


if __name__ == "__main__":
    if not COOKIES and (not USERNAME or not PASSWORD):
        print("请设置 LINUXDO_COOKIES（Cookie 登录），或同时设置 USERNAME 和 PASSWORD（账号密码登录）")
        exit(1)
    browser = LinuxDoBrowser()
    browser.run()
