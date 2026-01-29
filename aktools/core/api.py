# -*- coding:utf-8 -*-
# /usr/bin/env python
"""
Date: 2024/1/12 22:05
Desc: HTTP 模式主文件
"""

import json
import logging
import os
import random
import threading
import time
import urllib.parse
from collections import deque
from logging.handlers import TimedRotatingFileHandler

import requests
from cachetools import TTLCache
from cachetools.keys import hashkey

import akshare as ak
from fastapi import APIRouter
from fastapi import Depends, status
from fastapi import Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from aktools.datasets import get_pyscript_html, get_template_path
from aktools.login.user_login import User, get_current_active_user

app_core = APIRouter()

# ==============================================================================
# 反抓取策略配置
# ==============================================================================

# 使用 fake-useragent 库获取 Chrome 浏览器的 User-Agent
# 在模块加载时初始化一次，之后一直使用同一个 User-Agent，避免频繁更换触发反爬检测
try:
    from fake_useragent import UserAgent

    _ua = UserAgent()
    # 固定使用 Chrome 浏览器的 User-Agent
    CHROME_USER_AGENT = _ua.chrome
except Exception:
    # 如果 fake-useragent 不可用，使用备用的 Chrome User-Agent
    CHROME_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# 默认浏览器 headers
DEFAULT_BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}


def get_browser_headers() -> dict:
    """
    获取浏览器 headers

    使用固定的 Chrome User-Agent，不随机更换，以避免触发反爬检测
    """
    headers = DEFAULT_BROWSER_HEADERS.copy()
    headers["User-Agent"] = CHROME_USER_AGENT
    return headers


class RateLimiter:
    """
    基于滑动窗口的限流器

    在指定的时间窗口内限制请求次数。如果超过限制，会自动等待直到可以发送新请求。
    """

    def __init__(self, max_requests: int = 10, window_seconds: int = 60):
        """
        初始化限流器

        :param max_requests: 时间窗口内允许的最大请求数
        :param window_seconds: 时间窗口大小（秒）
        """
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.request_timestamps: deque = deque()
        self._lock = threading.Lock()

    def _clean_old_requests(self, current_time: float):
        """清理过期的请求时间戳"""
        cutoff_time = current_time - self.window_seconds
        while self.request_timestamps and self.request_timestamps[0] < cutoff_time:
            self.request_timestamps.popleft()

    def acquire(self):
        """
        获取请求许可

        如果当前时间窗口内的请求数已达上限，会阻塞等待直到有配额可用。
        """
        with self._lock:
            current_time = time.time()
            self._clean_old_requests(current_time)

            if len(self.request_timestamps) >= self.max_requests:
                # 计算需要等待的时间
                oldest_request = self.request_timestamps[0]
                wait_time = (oldest_request + self.window_seconds) - current_time

                if wait_time > 0:
                    # 添加一个小的随机延迟，使行为更自然
                    jitter = random.uniform(0.1, 0.5)
                    total_wait = wait_time + jitter
                    logging.getLogger("AKToolsLog").info(
                        f"限流触发：已达到每 {self.window_seconds} 秒 {self.max_requests} 次的请求限制，"
                        f"等待 {total_wait:.2f} 秒后继续..."
                    )
                    time.sleep(total_wait)

                    # 重新获取时间并清理
                    current_time = time.time()
                    self._clean_old_requests(current_time)

            # 记录这次请求
            self.request_timestamps.append(current_time)

    def get_remaining_requests(self) -> int:
        """获取当前时间窗口内剩余的可用请求数"""
        with self._lock:
            self._clean_old_requests(time.time())
            return max(0, self.max_requests - len(self.request_timestamps))


# 从环境变量读取限流配置
rate_limit_max_requests = int(os.getenv("AKTOOLS_RATE_LIMIT_MAX_REQUESTS", "10"))
rate_limit_window_seconds = int(os.getenv("AKTOOLS_RATE_LIMIT_WINDOW_SECONDS", "60"))

