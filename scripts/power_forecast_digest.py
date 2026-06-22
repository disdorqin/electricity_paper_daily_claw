#!/usr/bin/env python3
"""Daily electricity forecasting research digest for WeCom.

Secrets:
  WECHAT_WEBHOOK  required, enterprise WeChat robot webhook
  OPENAI_API_KEY  optional but recommended for article-style summary
  OPENAI_MODEL    optional, default gpt-4o-mini
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter

WEBHOOK_ENV_NAMES = [
    "WECHAT_WEBHOOK",
    "WECOM_WEBHOOK",
    "WECHAT_BOT_WEBHOOK",
    "WECHAT_ROBOT_WEBHOOK",
    "QYWX_WEBHOOK",
    "ENTERPRISE_WECHAT_WEBHOOK",
]

ARXIV_QUERIES = [
    "all:electricity AND all:load AND all:forecasting",
    "all:power AND all:load AND all:forecasting",
    "all:peak AND all:demand AND all:forecasting",
    "all:probabilistic AND all:forecasting AND all:electricity",
    "all:time AND all:series AND all:forecasting AND all:mamba",
    "all:transformer AND all:time AND all:series AND all:forecasting",
    "all:PatchTST OR all:iTransformer OR all:Temporal Fusion Transformer",
]
GITHUB_QUERIES = [
    "electricity load forecasting time series",
    "peak demand forecasting electricity",
    "probabilistic forecasting electricity",
    "mamba time series forecasting",
    "PatchTST load forecasting",
    "ensemble stacking blending time series forecasting",
]

REQUIRED_TOPIC_TERMS = [
    "electricity", "power", "load", "demand", "grid", "energy", "forecast", "forecasting",
    "time series", "mamba", "patchtst", "itransformer", "temporal fusion", "tft",
    "probabilistic", "uncertainty", "ensemble", "stacking", "blending", "peak",
]

NEGATIVE_TOPIC_TERMS = [
    "quantum", "molecule", "chemical", "text-to-speech", "speech", "virus", "ivf",
    "spin hall", "neurosymbolic", "logic programs", "causal games",
]

TRICK_LIBRARY = [
    {
        "name": "峰值加权损失",
        "trigger": ["peak", "demand", "load", "electricity"],
        "why": "普通 MAE/MSE 会被大量平稳点主导，尖峰点容易被模型抹平。",
        "do": "将 y 位于训练集 top 5% 或超过业务阈值的样本权重设为 3/5/10，分别训练并比较。",
        "metric": "overall MAE、top-5% MAE、peak recall、peak precision。",
    },
    {
        "name": "分类 + 回归双头",
        "trigger": ["peak", "probabilistic", "uncertainty", "threshold"],
        "why": "尖峰本质上也是事件预测，只回归负荷值会降低预警敏感度。",
        "do": "一个 head 预测 load value，另一个 head 预测 P(load > threshold)，最终按概率修正预警分数。",
        "metric": "AUC、F1、peak recall、top-k hit rate。",
    },
    {
        "name": "LightGBM 主趋势 + 深度残差",
        "trigger": ["ensemble", "stacking", "blending", "patchtst", "mamba", "transformer"],
        "why": "电力负荷强依赖日历、天气、滚动统计，树模型稳；深度模型适合补充非线性残差。",
        "do": "先训 LightGBM 得到 y_base，再用 PatchTST/Mamba/iTransformer 学 residual = y - y_base。",
        "metric": "MAE/RMSE、峰值区间误差、残差方差下降幅度。",
    },
    {
        "name": "二层 stacking",
        "trigger": ["ensemble", "stacking", "blending", "tft", "mamba", "patchtst"],
        "why": "不同模型擅长不同模式：树模型吃特征，PatchTST 看长窗口，TFT 吃协变量，Mamba 看长依赖。",
        "do": "用 KFold/时间滑窗 OOF 预测训练 Ridge/BayesianRidge/LightGBM meta model，避免泄露。",
        "metric": "验证集稳定性、不同月份/工作日/极端天气切片表现。",
    },
    {
        "name": "概率区间与阈值预警",
        "trigger": ["probabilistic", "uncertainty", "quantile", "interval"],
        "why": "业务更关心是否超过阈值，而不是一个点预测是否漂亮。",
        "do": "输出 P50/P90/P95 或均值+方差，直接计算 P(load > threshold) 作为预警信号。",
        "metric": "coverage、pinball loss、预警提前量、误报率。",
    },
]


def get(url: str, headers: dict[str, str] | None = None) -> str:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "electricity-paper-daily/1.0"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", errors="replace")


def post_json(url: str, payload: dict, headers: dict[str, str] | None = None) -> str:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=80) as r:
        return r.read().decode("utf-8", errors="replace")


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def topic_hit(text: str) -> bool:
    t = text.lower()
    return any(term in t for term in REQUIRED_TOPIC_TERMS)


def negative_hit(text: str) -> bool:
    t = text.lower()
    return any(term in t for term in NEGATIVE_TOPIC_TERMS)


def score(text: str) -> int:
    t = text.lower()
    weights = {
        "electricity": 9, "power": 6, "load": 9, "peak": 10, "demand": 8,
        "grid": 6, "energy": 5, "forecast": 7, "forecasting": 7,
        "probabilistic": 8, "uncertainty": 7, "ensemble": 9,
        "stacking": 9, "blending": 9, "mamba": 6, "transformer": 5,
        "patchtst": 7, "itransformer": 7, "tft": 6, "temporal fusion": 8,
        "time series": 6, "quantile": 6, "anomaly": 4,
    }
    value = sum(v for k, v in weights.items() if k in t)
    if negative_hit(text):
        value -= 12
    return value


def fetch_arxiv(limit: int = 8) -> list[dict]:
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out, seen = [], set()
    for q in ARXIV_QUERIES:
        url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode({
            "search_query": q,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": 10,
        })
        try:
            root = ET.fromstring(get(url))
        except Exception as e:
            print(f"[warn] arxiv failed: {q}: {e}")
            continue
        for entry in root.findall("a:entry", ns):
            link = clean(entry.findtext("a:id", "", ns))
            if not link or link in seen:
                continue
            title = clean(entry.findtext("a:title", "", ns))
            summary = clean(entry.findtext("a:summary", "", ns))
            full_text = title + " " + summary
            item_score = score(full_text)
            if not topic_hit(full_text) or item_score < 14 or negative_hit(full_text):
                continue
            seen.add(link)
            out.append({
                "type": "paper",
                "title": title,
                "url": link,
                "date": clean(entry.findtext("a:published", "", ns))[:10],
                "summary": summary[:700],
                "score": item_score,
            })
    return sorted(out, key=lambda x: x["score"], reverse=True)[:limit]


def fetch_github(limit: int = 8) -> list[dict]:
    headers = {"User-Agent": "electricity-paper-daily/1.0", "Accept": "application/vnd.github+json"}
    if os.getenv("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    out, seen = [], set()
    for q in GITHUB_QUERIES:
        url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode({
            "q": q,
            "sort": "updated",
            "order": "desc",
            "per_page": 8,
        })
        try:
            data = json.loads(get(url, headers))
        except Exception as e:
            print(f"[warn] github failed: {q}: {e}")
            continue
        for repo in data.get("items", []):
            name = repo.get("full_name", "")
            if not name or name in seen:
                continue
            desc = clean(repo.get("description") or "")
            full_text = name + " " + desc
            item_score = score(full_text)
            if item_score < 10 or negative_hit(full_text):
                continue
            seen.add(name)
            stars = int(repo.get("stargazers_count") or 0)
            out.append({
                "type": "repo",
                "title": name,
                "url": repo.get("html_url", ""),
                "date": (repo.get("updated_at") or "")[:10],
                "stars": stars,
                "summary": desc[:500],
                "score": item_score + min(stars // 100, 10),
            })
    return sorted(out, key=lambda x: (x["score"], x.get("stars", 0)), reverse=True)[:limit]


def infer_tags(items: list[dict]) -> list[str]:
    text = " ".join((x.get("title", "") + " " + x.get("summary", "")).lower() for x in items)
    tag_map = {
        "尖峰/阈值": ["peak", "threshold", "demand"],
        "概率预测": ["probabilistic", "uncertainty", "quantile", "interval"],
        "模型融合": ["ensemble", "stacking", "blending"],
        "Transformer/Patch": ["transformer", "patchtst", "itransformer", "tft", "temporal fusion"],
        "Mamba/长依赖": ["mamba", "state space"],
        "电力负荷": ["electricity", "power", "load", "grid", "energy"],
    }
    hits = []
    for tag, words in tag_map.items():
        if any(w in text for w in words):
            hits.append(tag)
    return hits[:4] or ["电力负荷", "模型融合", "尖峰预测"]


def choose_tricks(items: list[dict], limit: int = 3) -> list[dict]:
    text = " ".join((x.get("title", "") + " " + x.get("summary", "")).lower() for x in items)
    ranked = []
    for trick in TRICK_LIBRARY:
        hit_count = sum(1 for term in trick["trigger"] if term in text)
        ranked.append((hit_count, trick))
    ranked.sort(key=lambda x: x[0], reverse=True)
    selected = [trick for hit_count, trick in ranked if hit_count > 0][:limit]
    return selected or TRICK_LIBRARY[:limit]


def build_source_lines(items: list[dict], max_items: int = 6) -> list[str]:
    lines = []
    for i, item in enumerate(items[:max_items], 1):
        star = f"，⭐{item.get('stars')}" if item.get("stars") else ""
        summary = item.get("summary", "")[:130]
        lines.append(f"{i}. [{item['title']}]({item['url']})（{item['type']}，{item.get('date','')}{star}）")
        if summary:
            lines.append(f"   - 看点：{summary}")
    return lines


def fallback_digest(papers: list[dict], repos: list[dict]) -> str:
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d")
    items = papers + repos
    tags = infer_tags(items)
    tricks = choose_tricks(items, 3)
    source_lines = build_source_lines(items, 6)

    lines = [
        f"# ⚡ 电力预测科研日报｜{today}",
        f"> 今日主题：{' / '.join(tags)}",
        "",
        "## 先给结论",
        "今天不要只看整体 MAE。更值得推进的是：**峰值样本单独建模 + 概率预警 + LightGBM/深度模型融合**。目标是提升尖峰召回，而不是让模型在平稳区间刷分。",
        "",
        "## 今日值得看",
    ]
    if source_lines:
        lines.extend(source_lines)
    else:
        lines.append("今天没有抓到足够高相关的新论文/仓库，建议继续沿用近期实验路线。")

    lines += ["", "## 可迁移 trick（直接能进实验）"]
    for i, trick in enumerate(tricks, 1):
        lines += [
            f"{i}. **{trick['name']}**",
            f"   - 为什么：{trick['why']}",
            f"   - 怎么做：{trick['do']}",
            f"   - 看什么指标：{trick['metric']}",
        ]

    lines += [
        "",
        "## 今天最值得跑的 3 个实验",
        "1. **Peak-weight LightGBM baseline**",
        "   - 特征：hour/dayofweek/month/holiday/temperature + lag1/lag24/lag168 + rolling mean/max。",
        "   - 设置：top 5% 负荷样本权重分别试 1/3/5/10。",
        "   - 成功标准：top-5% MAE 下降，overall MAE 不明显恶化。",
        "2. **Deep residual 融合**",
        "   - 公式：`final = LightGBM_pred + DeepModel_residual`。",
        "   - DeepModel 候选：PatchTST / iTransformer / Mamba，窗口先试 96/168/336。",
        "   - 成功标准：峰值附近残差方差下降，极端日切片更稳。",
        "3. **Peak probability head**",
        "   - 标签：`is_peak = y >= train_quantile(0.95)`。",
        "   - 输出：负荷回归值 + P(尖峰)。",
        "   - 成功标准：peak recall、top-k hit rate 提升；误报率可控。",
        "",
        "## 明天继续跟进",
        "优先查找带代码的 electricity/load forecasting 仓库；如果出现 probabilistic/quantile/ensemble 相关论文，优先转成峰值预警实验。",
    ]
    return "\n".join(lines)


def llm_digest(papers: list[dict], repos: list[dict]) -> str | None:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return None
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d")
    source_json = json.dumps({"papers": papers, "repos": repos}, ensure_ascii=False)
    prompt = f"""请基于这些来源写一篇中文企业微信 Markdown 日报。
