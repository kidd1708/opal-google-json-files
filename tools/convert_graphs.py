#!/usr/bin/env python3
"""
convert_graphs.py — Universal workflow compiler.

Reads the three Google AI Studio "Canvas / bgl.json" graph exports in this
repository and converts them into one portable, engine-agnostic JSON graph
(`graph/workflows.compiled.json`) that runs on ANY provider:

  * node types are mapped to universal capabilities (input / generate-text /
    generate-image / output)
  * every Gemini model referenced by the original graphs is remapped to the
    canonical universal ids used by this app:
        text-3-pro   -> gemini-2.5-pro         (Gemini Pro)
        text-3-flash -> gemini-2.5-flash       (fast tier of the Pro family)
        image-pro    -> gemini-3-pro-image     (Nano Banana Pro)
        image        -> gemini-2.5-flash-image (Nano Banana)
        agent        -> router (pure code, no LLM call needed)
  * {{...}} template references (inputs + control-flow/routing tools) are
    parsed into a structured `refs` list so any runtime can resolve them.
"""

import json
import re
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Universal model registry (single source of truth for the whole app).
# ---------------------------------------------------------------------------
MODEL_MAP = {
    # legacy Canvas generation-mode ids  ->  universal Gemini model ids
    "text-3-pro": "gemini-2.5-pro",
    "text-3-flash": "gemini-2.5-flash",
    "image-pro": "gemini-3-pro-image",      # Nano Banana Pro
    "image": "gemini-2.5-flash-image",      # Nano Banana
    "agent": "router",                       # deterministic routing, no API call
}

CAPABILITY_MAP = {
    "21ee02e7-83fa-49d0-964c-0cab10eafc2c": "input",
    "module:main": None,          # decided by generation-mode
    "module:render-outputs": "output",
}

SOURCE_FILES = [
    "Digital Product Factory.json",
    "Podcast Factory.json",
    "Ultimate Social Media Factory.json",
]

# NOTE: the Canvas exports store the reference JSON *inline* inside the outer
# JSON string (i.e. with escaped quotes), so the literal text we scan contains
# sequences like  "}}\n{{"  where a backslash-n is two literal characters, not
# a newline.  A lazy `.*?` would happily swallow the inner `}` and produce
# invalid JSON, so we forbid backslashes and braces inside the payload.
REF_RE = re.compile(r"\{\{([^{\\}]+(?:\\.[^{\\}]*)*)\}\}")


