import os, re, json, logging, random, asyncio, feedparser, requests
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler 
from astrbot.api.event import filter, MessageChain, AstrMessageEvent
from astrbot.api.star import Context, Star, register

class NewsScout:
    def __init__(self, sources: dict = None):
        if sources:
            self.sources = sources
        else:
            self.sources = {
                "FT中文": "http://www.ftchinese.com/rss/feed",
                "WSJ中文": "https://cn.wsj.com/zh-hans/rss",
                "联合早报": "https://rsshub.app/zaobao/realtime/china",
                "财新网": "https://rsshub.rssforever.com/caixin/latest",
                "华尔街见闻": "https://rsshub.rssforever.com/wallstreetcn/news",
                "Solidot": "https://www.solidot.org/index.rss",
                "BBC": "https://feeds.bbci.co.uk/news/rss.xml"
            }
    async def get_curated_report(self, context, provider_id):
        raw_data = []
        import socket
        socket.setdefaulttimeout(10)

        for name, url in self.sources.items():
            try:
                logging.info(f"[NewsScout] 正在尝试解析: {name}...")
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                }
                try:
                    resp = requests.get(url, headers=headers, timeout=12)
                    resp.raise_for_status()
                    feed = feedparser.parse(resp.text)
                except Exception as proxy_err:
                    logging.warning(f"⚠️ 抓取 {name} 失败，尝试直连... ")
                    feed = feedparser.parse(url)

                if feed.entries:
                    count = 0
                    for entry in feed.entries[:10]:
                        content_raw = entry.get('summary', entry.get('description', ''))
                        summary = re.sub(r'<[^>]+>', '', content_raw)[:80]
                        raw_data.append(f"[{name}] {entry.title}: {summary.strip()}")
                        count += 1
                    logging.info(f"✅ {name} 成功获取 {count} 条资讯")
                else:
                    logging.warning(f"⚠️ {name} 所有尝试均未获取到内容")

            except Exception as e:
                logging.error(f"❌ {name} 过程异常: {e}")
                continue

        if not raw_data: 
            return "暂时没有抓取到新资讯。可能是源站网络波动，建议稍后再试 (face14)"

        prompt = (
            "你是一位冷静、高效的资深编辑。以下是过去 12 小时的全球情报素材（含英文）：\n"
            f"{chr(10).join(raw_data)}\n\n"
            "请完成：\n"
            "- 语义去重与新闻整合：合并不同来源对同一大事的报道，英文内容请直接翻译要点。（这部分无须直接反馈给我）\n"
            "- 要闻精选与内容提炼：基于这些报道，选出 5 条对中国市场、全球政治或全球科技有重大影响的消息，每条新闻用一句话补充其要点并说明它为何重要（不超过100字），注明新闻来源。"
        )

        resp = await context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
        return resp.completion_text if resp else "提炼失败。"


def _parse_time(time_str: str):
    """Parse 'HH:MM' string into (hour, minute) ints."""
    try:
        parts = time_str.strip().split(":")
        return int(parts[0]), int(parts[1])
    except Exception:
        return 0, 0

