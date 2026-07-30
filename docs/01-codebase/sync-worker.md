# Sync worker — `sync-worker/`

Salesforce → Parquet → DuckDB → LanceDB extraction service. Single container, single process,
single thread, no HTTP surface, no ports published.

**Scope**: 11 Python modules under [`sync-worker/syncworker/`](../../sync-worker/syncworker/) = 1,773 LOC,
plus [`config.yaml`](../../sync-worker/config.yaml) (852 LOC). Service total 28 files / 4,024 LOC;
tests 104 passing in 1.1 s.

| Module | LOC | Role |
|---|---|---|
| [`__init__.py`](../../sync-worker/syncworker/__init__.py) | 18 | Package docstring + stale `__all__` |
| [`config.py`](../../sync-worker/syncworker/config.py) | 87 | Env settings + `config.yaml` parse/validate |
| [`jsonlog.py`](../../sync-worker/syncworker/jsonlog.py) | 39 | One-JSON-per-line stdout logging |
| [`secrets.py`](../../sync-worker/syncworker/secrets.py) | 181 | Salesforce credential resolution (env only) |
| [`sf_auth.py`](../../sync-worker/syncworker/sf_auth.py) | 129 | RS256 JWT assertion + access-token cache |
| [`sf_client.py`](../../sync-worker/syncworker/sf_client.py) | 258 | Bulk API 2.0 + REST SOQL + describe |
| [`storage.py`](../../sync-worker/syncworker/storage.py) | 167 | Parquet write + DuckDB upsert + watermarks |
| [`rag_index.py`](../../sync-worker/syncworker/rag_index.py) | 154 | Embedding calls + LanceDB re-index |
| [`chunking.py`](../../sync-worker/syncworker/chunking.py) | 41 | Whitespace-window text chunker |
| [`main.py`](../../sync-worker/syncworker/main.py) | 305 | Sync loop, field adoption, backoff |
| [`objects.py`](../../sync-worker/syncworker/objects.py) | 394 | `python -m syncworker.objects` CLI |

## Orientation — one cycle