def slugify(title: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", title.strip()).strip("-").lower()
    return s or uuid.uuid4().hex[:8]


def parts_text(container) -> str:
    if not container:
        return ""
    parts = container.get("parts", []) if isinstance(container, dict) else []
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def build_raw_index(raw):
    """original id -> node title, for label lookups during conversion."""
    return {n["id"]: n["metadata"]["title"] for n in raw["nodes"]}


def parse_refs(text: str):
    """Return (cleaned_template, [ref,...]) where each ref is structured."""
    refs = []

    def repl(m):
        payload = m.group(1)
        try:
            r = json.loads("{" + payload + "}")
        except Exception:
            return m.group(0)
        kind = r.get("type")
        if kind == "tool" and r.get("path") == "control-flow/routing":
            refs.append({
                "kind": "route",
                "nodeId": r.get("instance"),
                "title": r.get("title"),
            })
            return "@{" + r.get("instance", "") + "}"
        if kind in ("in", "out"):
            refs.append({
                "kind": kind,
                "nodeId": r.get("path"),
                "title": r.get("title"),
            })
            return "@{" + r.get("path", "") + "}"
        return m.group(0)

    cleaned = REF_RE.sub(repl, text)
    return cleaned, refs


def capability_for(node) -> str:
    frag = node["type"].split("#", 1)[1]
    base = CAPABILITY_MAP.get(frag)
    if base:
        return base
    mode = node.get("configuration", {}).get("generation-mode")
    if mode in ("image", "image-pro"):
        return "generate-image"
    if mode == "agent":
        return "router"
    return "generate-text"


def convert_file(fname: str):
    raw = json.loads((ROOT / fname).read_text(encoding="utf-8"))
    byid_title = build_raw_index(raw)

    # Pass 1 — build the original-id -> short-id map up front, because router
    # nodes reference routing targets that may appear *later* in the node list.
    id_map, used = {}, set()
    for n in raw["nodes"]:
        base = slugify(n["metadata"]["title"])
        short, i = base, 2
        while short in used:
            short = f"{base}-{i}"
            i += 1
        used.add(short)
        id_map[n["id"]] = short

    nodes_out, edges_out = [], []

    for n in raw["nodes"]:
        nid = n["id"]
        short = id_map[nid]

        cfg = n.get("configuration", {})
        cap = capability_for(n)
        node = {
            "id": short,
            "originalId": nid,
            "title": n["metadata"]["title"],
            "capability": cap,
            "position": n.get("metadata", {}).get("visual", {"x": 0, "y": 0}),
        }

        if cap == "input":
            node["label"] = parts_text(cfg.get("description"))
            node["required"] = bool(cfg.get("p-required", True))
            node["modality"] = cfg.get("p-modality", "Text")

        elif cap in ("generate-text", "generate-image"):
            legacy = cfg.get("generation-mode", "text-3-pro")
            tmpl, refs = parse_refs(parts_text(cfg.get("config$prompt")))
            sys_t, _ = parse_refs(parts_text(cfg.get("b-system-instruction")))
            node["model"] = MODEL_MAP[legacy]
            node["legacyMode"] = legacy
            node["template"] = tmpl
            node["refs"] = [{"kind": r["kind"],
                             "nodeId": id_map.get(r["nodeId"], r["nodeId"]),
                             "title": r["title"]} for r in refs]
            if sys_t:
                node["systemInstruction"] = sys_t
            if "p-aspect-ratio" in cfg:
                node["aspectRatio"] = cfg["p-aspect-ratio"]

        elif cap == "router":
            tmpl, refs = parse_refs(parts_text(cfg.get("config$prompt")))
            inputs = [r for r in refs if r["kind"] == "in"]
            # Authoritative route list: outgoing edges whose outPort names the
            # target node id (the Canvas routing tool wires itself that way).
            edge_routes = [e["to"] for e in raw["edges"]
                           if e["from"] == nid and e.get("out") == e["to"]]
            routes = []
            seen = set()
            for oid in edge_routes:
                sid = id_map[oid]
                if sid not in seen:
                    seen.add(sid)
                    label = next((rr["title"] for rr in refs
                                  if rr["kind"] == "route" and rr["nodeId"] == oid),
                                 byid_title.get(oid, oid))
                    routes.append({"targetId": sid, "label": label})
            # keep any declared route refs that had no matching edge (rare)
            for r in refs:
                if r["kind"] == "route" and r["nodeId"]:
                    sid = id_map.get(r["nodeId"])
                    if sid and sid not in seen:
                        seen.add(sid)
                        routes.append({"targetId": sid, "label": r["title"]})
            node["routes"] = routes
            node["inputRefs"] = [{"nodeId": id_map.get(r["nodeId"], r["nodeId"]),
                                  "title": r["title"]} for r in inputs]

        elif cap == "output":
            tmpl, refs = parse_refs(parts_text(cfg.get("text")))
            node["template"] = tmpl
            node["refs"] = [{"nodeId": id_map.get(r["nodeId"], r["nodeId"]),
                             "title": r["title"], "kind": r["kind"]} for r in refs]
            node["renderMode"] = cfg.get("p-render-mode", "markdown")

        nodes_out.append(node)

    for e in raw["edges"]:
        edges_out.append({
            "from": id_map[e["from"]],
            "to": id_map[e["to"]],
            "outPort": e.get("out", "context"),
            "inPort": e.get("in", "context"),
        })

    return {
        "id": slugify(raw["title"]),
        "title": raw["title"],
        "description": raw.get("description", ""),
        "sourceFile": fname,
        "nodes": nodes_out,
        "edges": edges_out,
    }


def main():
    out_dir = ROOT / "graph"
    out_dir.mkdir(exist_ok=True)
    workflows = [convert_file(f) for f in SOURCE_FILES]

    doc = {
        "schema": "universal-workflow-graph/v1",
        "generatedBy": "tools/convert_graphs.py",
        "provider": "google-generative-ai",
        "models": {
            "gemini-2.5-pro": {"role": "text", "displayName": "Gemini 2.5 Pro"},
            "gemini-2.5-flash": {"role": "text", "displayName": "Gemini 2.5 Flash"},
            "gemini-3-pro-image": {"role": "image", "displayName": "Gemini 3 Pro Image (Nano Banana Pro)"},
            "gemini-2.5-flash-image": {"role": "image", "displayName": "Gemini 2.5 Flash Image (Nano Banana)"},
            "router": {"role": "logic", "displayName": "Deterministic router (no API call)"},
        },
        "modelMap": MODEL_MAP,
        "workflows": workflows,
    }

    target = out_dir / "workflows.compiled.json"
    target.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    total_nodes = sum(len(w["nodes"]) for w in workflows)
    total_edges = sum(len(w["edges"]) for w in workflows)
    print(f"Wrote {target.relative_to(ROOT)}")
    for w in workflows:
        caps = {}
        for n in w["nodes"]:
            caps[n["capability"]] = caps.get(n["capability"], 0) + 1
        print(f"  - {w['title']}: {len(w['nodes'])} nodes {caps}, {len(w['edges'])} edges")
    print(f"TOTAL: {total_nodes} nodes, {total_edges} edges")


if __name__ == "__main__":
    main()
