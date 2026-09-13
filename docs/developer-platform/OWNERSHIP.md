# Ownership matrix — developer platform programme

Written before any parallel modification, as the rule that keeps concurrent
workstreams out of each other's files. A wave may only write the paths its team
owns; anything else goes through the integration lead (this session).

## Workstreams outside this programme

| workstream | branch / worktree | files it owns | overlap with us |
|---|---|---|---|
| vLLM availability | `feat/vllm-availability-2026-09-12` in `../personal-LLM-Chabot-ha` — **landed on `main` (b047cc1)**, nothing outstanding | `monitoring/engine-controller/**`, `orchestrator/app/{continuity,admission,breaker,engine_state}.py`, `scripts/cluster-*`, `docs/availability/**` | we CALL its admission/breaker/continuity code, we do not edit it |
| Artifact Studio 2 | merged (`b047cc1`) | `orchestrator/app/artifacts/**`, `frontend/components/artifacts/**` | none; the public API does not expose artifacts |

Rule: this programme starts from `origin/dev`, integrates whatever has landed
there before it lands, and never edits a file another live workstream is
holding. If that becomes necessary, it is an integration review, not an edit.

## Teams and the paths each owns

| team | owns (writes) | reads only |
|---|---|---|
| **Database** | `orchestrator/app/db.py` (the V34 migration block and its accessors only), `orchestrator/tests/test_api_platform_db.py` | everything else |
| **API platform core** | `orchestrator/app/apiplatform/**` (new package: keys, projects, scopes, quotas, idempotency, usage), its tests | `app/db.py`, `app/config.py` |
| **Public API surface** | `orchestrator/app/publicapi/**` (new package: routers, models, errors, streaming, background), its tests | `app/llm.py`, `app/admission.py`, `app/engines/**` |
| **Model registry** | `orchestrator/app/publicapi/registry.py`, `orchestrator/app/model_capabilities.py` (additive only) | `app/llm.py`, `app/engine_state.py` |
| **Webhooks** | `orchestrator/app/apiplatform/webhooks/**`, its tests | `app/core/net.py` |
| **Orchestrator wiring** | `orchestrator/app/main.py` (mount points and capability registration only — surgical, one owner) | — |
| **Frontend console** | `frontend/app/(console)/**` or the agreed route group, `frontend/components/devplatform/**`, its tests | `frontend/components/admin/**`, `frontend/lib/**` |
| **Frontend docs** | `frontend/app/docs/**`, `frontend/content/docs/**`, its tests | the console's components |
| **Frontend BFF hardening** | `frontend/app/api/**` route handlers (security fixes only), `frontend/lib/*` where a fix requires it | pages |
| **CI/CD** | `.github/workflows/**` | everything |
| **Docs and evidence** | `docs/developer-platform/**`, `CHANGELOG.md`, `README.md` | everything |

Two teams never write the same file in the same wave. `orchestrator/app/main.py`
and `orchestrator/app/db.py` are single-owner files: exactly one wave touches
each, and its change is reviewed on its own.

## Integration order

1. Database migration (V34) lands first — everything else builds on its tables.
2. API platform core (keys, projects, quotas, idempotency) — depends on 1.
3. Public API surface + model registry + streaming — depends on 2.
4. Background responses + webhooks — depends on 3.
5. Frontend console + docs — depends on the API surface existing (typed against it).
6. BFF hardening and CI/CD — independent, can run beside any wave.
7. Integration: rebase on `origin/dev`, full suites, e2e on the isolated stack,
   adversarial security review, then `dev` and a `dev → main` pull request.
