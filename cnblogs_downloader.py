import json
import os
import re
import sys
import threading
from datetime import datetime

import httpx

import lib.cnblogs_api as api

from lxml import etree
import html2text
from distutils.util import strtobool

MD_HEAD = """
---
layout:     post
title:      "{title}"
date:       {date}
tag:        {category}
description: ""
---

link:       [{link}]({link})

"""

class CnblogsDownloader:
    """
    下载器类，日志均print到控制台\n
    """

    _FLAG_FILE_NAME = ".CnblogsDownloaderFlag.json"

    _IMG_PATTERN = re.compile(r'(!\[[^\]]*?\]\()([^\)]*/([^\)]*?))(\))|(<img[^>]*?src=")([^"]*/([^"]*?))("[^>]*?>)')
    """
    预先编译正则
    called by :py:func:`CnblogsDownloader._download_replace_img`\n
    此处正则中使用(?:)非捕获元无效
    """

    def __init__(self, cnblogs_cookie, workdir, download_img=False):
        """
        初始化下载器类

        :param str cnblogs_cookie: 博客园Cookie ``.Cnblogs.AspNetCore.Cookies`` 的值
        :param str workdir: 工作目录，即下载目录
        :param bool download_img: 是否离线随笔中引用的图片
        """
        self._total_essay = 0
        self._updated_essay = 0
        self._is_first_run = True
        self._last_update = None
        self._workdir = workdir
        self._download_img = download_img
        self._lock = threading.Lock()
        self._http_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/99.0.4844.74 Safari/537.36",
            "Referer": "https://i.cnblogs.com/",
            "Cookie": rf".Cnblogs.AspNetCore.Cookies={cnblogs_cookie}"}
        self._category = api.get_category_list(self._http_headers)
        if type(self._category) == dict:
            errors = self._category.get("errors1")
            if errors is not None and errors[0] == "Unauthorized":
                print("Cookie 已过期，请重新配置Cookie")
                sys.exit()

        flag_path = rf"{workdir}\{self._FLAG_FILE_NAME}"
        if os.path.isfile(flag_path):
            self._is_first_run = False
            flag = None
            with open(flag_path, "r", encoding="utf-8") as f:
                flag = json.load(f)
                pass
            # download_to_subdir最后还有写入操作
            last_update = flag["last_update"]
            self._last_update = datetime.strptime(last_update, "%Y-%m-%dT%H:%M:%S")

    def download_to_subdir(self):
        """
        开始下载\n
        主函数，多线程下载随笔及图片，线程数为随笔的分类数

        :rtype: int
        :return: 更新的随笔数量
        """
        current_path = os.getcwd()
        os.chdir(self._workdir)
        self._category.append({"categoryId": 0, "title": "未分类"})
        download_threads = []
        for category in self._category:
            # (category,) 一个元素的元组 (category)是列表转元组
            download_thread = threading.Thread(target=self._category_download_thread, args=(category,))
            download_thread.start()
            download_threads.append(download_thread)
        for download_thread in download_threads:
            download_thread.join()
        print(rf"总共{self._total_essay}篇随笔，更新了{self._updated_essay}篇")
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        with open(rf"{self._workdir}\{self._FLAG_FILE_NAME}", "w", encoding="utf-8") as f:
            f.write(rf'{{"last_update": "{now}"}}')
        os.chdir(current_path)
        return self._updated_essay

    def _category_download_thread(self, category):
        """
        每个分类一个线程去下载随笔内容，需要的话，还能下载图片\n
        多线程共用一个工作目录，任何一个线程os.chdir都会改变整个程序的工作目录

        :param dict category: 分类的基本信息
        """
        dirname = category["title"]
        dirname = re.sub(rf'(\\|/|\?|\||"|:|\*|<|>)', " ", dirname)
        if not os.path.isdir(dirname):
            os.mkdir(dirname)
        write_absolute_path = rf"{self._workdir}\{dirname}"

        essays = api.get_posts_list(self._http_headers, category_id=str(category["categoryId"]))
        self._lock.acquire()
        self._total_essay = self._total_essay + essays["postsCount"]
        self._lock.release()
        for essay_pre in essays["postList"]:
            filename = essay_pre["title"]
            # 替换特殊字符，Windows文件名不允许出现特殊字符： \/:*?"<>|
            filename = re.sub(rf'(\\|/|\?|\||"|:|\*|<|>)', " ", filename)
            filename = rf'{filename}{"[非公开]" if not essay_pre["isPublished"] else ""}' \
                       rf'{"[草稿]" if essay_pre["isDraft"] else ""}.md'

            essay_date_updated = datetime.strptime(essay_pre["dateUpdated"], "%Y-%m-%dT%H:%M:%S")
            if (not self._is_first_run) and os.path.isfile(rf"{write_absolute_path}\{filename}") and \
                    (self._last_update - essay_date_updated).total_seconds() > 0:
                self._lock.acquire()
                print(rf"已是最新：{dirname}\{filename}")
                self._lock.release()
                continue

            #准备头部
            title = essay_pre["title"]
            link_name = "https:" + essay_pre["url"]
            pub_date = datetime.strptime(essay_pre["datePublished"], "%Y-%m-%dT%H:%M:%S")
            md_head = MD_HEAD.format(title=title, link=link_name, date=pub_date, category=dirname)

            essay = api.get_post_by_id(self._http_headers, str(essay_pre["id"]))

            essay_content = essay["blogPost"]["postBody"]
            # 将html转换成Markdown
            if not essay["blogPost"]["isMarkdown"]:
                page = etree.HTML(essay_content)  # 将HTML标签补全
                content = page.xpath("//html")[0]
                content = etree.tostring(content, encoding='utf-8').decode('utf-8')
                h = html2text.HTML2Text()  # 将html转换成Markdown
                h.mark_code = True
                h.body_width = 0
                content = h.handle(content)
                content = content.replace('[code]', '```csharp').replace('[/code]', '```')
                essay_content = content

            # 如果设置了下载离线图片，则对图片的路径进行替换
            if strtobool(self._download_img):
                essay_content = CnblogsDownloader._download_replace_img(filename, essay_content, write_absolute_path)

            with open(rf"{write_absolute_path}\{filename}", "w", encoding="utf-8") as f:
                f.write(md_head.strip("\n")+"\n"+essay_content)
            self._lock.acquire()
            self._updated_essay = self._updated_essay + 1
            print(rf"已下载随笔：{dirname}\{filename}")
            self._lock.release()

    @staticmethod
    def _download_replace_img(essay_title, essay_content, workdir):
        """
        替换文章内容中的图片，包括 ``![]()`` 和 ``<img src="xx" style="height:450px">`` 的格式\n
        img标签中其他属性也会被保留，比如替换后为 ``<img src="./img/xx" style="height:450px">`` \n
        最后根据图片链接下载图片

        :rtype: str
        :return: 完成替换后的文章内容
        """
        img_url = []

        # bug：写成lambda表达式用or连接两句时，只会执行最后一个表达式，猜测是因为前面的语句没有返回值
        def replace(m):
            """
            m : match 对象
            match方法是从头开始匹配的，从中间截取字符串，是无法匹配到的。这也是match方法的局限性。
            match方法匹配不到结果时，返回的是None，匹配到结果时，返回的是match对象。
            match方法匹配到结果时，使用match对象的group方法，获取匹配结果。
            """
            jekyll_img_path = f"{essay_title}.assets"

            img_url.append(m.group(2) if m.group(2) else m.group(6))

            # group(1) 是分类
            return rf"{m.group(1)}./{essay_title}.assets/{m.group(3)}{m.group(4)}" if m.group(
                3) else rf"{m.group(5)}./{essay_title}.assets/{m.group(7)}{m.group(8)}"

        '''
        Python 的 re 模块提供了re.sub用于替换字符串中的匹配项。
        语法：

        re.sub(pattern, repl, string, count=0, flags=0)
        参数：

        pattern : 正则中的模式字符串。
        repl : 替换的字符串，也可为一个函数。
        string : 要被查找替换的原始字符串。
        count : 模式匹配后替换的最大次数，默认 0 表示替换所有的匹配。

        在这里，我们定义了一个名为replace_func的函数，它接受一个叫做match的参数，
        该参数表示的是当前匹配的MatchObject对象。replace_func函数内部首先使用group(0)方法获取到匹配到的完整单词，
        然后调用upper()方法将其转换成大写字母。最后将转换后的结果作为替换文本返回即可。

        如果需要反复使用同一正则表达式进行替换，可以考虑使用re.compile()函数预编译正则表达式，以提高程序运行效率。
        经过预编译后的正则表达式可以通过sub()方法的参数调用。
        '''
        essay_content = CnblogsDownloader._IMG_PATTERN.sub(replace, essay_content)
        http_headers = {"Referer": "https://i.cnblogs.com/"}
        if len(img_url) > 0 and (not os.path.isdir(rf"{workdir}\{essay_title}.assets")):
            os.mkdir(rf"{workdir}\{essay_title}.assets")
        for url in img_url:
            # 不再校验文件名的合法性
            img_name = url.split("/")[-1]
            img_path = rf"{workdir}/{essay_title}.assets/{img_name}"
            if os.path.isfile(img_path):
                print(rf"图片已存在：{img_name}")
                continue
            try:
                r = httpx.get(url, headers=http_headers, timeout=api.TIMEOUT)
                with open(img_path, "wb") as f:
                    f.write(r.content)
                print(rf"已为《{essay_title}》下载图片：{img_name}")
            except Exception as e:
                print(f"error: 为《{essay_title}》下载图片失败，链接：{url}")
        return essay_content
