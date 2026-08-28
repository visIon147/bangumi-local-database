from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from markdown_it.token import Token
from markdown_it.utils import EnvType, OptionsDict


class PublicDocumentError(LookupError):
    """Raised when a bundled public help document cannot be loaded safely."""


@dataclass(frozen=True, slots=True)
class PublicDocument:
    slug: str
    filename: str
    title: str
    description: str
    markdown: str
    html: str


DOCUMENTS = {
    "ui-guide": (
        "UI_GUIDE.md",
        "UI 使用指南",
        "页面导航、安全计划操作、Steam、评分、探索、图片和故障排查。",
    ),
    "readme": (
        "README.md",
        "项目 README",
        "安装、配置、CLI、同步模型、版本能力和公开发布边界。",
    ),
    "steam-setup": (
        "STEAM_SETUP.md",
        "Steam 配置指南",
        "Steam 手机令牌、Web API Key、本地账户 ID、Steam ID64 与故障排查。",
    ),
    "changelog": (
        "CHANGELOG.md",
        "版本变更",
        "正式版本的用户功能、安装方式和已知限制。",
    ),
    "license": (
        "LICENSE",
        "MIT License",
        "本项目的开源许可条款。",
    ),
}
_PACKAGE_CONTENT = Path(__file__).resolve().parent / "content"
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_MAX_DOCUMENT_BYTES = 2 * 1024 * 1024


class _PublicDocumentRenderer(RendererHTML):
    def link_open(
        self,
        tokens: Sequence[Token],
        index: int,
        options: OptionsDict,
        env: EnvType,
    ) -> str:
        token = tokens[index]
        href = token.attrGet("href") or ""
        if href == "UI_GUIDE.md":
            token.attrSet("href", "/help/ui-guide")
        elif href == "README.md":
            token.attrSet("href", "/help/readme")
        elif href == "STEAM_SETUP.md":
            token.attrSet("href", "/help/steam-setup")
        elif href == "CHANGELOG.md":
            token.attrSet("href", "/help/changelog")
        elif href == "LICENSE":
            token.attrSet("href", "/help/license")
        elif href.startswith(("https://", "http://")):
            token.attrSet("target", "_blank")
            token.attrSet("rel", "noopener noreferrer")
        return self.renderToken(tokens, index, options, env)


_MARKDOWN = MarkdownIt(
    "commonmark",
    options_update={"html": False, "linkify": False, "typographer": False},
    renderer_cls=_PublicDocumentRenderer,
)


def render_public_markdown(markdown: str) -> str:
    """Render trusted release Markdown while keeping embedded HTML disabled."""

    return _MARKDOWN.render(markdown)


def _document_path(filename: str) -> Path:
    for candidate in (_PACKAGE_CONTENT / filename, _PROJECT_ROOT / filename):
        if candidate.is_file():
            return candidate
    raise PublicDocumentError(f"Public document is unavailable: {filename}")


def load_public_document(slug: str) -> PublicDocument:
    definition = DOCUMENTS.get(slug)
    if definition is None:
        raise PublicDocumentError(f"Unknown public document: {slug}")
    filename, title, description = definition
    path = _document_path(filename)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise PublicDocumentError(f"Public document cannot be read: {filename}") from exc
    if len(payload) > _MAX_DOCUMENT_BYTES:
        raise PublicDocumentError(f"Public document is too large: {filename}")
    try:
        markdown = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PublicDocumentError(f"Public document is not UTF-8: {filename}") from exc
    return PublicDocument(
        slug=slug,
        filename=filename,
        title=title,
        description=description,
        markdown=markdown,
        html=render_public_markdown(markdown),
    )


def public_document_catalog() -> tuple[PublicDocument, ...]:
    return tuple(load_public_document(slug) for slug in DOCUMENTS)
