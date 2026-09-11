"""Artifact Studio — a person asks for a document in chat and gets a real file.

    "Create a professional PDF about this."     → document   → .pdf (+ .docx)
    "Make a deck for the CEO."                  → presentation → .pptx (+ preview)
    "Turn this into an Excel tracker."          → workbook   → .xlsx
    "Make slide 4 shorter." / "Also as PDF."    → a NEW VERSION of the same artifact

The shape, in one paragraph. `intent.py` decides — deterministically first —
whether a turn asks for a file at all, and for what. `engines/artifact.py`
turns the conversation (and, where allowed, sources) into an `ArtifactSpec`
(`spec.py`): structured content the model writes and this code validates.
`pipeline.py` runs that as a DURABLE job — a row in `artifact_jobs`, a lease,
a heartbeat, resumable stages — so the file is produced whether or not the
browser stays open. `render/` turns the spec into files with deterministic
renderers (never the model), `validate` reopens every file it produced,
`preview` rasterises pages for the in-app viewer, and `store` publishes the
version atomically under an owner-scoped, ID-keyed directory. `api.py` serves
files, previews and status to their owner only. The chat turn that started the
job forwards its progress as `step` events and ends with ONE `meta` carrying
`artifacts[]`. It does NOT also emit the legacy `report_files[]`: those names
resolve through the flat `/reports/{filename}` route, which cannot serve an
ID-keyed file, and a card whose link 404s is worse than no card. The admin
report listing reads its own table and gains an artifacts view separately.

Module ownership — one owner per file, so the pieces can be built side by side:

    types.py     stages, statuses, effort budgets, MIME table, storage paths   (contracts)
    spec.py      ArtifactSpec and its typed variants, pydantic v2               (contracts)
    formats.py   which formats a request gets when it does not say             (contracts)
    intent.py    CREATE / EDIT / CONVERT / EXPORT / NO_ARTIFACT                 (chat integration)
    db.py        the V31 tables and every SQL accessor                          (jobs)
    pipeline.py  lease, heartbeat, stages, progress fan-out, requeue, sweep     (jobs)
    store.py     owner-scoped paths, atomic publish, manifest, checksums        (jobs)
    render/      html/pdf/docx/pptx/xlsx/charts + theme + validate + preview    (renderers)
    api.py       the HTTP surface                                               (chat integration)
    ../engines/artifact.py   the chat engine                                    (chat integration)

Everything the model produces is DATA: it is parsed into `ArtifactSpec`,
validated, and rendered by code. The model never emits a byte of a file, a
filename, an ID, a URL, or a completion state.
"""