# 全局限流器实例
rate_limiter = RateLimiter(
    max_requests=rate_limit_max_requests, window_seconds=rate_limit_window_seconds
)

# ==============================================================================
# Monkey-patch requests 模块以注入浏览器 headers 和代理
# ==============================================================================

# 从环境变量读取 HTTP 代理配置
# 优先使用 AKTOOLS_HTTP_PROXY，其次使用标准的 HTTP_PROXY/HTTPS_PROXY
_http_proxy = (
    os.getenv("AKTOOLS_HTTP_PROXY")
    or os.getenv("HTTP_PROXY")
    or os.getenv("http_proxy")
)
_https_proxy = (
    os.getenv("AKTOOLS_HTTPS_PROXY")
    or os.getenv("HTTPS_PROXY")
    or os.getenv("https_proxy")
)

# 构建代理字典（如果配置了代理）
PROXIES = {}
if _http_proxy:
    PROXIES["http"] = _http_proxy
if _https_proxy:
    PROXIES["https"] = _https_proxy
# 如果只配置了 HTTP 代理，也用于 HTTPS（常见场景）
if _http_proxy and not _https_proxy:
    PROXIES["https"] = _http_proxy

_original_request = requests.Session.request


def _patched_request(self, method, url, **kwargs):
    """
    包装 requests.Session.request 方法，自动添加浏览器 headers 和代理
    """
    # 如果没有提供 headers，使用浏览器 headers
    if "headers" not in kwargs or kwargs["headers"] is None:
        kwargs["headers"] = get_browser_headers()
    else:
        # 合并 headers，用户提供的 headers 优先级更高
        browser_headers = get_browser_headers()
        browser_headers.update(kwargs["headers"])
        kwargs["headers"] = browser_headers

    # 如果配置了代理且请求没有提供代理，则使用环境变量中的代理
    if PROXIES and ("proxies" not in kwargs or kwargs["proxies"] is None):
        kwargs["proxies"] = PROXIES

    # 添加随机延迟使请求行为更自然（0.1 到 0.5 秒之间）
    if os.getenv("AKTOOLS_RANDOM_DELAY", "true").lower() == "true":
        delay = random.uniform(0.1, 0.5)
        time.sleep(delay)

    return _original_request(self, method, url, **kwargs)


# 应用 monkey-patch
requests.Session.request = _patched_request

# 创建一个日志记录器
logger = logging.getLogger(name="AKToolsLog")
logger.setLevel(logging.INFO)

# 创建一个TimedRotatingFileHandler来进行日志轮转
handler = TimedRotatingFileHandler(
    filename="/tmp/aktools_log.log"
    if os.getenv("VERCEL") == "1"
    else "aktools_log.log",
    when="midnight",
    interval=1,
    backupCount=7,
    encoding="utf-8",
)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)

# 使用日志记录器记录信息
logger.info("这是一个信息级别的日志消息")

enable_cache = os.getenv("AKTOOLS_CACHE_ENABLE", "true").lower() == "true"
cache_maxsize = int(os.getenv("AKTOOLS_CACHE_MAXSIZE", "128"))
cache_ttl = int(os.getenv("AKTOOLS_CACHE_TTL", "3600"))

api_cache = TTLCache(maxsize=cache_maxsize, ttl=cache_ttl)


def invoke_ak_api(item_id: str, eval_str: str = None):
    """
    调用 AKShare API 接口

    此函数集成了限流和缓存机制：
    - 限流：在发起请求前调用 rate_limiter.acquire()，确保请求频率不超过设定的限制
    - 缓存：如果启用缓存，会优先返回缓存结果

    :param item_id: AKShare 接口名称，如 "stock_zh_a_hist"
    :param eval_str: 接口参数字符串，如 'symbol="000001"'
    :return: (JSON 数据, 缓存状态) 的元组
    """

    def _fetch():
        # 应用限流：在执行实际请求前获取许可
        rate_limiter.acquire()
        logger.info(
            f"调用 AKShare API: {item_id}, 剩余配额: {rate_limiter.get_remaining_requests()}/{rate_limiter.max_requests}"
        )

        if eval_str is None:
            cmd = "ak." + item_id + "()"
        else:
            cmd = "ak." + item_id + f"({eval_str})"
        received_df = eval(cmd)
        if received_df is None:
            return None
        return received_df.to_json(orient="records", date_format="iso")

    if not enable_cache:
        return _fetch(), "DISABLED"

    key = hashkey(item_id, eval_str)
    try:
        return api_cache[key], "HIT"
    except KeyError:
        res = _fetch()
        api_cache[key] = res
        return res, "MISS"


