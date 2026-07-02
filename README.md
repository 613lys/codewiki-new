# CodeWiki Task Script

This is a stripped-down CodeWiki build that only supports the local task-file workflow.

## Install

```powershell
python -m pip install -r requirements.txt
```

No editable install or command registration is required.

## Run

Run from this directory:

```powershell
python run_task.py --repo . --output docs --agent-json
```

Limit scanning to selected folders under the repository root:

```powershell
python run_task.py --repo . --scan A --scan B --output docs --agent-json
```

When CodeWiki needs generated output, it writes task files under:

```text
docs/.codewiki/tasks/tasks
```

Complete the selected task files in your AI IDE and write the exact required output to the matching result paths under:

```text
docs/.codewiki/tasks/results
```

Then rerun the same `python run_task.py ...` command until `pending_tasks.json` reports `status: complete`.
When generation completes, `index.html` is created in the output directory using the CodeWiki static documentation viewer.

Each result is validated before it is used by the next pipeline step. Invalid
wrapper tags, malformed grouped component dictionaries, malformed
`<SUB_MODULES>` results, markdown wrapped in a single code fence, and invalid
Mermaid diagrams create a repair task. Complete the repair task by overwriting
the same result file, then rerun the same command.

To regenerate only the HTML viewer for an existing docs directory:

```powershell
python generate_html.py --docs-dir docs
```

## Useful Options

```powershell
python run_task.py --include "*.py,*.ts" --exclude "node_modules/**,dist/**"
python run_task.py --scan apps/backend --scan packages/core
python run_task.py --focus "src/core,src/reference" --doc-type architecture
python run_task.py --max-depth 3 --max-token-per-module 25000
python run_task.py --instructions "Focus on architecture and public interfaces"
```
