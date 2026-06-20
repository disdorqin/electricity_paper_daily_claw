#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
电力预测科研日报 v2.0 - 每天 08:00(北京时间) 自动推送
特性:
  - OpenAlex + arXiv 双源检索
  - 自动下载 OA PDF / arXiv LaTeX 源
  - PyMuPDF 提取全文与图表
  - arXiv LaTeX 提取数学公式
  - LLM 生成含方法/公式/实验解读的公众号文章
  - 图片托管到 GitHub 仓库 assets 目录
  - 钉钉群机器人推送
"""

import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import os
import re
import io
import json
import tarfile
import zipfile
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any
from collections import Counter
from urllib.parse import urlparse

import requests

# ── 配置 ──
TZ = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "electricity_daily"
OUTPUT_DIR.mkdir(exist_ok=True)

TEMP_DIR = BASE_DIR / ".tmp"
TEMP_DIR.mkdir(exist_ok=True)

ASSETS_DIR = BASE_DIR / "assets"
ASSETS_DIR.mkdir(exist_ok=True)

SEARCH_QUERIES = [
    "electricity price forecasting",
    "electricity price prediction deep learning",
    "peak price spike electricity market prediction",
    "negative electricity price forecasting",
    "price volatility electricity market deep learning",
    "day-ahead electricity price forecast transformer",
    "电力预测 深度学习",
    "尖峰电价 预测",
    "负电价 电力市场",
    "电价波动 电力市场 预测",
]

TOP_JOURNALS = {
    "ieee transactions on power systems": 95,
    "ieee transactions on smart grid": 93,
    "applied energy": 92,
    "energy": 88,
    "energy economics": 87,
    "ieee transactions on sustainable energy": 90,
    "electric power systems research": 82,
    "international journal of electrical power & energy systems": 80,
    "journal of modern power systems and clean energy": 83,
    "csee journal of power and energy systems": 78,
    "neurips": 92,
    "icml": 92,
    "aaai": 88,
    "ijcai": 86,
    "中国电机工程学报": 85,
    "电力系统自动化": 83,
    "电网技术": 81,
    "电力自动化设备": 78,
    "电力系统保护与控制": 75,
}

LLM_CANDIDATES = [
    {
        "name": "agnes",
        "base_url": "https://apihub.agnes-ai.com/v1",
        "api_key_env": "AGNES_API_KEY",
        "model_env": "AGNES_MODEL",
        "default_model": "agnes-2.0-flash",
    },
    {
        "name": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "model_env": "DEEPSEEK_MODEL",
        "default_model": "deepseek-chat",
    },
    {
        "name": "sensenova",
        "base_url": "https://token.sensenova.cn/v1",
        "api_key_env": "SENSENOVA_API_KEY",
        "model_env": "SENSENOVA_MODEL",
        "default_model": "sensenova-6.7-flash-lite",
    },
]

# 通知通道配置
DINGTALK_WEBHOOK_ENV = "DINGTALK_WEBHOOK"
WECOM_WEBHOOK_ENV = "WECOM_WEBHOOK"       # 企业微信群机器人 webhook
PUSHBOT_URL_ENV = "PUSHBOT_URL"           # push-bot 群聊推送地址
SERVERCHAN_SENDKEY_ENV = "SERVERCHAN_SENDKEY"  # Server 酱 Turbo SendKey
NOTIFY_CHANNELS_ENV = "NOTIFY_CHANNELS"   # 逗号分隔，如 serverchan,wecom,dingtalk

GITHUB_TOKEN_ENV = "GITHUB_TOKEN"
GITHUB_REPO_ENV = "GITHUB_REPO"  # 格式: owner/repo

MAX_PDF_PAGES = 12  # 限制解析页数，避免过长
MAX_IMAGES_PER_PAPER = 4
MAX_FORMULAS_PER_PAPER = 3

# 通知内容长度限制
PUSHBOT_MAX_LENGTH = 4000     # push-bot 单条消息建议长度
WECOM_MAX_LENGTH = 1800       # 企业微信群机器人文本消息约 2048 字节安全上限
SERVERCHAN_MAX_LENGTH = 32000 # Server 酱 内容限制约 64KB，留余量
DINGTALK_MAX_LENGTH = 18000


# ── 工具函数 ──

def now_cn() -> datetime:
    return datetime.now(TZ)


def today_str() -> str:
    return now_cn().strftime("%Y-%m-%d")


def slugify(text: str) -> str:
    return re.sub(r"[^\w\-]", "_", text)[:50].strip("_")


def get_first_link(work: dict) -> str:
    arxiv_id = work.get("arxiv_id")
    if arxiv_id:
        return f"https://arxiv.org/abs/{arxiv_id}"
    doi = work.get("doi")
    if doi:
        return doi
    wid = work.get("id")
    if wid and wid.startswith("https://"):
        return wid
    for loc_key in ["primary_location", "best_oa_location"]:
        loc = work.get(loc_key, {}) or {}
        if loc.get("landing_page_url"):
            return loc["landing_page_url"]
    return ""


def get_pdf_url(work: dict) -> str:
    """尝试获取论文 PDF 链接"""
    # arXiv
    arxiv_id = work.get("arxiv_id")
    if arxiv_id:
        return f"https://arxiv.org/pdf/{arxiv_id}.pdf"

    # OpenAlex OA PDF
    for loc_key in ["best_oa_location", "primary_location"]:
        loc = work.get(loc_key, {}) or {}
        pdf_url = (loc.get("pdf_url") or "").strip()
        if pdf_url:
            return pdf_url

    # DOI via Unpaywall
    doi = work.get("doi", "")
    if doi:
        try:
            email = os.getenv("UNPAYWALL_EMAIL", "test@example.com")
            url = f"https://api.unpaywall.org/v2/{doi}?email={email}"
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                data = r.json()
                best = data.get("best_oa_location", {}) or {}
                pdf = best.get("url_for_pdf") or ""
                if pdf:
                    return pdf
        except Exception:
            pass
    return ""


def get_journal_score(work: dict) -> tuple[int, str]:
    source_name = ""
    display_name = ""
    if work.get("source") == "arxiv":
        return 60, "arXiv"
    for loc_key in ["primary_location", "best_oa_location"]:
        loc = work.get(loc_key, {}) or {}
        src = loc.get("source", {}) or {}
        if src.get("display_name"):
            source_name = str(src["display_name"]).lower()
            display_name = str(src["display_name"])
            break
    if not source_name:
        return 50, "未标注来源"
    for name, score in TOP_JOURNALS.items():
        if name in source_name or source_name in name:
            return score, display_name or name
    if "arxiv" in source_name:
        return 60, "arXiv"
    if "ssrn" in source_name:
        return 40, "SSRN"
    return 65, display_name or "Unknown"


def extract_authors(work: dict) -> str:
    authorships = work.get("authorships", [])
    names = []
    for a in authorships[:5]:
        author = a.get("author", {}) or {}
        if author.get("display_name"):
            names.append(author["display_name"])
    if len(authorships) > 5:
        names.append("...")
    return ", ".join(names) if names else work.get("authors", "未知")


def extract_abstract(work: dict) -> str:
    if "abstract" in work and work["abstract"]:
        return work["abstract"]
    ab = work.get("abstract_inverted_index")
    if ab and isinstance(ab, dict):
        words = []
        for word, positions in ab.items():
            if isinstance(positions, list):
                for pos in positions:
                    words.append((pos, word))
        words.sort()
        return " ".join(w for _, w in words)
    return "无摘要"


def get_cited_by_count(work: dict) -> int:
    return work.get("cited_by_count", 0)


def get_publication_date(work: dict) -> str:
    return work.get("publication_date") or str(work.get("publication_year", "")) or ""


# ── API 搜索: OpenAlex ──

def search_openalex(query: str, per_page: int = 12) -> list[dict]:
    url = "https://api.openalex.org/works"
    params = {
        "search": query,
        "per_page": per_page,
        "sort": "publication_date:desc",
        "select": "id,doi,title,authorships,publication_date,publication_year,cited_by_count,primary_location,best_oa_location,open_access,abstract_inverted_index,type",
        "filter": "from_publication_date:2023-01-01",
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        for r in results:
            r["source"] = "openalex"
        return results
    except Exception as e:
        print(f"  [WARN] OpenAlex 搜索失败 ({query[:30]}...): {e}")
        return []


# ── API 搜索: arXiv ──

def search_arxiv(query: str, max_results: int = 10) -> list[dict]:
    if any("\u4e00" <= ch <= "\u9fff" for ch in query):
        return []
    url = "http://export.arxiv.org/api/query"
    params = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.text.encode("utf-8"))
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        results = []
        for entry in root.findall("atom:entry", ns):
            title = entry.find("atom:title", ns)
            summary = entry.find("atom:summary", ns)
            published = entry.find("atom:published", ns)
            arxiv_id = entry.find("atom:id", ns)
            authors = entry.findall("atom:author/atom:name", ns)
            if title is None:
                continue
            title_text = title.text.strip().replace("\n", " ")
            arxiv_id_text = (arxiv_id.text or "").split("/")[-1]
            results.append({
                "source": "arxiv",
                "arxiv_id": arxiv_id_text,
                "title": title_text,
                "abstract": (summary.text or "").strip().replace("\n", " "),
                "publication_date": (published.text or "")[:10] if published is not None else "",
                "publication_year": int((published.text or "")[:4]) if published is not None else None,
                "cited_by_count": 0,
                "authors": ", ".join([a.text for a in authors[:5] if a.text]),
                "authorships": [],
            })
        return results
    except Exception as e:
        print(f"  [WARN] arXiv 搜索失败 ({query[:30]}...): {e}")
        return []


def search_all() -> list[dict]:
    all_works: dict[str, dict] = {}
    print("\n[学术搜索]")
    for i, query in enumerate(SEARCH_QUERIES, 1):
        print(f"  [{i}/{len(SEARCH_QUERIES)}] 搜索: {query[:40]}...")
        works = search_openalex(query)
        for w in works:
            wid = w.get("id", "") or w.get("doi", "")
            if wid and wid not in all_works:
                all_works[wid] = w
        arxiv_works = search_arxiv(query)
        for w in arxiv_works:
            wid = w.get("arxiv_id", "")
            if wid and wid not in all_works:
                all_works[wid] = w
        print(f"    -> OpenAlex {len(works)} 篇, arXiv {len(arxiv_works)} 篇, 累计去重 {len(all_works)} 篇")
    print(f"\n  去重后总计: {len(all_works)} 篇")
    return list(all_works.values())


# ── 评分 ──

def score_paper(work: dict) -> tuple[float, str]:
    now = now_cn()
    pub_date_str = get_publication_date(work)
    pub_date = None
    if pub_date_str and len(pub_date_str) >= 10:
        try:
            pub_date = datetime.strptime(str(pub_date_str)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    time_score = 0
    if pub_date:
        days_old = (now - pub_date).days
        if days_old < 30: time_score = 30
        elif days_old < 90: time_score = 28
        elif days_old < 180: time_score = 24
        elif days_old < 365: time_score = 20
        elif days_old < 730: time_score = 15
        else: time_score = 8
    else:
        year = work.get("publication_year", 0)
        if year >= 2025: time_score = 25
        elif year >= 2024: time_score = 20
        elif year >= 2023: time_score = 15
        elif year >= 2022: time_score = 10
        else: time_score = 5

    journal_score, journal_name = get_journal_score(work)
    citations = get_cited_by_count(work)
    if citations >= 100: cite_score = 20
    elif citations >= 50: cite_score = 18
    elif citations >= 20: cite_score = 15
    elif citations >= 10: cite_score = 12
    elif citations >= 5: cite_score = 8
    elif citations >= 1: cite_score = 5
    else: cite_score = 2

    title = (work.get("title") or "").lower()
    abstract_text = extract_abstract(work).lower()
    relevance_terms = [
        "electricity price", "price forecast", "peak load", "price spike",
        "negative price", "price volatility", "power market", "day-ahead",
        "deep learning", "transformer", "lstm", "time series",
        "电价", "尖峰", "负电价", "电价波动",
        "电力市场", "日前", "深度学习"
    ]
    keywords_found = sum(1 for t in relevance_terms if t in title or t in abstract_text)
    relevance_score = min(keywords_found * 3, 20)

    total = time_score + journal_score + cite_score + relevance_score
    details = f"时间{time_score} + 期刊{journal_score} + 引用{cite_score} + 相关{relevance_score} = {total}"
    return total, details


# ── PDF / LaTeX 下载与解析 ──

def safe_download(url: str, path: Path, timeout: int = 60) -> bool:
    try:
        print(f"    [下载] {url[:70]}...")
        r = requests.get(url, timeout=timeout, stream=True)
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except Exception as e:
        print(f"    [FAIL] 下载失败: {e}")
        return False


def extract_text_from_pdf(pdf_path: Path) -> str:
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(str(pdf_path))
        texts = []
        for i, page in enumerate(doc):
            if i >= MAX_PDF_PAGES:
                break
            texts.append(page.get_text())
        doc.close()
        text = "\n".join(texts)
        # 简单清理
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return text[:20000]
    except Exception as e:
        print(f"    [WARN] PDF 文本提取失败: {e}")
        return ""


def extract_images_from_pdf(pdf_path: Path, output_dir: Path, prefix: str) -> list[Path]:
    """用 PyMuPDF 提取 PDF 中的图片对象"""
    try:
        import fitz
    except ImportError:
        return []

    output_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    try:
        doc = fitz.open(str(pdf_path))
        seen = set()
        for page_idx, page in enumerate(doc):
            if page_idx >= MAX_PDF_PAGES:
                break
            img_list = page.get_images(full=True)
            for img_idx, img in enumerate(img_list):
                xref = img[0]
                if xref in seen:
                    continue
                seen.add(xref)
                try:
                    base_image = doc.extract_image(xref)
                    image_bytes = base_image["image"]
                    ext = base_image["ext"]
                    if ext.lower() not in ["png", "jpg", "jpeg"]:
                        continue
                    filename = f"{prefix}_p{page_idx+1}_img{img_idx+1}.{ext}"
                    img_path = output_dir / filename
                    with open(img_path, "wb") as f:
                        f.write(image_bytes)
                    # 过滤太小的图（可能是图标）
                    if img_path.stat().st_size < 2048:
                        img_path.unlink()
                        continue
                    saved.append(img_path)
                    if len(saved) >= MAX_IMAGES_PER_PAPER:
                        break
                except Exception:
                    continue
            if len(saved) >= MAX_IMAGES_PER_PAPER:
                break
        doc.close()
    except Exception as e:
        print(f"    [WARN] PDF 图片提取失败: {e}")
    return saved


def find_main_tex(extract_dir: Path) -> Path | None:
    """在解压后的 LaTeX 源文件中找到主 tex 文件"""
    tex_files = list(extract_dir.rglob("*.tex"))
    if not tex_files:
        return None
    # 优先找包含 \documentclass 的文件
    for tf in tex_files:
        try:
            content = tf.read_text(encoding="utf-8", errors="ignore")
            if "\\documentclass" in content:
                return tf
        except Exception:
            continue
    return tex_files[0]


def extract_formulas_from_tex(tex_path: Path) -> list[dict]:
    """从 LaTeX 源文件中提取公式"""
    try:
        text = tex_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []

    formulas = []
    # 匹配 \begin{equation} ... \end{equation}
    for m in re.finditer(r"\\begin\{equation\}\*?(.*?)\\end\{equation\}", text, re.DOTALL):
        body = m.group(1).strip()
        if body and len(body) > 5:
            formulas.append({"type": "equation", "latex": body})
    # 匹配 \[ ... \]
    for m in re.finditer(r"\\\[(.*?)\\\]", text, re.DOTALL):
        body = m.group(1).strip()
        if body and len(body) > 5:
            formulas.append({"type": "display", "latex": body})
    # 去重并限制数量
    seen = set()
    unique = []
    for f in formulas:
        key = f["latex"][:80]
        if key not in seen:
            seen.add(key)
            unique.append(f)
        if len(unique) >= MAX_FORMULAS_PER_PAPER:
            break
    return unique


def extract_text_from_tex(tex_path: Path) -> str:
    try:
        text = tex_path.read_text(encoding="utf-8", errors="ignore")
        # 移除注释
        text = re.sub(r"(?<!\\)%.*?\n", "\n", text)
        # 移除部分命令
        for cmd in ["\\usepackage", "\\documentclass", "\\begin{document}", "\\end{document}",
                    "\\bibliography", "\\bibliographystyle"]:
            text = text.replace(cmd, "")
        # 移除图片引用
        text = re.sub(r"\\includegraphics.*?\}", "", text)
        return text[:20000]
    except Exception:
        return ""


def download_arxiv_source(arxiv_id: str, output_dir: Path) -> Path | None:
    """下载 arXiv LaTeX 源并解压"""
    url = f"https://arxiv.org/e-print/{arxiv_id}"
    tar_path = output_dir / f"{arxiv_id}_source.tar.gz"
    if safe_download(url, tar_path, timeout=60):
        try:
            extract_dir = output_dir / f"{arxiv_id}_source"
            extract_dir.mkdir(exist_ok=True)
            if tarfile.is_tarfile(tar_path):
                with tarfile.open(tar_path, "r:gz") as tar:
                    tar.extractall(path=extract_dir)
                tar_path.unlink()
                return extract_dir
            else:
                # 尝试 zip
                zip_path = output_dir / f"{arxiv_id}_source.zip"
                tar_path.rename(zip_path)
                if zipfile.is_zipfile(zip_path):
                    with zipfile.ZipFile(zip_path, "r") as z:
                        z.extractall(path=extract_dir)
                    zip_path.unlink()
                    return extract_dir
        except Exception as e:
            print(f"    [WARN] arXiv 源解压失败: {e}")
    return None


def collect_source_images(extract_dir: Path) -> list[Path]:
    """收集 LaTeX 源文件中的图片"""
    images = []
    for ext in ["*.png", "*.jpg", "*.jpeg", "*.pdf"]:
        images.extend(extract_dir.rglob(ext))
    # 优先选文件名包含 figure 的
    images.sort(key=lambda p: ("figure" not in p.name.lower(), p.name))
    return images[:MAX_IMAGES_PER_PAPER]


# ── GitHub 图床 ──

def upload_to_github(local_path: Path, repo: str, token: str, remote_path: str) -> str:
    """把本地文件上传到 GitHub 仓库，返回 raw URL"""
    api_url = f"https://api.github.com/repos/{repo}/contents/{remote_path}"
    content = local_path.read_bytes()
    import base64
    b64 = base64.b64encode(content).decode()

    data = {
        "message": f"Add asset {remote_path}",
        "content": b64,
        "branch": "main",
    }
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    # 检查是否已存在
    r = requests.get(api_url, headers=headers, params={"ref": "main"}, timeout=30)
    if r.status_code == 200:
        existing = r.json()
        data["sha"] = existing["sha"]

    r = requests.put(api_url, headers=headers, json=data, timeout=60)
    r.raise_for_status()
    resp = r.json()
    return resp["content"]["download_url"]


def get_image_public_url(local_path: Path, repo: str | None, token: str | None) -> str:
    """本地相对路径 或 GitHub raw URL"""
    if not repo or not token:
        # 本地模式：返回相对路径
        return str(local_path.relative_to(BASE_DIR).as_posix())

    date_folder = today_str()
    remote_path = f"assets/{date_folder}/{local_path.name}"
    try:
        url = upload_to_github(local_path, repo, token, remote_path)
        print(f"    [OK] 上传图片: {url[:80]}...")
        return url
    except Exception as e:
        print(f"    [WARN] GitHub 上传失败: {e}，使用本地路径")
        return str(local_path.relative_to(BASE_DIR).as_posix())


# ── 论文富化（全文/图片/公式） ──

def enrich_paper(work: dict) -> dict:
    """给论文添加 full_text, images, formulas 等字段"""
    paper_id = work.get("arxiv_id") or work.get("id", "").split("/")[-1]
    paper_dir = TEMP_DIR / slugify(paper_id)
    paper_dir.mkdir(parents=True, exist_ok=True)

    assets_subdir = ASSETS_DIR / today_str()
    assets_subdir.mkdir(parents=True, exist_ok=True)

    result = {
        "full_text": "",
        "images": [],  # list of dict {"path": Path, "url": str, "caption": str}
        "formulas": [],
        "parse_status": "abstract_only",
    }

    arxiv_id = work.get("arxiv_id")
    if arxiv_id:
        # 1) 尝试 LaTeX 源
        print(f"  [解析 arXiv 源] {arxiv_id}")
        src_dir = download_arxiv_source(arxiv_id, paper_dir)
        if src_dir:
            tex_path = find_main_tex(src_dir)
            if tex_path:
                result["full_text"] = extract_text_from_tex(tex_path)
                result["formulas"] = extract_formulas_from_tex(tex_path)
                imgs = collect_source_images(src_dir)
                for img in imgs:
                    target = assets_subdir / f"{slugify(arxiv_id)}_{img.name}"
                    shutil.copy(img, target)
                    result["images"].append({
                        "path": target,
                        "url": "",
                        "caption": img.name,
                    })
                if result["full_text"] or result["formulas"]:
                    result["parse_status"] = "latex_source"

        # 2) 如果 LaTeX 源失败，尝试 PDF
        if result["parse_status"] == "abstract_only":
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
            pdf_path = paper_dir / f"{arxiv_id}.pdf"
            if safe_download(pdf_url, pdf_path):
                result["full_text"] = extract_text_from_pdf(pdf_path)
                imgs = extract_images_from_pdf(pdf_path, assets_subdir, slugify(arxiv_id))
                for img in imgs:
                    result["images"].append({
                        "path": img,
                        "url": "",
                        "caption": img.name,
                    })
                if result["full_text"]:
                    result["parse_status"] = "pdf"
    else:
        # 非 arXiv：尝试 OA PDF
        pdf_url = get_pdf_url(work)
        if pdf_url:
            pdf_path = paper_dir / f"{slugify(work.get('id','paper'))}.pdf"
            if safe_download(pdf_url, pdf_path):
                result["full_text"] = extract_text_from_pdf(pdf_path)
                imgs = extract_images_from_pdf(pdf_path, assets_subdir, slugify(work.get("id", "paper")))
                for img in imgs:
                    result["images"].append({
                        "path": img,
                        "url": "",
                        "caption": img.name,
                    })
                if result["full_text"]:
                    result["parse_status"] = "pdf"

    return result


# ── LLM ──

def llm_chat(messages: list[dict], max_tokens: int = 6000) -> str:
    from openai import OpenAI
    last_error = None
    for cfg in LLM_CANDIDATES:
        api_key = os.getenv(cfg["api_key_env"])
        if not api_key:
            continue
        base_url = os.getenv(cfg["model_env"].replace("_MODEL", "_BASE_URL"), cfg["base_url"])
        model = os.getenv(cfg["model_env"], cfg["default_model"])
        try:
            print(f"  [LLM] 尝试 {cfg['name']} / {model}")
            client = OpenAI(api_key=api_key, base_url=base_url)
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.7,
                max_tokens=max_tokens,
            )
            content = resp.choices[0].message.content
            if content:
                return content.strip()
        except Exception as e:
            last_error = e
            print(f"  [WARN] {cfg['name']} 失败: {e}")
            continue
    raise RuntimeError(f"所有 LLM 均调用失败。最后一次错误: {last_error}")


def build_paper_card(rank: int, work: dict, score: float, details: str, enrich: dict) -> dict:
    _, jname = get_journal_score(work)
    return {
        "rank": rank,
        "title": work.get("title", "未知标题"),
        "journal": jname,
        "authors": extract_authors(work),
        "date": get_publication_date(work) or str(work.get("publication_year", "")),
        "citations": get_cited_by_count(work),
        "abstract": extract_abstract(work),
        "link": get_first_link(work),
        "score": score,
        "score_details": details,
        "parse_status": enrich.get("parse_status", "abstract_only"),
        "full_text": enrich.get("full_text", ""),
        "formulas": enrich.get("formulas", []),
        "images": [{"url": img.get("url", ""), "caption": img.get("caption", "")} for img in enrich.get("images", [])],
    }


def generate_article(top10: list[tuple[float, dict, str, dict]]) -> str:
    today = today_str()
    weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now_cn().weekday()]

    top3_cards = [build_paper_card(i + 1, w, s, d, e) for i, (s, w, d, e) in enumerate(top10[:3])]
    other7_cards = [build_paper_card(i + 4, w, s, d, e) for i, (s, w, d, e) in enumerate(top10[3:10])]

    prompt = f"""你是一位专注能源人工智能的资深科技编辑，正在为公众号撰写一期深度版「电力预测科研日报 v2.0」。
