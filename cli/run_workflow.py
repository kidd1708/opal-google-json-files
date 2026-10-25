#!/usr/bin/env python3
"""run_workflow.py — headless runner for the compiled universal workflows.

Examples
--------
# list available workflows
python3 cli/run_workflow.py --list

# dry-run in simulation mode (no API key needed)
python3 cli/run_workflow.py product-factory-machine-2-0 \
    --input menu-select="2"

# live run through the Gemini API (Gemini 2.5 Pro / Nano Banana Pro)
GEMINI_API_KEY=... python3 cli/run_workflow.py product-factory-machine-2-0 \
    --input menu-select="2" --all-pro

Input keys may be a node id ("menu-select"), the original canvas uuid,
or the node title/label (case-insensitive).
"""
import argparse
import json
import os
import re
import sys
import urllib.request

GRAPH = os.path.join(os.path.dirname(__file__), "..", "graph", "workflows.compiled.json")
BASE_URL = os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")
TEXT_PRO, IMAGE_PRO = "gemini-2.5-pro", "gemini-3-pro-image"
ROLE = {"gemini-2.5-pro": "text", "gemini-2.5-flash": "text",
        "gemini-3-pro-image": "image", "gemini-2.5-flash-image": "image"}


def norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def resolve(wf, ref):
    for n in wf["nodes"]:
        if n["id"] == ref:
            return n["id"]
    for n in wf["nodes"]:
        if n.get("originalId") == ref:
            return n["id"]
    return ref


def render(tpl, wf, outs):
    def sub(m):
        rid = resolve(wf, m.group(1))
        v = outs.get(rid)
        if v is None:
            title = next((n.get("title", rid) for n in wf["nodes"] if n["id"] == rid), rid)
            return f"[pending:{title}]"
        return to_str(v)
    return re.sub(r"@\{([^}]+)\}", sub, str(tpl or ""))