`main()` ([main.py:259](../../sync-worker/syncworker/main.py#L259)) builds one `TokenManager`,
one `SalesforceClient` and one `RagIndexer` **once for the process lifetime**
([main.py:269-274](../../sync-worker/syncworker/main.py#L269-L274)), then loops forever:
open DuckDB → `run_cycle` → close DuckDB → sleep `SYNC_INTERVAL_MINUTES`
([main.py:280-299](../../sync-worker/syncworker/main.py#L280-L299)).

Per object ([main.py:106](../../sync-worker/syncworker/main.py#L106)): read watermark → `describe` →
optionally adopt new fields → **Bulk API 2.0 full extract when the watermark is NULL**, else
**REST SOQL incremental on `SystemModstamp >`** → per batch: Parquet file + DuckDB delete-then-insert
upsert + LanceDB re-index → **then** write the watermark.

There is no `async` keyword anywhere in the package. Every call is blocking and sequential.

---

## `syncworker/__init__.py`

**Purpose** — Package docstring describing the pipeline; declares `__all__`.

**Public surface** — `__all__: list[str]` — [`__init__.py:9-18`](../../sync-worker/syncworker/__init__.py#L9-L18).
Lists `chunking, config, jsonlog, rag_index, secrets, sf_auth, sf_client, storage`.
**`main` and `objects` are omitted** despite being the two entrypoints
([main.py:259](../../sync-worker/syncworker/main.py#L259),
[objects.py:298](../../sync-worker/syncworker/objects.py#L298)).

**Control flow** — None. Declarative only; no imports beyond the docstring and `__all__`.

**State & side effects** — None. No DB, no filesystem, no network, no GPU, no global mutation.

**Dependencies** — Inbound: implicit, via every `from syncworker.X import …` in the test suite
(e.g. [`tests/test_chunking.py:3`](../../sync-worker/tests/test_chunking.py#L3),
[`tests/test_jwt.py:7`](../../sync-worker/tests/test_jwt.py#L7)). Outbound: none.

**Config** — None.

**Failure modes** — None raised. `__all__` drift means `from syncworker import *` does not expose the
entrypoints — cosmetic only.

**Concurrency** — n/a.

**Complexity hotspots** — None. 18 LOC, no functions.

**Findings** — None of the catalogued IDs apply; the module has no behaviour.

---

## `syncworker/config.py`

**Purpose** — Reads worker settings from environment variables and parses/validates the synced-object
list out of `config.yaml`.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `_IDENT_RE` | `re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")` | [config.py:11](../../sync-worker/syncworker/config.py#L11) |
| `Settings` | `@dataclass(frozen=True)`, 11 fields | [config.py:14-30](../../sync-worker/syncworker/config.py#L14-L30) |
| `load_settings` | `() -> Settings` | [config.py:33](../../sync-worker/syncworker/config.py#L33) |
| `ObjectConfig` | `@dataclass(frozen=True)`; `name`, `fields`, `rag_fields` | [config.py:54-58](../../sync-worker/syncworker/config.py#L54-L58) |
| `load_object_configs` | `(path: str) -> list[ObjectConfig]` | [config.py:61](../../sync-worker/syncworker/config.py#L61) |

**Control flow** — `load_object_configs`:
1. `open(path, encoding="utf-8")` + `yaml.safe_load` — [config.py:63-64](../../sync-worker/syncworker/config.py#L63-L64).
2. Reject non-dict payload / missing `objects` list — [config.py:65-66](../../sync-worker/syncworker/config.py#L65-L66).
3. Per entry read `name`, `fields`, `rag_fields` — [config.py:70-72](../../sync-worker/syncworker/config.py#L70-L72).
4. Validate object name against `_IDENT_RE` — [config.py:73-74](../../sync-worker/syncworker/config.py#L73-L74).
5. Validate every field and rag-field name — [config.py:75-77](../../sync-worker/syncworker/config.py#L75-L77).
6. Require `Id` **and** `SystemModstamp` in `fields` — [config.py:78-79](../../sync-worker/syncworker/config.py#L78-L79).
7. Require every `rag_field` ⊆ `fields` — [config.py:80-82](../../sync-worker/syncworker/config.py#L80-L82).
8. Append a frozen `ObjectConfig` — [config.py:83](../../sync-worker/syncworker/config.py#L83); reject an empty list — [config.py:85-86](../../sync-worker/syncworker/config.py#L85-L86).

**State & side effects** — One filesystem READ of `config_path`
([config.py:63](../../sync-worker/syncworker/config.py#L63)) plus the env reads below. No writes, no
network, no GPU, no global mutation.

**Dependencies** — Inbound: [main.py:19](../../sync-worker/syncworker/main.py#L19),
[`tests/test_config.py:3`](../../sync-worker/tests/test_config.py#L3),
[`tests/test_objects_cli.py:158,269`](../../sync-worker/tests/test_objects_cli.py#L158).
Outbound: `os`, `re`, `dataclasses`, `yaml`.

**Config**

| Var | `file:line` | Default |
|---|---|---|
| `SYNC_INTERVAL_MINUTES` | [config.py:36](../../sync-worker/syncworker/config.py#L36) | `30` |
| `SYNC_AUTO_FIELDS` | [config.py:37-38](../../sync-worker/syncworker/config.py#L37-L38) | `true` |
| `SYNC_MAX_FIELDS` | [config.py:39](../../sync-worker/syncworker/config.py#L39) | `80` |
| `SYNC_REPORT_NEW_OBJECTS` | [config.py:40-41](../../sync-worker/syncworker/config.py#L40-L41) | `true` |
| `PARQUET_DIR` | [config.py:42](../../sync-worker/syncworker/config.py#L42) | `/data/parquet` |
| `DUCKDB_PATH` | [config.py:43](../../sync-worker/syncworker/config.py#L43) | `/data/warehouse.duckdb` |
| `LANCEDB_DIR` | [config.py:44](../../sync-worker/syncworker/config.py#L44) | `/data/lancedb` |
| `EMBED_VIA` | [config.py:45](../../sync-worker/syncworker/config.py#L45) | `http://vllm-embed:30003/v1` |
| `EMBED_MODEL` | [config.py:46](../../sync-worker/syncworker/config.py#L46) | `Qwen/Qwen3-Embedding-0.6B` |
| `SF_API_VERSION` | [config.py:47](../../sync-worker/syncworker/config.py#L47) | `v61.0` |
| `SYNC_CONFIG_PATH` | [config.py:48-50](../../sync-worker/syncworker/config.py#L48-L50) | `<pkg>/../config.yaml` |

`SYNC_AUTO_FIELDS`, `SYNC_MAX_FIELDS` and `SYNC_REPORT_NEW_OBJECTS` are read here but are **absent
from the compose `environment:` block** ([docker-compose.yml:293-318](../../docker-compose.yml#L293-L318))
and there is no `env_file:`, so setting them in `.env` has no effect — see
[infra-docker-compose.md](infra-docker-compose.md).

**Failure modes**
- `int(os.getenv("SYNC_INTERVAL_MINUTES", "30"))` ([config.py:36](../../sync-worker/syncworker/config.py#L36))
  and `SYNC_MAX_FIELDS` ([:39](../../sync-worker/syncworker/config.py#L39)) raise a bare `ValueError`
  on a non-numeric value. `main()` calls `load_settings()` **outside** its try/except
  ([main.py:261](../../sync-worker/syncworker/main.py#L261) vs. the `try:` at
  [main.py:281](../../sync-worker/syncworker/main.py#L281)) → uncaught crash at startup.
- `open()` raises `FileNotFoundError`; `yaml.safe_load` raises `yaml.YAMLError` — neither caught.
- Nothing is swallowed; no bare `except`.
- Boolean parsing accepts anything not in `("0","false","no")` as true
  ([config.py:38,41](../../sync-worker/syncworker/config.py#L38)), so `SYNC_AUTO_FIELDS=off` silently
  means **true**.
- `sync_max_fields` is consulted only by `adopt_new_fields`
  ([main.py:91](../../sync-worker/syncworker/main.py#L91)); it never bounds the configured list itself.

**Concurrency** — Fully synchronous. No module-level mutable state (`_IDENT_RE` is immutable).
Read once per process ([main.py:262](../../sync-worker/syncworker/main.py#L262)) and never re-read.

**Complexity hotspots** — None over threshold. `load_object_configs` = 27 LOC
([config.py:61-87](../../sync-worker/syncworker/config.py#L61-L87)), cyclomatic ≈ 9.

**Findings** — `REL-02` (a config parse error kills a container that has no `restart:` policy).
Env-name drift: this worker reads `EMBED_VIA` ([config.py:45](../../sync-worker/syncworker/config.py#L45))
while the orchestrator reads `EMBED_BASE_URL` for the same endpoint
([docker-compose.yml:241](../../docker-compose.yml#L241) vs [:314](../../docker-compose.yml#L314)).

---

## `syncworker/jsonlog.py`

**Purpose** — One-JSON-object-per-line stdout logging; promotes everything passed via `extra=` into
the JSON payload.

**Public surface**
- `_STANDARD_ATTRS: frozenset` — [jsonlog.py:12-14](../../sync-worker/syncworker/jsonlog.py#L12-L14). Built at import time from a throwaway `logging.LogRecord`.
- `class JsonFormatter(logging.Formatter)` — [jsonlog.py:17](../../sync-worker/syncworker/jsonlog.py#L17); `format(self, record) -> str` — [jsonlog.py:18](../../sync-worker/syncworker/jsonlog.py#L18).
- `setup_logging(level: int = logging.INFO) -> None` — [jsonlog.py:34](../../sync-worker/syncworker/jsonlog.py#L34).

**Control flow** — `JsonFormatter.format`:
1. Base payload `ts`/`level`/`logger`/`message` — [jsonlog.py:19-25](../../sync-worker/syncworker/jsonlog.py#L19-L25); timestamp built from `time.gmtime(record.created)` plus `.NNNZ` from `record.msecs` — [:20-22](../../sync-worker/syncworker/jsonlog.py#L20-L22).
2. Copy every non-standard, non-underscore `record.__dict__` key into the payload — [jsonlog.py:26-28](../../sync-worker/syncworker/jsonlog.py#L26-L28).
3. Append `exc` when `record.exc_info` — [jsonlog.py:29-30](../../sync-worker/syncworker/jsonlog.py#L29-L30).
4. `json.dumps(payload, default=str)` — [jsonlog.py:31](../../sync-worker/syncworker/jsonlog.py#L31).

`setup_logging`: `StreamHandler(sys.stdout)` → attach `JsonFormatter` → **replace** the root logger's
handler list → set level — [jsonlog.py:35-39](../../sync-worker/syncworker/jsonlog.py#L35-L39).

**State & side effects** — **Global mutation**: `root.handlers[:] = [handler]`
([jsonlog.py:38](../../sync-worker/syncworker/jsonlog.py#L38)) destroys any pre-existing root handler,
and `root.setLevel(level)` ([:39](../../sync-worker/syncworker/jsonlog.py#L39)) mutates global logging
config. Writes to `sys.stdout` only. No DB, no network, no GPU, no env reads.

**Dependencies** — Inbound: [main.py:20](../../sync-worker/syncworker/main.py#L20), called at
[main.py:260](../../sync-worker/syncworker/main.py#L260). Not imported by any test.
Outbound: `json`, `logging`, `sys`, `time`.

**Config** — None.

**Failure modes** — `json.dumps(…, default=str)`
([jsonlog.py:31](../../sync-worker/syncworker/jsonlog.py#L31)) cannot raise on unserialisable values,
so a log call never kills the worker. Nothing swallowed, no bare `except`. **Latent risk**: every
`extra=` key is emitted verbatim, so a future `extra={"token": …}` would print a secret. No current
call site does — the live payloads are event names, object names, counts, field lists and paths
([main.py:100-102,157-159,189-192](../../sync-worker/syncworker/main.py#L100-L102);
[sf_client.py:66-73](../../sync-worker/syncworker/sf_client.py#L66-L73);
[sf_auth.py:126-127](../../sync-worker/syncworker/sf_auth.py#L126-L127);
[rag_index.py:146-152](../../sync-worker/syncworker/rag_index.py#L146-L152)).

**Concurrency** — Synchronous; `logging.StreamHandler` takes the module lock per emit.
`_STANDARD_ATTRS` is an immutable frozenset computed once at import.

**Complexity hotspots** — None. `format` = 14 LOC.

**Findings** — `OBS-01`. There is no correlation/trace id in the payload and none is accepted from a
caller, so a sync cycle cannot be joined to any orchestrator or model-server event. The timestamp is
assembled by string concatenation rather than `strftime`
([jsonlog.py:20-22](../../sync-worker/syncworker/jsonlog.py#L20-L22)).

---

## `syncworker/secrets.py`

**Purpose** — Resolves Salesforce credentials **entirely from environment variables**. AWS Secrets
Manager was removed on 2026-07-28 ([secrets.py:1-6](../../sync-worker/syncworker/secrets.py#L1-L6));
no cloud call remains and `boto3` is absent from
[`requirements.txt`](../../sync-worker/requirements.txt).

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `ENV_KEYS` | tuple — **dead, zero references** | [secrets.py:43](../../sync-worker/syncworker/secrets.py#L43) |
| `ENV_KEYS_FILE` | tuple — **dead, zero references** | [secrets.py:45](../../sync-worker/syncworker/secrets.py#L45) |
| `DEFAULT_KEY_PATH` | `"/data/sf_jwt_key.pem"` | [secrets.py:50](../../sync-worker/syncworker/secrets.py#L50) |
| `_THUMBPRINT_RE` | 40/64 hex chars | [secrets.py:53](../../sync-worker/syncworker/secrets.py#L53) |
| `_check_pem` | `(pem: bytes, source: str) -> bytes` | [secrets.py:60](../../sync-worker/syncworker/secrets.py#L60) |
| `SalesforceCredentials` | `@dataclass`, redacting `__repr__`/`__str__` | [secrets.py:82-95](../../sync-worker/syncworker/secrets.py#L82-L95) |
| `_pem_from_file` | `(path_value: str) -> bytes` | [secrets.py:102](../../sync-worker/syncworker/secrets.py#L102) |
| `credentials_from_env` | `(env: dict \| None = None) -> SalesforceCredentials \| None` | [secrets.py:118](../../sync-worker/syncworker/secrets.py#L118) |
| `fetch_sf_credentials` | `(secret_name=None, region=None) -> SalesforceCredentials` | [secrets.py:163](../../sync-worker/syncworker/secrets.py#L163) |

**Control flow** — `credentials_from_env`
([secrets.py:118-160](../../sync-worker/syncworker/secrets.py#L118-L160)):
1. `e = os.environ if env is None else env` — [:126](../../sync-worker/syncworker/secrets.py#L126).
2. Return `None` unless `SF_CLIENT_ID`, `SF_USERNAME` and `SF_LOGIN_URL` are all truthy — [:127-128](../../sync-worker/syncworker/secrets.py#L127-L128).
3. **`SF_CLIENT_SECRET` wins first**: if non-blank after `.strip()`, return credentials carrying only the secret and `private_key_pem=b""` — [:130-138](../../sync-worker/syncworker/secrets.py#L130-L138). No key material is loaded or validated.
4. Else `SF_PRIVATE_KEY_FILE` → `_pem_from_file` — [:140,142-143](../../sync-worker/syncworker/secrets.py#L140).
5. Else `SF_PRIVATE_KEY_B64` → `base64.b64decode(…, validate=True)` wrapped into a `ValueError`, then `_check_pem` — [:141,144-149](../../sync-worker/syncworker/secrets.py#L141-L149).
6. Else if `DEFAULT_KEY_PATH` exists on disk, read + `_check_pem` — [:150-151](../../sync-worker/syncworker/secrets.py#L150-L151). **This filesystem probe runs even when a caller supplies an `env` dict.**
7. Else return `None` — [:152-153](../../sync-worker/syncworker/secrets.py#L152-L153); otherwise build credentials with `login_url.rstrip("/")` — [:155-160](../../sync-worker/syncworker/secrets.py#L155-L160).

`fetch_sf_credentials` does `del secret_name, region`
([secrets.py:172](../../sync-worker/syncworker/secrets.py#L172)) — the two parameters exist only for
backwards compatibility; both live callers pass nothing
([main.py:269](../../sync-worker/syncworker/main.py#L269),
[objects.py:278](../../sync-worker/syncworker/objects.py#L278)).

**State & side effects** — Filesystem READS only: `_pem_from_file`
([secrets.py:115](../../sync-worker/syncworker/secrets.py#L115)) and the `DEFAULT_KEY_PATH` probe
([:150-151](../../sync-worker/syncworker/secrets.py#L150-L151)). **Zero network egress, zero cloud
SDK, zero DB, zero GPU, no global mutation.**

**Dependencies** — Inbound: [main.py:22,269](../../sync-worker/syncworker/main.py#L269),
[objects.py:273,278](../../sync-worker/syncworker/objects.py#L273),
[sf_auth.py:16](../../sync-worker/syncworker/sf_auth.py#L16) (type import),
[`tests/test_secrets.py:6`](../../sync-worker/tests/test_secrets.py#L6),
[`tests/test_jwt.py:87,117,138`](../../sync-worker/tests/test_jwt.py#L87).
Outbound: `base64`, `binascii`, `os`, `re`, `dataclasses`, `pathlib`.

**Config** — `SF_CLIENT_ID`, `SF_USERNAME`, `SF_LOGIN_URL`
([secrets.py:99,134-136,156-158](../../sync-worker/syncworker/secrets.py#L99));
`SF_CLIENT_SECRET` ([:130](../../sync-worker/syncworker/secrets.py#L130));
`SF_PRIVATE_KEY_FILE` ([:140](../../sync-worker/syncworker/secrets.py#L140));
`SF_PRIVATE_KEY_B64` ([:141](../../sync-worker/syncworker/secrets.py#L141)); plus the hardcoded
`DEFAULT_KEY_PATH` ([:50](../../sync-worker/syncworker/secrets.py#L50)).

**Failure modes**
- Raises `ValueError` for bad base64 ([:148](../../sync-worker/syncworker/secrets.py#L148)),
  a thumbprint supplied as a path ([:106-111](../../sync-worker/syncworker/secrets.py#L106-L111)),
  a missing key file ([:114](../../sync-worker/syncworker/secrets.py#L114)),
  non-PEM material ([:70-78](../../sync-worker/syncworker/secrets.py#L70-L78)),
  and no credentials at all ([:176-181](../../sync-worker/syncworker/secrets.py#L176-L181)).
- **Nothing is swallowed.** The one `except (binascii.Error, ValueError)`
  ([:147](../../sync-worker/syncworker/secrets.py#L147)) is narrow and re-raises `from exc`.
- The `SF_CLIENT_SECRET` short-circuit ([:130-138](../../sync-worker/syncworker/secrets.py#L130-L138))
  means a deployment setting both a secret and a key silently ignores the key — asserted deliberately
  by [`tests/test_secrets.py:179-186`](../../sync-worker/tests/test_secrets.py#L179-L186).
- `login_url` is never validated as https or well-formed; a typo becomes the JWT `aud` claim and the
  POST target unchecked ([sf_auth.py:36,76](../../sync-worker/syncworker/sf_auth.py#L36)).
- **Secret hygiene is a strength**: `__repr__`/`__str__` return the literal
  `"SalesforceCredentials(<redacted>)"` ([:92-95](../../sync-worker/syncworker/secrets.py#L92-L95)),
  tests assert it ([`tests/test_secrets.py:50-53,188-190`](../../sync-worker/tests/test_secrets.py#L50-L53)),
  and no exception message embeds a value.

**Concurrency** — Synchronous, no shared mutable state. `SalesforceCredentials` is **not frozen** and
is shared by reference with `TokenManager` ([sf_auth.py:52](../../sync-worker/syncworker/sf_auth.py#L52)).

**Complexity hotspots** — None over threshold. Largest: `credentials_from_env` = 43 LOC
([secrets.py:118-160](../../sync-worker/syncworker/secrets.py#L118-L160)), cyclomatic ≈ 8.

**Findings** — `SEC-06`: this module proves the AWS path is gone, yet
[`.env.example:7-14`](../../.env.example#L7-L14) still solicits `AWS_REGION`,
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `SF_SECRET_NAME`, and
[docker-compose.yml:327](../../docker-compose.yml#L327) still says "or AWS Secrets Manager".
`SEC-04`-adjacent: `DEFAULT_KEY_PATH = /data/sf_jwt_key.pem`
([secrets.py:50](../../sync-worker/syncworker/secrets.py#L50)) puts the RSA signing key inside the
`data` volume, which is also mounted read-write into the orchestrator
([docker-compose.yml:269](../../docker-compose.yml#L269)) — the container that executes LLM-written
SQL. Defence-in-depth risk only: the SQL engine opens DuckDB with `enable_external_access=False`
([sql.py:124-132](../../orchestrator/app/engines/sql.py#L124-L132)).
`QUAL-01`-adjacent dead symbols: `ENV_KEYS` / `ENV_KEYS_FILE`
([secrets.py:43,45](../../sync-worker/syncworker/secrets.py#L43)) have zero references anywhere
(verified by `grep -rn ENV_KEYS`), which contradicts the report-level claim that `is_safe_select` is
the only dead symbol in the monorepo.

---

## `syncworker/sf_auth.py`

**Purpose** — Builds the RS256 JWT assertion and manages the cached Salesforce access token for both
the JWT-bearer and client-credentials grants.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `JWT_VALIDITY_SECONDS` | `180` | [sf_auth.py:20](../../sync-worker/syncworker/sf_auth.py#L20) |
| `GRANT_TYPE` | `"urn:ietf:params:oauth:grant-type:jwt-bearer"` | [sf_auth.py:21](../../sync-worker/syncworker/sf_auth.py#L21) |
| `build_jwt_assertion` | `(client_id, username, login_url, private_key_pem: bytes, now: float\|None=None) -> str` | [sf_auth.py:24-39](../../sync-worker/syncworker/sf_auth.py#L24-L39) |
| `TokenManager` | class | [sf_auth.py:42](../../sync-worker/syncworker/sf_auth.py#L42) |
| `TokenManager.TOKEN_TTL_SECONDS` | `25 * 60` | [sf_auth.py:47](../../sync-worker/syncworker/sf_auth.py#L47) |
| `TokenManager.__init__` | `(creds, http: httpx.Client\|None=None)` | [sf_auth.py:49](../../sync-worker/syncworker/sf_auth.py#L49) |
| `TokenManager.get_token` | `() -> tuple[str, str]` | [sf_auth.py:58](../../sync-worker/syncworker/sf_auth.py#L58) |
| `TokenManager.invalidate` | `() -> None` | [sf_auth.py:70](../../sync-worker/syncworker/sf_auth.py#L70) |
| `TokenManager._request_token` | `() -> tuple[str, str]` | [sf_auth.py:75](../../sync-worker/syncworker/sf_auth.py#L75) |

**Control flow** — the JWT flow, end to end.

*Assertion* ([sf_auth.py:24-39](../../sync-worker/syncworker/sf_auth.py#L24-L39)):
1. `issued_at = int(time.time() if now is None else now)` — [:32](../../sync-worker/syncworker/sf_auth.py#L32). **Wall clock**, not monotonic.
2. Claims are exactly `{iss: client_id, sub: username, aud: login_url, exp: issued_at + 180}` — [:33-38](../../sync-worker/syncworker/sf_auth.py#L33-L38). **There is no `iat`, no `nbf` and no `jti`.**
3. `jwt.encode(claims, private_key_pem, algorithm="RS256")` — [:39](../../sync-worker/syncworker/sf_auth.py#L39). PyJWT hands the PEM to `cryptography`, so a malformed or encrypted key raises **here**, not in `secrets.py`.

*Token acquisition* ([sf_auth.py:75-129](../../sync-worker/syncworker/sf_auth.py#L75-L129)):
1. `url = f"{creds.login_url}/services/oauth2/token"` — [:76](../../sync-worker/syncworker/sf_auth.py#L76).
2. **If `creds.client_secret` is set** → POST `grant_type=client_credentials` + `client_id` + `client_secret` — [:78-90](../../sync-worker/syncworker/sf_auth.py#L78-L90). No JWT is built at all.
3. Else → `build_jwt_assertion(...)`, POST `grant_type=<GRANT_TYPE>` + `assertion=` — [:91-100](../../sync-worker/syncworker/sf_auth.py#L91-L100).
4. Non-200: parse `error_description` inside `except Exception: error = ""` ([:106-109](../../sync-worker/syncworker/sf_auth.py#L106-L109)), attach a "My Domain" hint ([:110-115](../../sync-worker/syncworker/sf_auth.py#L110-L115)) or a "Run As" hint ([:116-120](../../sync-worker/syncworker/sf_auth.py#L116-L120)), then `raise RuntimeError(f"…HTTP {status}{hint}")` ([:121-123](../../sync-worker/syncworker/sf_auth.py#L121-L123)). **The response body is deliberately never logged.**
5. 200: read `body["access_token"]` and `body["instance_url"].rstrip("/")` — [:124,129](../../sync-worker/syncworker/sf_auth.py#L124); log `event=sf_token_obtained` — [:125-128](../../sync-worker/syncworker/sf_auth.py#L125-L128).

*Caching* — `get_token` ([:58-68](../../sync-worker/syncworker/sf_auth.py#L58-L68)): `stale` when
`_access_token is None` or `time.monotonic() - _obtained_at > 25*60`
([:60-63](../../sync-worker/syncworker/sf_auth.py#L60-L63)); `_obtained_at` starts at `0.0`
([:56](../../sync-worker/syncworker/sf_auth.py#L56)) so the first call always refreshes. `invalidate()`
([:70-73](../../sync-worker/syncworker/sf_auth.py#L70-L73)) nulls the token but leaves `_obtained_at`
— harmless because the `is None` test dominates the `or`. Its only caller is the 401 retry at
[sf_client.py:140](../../sync-worker/syncworker/sf_client.py#L140).

**State & side effects** — **Network egress: `POST {SF_LOGIN_URL}/services/oauth2/token`**
([sf_auth.py:83,98](../../sync-worker/syncworker/sf_auth.py#L83)) — the only outbound call in this
module. Instance mutation of `_access_token`/`_instance_url`/`_obtained_at`
([:54-56,65-66,72-73](../../sync-worker/syncworker/sf_auth.py#L54-L56)). Reads wall clock
([:32](../../sync-worker/syncworker/sf_auth.py#L32)) and monotonic clock
([:62,66](../../sync-worker/syncworker/sf_auth.py#L62)). No filesystem, DB, GPU or env reads.

**Dependencies** — Inbound: [sf_client.py:25](../../sync-worker/syncworker/sf_client.py#L25) (used at
[sf_client.py:134,140](../../sync-worker/syncworker/sf_client.py#L134)),
[main.py:23,270](../../sync-worker/syncworker/main.py#L270),
[objects.py:274,278](../../sync-worker/syncworker/objects.py#L274),
[`tests/test_jwt.py:7,86,116,137`](../../sync-worker/tests/test_jwt.py#L7).
Outbound: `logging`, `time`, `httpx`, `jwt` (PyJWT), `.secrets.SalesforceCredentials`.

**Config** — No direct `os.getenv`. Everything arrives via `SalesforceCredentials`:
`SF_LOGIN_URL` → [:76](../../sync-worker/syncworker/sf_auth.py#L76); `SF_CLIENT_ID` →
[:34,87](../../sync-worker/syncworker/sf_auth.py#L34); `SF_USERNAME` →
[:35](../../sync-worker/syncworker/sf_auth.py#L35); `SF_CLIENT_SECRET` →
[:78,88](../../sync-worker/syncworker/sf_auth.py#L78); key PEM → [:39](../../sync-worker/syncworker/sf_auth.py#L39).

**Failure modes**
- **Clock skew is entirely unhandled.** `exp = wall_clock_now + 180` with no `iat`, no `nbf` and no
  skew allowance ([:32,37](../../sync-worker/syncworker/sf_auth.py#L32)). On a workstation with
  drifting time, Salesforce rejects the assertion and the operator sees only
  `RuntimeError: … HTTP 400` ([:121-123](../../sync-worker/syncworker/sf_auth.py#L121-L123)), which
  names neither the clock nor the `error_description`.
- `TOKEN_TTL_SECONDS = 25*60` ([:47](../../sync-worker/syncworker/sf_auth.py#L47)) is a guess — the
  comment at [:45-46](../../sync-worker/syncworker/sf_auth.py#L45-L46) states Salesforce returns no
  `expires_in` for this grant. A shorter org session timeout self-heals via the 401 retry
  ([sf_client.py:138-143](../../sync-worker/syncworker/sf_client.py#L138-L143)) at the cost of a round trip.
- **No retry, no backoff, no rate-limit awareness** on the token POST itself. One transient 503 from
  the login host raises `RuntimeError` and kills the whole cycle — caught only at
  [main.py:292](../../sync-worker/syncworker/main.py#L292), which then backs off 30 s → 30 min.
- **Swallowed**: `except Exception: error = ""`
  ([:106-109](../../sync-worker/syncworker/sf_auth.py#L106-L109)) discards every JSON-parse failure,
  so a proxy's HTML 502 loses all diagnostic content and only the status code survives.
- `assert self._access_token is not None …` ([:67](../../sync-worker/syncworker/sf_auth.py#L67)) is a
  control-flow `assert`, stripped by `python -O`; afterwards `get_token` could return `(None, None)`.
- `body["access_token"]` / `body["instance_url"]` ([:129](../../sync-worker/syncworker/sf_auth.py#L129))
  raise `KeyError` on an unexpected 200 body. Not caught.
- **Timeout is bounded**: the default client is `httpx.Client(timeout=30.0)`
  ([:53](../../sync-worker/syncworker/sf_auth.py#L53)) — but it is never closed.

**Concurrency** — Fully synchronous. `TokenManager` holds shared mutable state with **no lock**
([:54-56](../../sync-worker/syncworker/sf_auth.py#L54-L56)) and a read-modify-write window between the
staleness check and the stamp ([:60-66](../../sync-worker/syncworker/sf_auth.py#L60-L66)). The worker
is single-threaded, so no race is reachable today; the class is not thread-safe.

**Complexity hotspots** — `_request_token` = 55 LOC
([sf_auth.py:75-129](../../sync-worker/syncworker/sf_auth.py#L75-L129)), cyclomatic ≈ 7. Under both
thresholds.

**Findings** — `TEST-02`: [`tests/test_jwt.py`](../../sync-worker/tests/test_jwt.py) covers claim
shape, signature rejection, expiry and the client-credentials body, but **nothing covers clock skew,
the proactive TTL refresh, `invalidate()`, the JWT-bearer POST body, or the 401 retry**. No end-to-end
test ever exercises a real token → query → upsert path.

---

## `syncworker/sf_client.py`

**Purpose** — Read-only Salesforce data access: Bulk API 2.0 query jobs, REST SOQL with pagination,
`describe`, `sobjects` listing, and `Sforce-Limit-Info` parsing.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `LIMIT_INFO_HEADER` | `"Sforce-Limit-Info"` | [sf_client.py:29](../../sync-worker/syncworker/sf_client.py#L29) |
| `LIMIT_WARN_THRESHOLD` | `0.80` | [sf_client.py:30](../../sync-worker/syncworker/sf_client.py#L30) |
| `READ_ONLY_BULK_OPERATIONS` | `frozenset({"query","queryAll"})` | [sf_client.py:31](../../sync-worker/syncworker/sf_client.py#L31) |
| `parse_limit_info` | `(header_value) -> tuple[int,int] \| None` | [sf_client.py:37](../../sync-worker/syncworker/sf_client.py#L37) |
| `check_api_limits` | `(header_value, logger=log) -> float \| None` | [sf_client.py:50](../../sync-worker/syncworker/sf_client.py#L50) |
| `_validate_identifiers` | `(object_name, fields)` | [sf_client.py:78](../../sync-worker/syncworker/sf_client.py#L78) |
| `build_full_soql` | `(object_name, fields) -> str` | [sf_client.py:86](../../sync-worker/syncworker/sf_client.py#L86) |
| `build_incremental_soql` | `(object_name, fields, watermark) -> str` | [sf_client.py:91](../../sync-worker/syncworker/sf_client.py#L91) |
| `SalesforceClient.__init__` | `(token_manager, api_version="v61.0", http=None, poll_interval=5.0, bulk_page_size=10000)` | [sf_client.py:107-119](../../sync-worker/syncworker/sf_client.py#L107-L119) |
| `SalesforceClient._request` | `(method, path, *, headers=None, _retry_auth=True, **kwargs) -> httpx.Response` | [sf_client.py:123](../../sync-worker/syncworker/sf_client.py#L123) |
| `SalesforceClient.describe_field_types` | `(object_name) -> dict` | [sf_client.py:159](../../sync-worker/syncworker/sf_client.py#L159) |
| `SalesforceClient.describe_fields` | `(object_name) -> set` | [sf_client.py:173](../../sync-worker/syncworker/sf_client.py#L173) |
| `SalesforceClient.list_objects` | `() -> dict` | [sf_client.py:182](../../sync-worker/syncworker/sf_client.py#L182) |
| `SalesforceClient.bulk_query` | `(soql, operation="query") -> Iterator[list[dict]]` | [sf_client.py:193](../../sync-worker/syncworker/sf_client.py#L193) |
| `SalesforceClient.soql_query` | `(soql) -> Iterator[list[dict]]` | [sf_client.py:242](../../sync-worker/syncworker/sf_client.py#L242) |

**Control flow**

`_request` ([sf_client.py:123-157](../../sync-worker/syncworker/sf_client.py#L123-L157)):
1. Reject any method other than GET/POST — [:132-133](../../sync-worker/syncworker/sf_client.py#L132-L133).
2. `token, instance_url = self._tm.get_token()` — [:134](../../sync-worker/syncworker/sf_client.py#L134) (may trigger the token POST).
3. Build the URL: `instance_url + path` when `path` starts with `/`, else `path` verbatim — [:135](../../sync-worker/syncworker/sf_client.py#L135).
4. Merge `Authorization: Bearer …` with caller headers, then `self._http.request(...)` — [:136-137](../../sync-worker/syncworker/sf_client.py#L136-L137).
5. On 401 with `_retry_auth`: log `sf_token_refresh`, `invalidate()`, recurse once with `_retry_auth=False` — [:138-143](../../sync-worker/syncworker/sf_client.py#L138-L143). This returns **before** `check_api_limits`.
6. `check_api_limits(resp.headers.get("Sforce-Limit-Info"))` — [:144](../../sync-worker/syncworker/sf_client.py#L144).
7. `raise_for_status()`; on `HTTPStatusError` re-raise a new one embedding `resp.text[:400]` with newlines flattened — [:145-156](../../sync-worker/syncworker/sf_client.py#L145-L156), `from None`.

`bulk_query` ([sf_client.py:193-238](../../sync-worker/syncworker/sf_client.py#L193-L238)):
1. Guard `operation in {"query","queryAll"}` — [:195-196](../../sync-worker/syncworker/sf_client.py#L195-L196).
2. `POST /services/data/{v}/jobs/query` — [:198-201](../../sync-worker/syncworker/sf_client.py#L198-L201); log the job id — [:203-206](../../sync-worker/syncworker/sf_client.py#L203-L206).
3. **Poll loop** `while True` — [:208-220](../../sync-worker/syncworker/sf_client.py#L208-L220): break on `JobComplete`, `RuntimeError` on `Failed`/`Aborted`, else `time.sleep(5.0)`. **No iteration cap, no deadline, no shutdown check.**
4. **Results loop** `while True` — [:222-238](../../sync-worker/syncworker/sf_client.py#L222-L238): `GET /jobs/query/{id}/results` with `maxRecords=10000` and an optional `locator`, `Accept: text/csv`; parse the entire body with `csv.DictReader(io.StringIO(resp.text))` ([:233](../../sync-worker/syncworker/sf_client.py#L233)); yield non-empty batches; stop when `Sforce-Locator` is absent or the literal `"null"`.

`soql_query` ([sf_client.py:242-258](../../sync-worker/syncworker/sf_client.py#L242-L258)): `GET
/services/data/{v}/query?q=…`, strip the `attributes` key from each record, yield batches, follow
`nextRecordsUrl` until `done`.

`describe_field_types` ([:159-171](../../sync-worker/syncworker/sf_client.py#L159-L171)) lazily
creates `self._describe_cache` via `getattr`/`setattr`
([:161-163](../../sync-worker/syncworker/sf_client.py#L161-L163)) — hidden instance state not declared
in `__init__`.

**State & side effects** — **Network egress, all to the Salesforce instance URL**:
`POST /jobs/query` ([:199-201](../../sync-worker/syncworker/sf_client.py#L199-L201)),
`GET /jobs/query/{id}` ([:209-211](../../sync-worker/syncworker/sf_client.py#L209-L211)),
`GET /jobs/query/{id}/results` ([:227-232](../../sync-worker/syncworker/sf_client.py#L227-L232)),
`GET /query` ([:244-246](../../sync-worker/syncworker/sf_client.py#L244-L246)),
`GET <nextRecordsUrl>` ([:258](../../sync-worker/syncworker/sf_client.py#L258)),
`GET /sobjects/{name}/describe` ([:165-167](../../sync-worker/syncworker/sf_client.py#L165-L167)),
`GET /sobjects/` ([:184](../../sync-worker/syncworker/sf_client.py#L184)). Indirectly triggers the
token POST. Mutates `_describe_cache`. No DB, filesystem, GPU or env reads.

**Dependencies** — Inbound: [main.py:24](../../sync-worker/syncworker/main.py#L24) — used at
[main.py:149,153,270](../../sync-worker/syncworker/main.py#L149);
[`tests/test_limits.py:3-7`](../../sync-worker/tests/test_limits.py#L3-L7).
Outbound: `csv`, `io`, `logging`, `re`, `time`, `httpx`, `.sf_auth.TokenManager`.

**Config** — No `os.getenv`. `api_version` is injected from `SF_API_VERSION`
([config.py:47](../../sync-worker/syncworker/config.py#L47) → [main.py:270](../../sync-worker/syncworker/main.py#L270)).
`poll_interval=5.0` and `bulk_page_size=10000` are **constructor defaults only**
([:113-114](../../sync-worker/syncworker/sf_client.py#L113-L114)) — `main.py` passes neither, so both
are effectively hardcoded and unconfigurable.

**Failure modes**
- **Unbounded Bulk poll loop** ([:208-220](../../sync-worker/syncworker/sf_client.py#L208-L220)): a job
  stuck in `InProgress` spins forever at 5 s intervals and blocks every other object.
- **No retry except a single 401** ([:138-143](../../sync-worker/syncworker/sf_client.py#L138-L143)).
  5xx, `ConnectError`, `ReadTimeout` and `REQUEST_LIMIT_EXCEEDED` (403) all propagate immediately.
- **No 429 / `Retry-After` handling anywhere.** `check_api_limits`
  ([:50-75](../../sync-worker/syncworker/sf_client.py#L50-L75)) only *warns* at ≥ 80 %; it never
  throttles, sleeps or aborts — and it is skipped entirely on the 401 path.
- **`csv.DictReader`'s default field-size limit is 131,072 characters** (measured on this host).
  Salesforce Long Text Area fields cap at exactly 131,072 characters and CSV quote-escaping *adds*
  characters, so a full-size value raises `_csv.Error` at
  [:233](../../sync-worker/syncworker/sf_client.py#L233) and aborts that object's whole extract.
  Nothing raises the limit.
- **Memory**: `resp.text` for a 10,000-row CSV page is materialised, copied into `io.StringIO`, then
  again into dicts — roughly 3× peak.
- The 401 retry replays a **POST** ([:141-143](../../sync-worker/syncworker/sf_client.py#L141-L143)),
  so it can create a second Bulk job for the same SOQL. Harmless (read-only) but burns API quota.
- **Nothing is swallowed** — no bare `except`; the single `except httpx.HTTPStatusError` re-raises.
  `raise … from None` ([:156](../../sync-worker/syncworker/sf_client.py#L156)) discards the chain.
- **Timeout bounded**: `httpx.Client(timeout=120.0)` ([:117](../../sync-worker/syncworker/sf_client.py#L117)).
- **SOQL injection is blocked**: object and every field are checked against `_IDENT_RE`
  ([:78-83](../../sync-worker/syncworker/sf_client.py#L78-L83), called from
  [:87](../../sync-worker/syncworker/sf_client.py#L87) and [:95](../../sync-worker/syncworker/sf_client.py#L95))
  and the watermark against a strict datetime regex ([:96-97](../../sync-worker/syncworker/sf_client.py#L96-L97)).
  Read-only posture is enforced three times: method allow-list, bulk-operation allow-list, and the
  module docstring's endpoint inventory ([:3-8](../../sync-worker/syncworker/sf_client.py#L3-L8)).

**Concurrency** — Fully synchronous. `bulk_query` and `soql_query` are **generators**, so all network
I/O happens lazily inside the caller's `for batch in batches:` loop
([main.py:163](../../sync-worker/syncworker/main.py#L163)). Consequence: the Parquet write, the DuckDB
upsert and the GPU embedding calls all run *between* Salesforce page fetches, holding the Bulk result
locator open for the entire duration. `_describe_cache` is unlocked per-instance state.

**Complexity hotspots** — `bulk_query` = 46 LOC
([sf_client.py:193-238](../../sync-worker/syncworker/sf_client.py#L193-L238)), cyclomatic ≈ 9 (two
unbounded `while True` loops). `_request` = 35 LOC
([:123-157](../../sync-worker/syncworker/sf_client.py#L123-L157)), cyclomatic ≈ 7, one level of
recursion. Both under the 60-LOC threshold.

**Findings** — `TEST-02`: only `parse_limit_info` and `check_api_limits` are tested
([`tests/test_limits.py`](../../sync-worker/tests/test_limits.py)). `_request`, `bulk_query`,
`soql_query`, `describe_fields`, `list_objects`, `build_full_soql` and `build_incremental_soql` have
**zero** coverage. Magic numbers: `0.80`, `5.0`, `10000`, `120.0`, `resp.text[:400]`.

---

## `syncworker/storage.py`

**Purpose** — Lands each batch as a Parquet file and upserts it into a per-object DuckDB table; stores
per-object sync watermarks in `_sync_meta`.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `META_TABLE` | `"_sync_meta"` | [storage.py:23](../../sync-worker/syncworker/storage.py#L23) |
| `_IDENT_RE` | `re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")` | [storage.py:24](../../sync-worker/syncworker/storage.py#L24) |
| `_safe_ident` | `(name) -> str` | [storage.py:27](../../sync-worker/syncworker/storage.py#L27) |
| `sf_datetime_literal` | `(dt: datetime) -> str` | [storage.py:33](../../sync-worker/syncworker/storage.py#L33) |
| `normalize_records` | `(records: list[dict]) -> list[dict]` | [storage.py:38](../../sync-worker/syncworker/storage.py#L38) |
| `write_parquet_batch` | `(df, object_name, parquet_dir) -> str` | [storage.py:59](../../sync-worker/syncworker/storage.py#L59) |
| `Store.__init__` | `(db_path: str)` | [storage.py:75](../../sync-worker/syncworker/storage.py#L75) |
| `Store.close` | `()` | [storage.py:87](../../sync-worker/syncworker/storage.py#L87) |
| `Store.get_watermark` | `(object_name) -> str \| None` | [storage.py:92](../../sync-worker/syncworker/storage.py#L92) |
| `Store.set_watermark` | `(object_name, watermark) -> None` | [storage.py:99](../../sync-worker/syncworker/storage.py#L99) |
| `Store._table_exists` | `(table) -> bool` | [storage.py:110](../../sync-worker/syncworker/storage.py#L110) |
| `Store._table_columns` | `(table) -> list[str]` | [storage.py:118](../../sync-worker/syncworker/storage.py#L118) |
| `Store.upsert` | `(object_name, df) -> int` | [storage.py:124](../../sync-worker/syncworker/storage.py#L124) |

**Control flow** — `upsert`
([storage.py:124-167](../../sync-worker/syncworker/storage.py#L124-L167)):
1. `_safe_ident(object_name)`; return `0` on an empty frame; raise `ValueError` if there is no `Id` column — [:126-130](../../sync-worker/syncworker/storage.py#L126-L130).
2. `df.drop_duplicates(subset=["Id"], keep="last")` — [:133](../../sync-worker/syncworker/storage.py#L133).
3. `con.register("_staging_df", df)` — [:136](../../sync-worker/syncworker/storage.py#L136), with `finally: con.unregister(...)` — [:165-166](../../sync-worker/syncworker/storage.py#L165-L166).
4. **First time**: `BEGIN` → `CREATE TABLE "<obj>" AS SELECT * FROM _staging_df` → `COMMIT` → return — [:138-142](../../sync-worker/syncworker/storage.py#L138-L142).
5. **Otherwise**: diff `DESCRIBE "<obj>"` against `DESCRIBE SELECT * FROM _staging_df` to find new columns — [:145-147](../../sync-worker/syncworker/storage.py#L145-L147).
6. `BEGIN` → `ALTER TABLE … ADD COLUMN` per new column ([:151-154](../../sync-worker/syncworker/storage.py#L151-L154)) → `DELETE FROM "<obj>" WHERE Id IN (SELECT Id FROM _staging_df)` ([:155-157](../../sync-worker/syncworker/storage.py#L155-L157)) → `INSERT INTO "<obj>" BY NAME SELECT * FROM _staging_df` ([:158-160](../../sync-worker/syncworker/storage.py#L158-L160)) → `COMMIT` ([:161](../../sync-worker/syncworker/storage.py#L161)); on any exception `ROLLBACK` and re-raise ([:162-164](../../sync-worker/syncworker/storage.py#L162-L164)).

`set_watermark` ([:99-106](../../sync-worker/syncworker/storage.py#L99-L106)) is a parameterised
`INSERT … ON CONFLICT (object_name) DO UPDATE`, run **outside** any explicit transaction so DuckDB
autocommits it as its own statement — which is exactly what makes the crash-safety property below hold.

**State & side effects**
- **DB writes** to `DUCKDB_PATH`: `_sync_meta` DDL ([:80-85](../../sync-worker/syncworker/storage.py#L80-L85)), watermark upsert ([:100-106](../../sync-worker/syncworker/storage.py#L100-L106)), per-object `CREATE TABLE` ([:140](../../sync-worker/syncworker/storage.py#L140)), `ALTER TABLE ADD COLUMN` ([:152-154](../../sync-worker/syncworker/storage.py#L152-L154)), `DELETE` ([:155-157](../../sync-worker/syncworker/storage.py#L155-L157)), `INSERT` ([:158-160](../../sync-worker/syncworker/storage.py#L158-L160)).
- **Filesystem writes**: `os.makedirs` for the DB parent ([:77](../../sync-worker/syncworker/storage.py#L77)) and for `PARQUET_DIR/<object>/` ([:62-63](../../sync-worker/syncworker/storage.py#L62-L63)); one Parquet file per batch named `<object>_<UTCstamp>_<uuid8>.parquet` ([:64-68](../../sync-worker/syncworker/storage.py#L64-L68)).
- No network, no GPU, no env reads, no global mutation.

**Dependencies** — Inbound: [main.py:25](../../sync-worker/syncworker/main.py#L25) — used at
[main.py:115,164,166,167,285](../../sync-worker/syncworker/main.py#L164);
[`tests/test_upsert.py:3`](../../sync-worker/tests/test_upsert.py#L3),
[`tests/test_watermark.py:1`](../../sync-worker/tests/test_watermark.py#L1).
Outbound: `logging`, `os`, `re`, `uuid`, `datetime`, `duckdb`, `pandas`, `pyarrow`, `pyarrow.parquet`.

**Config** — None directly; `db_path` and `parquet_dir` are injected from
[config.py:42-43](../../sync-worker/syncworker/config.py#L42-L43).

**Failure modes**
- `_safe_ident` raises `ValueError` on a non-identifier ([:28-29](../../sync-worker/syncworker/storage.py#L28-L29))
  and is applied to `object_name` ([:61,125](../../sync-worker/syncworker/storage.py#L125)) and to each
  drifted column name ([:153](../../sync-worker/syncworker/storage.py#L153)); the table name
  interpolated at [:140,156,159](../../sync-worker/syncworker/storage.py#L140) is the already-validated
  identifier, and `config.py` validates first, so no injection path exists via config.
- `upsert` `ROLLBACK`s and re-raises ([:162-164](../../sync-worker/syncworker/storage.py#L162-L164)).
  **No bare `except` anywhere in this file.**
- **`log` is defined at [:21](../../sync-worker/syncworker/storage.py#L21) and never used** — this
  module emits no telemetry at all; a Parquet or upsert failure is only visible through
  [main.py:246-250](../../sync-worker/syncworker/main.py#L246-L250).
- `duckdb.connect(db_path)` ([:79](../../sync-worker/syncworker/storage.py#L79)) raises
  `duckdb.IOException` if another process holds the write lock; that bubbles to
  [main.py:292](../../sync-worker/syncworker/main.py#L292) and triggers backoff.
- **`normalize_records` casts every value to `str` or `None`**
  ([:38-56](../../sync-worker/syncworker/storage.py#L38-L56)), so every column created by
  `CREATE TABLE AS SELECT` ([:140](../../sync-worker/syncworker/storage.py#L140)) is `VARCHAR`.
  Measured consequence on this repo's own DuckDB: `SUM(Amount)` → `BinderException: No function
  matches … 'sum(VARCHAR)'`; `WHERE Amount > 1000` → `BinderException`; `date_trunc('month', CloseDate)`
  → `BinderException`. The LLM sees these types verbatim because
  [schema_cache.py:50-55](../../orchestrator/app/core/schema_cache.py#L50-L55) reads
  `information_schema.columns` and renders `Opportunity(Id VARCHAR, Amount VARCHAR, …)` into the SQL prompt.
- No Parquet retention or compaction; no bound on directory growth.

**Concurrency** — Synchronous. `Store` holds one DuckDB connection per instance
([:79](../../sync-worker/syncworker/storage.py#L79)), created and closed once per cycle
([main.py:285,289](../../sync-worker/syncworker/main.py#L285)). DuckDB allows one writer per file; the
orchestrator opens the same file `read_only=True`
([schema_cache.py:40-47](../../orchestrator/app/core/schema_cache.py#L40-L47),
[sql.py:124-132](../../orchestrator/app/engines/sql.py#L124-L132)), which is compatible.

**Complexity hotspots** — Largest is `upsert` = 44 LOC
([storage.py:124-167](../../sync-worker/syncworker/storage.py#L124-L167)), cyclomatic ≈ 8. Under both
thresholds.

**Findings** — `DATA-02`: the per-object table is created by `CREATE TABLE AS SELECT`
([storage.py:140](../../sync-worker/syncworker/storage.py#L140)), which carries **no PRIMARY KEY and
no index on `Id`**, so the `DELETE … WHERE Id IN (SELECT Id FROM _staging_df)` at
[storage.py:156](../../sync-worker/syncworker/storage.py#L156) full-scans the whole object table once
per 10,000-row batch, every cycle, for all 48 objects. `TEST-02`: `write_parquet_batch` is never
tested, and no test covers rollback or the resulting column types.
`_sync_meta` lives in the `main` schema, so
[schema_cache.py:47-53](../../orchestrator/app/core/schema_cache.py#L47-L53) exposes it to the
SQL-writing LLM as if it were a business table.

---

## `syncworker/rag_index.py`

**Purpose** — Chunks configured long-text fields, embeds them through the vLLM OpenAI-compatible
`/embeddings` endpoint, and replaces the affected records' rows in the LanceDB `chunks` table.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `TABLE_NAME` | `"chunks"` | [rag_index.py:24](../../sync-worker/syncworker/rag_index.py#L24) |
| `EMBED_BATCH_SIZE` | `32` | [rag_index.py:25](../../sync-worker/syncworker/rag_index.py#L25) |
| `_SF_ID_RE` | `re.compile(r"^[a-zA-Z0-9]{15,18}$")` | [rag_index.py:26](../../sync-worker/syncworker/rag_index.py#L26) |
| `OpenAIEmbedder.__init__` | `(base_url, model, http: httpx.Client\|None=None)` | [rag_index.py:36-41](../../sync-worker/syncworker/rag_index.py#L36-L41) |
| `OpenAIEmbedder.embed` | `(texts: list[str]) -> list[list[float]]` | [rag_index.py:43](../../sync-worker/syncworker/rag_index.py#L43) |
| `RagIndexer.__init__` | `(lancedb_dir, embedder)` | [rag_index.py:61](../../sync-worker/syncworker/rag_index.py#L61) |
| `RagIndexer._connect` | `()` | [rag_index.py:66](../../sync-worker/syncworker/rag_index.py#L66) |
| `RagIndexer._open_or_create_table` | `(dim: int)` | [rag_index.py:73](../../sync-worker/syncworker/rag_index.py#L73) |
| `RagIndexer._open_table_if_exists` | `()` | [rag_index.py:91](../../sync-worker/syncworker/rag_index.py#L91) |
| `RagIndexer.index_records` | `(object_name, records, rag_fields) -> int` | [rag_index.py:97](../../sync-worker/syncworker/rag_index.py#L97) |

**Control flow** — `index_records`
([rag_index.py:97-154](../../sync-worker/syncworker/rag_index.py#L97-L154)):
1. Return `0` when `rag_fields` or `records` is empty — [:101-102](../../sync-worker/syncworker/rag_index.py#L101-L102).
2. Per record: read `Id`, skip anything failing `_SF_ID_RE` ([:107-109](../../sync-worker/syncworker/rag_index.py#L107-L109)); collect `record_ids`; read `SystemModstamp`; for each non-`None` rag field, `chunk_text(str(value))` and build `{text, object, record_id, field, system_modstamp}` — [:112-125](../../sync-worker/syncworker/rag_index.py#L112-L125). **No `vector` yet and no comparison against what is already indexed.**
3. If rows exist: one `embed()` call over the entire batch ([:128](../../sync-worker/syncworker/rag_index.py#L128)), attach vectors, then `_open_or_create_table(dim=len(rows[0]["vector"]))` ([:131](../../sync-worker/syncworker/rag_index.py#L131)) — **this is where the vector dimension is discovered: from the first returned embedding, at runtime.**
4. Else `_open_table_if_exists()`; return `0` if the table does not exist — [:135-137](../../sync-worker/syncworker/rag_index.py#L135-L137).
5. **`for rid in record_ids: table.delete(f"record_id = '{rid}'")`** — [:141-142](../../sync-worker/syncworker/rag_index.py#L141-L142). One LanceDB delete **per record**.
6. `table.add(rows)` and log `event=rag_indexed` — [:144-153](../../sync-worker/syncworker/rag_index.py#L144-L153).

`OpenAIEmbedder.embed` ([:43-57](../../sync-worker/syncworker/rag_index.py#L43-L57)) slices `texts`
into windows of 32, POSTs `{model, input}` to `{base_url}/embeddings`, `raise_for_status()`, extends
from `resp.json()["data"]`, then asserts count parity or raises `RuntimeError`.

**State & side effects**
- **Network egress + the only GPU/model call in the whole service**: `POST {EMBED_VIA}/embeddings`
  ([rag_index.py:47-50](../../sync-worker/syncworker/rag_index.py#L47-L50)), i.e.
  `http://vllm-embed:30003/v1/embeddings` ([docker-compose.yml:314](../../docker-compose.yml#L314)),
  running `Qwen/Qwen3-Embedding-0.6B` on the DGX Spark.
- **Filesystem/DB writes**: the LanceDB dataset under `LANCEDB_DIR` — `create_table`
  ([:89](../../sync-worker/syncworker/rag_index.py#L89)), `delete`
  ([:142](../../sync-worker/syncworker/rag_index.py#L142)), `add`
  ([:144](../../sync-worker/syncworker/rag_index.py#L144)); `lancedb.connect`
  ([:70](../../sync-worker/syncworker/rag_index.py#L70)) creates the directory.
- Instance mutation of `self._db` ([:64,70](../../sync-worker/syncworker/rag_index.py#L64)). No env reads.

**Dependencies** — Inbound: [main.py:21](../../sync-worker/syncworker/main.py#L21), constructed at
[main.py:271-274](../../sync-worker/syncworker/main.py#L271-L274), called at
[main.py:176](../../sync-worker/syncworker/main.py#L176);
[`tests/test_embeddings.py:11`](../../sync-worker/tests/test_embeddings.py#L11).
Outbound: `logging`, `re`, `httpx`, `.chunking.chunk_text`, and **lazily** `lancedb`
([:68](../../sync-worker/syncworker/rag_index.py#L68)) and `pyarrow`
([:74](../../sync-worker/syncworker/rag_index.py#L74)).
Downstream reader: the orchestrator opens the same table at
[rag.py:43-44](../../orchestrator/app/engines/rag.py#L43-L44).

**Config** — None read directly. `base_url` ← `EMBED_VIA`, `model` ← `EMBED_MODEL`, `lancedb_dir` ←
`LANCEDB_DIR`, all injected from
[config.py:44-46](../../sync-worker/syncworker/config.py#L44-L46) via
[main.py:271-274](../../sync-worker/syncworker/main.py#L271-L274).

**Failure modes**
- **No re-embedding guard.** `index_records` never compares incoming text against what is already
  stored — no content hash, no `system_modstamp` comparison, no chunk-level dedup. Any change to *any*
  field bumps `SystemModstamp`, so the record is re-fetched and every one of its long-text chunks is
  re-embedded on the GPU.
- **One LanceDB `delete()` per record** ([:141-142](../../sync-worker/syncworker/rag_index.py#L141-L142)),
  inside the per-batch loop.
- **`dim` is honoured only at creation** ([:73-89](../../sync-worker/syncworker/rag_index.py#L73-L89)).
  Changing `EMBED_MODEL` to a different-dimension model leaves the old table in place; `table.add`
  then fails on a schema mismatch, and [main.py:174-184](../../sync-worker/syncworker/main.py#L174-L184)
  swallows that failure forever.
- **No retry, no backoff** on the embed POST. A single 503 from a reloading vLLM aborts the batch's indexing.
- **Timeout bounded but very long**: `httpx.Client(timeout=300.0)`
  ([:41](../../sync-worker/syncworker/rag_index.py#L41)) — 5 minutes *per 32-text batch*, with no
  overall deadline across an unbounded number of batches.
- Nothing is swallowed *inside* this module, but every exception it raises is swallowed one level up
  at [main.py:174-184](../../sync-worker/syncworker/main.py#L174-L184) — **and the watermark still
  advances at [main.py:188](../../sync-worker/syncworker/main.py#L188)**, so the missed chunks are
  never retried. This is the one place where the otherwise-sound crash-safety design leaks.
- **Filter injection is guarded**: `record_id` is validated against `_SF_ID_RE`
  ([:26,108](../../sync-worker/syncworker/rag_index.py#L108)) before interpolation into the LanceDB
  filter string at [:142](../../sync-worker/syncworker/rag_index.py#L142). Correct but fragile — it is
  the only thing between config-driven data and a string-built predicate.
- No upper bound on `len(rows)` or on total chunk text before `embed`
  ([:128](../../sync-worker/syncworker/rag_index.py#L128)).

**Concurrency** — Synchronous, called inline from the batch loop
([main.py:174-176](../../sync-worker/syncworker/main.py#L174-L176)), so **GPU embedding blocks
Salesforce pagination**: the Bulk result locator and the REST `nextRecordsUrl` stay open while
thousands of embeddings are computed. `self._db` is per-instance lazy state; the single `RagIndexer`
built at [main.py:271](../../sync-worker/syncworker/main.py#L271) is shared across all 48 objects and
every cycle for the process lifetime. No locks; single-threaded.

**Complexity hotspots** — `index_records` = 58 LOC
([rag_index.py:97-154](../../sync-worker/syncworker/rag_index.py#L97-L154)), cyclomatic ≈ 12 — **over
the complexity threshold**, just under the LOC threshold.

**Findings** — `TEST-02`: [`tests/test_embeddings.py`](../../sync-worker/tests/test_embeddings.py)
covers `OpenAIEmbedder` thoroughly via `httpx.MockTransport` but **`RagIndexer` is entirely untested** —
no test exists for `index_records`, the per-record delete loop, the dimension lock, or delete-then-add
ordering. `PERF-04`-adjacent: the `httpx.Client` created at
[rag_index.py:41](../../sync-worker/syncworker/rag_index.py#L41) is never closed, though unlike the
orchestrator's case it is created once, not per call. No vector index is ever created on the `chunks`
table ([:79-89](../../sync-worker/syncworker/rag_index.py#L79-L89) defines a schema only), so LanceDB
search is a brute-force scan.

---

## `syncworker/chunking.py`

**Purpose** — Splits long text into overlapping windows for embedding. "Tokens" are
whitespace-separated words, not model tokens.

**Public surface**
- `DEFAULT_CHUNK_TOKENS = 800` — [chunking.py:10](../../sync-worker/syncworker/chunking.py#L10).
- `DEFAULT_OVERLAP_TOKENS = 100` — [chunking.py:11](../../sync-worker/syncworker/chunking.py#L11).
- `chunk_text(text, chunk_tokens=800, overlap_tokens=100) -> list[str]` — [chunking.py:14](../../sync-worker/syncworker/chunking.py#L14).

**Control flow**
1. Reject `chunk_tokens <= 0` — [chunking.py:25-26](../../sync-worker/syncworker/chunking.py#L25-L26).
2. Reject `overlap_tokens < 0` or `>= chunk_tokens` — [chunking.py:27-28](../../sync-worker/syncworker/chunking.py#L27-L28).
3. `tokens = text.split()` — [chunking.py:30](../../sync-worker/syncworker/chunking.py#L30); `[]` for empty/whitespace — [:31-32](../../sync-worker/syncworker/chunking.py#L31-L32).
4. `step = chunk_tokens - overlap_tokens` (700 by default) — [chunking.py:34](../../sync-worker/syncworker/chunking.py#L34).
5. Slide `range(0, len(tokens), step)`, join each window, break once `start + chunk_tokens >= len(tokens)` — [chunking.py:36-40](../../sync-worker/syncworker/chunking.py#L36-L40).

**State & side effects** — Pure function. No I/O of any kind.

**Dependencies** — Inbound: [rag_index.py:20](../../sync-worker/syncworker/rag_index.py#L20), called at
[rag_index.py:116](../../sync-worker/syncworker/rag_index.py#L116);
[`tests/test_chunking.py:3`](../../sync-worker/tests/test_chunking.py#L3). Outbound: none.

**Config** — None. `chunk_tokens`/`overlap_tokens` are **not** exposed as env vars anywhere and the
sole call site ([rag_index.py:116](../../sync-worker/syncworker/rag_index.py#L116)) passes neither, so
`800`/`100` are the only values ever used.

**Failure modes**
- Raises `ValueError` for invalid sizes ([:26,28](../../sync-worker/syncworker/chunking.py#L26)).
  Nothing swallowed, no bare `except`.
- **`text.split()` is whitespace-only** ([:30](../../sync-worker/syncworker/chunking.py#L30)). Text
  with little or no whitespace collapses to very few "tokens", so the chunk cap never triggers.
  Minified JSON is the pathological case — and
  [`config.yaml`](../../sync-worker/config.yaml) deliberately indexes 12 JSON-bearing fields as
  `rag_fields` ([config.yaml:547,786-787,794,815-816,827,834,841,852](../../sync-worker/config.yaml#L547)).
  A 100 KB space-free JSON blob is **one token → one 100 KB chunk** → one embedding request far past
  the model's 4096-token window ([docker-compose.yml:194](../../docker-compose.yml#L194)).
- Conversely, whitespace-heavy text (indented JSON, Rich Text Area HTML) explodes counts: 800
  whitespace-words ≠ 800 model tokens, so a "chunk" can still overflow the embedder.
- No cap on the number of chunks produced from one field.

**Concurrency** — Pure and stateless; safe anywhere.

**Complexity hotspots** — None. `chunk_text` = 28 LOC, cyclomatic ≈ 6.

**Findings** — `TEST-02`: [`tests/test_chunking.py`](../../sync-worker/tests/test_chunking.py) is the
strongest file in the service (boundaries, overlap, lossless reassembly) but asserts **nothing** about
non-whitespace-separated input, chunk byte size, or the embedder's context window — the exact gap the
JSON `rag_fields` fall through.

---

## `syncworker/main.py`

**Purpose** — The sync loop entrypoint: signal handling, per-object full/incremental sync, field
adoption, new-object reporting, exponential backoff.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `INITIAL_BACKOFF_SECONDS` | `30.0` | [main.py:29](../../sync-worker/syncworker/main.py#L29) |
| `MAX_BACKOFF_SECONDS` | `30 * 60.0` | [main.py:30](../../sync-worker/syncworker/main.py#L30) |
| `_StopFlag` | class; `install()` [:37](../../sync-worker/syncworker/main.py#L37), `_handle()` [:41](../../sync-worker/syncworker/main.py#L41), `sleep()` [:45](../../sync-worker/syncworker/main.py#L45) | [main.py:33](../../sync-worker/syncworker/main.py#L33) |
| `COMPOUND_TYPES` | `("address","location")` | [main.py:54](../../sync-worker/syncworker/main.py#L54) |
| `LONG_TEXT_TYPES` | `("textarea","richtextarea")` | [main.py:57](../../sync-worker/syncworker/main.py#L57) |
| `_NOISE_SUFFIXES` | `("__History","__Share","__Feed")` | [main.py:60](../../sync-worker/syncworker/main.py#L60) |
| `adopt_new_fields` | `(object_name, fields, rag_fields, client, settings) -> tuple[list, list]` | [main.py:63](../../sync-worker/syncworker/main.py#L63) |
| `sync_object` | `(obj, client, store, indexer, settings) -> int` | [main.py:106](../../sync-worker/syncworker/main.py#L106) |
| `report_new_objects` | `(objects, client) -> list[str]` | [main.py:197](../../sync-worker/syncworker/main.py#L197) |
| `run_cycle` | `(objects, client, store, indexer, settings) -> None` | [main.py:227](../../sync-worker/syncworker/main.py#L227) |
| `main` | `() -> None` | [main.py:259](../../sync-worker/syncworker/main.py#L259) |

**Control flow** — `main()` ([main.py:259-301](../../sync-worker/syncworker/main.py#L259-L301)):
1. `setup_logging()` — [:260](../../sync-worker/syncworker/main.py#L260).
2. `load_settings()` [:261](../../sync-worker/syncworker/main.py#L261) and `load_object_configs()` [:262](../../sync-worker/syncworker/main.py#L262) — **both outside the try/except** that starts at [:281](../../sync-worker/syncworker/main.py#L281).
3. Log `event=startup` — [:263-267](../../sync-worker/syncworker/main.py#L263-L267).
4. `fetch_sf_credentials()` [:269](../../sync-worker/syncworker/main.py#L269) → `SalesforceClient(TokenManager(creds), api_version)` [:270](../../sync-worker/syncworker/main.py#L270) → `RagIndexer(lancedb_dir, OpenAIEmbedder(...))` [:271-274](../../sync-worker/syncworker/main.py#L271-L274). **All three built once for the process lifetime.**
5. `_StopFlag().install()` — [:276-277](../../sync-worker/syncworker/main.py#L276-L277) (SIGTERM + SIGINT).
6. Loop `while not flag.stop` — [:280](../../sync-worker/syncworker/main.py#L280): fresh `Store` per cycle [:285](../../sync-worker/syncworker/main.py#L285) → `run_cycle` in `try/finally: store.close()` [:286-289](../../sync-worker/syncworker/main.py#L286-L289) → reset backoff, sleep `interval*60` [:290-291](../../sync-worker/syncworker/main.py#L290-L291); on `except Exception` log `event=cycle_error` with traceback then sleep `backoff`, `backoff = min(backoff*2, 1800)` [:292-299](../../sync-worker/syncworker/main.py#L292-L299).

`sync_object` ([main.py:106-194](../../sync-worker/syncworker/main.py#L106-L194)) — the core path:
1. `watermark = store.get_watermark(obj.name)` — [:114](../../sync-worker/syncworker/main.py#L114).
2. **`cycle_start = sf_datetime_literal(datetime.now(timezone.utc))`** — [:115](../../sync-worker/syncworker/main.py#L115). The **local worker clock**, captured *before* any network call.
3. `client.describe_fields(obj.name)` in a try ([:120-121](../../sync-worker/syncworker/main.py#L120-L121)); on failure log `event=describe_failed` and keep the configured fields ([:122-126](../../sync-worker/syncworker/main.py#L122-L126)).
4. Otherwise drop fields the integration user cannot see, from both `fields` and `rag_fields`, logging `event=fields_skipped` — [:128-136](../../sync-worker/syncworker/main.py#L128-L136).
5. If `sync_auto_fields`, `adopt_new_fields(...)` — [:142-145](../../sync-worker/syncworker/main.py#L142-L145).
6. **Mode selection** — [:147-154](../../sync-worker/syncworker/main.py#L147-L154): `watermark is None` → `bulk_query(build_full_soql(...))`; else → `soql_query(build_incremental_soql(..., watermark))`.
7. `for batch in batches:` — [:163-184](../../sync-worker/syncworker/main.py#L163-L184): `normalize_records` → `pd.DataFrame` → `write_parquet_batch` ([:166](../../sync-worker/syncworker/main.py#L166)) → **`store.upsert` ([:167](../../sync-worker/syncworker/main.py#L167))** → log `event=batch_stored` → `indexer.index_records(...)` wrapped in `try/except Exception: log event=rag_index_error` ([:174-184](../../sync-worker/syncworker/main.py#L174-L184)).
8. **`store.set_watermark(obj.name, cycle_start)` — [:188](../../sync-worker/syncworker/main.py#L188)**, reached only after the whole batch loop completes without raising.

**State & side effects**
- **DB writes** via `Store`: [main.py:167,188,285](../../sync-worker/syncworker/main.py#L167).
- **Filesystem writes**: Parquet under `PARQUET_DIR` [:166](../../sync-worker/syncworker/main.py#L166); LanceDB under `LANCEDB_DIR` [:176](../../sync-worker/syncworker/main.py#L176).
- **Network egress**: Salesforce [:121,143,149,153,209](../../sync-worker/syncworker/main.py#L121); the vLLM embedding endpoint [:176](../../sync-worker/syncworker/main.py#L176).
- **GPU/model call**: the embedding request behind [:176](../../sync-worker/syncworker/main.py#L176).
- **Global mutation**: `setup_logging()` replaces root handlers ([:260](../../sync-worker/syncworker/main.py#L260) → [jsonlog.py:38](../../sync-worker/syncworker/jsonlog.py#L38)); `signal.signal` installs process-wide handlers ([:38-39](../../sync-worker/syncworker/main.py#L38-L39)).
- **Env reads**: all indirect, via `load_settings()` and `fetch_sf_credentials()`.

**Dependencies** — Inbound: [`sync-worker/Dockerfile:34`](../../sync-worker/Dockerfile#L34)
(`CMD ["python","-m","syncworker.main"]`),
[`tests/test_discovery.py:11`](../../sync-worker/tests/test_discovery.py#L11). Nothing else in the
repo imports it. Outbound: `logging`, `signal`, `time`, `datetime`, `pandas`, and the six sibling
modules ([:19-25](../../sync-worker/syncworker/main.py#L19-L25)).

**Config** — No direct `os.getenv`. Consumes `Settings` fields: `config_path` [:262](../../sync-worker/syncworker/main.py#L262),
`sync_interval_minutes` [:266,291](../../sync-worker/syncworker/main.py#L291), `sf_api_version`
[:270](../../sync-worker/syncworker/main.py#L270), `lancedb_dir`/`embed_via`/`embed_model`
[:272-273](../../sync-worker/syncworker/main.py#L272-L273), `duckdb_path`
[:285](../../sync-worker/syncworker/main.py#L285), `parquet_dir`
[:166](../../sync-worker/syncworker/main.py#L166), `sync_auto_fields`
[:142](../../sync-worker/syncworker/main.py#L142), `sync_max_fields`
[:91](../../sync-worker/syncworker/main.py#L91), `sync_report_new_objects`
[:235](../../sync-worker/syncworker/main.py#L235).

**Failure modes**
- **Five swallowed handlers, by design**:

  | Location | Effect | Logged? |
  |---|---|---|
  | [main.py:81-82](../../sync-worker/syncworker/main.py#L81-L82) | describe failure during adoption → return inputs unchanged | **nothing at all** |
  | [main.py:122-126](../../sync-worker/syncworker/main.py#L122-L126) | describe failure → `event=describe_failed` | warning, no traceback |
  | [main.py:177-184](../../sync-worker/syncworker/main.py#L177-L184) | RAG index failure → `event=rag_index_error`; **the watermark still advances at [:188](../../sync-worker/syncworker/main.py#L188)** | error + traceback |
  | [main.py:210-211](../../sync-worker/syncworker/main.py#L210-L211) | object listing failure → `return []` | **nothing at all** |
  | [main.py:244-250](../../sync-worker/syncworker/main.py#L244-L250) | one object's failure does not stop the cycle | error + traceback |
  | [main.py:292-299](../../sync-worker/syncworker/main.py#L292-L299) | whole-cycle failure → exponential backoff 30 s → 30 min | error + traceback |

- **`load_settings()`/`load_object_configs()` sit outside the try** ([:261-262](../../sync-worker/syncworker/main.py#L261-L262)),
  so a bad `SYNC_INTERVAL_MINUTES` or malformed `config.yaml` crashes the process at startup — and with
  no `restart:` policy the container simply stays dead.
- **No shutdown responsiveness inside a cycle**: `flag.stop` is read only by `_StopFlag.sleep`
  ([:48](../../sync-worker/syncworker/main.py#L48)) and the outer `while`
  ([:280](../../sync-worker/syncworker/main.py#L280)). Neither `run_cycle`, `sync_object`, the Bulk
  poll loop nor the embedding loop checks it, so `docker stop` during a full extract waits out the
  grace period and then SIGKILLs.
- **No timeout on a cycle as a whole**; one object stuck in `bulk_query` blocks all others
  ([:239-243](../../sync-worker/syncworker/main.py#L239-L243) is a plain sequential loop).
- **No health/liveness signal** — no `HEALTHCHECK` in [`sync-worker/Dockerfile`](../../sync-worker/Dockerfile)
  and none in [docker-compose.yml:291-331](../../docker-compose.yml#L291-L331).
- Interval drift: the sleep at [:291](../../sync-worker/syncworker/main.py#L291) starts *after* the
  cycle, so the effective period is `interval + cycle_duration`.
- Watermark provenance: `cycle_start` is the **worker's** clock
  ([:115](../../sync-worker/syncworker/main.py#L115)), not Salesforce's, so worker clock skew ahead of
  the org silently skips records modified in the skew window.
- Salesforce **deletes are never propagated** — the incremental query is `SystemModstamp >` only
  ([:147-154](../../sync-worker/syncworker/main.py#L147-L154)); a deleted record stays in DuckDB and
  LanceDB forever.

**Concurrency** — Single-threaded, fully synchronous; **no `async` keyword exists anywhere in the
package**. Shared mutable state persisting across cycles: `TokenManager._access_token/_instance_url/_obtained_at`,
`SalesforceClient._describe_cache`, `RagIndexer._db` — all created at
[:270-274](../../sync-worker/syncworker/main.py#L270-L274) and never reset (so an adopted field is not
picked up until the container restarts). Signal handlers mutate `_StopFlag.stop`
([:41-43](../../sync-worker/syncworker/main.py#L41-L43)) — safe, one boolean.

**Complexity hotspots**
- **`sync_object` = 89 LOC** — [main.py:106-194](../../sync-worker/syncworker/main.py#L106-L194),
  cyclomatic ≈ 13. **Over both thresholds** and the most consequential function in the service.
- `adopt_new_fields` = 41 LOC ([main.py:63-103](../../sync-worker/syncworker/main.py#L63-L103)), cyclomatic ≈ 9.
- `main` = 47 LOC ([main.py:259-301](../../sync-worker/syncworker/main.py#L259-L301)), cyclomatic ≈ 5.

**Findings** — `REL-02` (no `restart:` and no healthcheck on this service, unlike every other:
[docker-compose.yml:291-331](../../docker-compose.yml#L291-L331) vs
[:89,134,172,203,343](../../docker-compose.yml#L89)), `OBS-01`, `TEST-02` (`sync_object` — 89 LOC, the
linchpin — has **zero** direct test coverage;
[`tests/test_watermark.py`](../../sync-worker/tests/test_watermark.py) covers only the `Store`
round-trip, never the ordering guarantee below). `COMPOUND_TYPES`/`LONG_TEXT_TYPES` are duplicated
verbatim in [objects.py:161,164](../../sync-worker/syncworker/objects.py#L161) — two sources of truth
for the same Salesforce type policy.

---

## `syncworker/objects.py`

**Purpose** — CLI (`python -m syncworker.objects`) to list/add/remove synced objects and to import an
org "Objects, Fields" spreadsheet, intersecting it with what the integration user can actually read.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `REQUIRED_FIELDS` | `("Id","SystemModstamp")` | [objects.py:41](../../sync-worker/syncworker/objects.py#L41) |
| `DEFAULT_CONFIG` | `…/sync-worker/config.yaml` | [objects.py:43](../../sync-worker/syncworker/objects.py#L43) |
| `ConfigError` | `Exception` subclass | [objects.py:46](../../sync-worker/syncworker/objects.py#L46) |
| `_split_header` | `(text) -> tuple[str, dict]` | [objects.py:50](../../sync-worker/syncworker/objects.py#L50) |
| `load` | `(path=DEFAULT_CONFIG) -> tuple[str, List[dict]]` | [objects.py:61](../../sync-worker/syncworker/objects.py#L61) |
| `_validate` | `(entry: dict) -> None` | [objects.py:71](../../sync-worker/syncworker/objects.py#L71) |
| `upsert_object` | `(objects, name, fields, rag_fields=(), replace=True) -> List[dict]` | [objects.py:111](../../sync-worker/syncworker/objects.py#L111) |
| `remove_object` | `(objects, name) -> List[dict]` | [objects.py:133](../../sync-worker/syncworker/objects.py#L133) |
| `dump` / `save` | `(header, objects[, path])` | [objects.py:142](../../sync-worker/syncworker/objects.py#L142) / [:149](../../sync-worker/syncworker/objects.py#L149) |
| `MAX_FIELDS_PER_OBJECT` | `60` | [objects.py:168](../../sync-worker/syncworker/objects.py#L168) |
| `parse_sheet` | `(path: Path) -> Dict[str, List[str]]` | [objects.py:171](../../sync-worker/syncworker/objects.py#L171) |
| `plan_from_sheet` | `(sheet, describe, existing=None) -> tuple[List[dict], List[str]]` | [objects.py:194](../../sync-worker/syncworker/objects.py#L194) |
| `_live_describe` | `()` | [objects.py:271](../../sync-worker/syncworker/objects.py#L271) |
| `main` | `(argv=None) -> int` | [objects.py:298](../../sync-worker/syncworker/objects.py#L298) |

**Control flow** — `main` ([objects.py:298-390](../../sync-worker/syncworker/objects.py#L298-L390)):
1. Build the parser; `--config` is registered on both a parent and the root parser
   ([:305-307](../../sync-worker/syncworker/objects.py#L305-L307)) so it works before or after the
   subcommand; subparsers are `required=True` ([:308](../../sync-worker/syncworker/objects.py#L308)).
2. Subcommands `list` [:310](../../sync-worker/syncworker/objects.py#L310), `add`
   [:312-318](../../sync-worker/syncworker/objects.py#L312-L318), `add-fields`
   [:320-324](../../sync-worker/syncworker/objects.py#L320-L324), `remove`
   [:326-327](../../sync-worker/syncworker/objects.py#L326-L327), `import-sheet`
   [:330-336](../../sync-worker/syncworker/objects.py#L330-L336).
3. `header, objects = load(args.config)` inside a `try` — [:340-341](../../sync-worker/syncworker/objects.py#L340-L341).
4. `import-sheet` → `parse_sheet` → `plan_from_sheet(sheet, _live_describe(), objects)`
   ([:351-352](../../sync-worker/syncworker/objects.py#L351-L352)) → print counts/notes → honour
   `--dry-run` → **`objects = entries`** ([:364](../../sync-worker/syncworker/objects.py#L364)), a
   wholesale replacement.
5. `save(header, objects, args.config)` — [:380](../../sync-worker/syncworker/objects.py#L380) — then
   print the restart instructions ([:381-386](../../sync-worker/syncworker/objects.py#L381-L386)).
6. `except ConfigError` → stderr, exit 2 — [:388-390](../../sync-worker/syncworker/objects.py#L388-L390).

`_live_describe` ([:271-286](../../sync-worker/syncworker/objects.py#L271-L286)) lazily imports
`fetch_sf_credentials`/`TokenManager`, takes **one** token
([:278-279](../../sync-worker/syncworker/objects.py#L278-L279)), pins it into an
`httpx.Client(timeout=60.0)` header ([:280](../../sync-worker/syncworker/objects.py#L280)), and returns
a closure doing `GET {instance}/services/data/v61.0/sobjects/{name}/describe`, returning
`resp.json() if resp.status_code == 200 else None`
([:282-284](../../sync-worker/syncworker/objects.py#L282-L284)).

**State & side effects**
- **Filesystem writes**: `path.write_text(...)` in `save` — [objects.py:152](../../sync-worker/syncworker/objects.py#L152), defaulting to [`sync-worker/config.yaml`](../../sync-worker/config.yaml).
- **Filesystem reads**: [:64](../../sync-worker/syncworker/objects.py#L64) (`load`), [:180](../../sync-worker/syncworker/objects.py#L180) (`parse_sheet`).
- **Network egress**: `GET …/sobjects/{name}/describe` [:283](../../sync-worker/syncworker/objects.py#L283) plus the token POST triggered at [:279](../../sync-worker/syncworker/objects.py#L279).
- stdout/stderr writes. No DB, no GPU, no global mutation.

**Dependencies** — Inbound: documented in [`README.md:217-231`](../../README.md#L217-L231), named by
the orchestrator's SQL error message at
[sql.py:311](../../orchestrator/app/engines/sql.py#L311), referenced in log text at
[main.py:206,219-220](../../sync-worker/syncworker/main.py#L206),
[`tests/test_objects_cli.py:11`](../../sync-worker/tests/test_objects_cli.py#L11). Outbound:
`argparse`, `csv`, `collections`, `re`, `sys`, `pathlib`, `typing`, `yaml`, and lazily `.secrets`,
`.sf_auth`, `httpx`.

**Config** — **No env vars read directly.** `_live_describe` inherits everything through
`fetch_sf_credentials()`. The API version is **hardcoded `v61.0`**
([objects.py:283](../../sync-worker/syncworker/objects.py#L283)), ignoring `SF_API_VERSION`
([config.py:47](../../sync-worker/syncworker/config.py#L47)).

**Failure modes**
- **`_live_describe` conflates every non-200 with "object not readable"**
  ([:284](../../sync-worker/syncworker/objects.py#L284)). A 401, a 403 `REQUEST_LIMIT_EXCEEDED`, a 429
  or a 500 all become `None`, which `plan_from_sheet`
  ([:227-229](../../sync-worker/syncworker/objects.py#L227-L229)) turns into "not readable by this
  user — skipped", and [:364](../../sync-worker/syncworker/objects.py#L364) then writes a config with
  that object **deleted**.
- `save` is **non-atomic** ([:152](../../sync-worker/syncworker/objects.py#L152)) — a crash or a full
  disk mid-write truncates `config.yaml`, after which `load_object_configs`
  ([config.py:65-66](../../sync-worker/syncworker/config.py#L65-L66)) refuses to start the worker. No
  backup is taken.
- `except ConfigError` ([:388](../../sync-worker/syncworker/objects.py#L388)) catches only that type.
  `yaml.YAMLError`, `OSError`, `httpx.ConnectError`, `KeyError` from
  [:213](../../sync-worker/syncworker/objects.py#L213) and `RuntimeError` from the token request all
  escape as tracebacks with exit code 1.
- The token is fetched once and pinned ([:279-280](../../sync-worker/syncworker/objects.py#L279-L280));
  a long `import-sheet` over hundreds of objects starts 401-ing with no refresh path. No retry, no
  rate-limit handling, one serial HTTP round trip per object.
- **[:385-386](../../sync-worker/syncworker/objects.py#L385-L386) prints a false statement**: *"The
  next cycle does a FULL extract for changed objects, then returns to incremental syncs."* Nothing here
  or in `storage.py` clears the watermark, and `sync_object` does a full extract only when
  `get_watermark()` returns `None` ([main.py:147](../../sync-worker/syncworker/main.py#L147)). A newly
  adopted field is therefore **NULL for every pre-existing row** and nothing backfills it — the shape
  is documented by [`tests/test_upsert.py:86-94`](../../sync-worker/tests/test_upsert.py#L86-L94).

**Concurrency** — Synchronous CLI, single process. Not safe to run concurrently with itself or with a
manual `config.yaml` edit: `load` → mutate → `save`
([:341](../../sync-worker/syncworker/objects.py#L341) … [:380](../../sync-worker/syncworker/objects.py#L380))
is an unlocked read-modify-write.

**Complexity hotspots**
- **`main` = 93 LOC** — [objects.py:298-390](../../sync-worker/syncworker/objects.py#L298-L390),
  cyclomatic ≈ 15. The largest function in the service. Over both thresholds.
- **`plan_from_sheet` = 75 LOC** — [objects.py:194-268](../../sync-worker/syncworker/objects.py#L194-L268),
  cyclomatic ≈ 16. Over both thresholds.

**Findings** — `TEST-02`: [`tests/test_objects_cli.py`](../../sync-worker/tests/test_objects_cli.py)
(333 LOC, 27 tests) covers the CLI's pure logic well, but the network-facing half is untested — the
only `_live_describe` test substitutes `lambda n: None` for *every* object
([:288-298](../../sync-worker/tests/test_objects_cli.py#L288-L298)), which is indistinguishable from a
total outage and never exercises the partial-failure case above.
`MAX_FIELDS_PER_OBJECT = 60` ([:168](../../sync-worker/syncworker/objects.py#L168)) is inconsistent
with `SYNC_MAX_FIELDS = 80` ([config.py:39](../../sync-worker/syncworker/config.py#L39)), so an
imported object is capped at 62 fields while runtime adoption immediately widens it back toward 80.

---

## `config.yaml` — the extraction contract

**Purpose** — Single source of truth for which Salesforce objects/fields are extracted and which
long-text fields are chunked and embedded into LanceDB. 852 LOC.

**Public surface** — the file's structure:
- Lines [1-29](../../sync-worker/config.yaml#L1-L29): comment header only — per-object key documentation
  ([:3-10](../../sync-worker/config.yaml#L3-L10)) and a commented `Project__c` template
  ([:12-28](../../sync-worker/config.yaml#L12-L28)). This block is what
  [objects.py:50-58](../../sync-worker/syncworker/objects.py#L50-L58) preserves verbatim across CLI edits.
- Line [30](../../sync-worker/config.yaml#L30): the single top-level key `objects:`.
- Lines [31-852](../../sync-worker/config.yaml#L31-L852): a flat YAML sequence; each entry has `name`
  (str), `fields` (list[str]) and optionally `rag_fields` (list[str]).

**Measured totals** (`yaml.safe_load` over the file): **48 objects · 631 fields · 61 `rag_fields`
across 34 objects**; 14 objects declare no `rag_fields` key.

| Group | Objects | Notes |
|---|---|---|
| Core CRM | `Account` [:31](../../sync-worker/config.yaml#L31), `Contact` [:180](../../sync-worker/config.yaml#L180), `Lead` [:322](../../sync-worker/config.yaml#L322), `Opportunity` [:389](../../sync-worker/config.yaml#L389), `Case` [:124](../../sync-worker/config.yaml#L124), `User` [:647](../../sync-worker/config.yaml#L647), `Campaign` [:92](../../sync-worker/config.yaml#L92), `Contract` [:201](../../sync-worker/config.yaml#L201), `Quote`/`QuoteLineItem` [:487](../../sync-worker/config.yaml#L487)/[:512](../../sync-worker/config.yaml#L512), `Order`/`OrderItem` [:428](../../sync-worker/config.yaml#L428)/[:441](../../sync-worker/config.yaml#L441), `Product2` [:473](../../sync-worker/config.yaml#L473), `OpportunityLineItem` [:419](../../sync-worker/config.yaml#L419), `AccountContactRelation` [:54](../../sync-worker/config.yaml#L54) | `Contact` and `Lead` carry **no** `rag_fields` — their descriptions are not semantically searchable |
| Service / ITSM | `Incident` [:227](../../sync-worker/config.yaml#L227), `Problem` [:449](../../sync-worker/config.yaml#L449), `ChangeRequest` [:151](../../sync-worker/config.yaml#L151), `Solution` [:633](../../sync-worker/config.yaml#L633), the three `*RelatedItem` objects [:173](../../sync-worker/config.yaml#L173)/[:256](../../sync-worker/config.yaml#L256)/[:466](../../sync-worker/config.yaml#L466), `Idea` [:222](../../sync-worker/config.yaml#L222) | `ChangeRequest` has the most rag fields of any standard object (5) |
| Field service | `WorkOrder` [:663](../../sync-worker/config.yaml#L663) (36 fields), `WorkOrderLineItem` [:703](../../sync-worker/config.yaml#L703), `WorkPlan*` [:731-749](../../sync-worker/config.yaml#L731), `WorkStep*` [:754](../../sync-worker/config.yaml#L754)/[:768](../../sync-worker/config.yaml#L768), `Asset`/`AssetRelationship` [:63](../../sync-worker/config.yaml#L63)/[:87](../../sync-worker/config.yaml#L87) | |
| Social | `SocialPost` [:574](../../sync-worker/config.yaml#L574) — **51 fields, the widest object** — and `SocialPersona` [:549](../../sync-worker/config.yaml#L549) | Still under the 80-field adoption ceiling, so runtime adoption keeps widening it |
| Org-custom | `Interview__c` [:263](../../sync-worker/config.yaml#L263) (41 fields, **9 rag fields**), `Recruiter__c` [:522](../../sync-worker/config.yaml#L522), `Marketing__c` [:350](../../sync-worker/config.yaml#L350), `Onboarding__c` [:369](../../sync-worker/config.yaml#L369), `Invoice__c` [:316](../../sync-worker/config.yaml#L316), `Candidate_Training__c` [:118](../../sync-worker/config.yaml#L118), `Step_Deliverable_Definition__c` [:642](../../sync-worker/config.yaml#L642) | `Interview__c` also carries apparent duplicate-purpose fields `End_Time__c`/`EndTime__c` [:275-276](../../sync-worker/config.yaml#L275-L276) |
| PandaDoc managed package | 7 `pandadoc__*` objects [:778-852](../../sync-worker/config.yaml#L778-L852) | 9 of their 10 rag fields are **JSON blobs** |

**12 of the 61 `rag_fields` are declared-JSON columns**
([:547,786-787,794,815-816,827,834,841,852](../../sync-worker/config.yaml#L547)) — the exact input
class that defeats `chunk_text`'s whitespace tokenizer.

**Control flow** — Declarative. Consumed by
[config.py:61-87](../../sync-worker/syncworker/config.py#L61-L87) at startup
([main.py:262](../../sync-worker/syncworker/main.py#L262)) and rewritten by
[objects.py:149-152](../../sync-worker/syncworker/objects.py#L149-L152).

**State & side effects** — None. Mounted read-only at
[docker-compose.yml:324](../../docker-compose.yml#L324) and also baked into the image at
[`sync-worker/Dockerfile:16`](../../sync-worker/Dockerfile#L16).

**Dependencies** — Inbound: [config.py:49](../../sync-worker/syncworker/config.py#L49) (default path),
[objects.py:43](../../sync-worker/syncworker/objects.py#L43),
[`tests/test_config.py:5`](../../sync-worker/tests/test_config.py#L5),
[`sync-worker/Dockerfile:16`](../../sync-worker/Dockerfile#L16),
[docker-compose.yml:324](../../docker-compose.yml#L324). Outbound: none.

**Config** — The file *is* config; `SYNC_CONFIG_PATH` selects it
([config.py:48](../../sync-worker/syncworker/config.py#L48), set to `/app/config.yaml` at
[`sync-worker/Dockerfile:32`](../../sync-worker/Dockerfile#L32)).

**Failure modes**
- Structural validation is enforced by the loader and holds for all 48 entries: every entry carries
  `Id` and `SystemModstamp` ([config.py:78-79](../../sync-worker/syncworker/config.py#L78-L79)) and
  every `rag_field` appears in `fields` ([config.py:80-82](../../sync-worker/syncworker/config.py#L80-L82)).
- **The mount is `:ro`** ([docker-compose.yml:324](../../docker-compose.yml#L324)), so
  `python -m syncworker.objects add …` run *inside* the container — as
  [`README.md:217-231`](../../README.md#L217-L231) instructs — fails with a read-only filesystem error
  at [objects.py:152](../../sync-worker/syncworker/objects.py#L152). Only `list` works in-container.
- Nothing bounds the total: 631 fields × 48 objects × 48 cycles/day is the API-call driver, with no
  per-object enable/disable and no schedule.
- **Header drift**: line [1](../../sync-worker/config.yaml#L1) still says *"the eight standard objects
  synced in Phase 1"* — there are 48.

**Concurrency** — Read once per process start
([main.py:262](../../sync-worker/syncworker/main.py#L262)) and **never re-read**, so editing the
live-mounted file has no effect until the container restarts — exactly what
[objects.py:382-383](../../sync-worker/syncworker/objects.py#L382-L383) tells the operator to do.

**Complexity hotspots** — n/a (data).

**Findings** — `TEST-02`: [`tests/test_config.py`](../../sync-worker/tests/test_config.py) asserts
only that six core objects exist and that the loader's own invariants hold — two of its four tests are
tautological. Nothing pins a field-count floor or specific analytical fields, which is how
`Account.AnnualRevenue` disappeared between the current file and the untracked
`sync-worker/config.yaml.bak` snapshot.

---

## Crash safety — the watermark ordering guarantee (STRENGTH)

This is the strongest correctness property in the service, and it is deliberate.

1. **The upsert is a single transaction.**
   [storage.py:149-161](../../sync-worker/syncworker/storage.py#L149-L161) runs
   `BEGIN` → `ALTER TABLE … ADD COLUMN`* → `DELETE FROM "<obj>" WHERE Id IN (SELECT Id FROM
   _staging_df)` → `INSERT INTO "<obj>" BY NAME SELECT * FROM _staging_df` → `COMMIT`, with
   `ROLLBACK` + re-raise on any exception
   ([:162-164](../../sync-worker/syncworker/storage.py#L162-L164)). An intra-batch
   `drop_duplicates(subset=["Id"], keep="last")`
   ([:133](../../sync-worker/syncworker/storage.py#L133)) removes duplicate Ids first. Replaying the
   same batch therefore produces byte-identical rows.
2. **The watermark is written last.**
   [main.py:167](../../sync-worker/syncworker/main.py#L167) upserts inside the `for batch in batches:`
   loop; [main.py:188](../../sync-worker/syncworker/main.py#L188) writes the watermark **after** the
   loop completes without raising. The value written is `cycle_start`
   ([main.py:115](../../sync-worker/syncworker/main.py#L115)), captured *before* the first network
   call — so records modified *during* the extract are re-fetched next cycle rather than skipped.
3. **Therefore a crash mid-cycle is safe.** If the process dies after N batches but before
   [main.py:188](../../sync-worker/syncworker/main.py#L188), the watermark is unchanged, so the next
   cycle re-runs the *same* query and re-applies the *same* rows. DuckDB converges to the identical
   state. Proven for the two-batch case by
   [`tests/test_upsert.py:27-61`](../../sync-worker/tests/test_upsert.py#L27-L61), which asserts a
   `GROUP BY Id HAVING count(*) > 1` returns nothing.

**Where the guarantee does not extend:**

| Layer | Idempotent on replay? | Why |
|---|---|---|
| DuckDB tables | **Yes** | delete-by-Id + insert in one transaction ([storage.py:149-161](../../sync-worker/syncworker/storage.py#L149-L161)) |
| `_sync_meta` watermark | **Yes** | written last, `ON CONFLICT DO UPDATE` ([storage.py:99-106](../../sync-worker/syncworker/storage.py#L99-L106)) |
| Parquet files | **No** | each replay writes new `uuid4().hex[:8]` filenames ([storage.py:64-68](../../sync-worker/syncworker/storage.py#L64-L68)); the crashed run's files remain forever |
| LanceDB `chunks` | **No** | an index failure is swallowed at [main.py:177-184](../../sync-worker/syncworker/main.py#L177-L184) while the watermark advances at [:188](../../sync-worker/syncworker/main.py#L188), so those chunks are never retried |
| Salesforce deletes | **n/a** | never propagated to either store ([main.py:147-154](../../sync-worker/syncworker/main.py#L147-L154)) |

Cost of the design: when the watermark is `NULL` (first sync), a crash means the **entire Bulk full
extract repeats from scratch** — there is no resumability, no partial-progress marker and no reuse of
the completed Bulk job id.

---

## Cross-module observations

| Observation | Evidence |
|---|---|
| **Zero** `TODO`/`FIXME`/`HACK` markers in the whole service | `rg` over `syncworker/`, `tests/`, `Dockerfile`, `config.yaml` |
| `_IDENT_RE` exists in **four** copies, three identical and one allowing a leading `_` | [config.py:11](../../sync-worker/syncworker/config.py#L11), [objects.py:36](../../sync-worker/syncworker/objects.py#L36), [sf_client.py:34](../../sync-worker/syncworker/sf_client.py#L34), [storage.py:24](../../sync-worker/syncworker/storage.py#L24) |
| Dependency pins **do** cap majors here, unlike the orchestrator | [`sync-worker/requirements.txt:3-10`](../../sync-worker/requirements.txt#L3-L10) (`httpx>=0.27,<1`, `lancedb>=0.8,<1`, …) — but still no lockfile and no hashes; `lancedb>=0.8,<1` spans a large API-churn window that [main.py:177-184](../../sync-worker/syncworker/main.py#L177-L184) would swallow |
| The container runs **non-root** (uid 10001), unlike the orchestrator | [`sync-worker/Dockerfile:20-23`](../../sync-worker/Dockerfile#L20-L23) |
| No `.dockerignore` exists for `./sync-worker` | verified by `ls` — only `orchestrator/` and `frontend/` have one |
| No coverage tool, no linter, no type checker | [`sync-worker/requirements-dev.txt`](../../sync-worker/requirements-dev.txt) is 2 lines; a `# noqa: ARG002` at [main.py:41](../../sync-worker/syncworker/main.py#L41) has no linter to serve |

**Service-level findings**: `REL-02`, `OBS-01`, `TEST-01` (no CI runs these 104 tests), `TEST-02`,
`SEC-06`, `DATA-02`.
</content>
</invoke>
