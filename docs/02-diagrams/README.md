# Diagram Suite — 24 diagrams

All 24 sources are in [`src/`](src/), rendered to SVG **and** PNG in [`render/`](render/).
Every source passes `plantuml -checkonly`, and every diagram is **100% standalone** — you can paste any one of
them into draw.io with no other file.

---

## Importing into draw.io

1. **Arrange → Insert → Advanced → PlantUML…**
2. Open the `.puml` you want from [`src/`](src/), copy the **entire** file
3. Paste it into the dialog and click **Insert**

To edit the generated shape afterwards: **Extras → Edit Diagram…** exposes the underlying XML.

### Why this works — and the rules that keep it working

draw.io renders one pasted blob on its own server. Anything the blob depends on that is not in the blob will
fail. So every file in `src/` obeys:

| Rule | Why |
|---|---|
| **No local includes** — no `!include ./_style.puml`, no relative paths | draw.io has no access to your filesystem. The shared style block is **duplicated verbatim** into all 24 files |
| **No `!includeurl`** — nothing is fetched at render time | Breaks offline, and draw.io's renderer will not fetch it. URLs appearing as *label text* (e.g. `http://vllm:30000` documenting a service endpoint) are fine — nothing resolves them |
| **Only PlantUML stdlib includes** (`!include <…>`) | These ship inside `plantuml.jar` and resolve on draw.io's server and offline |
| **No AWS icons** | AWS is genuinely not used here — see [`../ASSUMPTIONS.md#a5`](../ASSUMPTIONS.md) |

Both rules are mechanically verified in [`../04-VERIFICATION.md`](../04-VERIFICATION.md):

```bash
grep -rn '!include' src/ | grep -v '!include <'   # must print nothing
grep -rn 'includeurl' src/                        # must print nothing
```

(Both return nothing. A separate grep for `http://` finds 10 hits, all of them internal service endpoints
written as arrow labels in diagrams 06 and 21 — descriptive text, never resolved.)

---

## The 24 diagrams

| # | File | Type | Audience | The one question it answers |
|---|---|---|---|---|
| 01 | [`01-c4-context.puml`](src/01-c4-context.puml) | C4 L1 Context | Exec | What is this system, who uses it, and what does it talk to? |
| 02 | [`02-c4-container.puml`](src/02-c4-container.puml) | C4 L2 Container | Exec / Eng | What are the running pieces, on which ports, speaking what? |
| 03 | [`03-c4-component-orchestrator.puml`](src/03-c4-component-orchestrator.puml) | C4 L3 Component | Eng | How is the orchestrator organised — engines, core guards, and who calls whom? |
| 04 | [`04-c4-component-syncworker.puml`](src/04-c4-component-syncworker.puml) | C4 L3 Component | Eng | How does Salesforce data physically get into the warehouse? |
| 05 | [`05-c4-component-frontend.puml`](src/05-c4-component-frontend.puml) | C4 L3 Component | Eng | How does the Next.js app route a request and render a stream? |
| 06 | [`06-deployment.puml`](src/06-deployment.puml) | C4 Deployment | Eng / Ops | What runs on the box, with how much GPU, on which ports and volumes? |
| 07 | [`07-seq-chat-stream.puml`](src/07-seq-chat-stream.puml) | Sequence | Eng | What happens between pressing Enter and seeing the first token? |
| 08 | [`08-seq-router-dispatch.puml`](src/08-seq-router-dispatch.puml) | Sequence | Eng | How is an engine chosen, and what happens when classification fails? |
| 09 | [`09-seq-agent-loop.puml`](src/09-seq-agent-loop.puml) | Sequence | Eng | How does the agent plan, execute steps, and avoid looping forever? |
| 10 | [`10-seq-text-to-sql.puml`](src/10-seq-text-to-sql.puml) | Sequence | Eng / Security | How does a question become SQL, and what stops that SQL being dangerous? |
| 11 | [`11-seq-rag.puml`](src/11-seq-rag.puml) | Sequence | Eng | How is retrieved context selected, ranked and budgeted into the prompt? |
| 12 | [`12-seq-sf-sync.puml`](src/12-seq-sf-sync.puml) | Sequence | Eng | How does one sync cycle work, and is it safe to kill mid-run? |
| 13 | [`13-seq-upload-document.puml`](src/13-seq-upload-document.puml) | Sequence | Eng / Security | What happens to an uploaded file, and what stops a hostile one? |
| 14 | [`14-seq-compaction.puml`](src/14-seq-compaction.puml) | Sequence | Eng | What happens when a conversation outgrows the model's window? |
| 15 | [`15-seq-auth.puml`](src/15-seq-auth.puml) | Sequence | Security | Who is the user, and what actually authenticates them? (Answer: nothing) |
| 16 | [`16-activity-request-lifecycle.puml`](src/16-activity-request-lifecycle.puml) | Activity (swimlanes) | Eng | End to end, which layer owns which decision? |
| 17 | [`17-state-conversation.puml`](src/17-state-conversation.puml) | State | Eng | What states can a conversation be in, and what moves it between them? |
| 18 | [`18-er-data-model.puml`](src/18-er-data-model.puml) | ER | Eng / Data | What is stored where, with what keys — and what is *not* cleaned up? |
| 19 | [`19-dfd-trust-boundaries.puml`](src/19-dfd-trust-boundaries.puml) | DFD L0 + L1 | Security | Where does data cross a trust boundary, and which flows carry CRM/PII? |
| 20 | [`20-class-engines.puml`](src/20-class-engines.puml) | Class | Eng | What is the engine contract, and which modules violate it? |
| 21 | [`21-network-ports.puml`](src/21-network-ports.puml) | Network | Security / Ops | What is listening, what is published, and what is authenticated? |
| 22 | [`22-threat-model.puml`](src/22-threat-model.puml) | Component + notes | Security | For each STRIDE threat, is there a control, and does it work? |
| 23 | [`23-cicd-test-topology.puml`](src/23-cicd-test-topology.puml) | Activity | Eng / Lead | What is tested and automated today — and what simply does not exist? |
| 24 | [`24-remediation-gantt.puml`](src/24-remediation-gantt.puml) | Gantt | Exec / Lead | In what order do we fix things over the next 90 days? |

