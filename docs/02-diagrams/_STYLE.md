# Diagram Style Guide

The contract every file in [`src/`](src/) obeys. Follow it when adding a diagram so the suite stays coherent
and draw.io-importable.

---

## 1. The canonical style block

Paste this verbatim at the top of every file, replacing `<DIAGRAM-NAME>` with the filename minus `.puml`.
It is **duplicated in all 24 files on purpose** — see §4.

```plantuml
@startuml <DIAGRAM-NAME>
!theme plain
skinparam backgroundColor #FFFFFF
skinparam defaultFontName "Inter, Segoe UI, sans-serif"
skinparam defaultFontSize 13
skinparam roundCorner 8
skinparam shadowing false
skinparam ArrowColor #4A5568
skinparam ArrowFontColor #4A5568
skinparam NoteBackgroundColor #FFFBEB
skinparam NoteBorderColor #F59E0B
' Layer palette — byte-identical across ALL 24 diagrams
!$C_EDGE = "#2563EB"   /' frontend / edge '/
!$C_APP  = "#7C3AED"   /' orchestrator / app logic '/
!$C_GPU  = "#16A34A"   /' inference / GPU '/
!$C_DATA = "#D97706"   /' storage / data '/
!$C_EXT  = "#64748B"   /' external systems '/
!$C_RISK = "#DC2626"   /' risk / trust boundary '/
```

> **`@startuml <name>` sets the output filename.** PlantUML names the rendered SVG/PNG after this token, *not*
> after the source file. If they disagree, `render/` will not match `src/`. Keep them identical.

Order within a file: style block → `!include` lines → `title` → body → `legend right` → `@enduml`.

---

## 2. Palette and naming

| Token | Hex | Use for |
|---|---|---|
| `$C_EDGE` | `#2563EB` | frontend, browser, Next.js API routes |
| `$C_APP` | `#7C3AED` | orchestrator, engines, core modules, sync-worker |
| `$C_GPU` | `#16A34A` | vLLM services, embeddings, reranker |
| `$C_DATA` | `#D97706` | DuckDB, LanceDB, SQLite, Parquet, volumes |
| `$C_EXT` | `#64748B` | Salesforce, search providers, public web, Hugging Face |
| `$C_RISK` | `#DC2626` | trust boundaries, unauthenticated paths, missing controls |

**The same entity must have the same name and the same colour in every diagram.** Canonical names:

`frontend` · `orchestrator` · `sync-worker` · `searxng` · `vllm` (MAIN) · `vllm-router` (AGENT) ·
`vllm-embed` · `vllm-vision` · `DuckDB warehouse` · `LanceDB` · `app.sqlite3` · `Salesforce org`

Do not introduce `orchestrator-api`, `the orchestrator`, `FastAPI service` or similar variants.

---

## 3. Verified include allowlist

Every entry below was confirmed to resolve offline in the bundled PlantUML 1.2024.7. **Anything not on this
list must be probed before use** (§6).

**C4** — `<C4/C4_Context>` · `<C4/C4_Container>` · `<C4/C4_Component>` · `<C4/C4_Deployment>` · `<C4/C4_Dynamic>`

**tupadr3** — `<tupadr3/common>` is required before any tupadr3 sprite.
- `devicons2/`: `python` · `nextjs` · `typescript` · `docker` · `nodejs` · `postgresql` · `react_original`
- `font-awesome-5/`: `database` · `microchip` · `server` · `lock` · `shield_alt` · `search` · `file_alt` ·
  `chart_bar` · `user` · `robot` · `cloud` · `exclamation_triangle` · `network_wired` · `hdd` · `cogs`

**logos** — `salesforce` · `python` · `nextjs` · `react` · `typescript` · `docker-icon` · `nvidia`

**material / office** — `<material/memory>` · `<material/database>` · `<office/Servers/database_server>`

### Verified ABSENT — never use

| Wrong | Right |
|---|---|
| `<tupadr3/devicons2/react>` | `<tupadr3/devicons2/react_original>` |
| `<tupadr3/devicons2/nextjs_original>` | `<tupadr3/devicons2/nextjs>` |
| `<logos/duckdb>` | none exists — use `font-awesome-5/database` in `$C_DATA` |
| `awslib14/<Service>/<Icon>` | not applicable — **AWS is not used here** ([`../ASSUMPTIONS.md#a5`](../ASSUMPTIONS.md)) |

### Sprite names have no `fa5_` prefix

`!include <tupadr3/font-awesome-5/hdd>` registers a sprite called **`hdd`**, so write `$sprite="hdd"`.
`$sprite="fa5_hdd"` is wrong. This matters because a bad sprite name **usually renders as a silently missing
icon rather than an error** — one instance in this suite did raise a parse error, which is how the convention
was caught across every file.

---

## 4. draw.io constraints (non-negotiable)

1. **Every file is standalone.** draw.io imports one pasted blob with no filesystem access, so the style block
   is duplicated rather than `!include`d.
2. **No local includes.** `grep -rn '!include' src/ | grep -v '!include <'` must print nothing.
3. **No `!includeurl`, and no bare `http://` / `https://` anywhere** — it breaks offline rendering and draw.io's
   renderer will not fetch it. Write URLs as prose inside notes only if genuinely required.
4. **Only stdlib includes** (`!include <…>`), from §3.

---

## 5. Quality bar

- **`title`** on every diagram, citing its evidence: `title … \n(evidence: orchestrator/app/main.py:274)`
- **Every arrow labelled protocol · payload · sync|async** — `HTTP POST /chat · JSON · async`,
  `SSE text/event-stream`, `JWT Bearer · HTTPS`, `file I/O · blocking`
- **`note` callouts on every** retry, timeout, unbounded resource, swallowed exception and unauthenticated hop,
  each citing `file:LINE`. These notes carry most of the suite's value — make them specific, never decorative
- **Trust boundaries** as dashed red (`$C_RISK`) rectangles with a label
- **Max ~25 nodes.** Split a diagram rather than crowd it. (`03-c4-component-orchestrator` runs to 28 because a
  complete engine↔core map is more useful whole than split; it is the deliberate exception)
- **`legend right`** closing every diagram, explaining colours and notation
- Prefer **sequence diagrams** for flows — they need no C4 includes and render without graphviz
- Reference finding IDs (`SEC-01`, `PERF-01`, …) so a diagram links back to
  [`../03-report/FINDINGS.csv`](../03-report/FINDINGS.csv)

---

## 6. Verify before you commit

```bash
# 1. syntax — NOTE: -checkonly exits 0 even on failure, so grep the OUTPUT
out=$(java -jar plantuml.jar -checkonly src/NN-name.puml 2>&1); echo "$out"
echo "$out" | grep -qi error && echo "BROKEN" || echo "OK"

# 2. draw.io portability
grep -n '!include' src/NN-name.puml | grep -v '!include <'   # must print nothing
grep -n 'includeurl\|http://\|https://' src/NN-name.puml     # must print nothing

# 3. probe any new sprite before relying on it
printf '@startuml t\n!include <tupadr3/common>\n!include <tupadr3/font-awesome-5/NEW>\nlistsprites\n@enduml\n' > /tmp/p.puml
java -jar plantuml.jar -Playout=smetana -tsvg -o /tmp /tmp/p.puml   # prints the real sprite names

# 4. render everything
./render.sh
```

A rendered SVG under ~3 KB is almost always PlantUML's *error image* rather than a diagram — check the size,
not just the exit code.