请严格基于下面真实的论文数据（含全文/公式/图表），撰写一篇专业、深入、有可读性的 Markdown 文章。

今日日期：{today} {weekday_cn}

## 输出要求
1. 标题吸睛，包含日期和主题，例如「{today} 电力预测深度日报 | ...」。
2. 开头写 300 字行业导语，概括今日论文整体趋势、方法热点、实验发现。
3. TOP 3 论文每篇单独成节，深度解读：
   - **研究背景与问题**：这篇论文解决什么痛点
   - **方法框架**：用通俗语言讲解核心模型/方法，配「方法图说明」（如果有图片）
   - **核心公式讲解**：对提供的 LaTeX 公式，解释每个符号含义、公式在解决什么问题（没有公式则跳过）
   - **实验结果**：解读实验结果图/表格说明，提炼关键数字
   - **一句话 takeaway**
   - 必须保留可点击链接：`[论文链接](url)`
   - 图片引用格式：`![图注](image_url)`
   - 公式用 LaTeX 块：$$...$$
4. 其余 7 篇用「一句话短评 + 链接」列表展示。
5. 结尾给出 3 条「今日研究启发」。
6. 全文用 Markdown，可直接复制到公众号编辑器。

## TOP 3 论文（含全文/公式/图片）
{json.dumps(top3_cards, ensure_ascii=False, indent=2)}