@register("obsidian_reminder", "quasarrise", "扫描 Obsidian 待办并定时推送提醒，集成 RSS 新闻简报", "1.0.0")
class ObsidianReminder(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        cfg = config or {}

        # --- 插件配置（来自 WebUI）---
        self.vault_path = cfg.get("vault_path") or ""
        self.config_dir = "/AstrBot/data"
        self.config_file = f"{self.config_dir}/obsidian_config.json"

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

        # 新闻源（总是保存，新闻启用时才用）
        raw_sources = cfg.get("news_sources", [])
        self.news_sources = {}
        if raw_sources:
            for item in raw_sources:
                name = (item.get("name") or "").strip()
                url = (item.get("url") or "").strip()
                if name and url:
                    self.news_sources[name] = url

        # --- Bot 绑定配置（!obreg 管理）---
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
        logging.info("--- [Obsidian Task Reminder] v1.0.0 已启动 ---")

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
                if t_count + o_count == 0: return "今天暂时没啥要紧事，玩去吧。"
                greetings = ["看下当前的进度：", "今天还有这些事：", "今天的活儿：", "OK，这是还没处理的："]
            elif p_mode == 'morning':
                greetings = ["早啊！今天的活儿：", "咱们先把这几个搞定：", "起来了吗？今天得接着忙活这些："]
            elif p_mode == 'night':
                return "今天怎么样？顺便看一眼明天还有啥事："
            else:
                greetings = ["进度提醒：", "这些还没勾掉："]
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
        report_lines = [get_greeting(len(today_tasks), len(overdue_tasks), mode)]
        if all_tasks:
            for t in all_tasks:
             # prefix = "[今]" if t['is_today'] else "[延]"
                suffix = "" if t['is_today'] else f" ({t['date']})"
                # 优先级特别高的任务加个加粗
                p_label = f"**{t['priority'].upper()}**" if self.priority_map.get(t['priority']) >= 4 else t['priority'].upper()
                # report_lines.append(f"[{t['priority'].upper()}] {t['text']}{suffix}")
                report_lines.append(f"[{p_label}]{t['project']}:{t['text']}{suffix}")
        # 5. 明日预告逻辑 (保持精简)
        if mode == 'night':
            tomorrow_str = (today_date + timedelta(days=1)).strftime("%Y-%m-%d")
            tomorrow_tasks = self.scan_tasks(tomorrow_str)
            imp_tomorrow = [t for t in tomorrow_tasks if self.priority_map.get(t['priority'], 2) >= 4]
            if imp_tomorrow:
                report_lines.append("\n明天有这些事：")
                for t in imp_tomorrow:
                    report_lines.append(f"• {t['text']}")
        chain = MessageChain().message("\n".join(report_lines))
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

        # 2. 自然语言手动查询
        # 1) 核心意图词（没有这些词，大概率不是在查任务）
        core_tasks = ["任务", "待办", "todo", "事项", "安排", "要办","要忙的","有什么事","有啥事"]
        # 2) 辅助询问词
        query_words = ["今天", "今日", "什么", "有哪些", "查下", "看下", "查"]
        # 3) 排除词黑名单（防止误伤你的日期计算或天气询问）
        negative_words = ["天气", "为什么", "计算", "到底", "可能", "原因", "认为", "分析"]
        # 逻辑判断：
        # 条件 A: 包含核心词（如：任务、待办）
        has_core = any(c in msg for c in core_tasks)
        # 条件 B: 包含“今天/什么/看”等词，且消息非常短（典型指令特征）
        is_short_cmd = any(q in msg for q in query_words) and len(msg) <= 5
        # 条件 C: 排除黑名单
        not_negative = not any(n in msg for n in negative_words)
        # 最终组合：(有核心词 OR 是超短查询) AND 不在黑名单内
        is_query = (has_core or is_short_cmd) and not_negative
        
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
            prompt = f"你是一个专业的合作伙伴和靠谱的朋友。请分析以下本周未完成的任务：\n{context_str}\n 请以你的人格设定，结合项目名和标签，给出一个简短的进度压力分析和下周建议。不要列清单，直接给结论。限 100 字。"
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

            # 3. 构造素材和 Prompt (复用之前的逻辑)
            context_str = "\n".join([
                f"- 项目:[{t['project']}] 任务:{t['text']} 标签:{'/'.join(t['tags'])}"
                for t in all_tasks
            ])
            prompt = (
                f"你是一个专业的合作伙伴和靠谱的朋友。请分析以下本周未完成的任务：\n{context_str}\n"
                "请以你的人格设定，结合项目名和标签，给出一个简短的进度压力分析和下周建议。不要列清单，直接给结论。限 100 字。"
            )

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

    async def send_humanized_report(self, target_umo, raw_report):
        # 1. 自动生成标题
        now = datetime.now().hour
        title = "🌅 早间资讯汇总" if 5 <= now < 12 else "🌙 晚间资讯汇总"
        if 12 <= now < 18: title = "☕ 午后情报摘要"

        # 2. 预处理：先清洗 Markdown 还是后清洗？
        # 我们先清洗，这样字数统计才是最准确的（去掉 ** 等占位符）
        clean_full_text = self.clean_markdown(raw_report)
        final_paragraphs = [title] # 第一段永远是标题
        current_chunk = ""
        
        # 3. 逻辑分段：按行处理，精准控制字数
        lines = clean_full_text.split('\n')
        for line in lines:
            stripped_line = line.strip()
            # --- 强制分段条件判定 ---
            # 条件 A: 遇到了真正的空行 (连续换行)
            is_empty_line = not stripped_line 
            # 条件 B: 遇到了标题行 (以 ## 开头)
            is_new_topic = stripped_line.startswith('##')
            # 条件 C: 当前累积的内容已经足够长了 (100字换行 / 150字句尾)
            is_too_long = len(current_chunk) > 100
            is_critical_long = len(current_chunk) > 150 and any(p in current_chunk[-5:] for p in "。！？")
            if (is_empty_line or is_new_topic or is_too_long or is_critical_long) and current_chunk.strip():
                final_paragraphs.append(current_chunk.strip())
                current_chunk = ""
                # 如果是标题行，直接开始新的一块
                if is_new_topic:
                    current_chunk = stripped_line + "\n"
                continue
            if stripped_line:
                current_chunk += stripped_line + "\n"
        # 补漏：添加最后剩余的内容
        if current_chunk.strip():
            final_paragraphs.append(current_chunk.strip())
            
        # 4. 发送逻辑 (带随机延迟)
        logging.info(f"[NewsScout] 最终分段统计: {len(final_paragraphs)} 段")
        for i, para in enumerate(final_paragraphs):
            # 随机延迟策略
            # 第一段(标题)前摇久一点；短段落快发，长段落慢发
            if i == 0:
                delay = random.uniform(5.0, 8.0)
            else:
                # 基础 2.5 秒 + 每 10 个字增加 0.5 秒延迟，封顶 8 秒
                delay = 2.5 + min(len(para) / 10 * 0.5, 5.5)
                delay += random.uniform(0, 2.0) # 加入微小波动
            await asyncio.sleep(delay)
            # 执行发送
            chain = MessageChain().message(para)
            await self.send_to_authorized_bots(chain, only_to_umo=target_umo)
            logging.info(f"[NewsScout] 段落 {i+1} 已推送 ({len(para)}字)")

    def clean_markdown(self, text):
        """移除 Markdown 符号"""
        import re
        text = re.sub(r'[*#`_~>-]', '', text)
        #text = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', text)
        return text.strip()
            
    async def news_scout_task(self, target_umo=None):
        """核心情报处理函数：由定时器或 !news 指令触发"""
        if not self.config_data: 
            logging.error("[NewsScout] 尚未绑定 UMO，请先发送 !obreg")
            return
        
        try:
            # 1. 获取 LLM 模型
            # 必须传入 umo 参数，这样 AstrBot 才知道该会话当前关联的是哪个模型
            query_umo = target_umo if target_umo else next(iter(self.config_data.values()), None)
            if not query_umo:
                logging.error("[NewsScout] 配置库为空，无法获取 UMO 以查询 Provider")
                return
            p_id = await self.context.get_current_chat_provider_id(umo=query_umo)
            if not p_id:
                logging.error(f"[NewsScout] 无法为 UMO {query_umo} 获取到 Provider ID。请确保该账号已在 AstrBot 中配置模型。")
                return
            
            # 2. 实例化 NewsScout 并抓取提炼
            scout = NewsScout(sources=self.news_sources if self.news_sources else None)
            report = await scout.get_curated_report(self.context, p_id)
            if report:
                await self.send_humanized_report(target_umo, report) 
            else:
                # 至少给个回音
                await self.context.send_message(target_umo, MessageChain().message("⚠️ 抓取失败或今日无新闻更新。"))
                
        except Exception as e:
            logging.error(f"[NewsScout] 任务失败: {e}")
            if target_umo: # 仅在主动查询时回传错误
                await self.context.send_message(target_umo, MessageChain().message(f"❌ 运行出错: {str(e)}"))