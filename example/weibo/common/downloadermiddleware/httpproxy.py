#coding:utf8
# author: shikanon
import base64
import random
import logging
import os
import codecs
from six.moves.urllib.parse import unquote
try:
    from urllib2 import _parse_proxy
except ImportError:
    from urllib.request import _parse_proxy
from six.moves.urllib.parse import urlunparse

from scrapy.utils.response import response_status_message
#from scrapy.utils.httpobj import urlparse_cached
from scrapy.utils.python import to_bytes
from twisted.internet import defer
from twisted.internet.error import TimeoutError, DNSLookupError, \
        ConnectionRefusedError, ConnectionDone, ConnectError, \
        ConnectionLost, TCPTimedOutError
from scrapy.exceptions import NotConfigured
from scrapy.xlib.tx import ResponseFailed

logger = logging.getLogger(__name__)
__author__ = "shikanon"


class RandomProxyMiddleware(object):

    def __init__(self, settings):
        '''需要配置HTTPPROXY_FILE_PATH, RETRY_TIMES, RETRY_HTTP_CODES, PROXY_USED_TIMES
        HTTPPROXY_FILE_PATH为代理ip文件的路径, RETRY_TIMES单个连接的重试次数,
        RETRY_HTTP_CODES为重试的触发HTTP返回码, PROXY_USED_TIMES为单个代理ip的失败次数, 默认为RETRY_HTTP_CODES的一半.
        (1)proxy_file格式为：请求协议://用户名:密码@ip地址:端口号
        http://proxy.example.com/
        或者http://joe:password@proxy.example.com/
        (2)proxy_dict格式为{ip_address:{"status":"valid","chance":4}，"status"表示状态,"chance"表示代理可用次数
        还可以尝试的机会次数,小于等于0则status变为invalid。
        例如：
        proxy_dict = {
            "http://joe:password@proxy.example.com/":{"status":"valid","chance":2},
            "http://proxy.example2.com/":{"status":"invalid","chance":0}
        }
        (3)requests的meta构建了proxy、retry_times、dont_retry和failed_proxies
        (4)如果遇到错误，可以在在Request的dont_filter设置为False，提交队列即可从抓不会被过滤掉。
        '''
        if not settings.getint('RETRY_TIMES'):
            self.max_retry_times = 4
        else:
            self.max_retry_times = settings.getint('RETRY_TIMES')
        if not settings.getint('PROXY_USED_TIMES'):
            self.max_proxy_chance = self.max_retry_times / 2
        else:
            self.max_proxy_chance = settings.getint('PROXY_USED_TIMES')
        self.retry_http_codes = set(int(x) for x in settings.getlist('RETRY_HTTP_CODES'))
        #优先级调整
        if not settings.getint('RETRY_PRIORITY_ADJUST'):
            self.priority_adjust = -1
        else:
            self.priority_adjust = settings.getint('RETRY_PRIORITY_ADJUST')
        #加载proxy文件
        self.proxy_dict = {}
        if not settings.get('HTTPPROXY_FILE_PATH'):
            raise NotConfigured
        file_path = settings.get('HTTPPROXY_FILE_PATH')
        if os.path.exists(file_path):
            self.proxy_dict = self._load_data(file_path)
        else:
            raise ValueError

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler.settings)

    def _load_data(self, path):
        '''加载proxy数据'''
        proxy_dict = {}
        with codecs.open(path, "r", encoding="utf8") as fr:
            for url in fr:
                url = url.replace("\r","").replace("\n","")
                if url:
                    proxy_dict[url] = {"status":"valid", "chance":self.max_proxy_chance}
        return proxy_dict

    def _del_invaild_proxy(self, request):
        '''失败了，减少proxy使用机会chance，当chance为0删除无效的proxy'''
        proxy = request.meta['proxy']
        if proxy in self.proxy_dict.keys():
            self.proxy_dict[proxy]["chance"] = self.proxy_dict[proxy]["chance"] - 1
            if self.proxy_dict[proxy]["chance"] <= 0:
                del self.proxy_dict[proxy]

    def _choose_proxy(self, request):
        '''代理地址的选择方法:
        1）随机选取
        2) 有一定的概率不采用代理'''
        if random.random() < 0.95:
            request.meta['proxy'] = random.choice(self.proxy_dict.keys())
        return request

    def _set_proxy(self, request, proxy_url):
        creds, proxy = self._get_proxy(proxy_url)
        request.meta['proxy'] = proxy
        if creds:
            request.headers['Proxy-Authorization'] = b'Basic ' + creds

    def _get_proxy(self, url):
        #>>> _parse_proxy('http://joe:password@proxy.example.com/')
        #('http', 'joe', 'password', 'proxy.example.com')
        proxy_type, user, password, hostport = _parse_proxy(url)
        proxy_url = urlunparse((proxy_type or "http", hostport, '', '', '', ''))
        #如果有用户生成证书用于连接
        if user:
            user_pass = to_bytes(
                '%s:%s' % (unquote(user), unquote(password)),
                encoding="utf-8")
            creds = base64.b64encode(user_pass).strip()
        else:
            creds = None
        return creds, proxy_url

    def _retry(self, request, reason, spider):
        '''重试方法：
        1）当连接小于特定次数时重抓，
        2) 当reason为500，抓取'''
        retries = request.meta.get('retry_times', 0) + 1
        if retries <= self.max_retry_times:
            logger.debug("Retrying %(request)s (failed %(retries)d times): %(reason)s",
                         {'request': request, 'retries': retries, 'reason': reason},
                         extra={'spider': spider})
            retryreq = request.copy()
            retryreq.meta['retry_times'] = retries
            retryreq.dont_filter = True
            retryreq.priority = request.priority + self.priority_adjust
            return retryreq
        else:
            logger.debug("Gave up retrying %(request)s (failed %(retries)d times): %(reason)s",
                         {'request': request, 'retries': retries, 'reason': reason},
                         extra={'spider': spider})

    def process_request(self, request, spider):
        # 为requests设置proxy
        request = self._choose_proxy(request)
        if "proxy" in request.meta:
            self._set_proxy(request, request.meta['proxy'])

    def process_response(self, request, response, spider):
        '''对特定的http返回码进行重新抓取,主要针对500和599等'''
        if "proxy" in request.meta:
            logger.debug("Use proxy: " + request.meta["proxy"] + "to crawler")
        if request.meta.get('dont_retry', False):
            return response
        if response.status in self.retry_http_codes:
            reason = response_status_message(response.status)
            self._del_invaild_proxy(request)
            return self._retry(request, reason, spider) or response
        return response

    def process_exception(self, request, exception, spider):
        '''遇到错误尝试重试,'''
        EXCEPTIONS_TO_RETRY = (defer.TimeoutError, TimeoutError, DNSLookupError, ConnectionRefusedError, ConnectionDone,
        ConnectError, ConnectionLost, TCPTimedOutError, ResponseFailed, IOError)
        if isinstance(exception, EXCEPTIONS_TO_RETRY) \
                and not request.meta.get('dont_retry', False):
            return self._retry(request, exception, spider)