## 其余 7 篇论文
{json.dumps(other7_cards, ensure_ascii=False, indent=2)}

请直接输出文章正文，不要输出"好的"等额外说明。
"""

    return llm_chat([{"role": "user", "content": prompt}], max_tokens=8000)


# ── 通知推送抽象层 ──

def markdown_to_text(markdown_text: str) -> str:
    """把 Markdown 转成适合微信群阅读的纯文本"""
    text = markdown_text
    # 移除图片引用，保留链接文本
    text = re.sub(r"!\[(.*?)\]\(.*?\)", r"[图:\1]", text)
    # 把链接转成 文本(url) 形式
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1(\2)", text)
    # 移除加粗、斜体标记
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"__(.*?)__", r"\1", text)
    text = re.sub(r"\*(.*?)\*", r"\1", text)
    text = re.sub(r"_(.*?)_", r"\1", text)
    # 代码块简化
    text = re.sub(r"```[\s\S]*?```", "[代码块]", text)
    text = re.sub(r"`(.*?)`", r"\1", text)
    # 标题前面加换行和 # 号提示
    text = re.sub(r"^#{1,6}\s+(.*)$", r"\n【\1】", text, flags=re.MULTILINE)
    # 列表符号统一
    text = re.sub(r"^\s*[-*+]\s+", "• ", text, flags=re.MULTILINE)
    # 水平线
    text = re.sub(r"^\s*-{3,}\s*$", "\n---\n", text, flags=re.MULTILINE)
    # 合并多余空行
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def push_dingtalk(markdown_text: str, webhook: str) -> bool:
    """钉钉 Markdown 推送"""
    if not webhook:
        print("  [SKIP] 未配置 DINGTALK_WEBHOOK，跳过钉钉推送")
        return False
    safe_text = markdown_text[:DINGTALK_MAX_LENGTH]
    if len(markdown_text) > DINGTALK_MAX_LENGTH:
        safe_text += "\n\n> 内容过长，已截断。完整版请查看仓库 Artifacts 或本地文件。"
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": "电力预测科研日报 v2.0",
            "text": safe_text,
        }
    }
    try:
        resp = requests.post(webhook, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode") == 0:
            print("  [OK] 钉钉推送成功")
            return True
        else:
            print(f"  [FAIL] 钉钉返回错误: {data}")
            return False
    except Exception as e:
        print(f"  [FAIL] 钉钉推送异常: {e}")
        return False


def push_wecom(title: str, content: str, webhook: str) -> bool:
    """企业微信群机器人文本推送"""
    if not webhook:
        print("  [SKIP] 未配置 WECOM_WEBHOOK，跳过企业微信推送")
        return False

    # 企业微信群机器人对 Markdown 支持弱，转成纯文本
    text_content = markdown_to_text(content)
    full_text = f"{title}\n\n{text_content}"

    # 按字节截断，避免超过 2048 字节上限
    encoded = full_text.encode("utf-8")
    if len(encoded) > WECOM_MAX_LENGTH:
        safe_bytes = encoded[:WECOM_MAX_LENGTH]
        full_text = safe_bytes.decode("utf-8", errors="ignore") + "\n\n[内容过长，完整版见日报文件]"

    # 日志隐藏完整 webhook key
    safe_log = webhook[:50] + "..." if len(webhook) > 50 else webhook
    print(f"  [PUSH] 推送到企业微信群: {safe_log}")

    payload = {
        "msgtype": "text",
        "text": {"content": full_text}
    }
    try:
        resp = requests.post(webhook, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode") == 0:
            print("  [OK] 企业微信推送成功")
            return True
        else:
            print(f"  [FAIL] 企业微信返回错误: {data}")
            return False
    except Exception as e:
        print(f"  [FAIL] 企业微信推送异常: {e}")
        return False


def push_pushbot(title: str, content: str, push_url: str, max_retries: int = 3) -> bool:
    """
    通过 push-bot 推送消息到微信群
    push_url 形如: https://your-push-bot.com/room/:token
    """
    if not push_url:
        print("  [SKIP] 未配置 PUSHBOT_URL，跳过 push-bot 推送")
        return False

    # 转换 Markdown 为纯文本，更适合微信群阅读
    text_content = markdown_to_text(content)

    # 组合标题和正文
    full_text = f"{title}\n\n{text_content}"

    # 截断超长内容
    if len(full_text) > PUSHBOT_MAX_LENGTH:
        full_text = full_text[:PUSHBOT_MAX_LENGTH] + "\n\n[内容过长，后续已截断]"

    # 日志里隐藏完整 URL，只显示前 30 字符
    safe_url_log = push_url[:30] + "..." if len(push_url) > 30 else push_url
    print(f"  [PUSH] 推送到 push-bot: {safe_url_log}")

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            # push-bot 支持 GET /room/:token?msg=xxx
            params = {"msg": full_text}
            resp = requests.get(push_url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json() if resp.text else {}
            print(f"    [OK] push-bot 推送成功 (尝试 {attempt}/{max_retries})")
            return True
        except Exception as e:
            last_error = e
            print(f"    [WARN] push-bot 推送失败 (尝试 {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                import time
                time.sleep(2 ** attempt)  # 指数退避
            continue

    print(f"  [FAIL] push-bot 推送最终失败: {last_error}")
    return False


def push_serverchan(title: str, content: str, sendkey: str, max_retries: int = 3) -> bool:
    """
    通过 Server 酱 Turbo 推送消息到个人微信
    需要在 sct.ftqq.com 后台配置好企业微信应用消息通道
    """
    if not sendkey:
        print("  [SKIP] 未配置 SERVERCHAN_SENDKEY，跳过 Server 酱推送")
        return False

    # Server 酱 支持 Markdown，但企业微信应用消息对 Markdown 支持有限
    # 这里保留 Markdown 链接，移除复杂格式
    safe_content = content
    if len(safe_content) > SERVERCHAN_MAX_LENGTH:
        safe_content = safe_content[:SERVERCHAN_MAX_LENGTH] + "\n\n[内容过长，后续已截断。完整版见日报文件或 GitHub Artifact。]"

    # 日志里隐藏 SendKey
    safe_key_log = sendkey[:10] + "..." if len(sendkey) > 10 else sendkey
    print(f"  [PUSH] 推送到 Server 酱 Turbo: {safe_key_log}")

    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    payload = {
        "title": title,
        "desp": safe_content,
    }

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, data=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") == 0 or data.get("errno") == 0:
                print(f"    [OK] Server 酱推送成功 (尝试 {attempt}/{max_retries})")
                return True
            else:
                print(f"    [WARN] Server 酱返回错误: {data}")
                last_error = data
        except Exception as e:
            last_error = e
            print(f"    [WARN] Server 酱推送失败 (尝试 {attempt}/{max_retries}): {e}")

        if attempt < max_retries:
            import time
            time.sleep(2 ** attempt)

    print(f"  [FAIL] Server 酱推送最终失败: {last_error}")
    return False


def get_notify_channels() -> list[str]:
    """从环境变量读取启用的通知通道列表"""
    channels_str = os.getenv(NOTIFY_CHANNELS_ENV, "serverchan,wecom,dingtalk").lower()
    return [c.strip() for c in channels_str.split(",") if c.strip()]


def send_notification(title: str, content: str) -> dict[str, bool]:
    """
    统一发送通知到所有配置的通道
    返回每个通道的推送结果
    """
    channels = get_notify_channels()
    print(f"\n[通知推送] 启用通道: {', '.join(channels)}")

    results = {}

    if "serverchan" in channels:
        sendkey = os.getenv(SERVERCHAN_SENDKEY_ENV, "")
        results["serverchan"] = push_serverchan(title, content, sendkey)

    if "wecom" in channels:
        webhook = os.getenv(WECOM_WEBHOOK_ENV, "")
        results["wecom"] = push_wecom(title, content, webhook)

    if "pushbot" in channels:
        pushbot_url = os.getenv(PUSHBOT_URL_ENV, "")
        results["pushbot"] = push_pushbot(title, content, pushbot_url)

    if "dingtalk" in channels:
        webhook = os.getenv(DINGTALK_WEBHOOK_ENV, "")
        results["dingtalk"] = push_dingtalk(content, webhook)

    return results


# ── 保存日报 ──

def save_report(content: str) -> Path:
    filename = f"电力预测科研日报_{today_str()}.md"
    filepath = OUTPUT_DIR / filename
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"\n[OK] 日报已保存: {filepath}")
    return filepath


# ── 主流程 ──

def main():
    print("=" * 60)
    print("电力预测科研日报生成器 v2.0")
    print("=" * 60)
    print(f"\n[开始时间] {now_cn().strftime('%Y-%m-%d %H:%M:%S')}")

    print("\n[Step 1/6] 搜索学术论文 (OpenAlex + arXiv)...")
    all_works = search_all()
    if not all_works:
        print("\n[失败] 没有搜到任何论文。")
        return

    print(f"\n[Step 2/6] 评分排序 ({len(all_works)} 篇)...")
    scored = [(score_paper(w)[0], w, score_paper(w)[1]) for w in all_works]
    scored.sort(key=lambda x: x[0], reverse=True)
    top10 = scored[:10]

    print("\n  TOP 10 候选:")
    for i, (s, w, d) in enumerate(top10, 1):
        title = (w.get("title") or "未知")[:55]
        _, jname = get_journal_score(w)
        print(f"  {i:2d}. [{s:.0f}分] {title}")
        print(f"      期刊:{jname} | {d}")

    print(f"\n[Step 3/6] 下载并解析 TOP 3 论文全文/图表/公式...")
    repo = os.getenv(GITHUB_REPO_ENV, "")
    token = os.getenv(GITHUB_TOKEN_ENV, "")
    enriched_top10 = []
    for rank, (s, w, d) in enumerate(top10, 1):
        print(f"\n  [{rank}/10] {w.get('title', '未知')[:50]}...")
        enrich = enrich_paper(w)
        # 上传图片
        for img in enrich.get("images", []):
            img["url"] = get_image_public_url(img["path"], repo if repo else None, token if token else None)
        enriched_top10.append((s, w, d, enrich))
        print(f"      解析状态: {enrich['parse_status']}, 文本 {len(enrich['full_text'])} 字, "
              f"图片 {len(enrich['images'])}, 公式 {len(enrich['formulas'])}")

    print(f"\n[Step 4/6] 用 LLM 生成深度解读文章...")
    try:
        article = generate_article(enriched_top10)
    except Exception as e:
        print(f"\n[失败] LLM 生成失败: {e}")
        return

    print(f"\n[Step 5/6] 保存日报...")
    filepath = save_report(article)

    print(f"\n[Step 6/6] 推送通知...")
    title = "电力预测科研日报 v2.0"
    send_notification(title, article)

    print(f"\n{'='*60}")
    print("[完成] 日报已生成并尝试推送")
    print(f"[文件] {filepath}")
    print(f"{'='*60}")

    print("\n---REPORT_START---")
    print(article)
    print("---REPORT_END---")


if __name__ == "__main__":
    main()
