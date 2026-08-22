"""联网内容包：web_search（DDG HTML）+ web_fetch（httpx 抓取 + 正则抽正文）。

todo #7 内容包；SSRF 防护：只放行公网 http(s)，拒绝解析到私有/回环/链路本地地址。
"""

from __future__ import annotations

import html
import ipaddress
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_REDIRECT_STATUS = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 5


class NetworkPolicyError(ValueError):
    """URL 不满足网络策略（非 http(s) 或解析到非公网地址）。"""


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str


async def search_web(query: str, max_results: int = 5, timeout: float = 15.0) -> list[SearchResult]:
    """DDG HTML 端点检索，返回标题/链接/摘要。"""
    url = f"https://duckduckgo.com/html/?q={quote(query)}"
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(url, headers={"user-agent": _USER_AGENT})
        response.raise_for_status()
    return _parse_duckduckgo(response.text)[:max_results]


async def fetch_url(url: str, max_length: int = 10_000, timeout: float = 15.0) -> str:
    """抓取公开页面；HTML 抽正文、按 max_length 截断。

    SSRF 防护覆盖重定向：关闭自动跟随，每一跳都先过 `_validate_public_url`，
    最多 _MAX_REDIRECTS 跳，超限抛 NetworkPolicyError。
    """
    _validate_public_url(url)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            response = await client.get(current, headers={"user-agent": _USER_AGENT})
            location = response.headers.get("location")
            if response.status_code in _REDIRECT_STATUS and location:
                current = str(response.url.join(location))
                _validate_public_url(current)
                continue
            response.raise_for_status()
            break
        else:
            raise NetworkPolicyError(f"too many redirects while fetching: {url}")
    text = response.text
    if "html" in response.headers.get("content-type", ""):
        text = _html_to_markdown(text)
    if len(text) > max_length:
        text = text[:max_length] + "\n... [truncated]"
    return text or "(empty page)"


def _parse_duckduckgo(raw_html: str) -> list[SearchResult]:
    pattern = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>'
        r"[\s\S]*?"
        r'<a[^>]+class="result__snippet"[^>]*>([\s\S]*?)</a>',
        re.I,
    )
    results = []
    for match in pattern.finditer(raw_html):
        url = _normalize_duckduckgo_url(html.unescape(match.group(1)))
        if "y.js" in url:  # DDG 广告跳转，丢弃
            continue
        results.append(
            SearchResult(
                title=_clean(match.group(2)),
                url=url,
                snippet=_clean(match.group(3)),
            )
        )
    return results


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def _normalize_duckduckgo_url(url: str) -> str:
    """DDG 跳转链接解出真实目标 URL。"""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if "uddg" in params and params["uddg"]:
        return unquote(params["uddg"][0])
    return url


