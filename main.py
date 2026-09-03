import os, re, json, logging, random, asyncio, feedparser, requests
from datetime import datetime, timedelta, date
from apscheduler.schedulers.asyncio import AsyncIOScheduler 
from astrbot.api.event import filter, MessageChain, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


# --- 内置默认 LLM 提示词模板（WebUI 可配置；素材/清单由插件自动拼在结尾，无需占位符） ---
DEFAULT_NEWS_PROMPT = (
    "你是一位冷静、高效的资深编辑。请完成以下任务：\n"
    "1. 语义去重与新闻整合：合并不同来源对同一大事的报道，英文内容直接翻译要点（这部分无须反馈给我）。\n"
    "2. 要闻精选与内容提炼：基于结尾列出的素材，选出 5 条对中国市场、全球政治或全球科技有重大影响的消息，"
    "每条用一句话补充要点并说明它为何重要（不超过100字），注明新闻来源。"
)

DEFAULT_REVIEW_PROMPT = (
    "你是一个专业的合作伙伴和靠谱的朋友。请基于结尾列出的本周未完成任务清单，"
    "以你的人格设定，结合项目名和标签，给出简短的进度压力分析和下周建议。"
    "不要列清单，直接给结论。限 100 字。"
)


class NewsScout:
    """RSS 新闻抓取与处理（支持 LLM 提炼和原文整理两种模式）"""

    FETCH_LIMIT = 8  # 每个源最多取多少条

    @staticmethod
    async def fetch_sources(sources: dict) -> list:
        """抓取多个 RSS 源，返回标准化条目列表"""
        import socket
        socket.setdefaulttimeout(10)
        entries = []
        for name, url in sources.items():
            try:
                logging.info(f"[NewsScout] 正在抓取: {name}...")
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                }
                try:
                    resp = requests.get(url, headers=headers, timeout=12)
                    resp.raise_for_status()
                    feed = feedparser.parse(resp.text)
                except Exception:
                    feed = feedparser.parse(url)

                if feed.entries:
                    count = 0
                    for entry in feed.entries[:NewsScout.FETCH_LIMIT]:
                        content_raw = entry.get('summary', entry.get('description', ''))
                        desc = re.sub(r'<[^>]+>', '', content_raw)[:80]
                        link = entry.get('link', '')
                        entries.append({
                            'source': name,
                            'title': entry.title.strip(),
                            'desc': desc.strip(),
                            'link': link.strip(),
                        })
                        count += 1
                    logging.info(f"✅ {name} → {count} 条")
                else:
                    logging.warning(f"⚠️ {name} 无内容")
            except Exception as e:
                logging.error(f"❌ {name} 异常: {e}")
        return entries

    @staticmethod
    async def llm_report(entries: list, context, provider_id: str, prompt_template: str | None = None) -> str | None:
        """LLM 提炼：去重 + 精选 + 要闻摘要"""
        raw_lines = []
        for e in entries:
            raw_lines.append(f"[{e['source']}] {e['title']}: {e['desc']}")
        if not raw_lines:
            return None

        tmpl = prompt_template or DEFAULT_NEWS_PROMPT
        prompt = tmpl.rstrip("\n") + "\n\n素材如下：\n" + chr(10).join(raw_lines)
        resp = await context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
        return resp.completion_text if resp else None

    @staticmethod
    def raw_report(entries: list, group_name: str) -> str | None:
        """原文链接模式：脚本整理标题+链接，零 LLM 开销"""
        if not entries:
            return None

        lines = [f"📰 {group_name}", "─" * 12, ""]
        current_source = None
        for e in entries:
            if e['source'] != current_source:
                current_source = e['source']
                lines.append(f"▎{current_source}")
            link_part = f" {e['link']}" if e['link'] else ""
            lines.append(f"  · {e['title']}{link_part}")
        
        return "\n".join(lines)

    @staticmethod
    async def process_group(name: str, sources: dict, mode: str, context, provider_id: str | None = None, prompt_template: str | None = None) -> str | None:
        """处理一个新闻分组，返回格式化报告"""
        entries = await NewsScout.fetch_sources(sources)
        if not entries:
            return None

        if mode == "llm":
            if not provider_id:
                logging.warning(f"[NewsScout] 分组「{name}」为 llm 模式但无 provider_id")
                return None
            return await NewsScout.llm_report(entries, context, provider_id, prompt_template)
        else:
            return NewsScout.raw_report(entries, name)


def _parse_time(time_str: str):
    """Parse 'HH:MM' string into (hour, minute) ints."""
    try:
        parts = time_str.strip().split(":")
        return int(parts[0]), int(parts[1])
    except Exception:
        return 0, 0