**Start here:** 01 → 02 → 06 → 21 for the shape of the system and its exposure; then 07 and 10 for how a request
actually flows; then 22 for the security picture.

---

## Shared legend

The same palette is used in **all 24** diagrams, and the same entity always has the same name and colour.

| Colour | Hex | Layer |
|---|---|---|
| 🔵 | `#2563EB` | frontend / edge |
| 🟣 | `#7C3AED` | orchestrator / app logic |
| 🟢 | `#16A34A` | inference / GPU |
| 🟠 | `#D97706` | storage / data |
| ⚫ | `#64748B` | external systems |
| 🔴 | `#DC2626` | risk / trust boundary |

**Canonical entity names** (identical everywhere): `frontend`, `orchestrator`, `sync-worker`, `searxng`,
`vllm` (MAIN), `vllm-router` (AGENT), `vllm-embed`, `vllm-vision`, `DuckDB warehouse`, `LanceDB`,
`app.sqlite3`, `Salesforce org`.

**Notation**
- Dashed red rectangle = trust boundary
- Arrow labels carry **protocol · payload · sync|async**, e.g. `HTTP POST /chat · JSON · async`
- Amber `note` boxes flag a retry, timeout, unbounded resource, swallowed exception or unauthenticated hop, and
  cite `file:LINE`
- Finding IDs (`SEC-01`, `PERF-01`, …) in notes index [`../03-report/FINDINGS.csv`](../03-report/FINDINGS.csv)

---

## Regenerating

```bash
./render.sh
```

That script syntax-checks every source, then writes SVG and PNG into `render/`. It refuses to render if any
file fails the check.

### Toolchain notes

Neither `plantuml` nor `graphviz` is installed on this host and `apt-get` requires a password, so `render.sh`:

- drives **`plantuml.jar` 1.2024.7** with the system JRE. Override the location with `PLANTUML_JAR=/path/to/jar`.
  To fetch it:
  ```bash
  curl -sSL -o plantuml.jar \
    https://github.com/plantuml/plantuml/releases/download/v1.2024.7/plantuml-1.2024.7.jar
  ```
- uses PlantUML's built-in **Smetana** layout engine (`-Playout=smetana`) because `dot` is absent. The flag is
  passed on the **command line only** — never written into the `.puml` files — so the sources stay portable and
  draw.io renders them with its own graphviz.

> **`-checkonly` exits 0 even when a diagram fails to parse.** `render.sh` therefore greps its *output* for
> `error` rather than testing `$?`. If you write your own tooling, do the same — this was verified the hard way.

### Sprite naming gotcha

In the bundled tupadr3 version, font-awesome-5 sprites are registered under their **bare** name — `hdd`,
`database`, `cogs` — **not** `fa5_hdd`. A wrong sprite name usually renders as a silently missing icon rather
than an error, so verify new icons before relying on them:

```bash
printf '@startuml t\n!include <tupadr3/common>\n!include <tupadr3/font-awesome-5/hdd>\nlistsprites\n@enduml\n' > /tmp/p.puml
java -jar plantuml.jar -Playout=smetana -tsvg -o /tmp /tmp/p.puml   # lists the real sprite names
```

Also note `<tupadr3/devicons2/react>` does **not** exist — the name is `react_original`. The full verified
allowlist is in [`_STYLE.md`](_STYLE.md).
