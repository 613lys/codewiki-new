"""Task-file backend.

Whenever the pipeline needs generated text, it writes a deterministic task file
under the docs output directory and stops. Existing result files are consumed
automatically on the next run, so the pipeline can move to the next stage.
"""

from __future__ import annotations

import hashlib
import ast
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List

from codewiki.src.be.backend import PendingTask, TaskBackendBase
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.prompt_template import (
    SUBMODULE_PLANNING_PROMPT,
    USER_PROMPT,
    format_leaf_system_prompt,
    format_system_prompt,
)
from codewiki.src.be.utils import is_complex_module, validate_mermaid_diagrams
from codewiki.src.config import MODULE_TREE_FILENAME, OVERVIEW_FILENAME, Config
from codewiki.src.utils import file_manager


class TaskBackend(TaskBackendBase):
    """Backend that exchanges prompts and results via local task files."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._bridge_dir = Path(config.docs_dir) / ".codewiki" / "tasks"
        self._tasks_dir = self._bridge_dir / "tasks"
        self._results_dir = self._bridge_dir / "results"
        self.pending_tasks: list[dict[str, str]] = []

    def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        temperature: float = 0.0,
    ) -> str:
        task_id = self._task_id("completion", prompt, model or "")
        result_path = self._results_dir / f"{task_id}.txt"
        if result_path.exists():
            response = result_path.read_text(encoding="utf-8")
            try:
                self._validate_completion_result(prompt, response)
            except ValueError as error:
                repair_task_path = self._tasks_dir / f"{task_id}_repair.md"
                repair_metadata = {
                    "task_id": f"{task_id}_repair",
                    "kind": "completion_repair",
                    "model": model or self._config.main_model,
                    "temperature": temperature,
                    "result_path": str(result_path),
                    "invalid_result_path": str(result_path),
                    "validation_error": str(error),
                }
                self._write_task(
                    repair_task_path,
                    self._format_completion_repair_task(
                        result_path=result_path,
                        validation_error=str(error),
                        prompt=prompt,
                    ),
                    repair_metadata,
                    overwrite=True,
                )
                self._add_pending(
                    "completion_repair",
                    repair_task_path,
                    result_path,
                    extra={
                        "validation_error": str(error),
                        "invalid_result_path": str(result_path),
                    },
                )
                self.raise_if_pending()
            return response

        task_path = self._tasks_dir / f"{task_id}.md"
        task_kind = self._completion_task_kind(prompt)
        metadata = {
            "task_id": task_id,
            "kind": task_kind,
            "model": model or self._config.main_model,
            "temperature": temperature,
            "result_path": str(result_path),
        }
        instructions = (
            "Complete this CodeWiki task.\n\n"
            f"Write the exact model response to:\n\n`{result_path}`\n\n"
            "Keep any requested XML-style wrapper tags from the prompt. For "
            "clustering tasks, the response must include the "
            "`<GROUPED_COMPONENTS>...</GROUPED_COMPONENTS>` block."
        )
        self._write_task(task_path, instructions, metadata, prompt)
        self._add_pending(task_kind, task_path, result_path)
        self.raise_if_pending()

    async def run_module_agent(
        self,
        module_name: str,
        components: Dict[str, Node],
        core_component_ids: List[str],
        module_path: List[str],
        working_dir: str,
    ) -> Dict[str, Any]:
        module_tree_path = os.path.join(working_dir, MODULE_TREE_FILENAME)
        module_tree = file_manager.load_json(module_tree_path)

        overview_docs_path = os.path.join(working_dir, OVERVIEW_FILENAME)
        if os.path.exists(overview_docs_path):
            return module_tree

        docs_path = os.path.join(working_dir, f"{module_name}.md")
        if os.path.exists(docs_path):
            return module_tree

        # Keep the task id stable across reruns. The module tree can change as
        # earlier task results are applied, so it must not be part of the id.
        task_fingerprint = json.dumps(
            {
                "module_name": module_name,
                "module_path": module_path,
                "core_component_ids": sorted(core_component_ids),
            },
            sort_keys=True,
        )
        task_id = self._task_id("module", task_fingerprint)
        result_path = self._results_dir / f"{task_id}.md"
        task_body = self._format_module_task(
            module_name=module_name,
            components=components,
            core_component_ids=core_component_ids,
            module_path=module_path,
            working_dir=working_dir,
            module_tree=module_tree,
            result_path=result_path,
            docs_path=Path(docs_path),
        )

        if result_path.exists():
            content = result_path.read_text(encoding="utf-8").strip()
            validation_errors = self._validate_module_markdown_content(content)
            validation_result = await validate_mermaid_diagrams(
                str(result_path),
                result_path.name,
            )
            if self._is_mermaid_validation_error(validation_result):
                validation_errors.append(validation_result)
            if validation_errors:
                repair_task_path = self._tasks_dir / f"{task_id}_repair.md"
                repair_metadata = {
                    "task_id": f"{task_id}_repair",
                    "kind": "module_documentation_repair",
                    "module_name": module_name,
                    "module_path": module_path,
                    "result_path": str(result_path),
                    "docs_path": docs_path,
                    "validation_error": "\n\n".join(validation_errors),
                }
                self._write_task(
                    repair_task_path,
                    self._format_module_repair_task(
                        module_name=module_name,
                        result_path=result_path,
                        validation_result="\n\n".join(validation_errors),
                    ),
                    repair_metadata,
                    overwrite=True,
                )
                self._add_pending(
                    "module_documentation_repair",
                    repair_task_path,
                    result_path,
                    extra={"validation_error": "\n\n".join(validation_errors)},
                )
                self.raise_if_pending()
            file_manager.save_text(content + "\n", docs_path)
            file_manager.save_json(module_tree, module_tree_path)
            return module_tree

        task_path = self._tasks_dir / f"{task_id}.md"
        metadata = {
            "task_id": task_id,
            "kind": "module_documentation",
            "module_name": module_name,
            "module_path": module_path,
            "core_component_ids": core_component_ids,
            "result_path": str(result_path),
            "docs_path": docs_path,
        }
        self._write_task(task_path, task_body, metadata)
        self._add_pending("module_documentation", task_path, result_path)
        return module_tree

    async def plan_submodules(
        self,
        module_name: str,
        components: Dict[str, Node],
        core_component_ids: List[str],
        module_path: List[str],
        working_dir: str,
        module_tree: Dict[str, Any],
    ) -> Dict[str, Any] | None:
        core_component_references = self._format_component_references(
            components,
            core_component_ids,
        )
        prompt = SUBMODULE_PLANNING_PROMPT.format(
            module_name=module_name,
            module_tree=json.dumps(module_tree, indent=2),
            core_component_references=core_component_references,
        )
        task_id = self._task_id(
            "submodule_planning",
            module_name,
            "/".join(module_path),
            prompt,
        )
        result_path = self._results_dir / f"{task_id}.txt"
        if result_path.exists():
            try:
                return self._validate_submodule_result(
                    result_path.read_text(encoding="utf-8"),
                    core_component_ids,
                )
            except Exception as error:
                repair_task_path = self._tasks_dir / f"{task_id}_repair.md"
                repair_metadata = {
                    "task_id": f"{task_id}_repair",
                    "kind": "submodule_planning_repair",
                    "module_name": module_name,
                    "module_path": module_path,
                    "core_component_ids": core_component_ids,
                    "result_path": str(result_path),
                    "invalid_result_path": str(result_path),
                    "parse_error": str(error),
                }
                repair_instructions = self._format_submodule_repair_task(
                    module_name=module_name,
                    result_path=result_path,
                    parse_error=str(error),
                    prompt=prompt,
                )
                self._write_task(
                    repair_task_path,
                    repair_instructions,
                    repair_metadata,
                    overwrite=True,
                )
                self._add_pending(
                    "submodule_planning_repair",
                    repair_task_path,
                    result_path,
                    extra={
                        "parse_error": str(error),
                        "invalid_result_path": str(result_path),
                    },
                )
                self.raise_if_pending()
                return None

        task_path = self._tasks_dir / f"{task_id}.md"
        metadata = {
            "task_id": task_id,
            "kind": "submodule_planning",
            "module_name": module_name,
            "module_path": module_path,
            "core_component_ids": core_component_ids,
            "result_path": str(result_path),
        }
        instructions = (
            "Complete this CodeWiki planning task.\n\n"
            f"Write the exact response to:\n\n`{result_path}`\n\n"
            "The response must include exactly one "
            "`<SUB_MODULES>...</SUB_MODULES>` block."
        )
        self._write_task(task_path, instructions, metadata, prompt)
        self._add_pending("submodule_planning", task_path, result_path)
        self.raise_if_pending()
        return None

    @staticmethod
    def _format_submodule_repair_task(
        *,
        module_name: str,
        result_path: Path,
        parse_error: str,
        prompt: str,
    ) -> str:
        return (
            "# CodeWiki Task\n\n"
            "Repair the existing submodule planning result.\n\n"
            "## Parse Error\n\n"
            f"`{parse_error}`\n\n"
            "## Required Action\n\n"
            f"- Overwrite `{result_path}` with a valid response.\n"
            "- The response must contain exactly one `<SUB_MODULES>...</SUB_MODULES>` block.\n"
            "- The content inside `<SUB_MODULES>` must be a dictionary/object, not an array/list.\n"
            "- Each top-level key must be a submodule name.\n"
            "- Each value must contain `path` and `components`.\n"
            "- Every component id must be copied exactly from the original task.\n"
            "- Do not invent component ids and do not use the same component id twice.\n"
            "- If no useful split exists, write an empty object.\n\n"
            "## Valid Empty Response\n\n"
            "```text\n"
            "<SUB_MODULES>\n"
            "{}\n"
            "</SUB_MODULES>\n"
            "```\n\n"
            "## Valid Split Response Shape\n\n"
            "```text\n"
            "<SUB_MODULES>\n"
            "{\n"
            f"  \"{module_name} child module\": {{\n"
            "    \"path\": \"path/to/module\",\n"
            "    \"components\": [\n"
            "      \"exact/component/id::Name\"\n"
            "    ]\n"
            "  }\n"
            "}\n"
            "</SUB_MODULES>\n"
            "```\n\n"
            "## Original Planning Prompt\n\n"
            f"{prompt}\n"
        )

    @staticmethod
    def _format_completion_repair_task(
        *,
        result_path: Path,
        validation_error: str,
        prompt: str,
    ) -> str:
        return (
            "# CodeWiki Task\n\n"
            "Repair the existing task result so it matches the format required by the next pipeline step.\n\n"
            "## Validation Error\n\n"
            "```text\n"
            f"{validation_error}\n"
            "```\n\n"
            "## Required Action\n\n"
            f"- Overwrite `{result_path}` with a valid response.\n"
            "- Preserve the wrapper tags requested by the original prompt.\n"
            "- Do not add chat prefaces, explanations about this repair, or a code fence around the whole response.\n\n"
            "## Original Prompt\n\n"
            f"{prompt}\n"
        )

    def _format_module_task(
        self,
        *,
        module_name: str,
        components: Dict[str, Node],
        core_component_ids: List[str],
        module_path: List[str],
        working_dir: str,
        module_tree: Dict[str, Any],
        result_path: Path,
        docs_path: Path,
    ) -> str:
        """Create an AI-IDE task that references source files to read."""
        repo_root = Path(self._config.repo_path).resolve()

        source_refs, missing_components = self._format_component_references_with_missing(
            components,
            core_component_ids,
            repo_root,
        )

        missing_section = ""
        if missing_components:
            missing_section = (
                "\n## Components Not Found In Analysis Map\n\n"
                + "\n".join(f"- `{component_id}`" for component_id in missing_components)
                + "\n"
            )

        system_prompt = self._format_original_system_prompt(
            module_name=module_name,
            components=components,
            core_component_ids=core_component_ids,
        )
        user_prompt = USER_PROMPT.format(
            module_name=module_name,
            module_tree=self._format_module_tree_for_prompt(module_tree, module_name),
            formatted_core_component_codes=(
                "This task is running in local task mode, so source code is not copied here.\n"
                "Read the following files directly from the repository workspace.\n\n"
                f"{source_refs}"
            ),
        )

        return (
            "# CodeWiki Task\n\n"
            "Complete the documentation task below.\n\n"
            "## Workspace Reading Contract\n\n"
            f"- Repository root: `{repo_root}`.\n"
            "- Treat every file listed in `<CORE_COMPONENT_CODES>` as required reading.\n"
            "- Open those files directly from the repository workspace before writing the result.\n"
            "- Use the listed component IDs to focus your reading, but inspect surrounding code, imports, and callers when needed.\n"
            "- This task intentionally contains file references instead of copied source code.\n\n"
            "## Output Contract\n\n"
            f"- Write only the final markdown document to `{result_path}`.\n"
            "- Do not include chat prefaces, explanations about this task, or a code fence around the whole file.\n"
            f"- The final document will be copied to `{docs_path}` on the next run.\n\n"
            "## System Prompt\n\n"
            f"{system_prompt}\n"
            "\n## User Prompt\n\n"
            f"{user_prompt}\n"
            + missing_section
        )

    def _format_original_system_prompt(
        self,
        *,
        module_name: str,
        components: Dict[str, Node],
        core_component_ids: List[str],
    ) -> str:
        custom_instructions = self._config.get_prompt_addition()
        if is_complex_module(components, core_component_ids):
            return format_system_prompt(module_name, custom_instructions)
        return format_leaf_system_prompt(module_name, custom_instructions)

    @staticmethod
    def _format_module_repair_task(
        *,
        module_name: str,
        result_path: Path,
        validation_result: str,
    ) -> str:
        return (
            "# CodeWiki Task\n\n"
            f"Repair the existing markdown result for `{module_name}`.\n\n"
            "## Mermaid Validation Error\n\n"
            "```text\n"
            f"{validation_result}\n"
            "```\n\n"
            "## Required Action\n\n"
            f"- Edit `{result_path}` in place.\n"
            "- Fix every validation error reported above.\n"
            "- Keep the document as final markdown only.\n"
            "- Do not wrap the whole document in a code fence or add chat prefaces.\n"
        )

    def _format_component_references(
        self,
        components: Dict[str, Node],
        core_component_ids: List[str],
    ) -> str:
        refs, _ = self._format_component_references_with_missing(
            components,
            core_component_ids,
            Path(self._config.repo_path).resolve(),
        )
        return refs

    def _format_component_references_with_missing(
        self,
        components: Dict[str, Node],
        core_component_ids: List[str],
        repo_root: Path,
    ) -> tuple[str, list[str]]:
        source_files: dict[str, list[str]] = {}
        missing_components: list[str] = []
        for component_id in core_component_ids:
            component = components.get(component_id)
            if component is None:
                missing_components.append(component_id)
                continue
            relative_path = self._component_relative_path(component, repo_root)
            source_files.setdefault(relative_path, []).append(component_id)

        source_lines: list[str] = []
        for index, (relative_path, ids) in enumerate(sorted(source_files.items()), 1):
            source_lines.append(f"{index}. `{relative_path}`")
            for component_id in sorted(ids):
                source_lines.append(f"   - `{component_id}`")

        if not source_lines:
            return "No source files were provided by analysis.", missing_components
        return "\n".join(source_lines), missing_components

    @staticmethod
    def _parse_submodule_result(response: str) -> Dict[str, Any]:
        content = TaskBackend._extract_single_block(response, "SUB_MODULES")
        parsed = TaskBackend._parse_mapping_literal(content, "Submodule planning")
        if not isinstance(parsed, dict):
            raise ValueError("Submodule planning response must be a dictionary")
        return parsed

    @staticmethod
    def _validate_submodule_result(
        response: str,
        allowed_component_ids: List[str],
    ) -> Dict[str, Any]:
        parsed = TaskBackend._parse_submodule_result(response)
        errors = TaskBackend._validate_grouped_component_mapping(
            parsed,
            allowed_component_ids=allowed_component_ids,
            allow_empty=True,
            label="Submodule planning",
        )
        if errors:
            raise ValueError("; ".join(errors))
        return parsed

    @staticmethod
    def _validate_completion_result(prompt: str, response: str) -> None:
        if "<GROUPED_COMPONENTS>" in prompt:
            content = TaskBackend._extract_single_block(response, "GROUPED_COMPONENTS")
            parsed = TaskBackend._parse_mapping_literal(content, "Grouped components")
            errors = TaskBackend._validate_grouped_component_mapping(
                parsed,
                allowed_component_ids=None,
                allow_empty=False,
                label="Grouped components",
            )
            if errors:
                raise ValueError("; ".join(errors))
            return

        if "<OVERVIEW>" in prompt:
            content = TaskBackend._extract_single_block(response, "OVERVIEW").strip()
            errors = TaskBackend._validate_module_markdown_content(content)
            if errors:
                raise ValueError("; ".join(errors))

        if "return the list of relative paths in JSON format" in prompt:
            parsed = TaskBackend._extract_json_array(response)
            if not all(isinstance(item, str) and item.strip() for item in parsed):
                raise ValueError("Folder filtering response must be a JSON array of non-empty strings")

    @staticmethod
    def _extract_single_block(response: str, tag: str) -> str:
        start_tag = f"<{tag}>"
        end_tag = f"</{tag}>"
        if response.count(start_tag) != 1 or response.count(end_tag) != 1:
            raise ValueError(f"Response must contain exactly one {start_tag}...{end_tag} block")
        start = response.index(start_tag) + len(start_tag)
        end = response.index(end_tag)
        if end <= start:
            raise ValueError(f"{start_tag} block is empty")
        trailing = response[end + len(end_tag):].strip()
        if trailing:
            raise ValueError(f"Response has extra content after {end_tag}")
        return response[start:end].strip()

    @staticmethod
    def _parse_mapping_literal(content: str, label: str) -> Dict[str, Any]:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            try:
                parsed = ast.literal_eval(content)
            except (SyntaxError, ValueError) as error:
                raise ValueError(f"{label} block must contain a JSON/Python dictionary: {error}") from error
        if not isinstance(parsed, dict):
            raise ValueError(f"{label} block must contain a dictionary/object")
        return parsed

    @staticmethod
    def _extract_json_array(response: str) -> list[Any]:
        try:
            parsed = json.loads(response.strip())
        except json.JSONDecodeError:
            match = re.search(r"\[[\s\S]*\]", response)
            if not match:
                raise ValueError("Response must contain a JSON array")
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError as error:
                raise ValueError(f"Response must contain a valid JSON array: {error}") from error
        if not isinstance(parsed, list):
            raise ValueError("Response must contain a JSON array")
        return parsed

    @staticmethod
    def _validate_grouped_component_mapping(
        parsed: Dict[str, Any],
        *,
        allowed_component_ids: List[str] | None,
        allow_empty: bool,
        label: str,
    ) -> List[str]:
        errors: list[str] = []
        if not parsed and not allow_empty:
            errors.append(f"{label} result must contain at least one module")
            return errors

        allowed = set(allowed_component_ids) if allowed_component_ids is not None else None
        used: set[str] = set()
        for module_name, module_info in parsed.items():
            if not isinstance(module_name, str) or not module_name.strip():
                errors.append(f"{label} has an empty or non-string module name")
                continue
            if not isinstance(module_info, dict):
                errors.append(f"{label} module `{module_name}` must be an object")
                continue
            path = module_info.get("path", "")
            if path is not None and not isinstance(path, str):
                errors.append(f"{label} module `{module_name}` has non-string path")
            components = module_info.get("components")
            if not isinstance(components, list):
                errors.append(f"{label} module `{module_name}` must contain a components list")
                continue
            if not components:
                errors.append(f"{label} module `{module_name}` has an empty components list")
            for component_id in components:
                if not isinstance(component_id, str) or not component_id.strip():
                    errors.append(f"{label} module `{module_name}` has an empty or non-string component id")
                    continue
                if allowed is not None and component_id not in allowed:
                    errors.append(f"{label} module `{module_name}` uses unknown component id `{component_id}`")
                if component_id in used:
                    errors.append(f"{label} component id `{component_id}` is used more than once")
                used.add(component_id)
        return errors

    @staticmethod
    def _validate_module_markdown_content(content: str) -> List[str]:
        errors: list[str] = []
        stripped = content.strip()
        if not stripped:
            return ["Markdown result is empty"]
        if stripped.startswith("```") and stripped.endswith("```"):
            errors.append("Markdown result must not be wrapped in a single code fence")
        lower_start = stripped[:300].lower()
        preface_markers = [
            "here is",
            "sure,",
            "certainly,",
            "i have generated",
            "the final markdown",
        ]
        if any(marker in lower_start for marker in preface_markers):
            errors.append("Markdown result appears to contain a chat preface")
        if "# codewiki task" in lower_start:
            errors.append("Markdown result appears to contain the task instructions instead of the answer")
        return errors

    @staticmethod
    def _component_relative_path(component: Node, repo_root: Path) -> str:
        raw_path = Path(getattr(component, "file_path", "") or getattr(component, "relative_path", ""))
        if raw_path.is_absolute():
            try:
                return str(raw_path.resolve().relative_to(repo_root))
            except ValueError:
                return str(raw_path)
        return str(raw_path)

    @staticmethod
    def _format_module_tree_for_prompt(module_tree: dict[str, Any], module_name: str) -> str:
        lines: list[str] = []

        def _format_tree(tree: dict[str, Any], indent: int = 0) -> None:
            for key, value in tree.items():
                if key == module_name:
                    lines.append(f"{'  ' * indent}{key} (current module)")
                else:
                    lines.append(f"{'  ' * indent}{key}")

                by_file: dict[str, list[str]] = {}
                for component_id in value.get("components", []):
                    if "::" in component_id:
                        file_path, name = component_id.split("::", 1)
                        by_file.setdefault(file_path, []).append(name)
                    else:
                        by_file.setdefault("", []).append(component_id)

                for file_path, names in by_file.items():
                    if file_path:
                        lines.append(f"{'  ' * (indent + 1)} {file_path}: {', '.join(names)}")
                    else:
                        lines.append(f"{'  ' * (indent + 1)} {', '.join(names)}")

                children = value.get("children", {})
                if isinstance(children, dict) and children:
                    lines.append(f"{'  ' * (indent + 1)} Children:")
                    _format_tree(children, indent + 2)

        _format_tree(module_tree, 0)
        return "\n".join(lines)

    def _write_task(
        self,
        task_path: Path,
        instructions: str,
        metadata: Dict[str, Any],
        prompt: str | None = None,
        overwrite: bool = False,
    ) -> None:
        self._tasks_dir.mkdir(parents=True, exist_ok=True)
        self._results_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = task_path.with_suffix(".json")

        if overwrite or not task_path.exists():
            body = instructions if instructions.lstrip().startswith("#") else (
                "# CodeWiki Task\n\n"
                f"{instructions}"
            )
            task_path.write_text(
                f"{body.rstrip()}\n\n"
                + (f"\n## Prompt\n\n{prompt}\n" if prompt is not None else ""),
                encoding="utf-8",
            )
        if overwrite or not metadata_path.exists():
            metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    def _add_pending(
        self,
        kind: str,
        task_path: Path,
        result_path: Path,
        extra: Dict[str, Any] | None = None,
    ) -> None:
        if result_path.exists() and kind not in {
            "completion_repair",
            "submodule_planning_repair",
            "module_documentation_repair",
        }:
            return

        task = {
            "kind": kind,
            "task_path": str(task_path),
            "result_path": str(result_path),
            **self._task_guidance(kind),
        }
        if extra:
            task.update(extra)
        if task not in self.pending_tasks:
            self.pending_tasks.append(task)
        self._write_pending_manifest()

    def _write_pending_manifest(self) -> None:
        self._bridge_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = self._bridge_dir / "pending_tasks.json"
        rerun_command = os.environ.get("CODEWIKI_RERUN_COMMAND", "python run_task.py")
        self.pending_tasks = [
            task
            for task in self.pending_tasks
            if not Path(task["result_path"]).exists()
            or str(task.get("kind", "")).endswith("_repair")
        ]
        indexed_tasks = [
            {
                "index": index,
                "task_id": Path(task["task_path"]).stem,
                **task,
            }
            for index, task in enumerate(self.pending_tasks, 1)
        ]
        selection_required = len(indexed_tasks) > 1
        manifest = {
            "status": "pending",
            "next_action": "complete_tasks",
            "selection_required": selection_required,
            "user_confirmation_prompt": (
                f"There are {len(indexed_tasks)} pending CodeWiki tasks. Ask the user which "
                "task range to complete before writing results. Accept values like `all`, "
                f"`1-3`, or `4`. Complete only the selected task indexes, then rerun "
                f"`{rerun_command}`."
                if selection_required
                else f"There is 1 pending CodeWiki task. Complete task 1, then rerun `{rerun_command}`."
            ),
            "range_examples": ["all", "1-3", "4"] if selection_required else ["1"],
            "agent_prompt": (
                "You are running CodeWiki in agent batch mode. If selection_required is true, "
                "ask the user which task indexes or range to complete before writing any "
                "results. Complete only the selected tasks. For each selected task, read "
                "task_path, inspect the referenced repository files directly, write the exact "
                f"required response to result_path, then rerun `{rerun_command}`. Repeat until "
                "CodeWiki reports status=complete."
            ),
            "rerun_command": rerun_command,
            "task_count": len(indexed_tasks),
            "tasks": indexed_tasks,
            "instructions": [
                "If selection_required is true, ask the user which task indexes to complete.",
                "Read each selected task_path.",
                "Complete selected tasks by reading referenced repository files.",
                "Write the exact response for each selected task to its result_path.",
                f"Rerun `{rerun_command}`.",
            ],
        }
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    @staticmethod
    def _task_guidance(kind: str) -> Dict[str, str]:
        if kind == "completion":
            return {
                "expected_action": (
                    "Read the task prompt and write the exact model response requested by the prompt."
                ),
                "output_contract": (
                    "Keep required wrapper tags such as <GROUPED_COMPONENTS>, <SUB_MODULES>, "
                    "or <OVERVIEW> when the task asks for them."
                ),
            }
        if kind == "folder_filtering":
            return {
                "expected_action": (
                    "Read the repository path list in task_path and choose the files or folders "
                    "that represent the core functionality of the project."
                ),
                "output_contract": (
                    "Return a JSON array of repository-relative paths. Reasoning text is allowed "
                    "before the JSON array, but the response must contain a valid JSON array."
                ),
            }
        if kind == "module_documentation":
            return {
                "expected_action": (
                    "Generate the final markdown document for the requested module by reading "
                    "the referenced source files directly from the repository."
                ),
                "output_contract": (
                    "Write only the final markdown document to result_path. Do not wrap the "
                    "whole document in a code fence or add chat prefaces."
                ),
            }
        if kind == "submodule_planning":
            return {
                "expected_action": (
                    "Plan useful child modules from the listed component IDs by reading the "
                    "referenced source files directly from the repository."
                ),
                "output_contract": (
                    "Write exactly one <SUB_MODULES>...</SUB_MODULES> block to result_path."
                ),
            }
        if kind == "submodule_planning_repair":
            return {
                "expected_action": (
                    "Repair the malformed submodule planning result by reading task_path and "
                    "overwriting result_path with a valid <SUB_MODULES> dictionary response."
                ),
                "output_contract": (
                    "Write exactly one <SUB_MODULES>...</SUB_MODULES> block. The wrapped "
                    "content must be a dictionary/object, not a list/array."
                ),
            }
        if kind == "completion_repair":
            return {
                "expected_action": (
                    "Repair the malformed task result by reading task_path and overwriting "
                    "result_path with a response that matches the original prompt's output contract."
                ),
                "output_contract": (
                    "Keep exactly the wrapper tags requested by the original prompt and do not "
                    "add extra prose outside the required response."
                ),
            }
        if kind == "module_documentation_repair":
            return {
                "expected_action": (
                    "Repair the existing module markdown result by fixing invalid Mermaid diagrams."
                ),
                "output_contract": (
                    "Overwrite result_path with final markdown. Keep Mermaid diagrams valid and "
                    "do not add chat prefaces."
                ),
            }
        return {
            "expected_action": "Read task_path and write the required response to result_path.",
            "output_contract": "Follow the output contract inside task_path exactly.",
        }

    @staticmethod
    def _is_mermaid_validation_error(validation_result: str) -> bool:
        return validation_result.startswith("Mermaid syntax errors found")

    @staticmethod
    def _completion_task_kind(prompt: str) -> str:
        if "return the list of relative paths in JSON format" in prompt:
            return "folder_filtering"
        return "completion"

    def has_pending_tasks(self) -> bool:
        return bool(self.pending_tasks)

    def raise_if_pending(self) -> None:
        if not self.pending_tasks:
            return

        lines = [
            f"Created {len(self.pending_tasks)} pending task(s).",
            f"Pending manifest: {self._bridge_dir / 'pending_tasks.json'}",
            "",
            "Complete them in your AI IDE, write each response to its result path,",
            f"then rerun `{os.environ.get('CODEWIKI_RERUN_COMMAND', 'python run_task.py')}`.",
            "",
        ]
        for index, task in enumerate(self.pending_tasks, 1):
            lines.extend(
                [
                    f"{index}. {task['kind']}",
                    f"   Task: {task['task_path']}",
                    f"   Result: {task['result_path']}",
                    "",
                ]
            )
        raise PendingTask("\n".join(lines).rstrip())

    @staticmethod
    def _task_id(kind: str, *parts: str) -> str:
        digest = hashlib.sha256("\n---\n".join(parts).encode("utf-8")).hexdigest()[:16]
        safe_kind = re.sub(r"[^a-zA-Z0-9_-]+", "_", kind).strip("_")
        return f"{safe_kind}_{digest}"