class ChineseDateParser:
    """从中文自然语言中解析日期和任务文本。

    先尝试在开头找日期，找不到再在结尾找。
    日期和内容之间可无间隔、空格、中英文逗号。
    内容段原样保留（含时间、标签、其他日期文字）。

    支持格式:
      - 2026年6月23日买菜 / 买菜2026年6月23日
      - 2026-07-04干活 / 干活2026-07-04
      - 今天修手表 / 修手表今天
      - 明天开发票 / 开发票明天
      - 下周二看病 / 看病下周二
      - 7月4日交稿 / 交稿7月4日
      - 25号交稿 / 交稿25号
      等等
    """
    _SEP = ' ，,、\t'

    WEEKDAY_NAMES = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

    @classmethod
    def parse(cls, text: str):
        """解析文本，返回 (datetime.date or None, task_text)。"""
        from datetime import date
        today = date.today()
        text = text.strip()
        if not text:
            return None, ""

        # Pass 1：在开头找日期
        for name in cls._BEGIN_HANDLER_NAMES:
            handler = getattr(cls, name)
            result = handler(text, today)
            if result is not None:
                dt, content = result
                return dt, content.lstrip(cls._SEP).strip()

        # Pass 2：在结尾找日期
        for name in cls._END_HANDLER_NAMES:
            handler = getattr(cls, name)
            result = handler(text, today)
            if result is not None:
                dt, content = result
                return dt, content.rstrip(cls._SEP).strip()

        return None, text

    # ── 开头匹配 handlers（即原有逻辑）────────────────────

    @classmethod
    def _parse_ymd(cls, t, today):
        m = re.match(r'^(\d{4})\s*[年]\s*(\d{1,2})\s*[月]\s*(\d{1,2})\s*[日号]?\s*', t)
        if m:
            return date(int(m[1]), int(m[2]), int(m[3])), t[m.end():]

    @classmethod
    def _parse_iso_date(cls, t, today):
        m = re.match(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s*', t)
        if m:
            try: return date(int(m[1]), int(m[2]), int(m[3])), t[m.end():]
            except: return None

    @classmethod
    def _parse_compact_date(cls, t, today):
        m = re.match(r'^(\d{4})(\d{2})(\d{2})', t)
        if m:
            y, mo, d = int(m[1]), int(m[2]), int(m[3])
            if 1 <= mo <= 12 and 1 <= d <= 31:
                try: return date(y, mo, d), t[m.end():]
                except: return None

    @classmethod
    def _parse_today(cls, t, today):
        m = re.match(r'^今[天日]', t)
        if m: return today, t[m.end():]

    @classmethod
    def _parse_tomorrow(cls, t, today):
        m = re.match(r'^明[天日]', t)
        if m: return today + timedelta(1), t[m.end():]

    @classmethod
    def _parse_after(cls, t, today):
        m = re.match(r'^(大?)后[天日]', t)
        if m:
            off = 3 if m[1] == '大' else 2
            return today + timedelta(off), t[m.end():]

    @classmethod
    def _parse_weekday_rel(cls, t, today):
        m = re.match(r'^((?:下下|下|这|本)?)(礼拜|星期|周)([一二三四五六日天])', t)
        if not m: return None
        d = cls._weekday_delta(today, m[1] or '这', cls.WEEKDAY_NAMES.get(m[3]))
        if d is None: return None
        return today + timedelta(d), t[m.end():]

    @classmethod
    def _parse_next_month(cls, t, today):
        m = re.match(r'^下(?:个)?月\s*(\d{1,2})\s*[日号]?\s*', t)
        if m: return cls._calc_next_month(today, int(m[1])), t[m.end():]

    @classmethod
    def _parse_md(cls, t, today):
        m = re.match(r'^(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?\s*', t)
        if not m: return None
        d = cls._resolve_md(today, int(m[1]), int(m[2]))
        if d: return d, t[m.end():]

    @classmethod
    def _parse_day_only(cls, t, today):
        m = re.match(r'^(\d{1,2})\s*[日号]\s*', t)
        if not m: return None
        import calendar
        day = int(m[1])
        last = calendar.monthrange(today.year, today.month)[1]
        if 1 <= day <= last:
            return date(today.year, today.month, day), t[m.end():]

    # ── 结尾匹配 handlers ──────────────────────────

    @classmethod
    def _end_ymd(cls, t, today):
        """买菜2026年6月23日"""
        m = re.search(r'(\d{4})\s*[年]\s*(\d{1,2})\s*[月]\s*(\d{1,2})\s*[日号]?\s*$', t)
        if m:
            return date(int(m[1]), int(m[2]), int(m[3])), t[:m.start()]

    @classmethod
    def _end_iso_date(cls, t, today):
        """干活2026-07-04"""
        m = re.search(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s*$', t)
        if m:
            try: return date(int(m[1]), int(m[2]), int(m[3])), t[:m.start()]
            except: return None

    @classmethod
    def _end_compact_date(cls, t, today):
        """干活20260704"""
        m = re.search(r'(\d{4})(\d{2})(\d{2})$', t)
        if m:
            y, mo, d = int(m[1]), int(m[2]), int(m[3])
            if 1 <= mo <= 12 and 1 <= d <= 31:
                try: return date(y, mo, d), t[:m.start()]
                except: return None

    @classmethod
    def _end_today(cls, t, today):
        m = re.search(r'今[天日]$', t)
        if m: return today, t[:m.start()]

    @classmethod
    def _end_tomorrow(cls, t, today):
        m = re.search(r'明[天日]$', t)
        if m: return today + timedelta(1), t[:m.start()]

    @classmethod
    def _end_after(cls, t, today):
        m = re.search(r'(大?)后[天日]$', t)
        if m:
            off = 3 if m[1] == '大' else 2
            return today + timedelta(off), t[:m.start()]

    @classmethod
    def _end_weekday_rel(cls, t, today):
        m = re.search(r'((?:下下|下|这|本)?)(礼拜|星期|周)([一二三四五六日天])$', t)
        if not m: return None
        d = cls._weekday_delta(today, m[1] or '这', cls.WEEKDAY_NAMES.get(m[3]))
        if d is None: return None
        return today + timedelta(d), t[:m.start()]

    @classmethod
    def _end_next_month(cls, t, today):
        m = re.search(r'下(?:个)?月\s*(\d{1,2})\s*[日号]?\s*$', t)
        if m: return cls._calc_next_month(today, int(m[1])), t[:m.start()]

    @classmethod
    def _end_md(cls, t, today):
        m = re.search(r'(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?\s*$', t)
        if not m: return None
        d = cls._resolve_md(today, int(m[1]), int(m[2]))
        if d: return d, t[:m.start()]

    @classmethod
    def _end_day_only(cls, t, today):
        m = re.search(r'(\d{1,2})\s*[日号]\s*$', t)
        if not m: return None
        import calendar
        day = int(m[1])
        last = calendar.monthrange(today.year, today.month)[1]
        if 1 <= day <= last:
            return date(today.year, today.month, day), t[:m.start()]

    # ── 共享工具方法 ──────────────────────────

    @classmethod
    def _weekday_delta(cls, today, prefix, weekday):
        if weekday is None: return None
        d = weekday - today.weekday()
        if prefix in ('这', '本'):
            return d + 7 if d <= 0 else d
        if prefix == '下': return d + 7
        if prefix == '下下': return d + 14
        return d

    @classmethod
    def _calc_next_month(cls, today, day):
        import calendar
        mo = today.month + 1
        y = today.year
        if mo > 12: mo, y = 1, y + 1
        last = calendar.monthrange(y, mo)[1]
        return date(y, mo, min(day, last))

    @classmethod
    def _resolve_md(cls, today, month, day):
        try:
            d = date(today.year, month, day)
        except ValueError:
            return None
        if d < today:
            try: d = date(today.year + 1, month, day)
            except: return None
        return d

    _BEGIN_HANDLER_NAMES = [
        '_parse_ymd', '_parse_iso_date', '_parse_compact_date',
        '_parse_today', '_parse_tomorrow', '_parse_after',
        '_parse_weekday_rel', '_parse_next_month', '_parse_md', '_parse_day_only',
    ]

    _END_HANDLER_NAMES = [
        '_end_ymd', '_end_iso_date', '_end_compact_date',
        '_end_today', '_end_tomorrow', '_end_after',
        '_end_weekday_rel', '_end_next_month', '_end_md', '_end_day_only',
    ]


@register("obsidian_reminder", "quasarrise", "扫描 Obsidian 待办并定时推送提醒，集成 RSS 新闻简报", "1.2.0")
class ObsidianReminder(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        cfg = config or {}

        # --- 插件配置（来自 WebUI）---
        self.vault_path = cfg.get("vault_path") or ""
        data_dir = get_astrbot_data_path()
        self.config_dir = os.path.join(data_dir, "config")
        self.config_file = os.path.join(self.config_dir, "obsidian_reminder_config.json")
        self.task_file_template = cfg.get("task_file") or "{{date}}.md"
        self.task_format = cfg.get("task_format") or "emoji"
        self.task_mode = cfg.get("task_mode") or "daily"
        # 任务文档列表
        self.task_docs = []
        raw_docs = cfg.get("task_docs", [])
        if raw_docs:
            for item in raw_docs[:4]:
                path = (item.get("path") or "").strip()
                trigger = (item.get("trigger") or "").strip()
                if path:
                    # 触发词默认用文件名（不含路径和后缀）
                    _stem = os.path.splitext(os.path.basename(path))[0]
                    self.task_docs.append({
                        "path": path,
                        "trigger": trigger or _stem,
                    })

        # 任务推送时间（空字符串 = 不推送）
        m_h, m_m = _parse_time(cfg.get("morning_push_time") or "")
        n_h, n_m = _parse_time(cfg.get("noon_push_time") or "")
        e_h, e_m = _parse_time(cfg.get("night_push_time") or "")

        # 周复盘（需同时设置 day + time）
        self.review_day = (cfg.get("review_day") or "").strip()
        r_h, r_m = _parse_time(cfg.get("review_time") or "")

        # 新闻推送
        self.news_enabled = cfg.get("news_enabled", False)
        nm_h, nm_m = _parse_time(cfg.get("morning_news_time") or "")
        ne_h, ne_m = _parse_time(cfg.get("night_news_time") or "")

        # 新闻分组（最多3组，每组可选 llm / raw 模式）
        raw_groups = cfg.get("news_groups", [])
        self.news_groups = []
        if raw_groups:
            for g in raw_groups[:3]:
                name = (g.get("name") or "").strip()
                mode = (g.get("mode") or "llm").strip()
                sources = {}
                raw_text = (g.get("sources_text") or "").strip()
                for line in raw_text.split('\n'):
                    line = line.strip()
                    if '|' not in line or line.startswith('#'):
                        continue
                    parts = line.split('|', 1)
                    sname = parts[0].strip()
                    surl = parts[1].strip()
                    if sname and surl:
                        sources[sname] = surl
                if name and sources:
                    self.news_groups.append({
                        "name": name,
                        "mode": mode,
                        "sources": sources,
                    })
                    logging.info(f"[NewsScout] 加载分组「{name}」({mode}, {len(sources)}个源)")

        # --- LLM 提示词模板（WebUI 可配置，空则用内置默认） ---
        self.news_prompt = (cfg.get("news_prompt") or "").strip() or DEFAULT_NEWS_PROMPT
        self.review_prompt = (cfg.get("review_prompt") or "").strip() or DEFAULT_REVIEW_PROMPT

        # --- Bot 绑定配置（!obreg 管理） ---
        self.config_data = {} 
        if os.path.exists(self.config_file):
            with open(self.config_file, 'r') as f:
                try:
                    self.config_data = json.load(f)
                except:
                    self.config_data = {}

        self.priority_map = {"highest": 5, "high": 4, "medium": 3, "normal": 2, "low": 1, "lowest": 0}

        # --- 调度器（仅当配置了时间才添加 job）---
        self.scheduler = AsyncIOScheduler()
        if m_h or m_m:
            self.scheduler.add_job(self.scheduled_push, 'cron', hour=m_h, minute=m_m, args=['morning'])
        if n_h or n_m:
            self.scheduler.add_job(self.scheduled_push, 'cron', hour=n_h, minute=n_m, args=['noon'])
        if e_h or e_m:
            self.scheduler.add_job(self.scheduled_push, 'cron', hour=e_h, minute=e_m, args=['night'])
        if self.review_day and (r_h or r_m):
            self.scheduler.add_job(self.weekly_review_cron, 'cron',
                                   day_of_week=self.review_day, hour=r_h, minute=r_m)
        if self.news_enabled:
            if nm_h or nm_m:
                self.scheduler.add_job(self.news_scout_task, 'cron', hour=nm_h, minute=nm_m)
            if ne_h or ne_m:
                self.scheduler.add_job(self.news_scout_task, 'cron', hour=ne_h, minute=ne_m)
        self.scheduler.start()
        logging.info("--- [Obsidian Task Reminder] v1.2.0 已启动 ---")

    def _load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f: return json.load(f).get("target_id")
            except: pass
        return None

    async def scheduled_push(self, mode='morning'):
        if not self.vault_path: return  # 未配置笔记库路径，功能关闭
        if not self.config_data: return # 只要没绑定任何一个 Bot 就退出
        """任务融合推送逻辑"""
        today_date = datetime.now()
        today_str = today_date.strftime("%Y-%m-%d")
        # 1. 获取所有相关任务
        today_tasks = self.scan_tasks(today_str)
        overdue_tasks = self.scan_overdue(days_back=7)
        # 2. 语气生成器 (真人伙伴风格)
        def get_greeting(t_count, o_count, p_mode):
            if p_mode == 'manual':
                if t_count + o_count == 0: return "🎉 今天暂时没啥要紧事，玩去吧。"
                greetings = ["📋 看下当前的进度：", "📋 今天还有这些事：", "📋 今天的活儿：", "📋 还没处理的："]
            elif p_mode == 'morning':
                greetings = ["🌅 早啊！今天的活儿：", "🌅 咱们先把这几个搞定：", "🌅 起来了吗？今天得接着忙活这些："]
            elif p_mode == 'night':
                return "🌙 今天怎么样？顺便看一眼明天还有啥事："
            else:
                greetings = ["⏳ 进度提醒：", "⏳ 这些还没勾掉："]
            return random.choice(greetings)
        # 3. 合并与排序 (逻辑同 6.2)
        all_tasks = []
        for t in today_tasks:
            t['is_today'] = True
            all_tasks.append(t)
        for t in overdue_tasks:
            t['is_today'] = False
            all_tasks.append(t)
        all_tasks.sort(key=lambda x: (x['is_today'], self.priority_map.get(x['priority'], 2)), reverse=True)
        # 4. 构造消息体
        priority_emoji = {"highest": "🔺", "high": "⏫", "medium": "🔼", "normal": "🔵", "low": "🔽", "lowest": "⏬"}
        report_lines = [get_greeting(len(today_tasks), len(overdue_tasks), mode)]
        if all_tasks:
            for t in all_tasks:
                pe = priority_emoji.get(t['priority'], "⚪")
                pfx = "" if t['is_today'] else f" ⏰{t['date']}"
                report_lines.append(f"{pe} {t['project']}:{t['text']}{pfx}")
        # 5. 明日预告逻辑 (保持精简)
        if mode == 'night':
            tomorrow_str = (today_date + timedelta(days=1)).strftime("%Y-%m-%d")
            tomorrow_tasks = self.scan_tasks(tomorrow_str)
            imp_tomorrow = [t for t in tomorrow_tasks if self.priority_map.get(t['priority'], 2) >= 4]
            if imp_tomorrow:
                report_lines.append("\n📆 明天的事：")
                for t in imp_tomorrow:
                    report_lines.append(f"🟠 {t['project']}:{t['text']}")
        chain = MessageChain().message("\r\n".join(report_lines))
        await self.send_to_authorized_bots(chain)

    def scan_tasks(self, date_str):
        """上下文增强版扫描：保留文件名、标签和优先级"""
        found = []
        if not os.path.exists(self.vault_path): return found
        
        for root, dirs, files in os.walk(self.vault_path):
            if '.obsidian' in dirs: dirs.remove('.obsidian')
            for file in files:
                if file.endswith(".md"):
                    file_name = file[:-3] # 去掉 .md 后缀，作为项目背景
                    try:
                        with open(os.path.join(root, file), 'r', encoding='utf-8') as f:
                            for line in f:
                                if "- [ ]" in line and date_str in line:
                                    # 1. 提取原始行
                                    raw_text = re.sub(r"^\s*-\s*\[ \]\s*", "", line).strip()
                                    
                                    # 2. 提取优先级
                                    p_match = re.search(r"\[priority::\s*(\w+)\s*\]", raw_text, re.I)
                                    p_level = p_match.group(1).lower() if p_match else "normal"
                                    
                                    # 3. 提取所有标签（保留除了 #task 以外的）
                                    tags = re.findall(r"#(\w+)", raw_text)
                                    other_tags = [t for t in tags if t.lower() != "task"]
                                    
                                    # 4. 彻底净化任务正文（去掉日期、Dataview语法和所有标签）
                                    clean_text = re.sub(r"📅\s*\d{4}-\d{2}-\d{2}|\d{4}-\d{2}-\d{2}", "", raw_text)
                                    clean_text = re.sub(r"\[\w+::.*?\]", "", clean_text)
                                    clean_text = re.sub(r"#\w+", "", clean_text).strip()
                                    
                                    if clean_text:
                                        found.append({
                                            "text": clean_text,
                                            "project": file_name,      # 文件名：提供背景
                                            "tags": other_tags,        # 标签：提供性质
                                            "priority": p_level,
                                            "date": date_str
                                        })
                    except: pass
        return found

    def scan_overdue(self, days_back=7):
        """扫描过去几天的未完成任务"""
        overdue = []
        today = datetime.now().date()
        # 生成过去几天的日期列表
        past_dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, days_back + 1)]
        
        for d_str in past_dates:
            tasks = self.scan_tasks(d_str)
            overdue.extend(tasks)
        return overdue

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-1)
    async def on_all_message(self, event: AstrMessageEvent):
        curr_session = event.unified_msg_origin
        msg = event.message_str.strip().lower()
        
        # 获取当前收到消息的机器人账号 ID
        current_bot_id = "unknown"
        if hasattr(event, 'message_obj') and event.message_obj:
            current_bot_id = str(event.message_obj.self_id)
        elif hasattr(event, 'self_id'):
            current_bot_id = str(event.self_id)
        # 1. 绑定指令
        if msg == "!obreg":
            event.stop_event()
            if current_bot_id == "unknown":
                # 如果还是获取不到，尝试最后一招：从 UMO 中截取（通常是 platform:id）
                current_bot_id = curr_session.split(':')[0] if ':' in curr_session else "unknown"
            self.config_data[current_bot_id] = curr_session
            # 持久化存储
            os.makedirs(self.config_dir, exist_ok=True)
            with open(self.config_file, 'w') as f:
                json.dump(self.config_data, f)
            await self.context.send_message(curr_session, MessageChain().message(
                f"✅ 机器人 [{current_bot_id}] 绑定成功！\n"
                f"当前接收终端: {curr_session}"
            ))
            return

        # --- [权限锁] ---
        bound_target = self.config_data.get(current_bot_id)
        if not bound_target or curr_session != bound_target:
            # logging.debug(f"拒绝访问: {curr_session} 试图访问 Bot {current_bot_id}")
            return

        # 2. 添加任务 !obadd <自然语言>
        if msg.startswith("!obadd"):
            event.stop_event()
            raw = event.message_str.strip()[len("!obadd"):].strip()
            if not raw:
                await self.context.send_message(curr_session, MessageChain().message(
                    "用法示例：\n"
                    "!obadd 明天修手表\n"
                    "!obadd 2026年6月23日买菜\n"
                    "!obadd 下周二看病"
                ))
                return
            await self._add_task(curr_session, raw)
            return

        # 3. 自然语言添加任务：加个任务 / 加个待办 / 加任务 / 加待办
        _add_keywords = ["加个任务", "加个待办", "加任务", "加待办"]
        for _pat in _add_keywords:
            _idx = msg.find(_pat)
            if 0 <= _idx <= 1:
                event.stop_event()
                _add_raw = event.message_str.strip()[_idx + len(_pat):]
                if _add_raw and _add_raw[0] in ' ，,、：:;；\t':
                    _add_raw = _add_raw[1:]
                _add_raw = _add_raw.strip()
                if _add_raw:
                    await self._add_task(curr_session, _add_raw)
                else:
                    await self.context.send_message(curr_session, MessageChain().message(
                        "用法示例：\n加个任务明天修手表\n加个待办买菜\n加任务下周二看病"
                    ))
                return

        # 4. 自然语言手动查询
        core_tasks = ["任务", "待办", "todo", "事项", "安排", "要办","要忙的","有什么事","有啥事"]
        # 2) 辅助询问词
        query_words = ["今天", "今日", "什么", "有哪些", "查下", "看下", "查"]
        # 3) 排除词黑名单（防止误伤你的日期计算或天气询问）
        negative_words = ["天气", "为什么", "计算", "到底", "可能", "原因", "认为", "分析",
                           "加个任务", "加个待办", "加任务", "加待办"]
        # 逻辑判断：
        # 条件 A: 包含核心词（如：任务、待办）
        has_core = any(c in msg for c in core_tasks)
        # 条件 B: 包含“今天/什么/看”等词，且消息非常短（典型指令特征）
        is_short_cmd = any(q in msg for q in query_words) and len(msg) <= 5
        # 条件 C: 排除黑名单
        not_negative = not any(n in msg for n in negative_words)
        # 最终组合：(有核心词 OR 是超短查询) AND 不在黑名单内
        is_query = has_core and (any(q in msg for q in query_words)) and not_negative
        
        # 3. 命中查询意图，直接调用封装好的推送函数
        if is_query:
            event.stop_event() # 拦截，不让 LLM 说话
            if not self.config_data and msg != "!obreg": return
            # mode='manual' 会触发今日+过期的融合列表，但不包含明天的预告
            await self.scheduled_push(mode='manual')
            
        # 4. 增加一个特殊指令用于手动测试复盘
        if ("复盘" in msg or "进度" in msg) and len(msg) < 10:
            event.stop_event()
            if not self.config_data and msg != "!obreg": return
            
            # 搜集数据
            all_tasks = self.scan_overdue(days_back=7) + self.scan_tasks(datetime.now().strftime("%Y-%m-%d"))
            if not all_tasks:
                await self.context.send_message(curr_session, MessageChain().message("这周任务全清了，你小子可以啊！"))
                return
            
            # 获取模型并生成
            umo = event.unified_msg_origin
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            context_str = "\n".join([f"- 项目:[{t['project']}] 任务:{t['text']} 标签:{'/'.join(t['tags'])}" for t in all_tasks])
            prompt = self.review_prompt.rstrip("\n") + "\n\n本周未完成任务清单：\n" + context_str
            llm_resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
            if llm_resp:
                await self.context.send_message(curr_session, MessageChain().message(f"📊 **实时一周复盘**\n\n{llm_resp.completion_text}"))
        
        # 5. 新闻测试指令 !news 
        if msg == "!news":
            event.stop_event()
            await self.context.send_message(curr_session, MessageChain().message("正在为你收集资讯，请稍候..."))
            asyncio.create_task(self.news_scout_task(target_umo=curr_session)) # 直接触发下面的任务函数
            return

    async def send_to_authorized_bots(self, chain, only_to_umo=None):
        if not self.config_data:
            logging.warning("[Obsidian Reminder] config_data 为空，放弃推送")
            return
        if only_to_umo:
            try:
                await self.context.send_message(only_to_umo, chain)
                logging.info(f"🎯 已定向回复至: {only_to_umo}")
            except Exception as e:
                logging.error(f"❌ 定向回复失败: {e}")
            return
        for bot_id, target_id in self.config_data.items():
            try:
                # 核心逻辑：构造一个虚拟的 UMO，强制 AstrBot 路由到对应的终端
                # target_id 就是你日志里看到的 o9cq80... 那个长字符串
                await self.context.send_message(target_id, chain)
                logging.info(f"✅ 已请求核心层推送至: {target_id} (来自 Bot: {bot_id})")
                # 多个目标之间稍作停顿
                await asyncio.sleep(random.uniform(1.0, 3.0))
                logging.info(f"✅ 推送成功: {target_id}")
            except Exception as e:
                logging.error(f"❌ 推送失败 {target_id}: {e}")
                    
    async def weekly_review_cron(self):
        """每周日晚 21:00 触发的自动复盘"""
        if not self.config_data: return
        
        logging.info("--- [Obsidian Reminder] 正在执行周日智能复盘 ---")
        
        # 1. 汇总过去 7 天和今天的任务
        overdue_tasks = self.scan_overdue(days_back=7)
        today_tasks = self.scan_tasks(datetime.now().strftime("%Y-%m-%d"))
        all_tasks = today_tasks + overdue_tasks
        if not all_tasks:
            return #（如果没任务就不打扰了） "这周任务全清了，你小子可以啊！"

        try:
            # 2. 获取 Provider ID
            # 在没有 event 的情况下，我们直接获取插件上下文绑定的默认模型 ID
            providers = await self.context.get_all_providers()
            if not providers: return
            provider_id = providers[0].id # 使用第一个可用的模型

            # 3. 构造素材和 Prompt (复用之前的逻辑，模板可配置)
            context_str = "\n".join([
                f"- 项目:[{t['project']}] 任务:{t['text']} 标签:{'/'.join(t['tags'])}"
                for t in all_tasks
            ])
            prompt = self.review_prompt.rstrip("\n") + "\n\n本周未完成任务清单：\n" + context_str

            # 4. 调用 AI
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )

            if llm_resp:
                report = f"📊 **一周复盘**\n\n{llm_resp.completion_text}"
                await self.send_to_authorized_bots(MessageChain().message(report))
                
        except Exception as e:
            logging.error(f"[Obsidian Reminder] LLM 调用失败: {e}")
            return "（AI复盘暂时不可用，但看清单你这周挺忙的，注意休息吧！）"

    def clean_markdown(self, text):
        """移除 Markdown 符号"""
        import re
        text = re.sub(r'[*#`_~>-]', '', text)
        #text = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', text)
        return text.strip()
            
    async def news_scout_task(self, target_umo=None):
        """核心情报处理：遍历所有新闻分组，按各组模式分别处理推送"""
        if not self.config_data:
            logging.error("[NewsScout] 尚未绑定 UMO，请先发送 !obreg")
            return
        if not self.news_groups:
            if target_umo:
                await self.context.send_message(target_umo, MessageChain().message("⚠️ 未配置新闻分组，请先在 WebUI 设置新闻源。"))
            return

        query_umo = target_umo if target_umo else next(iter(self.config_data.values()), None)
        if not query_umo:
            logging.error("[NewsScout] 配置库为空，无法获取 UMO 以查询 Provider")
            return
        p_id = await self.context.get_current_chat_provider_id(umo=query_umo)
        if not p_id:
            logging.error(f"[NewsScout] 无法为 UMO {query_umo} 获取到 Provider ID")
            return

        now_hour = datetime.now().hour
        time_label = "🌅 早间" if 5 <= now_hour < 12 else ("🌙 晚间" if now_hour >= 18 else "☕ 午后")

        for idx, group in enumerate(self.news_groups):
            group_name = group["name"]
            group_mode = group["mode"]
            sources = group["sources"]

            logging.info(f"[NewsScout] 处理分组 [{idx+1}/{len(self.news_groups)}]「{group_name}」({group_mode})")

            try:
                report = await NewsScout.process_group(
                    name=group_name, sources=sources,
                    mode=group_mode, context=self.context,
                    provider_id=p_id if group_mode == "llm" else None,
                    prompt_template=self.news_prompt if group_mode == "llm" else None,
                )
            except Exception as e:
                logging.error(f"[NewsScout] 分组「{group_name}」处理异常: {e}")
                continue

            if not report:
                logging.warning(f"[NewsScout] 分组「{group_name}」未产生报告")
                if idx == 0 and target_umo:
                    await self.context.send_message(
                        target_umo,
                        MessageChain().message(f"⚠️「{group_name}」暂无内容。可能是源站网络波动。"),
                    )
                continue

            # 在分组之间添加分隔延迟
            if idx > 0:
                await asyncio.sleep(random.uniform(3.0, 5.0))

            if group_mode == "llm":
                await self._send_llm_report(target_umo, report, time_label)
            else:
                await self._send_raw_report(target_umo, report, time_label)

    async def _send_llm_report(self, target_umo, raw_report, time_label):
        """发送 LLM 提炼报告（带智能分段+随机延迟）"""
        # 1. 构建标题
        title = f"{time_label}资讯汇总"

        # 2. 预处理：清洗 Markdown 后分段
        clean_full = self.clean_markdown(raw_report)
        final_paragraphs = [title]
        current_chunk = ""
        lines = clean_full.split('\n')
        for line in lines:
            stripped_line = line.strip()
            is_empty_line = not stripped_line
            is_new_topic = stripped_line.startswith('##')
            is_too_long = len(current_chunk) > 100
            is_critical_long = len(current_chunk) > 150 and any(p in current_chunk[-5:] for p in "。！？")
            if (is_empty_line or is_new_topic or is_too_long or is_critical_long) and current_chunk.strip():
                final_paragraphs.append(current_chunk.strip())
                current_chunk = ""
                if is_new_topic:
                    current_chunk = stripped_line + "\n"
                continue
            if stripped_line:
                current_chunk += stripped_line + "\n"
        if current_chunk.strip():
            final_paragraphs.append(current_chunk.strip())

        # 3. 发送（带随机延迟）
        logging.info(f"[NewsScout] LLM 报告分段: {len(final_paragraphs)} 段")
        for i, para in enumerate(final_paragraphs):
            if i == 0:
                delay = random.uniform(5.0, 8.0)
            else:
                delay = 2.5 + min(len(para) / 10 * 0.5, 5.5)
                delay += random.uniform(0, 2.0)
            await asyncio.sleep(delay)
            chain = MessageChain().message(para)
            await self.send_to_authorized_bots(chain, only_to_umo=target_umo)
            logging.info(f"[NewsScout] 段落 {i+1} 已推送 ({len(para)}字)")

    async def _send_raw_report(self, target_umo, raw_report, time_label):
        """发送原文链接报告（直接推送，不分段）"""
        final = f"{time_label}速览\n\n{raw_report}"
        logging.info(f"[NewsScout] 原文报告: {len(final)} 字")
        await asyncio.sleep(random.uniform(2.0, 4.0))
        chain = MessageChain().message(final)
        await self.send_to_authorized_bots(chain, only_to_umo=target_umo)
        logging.info("[NewsScout] 原文报告已推送")

    async def _add_task(self, session, raw_text):
        """解析文本并写入 Obsidian 任务。被 !obadd 和自然语言触发共用。"""
        if not self.vault_path:
            await self.context.send_message(session, MessageChain().message(
                "❌ 请先在 WebUI 配置页面设置「Obsidian 笔记库路径」"
            ))
            return
        # 提取文档指定符 [xxx] 并清洗文本
        clean_text, doc_path = self._extract_doc_spec(raw_text)
        if len(clean_text) <= 1:
            await self.context.send_message(session, MessageChain().message(
                "❌ 没识别到任务内容，例：!obadd 明天修手表"
            ))
            return
        task_date, task_text = ChineseDateParser.parse(clean_text)
        if not task_text:
            await self.context.send_message(session, MessageChain().message(
                "❌ 没识别到任务内容，例：!obadd 明天修手表"
            ))
            return
        try:
            if self.task_format == "dataview":
                if task_date:
                    line = f"- [ ] #task {task_text}  [due:: {task_date.isoformat()}]"
                    date_hint = f"📅 {task_date.isoformat()}"
                else:
                    line = f"- [ ] #task {task_text}"
                    date_hint = "📅 未指定日期"
            else:
                if task_date:
                    line = f"- [ ] #task {task_text} 📅 {task_date.isoformat()}"
                    date_hint = f"📅 {task_date.isoformat()}"
                else:
                    line = f"- [ ] #task {task_text}"
                    date_hint = "📅 未指定日期"
            # 确定文件路径
            if self.task_mode == "docs":
                if doc_path:
                    file_path = os.path.join(self.vault_path, doc_path)
                elif self.task_docs:
                    file_path = os.path.join(self.vault_path, self.task_docs[0]["path"])
                else:
                    file_path = self._resolve_task_file(task_date)
            else:
                file_path = self._resolve_task_file(task_date)
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            # 如果文件存在且不以换行结尾，先补一个换行再追加
            if os.path.exists(file_path):
                with open(file_path, 'rb') as f:
                    f.seek(0, 2)  # 跳到末尾
                    if f.tell() > 0:
                        f.seek(-1, 2)
                        if f.read(1) != b'\n':
                            with open(file_path, 'a', encoding='utf-8') as f2:
                                f2.write('\n')
            with open(file_path, 'a', encoding='utf-8') as f:
                f.write(line + "\n")
            await self.context.send_message(session, MessageChain().message(
                f"✅ 已添加任务\n📁 {os.path.relpath(file_path, self.vault_path)}\n"
                f"{date_hint}\n{line}"
            ))
        except Exception as e:
            logging.error(f"[_add_task] 写入失败: {e}")
            await self.context.send_message(session, MessageChain().message(
                f"❌ 写入失败: {e}"
            ))

    def _extract_doc_spec(self, raw_text):
        """从文本末尾提取 [文档指定符]，返回 (清洗后文本, 匹配的path或None)。"""
        text = raw_text.strip()
        if self.task_mode != "docs" or not self.task_docs:
            return text, None
        m = re.search(r'\[([^\]]+)\]$', text)
        if not m:
            return text, None
        spec = m.group(1).strip()
        # 按触发词匹配（未设置触发词时自动用文件名代替）
        for doc in self.task_docs:
            if spec == doc["trigger"]:
                return text[:m.start()].strip(), doc["path"]
        return text, None

    def _resolve_task_file(self, task_date):
        """根据 task_file 模板和日期解析出完整路径。task_date 为 None 时使用今天。"""
        from datetime import date
        d = task_date if task_date else date.today()
        relative = self.task_file_template.replace("{{date}}", d.isoformat())
        return os.path.join(self.vault_path, relative)