日期：{today}
主题：时序预测、电力尖峰预测、负荷预测、模型融合提高精度、trick idea。

请严格按这个结构输出，不要改变标题层级：
# ⚡ 电力预测科研日报｜{today}
> 今日主题：用 3-5 个标签概括

## 先给结论
用 3 句话说今天最值得做什么，必须和电力尖峰预测/负荷预测有关。

## 今日值得看
挑 4-6 个最高相关来源。每个来源必须包含：标题、URL、看点、为什么值得看、能迁移到电力尖峰预测的做法。

## 可迁移 trick（直接能进实验）
给 3-5 条。每条必须包含：为什么、怎么做、看什么指标。不要只写概念。

## 今天最值得跑的 3 个实验
每个实验都写：目标、具体设置、特征/模型、评估指标、成功标准。优先包括 peak-weight loss、Deep residual、Peak probability head、stacking、probabilistic warning。

## 明天继续跟进
给 2-3 个后续搜索或实验方向。

硬性要求：
- 不要堆链接，不要泛泛而谈。
- 不要编造来源之外的具体实验结果。
- 如果来源不够相关，就明确说“今天来源质量一般”，然后给稳妥实验建议。
- 总长度控制在 3000 中文字以内，适合企业微信阅读。

来源：{source_json}"""
    payload = {
        "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "input": [
            {"role": "system", "content": "你是电力负荷预测和时序预测科研助理，擅长把论文/仓库变成可执行实验方案。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.35,
        "max_output_tokens": 2800,
    }
    try:
        data = json.loads(post_json("https://api.openai.com/v1/responses", payload, {"Authorization": "Bearer " + key}))
        chunks = []
        for out in data.get("output", []):
            for c in out.get("content", []):
                if c.get("type") in ("output_text", "text"):
                    chunks.append(c.get("text", ""))
        return "\n".join(chunks).strip() or None
    except Exception as e:
        print(f"[warn] openai failed, use fallback: {e}")
        return None


def chunks(text: str, limit: int = 3600) -> list[str]:
    parts, cur = [], ""
    for para in text.split("\n\n"):
        cand = para if not cur else cur + "\n\n" + para
        if len(cand.encode("utf-8")) <= limit:
            cur = cand
        else:
            if cur:
                parts.append(cur)
            cur = para
    if cur:
        parts.append(cur)
    return parts


def get_wecom_webhook() -> str:
    for name in WEBHOOK_ENV_NAMES:
        value = os.getenv(name)
        if value:
            print(f"[info] using webhook secret: {name}")
            return value
    raise RuntimeError(
        "Missing WeCom webhook secret. Add one of these GitHub Actions secrets: "
        + ", ".join(WEBHOOK_ENV_NAMES)
    )


def send_wecom(text: str) -> None:
    hook = get_wecom_webhook()
    parts = chunks(text)
    total = len(parts)
    for i, part in enumerate(parts, 1):
        if total > 1:
            part = f"# ⚡ 电力预测科研日报（续 {i}/{total}）\n\n" + part
        print(post_json(hook, {"msgtype": "markdown", "markdown": {"content": part}}))


def main() -> None:
    papers = fetch_arxiv()
    repos = fetch_github()
    print(f"[info] selected {len(papers)} papers and {len(repos)} repos")
    digest = llm_digest(papers, repos) or fallback_digest(papers, repos)
    print(digest)
    send_wecom(digest)


if __name__ == "__main__":
    main()
