"""Generate the CodeWiki static HTML viewer from markdown output."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


class HTMLGenerationError(RuntimeError):
    """Raised when the HTML viewer cannot be generated."""


class HTMLGenerator:
    """Creates the same client-side markdown viewer used by CodeWiki."""

    def __init__(self, template_dir: Path | None = None) -> None:
        if template_dir is None:
            template_dir = Path(__file__).parent / "templates" / "github_pages"
        self.template_dir = Path(template_dir)

    def generate(
        self,
        *,
        output_path: Path,
        title: str,
        docs_dir: Path,
        module_tree: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        repository_url: str | None = None,
        github_pages_url: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        docs_dir = Path(docs_dir)
        output_path = Path(output_path)
        module_tree = module_tree if module_tree is not None else self.load_module_tree(docs_dir)
        metadata = metadata if metadata is not None else self.load_metadata(docs_dir)
        config = config or {}

        template_path = self.template_dir / "viewer_template.html"
        if not template_path.exists():
            raise HTMLGenerationError(f"Template not found: {template_path}")

        info_content = self._build_info_content(metadata)
        repo_link = ""
        if repository_url:
            repo_link = (
                f'<a href="{self._escape_html(repository_url)}" '
                'class="repo-link" target="_blank">View Repository</a>'
            )

        docs_base_path = ""
        if output_path.parent != docs_dir:
            docs_base_path = docs_dir.name

        replacements = {
            "{{TITLE}}": self._escape_html(title),
            "{{REPO_LINK}}": repo_link,
            "{{SHOW_INFO}}": "block" if info_content else "none",
            "{{INFO_CONTENT}}": info_content,
            "{{CONFIG_JSON}}": json.dumps(config, indent=2),
            "{{MODULE_TREE_JSON}}": json.dumps(module_tree, indent=2),
            "{{METADATA_JSON}}": json.dumps(metadata, indent=2) if metadata else "null",
            "{{DOCS_CONTENT_JSON}}": json.dumps(
                self._load_markdown_documents(docs_dir),
                ensure_ascii=False,
                indent=2,
            ),
            "{{DOCS_BASE_PATH}}": docs_base_path,
        }

        html_content = template_path.read_text(encoding="utf-8")
        for placeholder, value in replacements.items():
            html_content = html_content.replace(placeholder, value)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(html_content, encoding="utf-8")

    def detect_repository_info(self, repo_path: Path) -> dict[str, str | None]:
        info = {
            "name": Path(repo_path).name,
            "url": None,
            "github_pages_url": None,
        }
        git_config = Path(repo_path) / ".git" / "config"
        if not git_config.exists():
            return info

        remote_url = None
        in_origin = False
        for line in git_config.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if stripped.startswith("[remote "):
                in_origin = stripped == '[remote "origin"]'
                continue
            if in_origin and stripped.startswith("url = "):
                remote_url = stripped.split("=", 1)[1].strip()
                break

        if not remote_url:
            return info

        if remote_url.startswith("git@github.com:"):
            remote_url = remote_url.replace("git@github.com:", "https://github.com/")
        remote_url = remote_url.rstrip("/").removesuffix(".git")
        info["url"] = remote_url

        if "github.com" in remote_url:
            parts = remote_url.split("/")
            if len(parts) >= 2:
                owner = parts[-2]
                repo = parts[-1]
                info["github_pages_url"] = f"https://{owner}.github.io/{repo}/"

        return info

    def load_module_tree(self, docs_dir: Path) -> dict[str, Any]:
        module_tree_path = Path(docs_dir) / "module_tree.json"
        if not module_tree_path.exists():
            return {
                "Overview": {
                    "description": "Repository overview",
                    "components": [],
                    "children": {},
                }
            }
        try:
            return json.loads(module_tree_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise HTMLGenerationError(f"Failed to load module tree: {exc}") from exc

    def load_metadata(self, docs_dir: Path) -> dict[str, Any] | None:
        metadata_path = Path(docs_dir) / "metadata.json"
        if not metadata_path.exists():
            return None
        try:
            return json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _load_markdown_documents(self, docs_dir: Path) -> dict[str, str]:
        docs: dict[str, str] = {}
        for path in sorted(Path(docs_dir).glob("*.md")):
            try:
                docs[path.name] = path.read_text(encoding="utf-8")
            except Exception:
                continue
        return docs

    def _build_info_content(self, metadata: dict[str, Any] | None) -> str:
        if not metadata or not metadata.get("generation_info"):
            return ""

        info = metadata.get("generation_info", {})
        stats = metadata.get("statistics", {})
        parts: list[str] = []

        if info.get("main_model"):
            parts.append(
                f'<div class="info-row"><strong>Model:</strong> '
                f'{self._escape_html(str(info["main_model"]))}</div>'
            )

        if info.get("timestamp"):
            formatted = self._format_date(str(info["timestamp"]))
            if formatted:
                parts.append(
                    f'<div class="info-row"><strong>Generated:</strong> {formatted}</div>'
                )

        if info.get("commit_id"):
            parts.append(
                f'<div class="info-row"><strong>Commit:</strong> '
                f'{self._escape_html(str(info["commit_id"])[:8])}</div>'
            )

        if stats.get("total_components"):
            parts.append(
                f'<div class="info-row"><strong>Components:</strong> '
                f'{int(stats["total_components"]):,}</div>'
            )

        if stats.get("max_depth"):
            parts.append(
                f'<div class="info-row"><strong>Max Depth:</strong> '
                f'{self._escape_html(str(stats["max_depth"]))}</div>'
            )

        return "\n                ".join(parts)

    @staticmethod
    def _format_date(value: str) -> str:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except Exception:
            return ""

    @staticmethod
    def _escape_html(text: str) -> str:
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;")
        )
