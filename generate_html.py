"""Generate the CodeWiki static HTML viewer for an existing docs directory."""

from __future__ import annotations

import argparse
from pathlib import Path

from codewiki.html_generator import HTMLGenerator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate index.html from an existing CodeWiki task docs directory.",
    )
    parser.add_argument(
        "--docs-dir",
        default="docs",
        help="Documentation directory containing markdown files and module_tree.json.",
    )
    parser.add_argument(
        "--output",
        help="Output HTML path. Defaults to <docs-dir>/index.html.",
    )
    parser.add_argument(
        "--title",
        help="Viewer title. Defaults to '<docs-dir name> Documentation'.",
    )
    parser.add_argument(
        "--repo",
        help="Optional repository path used to detect a repository URL.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    docs_dir = Path(args.docs_dir).expanduser().resolve()
    if not docs_dir.exists() or not docs_dir.is_dir():
        raise SystemExit(f"Documentation directory not found: {docs_dir}")

    markdown_files = sorted(docs_dir.glob("*.md"))
    if not markdown_files:
        raise SystemExit(f"No markdown files found in: {docs_dir}")

    output_path = Path(args.output).expanduser().resolve() if args.output else docs_dir / "index.html"
    title = args.title or f"{docs_dir.name} Documentation"

    generator = HTMLGenerator()
    repo_info = {"url": None, "github_pages_url": None}
    if args.repo:
        repo_info = generator.detect_repository_info(Path(args.repo).expanduser().resolve())

    generator.generate(
        output_path=output_path,
        title=title,
        docs_dir=docs_dir,
        repository_url=repo_info.get("url"),
        github_pages_url=repo_info.get("github_pages_url"),
    )

    print(f"HTML viewer generated: {output_path}")
    print(f"Embedded markdown files: {len(markdown_files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
