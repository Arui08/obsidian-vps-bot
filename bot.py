"""TG Bot 主程序：监听消息 → 识别链接 → AI 摘要 → 写入 Obsidian → git推送"""
import asyncio
import logging
import re
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

from common import (
    TG_BOT_TOKEN, TG_CHAT_ID, MODEL_FAST,
    ai_chat, ai_chat_audio, tg_send, git_pull, git_commit_push,
    write_note, safe_filename, today_str, now_str,
    extract_urls, fetch_url, fetch_github_repo, fetch_tweet,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot")

ALLOWED_CHAT = str(TG_CHAT_ID)


def is_allowed(update: Update) -> bool:
    """只接受指定用户的消息"""
    if not update.effective_chat:
        return False
    return str(update.effective_chat.id) == ALLOWED_CHAT


def detect_link_type(url: str) -> str:
    if "github.com" in url:
        return "github"
    if re.search(r"(?:twitter\.com|x\.com)/[^/]+/status/\d+", url):
        return "tweet"
    if "mp.weixin.qq.com" in url:
        return "wechat"
    if any(d in url for d in ["youtube.com", "youtu.be", "bilibili.com", "b23.tv"]):
        return "video"
    return "article"


def summarize_github(url: str) -> tuple:
    """返回 (filename, markdown内容, TG消息)"""
    info = fetch_github_repo(url)
    if not info.get("readme") and not info.get("description"):
        return None, None, None

    prompt = f"""你是技术内容整理助手。请分析下面的GitHub仓库，输出中文笔记，格式严格按以下markdown：

## 项目简介
（一句话说清楚是做什么的，30字内）

## 核心功能
- 列出3-5条关键能力

## 技术栈
（涉及的主要语言/框架/技术）

## 适用场景
（什么人在什么情况会用）

## 亮点/独特性
（这个项目相比同类有什么不一样的地方，2-3点）

仓库信息：
- 名称：{info.get('full_name')}
- 描述：{info.get('description')}
- Stars：{info.get('stars')}
- 主语言：{info.get('language')}
- Topics：{', '.join(info.get('topics', []))}

README（已截断）：
{info.get('readme', '')[:15000]}
"""
    summary = ai_chat(prompt, model=MODEL_FAST, max_tokens=2000)

    title = info.get("full_name", url)
    fname = safe_filename(f"{today_str()}_GH_{title.replace('/', '_')}")

    md = f"""---
type: github
url: {info.get('url', url)}
stars: {info.get('stars', 0)}
language: {info.get('language', '')}
topics: {info.get('topics', [])}
created: {now_str()}
tags: [github, inbox]
---

# {title}

> {info.get('description', '')}

🔗 {info.get('url', url)}
⭐ {info.get('stars', 0)} | 🛠 {info.get('language', '')}

{summary}
"""

    tg_msg = f"📦 已收录 GitHub：{title}\n⭐ {info.get('stars', 0)} | 🛠 {info.get('language', '')}\n\n{summary[:600]}"
    return fname, md, tg_msg


def summarize_article(url: str) -> tuple:
    """返回 (filename, markdown, TG消息)"""
    raw = fetch_url(url)
    if "抓取失败" in raw[:30]:
        return None, None, raw

    prompt = f"""你是内容整理助手。下面是一篇网页正文，请整理成中文笔记：

## 一句话总结
（不超过30字）

## 关键观点
- 列出3-6个核心观点

## 值得记住的细节
- 列2-4条具体信息（数据、案例、引用）

## 我的延伸思考方向
- 提2点可以进一步研究/应用的方向

正文：
{raw[:20000]}
"""
    summary = ai_chat(prompt, model=MODEL_FAST, max_tokens=2000)

    first_line = raw.split("\n", 1)[0].lstrip("# ").strip()[:60] or "未命名文章"
    fname = safe_filename(f"{today_str()}_文章_{first_line}")

    md = f"""---
type: article
url: {url}
created: {now_str()}
tags: [article, inbox]
---

# {first_line}

🔗 {url}

{summary}
"""
    tg_msg = f"📰 已收录文章：{first_line}\n\n{summary[:600]}"
    return fname, md, tg_msg


def summarize_video(url: str) -> tuple:
    """视频暂不抓字幕，先做URL+title简化处理"""
    raw = fetch_url(url)
    title = raw.split("\n", 1)[0].lstrip("# ").strip()[:80] or "视频"

    prompt = f"""下面是一个视频页面的标题和描述，请基于这些信息：
1. 用一句话推断视频主题
2. 列出可能的看点
3. 标注适合什么人看

页面信息：
{raw[:5000]}
"""
    summary = ai_chat(prompt, model=MODEL_FAST, max_tokens=1500)

    fname = safe_filename(f"{today_str()}_视频_{title}")
    md = f"""---
type: video
url: {url}
created: {now_str()}
tags: [video, inbox]
---

# {title}

🔗 {url}

{summary}

> 注：视频字幕未抓取，以上为基于标题/描述的推测。需要详细笔记请观看后手动补充。
"""
    tg_msg = f"🎬 已收录视频：{title}\n\n{summary[:500]}"
    return fname, md, tg_msg


def summarize_tweet(url: str) -> tuple:
    """X/Twitter 推文：fxtwitter 抓原文 → AI 整理"""
    tw = fetch_tweet(url)
    if not tw or not tw.get("text"):
        return None, None, f"抓取推文失败: {url}"

    media_lines = ""
    if tw.get("media"):
        media_lines = "\n".join(
            f"- {m['type']}: {m['url']}" for m in tw["media"]
        )

    quote_block = f"\n\n引用推文：\n{tw['quote']}" if tw.get("quote") else ""

    prompt = f"""下面是一条 X (Twitter) 推文。请整理成中文笔记：

## 一句话提炼
（30字内说清楚作者在讲什么）

## 关键信息
- 列 2-4 条具体观点/信息/数据

## 我的延伸思考
- 1-2 点：这条推文背后的趋势/可以应用的场景

如果原文是中文就保留原意；如果是英文请翻译关键句。不要整篇翻译，提炼为主。

作者：{tw['author_name']} (@{tw['author_handle']})
发布时间：{tw['created_at']}
互动：{tw.get('likes',0)} 赞 / {tw.get('retweets',0)} 转 / {tw.get('replies',0)} 评

推文原文：
{tw['text']}{quote_block}
"""
    summary = ai_chat(prompt, model=MODEL_FAST, max_tokens=1500)

    short = tw["text"].replace("\n", " ")[:40] or "推文"
    fname = safe_filename(f"{today_str()}_推文_{tw['author_handle']}_{short}")

    quote_md = f"### 引用\n{tw['quote']}" if tw.get("quote") else ""
    media_md = f"## 媒体\n{media_lines}" if media_lines else ""

    md = f"""---
type: tweet
url: {tw['url']}
author: "{tw['author_name']} (@{tw['author_handle']})"
created: {now_str()}
posted_at: {tw['created_at']}
likes: {tw.get('likes',0)}
retweets: {tw.get('retweets',0)}
tags: [tweet, inbox]
---

# {tw['author_name']} 的推文

> {tw['author_name']} (@{tw['author_handle']})  ·  {tw['created_at']}
> 🔗 {tw['url']}

## 原文
{tw['text']}
{quote_md}

{media_md}

---

{summary}
"""
    tg_msg = f"🐦 已收录推文：@{tw['author_handle']}\n\n{summary[:600]}"
    return fname, md, tg_msg


def summarize_wechat(url: str) -> tuple:
    """微信公众号文章：复用 fetch_url（已对公众号做了正文识别）"""
    raw = fetch_url(url)
    if "抓取失败" in raw[:30]:
        return None, None, raw

    lines = raw.split("\n", 3)
    title = lines[0].lstrip("# ").strip()[:60] or "公众号文章"
    author = ""
    for line in lines[:4]:
        if line.startswith("作者："):
            author = line.replace("作者：", "").strip()
            break

    prompt = f"""你是内容整理助手。下面是一篇微信公众号文章正文，请整理成中文笔记：

## 一句话总结
（不超过30字）

## 文章脉络
（用 3-5 个小标题概括文章结构，每个小标题下用一两句话说核心）

## 关键观点
- 列出3-6个核心观点

## 值得记住的细节
- 列2-4条具体信息（数据、案例、引用）

## 我的延伸思考方向
- 提2点可以进一步研究/应用的方向

正文：
{raw[:25000]}
"""
    summary = ai_chat(prompt, model=MODEL_FAST, max_tokens=2500)

    fname = safe_filename(f"{today_str()}_公众号_{author}_{title}" if author else f"{today_str()}_公众号_{title}")

    md = f"""---
type: wechat
url: {url}
author: "{author}"
created: {now_str()}
tags: [wechat, article, inbox]
---

# {title}

> {author} · 微信公众号
> 🔗 {url}

{summary}
"""
    author_line = f"作者：{author}\n" if author else ""
    tg_msg = f"📱 已收录公众号文章：{title}\n{author_line}\n{summary[:600]}"
    return fname, md, tg_msg


def process_url(url: str) -> str:
    """处理单个URL，返回反馈给TG的消息"""
    log.info(f"处理: {url}")
    try:
        kind = detect_link_type(url)
        if kind == "github":
            fname, md, tg_msg = summarize_github(url)
        elif kind == "tweet":
            fname, md, tg_msg = summarize_tweet(url)
        elif kind == "wechat":
            fname, md, tg_msg = summarize_wechat(url)
        elif kind == "video":
            fname, md, tg_msg = summarize_video(url)
        else:
            fname, md, tg_msg = summarize_article(url)

        if not fname:
            return f"⚠️ 处理失败：{url}\n{tg_msg or ''}"

        git_pull()
        write_note("Inbox", fname, md)
        ok = git_commit_push(f"inbox: {fname}")
        return f"{tg_msg}\n\n💾 已存到 Inbox/{fname}.md{'  ✅同步' if ok else ' ⚠️本地保存(同步失败)'}"
    except Exception as e:
        log.exception("处理失败")
        return f"❌ 处理出错：{url}\n{e}"


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "👋 Obsidian 助手已就绪\n\n"
        "📌 用法：\n"
        "• 发链接，自动归档到 Inbox：\n"
        "   - GitHub 仓库\n"
        "   - X/Twitter 推文\n"
        "   - 微信公众号文章\n"
        "   - 普通网页文章\n"
        "   - YouTube/B站视频（仅标题描述）\n"
        "• 直接发文字 = 快速笔记\n"
        "• 发语音消息 = AI 自动转写+整理（最长10分钟）\n"
        "• /ask <问题> 跟你的 vault 对话\n"
        "• /trending 立即抓 GitHub Trending\n"
        "• /weekly 立即生成本周复盘\n"
        "• /ping 测试连接"
    )


