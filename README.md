# astrbot_plugin_obsidian_reminder

Obsidian Reminder —— 扫描 Obsidian 笔记库中的待办任务，按优先级定时推送提醒；集成多源 RSS 新闻简报（AI 精选 / 原文整理）。

- 版本：**v1.2.0**
- 作者：quasarrise
- 依赖：`feedparser`、`requests`、`apscheduler`

## 功能特性

- **任务定时推送**：早 / 午 / 晚三个时段（`morning_push_time` / `noon_push_time` / `night_push_time`），按优先级 + 是否超期渲染任务清单，晚间附次日重要任务预告。
- **周复盘**：`review_day` + `review_time` 都填写时，每周定时自动复盘未完成任务（LLM 生成进度压力分析与下周建议）。聊天里说「复盘」/「进度」随时手动触发。
- **任务写入 Obsidian**：聊天里用中文自然语言加任务，自动解析日期并写入指定笔记。
- **RSS 新闻简报**：多分组 / 多源，`llm` 模式由 AI 提炼精选，`raw` 模式直接整理标题 + 链接（零 LLM 开销）。

## 安装

把本目录拷入 AstrBot 的 `data/plugins/` 下（目录名保持 `astrbot_plugin_obsidian_reminder`）：

```bash
cd /path/to/Astrbot/data/plugins
git clone git@github.com:quasarrise/astrbot_plugin_obsidian_reminder.git
```

然后在 WebUI 的插件管理里加载（或重启 AstrBot）。代码改动后可用 AstrBot 的「重载」命令热更新，无需重启容器。

## 快速开始

1. 在 WebUI 插件设置页填好 **「Obsidian 笔记库路径」**（`vault_path`）——这是任务功能的**总开关**，留空则全部任务扫描与推送关闭。
2. 至少填一个推送时段（如早 `08:30`）。
3. 向机器人发送 **`!obreg`** 绑定当前会话（权限锁：插件只响应已绑定会话的指令）。
4. 完事。到点会自动推送。

## 聊天指令

| 指令 | 说明 |
|------|------|
| `!obreg` | 绑定当前会话（首次必做；插件只响应绑定过的会话） |
| `!obadd <自然语言>` | 添加任务，如 `!obadd 明天修手表` |
| `加个任务/加个待办/加任务/加待办 <内容>` | 自然语言加任务（同 !obadd） |
| `今天有什么任务` / `看下任务` 等 | 手动查询当前任务（命中「任务/待办」类关键词 + 短指令语义） |
| `复盘`、`进度` | 手动触发一次实时周复盘 |
| `!news` | 手动触发新闻简报（立即收集全部分组并推送） |

### 中文日期解析示例

`!obadd` 会自动从自然语言中提取日期与任务文本，日期可在句首或句尾，与内容间可无间隔、空格或中英文逗号：

```
!obadd 明天修手表
!obadd 买菜2026年6月23日
!obadd 下周二看病
!obadd 7月4日交稿
!obadd 25号交稿
```

支持的日期格式：`YYYY年M月D日` / `YYYY-MM-DD` / `YYYYMMDD` / `今天` / `明天` / `后天` / `大后天` / `(这|本|下|下下)周X`（相对星期）/ `M月D日` / `N号` / `下个月N号`。未识别到日期则写入今天。日期后的内容段原样保留（含时间、标签等文字）。

## WebUI 配置项

| 字段 | 说明 | 留空 |
|------|------|------|
| `vault_path` | Obsidian 笔记库路径 | 关闭全部任务功能 |
| `morning/noon/night_push_time` | 早 / 午 / 晚任务推送 `HH:MM` | 该时段不推送 |
| `review_day` | 周复盘日（mon...sun） | 复盘关闭 |
| `review_time` | 周复盘时间 `HH:MM` | 复盘关闭 |
| `news_enabled` | 新闻推送总开关 | off |
| `morning_news_time` / `night_news_time` | 早 / 晚新闻推送 `HH:MM` | 该时段不推送 |
| `news_groups` | 新闻分组（最多 3 组），每组可配多个 RSS 源 | 无分组不推送 |
| `task_file` | 日任务文档路径模板，`{{date}}` 替换为日期 | `{{date}}.md` |
| `task_mode` | 任务写入模式：`daily` / `docs` | daily |
| `task_format` | 任务记录格式：`emoji`(Tasks) / `dataview` | emoji |
| `task_docs` | 任务写入目标文档列表，可配触发词 | 用 task_file 路径 |
| `news_prompt` | 新闻 LLM 提炼提示词模板（v1.2.0） | 预填默认，可自定义 |
| `review_prompt` | 周复盘 LLM 提示词模板（v1.2.0） | 预填默认，可自定义 |

> **新闻分组**：`新闻分组` 下每条 `sources_text` 每行一个源，格式 `名称|网址`（`#` 开头的行忽略）。`mode` 选 `llm`（AI 提炼）或 `raw`（原文链接整理）。示例：

```
FT中文|http://www.ftchinese.com/rss/feed
WSJ中文|https://cn.wsj.com/zh-hans/rss
Solidot|https://www.solidot.org/index.rss
```

> **LLM 提示词模板**（`news_prompt` / `review_prompt`）：提示词是**纯指令**，无需占位符——采集到的新闻素材 / 本周任务清单会由插件**自动拼到结尾**。WebUI 输入框已预填内置默认模板（留空即用默认；也可改成你自己的指令）。建议在指令里提示模型"基于结尾列出的素材/清单"。

## 部署到 NAS（Docker）

```bash
# 已配好 ~/.ssh/config 的 Host nas（192.168.222.10）前提下：
scp -r main.py _conf_schema.json metadata.yaml nas:~/docker/astrbot/data/plugins/astrbot_plugin_obsidian_reminder/
ssh nas "cd ~/docker/astrbot && docker compose restart astrbot"

# 验证插件加载
ssh nas "cd ~/docker/astrbot && docker compose logs --tail=200 astrbot | grep 'Obsidian Task Reminder'"
```

启动日志出现 `[Obsidian Task Reminder] v1.2.0 已启动` 即加载成功。

## 版本历史

- **v1.2.0**：`news_prompt` / `review_prompt` 提示词改为 WebUI 可配置——纯指令模板、素材/任务清单自动拼尾、输入框预填默认（留空回退内置默认）；周复盘两处重复提示词合并；补录自 v1.0.0 以来的功能基线。
- **v1.0.0**：基线版本（任务扫描/推送、周复盘、新闻简报、中文日期解析、任务写入）。