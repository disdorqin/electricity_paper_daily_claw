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

ARXIV_QUERIES = [
    "all:(electricity load forecasting)",
    "all:(peak demand forecasting)",
    "all:(power load forecasting)",
    "all:(probabilistic forecasting electricity)",
    "all:(time series forecasting mamba)",
    "all:(transformer time series forecasting)",
]
GITHUB_QUERIES = [
    "electricity load forecasting time series",
    "peak demand forecasting electricity",
    "probabilistic forecasting electricity",
    "mamba time series forecasting",
    "PatchTST load forecasting",
    "ensemble stacking blending time series forecasting",
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


def score(text: str) -> int:
    t = text.lower()
    weights = {
        "electricity": 6, "power": 5, "load": 6, "peak": 8, "demand": 5,
        "forecast": 5, "probabilistic": 6, "uncertainty": 5, "ensemble": 7,
        "stacking": 7, "blending": 7, "mamba": 5, "transformer": 4,
        "patchtst": 5, "tft": 4, "temporal fusion": 5,
    }
    return sum(v for k, v in weights.items() if k in t)


def fetch_arxiv(limit: int = 8) -> list[dict]:
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out, seen = [], set()
    for q in ARXIV_QUERIES:
        url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode({
            "search_query": q,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": 5,
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
            seen.add(link)
            title = clean(entry.findtext("a:title", "", ns))
            summary = clean(entry.findtext("a:summary", "", ns))
            out.append({
                "type": "paper",
                "title": title,
                "url": link,
                "date": clean(entry.findtext("a:published", "", ns))[:10],
                "summary": summary[:700],
                "score": score(title + " " + summary),
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
            "per_page": 5,
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
            seen.add(name)
            desc = clean(repo.get("description") or "")
            stars = int(repo.get("stargazers_count") or 0)
            out.append({
                "type": "repo",
                "title": name,
                "url": repo.get("html_url", ""),
                "date": (repo.get("updated_at") or "")[:10],
                "stars": stars,
                "summary": desc[:500],
                "score": score(name + " " + desc) + min(stars // 100, 10),
            })
    return sorted(out, key=lambda x: (x["score"], x.get("stars", 0)), reverse=True)[:limit]


def fallback_digest(papers: list[dict], repos: list[dict]) -> str:
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d")
    lines = [
        f"# ⚡ 电力预测科研日报｜{today}",
        "",
        "今天重点：尖峰预测不要只优化整体 MAE，要单独盯住 top-k 高负荷区间、峰值召回率、概率预警和模型融合。",
        "",
        "## 今日值得看",
    ]
    for i, item in enumerate((papers + repos)[:10], 1):
        star = f"，⭐{item.get('stars')}" if item.get("stars") else ""
        lines.append(f"{i}. [{item['title']}]({item['url']})（{item['type']}，{item.get('date','')}{star}）")
        if item.get("summary"):
            lines.append(f"   - {item['summary'][:160]}")
    lines += [
        "",
        "## 可迁移 trick",
        "- 峰值加权损失：对 top 5% 负荷点加权 2/5/10 倍，单独观察 peak MAE。",
        "- 分类 + 回归双头：同时预测 load value 与 P(load > threshold)。",
        "- 残差融合：LightGBM 负责强特征主趋势，PatchTST/Mamba/iTransformer 预测残差。",
        "- 二层 stacking：LightGBM、TFT、PatchTST、Mamba、TCN 输出进入 Ridge/BayesianRidge/LightGBM meta model。",
        "- 概率预警：不要只看点预测，输出超过尖峰阈值的概率。",
        "",
        "## 今天最值得跑的 3 个实验",
        "1. LightGBM baseline：小时/星期/月份/节假日/温度/滚动均值/滚动最大值。",
        "2. Deep residual：y = LightGBM_pred + PatchTST_or_Mamba_residual。",
        "3. Peak head：回归负荷 + 分类尖峰，对比整体 MAE、top-5% MAE、peak recall。",
    ]
    return "\n".join(lines)


def llm_digest(papers: list[dict], repos: list[dict]) -> str | None:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return None
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d")
    prompt = f"""请基于这些来源写一篇中文企业微信 Markdown 公众号风格日报。
主题：时序预测、电力尖峰预测、负荷预测、模型融合提高精度、trick idea。
日期：{today}
要求：提炼 idea，不要堆链接；每个重要来源保留 URL；给出可迁移到电力尖峰预测的做法；最后给今天最值得跑的 3 个实验。不要编造来源之外的具体结果。
来源：{json.dumps({'papers': papers, 'repos': repos}, ensure_ascii=False)}"""
    payload = {
        "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "input": [
            {"role": "system", "content": "你是电力负荷预测和时序预测科研助理，擅长把论文/仓库变成可执行实验方案。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
        "max_output_tokens": 2200,
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


def chunks(text: str, limit: int = 3300) -> list[str]:
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


def send_wecom(text: str) -> None:
    hook = os.getenv("WECHAT_WEBHOOK")
    if not hook:
        raise RuntimeError("Missing WECHAT_WEBHOOK secret")
    parts = chunks(text)
    for i, part in enumerate(parts, 1):
        suffix = f"\n\n> 第 {i}/{len(parts)} 段" if len(parts) > 1 else ""
        print(post_json(hook, {"msgtype": "markdown", "markdown": {"content": part + suffix}}))


def main() -> None:
    papers = fetch_arxiv()
    repos = fetch_github()
    digest = llm_digest(papers, repos) or fallback_digest(papers, repos)
    print(digest)
    send_wecom(digest)


if __name__ == "__main__":
    main()
