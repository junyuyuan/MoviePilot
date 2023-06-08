from multiprocessing.dummy import Pool as ThreadPool
from multiprocessing.pool import ThreadPool
from threading import Event
from typing import Any
from urllib.parse import urljoin

from apscheduler.schedulers.background import BackgroundScheduler
from lxml import etree
from ruamel.yaml import CommentedMap

from app.core import EventManager, settings, eventmanager
from app.helper import ModuleHelper
from app.helper.cloudflare import under_challenge
from app.helper.sites import SitesHelper
from app.log import logger
from app.plugins import _PluginBase
from app.utils.http import RequestUtils
from app.utils.timer import TimerUtils
from app.utils.types import EventType


class AutoSignIn(_PluginBase):
    # 插件名称
    plugin_name = "站点自动签到"
    # 插件描述
    plugin_desc = "站点每日自动模拟登录或签到，避免长期未登录封号。"

    # 私有属性
    sites: SitesHelper = None
    # 事件管理器
    event: EventManager = None
    # 定时器
    _scheduler = None

    # 加载的模块
    _site_schema: list = []
    # 退出事件
    _event = Event()

    def init_plugin(self, config: dict = None):
        self.sites = SitesHelper()
        self.event = EventManager()

        # 停止现有任务
        self.stop_service()

        # 加载模块
        self._site_schema = ModuleHelper.load('app.plugins.autosignin.sites',
                                              filter_func=lambda _, obj: hasattr(obj, 'match'))

        # 定时服务
        self._scheduler = BackgroundScheduler(timezone=settings.TZ)
        triggers = TimerUtils.random_scheduler(num_executions=2,
                                               begin_hour=9,
                                               end_hour=23,
                                               max_interval=12 * 60,
                                               min_interval=6 * 60)
        for trigger in triggers:
            self._scheduler.add_job(self.sign_in, "cron", hour=trigger.hour, minute=trigger.minute)

        # 启动任务
        if self._scheduler.get_jobs():
            self._scheduler.print_jobs()
            self._scheduler.start()

    @staticmethod
    def get_command() -> dict:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return {
            "cmd": "/pts",
            "event": EventType.SiteSignin,
            "desc": "站点自动签到",
            "data": {}
        }

    @eventmanager.register(EventType.SiteSignin)
    def sign_in(self, event: Event = None):
        """
        自动签到
        """
        # 查询签到站点
        sign_sites = self.sites.get_indexers()
        if not sign_sites:
            logger.info("没有需要签到的站点")
            return

        # 执行签到
        logger.info("开始执行签到任务 ...")
        with ThreadPool(min(len(sign_sites), 5)) as p:
            status = p.map(self.signin_site, sign_sites)

        if status:
            logger.info("站点签到任务完成！")
            # 发送通知
            self.chain.run_module("post_message", title="站点自动签到", text="\n".join(status))
        else:
            logger.error("站点签到任务失败！")

    def __build_class(self, url) -> Any:
        for site_schema in self._site_schema:
            try:
                if site_schema.match(url):
                    return site_schema
            except Exception as e:
                logger.error("站点模块加载失败：%s" % str(e))
        return None

    def signin_site(self, site_info: CommentedMap) -> str:
        """
        签到一个站点
        """
        site_module = self.__build_class(site_info.get("url"))
        if site_module and hasattr(site_module, "signin"):
            try:
                status, msg = site_module().signin(site_info)
                # 特殊站点直接返回签到信息，防止仿真签到、模拟登陆有歧义
                return msg
            except Exception as e:
                return f"【{site_info.get('name')}】签到失败：{str(e)}"
        else:
            return self.__signin_base(site_info)

    def __signin_base(self, site_info: CommentedMap) -> str:
        """
        通用签到处理
        :param site_info: 站点信息
        :return: 签到结果信息
        """
        if not site_info:
            return ""
        site = site_info.get("name")
        site_url = site_info.get("url")
        site_cookie = site_info.get("cookie")
        ua = site_info.get("ua")
        if not site_url or not site_cookie:
            logger.warn(f"未配置 {site} 的站点地址或Cookie，无法签到")
            return ""
        # 模拟登录
        try:
            # 访问链接
            checkin_url = site_url
            if site_url.find("attendance.php") == -1:
                # 拼登签到地址
                checkin_url = urljoin(site_url, "attendance.php")
            logger.info(f"开始站点签到：{site}，地址：{checkin_url}...")
            res = RequestUtils(cookies=site_cookie,
                               headers=ua,
                               proxies=settings.PROXY if site_info.get("proxy") else None
                               ).get_res(url=checkin_url)
            if not res and site_url != checkin_url:
                logger.info(f"开始站点模拟登录：{site}，地址：{site_url}...")
                res = RequestUtils(cookies=site_cookie,
                                   headers=ua,
                                   proxies=settings.PROXY if site_info.get("proxy") else None
                                   ).get_res(url=site_url)
            # 判断登录状态
            if res and res.status_code in [200, 500, 403]:
                if not self.is_logged_in(res.text):
                    if under_challenge(res.text):
                        msg = "站点被Cloudflare防护，请更换Cookie和UA！"
                    elif res.status_code == 200:
                        msg = "Cookie已失效"
                    else:
                        msg = f"状态码：{res.status_code}"
                    logger.warn(f"{site} 签到失败，{msg}")
                    return f"【{site}】签到失败，{msg}！"
                else:
                    logger.info(f"{site} 签到成功")
                    return f"【{site}】签到成功"
            elif res is not None:
                logger.warn(f"{site} 签到失败，状态码：{res.status_code}")
                return f"【{site}】签到失败，状态码：{res.status_code}！"
            else:
                logger.warn(f"{site} 签到失败，无法打开网站")
                return f"【{site}】签到失败，无法打开网站！"
        except Exception as e:
            logger.warn("%s 签到失败：%s" % (site, str(e)))
            return f"【{site}】签到失败：{str(e)}！"

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))

    @classmethod
    def is_logged_in(cls, html_text: str) -> bool:
        """
        判断站点是否已经登陆
        :param html_text:
        :return:
        """
        html = etree.HTML(html_text)
        if not html:
            return False
        # 存在明显的密码输入框，说明未登录
        if html.xpath("//input[@type='password']"):
            return False
        # 是否存在登出和用户面板等链接
        xpaths = ['//a[contains(@href, "logout")'
                  ' or contains(@data-url, "logout")'
                  ' or contains(@href, "mybonus") '
                  ' or contains(@onclick, "logout")'
                  ' or contains(@href, "usercp")]',
                  '//form[contains(@action, "logout")]']
        for xpath in xpaths:
            if html.xpath(xpath):
                return True
        user_info_div = html.xpath('//div[@class="user-info-side"]')
        if user_info_div:
            return True

        return False