@app_core.get(
    "/private/{item_id}",
    description="私人接口",
    summary="该接口主要提供私密访问来获取数据",
)
def private_root(
    request: Request,
    item_id: str,
    current_user: User = Depends(get_current_active_user),
):
    """
    接收请求参数及接口名称并返回 JSON 数据
    此处由于 AKShare 的请求中是同步模式，所以这边在定义 root 函数中没有使用 asyncio 来定义，这样可以开启多线程访问
    :param request: 请求信息
    :type request: Request
    :param item_id: 必选参数; 测试接口名 ak.stock_dxsyl_em() 来获取 打新收益率 数据
    :type item_id: str
    :param current_user: 依赖注入，为了进行用户的登录验证
    :type current_user: str
    :return: 指定 接口名称 和 参数 的数据
    :rtype: json
    """
    interface_list = dir(ak)
    decode_params = urllib.parse.unquote(str(request.query_params))
    # print(decode_params)
    if item_id not in interface_list:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": "未找到该接口，请升级 AKShare 到最新版本并在文档中确认该接口的使用方式：https://akshare.akfamily.xyz"
            },
        )
    eval_str = decode_params.replace("&", '", ').replace("=", '="') + '"'
    if not bool(request.query_params):
        try:
            temp_df, cache_status = invoke_ak_api(item_id, None)
            if temp_df is None:
                return JSONResponse(
                    status_code=status.HTTP_404_NOT_FOUND,
                    content={
                        "error": "该接口返回数据为空，请确认参数是否正确：https://akshare.akfamily.xyz"
                    },
                    headers={"X-Cache-Status": cache_status},
                )
        except KeyError as e:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={
                    "error": f"请输入正确的参数错误 {e}，请升级 AKShare 到最新版本并在文档中确认该接口的使用方式：https://akshare.akfamily.xyz"
                },
            )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=json.loads(temp_df),
            headers={"X-Cache-Status": cache_status},
        )
    else:
        try:
            temp_df, cache_status = invoke_ak_api(item_id, eval_str)
            if temp_df is None:
                return JSONResponse(
                    status_code=status.HTTP_404_NOT_FOUND,
                    content={
                        "error": "该接口返回数据为空，请确认参数是否正确：https://akshare.akfamily.xyz"
                    },
                    headers={"X-Cache-Status": cache_status},
                )
        except KeyError as e:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={
                    "error": f"请输入正确的参数错误 {e}，请升级 AKShare 到最新版本并在文档中确认该接口的使用方式：https://akshare.akfamily.xyz"
                },
            )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=json.loads(temp_df),
            headers={"X-Cache-Status": cache_status},
        )


