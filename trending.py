"""每日 GitHub Trending 抓取 + AI 推文生成"""
import re
import requests
from bs4 import BeautifulSoup

from common import (
    MODEL_FAST, ai_chat, git_pull, git_commit_push,
    write_note, safe_filename, today_str, now_str,
    tg_send, fetch_github_repo,
)


def fetch_trending(language: str = "", since: str = "daily") -> list:
    """抓取trending页面的仓库列表"""
    url = f"https://github.com/trending/{language}?since={since}"
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    repos = []
    for article in soup.select("article.Box-row"):
        a = article.select_one("h2 a")
        if not a:
            continue
        full_name = a.get_text(strip=True).replace("\n", "").replace(" ", "")
        desc_el = article.select_one("p")
        desc = desc_el.get_text(strip=True) if desc_el else ""
        lang_el = article.select_one('span[itemprop="programmingLanguage"]')
        lang = lang_el.get_text(strip=True) if lang_el else ""
        stars_el = article.select_one('a[href$="/stargazers"]')
        stars = stars_el.get_text(strip=True) if stars_el else "0"
        today_el = article.select_one("span.d-inline-block.float-sm-right")
        today_stars = today_el.get_text(strip=True) if today_el else ""
        repos.append({
            "full_name": full_name,
            "url": f"https://github.com/{full_name}",
            "description": desc,
            "language": lang,
            "stars": stars,
            "today_stars": today_stars,
        })
        if len(repos) >= 10:
            break
    return repos


def pick_best_repo(repos: list) -> dict:
    """挑一个最有意思的仓库（优先非纯星数堆积、有实际内容的项目）"""
    if not repos:
        return None
    listing = "\n".join([
        f"{i+1}. {r['full_name']} ({r['language']}) - ⭐{r['stars']} - {r['description'][:100]}"
        for i, r in enumerate(repos)
    ])
    prompt = f"""下面是今天 GitHub Trending 的前10个仓库。请挑出**1个**最值得写成推文的项目。
挑选标准：实用价值高、有创新点、不是纯刷star的玩具项目。
只回复一个数字（1-10），不要其他内容。

{listing}
"""
    resp = ai_chat(prompt, model=MODEL_FAST, max_tokens=20)
    m = re.search(r"\d+", resp)
    idx = (int(m.group()) - 1) if m else 0
    idx = max(0, min(idx, len(repos) - 1))
    return repos[idx]


def make_tweet(repo: dict) -> tuple:
    """返回 (markdown内容, 简短TG推文)"""
    full = fetch_github_repo(repo["url"])
    readme_excerpt = full.get("readme", "")[:10000]

    prompt = f"""你是一个混迹在中文科技圈的推特博主，风格像"鸟哥|蓝鸟会"那种。现在要根据下面这个 GitHub 项目，写一条**口语化、有钩子、有人味**的中文推文。

铁律（违反任何一条都算失败）：
1. **绝对不用 emoji**，一个都不许出现（包括 🔥⭐✨🚀💡✅❌📌👇 这些，也不许用 1️⃣2️⃣3️⃣ 这种带圈数字 emoji）。要列点就用普通数字 "1. 2. 3." 加点。
2. **开头第一句必须是钩子**，要有情绪、像在群里跟朋友吹水那样。可参考但不要照抄："卧槽这玩意儿真有点东西"、"兄弟们，懒人福音它来了"、"绷不住了，又被一个项目刷屏"、"刚扒到一个有意思的项目"、"今天看到这个我直接坐不住了"。**禁止**任何"今天给大家推荐"、"为大家介绍"、"近期发现"、"分享一个"这种播音腔。
3. 钩子之后用 1-2 句话**说人话**讲清楚这玩意儿到底干啥的，不要专业术语堆砌，能用大白话就用大白话（比如不说"端到端推理框架"，说"你输入啥它直接给你跑出结果，中间步骤全包了"）。
4. 中间用 "1. 2. 3." 列 3 个最炸的亮点，每条 1-2 句话，要带具体细节或例子，不要空洞形容词。语气要像跟人聊天，可以用"这个真的绝"、"我试了一下"、"说实话有点东西"、"关键是"这种。
5. 来一段**痛点共鸣**（1-2 句），说说以前没这玩意儿的时候大家是怎么受苦的，让读者代入。
6. **行动召唤**，别太正经。可参考："反正我是先 mark 了"、"自己看，别光信我"、"准备搞来玩玩"、"感兴趣的兄弟自己去试"、"链接在下面，懂的都懂"。
7. 全文 350-500 字，**纯文本**，不用 markdown 的 # 标题、不用 ** 加粗、不用 > 引用块、不用反引号 `。段落之间空一行就行。
8. 行动召唤之后，单独一行附上仓库链接：{repo['url']}
9. **最后另起一行**，给 3-5 个相关的中文 hashtag（用 # 开头，空格分隔，比如 #GitHub #开源项目 #AI工具 #效率神器），根据项目实际内容选，要贴合主题。

仓库信息：
- 名称: {repo['full_name']}
- 描述: {repo['description']}
- 语言: {repo['language']}
- 总 stars: {repo['stars']}
- 今日新增: {repo['today_stars']}
- topics: {', '.join(full.get('topics', []))}

README 节选：
{readme_excerpt}

直接输出推文正文，不要"好的我来写"这种废话开头。"""
    article = ai_chat(prompt, model=MODEL_FAST, max_tokens=2500)

    md = f"""---
type: trending
date: {today_str()}
repo: {repo['full_name']}
url: {repo['url']}
stars: {repo['stars']}
today_stars: {repo['today_stars']}
language: {repo['language']}
created: {now_str()}
tags: [trending, github]
---

# {repo['full_name']}

> {repo['language']} | 总 {repo['stars']} | 今日 +{repo['today_stars']}

{article}

---

## 今日 Trending Top 10（备份）
"""
    safe_article = article.replace("```", "'''")
    tg_text = f"```\n{safe_article}\n```"
    return md, tg_text


def run_trending():
    git_pull()
    repos = fetch_trending()
    if not repos:
        return "⚠️ 抓取trending失败"
    pick = pick_best_repo(repos)
    if not pick:
        return "⚠️ 没有合适的项目"

    md, tg_text = make_tweet(pick)

    backup = "\n".join([
        f"- [{r['full_name']}]({r['url']}) - {r['language']} - {r['stars']} stars (+{r['today_stars']}) - {r['description'][:80]}"
        for r in repos
    ])
    md += backup + "\n"

    fname = safe_filename(f"{today_str()}_Trending_{pick['full_name'].replace('/', '_')}")
    write_note("Trending", fname, md)
    ok = git_commit_push(f"trending: {fname}")

    tg_send(tg_text, parse_mode="Markdown")
    return f"{pick['full_name']} 已发布{'（已同步）' if ok else '（同步失败）'}"


if __name__ == "__main__":
    print(run_trending())