async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(f"✅ pong  {now_str()}")


async def cmd_trending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text("⏳ 抓取 Trending 中...")
    try:
        from trending import run_trending
        msg = await asyncio.to_thread(run_trending)
        await update.message.reply_text(msg or "✅ 完成")
    except Exception as e:
        await update.message.reply_text(f"❌ 失败：{e}")


async def cmd_weekly(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text("⏳ 生成周报中（可能需要 1-2 分钟）...")
    try:
        from weekly import run_weekly
        msg = await asyncio.to_thread(run_weekly)
        await update.message.reply_text(msg or "✅ 完成")
    except Exception as e:
        await update.message.reply_text(f"❌ 失败：{e}")


async def cmd_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/ask <问题>：在 vault 里检索 + AI 综合回答"""
    if not is_allowed(update):
        return
    question = " ".join(ctx.args).strip() if ctx.args else ""
    if not question:
        await update.message.reply_text(
            "用法：/ask <问题>\n例：/ask 我之前看过哪些 AI agent 相关的内容"
        )
        return
    await update.message.reply_text(f"⏳ 在 vault 里搜索：{question[:60]}")
    try:
        from ask import answer
        git_pull()
        reply = await asyncio.to_thread(answer, question)
        for chunk in [reply[i:i + 3800] for i in range(0, len(reply), 3800)]:
            await update.message.reply_text(chunk)
    except Exception as e:
        log.exception("ask 失败")
        await update.message.reply_text(f"❌ 失败：{e}")


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        log.info(f"忽略非授权消息 chat={update.effective_chat.id if update.effective_chat else '?'}")
        return

    text = (update.message.text or update.message.caption or "").strip()
    if not text:
        return

    urls = extract_urls(text)
    if urls:
        await update.message.reply_text(f"⏳ 检测到 {len(urls)} 个链接，处理中...")
        for url in urls:
            result = await asyncio.to_thread(process_url, url)
            await update.message.reply_text(result[:4000])
        return

    fname = safe_filename(f"{today_str()}_笔记_{text[:30]}")
    md = f"""---
type: note
created: {now_str()}
tags: [inbox, quick-note]
---

# {text[:60]}

{text}
"""
    git_pull()
    write_note("Inbox", fname, md)
    ok = git_commit_push(f"note: {fname}")
    await update.message.reply_text(f"📝 已记录{'  ✅' if ok else ' ⚠️未同步'}")


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """语音消息：下载ogg → Gemini 转写+整理 → 存 Inbox"""
    if not is_allowed(update):
        return
    voice = update.message.voice or update.message.audio
    if not voice:
        return

    duration = getattr(voice, "duration", 0) or 0
    mime = getattr(voice, "mime_type", "") or ""
    log.info(f"voice 收到: duration={duration}s mime={mime} file_id={voice.file_id}")

    if duration > 600:
        await update.message.reply_text(f"⚠️ 语音过长（{duration}秒），目前最长支持10分钟")
        return

    await update.message.reply_text(f"⏳ 收到语音 {duration}秒，处理中...")

    try:
        tg_file = await voice.get_file()
        audio_bytes = bytes(await tg_file.download_as_bytearray())
        log.info(f"voice 下载完成: {len(audio_bytes)} bytes, magic={audio_bytes[:4]!r}")

        try:
            with open("/tmp/last_voice.ogg", "wb") as f:
                f.write(audio_bytes)
        except Exception:
            pass

        import subprocess, tempfile
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as fin, \
             tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as fout:
            fin.write(audio_bytes)
            fin.flush()
            in_path, out_path = fin.name, fout.name
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", in_path, "-ac", "1", "-ar", "16000", out_path],
                check=True, capture_output=True, timeout=60,
            )
            with open(out_path, "rb") as f:
                audio_bytes = f.read()
            log.info(f"voice 转码后: {len(audio_bytes)} bytes mp3")
        finally:
            for p in (in_path, out_path):
                try:
                    Path(p).unlink()
                except Exception:
                    pass

        prompt = """这是一段中文语音消息。请你做两件事，按以下格式输出：

## 转写原文
（一字一句的转写，保留口语用词，但去掉"嗯""啊""那个"这种无意义口头禅；如果有明显口误请按合理意思修正）

## 结构化笔记
（基于内容用 markdown 整理：用一句话总结主题，再列要点。如果是日记/感想就用"今日所感/反思"结构；如果是任务/想法就用"想法/下一步行动"结构；如果是知识记录就用"主题/要点"结构。自动判断该用哪种。）

## 标签
（给 3-5 个中文 hashtag，便于后续检索，比如 #工作 #灵感 #反思）"""

        result = await asyncio.to_thread(
            ai_chat_audio, prompt, audio_bytes, "mp3", MODEL_FAST, 3000
        )
        log.info(f"voice 转写结果前100字: {result[:100]}")

        first_line = ""
        for line in result.split("\n"):
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("-") and not line.startswith("*"):
                first_line = line
                break
        first_line = first_line[:30] or "语音笔记"

        fname = safe_filename(f"{today_str()}_语音_{first_line}")
        md = f"""---
type: voice
created: {now_str()}
duration: {duration}s
tags: [voice, inbox]
---

# 语音笔记 {now_str()}

> 时长 {duration}s

{result}
"""
        git_pull()
        write_note("Inbox", fname, md)
        ok = git_commit_push(f"voice: {fname}")
        await update.message.reply_text(
            f"🎤 已记录\n\n{result[:1500]}\n\n💾 Inbox/{fname}.md{'  ✅同步' if ok else ' ⚠️未同步'}"
        )
    except Exception as e:
        log.exception("语音处理失败")
        await update.message.reply_text(f"❌ 语音处理失败：{e}")


def main():
    log.info("启动 Obsidian Bot...")
    app = Application.builder().token(TG_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("trending", cmd_trending))
    app.add_handler(CommandHandler("weekly", cmd_weekly))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    log.info("Bot 监听中...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
