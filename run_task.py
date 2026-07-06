"""Run the CodeWiki task workflow from a plain Python script."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from codewiki.src.be.backend import PendingTask
from codewiki.src.be.cluster_modules import cluster_modules
from codewiki.src.be.documentation_generator import DocumentationGenerator
from codewiki.src.be.prompt_template import FILTER_FOLDERS_PROMPT
from codewiki.src.config import (
    CLUSTER_MODEL,
    DEFAULT_MAX_TOKEN_PER_LEAF_MODULE,
    DEFAULT_MAX_TOKEN_PER_MODULE,
    DEFAULT_MAX_TOKENS,
    FIRST_MODULE_TREE_FILENAME,
    MAIN_MODEL,
    MAX_DEPTH,
    MODULE_TREE_FILENAME,
    Config,
    set_cli_context,
)
from codewiki.src.utils import file_manager
from codewiki.html_generator import HTMLGenerator


LANGUAGE_EXTENSIONS = {
    "Python": [".py"],
    "Java": [".java"],
    "JavaScript": [".js", ".jsx"],
    "TypeScript": [".ts", ".tsx"],
    "C": [".c", ".h"],
    "C++": [".cpp", ".hpp", ".cc", ".hh", ".cxx", ".hxx"],
    "C#": [".cs"],
    "PHP": [".php", ".phtml", ".inc"],
    "Kotlin": [".kt", ".kts"],
}

EXCLUDED_DIRS = {
    ".eggs",
    ".env",
    ".git",
    ".gradle",
    ".idea",
    ".mvn",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".venv",
    ".vscode",
    "__pycache__",
    "bin",
    "bower_components",
    "build",
    "coverage",
    "dist",
    "env",
    "htmlcov",
    "node_modules",
    "obj",
    "target",
    "venv",
    "vendor",
}


class TaskWorkflowError(RuntimeError):
    """Raised for script-level validation or generation errors."""


@dataclass
class JobStatistics:
    total_files_analyzed: int = 0
    leaf_nodes: int = 0
    total_tokens_used: int = 0


@dataclass
class DocumentationJob:
    job_id: str = field(default_factory=lambda: str(uuid4()))
    repository_path: str = ""
    repository_name: str = ""
    output_directory: str = ""
    timestamp_start: str = field(default_factory=lambda: datetime.now().isoformat())
    timestamp_end: str | None = None
    status: str = "pending"
    error_message: str | None = None
    files_generated: list[str] = field(default_factory=list)
    module_count: int = 0
    statistics: JobStatistics = field(default_factory=JobStatistics)

    def start(self) -> None:
        self.status = "running"
        self.timestamp_start = datetime.now().isoformat()

    def complete(self) -> None:
        self.status = "completed"
        self.timestamp_end = datetime.now().isoformat()

    def fail(self, error_message: str) -> None:
        self.status = "failed"
        self.error_message = error_message
        self.timestamp_end = datetime.now().isoformat()

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def parse_patterns(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_scan_folders(scan_values: list[str]) -> list[str]:
    folders: list[str] = []
    for value in scan_values:
        for folder in parse_patterns(value):
            normalized = folder.strip().strip("/\\")
            if normalized and normalized not in folders:
                folders.append(normalized)
    return folders


def build_scan_include_patterns(
    scan_folders: list[str],
    include_patterns: list[str] | None,
) -> list[str]:
    patterns: list[str] = []
    base_patterns = include_patterns or ["**"]
    for folder in scan_folders:
        normalized_folder = folder.replace("\\", "/").strip("/")
        for pattern in base_patterns:
            normalized_pattern = pattern.replace("\\", "/").lstrip("/")
            if normalized_pattern in ("", "**"):
                candidates = [f"{normalized_folder}/**"]
            elif "/" in normalized_pattern:
                candidates = [f"{normalized_folder}/{normalized_pattern}"]
            else:
                candidates = [
                    f"{normalized_folder}/{normalized_pattern}",
                    f"{normalized_folder}/**/{normalized_pattern}",
                ]
            for candidate in candidates:
                if candidate not in patterns:
                    patterns.append(candidate)
    return patterns


def detect_supported_languages(directory: Path) -> list[tuple[str, int]]:
    language_counts: dict[str, int] = {}
    for language, extensions in LANGUAGE_EXTENSIONS.items():
        count = 0
        for extension in extensions:
            count += sum(
                1
                for path in directory.rglob(f"*{extension}")
                if path.is_file() and not any(part in EXCLUDED_DIRS for part in path.parts)
            )
        if count:
            language_counts[language] = count
    return sorted(language_counts.items(), key=lambda item: item[1], reverse=True)


def resolve_scan_folders(repo_path: Path, scan_folders: list[str]) -> list[Path]:
    if not scan_folders:
        return [repo_path]

    resolved: list[Path] = []
    for raw_folder in scan_folders:
        folder = Path(raw_folder).expanduser()
        candidate = folder.resolve() if folder.is_absolute() else (repo_path / folder).resolve()
        try:
            candidate.relative_to(repo_path)
        except ValueError as exc:
            raise TaskWorkflowError(f"Scan folder is outside the repository root: {raw_folder}") from exc
        if not candidate.exists():
            raise TaskWorkflowError(f"Scan folder does not exist: {raw_folder}")
        if not candidate.is_dir():
            raise TaskWorkflowError(f"Scan path is not a directory: {raw_folder}")
        if candidate not in resolved:
            resolved.append(candidate)
    return resolved


def validate_repository(repo_path: Path, scan_folders: list[str]) -> tuple[Path, list[tuple[str, int]]]:
    repo_path = repo_path.expanduser().resolve()
    if not repo_path.exists():
        raise TaskWorkflowError(f"Repository path does not exist: {repo_path}")
    if not repo_path.is_dir():
        raise TaskWorkflowError(f"Repository path is not a directory: {repo_path}")

    language_counts: dict[str, int] = {}
    for scan_path in resolve_scan_folders(repo_path, scan_folders):
        for language, count in detect_supported_languages(scan_path):
            language_counts[language] = language_counts.get(language, 0) + count
    languages = sorted(language_counts.items(), key=lambda item: item[1], reverse=True)
    if not languages:
        raise TaskWorkflowError(
            "No supported code files found. Supported languages: "
            + ", ".join(LANGUAGE_EXTENSIONS)
        )
    return repo_path, languages


def list_paths_two_depth(repo_path: Path) -> str:
    entries: list[str] = []
    base = repo_path.resolve()
    for path in sorted(base.rglob("*")):
        if any(part in EXCLUDED_DIRS for part in path.relative_to(base).parts):
            continue
        relative = path.relative_to(base)
        if len(relative.parts) > 2:
            continue
        marker = "/" if path.is_dir() else ""
        entries.append(str(relative).replace("\\", "/") + marker)
    return "\n".join(entries)


def extract_json_list(text: str) -> list[str]:
    try:
        parsed = json.loads(text.strip())
    except json.JSONDecodeError:
        import re

        match = re.search(r"\[[\s\S]*\]", text)
        if not match:
            raise TaskWorkflowError("Folder filtering result must contain a JSON array")
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise TaskWorkflowError("Folder filtering result must be a JSON array of strings")
    return [item.strip().strip("/\\") for item in parsed if item.strip()]


def paths_to_include_patterns(repo_path: Path, paths: list[str]) -> list[str]:
    patterns: list[str] = []
    repo_path = repo_path.resolve()
    for raw_path in paths:
        normalized = raw_path.replace("\\", "/").strip("/")
        if not normalized:
            continue
        candidate = (repo_path / normalized).resolve()
        try:
            candidate.relative_to(repo_path)
        except ValueError as exc:
            raise TaskWorkflowError(f"Filtered path is outside repository root: {raw_path}") from exc
        if not candidate.exists():
            continue
        if candidate.is_dir():
            pattern = f"{normalized}/**"
        else:
            pattern = normalized
        if pattern not in patterns:
            patterns.append(pattern)
    return patterns


def check_writable_output(output_dir: Path) -> None:
    output_dir = output_dir.expanduser().resolve()
    target = output_dir if output_dir.exists() else output_dir.parent
    if not target.exists():
        raise TaskWorkflowError(f"Parent output directory does not exist: {target}")
    if not target.is_dir():
        raise TaskWorkflowError(f"Output target is not a directory: {target}")
    if not os.access(target, os.W_OK):
        raise TaskWorkflowError(f"Output directory is not writable: {target}")


def pending_manifest_path(output_dir: Path) -> Path:
    return output_dir / ".codewiki" / "tasks" / "pending_tasks.json"


def read_pending_manifest(output_dir: Path) -> dict[str, Any]:
    manifest_path = pending_manifest_path(output_dir)
    if not manifest_path.exists():
        return {
            "status": "error",
            "message": "pending_tasks.json was not found",
            "pending_manifest_path": str(manifest_path),
        }
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {
            "status": "error",
            "message": f"pending_tasks.json is invalid JSON: {exc}",
            "pending_manifest_path": str(manifest_path),
        }
    payload.setdefault("pending_manifest_path", str(manifest_path))
    return payload


def write_status(output_dir: Path, status: str, **extra: Any) -> dict[str, Any]:
    task_dir = output_dir / ".codewiki" / "tasks"
    task_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = task_dir / "pending_tasks.json"
    payload = {
        "status": status,
        "next_action": "stop" if status == "complete" else "inspect_status",
        "output_dir": str(output_dir),
        "pending_manifest_path": str(manifest_path),
        **extra,
    }
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


class ScriptDocumentationGenerator:
    def __init__(
        self,
        repo_path: Path,
        output_dir: Path,
        config: dict[str, Any],
        verbose: bool,
    ) -> None:
        self.repo_path = repo_path
        self.output_dir = output_dir
        self.config = config
        self.verbose = verbose
        self.job = DocumentationJob(
            repository_path=str(repo_path),
            repository_name=repo_path.name,
            output_directory=str(output_dir),
        )
        self._configure_backend_logging()

    def _configure_backend_logging(self) -> None:
        level = logging.INFO if self.verbose else logging.WARNING
        root_logger = logging.getLogger()
        root_logger.setLevel(level)

        for logger_name in list(logging.Logger.manager.loggerDict):
            if logger_name == "codewiki" or logger_name.startswith("codewiki."):
                logger = logging.getLogger(logger_name)
                logger.handlers.clear()
                logger.setLevel(level)
                logger.propagate = True

        backend_logger = logging.getLogger("codewiki.src.be")
        backend_logger.handlers.clear()
        handler = logging.StreamHandler(sys.stdout if self.verbose else sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        backend_logger.addHandler(handler)
        backend_logger.setLevel(level)
        backend_logger.propagate = False

    def generate(self) -> DocumentationJob:
        self.job.start()
        try:
            set_cli_context(True)
            backend_config = Config.from_cli(
                repo_path=str(self.repo_path),
                output_dir=str(self.output_dir),
                main_model=self.config.get("main_model", MAIN_MODEL),
                cluster_model=self.config.get("cluster_model", CLUSTER_MODEL),
                max_tokens=self.config.get("max_tokens", DEFAULT_MAX_TOKENS),
                max_token_per_module=self.config.get(
                    "max_token_per_module",
                    DEFAULT_MAX_TOKEN_PER_MODULE,
                ),
                max_token_per_leaf_module=self.config.get(
                    "max_token_per_leaf_module",
                    DEFAULT_MAX_TOKEN_PER_LEAF_MODULE,
                ),
                max_depth=self.config.get("max_depth", MAX_DEPTH),
                agent_instructions=self.config.get("agent_instructions"),
            )
            asyncio.run(self._run_backend_generation(backend_config))
            self._finalize_job()
            self.job.complete()
            return self.job
        except Exception as exc:
            self.job.fail(str(exc))
            raise

    async def _run_backend_generation(self, backend_config: Config) -> None:
        if self.verbose:
            print("[1/4] Folder filtering")
        doc_generator = DocumentationGenerator(backend_config)
        self._apply_filter_prompt(doc_generator, backend_config)

        if self.verbose:
            print("[2/4] Dependency analysis")
        try:
            components, leaf_nodes = doc_generator.graph_builder.build_dependency_graph()
        except Exception as exc:
            raise TaskWorkflowError(f"Dependency analysis failed: {exc}") from exc
        self.job.statistics.total_files_analyzed = len(components)
        self.job.statistics.leaf_nodes = len(leaf_nodes)

        if self.verbose:
            print(f"[3/4] Module clustering ({len(leaf_nodes)} leaf nodes)")
        working_dir = str(self.output_dir.absolute())
        file_manager.ensure_directory(working_dir)
        first_module_tree_path = os.path.join(working_dir, FIRST_MODULE_TREE_FILENAME)
        module_tree_path = os.path.join(working_dir, MODULE_TREE_FILENAME)
        try:
            if os.path.exists(first_module_tree_path):
                module_tree = file_manager.load_json(first_module_tree_path)
            else:
                module_tree = cluster_modules(
                    leaf_nodes,
                    components,
                    backend_config,
                    completer=lambda prompt: doc_generator.backend.complete(
                        prompt,
                        model=backend_config.cluster_model or None,
                    ),
                )
                file_manager.save_json(module_tree, first_module_tree_path)
            file_manager.save_json(module_tree, module_tree_path)
            self.job.module_count = len(module_tree)
        except PendingTask:
            raise
        except Exception as exc:
            raise TaskWorkflowError(f"Module clustering failed: {exc}") from exc

        if self.verbose:
            print(f"[4/4] Documentation generation ({self.job.module_count} modules)")
        try:
            await doc_generator.generate_module_documentation(components, leaf_nodes)
            doc_generator.create_documentation_metadata(working_dir, components, len(leaf_nodes))
            self.job.files_generated = [
                path.name
                for path in self.output_dir.iterdir()
                if path.suffix in {".md", ".json"}
            ]
        except PendingTask:
            raise
        except Exception as exc:
            raise TaskWorkflowError(f"Documentation generation failed: {exc}") from exc

    def _apply_filter_prompt(
        self,
        doc_generator: DocumentationGenerator,
        backend_config: Config,
    ) -> None:
        if backend_config.include_patterns:
            return

        prompt = FILTER_FOLDERS_PROMPT.format(
            project_name=self.repo_path.name,
            files=list_paths_two_depth(self.repo_path),
        )
        response = doc_generator.backend.complete(prompt)
        filtered_paths = extract_json_list(response)
        include_patterns = paths_to_include_patterns(self.repo_path, filtered_paths)
        if not include_patterns:
            return

        agent_instructions = dict(backend_config.agent_instructions or {})
        agent_instructions["include_patterns"] = include_patterns
        backend_config.agent_instructions = agent_instructions

    def _finalize_job(self) -> None:
        metadata_path = self.output_dir / "metadata.json"
        if not metadata_path.exists():
            metadata_path.write_text(self.job.to_json(), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate CodeWiki task files for a local repository.",
    )
    parser.add_argument("--repo", default=".", help="Repository root to scan.")
    parser.add_argument("--output", "-o", default="docs", help="Output documentation directory.")
    parser.add_argument("--include", "-i", help="Comma-separated include globs.")
    parser.add_argument(
        "--scan",
        action="append",
        default=[],
        help="Repository-root-relative folder to scan. Repeat or comma-separate.",
    )
    parser.add_argument("--exclude", "-e", help="Comma-separated exclude globs.")
    parser.add_argument("--focus", "-f", help="Comma-separated modules or paths to focus.")
    parser.add_argument(
        "--doc-type",
        choices=["api", "architecture", "user-guide", "developer"],
        help="Documentation style to request in generated tasks.",
    )
    parser.add_argument("--instructions", help="Additional documentation instructions.")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--max-token-per-module", type=int, default=DEFAULT_MAX_TOKEN_PER_MODULE)
    parser.add_argument(
        "--max-token-per-leaf-module",
        type=int,
        default=DEFAULT_MAX_TOKEN_PER_LEAF_MODULE,
    )
    parser.add_argument("--max-depth", type=int, default=MAX_DEPTH)
    parser.add_argument("--agent-json", action="store_true", help="Print machine-readable status JSON.")
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser


def print_completion(output_dir: Path, job: DocumentationJob, elapsed: float) -> None:
    print("OK Documentation generation complete.")
    print(f"Output directory: {output_dir}")
    print(f"HTML viewer: {output_dir / 'index.html'}")
    print(f"Files analyzed: {job.statistics.total_files_analyzed}")
    print(f"Modules: {job.module_count}")
    print(f"Elapsed seconds: {elapsed:.1f}")
    print(f"Pending manifest: {pending_manifest_path(output_dir)}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    start_time = time.time()
    repo_path = Path(args.repo)
    output_dir = Path(args.output).expanduser().resolve()
    os.environ["CODEWIKI_RERUN_COMMAND"] = "python " + " ".join(
        [str(Path(__file__).name), *sys.argv[1:]]
    )

    try:
        scan_folders = parse_scan_folders(args.scan)
        repo_path, languages = validate_repository(repo_path, scan_folders)
        check_writable_output(output_dir)

        include_patterns = parse_patterns(args.include) or None
        if scan_folders:
            include_patterns = build_scan_include_patterns(scan_folders, include_patterns)

        agent_instructions = {
            "include_patterns": include_patterns,
            "exclude_patterns": parse_patterns(args.exclude) or None,
            "focus_modules": parse_patterns(args.focus) or None,
            "doc_type": args.doc_type,
            "custom_instructions": args.instructions,
        }
        agent_instructions = {
            key: value for key, value in agent_instructions.items() if value
        } or None

        if args.verbose:
            print(f"Repository: {repo_path}")
            print("Languages: " + ", ".join(f"{name} ({count})" for name, count in languages))
            if scan_folders:
                print("Scan folders: " + ", ".join(scan_folders))
            if include_patterns:
                print("Include patterns: " + ", ".join(include_patterns))

        generator = ScriptDocumentationGenerator(
            repo_path=repo_path,
            output_dir=output_dir,
            config={
                "main_model": MAIN_MODEL,
                "cluster_model": CLUSTER_MODEL,
                "agent_instructions": agent_instructions,
                "max_tokens": args.max_tokens,
                "max_token_per_module": args.max_token_per_module,
                "max_token_per_leaf_module": args.max_token_per_leaf_module,
                "max_depth": args.max_depth,
            },
            verbose=args.verbose,
        )
        job = generator.generate()
        elapsed = time.time() - start_time
        files_generated = list(job.files_generated)
        if "index.html" not in files_generated:
            files_generated.append("index.html")

        repo_info = HTMLGenerator().detect_repository_info(repo_path)
        HTMLGenerator().generate(
            output_path=output_dir / "index.html",
            title=f"{repo_info.get('name') or repo_path.name} Documentation",
            docs_dir=output_dir,
            repository_url=repo_info.get("url"),
            github_pages_url=repo_info.get("github_pages_url"),
        )
        status = write_status(
            output_dir,
            "complete",
            files_generated=files_generated,
            module_count=job.module_count,
            total_files_analyzed=job.statistics.total_files_analyzed,
            generation_time_seconds=elapsed,
        )
        if args.agent_json:
            print(json.dumps(status, indent=2))
        else:
            print_completion(output_dir, job, elapsed)
        return 0
    except PendingTask as exc:
        if args.agent_json:
            print(json.dumps(read_pending_manifest(output_dir), indent=2))
        else:
            print(str(exc), file=sys.stderr)
            print(f"Pending manifest: {pending_manifest_path(output_dir)}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("Interrupted by user.", file=sys.stderr)
        return 130
    except Exception as exc:
        if args.agent_json:
            print(
                json.dumps(
                    {
                        "status": "error",
                        "message": str(exc),
                        "output_dir": str(output_dir),
                        "pending_manifest_path": str(pending_manifest_path(output_dir)),
                    },
                    indent=2,
                )
            )
        else:
            print(f"ERROR {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