@app_core.get(
    path="/public/{item_id}",
    description="公开接口",
    summary="该接口主要提供公开访问来获取数据",
)
def public_root(request: Request, item_id: str):
    """
    接收请求参数及接口名称并返回 JSON 数据
    此处由于 AKShare 的请求中是同步模式，所以这边在定义 root 函数中没有使用 asyncio 来定义，这样可以开启多线程访问
    :param request: 请求信息
    :type request: Request
    :param item_id: 必选参数; 测试接口名 stock_dxsyl_em 来获取 打新收益率 数据
    :type item_id: str
    :return: 指定 接口名称 和 参数 的数据
    :rtype: json
    """
    interface_list = dir(ak)
    decode_params = urllib.parse.unquote(str(request.query_params))
    # print(decode_params)
    if item_id not in interface_list:
        logger.info(
            "未找到该接口，请升级 AKShare 到最新版本并在文档中确认该接口的使用方式：https://akshare.akfamily.xyz"
        )
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": "未找到该接口，请升级 AKShare 到最新版本并在文档中确认该接口的使用方式：https://akshare.akfamily.xyz"
            },
        )
    if "cookie" in decode_params:
        eval_str = (
            decode_params.split(sep="=", maxsplit=1)[0]
            + "='"
            + decode_params.split(sep="=", maxsplit=1)[1]
            + "'"
        )
        eval_str = eval_str.replace("+", " ")
    else:
        eval_str = decode_params.replace("&", '", ').replace("=", '="') + '"'
        eval_str = eval_str.replace("+", " ")  # 处理传递的参数中带空格的情况
    if not bool(request.query_params):
        try:
            temp_df, cache_status = invoke_ak_api(item_id, None)
            if temp_df is None:
                logger.info(
                    "该接口返回数据为空，请确认参数是否正确：https://akshare.akfamily.xyz"
                )
                return JSONResponse(
                    status_code=status.HTTP_404_NOT_FOUND,
                    content={
                        "error": "该接口返回数据为空，请确认参数是否正确：https://akshare.akfamily.xyz"
                    },
                    headers={"X-Cache-Status": cache_status},
                )
        except KeyError as e:
            logger.info(
                f"请输入正确的参数错误 {e}，请升级 AKShare 到最新版本并在文档中确认该接口的使用方式：https://akshare.akfamily.xyz"
            )
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={
                    "error": f"请输入正确的参数错误 {e}，请升级 AKShare 到最新版本并在文档中确认该接口的使用方式：https://akshare.akfamily.xyz"
                },
            )
        logger.info(f"获取到 {item_id} 的数据")
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=json.loads(temp_df),
            headers={"X-Cache-Status": cache_status},
        )
    else:
        try:
            temp_df, cache_status = invoke_ak_api(item_id, eval_str)
            if temp_df is None:
                logger.info(
                    "该接口返回数据为空，请确认参数是否正确：https://akshare.akfamily.xyz"
                )
                return JSONResponse(
                    status_code=status.HTTP_404_NOT_FOUND,
                    content={
                        "error": "该接口返回数据为空，请确认参数是否正确：https://akshare.akfamily.xyz"
                    },
                    headers={"X-Cache-Status": cache_status},
                )
        except KeyError as e:
            logger.info(
                f"请输入正确的参数错误 {e}，请升级 AKShare 到最新版本并在文档中确认该接口的使用方式：https://akshare.akfamily.xyz"
            )
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={
                    "error": f"请输入正确的参数错误 {e}，请升级 AKShare 到最新版本并在文档中确认该接口的使用方式：https://akshare.akfamily.xyz"
                },
            )
        logger.info(f"获取到 {item_id} 的数据")
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=json.loads(temp_df),
            headers={"X-Cache-Status": cache_status},
        )


def generate_html_response():
    file_path = get_pyscript_html(file="akscript.html")
    with open(file_path, encoding="utf8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content, status_code=200)


short_path = get_template_path()
templates = Jinja2Templates(directory=short_path)


@app_core.get(
    path="/show-temp/{interface}",
    response_class=HTMLResponse,
    description="展示 PyScript",
    summary="该接口主要展示 PyScript 游览器运行 Python 代码",
)
def akscript_temp(request: Request, interface: str):
    return templates.TemplateResponse(
        "akscript.html",
        context={
            "request": request,
            "ip": request.headers["host"],
            "interface": interface,
        },
    )


@app_core.get(
    path="/show",
    response_class=HTMLResponse,
    description="展示 PyScript",
    summary="该接口主要展示 PyScript 游览器运行 Python 代码",
)
def akscript():
    return generate_html_response()