def to_str(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "\n\n".join(to_str(x) for x in v)
    if isinstance(v, dict):
        return v.get("text", "") + ("\n" + "\n".join(v.get("dataUrls", []))[:60] if v.get("dataUrls") else "")
    return str(v)


def call_gemini(model, prompt, api_key, image_refs=()):
    parts = [{"text": prompt}] + [{"inlineData": {"mimeType": "image/png", "data": b}} for b in image_refs]
    cfg = {"contents": [{"role": "user", "parts": parts}]}
    if ROLE.get(model, "text") == "image":
        cfg["generationConfig"] = {"responseModalities": ["TEXT", "IMAGE"]}
    req = urllib.request.Request(
        f"{BASE_URL}/models/{model}:generateContent?key={api_key}",
        data=json.dumps(cfg).encode(), headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        j = json.load(r)
    out = (j.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    texts = [p["text"] for p in out if "text" in p]
    imgs = [f"data:{p['inlineData'].get('mimeType','image/png')};base64,{p['inlineData']['data']}"
            for p in out if "inlineData" in p]
    if not texts and not imgs:
        raise RuntimeError(f"Gemini {model}: empty response ({json.dumps(j)[:200]})")
    return {"kind": "image" if ROLE.get(model) == "image" else "text",
            "text": "\n".join(texts), "dataUrls": imgs}


def simulate(node, rendered):
    if node["capability"] == "generate-image":
        return {"kind": "image", "text": f"[simulation] {node['title']}",
                "dataUrls": ["data:image/svg+xml;base64," + __import__("base64").b64encode(
                    f'<svg xmlns="http://www.w3.org/2000/svg" width="720" height="480">'
                    f'<rect width="100%" height="100%" fill="#111a2e"/>'
                    f'<text x="20" y="40" fill="#7fd1ff" font-size="20">[SIM] {node["title"]}</text></svg>'
                    .encode()).decode()]}
    return {"kind": "text",
            "text": f"[simulation] {node['title']}\n\nPrompt preview:\n{rendered[:400]}",
            "dataUrls": []}


def run(wf, inputs, all_pro=False, api_key=None):
    api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
    outs, trace, errors = {}, [], []
    idx = {}
    for k, v in inputs.items():
        n = (next((x for x in wf["nodes"] if x["id"] == k), None)
             or next((x for x in wf["nodes"] if x.get("originalId") == k), None)
             or next((x for x in wf["nodes"] if norm(x.get("title")) == norm(k)), None)
             or next((x for x in wf["nodes"] if norm(x.get("label")) == norm(k)), None))
        if n:
            idx[n["id"]] = v
        else:
            errors.append(f'Unknown input key "{k}"')

    preds = {n["id"]: [] for n in wf["nodes"]}
    succs = {n["id"]: [] for n in wf["nodes"]}
    for e in wf["edges"]:
        if e["to"] in preds:
            preds[e["to"]].append(e["from"])
        if e["from"] in succs:
            succs[e["from"]].append(e["to"])

    fired, skipped, queue = set(), set(), []

    def skip(i):
        if i in skipped:
            return
        skipped.add(i)
        for s in succs.get(i, []):
            skip(s)

    def try_fire(i):
        if i in fired or i in queue:
            return
        ps = preds.get(i, [])
        if any(p in skipped for p in ps):
            skip(i); return
        if all(p in fired for p in ps):
            queue.append(i)

    for n in wf["nodes"]:
        if not preds[n["id"]]:
            try_fire(n["id"])

    guard = 0
    while queue and guard < 5000:
        guard += 1
        i = queue.pop(0)
        if i in fired:
            continue
        node = next((n for n in wf["nodes"] if n["id"] == i), None)
        if not node:
            continue
        fired.add(i)
        cap = node["capability"]
        if cap == "input":
            outs[i] = idx.get(i, "")
            trace.append(("input", node["title"], to_str(outs[i])[:80]))
        elif cap == "router":
            src = resolve(wf, node["inputRefs"][0]["nodeId"]) if node.get("inputRefs") else (preds[i] or [""])[0]
            choice = str(outs.get(src, "")).strip().lower()
            hit = next((r for r in node.get("routes", []) if str(r.get("label", "")).strip().lower() == choice), None) \
                or next((r for r in node.get("routes", []) if choice.startswith(str(r.get("label", "")).strip().lower())), None)
            if not hit:
                trace.append(("router", node["title"], f'no route for "{outs.get(src)}" — paused'))
                for s in succs[i]:
                    skip(s)
                continue
            trace.append(("router", node["title"], f'choice="{hit["label"]}" → {hit["targetId"]}'))
            tgt = resolve(wf, hit["targetId"])
            for s in succs[i]:
                if s in (tgt, hit["targetId"]):
                    if s not in skipped and s not in fired:
                        queue.append(s)
                else:
                    skip(s)
        elif cap == "output":
            rendered = render(node.get("template"), wf, outs)
            m = re.match(r"^@\{([^}]+)\}$", str(node.get("template") or "").strip())
            rid = resolve(wf, m.group(1)) if m else None
            v = outs.get(rid) if rid else None
            outs[i] = v if isinstance(v, dict) and v.get("dataUrls") else rendered
            trace.append(("output", node["title"], ""))
        elif cap in ("generate-text", "generate-image"):
            rendered = render(node.get("template"), wf, outs)
            model = node.get("model", TEXT_PRO)
            if all_pro:
                model = IMAGE_PRO if ROLE.get(model) == "image" else TEXT_PRO
            try:
                if api_key:
                    b64s = []
                    for p in preds[i]:
                        v = outs.get(p)
                        if isinstance(v, dict):
                            for du in v.get("dataUrls", []):
                                m = re.match(r"data:[^;]+;base64,(.+)", du)
                                if m:
                                    b64s.append(m.group(1))
                    outs[i] = call_gemini(model, rendered, api_key, b64s if cap == "generate-image" else ())
                    trace.append((cap, node["title"], f"{model} OK"))
                else:
                    outs[i] = simulate(node, rendered)
                    trace.append((cap, node["title"], f"{model} SIM"))
            except Exception as ex:
                errors.append({"node": i, "error": str(ex)})
                outs[i] = {"kind": "error", "text": f"[ERROR] {ex}", "dataUrls": []}
                trace.append((cap, node["title"], f"✗ {ex}"))
        else:
            outs[i] = ""
            trace.append((cap, node["title"], "passthrough"))
        for s in succs[i]:
            try_fire(s)

    results = [{"nodeId": n["id"], "title": n["title"], "value": outs[n["id"]]}
               for n in wf["nodes"] if n["capability"] == "output" and n["id"] in fired
               and to_str(outs.get(n["id"])).strip()]
    if not results:
        results = [{"nodeId": n["id"], "title": n["title"], "value": outs[n["id"]]}
                   for n in wf["nodes"]
                   if n["capability"] in ("generate-text", "generate-image") and n["id"] in fired and n["id"] in outs]
    return {"workflow": wf["id"], "trace": trace, "results": results,
            "errors": errors, "simulated": not api_key}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workflow", nargs="?", help="workflow id (see --list)")
    ap.add_argument("--input", "-i", action="append", default=[], metavar="KEY=VALUE",
                    help="set an input by node id / title / label")
    ap.add_argument("--all-pro", action="store_true", help="force Gemini 2.5 Pro + Nano Banana Pro on every node")
    ap.add_argument("--list", action="store_true", help="list workflows and exit")
    ap.add_argument("--json", action="store_true", help="dump full JSON result")
    args = ap.parse_args()

    graph = json.load(open(GRAPH))
    if args.list or not args.workflow:
        for w in graph["workflows"]:
            ins = [n["id"] for n in w["nodes"] if n["capability"] == "input"]
            print(f"- {w['id']}  ({len(w['nodes'])} nodes)  inputs: {', '.join(ins[:8])}")
        return

    wf = next((w for w in graph["workflows"] if w["id"] == args.workflow), None)
    if not wf:
        sys.exit(f"unknown workflow '{args.workflow}' — use --list")

    inputs = {}
    for kv in args.input:
        if "=" not in kv:
            sys.exit(f"--input expects KEY=VALUE, got: {kv}")
        k, v = kv.split("=", 1)
        inputs[k] = v

    out = run(wf, inputs, all_pro=args.all_pro)

    if args.json:
        print(json.dumps(out, indent=2)[:200_000])
        return

    print(f"\n=== {wf['title']} ===  {'(SIMULATION — set GEMINI_API_KEY for live runs)' if out['simulated'] else '(LIVE)'}")
    for t in out["trace"]:
        print(f"  [{t[0]:>15}] {t[1]}" + (f"  {t[2]}" if t[2] else ""))
    for e in out["errors"]:
        print(f"  ERROR: {e}")
    for r in out["results"]:
        v = r["value"]
        text = v if isinstance(v, str) else v.get("text", "")
        print(f"\n--- {r['title']} ---\n{text}")
        for du in (v.get("dataUrls", []) if isinstance(v, dict) else []):
            print(f"[image] {du[:80]}… ({len(du)} chars)")


if __name__ == "__main__":
    main()
