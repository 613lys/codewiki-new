# CodeWiki Task

Use this skill to generate documentation tasks for a local code repository. This skill does not call a model provider, does not require an API token, and does not use MCP. It only writes local task files and reads local result files.

## Run

From this directory, run:

```powershell
python run_task.py --repo <repo_path> --output <docs_dir> --agent-json
```

To scan only selected folders under the repository root:

```powershell
python run_task.py --repo <repo_path> --scan A --scan B --output <docs_dir> --agent-json
```

`--scan` can be repeated or comma-separated:

```powershell
python run_task.py --repo <repo_path> --scan A,B --output <docs_dir> --agent-json
```

If `--scan` or `--include` is not provided, the first task is usually a
`folder_filtering` task based on `FILTER_FOLDERS_PROMPT`. Complete it by writing
a JSON array of repository-relative core paths to the task's `result_path`, for
example:

```json
["src", "packages/core"]
```

The script converts those paths into scan include patterns before dependency
analysis. If you already know the folders to scan, prefer `--scan` to skip this
filtering task.

## Task Loop

The script writes task files under:

```text
<docs_dir>/.codewiki/tasks/tasks/
```

It expects matching result files under:

```text
<docs_dir>/.codewiki/tasks/results/
```

The current task manifest is:

```text
<docs_dir>/.codewiki/tasks/pending_tasks.json
```

When the script reports pending tasks, complete each task file and write the requested output to the result path named inside that task. Then run the same `python run_task.py ...` command again. Repeat until the manifest status is `complete`.

Common task kinds:

- `folder_filtering`: choose core repository paths as a JSON array.
- `completion`: complete clustering or overview prompts while preserving required tags such as `<GROUPED_COMPONENTS>` or `<OVERVIEW>`.
- `submodule_planning`: decide whether a complex leaf module should split further and write exactly one `<SUB_MODULES>...</SUB_MODULES>` block.
- `module_documentation`: read referenced workspace files and write the final markdown document.

Before a result is used by the next step, the script validates its expected
format. If the result is malformed, the manifest will contain a repair task
such as `completion_repair`, `submodule_planning_repair`, or
`module_documentation_repair`. Read that repair task, overwrite the same
`result_path`, and rerun the same command.

When generation is complete, the script writes markdown documents, metadata,
module trees, and an HTML viewer. Open:

```text
<docs_dir>/index.html
```

The HTML viewer uses the same CodeWiki-style client-side markdown viewer: the sidebar lists modules, clicking a module renders one markdown document, and Mermaid diagrams are rendered in the browser.

To regenerate only the HTML viewer for an existing docs directory, run:

```powershell
python generate_html.py --docs-dir <docs_dir>
```

This does not run repository analysis or create task files.

Optional HTML-only arguments:

```powershell
python generate_html.py --docs-dir <docs_dir> --output <docs_dir>\index.html --title "Project Documentation" --repo <repo_path>
```

Use HTML-only generation after editing markdown files, copying docs to another
directory, or regenerating the viewer without rerunning analysis.

## Useful Options

- `--include "*.py,*.ts"` limits file patterns.
- `--exclude "*test*,*spec*"` excludes file patterns.
- `--focus "src/core,packages/app"` asks the generated tasks to focus on specific modules.
- `--doc-type reference|architecture|user-guide|developer` changes the documentation style.
- `--instructions "..."` adds custom guidance to generated tasks.
- `--max-depth N` controls module decomposition depth.
- `--verbose` prints progress details.