class _HtmlToMarkdown(HTMLParser):
    """把 HTML 粗略转成 Markdown（stdlib 解析，维护标签栈）。

    保留标题/列表/引用/分隔线/粗斜体/行内代码/链接/代码块；
    表格按行拼纯文本；图片只留非空 alt；跳过噪声标签子树。
    """

    _BLOCK_TAGS = {
        "h1", "h2", "h3", "h4", "h5", "h6",
        "p", "div", "section", "article", "main", "figure", "figcaption", "form",
        "ul", "ol", "li", "blockquote", "pre", "table", "tr", "td", "th", "hr", "br",
    }
    _NOISE_TAGS = {"script", "style", "nav", "footer", "header", "aside", "iframe", "noscript"}
    _NO_SPACE_BEFORE = (
        ".", ",", ";", ":", "!", "?", ")", "]", "}", "…",
        # 全角标点同样不应前置空格（中文内容）
        "。", "，", "、", "；", "：", "！", "？", "）", "」", "』", "》", "”", "’",
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.line_closed = True
        self.skip_depth = 0
        self.list_stack: list[list[bool, int]] = []
        self.in_pre = False
        self.pre_started = False
        self.in_blockquote = False
        self.in_li = False
        self.inline_open = False
        self.pending_link_href: str | None = None

    # --- 输出原语 ---
    def _ensure_line(self) -> None:
        """确保有可写的当前行；新行在 blockquote 内带 '> ' 前缀。"""
        if self.line_closed or not self.out:
            self.out.append("> " if self.in_blockquote else "")
            self.line_closed = False

    def _close_line(self) -> None:
        self.line_closed = True

    def _ensure_gap(self) -> None:
        """块之间补空行；blockquote 内只换行，列表项内续当前行。"""
        if self.in_blockquote:
            self._close_line()
            return
        if self.in_li:
            return
        if self.out and self.out[-1] != "":
            self.out.append("")
        self.line_closed = True

    def _write_marker(self, marker: str) -> None:
        self._ensure_line()
        self.out[-1] += marker

    def _write_text(self, text: str) -> None:
        """行内文本：折叠空白；格式 span 内贴合，span 外补单空格。"""
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return
        self._ensure_line()
        if self.inline_open:
            self.out[-1] += text
        else:
            cur = self.out[-1]
            if cur and not cur.endswith(" ") and not text.startswith(self._NO_SPACE_BEFORE):
                self.out[-1] = cur + " " + text
            else:
                self.out[-1] = cur + text

    def _open_inline(self, marker: str) -> None:
        self._ensure_line()
        cur = self.out[-1]
        if cur and not cur.endswith(" "):
            cur += " "
        self.out[-1] = cur + marker
        self.inline_open = True

    def _close_inline(self, marker: str) -> None:
        self._ensure_line()
        self.out[-1] += marker
        self.inline_open = False

    def _write_raw(self, text: str) -> None:
        """逐字输出（pre 内）。"""
        self._ensure_line()
        self.out[-1] += text

    # --- HTMLParser 回调 ---
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.skip_depth:
            if tag in self._NOISE_TAGS:
                self.skip_depth += 1
            return
        if tag in self._NOISE_TAGS:
            self.skip_depth = 1
            return
        attrs = dict(attrs)
        if tag in self._BLOCK_TAGS:
            self._handle_block_start(tag)
        elif tag in {"strong", "b"}:
            self._open_inline("**")
        elif tag in {"em", "i"}:
            self._open_inline("*")
        elif tag == "code":
            if not self.in_pre:
                self._open_inline("`")
        elif tag == "a":
            self.pending_link_href = attrs.get("href") or ""
            self._open_inline("[")
        elif tag == "img":
            alt = (attrs.get("alt") or "").strip()
            if alt:
                self._write_text(alt)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self.skip_depth:
            if tag in self._NOISE_TAGS:
                self.skip_depth -= 1
            return
        if tag in self._NOISE_TAGS:
            return
        if tag in {"strong", "b"}:
            self._close_inline("**")
        elif tag in {"em", "i"}:
            self._close_inline("*")
        elif tag == "code":
            if not self.in_pre:
                self._close_inline("`")
        elif tag == "a":
            href = self.pending_link_href or ""
            self.pending_link_href = None
            self._close_inline(f"]({href})")
        elif tag == "pre":
            self.in_pre = False
            self._close_line()
            self._write_marker("```")
            self._close_line()
        elif tag in {"ul", "ol"}:
            if self.list_stack:
                self.list_stack.pop()
            self._close_line()
        elif tag == "li":
            self.in_li = False
            self._close_line()
        elif tag == "blockquote":
            self.in_blockquote = False
            self._close_line()
        elif tag == "tr":
            if self.out and not self.line_closed and self.out[-1].endswith(" | "):
                self.out[-1] = self.out[-1][:-3]
            self._close_line()
        elif tag in {"td", "th"}:
            self._write_marker(" | ")
        elif tag == "table":
            self._close_line()
        elif tag in self._BLOCK_TAGS:
            self._close_line()

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self.in_pre:
            if not self.pre_started:
                self.pre_started = True
                data = data.lstrip("\n")
            self._write_raw(data)
        else:
            self._write_text(data)

    def _handle_block_start(self, tag: str) -> None:
        if tag in {"ul", "ol"}:
            if not self.list_stack:
                self._ensure_gap()
            self.list_stack.append([tag == "ol", 0])
            return
        if tag == "li":
            if self.list_stack:
                ordered, n = self.list_stack[-1]
                marker = f"{n + 1}. " if ordered else "- "
                if ordered:
                    self.list_stack[-1][1] = n + 1
            else:
                marker = "- "
            indent = "  " * (len(self.list_stack) - 1)
            if not self.line_closed:
                self._close_line()
            self.out.append(indent + marker)
            self.line_closed = False
            self.in_li = True
            return
        if tag == "pre":
            self._ensure_gap()
            self._write_marker("```")
            self._close_line()
            self.in_pre = True
            self.pre_started = False
            return
        if tag == "hr":
            self._ensure_gap()
            self._write_marker("---")
            self._close_line()
            return
        if tag == "br":
            self._close_line()
            return
        if tag == "blockquote":
            self._ensure_gap()
            self.in_blockquote = True
            self._close_line()
            return
        if tag in {"table", "tr", "td", "th"}:
            if tag == "table":
                self._ensure_gap()
            return
        if len(tag) == 2 and tag[0] == "h" and tag[1].isdigit():
            self._ensure_gap()
            self._write_marker("#" * int(tag[1]) + " ")
            return
        self._ensure_gap()


def _html_to_markdown(raw_html: str) -> str:
    """把 HTML 粗略转成 Markdown 文本；解析异常时返回空串。"""
    parser = _HtmlToMarkdown()
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception:
        return ""
    return "\n".join(parser.out).strip()


def _validate_public_url(url: str) -> None:
    """只放行公网 http(s)；字面 IP 直接判，域名则解析后判。

    NetworkPolicyError 派生自 ValueError，判私网时必须让异常透传，
    不能包进「非 IP 主机名」的 except 里（否则 127.0.0.1 会被放过）。
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise NetworkPolicyError("only http/https URLs are allowed")
    if not parsed.hostname:
        raise NetworkPolicyError("URL must include a hostname")
    host = parsed.hostname
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None  # 非字面 IP，走域名解析
    if ip is not None:
        if _is_internal_address(ip):
            raise NetworkPolicyError(f"URL resolves to a non-public address: {ip}")
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise NetworkPolicyError(f"cannot resolve host: {host}") from exc
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _is_internal_address(ip):
            raise NetworkPolicyError(f"URL resolves to a non-public address: {ip}")


_FAKE_IP_RANGE = ipaddress.ip_network("198.18.0.0/15")


def _is_internal_address(ip) -> bool:
    """回环/链路本地/组播/私网判内网。

    198.18/15 是透明代理（Clash/Surge fake-ip）重写公网域名的保留段，
    判私网时放行，否则带代理的机器上所有公网域名都会被误拦。
    """
    if ip.is_loopback or ip.is_link_local or ip.is_multicast:
        return True
    if ip.version == 6:
        return ip.is_private  # fc00::/7 ULA
    return ip.is_private and ip not in _FAKE_IP_RANGE
