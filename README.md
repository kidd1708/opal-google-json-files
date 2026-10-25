# 🏭 Content Factory — Universal Gemini Workflow App

The three Google AI Studio *Canvas* graph exports in this repository
(`Digital Product Factory.json`, `Podcast Factory.json`,
`Ultimate Social Media Factory.json`) have been **compiled into universal,
engine-agnostic code** and wrapped in a real application:

| | Before (locked to Google Canvas) | After (universal code) |
|---|---|---|
| Graph format | `embed://a2/...bgl.json` opaque node types | `graph/workflows.compiled.json` (`universal-workflow-graph/v1`) |
| Models | legacy modes `text-3-pro`, `text-3-flash`, `image-pro`, `image`, `agent` | **Gemini Pro** `gemini-2.5-pro` · **Nano Banana Pro** `gemini-3-pro-image` (+ Flash / Nano Banana tiers) |
| Routing | hidden `control-flow/routing` tool refs | explicit deterministic `router` nodes (81 routes, zero API cost) |
| Runtime | only runs inside Google Canvas | Node.js server + browser UI + Python CLI, works with any Gemini API key |

## Model mapping (the "change into Gemini Pro & Banana Pro" step)

| Legacy Canvas mode | Universal model id | Display name | Used by |
|---|---|---|---|
| `text-3-pro` | `gemini-2.5-pro` | Gemini 2.5 Pro | writing/strategy prompts |
| `text-3-flash` | `gemini-2.5-flash` | Gemini 2.5 Flash | bulk social prompts |
| `image-pro` | `gemini-3-pro-image` | Nano Banana **Pro** | banners, moodboards, paintings |
| `image` | `gemini-2.5-flash-image` | Nano Banana | simple image posts |
| `agent` | `router` | *(no API call)* | menu branching logic |

Every text node can be switched to **Gemini Pro** and every image node to
**Banana Pro** from the UI/API (`?all_pro=1`), because the runtime keeps the
legacy role (`text` vs `image`) and lets you override the concrete model.

## Quick start

```bash
# 1. Recompile the JSON graphs into universal code (already committed)
python3 tools/convert_graphs.py

# 2. Serve the app (no npm install needed — zero dependencies)
GEMINI_API_KEY=your_key_here node server/server.mjs
#    → open http://localhost:8080

# 3. Or run a workflow head-less from the terminal
GEMINI_API_KEY=your_key_here python3 cli/run_workflow.py product-factory-machine-2-0 \
    --input menu-select="1" --input 1="fitness"
```

Without an API key the app still runs end-to-end in **simulation mode**, so you
can explore all 247 nodes of the three factories immediately.

## Layout

```
tools/convert_graphs.py        # JSON→universal compiler (re-run any time)
graph/workflows.compiled.json  # THE universal working code (single source of truth)
server/server.mjs              # dependency-free HTTP server + Gemini proxy
public/index.html              # web app UI
public/app.js                  # interactive flow runner (menu → branch → generate)
public/style.css
cli/run_workflow.py            # Python CLI that executes compiled workflows
README.md
```

## API

* `GET  /api/workflows` — list compiled workflows
* `GET  /api/workflow/<id>` — full graph
* `POST /api/run` — `{workflow, inputs}` → executes the router chain and calls
  Gemini for each reached generate-node (Pro / Banana Pro per node, or forced
  `all_pro`), returns rendered outputs.

## Notes

* The original `.json` exports are kept untouched for reference/diffing.
* Outputs render as Markdown; image nodes return base64 data URLs produced by
  Nano Banana (Pro).
* MIT license (see `LICENSE`).
