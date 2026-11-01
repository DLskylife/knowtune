"""多格式抽取：把任意文件变成 (纯文本 + 结构块)。

原则：抽取失败必须给出**人能读懂的原因**，绝不静默返回空文档。
扫描件 PDF 是最典型的坑——不报错、直接入库、检索永远召回不到，用户会怀疑
整套系统坏了，实际是文本层根本不存在。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..models import Block, LoadedDoc
from ..textnorm import fold

MAX_PREVIEW_CHARS = 200


class ExtractError(Exception):
    """抽取失败。message 会直接展示给用户，所以要写人话。"""


TEXT_EXTS = {
    ".md", ".markdown", ".txt", ".text", ".rst",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".c", ".cc",
    ".cpp", ".h", ".hpp", ".rb", ".php", ".sh", ".ps1", ".sql", ".css",
    ".html", ".htm", ".json", ".yaml", ".yml", ".toml", ".ini", ".conf",
    ".csv", ".log",
}
CODE_EXTS = TEXT_EXTS - {".md", ".markdown", ".txt", ".text", ".rst", ".html", ".htm", ".csv", ".log"}
SUPPORTED_EXTS = TEXT_EXTS | {".pdf", ".docx", ".xlsx", ".pptx"}


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """返回 (文本, 编码)。中文文档 GBK 比例不低，只试 utf-8 会炸一批。"""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "utf-16", "latin-1"):
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8-replace"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _split_markdown(text: str, default_page: int | None = None) -> list[Block]:
    """按标题层级与代码围栏切块，为每段记下所属章节路径。

    章节路径是后面"引用可回查"的关键：用户看到 [2] 点开，要知道它在
    《F2 检索层 > 混合融合》里，而不是一堆没有上下文的散句。
    """
    blocks: list[Block] = []
    stack: list[tuple[int, str]] = []
    para: list[str] = []
    code: list[str] = []
    in_code = False
    fence = ""

    def heading_path() -> str | None:
        return " > ".join(t for _, t in stack) if stack else None

    def flush_para() -> None:
        if not para:
            return
        body = "\n".join(para).strip()
        if body:
            blocks.append(
                Block(text=body, page_no=default_page, heading_path=heading_path(), kind="para")
            )
        para.clear()

    def flush_code() -> None:
        if not code:
            return
        body = "\n".join(code).strip("\n")
        if body.strip():
            blocks.append(
                Block(text=body, page_no=default_page, heading_path=heading_path(), kind="code")
            )
        code.clear()

    for line in text.splitlines():
        stripped = line.strip()
        if in_code:
            if stripped.startswith(fence):
                in_code = False
                flush_code()
            else:
                code.append(line)
            continue

        if stripped.startswith("```") or stripped.startswith("~~~"):
            flush_para()
            in_code = True
            fence = stripped[:3]
            continue

        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            title = stripped[level:].strip()
            if title and level <= 6:
                flush_para()
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, title))
                blocks.append(
                    Block(text=title, page_no=default_page, heading_path=heading_path(), kind="heading")
                )
                continue

        if not stripped:
            flush_para()
            continue

        para.append(line)

    if in_code:
        flush_code()
    flush_para()
    return blocks


def _load_text_file(path: Path) -> LoadedDoc:
    raw = path.read_bytes()
    text, enc = decode_bytes(raw)
    if not text.strip():
        raise ExtractError(f"文件内容为空（编码判定为 {enc}）")

    ext = path.suffix.lower()
    if ext in {".md", ".markdown"}:
        blocks = _split_markdown(text)
    elif ext in {".html", ".htm"}:
        blocks = _split_html(text)
    else:
        kind = "code" if ext in CODE_EXTS else "para"
        blocks = [Block(text=text.strip(), kind=kind)]

    if not blocks:
        raise ExtractError("解析后没有任何可索引内容")

    return LoadedDoc(
        text=text,
        blocks=blocks,
        title=path.stem,
        n_pages=None,
        extractor=f"text@{enc}",
        meta={"path": str(path), "ext": ext, "size_bytes": len(raw)},
    )


def _split_html(text: str) -> list[Block]:
    """HTML 优先用 trafilatura 抽正文，退化为去标签。"""
    try:
        import trafilatura

        main = trafilatura.extract(text, include_comments=False, include_tables=True)
        if main and main.strip():
            return [Block(text=p.strip(), kind="para") for p in main.split("\n\n") if p.strip()]
    except ImportError:
        pass
    except Exception:
        pass

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        body = soup.get_text("\n")
        return [Block(text=p.strip(), kind="para") for p in body.split("\n") if p.strip()]
    except ImportError:
        raise ExtractError(
            "解析 HTML 需要 beautifulsoup4 或 trafilatura；"
            '请执行 pip install -e ".[extract]"'
        ) from None


def _load_pdf(path: Path, min_text_chars: int) -> LoadedDoc:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ExtractError('解析 PDF 需要 pypdf；请执行 pip install -e ".[extract]"') from None

    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise ExtractError(f"PDF 打开失败：{type(exc).__name__}: {exc}") from exc

    if reader.is_encrypted:
        raise ExtractError("PDF 已加密，无法读取文本层；请先解密再导入")

    blocks: list[Block] = []
    empty_pages: list[int] = []
    for idx, page in enumerate(reader.pages, start=1):
        try:
            page_text = page.extract_text() or ""
        except Exception:
            page_text = ""
        if len(page_text.strip()) < min_text_chars:
            empty_pages.append(idx)
            continue
        for para in [p.strip() for p in page_text.split("\n\n") if p.strip()]:
            blocks.append(Block(text=para, page_no=idx, kind="para"))

    if not blocks:
        raise ExtractError(
            f"未检测到文本层（{len(reader.pages)} 页全部无有效文本），这是扫描件 PDF，需要 OCR 后再导入。"
            "本系统不做 OCR，请先用外部工具转成可复制文本的 PDF 或 Markdown。"
        )

    n_pages = len(reader.pages)
    text = "\n\n".join(b.text for b in blocks)
    meta = {"path": str(path), "n_pages": n_pages}
    if empty_pages:
        meta["pages_without_text"] = empty_pages
    return LoadedDoc(
        text=text,
        blocks=blocks,
        title=path.stem,
        n_pages=n_pages,
        extractor="pypdf",
        meta=meta,
    )


def _para_text(para) -> str:
    """段落全文：正文 + 超链接 + 修订插入 + 文本框。

    为什么不能直接用 `para.text`：python-docx 的 `text` 只取段落直属的 run，
    于是"文档里看得见的字"有三类会被静默丢掉——超链接文字、接受后的修订、
    以及**文本框**（w:txbxContent）。尤其文本框：中医/藏医这类资料大量用文本框
    排版，整段整段的内容不进 paragraphs，用户看到的就是"文件没全进去"。

    反过来，被删除的修订（w:del 里的 w:t）不能要：那是作者划掉的文字，
    混进来会让检索命中一段文档里根本不存在的句子。用 xpath 只取三层
    明确路径，天然排除了 w:del。
    """
    try:
        nodes = para._p.xpath(
            "./w:r/w:t | ./w:hyperlink//w:t | ./w:ins//w:t | .//w:txbxContent//w:t"
        )
    except Exception:  # 结构异常时退回官方 API，宁可少拿也不能抛
        return para.text or ""
    out: list[str] = []
    for n in nodes:
        t = n.text or ""
        if t:
            out.append(t)
    # 直接拼接，不插空格：中文段落常被 Word 拆成好几个 run，加空格会把
    # "藏医" 变成 "藏 医"，原文和 FTS 分词同时被破坏。
    return "".join(out) if out else (para.text or "")


def _table_text(table) -> str:
    """表格 → 每行一段 `列 | 列 | 列`。

    保留 `|` 而不是直接拼字符串：表格的语义在"这一格属于这一行这一列"，
    拼成一坨之后检索到的片段无法判断哪个值对应哪个字段。同时 chunker 的
    _hard_split 会按句子边界处理超长行，不会破坏结构。
    """
    lines: list[str] = []
    for row in table.rows:
        cells: list[str] = []
        for cell in row.cells:
            # 合并单元格会让相邻 cell.text 完全相同，不去重一行里会出现三遍
            val = (cell.text or "").strip().replace("\n", " ")
            if val and val not in cells:
                cells.append(val)
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def _iter_flow_items(document):
    """按**文档流顺序**产出段落与表格。

    `document.paragraphs` 只给段落，表格是另一个集合，于是表格内容整体丢失，
    而且就算分别遍历也无法还原"表格在正文的哪个位置"。按 body 的子元素
    顺序走，才跟读者看到的顺序一致——引用回查时页码/章节才对得上。
    """
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)


def _load_docx(path: Path) -> LoadedDoc:
    try:
        import docx
    except ImportError:
        raise ExtractError('解析 Word 需要 python-docx；请执行 pip install -e ".[extract]"') from None

    try:
        document = docx.Document(str(path))
    except Exception as exc:
        raise ExtractError(f"Word 打开失败：{type(exc).__name__}: {exc}") from exc

    blocks: list[Block] = []
    stack: list[tuple[int, str]] = []
    for item in _iter_flow_items(document):
        style = ""
        if hasattr(item, "style") and item.style is not None:
            style = (item.style.name or "").lower()
        is_table = style.startswith("table") or not hasattr(item, "runs")
        body = (_table_text(item) if is_table else _para_text(item)).strip()
        if not body:
            continue
        if not is_table and style.startswith("heading"):
            digits = "".join(ch for ch in style if ch.isdigit())
            level = int(digits) if digits else 1
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, body))
            blocks.append(
                Block(text=body, kind="heading", heading_path=" > ".join(t for _, t in stack))
            )
        else:
            blocks.append(
                Block(
                    text=body,
                    kind="table" if is_table else "para",
                    heading_path=" > ".join(t for _, t in stack) if stack else None,
                )
            )

    if not blocks:
        raise ExtractError("Word 文档没有可抽取的段落文本（可能内容全在图片里）")

    return LoadedDoc(
        text="\n\n".join(b.text for b in blocks),
        blocks=blocks,
        title=path.stem,
        n_pages=None,
        extractor="python-docx/flow",
        meta={"path": str(path)},
    )


def _load_xlsx(path: Path) -> LoadedDoc:
    try:
        import openpyxl
    except ImportError:
        raise ExtractError('解析 Excel 需要 openpyxl；请执行 pip install -e ".[extract]"') from None

    try:
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as exc:
        raise ExtractError(f"Excel 打开失败：{type(exc).__name__}: {exc}") from exc

    blocks: list[Block] = []
    for sheet in wb.worksheets:
        header: list[str] = []
        for row_idx, row in enumerate(sheet.iter_rows(values_only=True), start=1):
            cells = ["" if c is None else str(c).strip() for c in row]
            if not any(cells):
                continue
            if row_idx == 1:
                header = cells
                continue
            if header and len(header) == len(cells):
                line = " | ".join(f"{h}: {v}" for h, v in zip(header, cells) if v)
            else:
                line = " | ".join(c for c in cells if c)
            if line:
                blocks.append(Block(text=line, kind="table", heading_path=sheet.title))
    wb.close()

    if not blocks:
        raise ExtractError("Excel 中没有可抽取的单元格数据")

    return LoadedDoc(
        text="\n".join(b.text for b in blocks),
        blocks=blocks,
        title=path.stem,
        n_pages=None,
        extractor="openpyxl",
        meta={"path": str(path)},
    )


def _load_pptx(path: Path) -> LoadedDoc:
    try:
        from pptx import Presentation
    except ImportError:
        raise ExtractError('解析 PPT 需要 python-pptx；请执行 pip install -e ".[extract]"') from None

    try:
        prs = Presentation(str(path))
    except Exception as exc:
        raise ExtractError(f"PPT 打开失败：{type(exc).__name__}: {exc}") from exc

    blocks: list[Block] = []
    for idx, slide in enumerate(prs.slides, start=1):
        parts: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    body = "".join(run.text for run in para.runs).strip()
                    if body:
                        parts.append(body)
        if parts:
            blocks.append(Block(text="\n".join(parts), page_no=idx, kind="para"))

    if not blocks:
        raise ExtractError("PPT 中没有可抽取的文字（可能整份都是图片）")

    return LoadedDoc(
        text="\n\n".join(b.text for b in blocks),
        blocks=blocks,
        title=path.stem,
        n_pages=len(prs.slides),
        extractor="python-pptx",
        meta={"path": str(path)},
    )


def load_path(path: str | Path, *, min_pdf_text_chars: int = 20) -> LoadedDoc:
    p = Path(path)
    if not p.exists():
        raise ExtractError(f"文件不存在：{p}")
    if not p.is_file():
        raise ExtractError(f"不是普通文件：{p}")

    ext = p.suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise ExtractError(
            f"不支持的格式 {ext or '(无扩展名)'}；支持：{', '.join(sorted(SUPPORTED_EXTS))}"
        )

    if ext == ".pdf":
        doc = _load_pdf(p, min_pdf_text_chars)
    elif ext == ".docx":
        doc = _load_docx(p)
    elif ext == ".xlsx":
        doc = _load_xlsx(p)
    elif ext == ".pptx":
        doc = _load_pptx(p)
    else:
        doc = _load_text_file(p)
    return _folded(doc)


def _folded(doc: LoadedDoc) -> LoadedDoc:
    """抽取出口统一折叠兼容码位（见 textnorm）。

    放在出口而不是各家格式里，是因为这件事和格式无关：任何抽取器都可能吐出
    康熙部首，漏掉一条路径就等于留一个"这个格式的文档召回率莫名其妙低"的坑。
    blocks 和 text 两份都要改——分块器读 blocks，content_hash 读 text，只改
    一份会让"内容没变"的判断失效。
    """
    doc.text = fold(doc.text)
    for b in doc.blocks:
        b.text = fold(b.text)
    return doc


def load_inline_text(text: str, title: str) -> LoadedDoc:
    """粘贴导入。走与文件相同的 markdown 解析，保证两条路径行为一致。"""
    if not text.strip():
        raise ExtractError("内容为空")
    blocks = _split_markdown(text)
    if not blocks:
        raise ExtractError("解析后没有任何可索引内容")
    return _folded(LoadedDoc(
        text=text,
        blocks=blocks,
        title=title or f"pasted-{_sha256_text(text)[:8]}",
        n_pages=None,
        extractor="inline",
        meta={"chars": len(text)},
    ))


# --------------------------------------------------------------------- URL


def _assert_public_host(host: str) -> None:
    """SSRF 防护：拒绝回环 / 私网 / 链路本地地址。

    个人本机跑的服务如果允许抓 127.0.0.1，一个恶意网页就能让它去读
    本机管理接口。这不是"生产级加固"，是十分钟能做完的基本防线。
    """
    import ipaddress
    import socket

    if not host:
        raise ExtractError("URL 缺少主机名")

    candidates: list[str] = []
    try:
        ipaddress.ip_address(host)
        candidates.append(host)
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError as exc:
            raise ExtractError(f"域名解析失败：{host}（{exc}）") from exc
        candidates.extend({info[4][0] for info in infos})

    for raw in candidates:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ExtractError(f"拒绝访问内网/本机地址：{host} → {raw}")


def _extract_title(html: str) -> str | None:
    import re

    for pattern in (r"<title[^>]*>(.*?)</title>", r"<h1[^>]*>(.*?)</h1>"):
        m = re.search(pattern, html, re.S | re.I)
        if m:
            import html as html_mod

            title = html_mod.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
            if title:
                return re.sub(r"\s+", " ", title)[:200]
    return None


def load_url(
    url: str,
    *,
    timeout_s: float = 20.0,
    max_bytes: int = 5 * 1024 * 1024,
    allowed_domains: list[str] | None = None,
) -> LoadedDoc:
    """抓一个 URL 并抽正文。

    显式限制：只收 http/https、只收文本类型、体积上限、域名白名单（可选）。
    任何一条不满足都抛 ExtractError 给人话，不静默存空文档。
    """
    from urllib.parse import urlparse

    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ExtractError(f"只支持 http/https，收到 {parsed.scheme or '(空)'}")
    host = parsed.hostname or ""

    if allowed_domains:
        allowed = [d.lower().lstrip(".") for d in allowed_domains]
        if not any(host.lower() == d or host.lower().endswith("." + d) for d in allowed):
            raise ExtractError(
                f"域名不在白名单内：{host}；当前白名单 {', '.join(allowed)}"
            )

    _assert_public_host(host)

    try:
        import httpx
    except ImportError:
        raise ExtractError('抓取 URL 需要 httpx；请执行 pip install -e "."') from None

    try:
        with httpx.Client(
            timeout=httpx.Timeout(connect=8.0, read=timeout_s, write=10.0, pool=5.0),
            follow_redirects=True,
            headers={"User-Agent": "knowtune/0.1 (+local personal knowledge base)"},
        ) as client:
            resp = client.get(url)
    except httpx.HTTPError as exc:
        raise ExtractError(f"抓取失败：{type(exc).__name__}: {exc}") from exc

    if resp.status_code >= 400:
        raise ExtractError(f"抓取失败：HTTP {resp.status_code}")

    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype and not (
        ctype.startswith("text/") or ctype in {"application/xhtml+xml", "application/xml"}
    ):
        raise ExtractError(f"不支持的内容类型：{ctype}（只收录 HTML/纯文本）")

    raw = resp.content
    if len(raw) > max_bytes:
        raise ExtractError(f"页面超过 {max_bytes // (1024 * 1024)}MB，拒绝入库")

    text, enc = decode_bytes(raw)
    if not text.strip():
        raise ExtractError("页面内容为空")

    ext = ".html" if "html" in ctype or "<html" in text[:2000].lower() else ".txt"
    if ext == ".html":
        blocks = _split_html(text)
    else:
        blocks = [Block(text=text.strip(), kind="para")]

    if not blocks:
        raise ExtractError("页面没有抽到正文（可能是纯脚本渲染的页面，需要先转成静态 HTML）")

    title = _extract_title(text) or parsed.path.rstrip("/").split("/")[-1] or host
    return _folded(LoadedDoc(
        text=text,
        blocks=blocks,
        title=title,
        n_pages=None,
        extractor=f"http@{enc}",
        meta={"url": str(resp.url), "status": resp.status_code, "bytes": len(raw)},
    ))
