"""每周复盘：扫描vault本周新增/修改文件 → AI综合 → 写入Weekly + 发TG"""
import subprocess
from datetime import timedelta
from pathlib import Path

from common import (
    VAULT, MODEL_SMART, ai_chat,
    git_pull, git_commit_push, write_note, safe_filename,
    today_str, now_str, now_cn, tg_send,
)


def get_week_files(days: int = 7) -> list:
    """获取最近N天vault里新建或修改过的md文件"""
    since = now_cn() - timedelta(days=days)
    files = []
    for md in VAULT.rglob("*.md"):
        if any(p in md.parts for p in [".git", ".obsidian", ".trash"]):
            continue
        if "Weekly" in md.parts:
            continue
        try:
            mtime = md.stat().st_mtime
            if mtime >= since.timestamp():
                files.append(md)
        except OSError:
            continue
    return sorted(files, key=lambda p: p.stat().st_mtime)


def summarize_file(fp: Path) -> str:
    """读单个文件返回摘要展示"""
    try:
        content = fp.read_text(encoding="utf-8")
        rel = fp.relative_to(VAULT)
        return f"### {rel}\n```\n{content[:1500]}\n```\n"
    except Exception:
        return ""


def run_weekly():
    git_pull()

    files = get_week_files(7)
    if not files:
        msg = "⚠️ 本周 vault 没有新增/修改的笔记，无需生成周报"
        tg_send(msg)
        return msg

    bundle = "\n\n".join(summarize_file(f) for f in files)
    bundle = bundle[:80000]

    prompt = f"""你是这位用户的私人复盘教练。下面是本周 ({now_cn().strftime('%Y-%m-%d')} 之前7天) 在 Obsidian 里新建或修改的所有笔记。

请生成一份**中文周报**，markdown 格式，包含以下部分：

## 本周关注主题
（用 3-5 个关键词概括，背后简短解释）

## 知识吸收
（这周阅读/收藏了什么内容，按主题归类，提炼出每条的核心价值，避免简单罗列）

## 思维变化
（如果笔记里能看出观点变化或新认知，指出来）

## 值得深入的方向
（基于本周内容，建议接下来1-2个值得花时间钻研的方向，给出具体抓手）

## 一句话点评
（这周整体状态：是聚焦还是发散？方向清晰还是迷茫？）

要求：
- 不要客套话，直接进入分析
- 引用具体笔记标题时用 [[笔记标题]] 链接
- 字数 800-1500

笔记内容：
{bundle}
"""
    report = ai_chat(prompt, model=MODEL_SMART, max_tokens=4000)

    week_label = now_cn().strftime("%Y-W%W")
    file_index = "\n".join(f"- [[{f.stem}]]" for f in files)

    md = f"""---
type: weekly
week: {week_label}
date: {today_str()}
file_count: {len(files)}
created: {now_str()}
tags: [weekly, review]
---

# 第 {week_label} 周复盘

> 覆盖文件：{len(files)} 篇

{report}

---

## 本周涉及笔记
{file_index}
"""
    fname = safe_filename(f"{week_label}_周报")
    write_note("Weekly", fname, md)
    ok = git_commit_push(f"weekly: {fname}")

    tg_text = f"📊 本周复盘已生成\n📁 涵盖 {len(files)} 篇笔记\n\n{report}"
    tg_send(tg_text)

    return f"✅ 周报已生成 Weekly/{fname}.md{'  📦同步' if ok else ' ⚠️未同步'}"


if __name__ == "__main__":
    print(run_weekly())
