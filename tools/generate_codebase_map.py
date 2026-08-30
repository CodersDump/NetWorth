#!/usr/bin/env python3
"""
Regenerate the auto-generated sections of docs/CODEBASE_MAP.md.

- Backend: parsed with the `ast` module (exact signatures + line numbers).
- Frontend (frontend/js/app.js): regex over `function name(...)`,
  `const name = (...) =>`, and `// ==== section ====` banners.

Only the content between these markers is replaced; every hand-written
section (routes, data model, infra, coupling notes, scripts) is preserved:

    <!-- AUTOGEN:BACKEND START ... -->   ...   <!-- AUTOGEN:BACKEND END -->
    <!-- AUTOGEN:FRONTEND START ... -->  ...   <!-- AUTOGEN:FRONTEND END -->

Run from the repo root:  python tools/generate_codebase_map.py
"""
import ast
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP = os.path.join(REPO, "docs", "CODEBASE_MAP.md")

LAMBDA_ORDER = ["whoami", "register_player", "players", "groups",
                "matches", "tournaments", "finance", "progress_scheduler"]

# Curated app.js section banners: line -> title. Update if you add sections.
# Re-derived 2026-08-30 straight from the file's own "---------- X ----------"
# banner comments (grep '^\s*//\s*-{5,}') plus a couple of named-function
# anchors (loadProfileBundle) where no banner exists - see BACKLOG.md's
# v1.78.0 entry. Do this same grep again next time these drift rather than
# hand-nudging line numbers, since inserted code shifts everything after it.
JS_SECTIONS = {
    14: "Token freshness & authedFetch", 205: "Nickname/name display toggle",
    342: "Data-load helpers", 1199: "Segmented controls (match type / points-to-win)",
    1279: "Live point-by-point scoring",
    1360: "Split-screen live scoring", 1480: "Player registration",
    1588: "Delete / edit player", 1662: "Groups", 1791: "Matches (record/list/game-log)",
    1860: "Voice match entry", 2108: "Team pairing preview",
    2204: "Quick record: tap mode + Sessions + voice stack + shared queue",
    3170: "Unsaved-match safety net", 3256: "Game log & CSV export",
    3753: "Profile card customization", 4229: "Quests", 4341: "Achievements",
    4660: "Store & events admin", 5195: "Image uploads",
    5539: "Profile bundle / cards / charts",
    6495: "UPI payment card", 6553: "Finance tab (view-key + role gated)",
    7895: "Auth UI (Cognito login/signup/session)",
    7905: "Match review & reorder (SuperAdmin)", 8742: "Init & session restore",
    8761: "Tournaments", 12308: "Live scoring inside tournaments",
}


def summarize_py(path):
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    out = {"module_doc": ast.get_docstring(tree), "functions": [],
           "consts": [], "loc": src.count("\n") + 1}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = (ast.get_docstring(node) or "").strip().split("\n")[0]
            out["functions"].append(
                (node.name, ", ".join(a.arg for a in node.args.args),
                 node.lineno, doc))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.isupper():
                    out["consts"].append(t.id)
    return out


def backend_md():
    root = os.path.join(REPO, "backend", "lambdas")
    files = {}
    for name in os.listdir(root):
        p = os.path.join(root, name, "index.py")
        if os.path.isfile(p):
            files[name] = p
    order = sorted(files, key=lambda n: LAMBDA_ORDER.index(n)
                   if n in LAMBDA_ORDER else 99)
    lines = ["### Backend Lambdas (`backend/lambdas/<name>/index.py`)\n"]
    for name in order:
        info = summarize_py(files[name])
        lines.append(f"\n#### `{name}` — {info['loc']} LOC")
        md = (info["module_doc"] or "").split("\n")[0]
        if md:
            lines.append(f"_{md}_\n")
        if info["consts"]:
            lines.append("**Module constants:** `" + "`, `".join(info["consts"]) + "`\n")
        lines.append("| Function | Args | Line | What it does |")
        lines.append("|---|---|---|---|")
        for fn, args, ln, doc in info["functions"]:
            doc = (doc or "—").replace("|", "\\|")[:95]
            lines.append(f"| `{fn}` | {args[:45]} | {ln} | {doc} |")
    return "\n".join(lines)


def frontend_md():
    p = os.path.join(REPO, "frontend", "js", "app.js")
    src = open(p, encoding="utf-8").read().replace("\r\n", "\n")
    all_lines = src.split("\n")
    func_re = re.compile(r"^\s*(?:async\s+)?function\s+([A-Za-z0-9_$]+)\s*\(([^)]*)\)")
    arrow_re = re.compile(r"^\s*(?:const|let|var)\s+([A-Za-z0-9_$]+)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>")
    funcs = []
    for i, ln in enumerate(all_lines, 1):
        m = func_re.match(ln) or arrow_re.match(ln)
        if m:
            funcs.append((i, m.group(1), m.group(2).strip()))
    bl_sorted = sorted(JS_SECTIONS)

    def sec_for(line):
        cur, cur_bl = "Auth/token core (top of file)", 0
        for bl in bl_sorted:
            if bl <= line:
                cur, cur_bl = JS_SECTIONS[bl], bl
            else:
                break
        return cur_bl, cur

    groups = {}
    for i, name, args in funcs:
        groups.setdefault(sec_for(i), []).append((i, name, args))

    out = [f"### Frontend (`frontend/js/app.js` — {len(all_lines)} LOC, "
           f"flat global script, ~{len(funcs)} functions)\n",
           "_Loaded by `index.html` after an inline `<script>` defines the globals "
           "`API_BASE_URL`, `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID`, `UPI_ID`, "
           "`FINANCE_VIEW_KEY` placeholders. Functions live in global scope (not an IIFE); most are "
           "wired to `onclick=` in the HTML._\n"]
    for (bl, title) in sorted(groups):
        out.append(f"\n**{title}**  (from L{bl})")
        for i, name, args in groups[(bl, title)]:
            out.append(f"- `{name}({args[:40]})` — L{i}")
    return "\n".join(out)


def replace_between(text, start_pat, end_pat, new_body):
    m = re.search(start_pat, text)
    e = re.search(end_pat, text)
    if not m or not e:
        sys.exit(f"Markers not found: {start_pat!r} / {end_pat!r}. "
                 "Is docs/CODEBASE_MAP.md the marked version?")
    return text[:m.end()] + "\n" + new_body + "\n" + text[e.start():]


def main():
    text = open(MAP, encoding="utf-8").read()
    text = replace_between(text,
                           r"<!-- AUTOGEN:BACKEND START[^>]*-->",
                           r"<!-- AUTOGEN:BACKEND END -->", backend_md())
    text = replace_between(text,
                           r"<!-- AUTOGEN:FRONTEND START[^>]*-->",
                           r"<!-- AUTOGEN:FRONTEND END -->", frontend_md())
    open(MAP, "w", encoding="utf-8").write(text)
    print("Regenerated docs/CODEBASE_MAP.md (backend + frontend sections).")
    print("Remember: routes/data-model/infra sections are hand-written — "
          "update them yourself if the API or schema changed.")


if __name__ == "__main__":
    main()
