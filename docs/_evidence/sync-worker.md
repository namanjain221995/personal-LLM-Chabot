# Evidence base — `sync-worker/`

Read-only technical due diligence. Every claim below is anchored to a `path:LINE` I personally read.
Assigned scope: all of `sync-worker/` except `.venv/`, `__pycache__/`, `.pytest_cache/`.

**Total assigned LOC = 4024** (`wc -l` over the 27 assigned files).
Breakdown: `syncworker/*.py` 1773 · `tests/*.py` 1190 · `config.yaml` 852 · `config.yaml.bak` 158 ·
`Dockerfile` 34 · `requirements.txt` 10 · `conftest.py` 5 · `requirements-dev.txt` 2.

**TODO/FIXME/HACK markers: NONE.** Verified with
`rg -n 'TODO|FIXME|HACK|XXX' sync-worker/syncworker sync-worker/tests sync-worker/conftest.py sync-worker/Dockerfile sync-worker/config.yaml` → no matches.

**Git tracking** (`git ls-files sync-worker`): all `syncworker/*.py`, all `tests/*.py`, `Dockerfile`,
`config.yaml`, `conftest.py`, `requirements*.txt` are tracked. `config.yaml.bak` is NOT tracked
(`.gitignore:46` = `*.bak`). No key/secret files are tracked. `.env` is gitignored (`.gitignore:10`)
and `git ls-files --error-unmatch .env` fails → not tracked. **No secret VALUE was read or copied.**

---

## 1. Executive orientation (verified)

The worker is a single-threaded, synchronous polling loop. `main()` (`sync-worker/syncworker/main.py:259`)
builds one `TokenManager` + `SalesforceClient` + `RagIndexer` **once**, then loops forever:
open DuckDB → `run_cycle` → close DuckDB → sleep `SYNC_INTERVAL_MINUTES`.

Per object (`sync-worker/syncworker/main.py:106`): read watermark → `describe` → optionally adopt new
fields → **Bulk API 2.0 full extract when watermark is NULL**, else **REST SOQL incremental on
`SystemModstamp >`** → for each batch: Parquet file + DuckDB delete-then-insert upsert + LanceDB
re-index → **finally** write the watermark.

**AWS Secrets Manager is GONE.** `sync-worker/syncworker/secrets.py` has zero cloud calls; the module
docstring says so at `sync-worker/syncworker/secrets.py:1-6` and `fetch_sf_credentials` explicitly
discards its old `secret_name`/`region` arguments at `sync-worker/syncworker/secrets.py:172`
(`del secret_name, region`). `tests/test_secrets.py:72` asserts `boto3` does not appear in the file.
Everything resolves from env vars only.

**Re-sync IS idempotent for DuckDB**, but only because the watermark is written last. See §"storage.py"
and finding F-06 for the mid-batch crash analysis, and F-04 / F-05 for where idempotency does *not* hold.

---

### sync-worker/syncworker/__init__.py  (18 LOC)

**Purpose** — Package docstring describing the worker (Bulk 2.0 full extract → incremental REST SOQL →
Parquet/DuckDB → LanceDB RAG via vLLM `/embeddings`). Declares `__all__`.

**Public surface**
- `__all__` : list[str] — `sync-worker/syncworker/__init__.py:9-18`. Lists `chunking, config, jsonlog,
  rag_index, secrets, sf_auth, sf_client, storage`. **`main` and `objects` are omitted** despite being the
  two entrypoint modules (`sync-worker/syncworker/main.py:259`, `sync-worker/syncworker/objects.py:298`).

**Control flow** — none (declarative only).

**State & side effects** — none. No imports beyond the docstring/`__all__`.

**Dependencies** — inbound: implicit, every `from syncworker.X import` in tests
(`sync-worker/tests/test_chunking.py:3`, `test_config.py:3`, `test_upsert.py:3`, `test_secrets.py:6`,
`test_jwt.py:7`, `test_limits.py:3`, `test_embeddings.py:11`, `test_discovery.py:11`,
`test_objects_cli.py:11`). outbound: none.

**Config** — none.

**Failure modes** — none. `__all__` is stale (missing `main`, `objects`) so
`from syncworker import *` will not expose the entrypoints — cosmetic only.

**Concurrency** — n/a.

**Complexity hotspots** — none.

**Notable** — `__all__` drift (`sync-worker/syncworker/__init__.py:9-18`) vs. actual modules on disk.

---

### sync-worker/syncworker/config.py  (87 LOC)

**Purpose** — Reads worker settings from environment variables and parses/validates the synced-object
list out of `config.yaml`.

**Public surface**
- `_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")` — `sync-worker/syncworker/config.py:11`
- `@dataclass(frozen=True) class Settings` — `sync-worker/syncworker/config.py:14-30`. Fields:
  `sync_interval_minutes:int`, `sync_auto_fields:bool`, `sync_max_fields:int`,
  `sync_report_new_objects:bool`, `parquet_dir:str`, `duckdb_path:str`, `lancedb_dir:str`,
  `embed_via:str`, `embed_model:str`, `sf_api_version:str`, `config_path:str`.
- `load_settings() -> Settings` — `sync-worker/syncworker/config.py:33`
- `@dataclass(frozen=True) class ObjectConfig` — `sync-worker/syncworker/config.py:54-58`.
  `name:str`, `fields:tuple[str,...]`, `rag_fields:tuple[str,...] = ()`.
- `load_object_configs(path: str) -> list[ObjectConfig]` — `sync-worker/syncworker/config.py:61`

**Control flow** — `load_object_configs`:
1. `open(path, encoding="utf-8")` + `yaml.safe_load` — `sync-worker/syncworker/config.py:63-64`.
2. Reject non-dict / missing `objects` list — `sync-worker/syncworker/config.py:65-66`.
3. Per entry: read `name`, `fields`, `rag_fields` — `sync-worker/syncworker/config.py:70-72`.
4. Validate object name against `_IDENT_RE` — `sync-worker/syncworker/config.py:73-74`.
5. Validate every field/rag_field name — `sync-worker/syncworker/config.py:75-77`.
6. Require `Id` **and** `SystemModstamp` in `fields` — `sync-worker/syncworker/config.py:78-79`.
7. Require every `rag_field` ⊆ `fields` — `sync-worker/syncworker/config.py:80-82`.
8. Append frozen `ObjectConfig` — `sync-worker/syncworker/config.py:83`.
9. Reject an empty object list — `sync-worker/syncworker/config.py:85-86`.

**State & side effects** — filesystem READ of `config_path` (`sync-worker/syncworker/config.py:63`);
env reads (below). No writes, no network, no GPU. No global mutation.

**Dependencies** — inbound: `sync-worker/syncworker/main.py:19`
(`load_object_configs, load_settings`), `sync-worker/tests/test_config.py:3`,
`sync-worker/tests/test_objects_cli.py:158,269`. outbound: `os`, `re`, `dataclasses`, `yaml`.

**Config** — every env var this module consumes:
| var | line | default |
|---|---|---|
| `SYNC_INTERVAL_MINUTES` | `sync-worker/syncworker/config.py:36` | `30` |
| `SYNC_AUTO_FIELDS` | `sync-worker/syncworker/config.py:37-38` | `true` |
| `SYNC_MAX_FIELDS` | `sync-worker/syncworker/config.py:39` | `80` |
| `SYNC_REPORT_NEW_OBJECTS` | `sync-worker/syncworker/config.py:40-41` | `true` |
| `PARQUET_DIR` | `sync-worker/syncworker/config.py:42` | `/data/parquet` |
| `DUCKDB_PATH` | `sync-worker/syncworker/config.py:43` | `/data/warehouse.duckdb` |
| `LANCEDB_DIR` | `sync-worker/syncworker/config.py:44` | `/data/lancedb` |
| `EMBED_VIA` | `sync-worker/syncworker/config.py:45` | `http://vllm-embed:30003/v1` |
| `EMBED_MODEL` | `sync-worker/syncworker/config.py:46` | `Qwen/Qwen3-Embedding-0.6B` |
| `SF_API_VERSION` | `sync-worker/syncworker/config.py:47` | `v61.0` |
| `SYNC_CONFIG_PATH` | `sync-worker/syncworker/config.py:48-50` | `<pkg>/../config.yaml` |

**Failure modes**
- `int(os.getenv("SYNC_INTERVAL_MINUTES","30"))` — `sync-worker/syncworker/config.py:36` — raises bare
  `ValueError` on a non-numeric value. Same at `:39`. `main()` calls `load_settings()` **outside** its
  try/except (`sync-worker/syncworker/main.py:261` vs. the `try:` at `:281`) → uncaught crash at startup.
- `open()` raises `FileNotFoundError` if `SYNC_CONFIG_PATH` is wrong — `sync-worker/syncworker/config.py:63`.
- `yaml.safe_load` raises `yaml.YAMLError` on malformed YAML — not caught.
- Nothing is swallowed. No bare `except`.
- No bound on the number of objects/fields loaded; `sync_max_fields` is only consulted by
  `adopt_new_fields` (`sync-worker/syncworker/main.py:91`), **never applied to the configured list itself**.
- Boolean parsing accepts anything not in `("0","false","no")` as true —
  `sync-worker/syncworker/config.py:38,41`. `SYNC_AUTO_FIELDS=off` silently means **true**.

**Concurrency** — fully synchronous. No module-level mutable state (`_IDENT_RE` is immutable).

**Complexity hotspots** — none > 60 LOC. `load_object_configs` = 27 LOC
(`sync-worker/syncworker/config.py:61-87`), cyclomatic ≈ 9.

**Notable**
- `_IDENT_RE` at `sync-worker/syncworker/config.py:11` is the **third** copy of the same regex
  (`sync-worker/syncworker/objects.py:36`, `sync-worker/syncworker/sf_client.py:34`) and a fourth,
  slightly different one lives at `sync-worker/syncworker/storage.py:24` (`^[A-Za-z_]…`).
- `os.path.join(here, "..", "config.yaml")` — `sync-worker/syncworker/config.py:49` — non-normalised
  relative path; works but is `…/syncworker/../config.yaml`.
- Env-var naming drift: this worker reads `EMBED_VIA` (`:45`) while the orchestrator reads
  `EMBED_BASE_URL` (`docker-compose.yml:241` vs `docker-compose.yml:314`) for the same vLLM endpoint.

---

### sync-worker/syncworker/jsonlog.py  (39 LOC)

**Purpose** — One-JSON-object-per-line stdout logging; promotes anything passed via `extra=` into the
JSON payload.

**Public surface**
- `_STANDARD_ATTRS: frozenset` — `sync-worker/syncworker/jsonlog.py:12-14`. Built by constructing a
  throwaway `logging.LogRecord("",0,"",0,"",(),None)` at import time and unioning
  `{"message","asctime","taskName"}`.
- `class JsonFormatter(logging.Formatter)` — `sync-worker/syncworker/jsonlog.py:17`
  - `format(self, record) -> str` — `sync-worker/syncworker/jsonlog.py:18`
- `setup_logging(level: int = logging.INFO) -> None` — `sync-worker/syncworker/jsonlog.py:34`

**Control flow** — `JsonFormatter.format`:
1. Build base payload `ts`/`level`/`logger`/`message` — `sync-worker/syncworker/jsonlog.py:19-25`.
   Timestamp is `time.gmtime(record.created)` + `.NNNZ` from `record.msecs` — `:20-22`.
2. Copy every non-standard, non-underscore `record.__dict__` key into the payload —
   `sync-worker/syncworker/jsonlog.py:26-28`.
3. Append `exc` when `record.exc_info` — `sync-worker/syncworker/jsonlog.py:29-30`.
4. `json.dumps(payload, default=str)` — `sync-worker/syncworker/jsonlog.py:31`.

`setup_logging`: create `StreamHandler(sys.stdout)` → attach `JsonFormatter` → **replace** the root
logger's handler list wholesale → set level — `sync-worker/syncworker/jsonlog.py:35-39`.

**State & side effects** — **global mutation**: `root.handlers[:] = [handler]`
(`sync-worker/syncworker/jsonlog.py:38`) destroys any pre-existing root handler, and
`root.setLevel(level)` (`:39`) mutates global logging config. Writes to `sys.stdout` only. No DB, no
network, no GPU, no env reads.

**Dependencies** — inbound: `sync-worker/syncworker/main.py:20` (`setup_logging`), called at
`sync-worker/syncworker/main.py:260`. Not imported by tests. outbound: `json`, `logging`, `sys`, `time`.

**Config** — none.

**Failure modes** — `json.dumps(..., default=str)` (`:31`) cannot raise on unserialisable values, so a
log call never crashes the worker. Nothing is swallowed; no bare `except`. **Risk:** every `extra=`
key is emitted verbatim, so any future `extra={"token": ...}` would print a secret. Today no call site
does that — the only `extra` payloads are event names, object names, counts, field lists and file paths
(`sync-worker/syncworker/main.py:42,100-102,124-125,131-133,157-159,170-172,182-183,189-192,220-223,
248-249,252-255,264-266,294-296`; `sync-worker/syncworker/sf_client.py:66-73,139,204-206`;
`sync-worker/syncworker/sf_auth.py:126-127`; `sync-worker/syncworker/rag_index.py:146-152`).

**Concurrency** — synchronous; `logging.StreamHandler` takes the module lock per emit. `_STANDARD_ATTRS`
is a module-level *immutable* frozenset computed once at import.

**Complexity hotspots** — none. `format` = 14 LOC.

**Notable** — the timestamp is assembled by string concatenation rather than
`datetime.strftime` — `sync-worker/syncworker/jsonlog.py:20-22`. Magic literal `:03d` for millis.

---

### sync-worker/syncworker/secrets.py  (181 LOC)  ← git-tracked, read carefully

**Purpose** — Resolves Salesforce credentials **entirely from environment variables**. AWS Secrets
Manager was removed on 2026-07-28 (`sync-worker/syncworker/secrets.py:1-6`); no cloud call remains.

**Public surface**
- `ENV_KEYS` — `sync-worker/syncworker/secrets.py:43` — **DEAD CODE**, never referenced
  (`rg -n 'ENV_KEYS'` matches only its own definition).
- `ENV_KEYS_FILE` — `sync-worker/syncworker/secrets.py:45` — **DEAD CODE**, same.
- `DEFAULT_KEY_PATH = "/data/sf_jwt_key.pem"` — `sync-worker/syncworker/secrets.py:50`
- `_THUMBPRINT_RE = re.compile(r"^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$")` — `sync-worker/syncworker/secrets.py:53`
- `_looks_like_a_thumbprint(value: str) -> bool` — `sync-worker/syncworker/secrets.py:56`
- `_check_pem(pem: bytes, source: str) -> bytes` — `sync-worker/syncworker/secrets.py:60`
- `@dataclass class SalesforceCredentials` — `sync-worker/syncworker/secrets.py:82-95`.
  Fields `client_id:str`, `username:str`, `login_url:str`, `private_key_pem:bytes=b""`,
  `client_secret:str=""`. `__repr__` returns the literal `"SalesforceCredentials(<redacted>)"`
  (`:92-93`) and `__str__ = __repr__` (`:95`). **NOT frozen** — mutable.
- `_identity_present(e) -> bool` — `sync-worker/syncworker/secrets.py:98`
- `_pem_from_file(path_value: str) -> bytes` — `sync-worker/syncworker/secrets.py:102`
- `credentials_from_env(env: dict | None = None) -> SalesforceCredentials | None` — `sync-worker/syncworker/secrets.py:118`
- `fetch_sf_credentials(secret_name=None, region=None) -> SalesforceCredentials` — `sync-worker/syncworker/secrets.py:163`

**Control flow** — `credentials_from_env` (`sync-worker/syncworker/secrets.py:118-160`):
1. `e = os.environ if env is None else env` — `:126`.
2. Return `None` unless all three of `SF_CLIENT_ID`/`SF_USERNAME`/`SF_LOGIN_URL` are truthy —
   `:127-128` via `_identity_present` (`:98-99`).
3. **`SF_CLIENT_SECRET` wins first.** If non-blank after `.strip()`, return credentials carrying only
   the secret, `private_key_pem=b""` — `:130-138`. The client-credentials grant is then used and **no key
   material is loaded or validated at all**.
4. Else `SF_PRIVATE_KEY_FILE` — `:140,142-143` → `_pem_from_file`.
5. Else `SF_PRIVATE_KEY_B64` — `:141,144-149`: `base64.b64decode(key_b64, validate=True)`, wrapping
   `binascii.Error`/`ValueError` into `ValueError("SF_PRIVATE_KEY_B64 is not valid base64")`, then `_check_pem`.
6. Else if `DEFAULT_KEY_PATH` (`/data/sf_jwt_key.pem`) exists on disk → read + `_check_pem` — `:150-151`.
   **Note this filesystem probe happens even when a caller-supplied `env` dict is passed** (that is why
   `tests/test_secrets.py:8-15` has to monkeypatch `DEFAULT_KEY_PATH`).
7. Else return `None` — `:152-153`.
8. Build credentials with `login_url` `.rstrip("/")` — `:155-160`.

`_pem_from_file` (`:102-115`): strip → reject thumbprints with a message naming `THUMBPRINT` (`:105-111`)
→ `Path.is_file()` check (`:113-114`) → `read_bytes()` → `_check_pem`.

`_check_pem` (`:60-79`): looks at `pem.lstrip()[:200]`; if `b"PRIVATE KEY"` absent, raises — with a
dedicated message when `b"CERTIFICATE"` is present (`:69-74`). **Returns the bytes UNCHANGED**, deliberately
(`:62-65`).

`fetch_sf_credentials` (`:163-181`): `del secret_name, region` (`:172`) → `credentials_from_env()` →
raise a `ValueError` naming the three identity vars plus the three key forms (`:176-181`).

**State & side effects** — filesystem READS: `_pem_from_file` (`sync-worker/syncworker/secrets.py:115`)
and `DEFAULT_KEY_PATH` (`:150-151`). **Zero network egress. Zero cloud SDK.** No DB, no GPU, no global
mutation.

**Dependencies** — inbound: `sync-worker/syncworker/main.py:22,269` (`fetch_sf_credentials()`),
`sync-worker/syncworker/objects.py:273,278` (`fetch_sf_credentials()` inside `_live_describe`),
`sync-worker/syncworker/sf_auth.py:16` (type import of `SalesforceCredentials`),
`sync-worker/tests/test_secrets.py:6`, `sync-worker/tests/test_jwt.py:87,117,138`.
outbound: `base64`, `binascii`, `os`, `re`, `dataclasses`, `pathlib`.

**Config** — env vars consumed (all via the `e` mapping):
`SF_CLIENT_ID`, `SF_USERNAME`, `SF_LOGIN_URL` — `sync-worker/syncworker/secrets.py:99`,
`:134-136`, `:156-158`; `SF_CLIENT_SECRET` — `:130`; `SF_PRIVATE_KEY_FILE` — `:140`;
`SF_PRIVATE_KEY_B64` — `:141`. Plus the hardcoded path `DEFAULT_KEY_PATH` — `:50`.

**Failure modes**
- Raises `ValueError` for: bad base64 (`:148`), thumbprint-as-path (`:106-111`), missing key file
  (`:114`), non-PEM material (`:70-78`), and no credentials at all (`:176-181`).
- **Nothing is swallowed** — no bare `except`. The `except (binascii.Error, ValueError)` at `:147` is
  narrow and re-raises with `from exc`.
- No timeout/retry needed (no I/O beyond two local reads).
- The `SF_CLIENT_SECRET` short-circuit at `:130-138` means a deployment that sets *both* a secret and a
  key silently ignores the key (asserted deliberately by `tests/test_secrets.py:179-186`).
- `credentials_from_env` never validates that `login_url` is https or well-formed; a typo'd
  `SF_LOGIN_URL` becomes the JWT `aud` claim and the POST target unchecked
  (`sync-worker/syncworker/sf_auth.py:36,76`).

**Concurrency** — synchronous, no shared mutable state. `SalesforceCredentials` is a plain (mutable)
dataclass shared by reference with `TokenManager` (`sync-worker/syncworker/sf_auth.py:52`).

**Complexity hotspots** — none > 60 LOC. Largest: `credentials_from_env` = 43 LOC
(`sync-worker/syncworker/secrets.py:118-160`), cyclomatic ≈ 8.

**Notable**
- Secret hygiene is genuinely good: `__repr__`/`__str__` redact (`:92-95`), tests assert it
  (`tests/test_secrets.py:50-53,188-190`), and no exception message embeds a value.
- `DEFAULT_KEY_PATH = /data/sf_jwt_key.pem` (`:50`) places the RSA signing key inside the **`data`
  Docker volume**, which `docker-compose.yml:269` also mounts into the `orchestrator` container that runs
  LLM-generated SQL. See finding F-10.
- Dead constants `ENV_KEYS`/`ENV_KEYS_FILE` (`:43,45`).
- `fetch_sf_credentials`'s two parameters exist only for backwards compatibility and are immediately
  deleted (`:163-172`) — no live caller passes them (`main.py:269`, `objects.py:278` both call it bare).
- Documentation drift: `.env.example:13-14` still describes `SF_SECRET_NAME` and "JSON keys …
  PRIVATE_KEY_B64" for AWS Secrets Manager, and `docker-compose.yml:327` still says "or AWS Secrets
  Manager" — both refer to a path that no longer exists in code.
- `tests/test_secrets.py:1` docstring still says "Secrets Manager JSON second" — stale.

---

### sync-worker/syncworker/sf_auth.py  (129 LOC)  ← JWT Bearer focus

**Purpose** — Builds the RS256 JWT assertion and manages the cached Salesforce access token for both the
JWT-bearer and client-credentials grants.

**Public surface**
- `JWT_VALIDITY_SECONDS = 180` — `sync-worker/syncworker/sf_auth.py:20`
- `GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"` — `sync-worker/syncworker/sf_auth.py:21`
- `build_jwt_assertion(client_id, username, login_url, private_key_pem: bytes, now: float|None=None) -> str`
  — `sync-worker/syncworker/sf_auth.py:24-39`
- `class TokenManager` — `sync-worker/syncworker/sf_auth.py:42`
  - `TOKEN_TTL_SECONDS = 25 * 60` — `sync-worker/syncworker/sf_auth.py:47`
  - `__init__(self, creds: SalesforceCredentials, http: httpx.Client|None=None)` — `sync-worker/syncworker/sf_auth.py:49`
  - `get_token(self) -> tuple[str, str]` — `sync-worker/syncworker/sf_auth.py:58`
  - `invalidate(self) -> None` — `sync-worker/syncworker/sf_auth.py:70`
  - `_request_token(self) -> tuple[str, str]` — `sync-worker/syncworker/sf_auth.py:75`

**Control flow**

*Assertion construction* — `sync-worker/syncworker/sf_auth.py:24-39`:
1. `issued_at = int(time.time() if now is None else now)` — `:32`. **Wall-clock**, not monotonic.
2. Claims dict `{iss: client_id, sub: username, aud: login_url, exp: issued_at + 180}` — `:33-38`.
   **There is no `iat`, no `nbf`, and no `jti`.**
3. `jwt.encode(claims, private_key_pem, algorithm="RS256")` — `:39`. PyJWT hands the PEM to
   `cryptography`; a malformed/encrypted key raises here, not in `secrets.py`.

*Token caching* — `get_token` (`sync-worker/syncworker/sf_auth.py:58-68`):
1. `stale = self._access_token is None or time.monotonic() - self._obtained_at > TOKEN_TTL_SECONDS` — `:60-63`.
   `_obtained_at` starts at `0.0` (`:56`) so the first call is always stale.
2. If stale → `_request_token()` and stamp `_obtained_at = time.monotonic()` — `:64-66`.
3. `assert` both are non-None — `:67`. **A bare `assert`, which `python -O` strips.**
4. Return `(access_token, instance_url)` — `:68`.

*Reactive refresh on 401* — `invalidate()` (`:70-73`) nulls `_access_token` and `_instance_url` but
**leaves `_obtained_at` untouched**; that is harmless because the `is None` check dominates the `or`
at `:60-62`. The only caller is `sync-worker/syncworker/sf_client.py:140`.

*Token request* — `_request_token` (`sync-worker/syncworker/sf_auth.py:75-129`):
1. `url = f"{creds.login_url}/services/oauth2/token"` — `:76`.
2. **If `creds.client_secret` is set** → POST `grant_type=client_credentials`, `client_id`,
   `client_secret` — `:78-90`. No JWT is built.
3. Else → `build_jwt_assertion(...)` then POST `grant_type=<GRANT_TYPE>`, `assertion=` — `:91-100`.
4. On non-200: parse `error_description` inside a **bare-ish `except Exception: error = ""`** (`:106-109`),
   attach a "My Domain" hint (`:110-115`) or a "Run As" hint (`:116-120`), then
   `raise RuntimeError(f"…HTTP {status}{hint}")` — `:121-123`. **The body is deliberately not logged.**
5. On 200: `body["access_token"]`, `body["instance_url"].rstrip("/")` — `:124,129`; log
   `event=sf_token_obtained` — `:125-128`.

**State & side effects** — **network egress: `POST {SF_LOGIN_URL}/services/oauth2/token`**
(`sync-worker/syncworker/sf_auth.py:83,98`) — the only outbound call in this module. Instance mutation of
`_access_token`/`_instance_url`/`_obtained_at` (`:54-56,65-66,72-73`). Reads the wall clock (`:32`) and the
monotonic clock (`:62,66`). No filesystem, no DB, no GPU, no env reads.

**Dependencies** — inbound: `sync-worker/syncworker/sf_client.py:25` (`TokenManager` used at
`sf_client.py:134,140`), `sync-worker/syncworker/main.py:23,270`,
`sync-worker/syncworker/objects.py:274,278`, `sync-worker/tests/test_jwt.py:7,86,116,137`.
outbound: `logging`, `time`, `httpx`, `jwt` (PyJWT), `.secrets.SalesforceCredentials`.

**Config** — no direct `os.getenv`. Everything arrives through `SalesforceCredentials`
(`SF_LOGIN_URL` → `:76`; `SF_CLIENT_ID` → `:34,87`; `SF_USERNAME` → `:35`;
`SF_CLIENT_SECRET` → `:78,88`; key PEM → `:39`).

**Failure modes**
- **Clock skew: entirely unhandled.** `exp = wall_clock_now + 180` (`:32,37`) with no `nbf`, no `iat`,
  and no skew allowance. Salesforce rejects an assertion whose `exp` is more than 3 minutes ahead of
  *its* clock, or already past. On an air-gapped DGX with drifting/unsynced time the flow fails with an
  opaque `RuntimeError: Salesforce token request failed with HTTP 400` (`:121-123`) that names neither
  the clock nor the `error_description`. See finding F-08.
- **`TOKEN_TTL_SECONDS = 25*60` is a guess** (`:47`) — the comment at `:45-46` says Salesforce returns no
  `expires_in` for this grant. If the org's session timeout is set below 25 minutes, every request in the
  gap 401s once and is retried by `sf_client._request` (`sync-worker/syncworker/sf_client.py:138-143`),
  so it self-heals at the cost of an extra round trip.
- **No retry, no backoff, no rate-limit awareness** on the token POST itself (`:83,98`). A single
  transient 503 from `login.salesforce.com` raises `RuntimeError` and kills the whole cycle (caught only
  at `sync-worker/syncworker/main.py:292`, which then backs off 30 s → 30 min).
- **Timeout:** the default client is `httpx.Client(timeout=30.0)` (`:53`) — bounded. Good.
- **The `except Exception` at `:106-109` swallows every JSON-parse failure** and produces `error = ""`,
  so a non-JSON error page (e.g. a proxy's HTML 502) loses all diagnostic content; only the status code
  survives.
- `assert self._access_token is not None …` (`:67`) is a control-flow assert — removed under `python -O`,
  after which `get_token` could return `(None, None)` and fail later with an obscure `TypeError` in the
  f-string at `sync-worker/syncworker/sf_client.py:135-136`.
- `body["access_token"]` / `body["instance_url"]` (`:129`) raise `KeyError` on a 200 with an unexpected
  shape. Not caught.
- The `httpx.Client` created at `:53` is never closed.

**Concurrency** — fully synchronous. `TokenManager` holds shared mutable state
(`_access_token`, `_instance_url`, `_obtained_at`) with **no lock** — `sync-worker/syncworker/sf_auth.py:54-56`.
The worker is single-threaded today (`sync-worker/syncworker/main.py:280-299` is one loop), so no race
is currently reachable, but the class is not thread-safe and there is a read-modify-write window at
`:60-66` between the staleness check and the stamp.

**Complexity hotspots** — `_request_token` = 55 LOC (`sync-worker/syncworker/sf_auth.py:75-129`),
cyclomatic ≈ 7 (grant branch, status branch, two hint branches, one try/except). Under both thresholds.

**Notable**
- Magic numbers: `180` (`:20`), `25 * 60` (`:47`), `30.0` timeout (`:53`).
- The class docstring at `:1-6` claims tokens are "refreshed proactively after a TTL and reactively on
  401 (via invalidate())" — both verified true (`:60-63` and `sf_client.py:138-143`).
- The two error hints (`:110-120`) are string-matched against `error_description`; they break silently if
  Salesforce rewords the message.

---

### sync-worker/syncworker/sf_client.py  (258 LOC)

**Purpose** — Read-only Salesforce data access: Bulk API 2.0 query jobs, REST SOQL with pagination,
`describe`, `sobjects` listing, plus `Sforce-Limit-Info` parsing.

**Public surface**
- `LIMIT_INFO_HEADER = "Sforce-Limit-Info"` — `sync-worker/syncworker/sf_client.py:29`
- `LIMIT_WARN_THRESHOLD = 0.80` — `sync-worker/syncworker/sf_client.py:30`
- `READ_ONLY_BULK_OPERATIONS = frozenset({"query","queryAll"})` — `sync-worker/syncworker/sf_client.py:31`
- `_API_USAGE_RE = re.compile(r"api-usage=(\d+)/(\d+)")` — `sync-worker/syncworker/sf_client.py:33`
- `_IDENT_RE` — `sync-worker/syncworker/sf_client.py:34`
- `parse_limit_info(header_value: str|None) -> tuple[int,int]|None` — `sync-worker/syncworker/sf_client.py:37`
- `check_api_limits(header_value: str|None, logger=log) -> float|None` — `sync-worker/syncworker/sf_client.py:50`
- `_validate_identifiers(object_name, fields)` — `sync-worker/syncworker/sf_client.py:78`
- `build_full_soql(object_name, fields) -> str` — `sync-worker/syncworker/sf_client.py:86`
- `build_incremental_soql(object_name, fields, watermark) -> str` — `sync-worker/syncworker/sf_client.py:91`
- `class SalesforceClient` — `sync-worker/syncworker/sf_client.py:104`
  - `__init__(token_manager, api_version="v61.0", http=None, poll_interval=5.0, bulk_page_size=10000)` — `sync-worker/syncworker/sf_client.py:107-119`
  - `_request(method, path, *, headers=None, _retry_auth=True, **kwargs) -> httpx.Response` — `sync-worker/syncworker/sf_client.py:123`
  - `describe_field_types(object_name) -> dict` — `sync-worker/syncworker/sf_client.py:159`
  - `describe_fields(object_name) -> set` — `sync-worker/syncworker/sf_client.py:173`
  - `list_objects() -> dict` — `sync-worker/syncworker/sf_client.py:182`
  - `bulk_query(soql, operation="query") -> Iterator[list[dict]]` — `sync-worker/syncworker/sf_client.py:193`
  - `soql_query(soql) -> Iterator[list[dict]]` — `sync-worker/syncworker/sf_client.py:242`

**Control flow**

*`_request`* (`sync-worker/syncworker/sf_client.py:123-157`):
1. Reject any method other than GET/POST — `:132-133`.
2. `token, instance_url = self._tm.get_token()` — `:134` (may trigger a token POST).
3. Build URL: `instance_url + path` when `path` starts with `/`, else use `path` verbatim — `:135`.
4. Merge `Authorization: Bearer …` with caller headers — `:136`.
5. `self._http.request(...)` — `:137`.
6. **On 401 and `_retry_auth`**: log `sf_token_refresh`, `self._tm.invalidate()`, recurse once with
   `_retry_auth=False` — `:138-143`. Note this early-returns **before** `check_api_limits`.
7. `check_api_limits(resp.headers.get("Sforce-Limit-Info"))` — `:144`.
8. `raise_for_status()`; on `HTTPStatusError`, re-raise a new one whose message embeds
   `resp.text[:400]` with newlines flattened — `:145-156`, `from None`.

*`bulk_query`* (`sync-worker/syncworker/sf_client.py:193-238`):
1. Guard `operation in {"query","queryAll"}` — `:195-196`.
2. `POST /services/data/{v}/jobs/query` with `{"operation","query"}` — `:198-201`; log job id — `:203-206`.
3. **Poll loop** `while True` — `:208-220`: `GET /jobs/query/{id}`; break on `JobComplete`;
   raise `RuntimeError` on `Failed`/`Aborted` (`:215-219`); else `time.sleep(self._poll_interval)`
   (`:220`). **No iteration cap, no wall-clock deadline, no shutdown check.**
4. **Results loop** `while True` — `:222-238`: `GET /jobs/query/{id}/results` with
   `maxRecords=self._bulk_page_size` and optional `locator`, `Accept: text/csv` (`:224-232`);
   parse the *entire* body with `csv.DictReader(io.StringIO(resp.text))` (`:233`); yield non-empty
   batches (`:234-235`); read `Sforce-Locator` and stop when absent or the literal string `"null"`
   (`:236-238`).

*`soql_query`* (`sync-worker/syncworker/sf_client.py:242-258`):
1. `GET /services/data/{v}/query?q=<soql>` — `:244-246`.
2. Loop: strip the `attributes` key from every record (`:249-252`), yield non-empty batches (`:253-254`),
   stop when `done` is true or `nextRecordsUrl` is missing (`:255-256`), else fetch the next page (`:258`).

*`describe_field_types`* (`:159-171`): lazily creates `self._describe_cache` via
`getattr(self, "_describe_cache", None)` (`:161-163`), then on a miss does
`GET /services/data/{v}/sobjects/{name}/describe` and stores `{field name: type}` (`:164-170`).

**State & side effects** — **network egress, all to the Salesforce instance URL:**
`POST /services/data/{v}/jobs/query` (`:199-201`), `GET /jobs/query/{id}` (`:209-211`),
`GET /jobs/query/{id}/results` (`:227-232`), `GET /services/data/{v}/query` (`:244-246`),
`GET <nextRecordsUrl>` (`:258`), `GET /sobjects/{name}/describe` (`:165-167`),
`GET /sobjects/` (`:184`). Indirectly triggers the token POST via `TokenManager.get_token()` (`:134`).
Instance mutation of `_describe_cache` (`:163,168`). Logging only; no DB, no filesystem, no GPU, no env reads.

**Dependencies** — inbound: `sync-worker/syncworker/main.py:24` (`SalesforceClient`,
`build_full_soql`, `build_incremental_soql`) used at `main.py:149,153,270`;
`sync-worker/tests/test_limits.py:3-7`. outbound: `csv`, `io`, `logging`, `re`, `time`,
`collections.abc.Iterator`, `httpx`, `.sf_auth.TokenManager`.

**Config** — no `os.getenv` here. `api_version` is injected from `SF_API_VERSION`
(`sync-worker/syncworker/config.py:47` → `sync-worker/syncworker/main.py:270`).
`poll_interval` and `bulk_page_size` are **constructor defaults only** —
`sync-worker/syncworker/main.py:270` passes neither, so `5.0` s and `10000` rows are effectively hardcoded.

**Failure modes**
- **Unbounded Bulk job poll loop** — `sync-worker/syncworker/sf_client.py:208-220`. A job stuck in
  `UploadComplete`/`InProgress` spins forever at 5 s intervals. See finding F-01.
- **No retry on anything except a single 401** — `:138-143`. 5xx, `ConnectError`, `ReadTimeout`, and
  Salesforce's `REQUEST_LIMIT_EXCEEDED` (403) all propagate immediately.
- **No 429 / Retry-After handling anywhere.** `check_api_limits` (`:50-75`) only *warns* at ≥ 80 %; it
  never throttles, sleeps, or aborts.
- **Timeouts:** the default `httpx.Client(timeout=120.0)` (`:117`) covers connect+read+write+pool. Bounded.
- **Memory:** `resp.text` for a 10 000-row CSV page is materialised, then copied again into
  `io.StringIO` (`:233`), then again into a list of dicts. ~3× peak.
- **`csv.DictReader` default field-size limit is 131 072 characters** (verified:
  `python3 -c "import csv; print(csv.field_size_limit())"` → `131072`). Salesforce Long Text Area /
  Rich Text Area fields max out at exactly 131 072 characters, and CSV escaping of embedded `"`
  *adds* characters — so a full-size long-text value raises `_csv.Error: field larger than field limit`
  at `:233`, aborting that object's entire extract. Nothing raises the limit.
- `resp.json()` calls at `:202,211,258,169,187` raise on non-JSON bodies; `["id"]` at `:202` raises `KeyError`.
- **Nothing is swallowed in this module** — no bare `except`. The one `except httpx.HTTPStatusError`
  (`:147-156`) re-raises.
- `raise … from None` (`:156`) discards the original traceback chain.
- `_request`'s 401 retry replays a **POST** (`:141-143`), so a token expiring between `get_token` and the
  response can create a **second Bulk query job** for the same SOQL. Harmless (read-only) but it burns
  API quota.
- `build_incremental_soql`'s watermark regex (`:96`) accepts `Z` or `±HHMM`; `sf_datetime_literal`
  (`sync-worker/syncworker/storage.py:35`) always emits the `Z` form, so they agree.
- SOQL injection is blocked: object and every field are checked against `_IDENT_RE`
  (`:78-83`, called from `:87` and `:95`) and the watermark against a strict datetime regex (`:96-97`).

**Concurrency** — fully synchronous. `bulk_query` and `soql_query` are **generators**, so all network
I/O happens lazily inside the caller's `for batch in batches:` loop
(`sync-worker/syncworker/main.py:163`) — a fact that matters because the Parquet write, the DuckDB
upsert and the embedding calls run *between* Salesforce page fetches, keeping the Bulk job's result
locator alive for the whole time. `_describe_cache` is per-instance mutable state with no lock;
single-threaded today.

**Complexity hotspots** — none exceed 60 LOC. Largest: `bulk_query` = 46 LOC (`:193-238`), cyclomatic ≈ 9
(two unbounded `while True` loops, 4 branch points). `_request` = 35 LOC (`:123-157`), cyclomatic ≈ 7,
with one level of recursion.

**Notable**
- Magic numbers: `0.80` (`:30`), `poll_interval=5.0` (`:113`), `bulk_page_size=10000` (`:114`),
  `timeout=120.0` (`:117`), `resp.text[:400]` (`:151`).
- `_describe_cache` is created by `getattr`/`setattr` at `:161-163` rather than in `__init__` — hidden state.
- `_IDENT_RE` at `:34` duplicates `sync-worker/syncworker/config.py:11` and `sync-worker/syncworker/objects.py:36`.
- Read-only posture is genuinely enforced in three places: method allow-list (`:132-133`), bulk
  operation allow-list (`:195-196`), and the module docstring's endpoint inventory (`:3-8`).
- `check_api_limits` is skipped on the 401 path because `:141-143` returns first.

---

### sync-worker/syncworker/storage.py  (167 LOC)  ← watermark / idempotency focus

**Purpose** — Lands each batch as a Parquet file and upserts it into a per-object DuckDB table;
stores per-object sync watermarks in `_sync_meta`.

**Public surface**
- `META_TABLE = "_sync_meta"` — `sync-worker/syncworker/storage.py:23`
- `_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")` — `sync-worker/syncworker/storage.py:24`
- `_safe_ident(name) -> str` — `sync-worker/syncworker/storage.py:27`
- `sf_datetime_literal(dt: datetime) -> str` — `sync-worker/syncworker/storage.py:33`
  (`dt.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%SZ")` — `:35`)
- `normalize_records(records: list[dict]) -> list[dict]` — `sync-worker/syncworker/storage.py:38`
- `write_parquet_batch(df, object_name, parquet_dir) -> str` — `sync-worker/syncworker/storage.py:59`
- `class Store` — `sync-worker/syncworker/storage.py:72`
  - `__init__(db_path: str)` — `sync-worker/syncworker/storage.py:75`
  - `close()` — `sync-worker/syncworker/storage.py:87`
  - `get_watermark(object_name) -> str|None` — `sync-worker/syncworker/storage.py:92`
  - `set_watermark(object_name, watermark) -> None` — `sync-worker/syncworker/storage.py:99`
  - `_table_exists(table) -> bool` — `sync-worker/syncworker/storage.py:110`
  - `_table_columns(table) -> list[str]` — `sync-worker/syncworker/storage.py:118`
  - `upsert(object_name, df) -> int` — `sync-worker/syncworker/storage.py:124`

**Control flow**

*`Store.__init__`* (`:75-85`): `os.makedirs(dirname(db_path))` (`:76-78`) → `duckdb.connect(db_path)`
(`:79`) → `CREATE TABLE IF NOT EXISTS "_sync_meta" (object_name VARCHAR PRIMARY KEY, watermark VARCHAR,
updated_at TIMESTAMP)` (`:80-85`).

*`get_watermark`* (`:92-97`): parameterised `SELECT watermark FROM "_sync_meta" WHERE object_name = ?`
→ `row[0] if row else None`.

*`set_watermark`* (`:99-106`): parameterised `INSERT … VALUES (?, ?, now()) ON CONFLICT (object_name)
DO UPDATE SET watermark = excluded.watermark, updated_at = excluded.updated_at`. **Runs outside any
explicit transaction** → DuckDB autocommits it as its own statement.

*`upsert`* (`:124-167`):
1. `_safe_ident(object_name)` (`:125`); return `0` on an empty frame (`:127-128`); require an `Id`
   column else `ValueError` (`:129-130`).
2. `df.drop_duplicates(subset=["Id"], keep="last")` — `:133`.
3. `con.register("_staging_df", df)` — `:136`; `finally: con.unregister("_staging_df")` — `:165-166`.
4. **First time**: `BEGIN TRANSACTION` → `CREATE TABLE "<obj>" AS SELECT * FROM _staging_df` → `COMMIT`
   → return `len(df)` — `:138-142`.
5. **Otherwise**: compute new columns by diffing `DESCRIBE "<obj>"` (`:118-122`) against
   `DESCRIBE SELECT * FROM _staging_df` (`:146-147`).
6. `BEGIN TRANSACTION` → `ALTER TABLE … ADD COLUMN` per new column (`:151-154`) →
   `DELETE FROM "<obj>" WHERE Id IN (SELECT Id FROM _staging_df)` (`:155-157`) →
   `INSERT INTO "<obj>" BY NAME SELECT * FROM _staging_df` (`:158-160`) → `COMMIT` (`:161`);
   on any exception `ROLLBACK` and re-raise (`:162-164`).

**State & side effects**
- **DB writes**: DuckDB at `DUCKDB_PATH` — `_sync_meta` DDL (`:80-85`), watermark upsert (`:100-106`),
  per-object `CREATE TABLE` (`:140`), `ALTER TABLE ADD COLUMN` (`:152-154`), `DELETE` (`:155-157`),
  `INSERT` (`:158-160`).
- **Filesystem writes**: `os.makedirs` for the DB parent (`:77`) and for
  `PARQUET_DIR/<object>/` (`:62-63`); one Parquet file per batch named
  `<object>_<UTCstamp>_<uuid8>.parquet` (`:64-68`).
- No network, no GPU, no env reads, no global mutation.

**Dependencies** — inbound: `sync-worker/syncworker/main.py:25` imports
`Store, normalize_records, sf_datetime_literal, write_parquet_batch`; used at `main.py:115,164,166,167,285`.
`sync-worker/tests/test_upsert.py:3`, `sync-worker/tests/test_watermark.py:1`.
outbound: `logging`, `os`, `re`, `uuid`, `datetime`, `duckdb`, `pandas`, `pyarrow`, `pyarrow.parquet`.

**Config** — none directly; `db_path` and `parquet_dir` are injected from
`sync-worker/syncworker/config.py:42-43`.

**Failure modes**
- `_safe_ident` raises `ValueError` on a non-identifier (`:28-29`); it is applied to `object_name`
  (`:61,125`) and to each drifted column name (`:153`) — but the **table name inside the f-string at
  `:140,153,156,159` is the already-validated `table`**, so no injection path exists via config
  (`sync-worker/syncworker/config.py:73-77` validates first).
- `upsert` `ROLLBACK`s and re-raises on failure (`:162-164`) — nothing swallowed. **No bare `except`
  anywhere in this file.**
- The `finally: con.unregister` (`:165-166`) runs even on the early `return` at `:142` — but that early
  return is *inside* the `try`, so it is correct.
- **`log` is defined at `:21` and never used** — this module emits no log lines at all, so a Parquet
  write or upsert failure produces no module-level telemetry (only `main.py:246-250` reports it).
- No timeout concept (local I/O). No bound on Parquet directory growth. No retention/compaction.
- `duckdb.connect(db_path)` (`:79`) raises `duckdb.IOException` if another process holds the write lock;
  bubbles to `sync-worker/syncworker/main.py:292` → backoff.
- **`normalize_records` casts every value to `str` or `None`** (`:38-56`), so every DuckDB column is
  created `VARCHAR` by `CREATE TABLE AS SELECT` (`:140`). See finding F-02.

**Concurrency** — synchronous. `Store` holds one DuckDB connection per instance (`:79`), created and
closed once per cycle (`sync-worker/syncworker/main.py:285,289`). No module-level mutable state.
DuckDB itself allows one writer per file; the orchestrator opens the same file `read_only=True`
(`orchestrator/app/core/schema_cache.py:40-47`), which is compatible.

**IS RE-SYNC IDEMPOTENT? — verified answer**
- **DuckDB: YES.** `upsert` is DELETE-by-Id-then-INSERT inside one transaction (`:149-161`) plus an
  intra-batch `drop_duplicates(keep="last")` (`:133`). Replaying the same batch produces the same rows.
  `sync-worker/tests/test_upsert.py:27-61` proves no duplicate Ids across two batches.
- **Watermark: written LAST.** `sync-worker/syncworker/main.py:188` runs only after the whole
  `for batch in batches` loop (`main.py:163-184`) completes without raising.
- **Process dies after upsert, before watermark commit** → the watermark is unchanged, so the next cycle
  re-runs the *same* query and re-upserts the *same* rows. DuckDB converges. **Correct.**
  Cost: if the watermark was `NULL` (first sync), the next run repeats the **entire Bulk full extract**
  from scratch — there is no resumability, no partial-progress marker, and no reuse of the completed
  Bulk job id.
- **Parquet: NOT idempotent.** Each replay writes *new* files with fresh `uuid4().hex[:8]` names
  (`:64-68`), so the crashed run's partial files remain forever alongside the replay's. See F-05.
- **LanceDB: NOT idempotent in the failure direction.** See F-04.
- **DELETEs in Salesforce are never propagated** to DuckDB or LanceDB — see F-03.

**Complexity hotspots** — largest is `upsert` = 44 LOC (`sync-worker/syncworker/storage.py:124-167`),
cyclomatic ≈ 8. Under both thresholds.

**Notable**
- Magic: `uuid.uuid4().hex[:8]` (`:66`), stamp format `%Y%m%dT%H%M%S` (`:64`).
- `_IDENT_RE` here allows a leading `_` (`:24`) unlike the other three copies — needed for `_sync_meta`
  but never actually applied to it (the meta table name is interpolated raw at `:81,94,101`).
- `_sync_meta` lives in the `main` schema, so `orchestrator/app/core/schema_cache.py:47-53`
  (`information_schema.columns WHERE table_schema='main'`) exposes it to the SQL-writing LLM as if it
  were a business table.
- No PRIMARY KEY or index on any per-object table (`:140`), so `DELETE … WHERE Id IN (…)` (`:156`) is a
  full scan per batch.
- Unused import-level `log` (`:21`).

---

### sync-worker/syncworker/rag_index.py  (154 LOC)  ← embedding / dedup focus

**Purpose** — Chunks configured long-text fields, embeds them through the vLLM OpenAI-compatible
`/embeddings` endpoint, and replaces the affected records' rows in the LanceDB `chunks` table.

**Public surface**
- `TABLE_NAME = "chunks"` — `sync-worker/syncworker/rag_index.py:24`
- `EMBED_BATCH_SIZE = 32` — `sync-worker/syncworker/rag_index.py:25`
- `_SF_ID_RE = re.compile(r"^[a-zA-Z0-9]{15,18}$")` — `sync-worker/syncworker/rag_index.py:26`
- `class OpenAIEmbedder` — `sync-worker/syncworker/rag_index.py:29`
  - `__init__(base_url, model, http: httpx.Client|None=None)` — `sync-worker/syncworker/rag_index.py:36-41`
  - `embed(texts: list[str]) -> list[list[float]]` — `sync-worker/syncworker/rag_index.py:43`
- `class RagIndexer` — `sync-worker/syncworker/rag_index.py:60`
  - `__init__(lancedb_dir, embedder)` — `sync-worker/syncworker/rag_index.py:61`
  - `_connect()` — `sync-worker/syncworker/rag_index.py:66`
  - `_open_or_create_table(dim: int)` — `sync-worker/syncworker/rag_index.py:73`
  - `_open_table_if_exists()` — `sync-worker/syncworker/rag_index.py:91`
  - `index_records(object_name, records, rag_fields) -> int` — `sync-worker/syncworker/rag_index.py:97`

**Control flow**

*`OpenAIEmbedder.embed`* (`:43-57`):
1. Slice `texts` into fixed windows of `EMBED_BATCH_SIZE = 32` — `:45-46`.
2. `POST {base_url}/embeddings` with `{"model": …, "input": batch}` — `:47-50`.
3. `resp.raise_for_status()` — `:51`; `vectors.extend(item["embedding"] for item in resp.json()["data"])` — `:52`.
4. After all batches, assert count parity or raise `RuntimeError` — `:53-56`.

*`RagIndexer.index_records`* (`:97-154`):
1. Return `0` when `rag_fields` or `records` is empty — `:101-102`.
2. Per record: read `Id`, skip if it fails `_SF_ID_RE` (`:107-109`); append to `record_ids` (`:110`);
   read `SystemModstamp` (`:111`); for each `rag_field` with a non-`None` value, `chunk_text(str(value))`
   and build a row dict `{text, object, record_id, field, system_modstamp}` — `:112-125`.
   **No `vector` yet, and no comparison against what is already indexed.**
3. If rows exist: `self._embedder.embed([r["text"] for r in rows])` (`:128`) — **one call covering the
   whole batch**, then attach vectors (`:129-130`) and
   `_open_or_create_table(dim=len(rows[0]["vector"]))` (`:131`).
4. Else: `_open_table_if_exists()`; return `0` if the table does not exist yet (`:135-137`).
5. **`for rid in record_ids: table.delete(f"record_id = '{rid}'")`** — `:141-142`. One LanceDB delete
   **per record**.
6. `table.add(rows)` and log `event=rag_indexed` — `:144-153`. Return `len(rows)` — `:154`.

*`_open_or_create_table`* (`:73-89`): if `chunks` exists, `db.open_table` and **`dim` is ignored**
(`:77-78`); else create with schema
`vector: fixed_size_list<float32>[dim], text, object, record_id, field, system_modstamp` (`:79-89`).

**State & side effects**
- **Network egress: `POST {EMBED_VIA}/embeddings`** — `sync-worker/syncworker/rag_index.py:47-50`,
  i.e. `http://vllm-embed:30003/v1/embeddings` (`docker-compose.yml:314`).
  **This is the only GPU-model call in the whole sync-worker** — it runs `Qwen/Qwen3-Embedding-0.6B`
  on the DGX Spark.
- **Filesystem/DB writes**: LanceDB dataset under `LANCEDB_DIR` — `create_table` (`:89`),
  `delete` (`:142`), `add` (`:144`). `lancedb.connect(self._dir)` (`:70`) creates the directory.
- Instance mutation of `self._db` (`:64,70`). No env reads (values are injected).

**Dependencies** — inbound: `sync-worker/syncworker/main.py:21` (`OpenAIEmbedder`, `RagIndexer`),
constructed at `main.py:271-274`, called at `main.py:176`; `sync-worker/tests/test_embeddings.py:11`.
outbound: `logging`, `re`, `httpx`, `.chunking.chunk_text`, and **lazily** `lancedb` (`:68`) and
`pyarrow` (`:74`).

**Config** — none read directly. `base_url` ← `EMBED_VIA`, `model` ← `EMBED_MODEL`,
`lancedb_dir` ← `LANCEDB_DIR` (`sync-worker/syncworker/config.py:44-46` → `main.py:271-274`).

**Failure modes**
- **No re-embedding guard whatsoever.** `index_records` never compares the incoming text against what is
  already in LanceDB — there is no content hash, no `system_modstamp` comparison, no chunk-level dedup.
  Any change to *any* field of a record bumps `SystemModstamp`, so the record is re-fetched, and every one
  of its long-text chunks is re-embedded on the GPU. See finding F-07.
- **One LanceDB `delete()` per record** (`:141-142`) — see finding F-04b.
- **`dim` is only honoured at creation** (`:73-89`). Changing `EMBED_MODEL` to a different-dimension model
  leaves the old `chunks` table in place; `table.add(rows)` (`:144`) then fails on a schema mismatch, and
  `sync-worker/syncworker/main.py:174-184` swallows it forever.
- **No retry, no backoff on the embed POST** (`:47-51`). A single 503 from a reloading vLLM aborts the
  whole batch's indexing.
- **Timeout is bounded but very long**: `httpx.Client(timeout=300.0)` (`:41`) — 5 minutes *per 32-text
  batch*, with no overall deadline across the (unbounded) number of batches.
- `resp.json()["data"]` and `item["embedding"]` (`:52`) raise `KeyError` on an unexpected body.
- **Nothing is swallowed inside this module** — but every exception it raises is swallowed one level up
  at `sync-worker/syncworker/main.py:174-184`, which then lets the watermark advance anyway
  (`main.py:188`). See finding F-04.
- **Filter injection is guarded**: `record_id` is validated `^[a-zA-Z0-9]{15,18}$` (`:26,108`) before
  being interpolated into the LanceDB filter string at `:142`, so no quote can reach the predicate.
  The guard is correct but fragile — it is the only thing between config-driven data and a string-built filter.
- No upper bound on `len(rows)` or on total chunk text size before calling `embed` (`:128`).

**Concurrency** — synchronous, called inline from the batch loop
(`sync-worker/syncworker/main.py:174-176`), so **GPU embedding blocks Salesforce pagination**: the Bulk
result locator and the REST `nextRecordsUrl` stay open while thousands of embeddings are computed.
`self._db` is per-instance lazy state (`:64-71`); the single `RagIndexer` created at `main.py:271` is
shared across all 48 objects and all cycles for the process lifetime. No locks; single-threaded.

**Complexity hotspots** — `index_records` = 58 LOC (`sync-worker/syncworker/rag_index.py:97-154`),
cyclomatic ≈ 12 (2 nested loops + 6 conditionals + 2 more loops). **Over the complexity threshold**,
just under the LOC threshold.

**Notable**
- Magic numbers: `EMBED_BATCH_SIZE = 32` (`:25`, not configurable), `timeout=300.0` (`:41`),
  `{15,18}` ID length (`:26` — accepts 16- and 17-char strings that are not valid Salesforce Ids).
- The module docstring (`:1-11`) accurately describes the delete-then-reinsert design and the lazy
  `lancedb` import.
- The `chunks` table has no vector index created anywhere in this repo (`:79-89` only defines a schema),
  so LanceDB search is a brute-force scan.

---

### sync-worker/syncworker/chunking.py  (41 LOC)

**Purpose** — Splits long text into overlapping "token" windows for embedding. "Tokens" are
whitespace-separated words.

**Public surface**
- `DEFAULT_CHUNK_TOKENS = 800` — `sync-worker/syncworker/chunking.py:10`
- `DEFAULT_OVERLAP_TOKENS = 100` — `sync-worker/syncworker/chunking.py:11`
- `chunk_text(text, chunk_tokens=800, overlap_tokens=100) -> list[str]` — `sync-worker/syncworker/chunking.py:14`

**Control flow**
1. Reject `chunk_tokens <= 0` — `sync-worker/syncworker/chunking.py:25-26`.
2. Reject `overlap_tokens < 0 or >= chunk_tokens` — `sync-worker/syncworker/chunking.py:27-28`.
3. `tokens = text.split()` — `sync-worker/syncworker/chunking.py:30`. Returns `[]` for empty/whitespace — `:31-32`.
4. `step = chunk_tokens - overlap_tokens` (700 by default) — `sync-worker/syncworker/chunking.py:34`.
5. Slide `for start in range(0, len(tokens), step)`, append `" ".join(tokens[start:start+chunk_tokens])`,
   break once `start + chunk_tokens >= len(tokens)` — `sync-worker/syncworker/chunking.py:36-40`.

**State & side effects** — pure function. No I/O of any kind.

**Dependencies** — inbound: `sync-worker/syncworker/rag_index.py:20` (called at `rag_index.py:116`),
`sync-worker/tests/test_chunking.py:3`. outbound: none (stdlib-free).

**Config** — none. `chunk_tokens`/`overlap_tokens` are **not** exposed as env vars anywhere; the
`800`/`100` defaults are the only values ever used at the call site (`rag_index.py:116` passes neither).

**Failure modes**
- Raises `ValueError` for invalid sizes (`:26,28`). Nothing swallowed. No bare `except`.
- **`text.split()` is whitespace-only** (`:30`). Text with little or no whitespace collapses to very few
  "tokens", so the chunk cap never triggers. Minified JSON is the pathological case, and
  `config.yaml` deliberately indexes 12 JSON-bearing fields as `rag_fields`
  (`sync-worker/config.yaml:547,781-787,791-794,803-816,823-827,831-834,838-841,845-852`).
  A 100 KB minified JSON blob with no spaces is **one token → one 100 KB chunk** → one embedding request
  far past the model's context window. See finding F-09.
- Conversely, whitespace-heavy text (indented JSON, HTML from a Rich Text Area) explodes token counts:
  800 whitespace-words ≠ 800 model tokens, so a "chunk" can still overflow the embedder.
- No cap on the number of chunks produced from one field.

**Concurrency** — pure/stateless. Safe anywhere.

**Complexity hotspots** — none. `chunk_text` = 28 LOC, cyclomatic ≈ 6.

**Notable**
- The docstring at `:1-6` is honest that tokens are approximated by whitespace words.
- Magic numbers `800` / `100` (`:10-11`); their ratio (`step = 700`) is verified by
  `sync-worker/tests/test_chunking.py:39-50`.
- Overlap semantics are exactly as documented and well covered by tests.

---

### sync-worker/syncworker/main.py  (305 LOC)

**Purpose** — The sync loop entrypoint: signal handling, per-object full/incremental sync, field
adoption, new-object reporting, exponential backoff.

**Public surface**
- `INITIAL_BACKOFF_SECONDS = 30.0` — `sync-worker/syncworker/main.py:29`
- `MAX_BACKOFF_SECONDS = 30 * 60.0` — `sync-worker/syncworker/main.py:30`
- `class _StopFlag` — `sync-worker/syncworker/main.py:33`
  - `install()` — `sync-worker/syncworker/main.py:37` (registers SIGTERM + SIGINT — `:38-39`)
  - `_handle(signum, frame)` — `sync-worker/syncworker/main.py:41`
  - `sleep(seconds: float)` — `sync-worker/syncworker/main.py:45`
- `COMPOUND_TYPES = ("address","location")` — `sync-worker/syncworker/main.py:54`
- `LONG_TEXT_TYPES = ("textarea","richtextarea")` — `sync-worker/syncworker/main.py:57`
- `_NOISE_SUFFIXES = ("__History","__Share","__Feed")` — `sync-worker/syncworker/main.py:60`
- `adopt_new_fields(object_name, fields, rag_fields, client, settings) -> tuple[list,list]` — `sync-worker/syncworker/main.py:63`
- `sync_object(obj, client, store, indexer, settings) -> int` — `sync-worker/syncworker/main.py:106`
- `report_new_objects(objects, client) -> list[str]` — `sync-worker/syncworker/main.py:197`
- `run_cycle(objects, client, store, indexer, settings) -> None` — `sync-worker/syncworker/main.py:227`
- `main() -> None` — `sync-worker/syncworker/main.py:259`
- `if __name__ == "__main__": main()` — `sync-worker/syncworker/main.py:304-305`

**Control flow** — `main()` (`:259-301`):
1. `setup_logging()` — `:260`.
2. `settings = load_settings()` — `:261`; `objects = load_object_configs(settings.config_path)` — `:262`.
   **Both outside the try/except** at `:281`.
3. Log `event=startup` with object names — `:263-267`.
4. `creds = fetch_sf_credentials()` — `:269`; build `SalesforceClient(TokenManager(creds), api_version)` — `:270`;
   build `RagIndexer(lancedb_dir, OpenAIEmbedder(embed_via, embed_model))` — `:271-274`.
   **All three are created ONCE for the process lifetime.**
5. `_StopFlag().install()` — `:276-277`.
6. Loop `while not flag.stop` — `:280`:
   a. `store = Store(settings.duckdb_path)` — `:285` (fresh connection every cycle).
   b. `run_cycle(...)` in `try` / `finally: store.close()` — `:286-289`.
   c. Reset `backoff`, `flag.sleep(interval_minutes * 60)` — `:290-291`.
   d. `except Exception:` → log `event=cycle_error` with traceback (`:293-297`) →
      `flag.sleep(backoff)` → `backoff = min(backoff*2, 1800)` — `:298-299`.
7. Log `event=stopped` — `:301`.

`run_cycle` (`:227-256`): optional `report_new_objects` (`:235-236`) → per object
`try: sync_object(...) except Exception: append to failed, log event=object_sync_error` (`:239-250`)
→ log `event=cycle_done` with `failed_objects` and elapsed seconds (`:251-256`).

`sync_object` (`:106-194`):
1. `watermark = store.get_watermark(obj.name)` — `:114`.
2. **`cycle_start = sf_datetime_literal(datetime.now(timezone.utc))`** — `:115`. This is the LOCAL
   worker clock, captured *before* any network call.
3. `visible = client.describe_fields(obj.name)` in `try` (`:120-121`); on any exception log
   `event=describe_failed` and keep the configured fields (`:122-126`).
4. Else drop invisible fields from both `fields` and `rag_fields`, logging `event=fields_skipped` — `:128-136`.
5. If `settings.sync_auto_fields`, `adopt_new_fields(...)` — `:142-145`.
6. **Mode selection** — `:147-154`: `watermark is None` → `client.bulk_query(build_full_soql(...))`;
   else → `client.soql_query(build_incremental_soql(..., watermark))`.
7. Log `event=object_sync_start` — `:156-160`.
8. `for batch in batches:` — `:163-184`:
   `normalize_records` (`:164`) → `pd.DataFrame` (`:165`) → `write_parquet_batch` (`:166`) →
   `store.upsert` (`:167`) → log `event=batch_stored` (`:169-173`) →
   `indexer.index_records(...)` wrapped in `try/except Exception: log event=rag_index_error` (`:174-184`).
9. **`store.set_watermark(obj.name, cycle_start)`** — `:188`. Log `event=object_sync_done` — `:189-193`.

`adopt_new_fields` (`:63-103`): `client.describe_field_types` in a **broad `try/except Exception:
return fields, rag_fields`** (`:79-82`) → skip known/compound/noise-suffixed names (`:87-90`) →
stop at `settings.sync_max_fields` (`:91-92`) → collect, adding `textarea`/`richtextarea` to
`rag_fields` (`:93-95`) → log `event=fields_adopted` (`:97-102`).

`report_new_objects` (`:197-224`): `client.list_objects()` in a **broad `try/except Exception: return []`**
(`:208-211`) → keep only names ending `__c` not already configured (`:212-216`) → log
`event=new_objects_available` truncated to the first 25 (`:217-223`).

**State & side effects**
- **DB writes** via `Store` (see storage.py): DuckDB at `DUCKDB_PATH` — `main.py:167,188,285`.
- **Filesystem writes**: Parquet under `PARQUET_DIR` — `main.py:166`; LanceDB under `LANCEDB_DIR`
  — `main.py:176`.
- **Network egress**: Salesforce (via `SalesforceClient`) — `main.py:121,143,149,153,209`;
  the vLLM embedding endpoint (via `RagIndexer`) — `main.py:176`.
- **GPU/model call**: the embedding request behind `main.py:176`.
- **Global mutation**: `setup_logging()` replaces root handlers (`:260` →
  `sync-worker/syncworker/jsonlog.py:38`); `signal.signal` installs process-wide handlers (`:38-39`).
- **Env reads**: all indirect via `load_settings()` (`:261`) and `fetch_sf_credentials()` (`:269`).

**Dependencies** — inbound: `Dockerfile:34` (`CMD ["python","-m","syncworker.main"]`),
`sync-worker/tests/test_discovery.py:11` (imports `COMPOUND_TYPES`, `adopt_new_fields`,
`report_new_objects`). Nothing else in the repo imports it. outbound: `logging`, `signal`, `time`,
`datetime`, `pandas`, and the six sibling modules (`:19-25`).

**Config** — no direct `os.getenv`. Consumes `Settings` fields: `config_path` (`:262`),
`sync_interval_minutes` (`:266,291`), `sf_api_version` (`:270`), `lancedb_dir`/`embed_via`/`embed_model`
(`:272-273`), `duckdb_path` (`:285`), `parquet_dir` (`:166`), `sync_auto_fields` (`:142`),
`sync_max_fields` (`:91`), `sync_report_new_objects` (`:235`).

**Failure modes**
- **Swallowed, by design, with consequences**:
  - `except Exception: return fields, rag_fields` — `:81-82` (describe failure during adoption).
  - `except Exception:` → `event=describe_failed` — `:122-126`.
  - `except Exception:` → `event=rag_index_error` — `:177-184`. **The watermark still advances at `:188`.**
  - `except Exception: return []` — `:210-211` (object listing).
  - `except Exception:` per object — `:244-250` (one object's failure does not stop the cycle).
  - `except Exception:` per cycle — `:292-299` (whole-cycle failure → exponential backoff).
  All five log with `exc_info=True` except `:81-82` and `:210-211`, which log **nothing at all** — a
  permanently broken `describe` silently degrades field adoption with zero telemetry.
- **`load_settings()`/`load_object_configs()` at `:261-262` are outside the try** → a bad
  `SYNC_INTERVAL_MINUTES` or malformed `config.yaml` crashes the process at startup. Combined with the
  absence of `restart:` on the `sync-worker` service (`docker-compose.yml:291-334`; every other service
  has one at `docker-compose.yml:89,134,172,203,343`), the container simply stays dead.
- **No shutdown responsiveness inside a cycle**: `flag.stop` is only read by `_StopFlag.sleep` (`:48`)
  and the outer `while` (`:280`). Neither `run_cycle`, `sync_object`, the Bulk poll loop
  (`sync-worker/syncworker/sf_client.py:208-220`) nor the embedding loop checks it. A `docker stop`
  during a full extract waits out the grace period and then SIGKILLs.
- **No timeout on a cycle as a whole.** A single object stuck in `bulk_query` blocks all other objects
  indefinitely (`:239-243` is a plain sequential loop).
- **No health/liveness signal** — the Dockerfile has no `HEALTHCHECK` (`sync-worker/Dockerfile:1-34`) and
  the compose service defines none (`docker-compose.yml:291-334`).
- Interval drift: the sleep at `:291` starts *after* the cycle, so the effective period is
  `interval + cycle_duration`.

**Concurrency** — single-threaded, fully synchronous. No `async def` anywhere in the package
(verified: no `async` keyword in any `syncworker/*.py` file I read). Shared mutable state that persists
across cycles: `TokenManager._access_token/_instance_url/_obtained_at`,
`SalesforceClient._describe_cache`, `RagIndexer._db` — all created at `:270-274` and never reset.
Signal handlers mutate `_StopFlag.stop` from the signal context (`:41-43`) — safe, single boolean.

**Complexity hotspots**
- **`sync_object` = 89 LOC** — `sync-worker/syncworker/main.py:106-194`. Cyclomatic ≈ 13
  (try/except/else, 2 `if`s on dropped fields, adoption branch, mode branch, batch loop, indexer
  branch, inner try/except). **Over both thresholds.**
- `adopt_new_fields` = 41 LOC (`:63-103`), cyclomatic ≈ 9.
- `main` = 47 LOC (`:259-301`), cyclomatic ≈ 5.

**Notable**
- Magic numbers: `30.0` / `30*60.0` backoff (`:29-30`), `1.0` s sleep granularity (`:49`),
  `new[:25]` log truncation (`:222`), `round(..., 1)` (`:255`).
- `COMPOUND_TYPES` (`:54`) and `LONG_TEXT_TYPES` (`:57`) are **duplicated verbatim** in
  `sync-worker/syncworker/objects.py:161,164` — two sources of truth for the same Salesforce type policy.
- `report_new_objects` only ever reports `__c` objects (`:215`) although its log message says
  "Salesforce objects not yet synced" (`:219`) — standard objects are silently never surfaced.
- `_handle(self, signum, frame)` carries `# noqa: ARG002` (`:41`) — the only lint suppression in the package.
- `adopt_new_fields` iterates `types.items()` (`:86`), i.e. whatever order the describe response used, so
  *which* fields get adopted when the `sync_max_fields` cap bites (`:91-92`) is arbitrary and undocumented.

---

### sync-worker/syncworker/objects.py  (394 LOC)

**Purpose** — CLI (`python -m syncworker.objects`) to list/add/remove synced objects and to import an
org "Objects, Fields" spreadsheet, intersecting it with what the integration user can actually read.

**Public surface**
- `_IDENT_RE` — `sync-worker/syncworker/objects.py:36`
- `REQUIRED_FIELDS = ("Id","SystemModstamp")` — `sync-worker/syncworker/objects.py:41`
- `DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"` — `sync-worker/syncworker/objects.py:43`
- `class ConfigError(Exception)` — `sync-worker/syncworker/objects.py:46`
- `_split_header(text) -> tuple[str, dict]` — `sync-worker/syncworker/objects.py:50`
- `load(path=DEFAULT_CONFIG) -> tuple[str, List[dict]]` — `sync-worker/syncworker/objects.py:61`
- `_validate(entry: dict) -> None` — `sync-worker/syncworker/objects.py:71`
- `_ordered(fields) -> List[str]` — `sync-worker/syncworker/objects.py:92`
- `_dedupe(items) -> List[str]` — `sync-worker/syncworker/objects.py:101`
- `upsert_object(objects, name, fields, rag_fields=(), replace=True) -> List[dict]` — `sync-worker/syncworker/objects.py:111`
- `remove_object(objects, name) -> List[dict]` — `sync-worker/syncworker/objects.py:133`
- `dump(header, objects) -> str` — `sync-worker/syncworker/objects.py:142`
- `save(header, objects, path=DEFAULT_CONFIG) -> None` — `sync-worker/syncworker/objects.py:149`
- `COMPOUND_TYPES` — `sync-worker/syncworker/objects.py:161`
- `LONG_TEXT_TYPES` — `sync-worker/syncworker/objects.py:164`
- `MAX_FIELDS_PER_OBJECT = 60` — `sync-worker/syncworker/objects.py:168`
- `parse_sheet(path: Path) -> Dict[str, List[str]]` — `sync-worker/syncworker/objects.py:171`
- `plan_from_sheet(sheet, describe, existing=None) -> tuple[List[dict], List[str]]` — `sync-worker/syncworker/objects.py:194`
- `_live_describe()` — `sync-worker/syncworker/objects.py:271`
- `_csv(value) -> List[str]` — `sync-worker/syncworker/objects.py:294`
- `main(argv=None) -> int` — `sync-worker/syncworker/objects.py:298`
- `if __name__ == "__main__": raise SystemExit(main())` — `sync-worker/syncworker/objects.py:393-394`

**Control flow** — `main` (`:298-390`):
1. Build the parser; `--config` is registered on **both** a parent parser and the root parser
   (`:305-307`) so it works before or after the subcommand; `required=True` subparsers (`:308`).
2. Subcommands: `list` (`:310`), `add` (`:312-318`), `add-fields` (`:320-324`), `remove` (`:326-327`),
   `import-sheet` (`:330-336`).
3. `header, objects = load(args.config)` inside a `try` — `:340-341`.
4. `list` → print name / field count / rag fields, return 0 — `:343-348`.
5. `import-sheet` → `parse_sheet` (`:351`) → `plan_from_sheet(sheet, _live_describe(), objects)` (`:352`)
   → print counts and notes (`:353-358`) → raise if nothing usable (`:359-360`) → honour `--dry-run`
   (`:361-363`) → **`objects = entries`** (`:364`), i.e. a wholesale replacement.
6. `add` → `upsert_object(..., replace=True)` — `:366-368`.
7. `add-fields` → existence check (`:370-371`) then `upsert_object(..., replace=False)` — `:372-375`.
8. `remove` → `remove_object` — `:377-378`.
9. `save(header, objects, args.config)` — `:380` → print the action and the restart instructions
   (`:381-386`) → return 0.
10. `except ConfigError as exc:` → print to stderr, return 2 — `:388-390`.

`plan_from_sheet` (`:194-268`): merge prior fields under each sheet object (`:213-217`); carry over
objects present in the config but absent from the sheet (`:218-221`); per object call `describe(name)`
and **skip when it returns `None` or is not `queryable`** (`:226-229`); split wanted fields into
keep/blocked/compound (`:232-240`); skip the object entirely when nothing is visible (`:242-247`);
trim to `MAX_FIELDS_PER_OBJECT` (`:248-252`); derive `rag` from `LONG_TEXT_TYPES` plus any deliberate
prior rag field still in `keep` (`:254-257`); record notes (`:258-261`); `_validate` and append (`:263-267`).

`save` (`:149-152`): `_validate` every entry, then `path.write_text(dump(header, objects))` — a **single
non-atomic write**.

`_live_describe` (`:271-286`): imports `fetch_sf_credentials`/`TokenManager` lazily (`:273-274`),
gets one token (`:278-279`), builds an `httpx.Client(timeout=60.0)` with a **static** `Authorization`
header (`:280`), and returns a closure that does
`GET {instance}/services/data/v61.0/sobjects/{name}/describe` and returns
`resp.json() if resp.status_code == 200 else None` (`:282-284`).

**State & side effects**
- **Filesystem writes**: `path.write_text(...)` in `save` — `sync-worker/syncworker/objects.py:152`
  (target defaults to `sync-worker/config.yaml` via `:43`).
- **Filesystem reads**: `path.read_text` in `load` (`:64`), `open(path, ...)` in `parse_sheet` (`:180`).
- **Network egress**: `GET {instance}/services/data/v61.0/sobjects/{name}/describe` — `:283`, plus the
  token POST triggered by `TokenManager.get_token()` at `:279`.
- **stdout writes** — `:347,354-358,362,381-386`; **stderr** — `:389`.
- No DB, no GPU, no global mutation.

**Dependencies** — inbound: documented in `README.md:217-231`, referenced by the orchestrator's SQL
error message at `orchestrator/app/engines/sql.py:311` and by
`sync-worker/syncworker/main.py:206,219-220` (log text only, not a code call);
`sync-worker/tests/test_objects_cli.py:11`. outbound: `argparse`, `csv`, `collections`, `re`, `sys`,
`pathlib`, `typing`, `yaml`, and lazily `.secrets`, `.sf_auth`, `httpx` (`:273-276`).

**Config** — **no env vars read directly.** `_live_describe` inherits everything through
`fetch_sf_credentials()` (`:273,278`). The API version is **hardcoded `v61.0`** at `:283`, ignoring
`SF_API_VERSION` (`sync-worker/syncworker/config.py:47`).

**Failure modes**
- **`_live_describe` conflates every non-200 with "object not readable"** (`:284`). A 401, 403
  `REQUEST_LIMIT_EXCEEDED`, 429 or 500 all become `None`, which `plan_from_sheet:227-229` turns into
  "not readable by this user — skipped", and `main:364` then **writes a config with that object deleted**.
  See finding F-11.
- `save` is non-atomic (`:152`) — a crash or full disk mid-write truncates `config.yaml`, after which
  `load_object_configs` (`sync-worker/syncworker/config.py:65-66`) refuses to start the worker.
  No backup is taken (the `config.yaml.bak` on disk is stale and unrelated — see below).
- The `except ConfigError` at `:388` catches **only** `ConfigError`. `yaml.YAMLError`, `OSError`,
  `httpx.ConnectError` from `_live_describe`, `KeyError` from `plan_from_sheet:213` (`e["name"]`) and
  `RuntimeError` from the token request all escape as tracebacks with exit code 1.
- `_live_describe` fetches the token **once** (`:279`) and pins it into the client headers (`:280`);
  a long `import-sheet` run over hundreds of objects will start 401-ing with no refresh path.
- No retry, no rate-limit handling, no concurrency limit on the describe loop
  (`:226` inside the `for` at `:225`) — one HTTP round trip per object, serially.
- `plan_from_sheet:213` `{e["name"]: e for e in (existing or [])}` raises `KeyError` on a config entry
  without a `name`; `load` (`:61-68`) does not validate entries.
- `parse_sheet` (`:171-191`) assumes column 0 = object, column 1 = field and blindly skips the first row
  (`:182`); a differently-shaped CSV silently yields nonsense rather than an error.
- **`main:385-386` prints a false statement**: *"The next cycle does a FULL extract for changed objects,
  then returns to incremental syncs."* Nothing in this file or in `storage.py` clears the watermark, and
  `sync_object` only does a full extract when `get_watermark()` returns `None`
  (`sync-worker/syncworker/main.py:147`). See finding F-12.

**Concurrency** — synchronous CLI, single process, no shared state. Not safe to run concurrently with
itself or with a `config.yaml` edit: `load` → mutate → `save` is a read-modify-write with no locking
(`:341` … `:380`).

**Complexity hotspots**
- **`main` = 93 LOC** — `sync-worker/syncworker/objects.py:298-390`. Cyclomatic ≈ 15
  (5 subcommand branches + dry-run + empty-entries + rag branch in `list` + the except).
  **This is the largest function in the assigned scope.** Over both thresholds.
- **`plan_from_sheet` = 75 LOC** — `sync-worker/syncworker/objects.py:194-268`. Cyclomatic ≈ 16
  (3 merge loops + per-field 3-way split + 4 skip/trim conditions + 2 note conditions).
  Over both thresholds.

**Notable**
- Magic numbers: `MAX_FIELDS_PER_OBJECT = 60` (`:168`) — **inconsistent with**
  `SYNC_MAX_FIELDS` default `80` (`sync-worker/syncworker/config.py:39`), so an imported object is capped
  at 60+2 fields while runtime adoption will immediately widen it back toward 80.
- `COMPOUND_TYPES` (`:161`) / `LONG_TEXT_TYPES` (`:164`) duplicate `sync-worker/syncworker/main.py:54,57`.
- `_IDENT_RE` (`:36`) duplicates `sync-worker/syncworker/config.py:11` and `sync-worker/syncworker/sf_client.py:34`.
- Hardcoded `v61.0` (`:283`).
- `_validate` (`:71-89`) re-implements the same rules as `load_object_configs`
  (`sync-worker/syncworker/config.py:73-82`) with different error types — `tests/test_objects_cli.py:156-164`
  exists precisely to keep the two in sync.
- The comment-header preservation trick (`_split_header` at `:50-58`, `dump` at `:142-146`) is genuinely
  necessary and is tested at `tests/test_objects_cli.py:147-153`.

---

### sync-worker/config.yaml  (852 LOC)  ← structure and every configured object

**Purpose** — The single source of truth for which Salesforce objects/fields are extracted, and which
long-text fields are chunked + embedded into LanceDB.

**Structure**
- Lines `1-29`: comment header only — per-object key documentation (`sync-worker/config.yaml:3-10`) and a
  commented `Project__c` template (`sync-worker/config.yaml:12-28`). This block is what
  `sync-worker/syncworker/objects.py:50-58` preserves verbatim.
- Line `30`: the single top-level key `objects:`.
- Lines `31-852`: a flat YAML sequence. Each entry has `name:` (str), `fields:` (list[str]), and
  optionally `rag_fields:` (list[str]).

**Measured totals** (computed with `yaml.safe_load` over the file):
**48 objects · 631 fields · 61 rag_fields across 34 objects.**
14 objects have no `rag_fields` key at all.

**Every object configured** (name · field count · rag field count · rag fields):

| # | Object | start line | fields | rag | rag_fields |
|---|---|---|---|---|---|
| 1 | `Account` | `sync-worker/config.yaml:31` | 19 | 1 | Description |
| 2 | `AccountContactRelation` | `sync-worker/config.yaml:54` | 7 | 0 | — |
| 3 | `Asset` | `sync-worker/config.yaml:63` | 20 | 1 | Description |
| 4 | `AssetRelationship` | `sync-worker/config.yaml:87` | 3 | 0 | — |
| 5 | `Campaign` | `sync-worker/config.yaml:92` | 22 | 1 | Description |
| 6 | `Candidate_Training__c` | `sync-worker/config.yaml:118` | 4 | 0 | — |
| 7 | `Case` | `sync-worker/config.yaml:124` | 22 | 2 | Description, Subject |
| 8 | `ChangeRequest` | `sync-worker/config.yaml:151` | 14 | 5 | BusinessJustification, Description, FinalReviewNotes, RemediationPlan, RiskImpactAnalysis |
| 9 | `ChangeRequestRelatedItem` | `sync-worker/config.yaml:173` | 3 | 1 | Comment |
| 10 | `Contact` | `sync-worker/config.yaml:180` | 19 | 0 | — |
| 11 | `Contract` | `sync-worker/config.yaml:201` | 16 | 2 | Description, SpecialTerms |
| 12 | `Idea` | `sync-worker/config.yaml:222` | 3 | 0 | — |
| 13 | `Incident` | `sync-worker/config.yaml:227` | 24 | 2 | Description, ResolutionSummary |
| 14 | `IncidentRelatedItem` | `sync-worker/config.yaml:256` | 3 | 1 | Comment |
| 15 | `Interview__c` | `sync-worker/config.yaml:263` | 41 | 9 | Candidate_Feedback__c, Client_Feedback__c, Description__c, If_yes_why__c, Job_Description__c, Support_not_provided_reason__c, Support_Notes__c, Vendor_Feedback__c, Why_not_eligible__c |
| 16 | `Invoice__c` | `sync-worker/config.yaml:316` | 4 | 0 | — |
| 17 | `Lead` | `sync-worker/config.yaml:322` | 26 | 0 | — |
| 18 | `Marketing__c` | `sync-worker/config.yaml:350` | 14 | 2 | Reason_for_paused__c, Reason_for_Stop__c |
| 19 | `Onboarding__c` | `sync-worker/config.yaml:369` | 18 | 0 | — |
| 20 | `Opportunity` | `sync-worker/config.yaml:389` | 25 | 2 | Description, NextStep |
| 21 | `OpportunityLineItem` | `sync-worker/config.yaml:419` | 7 | 0 | — |
| 22 | `Order` | `sync-worker/config.yaml:428` | 9 | 1 | Description |
| 23 | `OrderItem` | `sync-worker/config.yaml:441` | 6 | 0 | — |
| 24 | `Problem` | `sync-worker/config.yaml:449` | 11 | 3 | Description, ResolutionSummary, RootCauseSummary |
| 25 | `ProblemRelatedItem` | `sync-worker/config.yaml:466` | 3 | 1 | Comment |
| 26 | `Product2` | `sync-worker/config.yaml:473` | 10 | 1 | Description |
| 27 | `Quote` | `sync-worker/config.yaml:487` | 21 | 1 | Description |
| 28 | `QuoteLineItem` | `sync-worker/config.yaml:512` | 8 | 0 | — |
| 29 | `Recruiter__c` | `sync-worker/config.yaml:522` | 22 | 2 | **COMPASS_State_JSON__c**, Description__c |
| 30 | `SocialPersona` | `sync-worker/config.yaml:549` | 21 | 1 | Bio |
| 31 | `SocialPost` | `sync-worker/config.yaml:574` | **51** | 5 | Content, Notes, PostTags, SourceTags, StatusMessage |
| 32 | `Solution` | `sync-worker/config.yaml:633` | 5 | 1 | SolutionNote |
| 33 | `Step_Deliverable_Definition__c` | `sync-worker/config.yaml:642` | 3 | 0 | — |
| 34 | `User` | `sync-worker/config.yaml:647` | 14 | 0 | — |
| 35 | `WorkOrder` | `sync-worker/config.yaml:663` | 36 | 1 | Description |
| 36 | `WorkOrderLineItem` | `sync-worker/config.yaml:703` | 24 | 1 | Description |
| 37 | `WorkPlan` | `sync-worker/config.yaml:731` | 5 | 1 | Description |
| 38 | `WorkPlanTemplate` | `sync-worker/config.yaml:740` | 5 | 1 | Description |
| 39 | `WorkPlanTemplateEntry` | `sync-worker/config.yaml:749` | 3 | 0 | — |
| 40 | `WorkStep` | `sync-worker/config.yaml:754` | 10 | 1 | Description |
| 41 | `WorkStepTemplate` | `sync-worker/config.yaml:768` | 6 | 1 | Description |
| 42 | `pandadoc__DocStatus__c` | `sync-worker/config.yaml:778` | 5 | 2 | **pandadoc__InputJSON__c, pandadoc__InputJSON_EV2__c** |
| 43 | `pandadoc__Object_Tokens__c` | `sync-worker/config.yaml:788` | 3 | 1 | **pandadoc__Data__c** |
| 44 | `pandadoc__PandaDocDocument__c` | `sync-worker/config.yaml:795` | 17 | 2 | **pandadoc__InputJSON__c, pandadoc__InputJSON_EV2__c** |
| 45 | `pandadoc__PandaDocLog__c` | `sync-worker/config.yaml:817` | 6 | 2 | pandadoc__Description__c, **pandadoc__Log_Data__c** |
| 46 | `pandadoc__Pricing_Item_Mapping__c` | `sync-worker/config.yaml:828` | 3 | 1 | **pandadoc__Config_JSON__c** |
| 47 | `pandadoc__Recipient_Map__c` | `sync-worker/config.yaml:835` | 3 | 1 | **pandadoc__Config_JSON__c** |
| 48 | `pandadoc__TriggerSetting__c` | `sync-worker/config.yaml:842` | 7 | 1 | **pandadoc__Custom_Settings__c** |

Bold = a field whose name declares it holds JSON/serialised state. **12 of the 61 rag_fields are JSON
blobs** (`sync-worker/config.yaml:547,786-787,794,815-816,827,834,841,852`), which is the input class that
breaks `chunk_text`'s whitespace tokenizer (finding F-09).

**Control flow** — declarative. Consumed by `sync-worker/syncworker/config.py:61-87` at startup
(`sync-worker/syncworker/main.py:262`) and rewritten by `sync-worker/syncworker/objects.py:149-152`.

**State & side effects** — none. Mounted read-only into the container at
`docker-compose.yml:324` (`./sync-worker/config.yaml:/app/config.yaml:ro`) and also baked into the image
at `sync-worker/Dockerfile:16`.

**Dependencies** — inbound: `sync-worker/syncworker/config.py:49` (default path),
`sync-worker/syncworker/objects.py:43` (`DEFAULT_CONFIG`), `sync-worker/tests/test_config.py:5`,
`sync-worker/Dockerfile:16`, `docker-compose.yml:324`. outbound: none.

**Config** — the file *is* config. `SYNC_CONFIG_PATH` selects it
(`sync-worker/syncworker/config.py:48`; set to `/app/config.yaml` at `sync-worker/Dockerfile:32`).

**Failure modes**
- Validation is enforced by `load_object_configs`: every entry must carry `Id` and `SystemModstamp`
  (`sync-worker/syncworker/config.py:78-79`) and every `rag_field` must appear in `fields`
  (`sync-worker/syncworker/config.py:80-82`). I verified both hold for all 48 entries (the file loads
  without raising and `tests/test_config.py:26-35` asserts it).
- **The mount at `docker-compose.yml:324` is `:ro`**, so `python -m syncworker.objects add …` executed
  *inside* the container (as `README.md:217-231` instructs) will fail with a read-only filesystem error
  when it reaches `sync-worker/syncworker/objects.py:152`. Only `list` works in-container.
- No `describe`-time validation: a field renamed or deleted in Salesforce is silently dropped at runtime
  by `sync-worker/syncworker/main.py:128-136` with a `fields_skipped` warning.
- Nothing bounds the total: 631 fields × 48 objects × 48 cycles/day is the API-call driver, and there is
  no per-object enable/disable or schedule.

**Concurrency** — read once per process start (`sync-worker/syncworker/main.py:262`); **never re-read**,
so an edit to the live-mounted file has no effect until the container restarts (which is exactly what
`sync-worker/syncworker/objects.py:382-383` tells the user to do).

**Complexity hotspots** — n/a (data).

**Notable**
- **Header drift**: line 1 says *"the eight standard objects synced in Phase 1"* —
  `sync-worker/config.yaml:1` — but there are **48**. The same stale sentence is in
  `sync-worker/config.yaml.bak:1`.
- `Account` no longer carries `AnnualRevenue` (present at `sync-worker/config.yaml.bak:37`, absent from
  `sync-worker/config.yaml:31-53`) — a revenue metric silently lost between the two versions.
- `Contact` and `Lead` lost their `Description` field and their `rag_fields` entirely
  (`sync-worker/config.yaml.bak:68-70,94-96` vs. `sync-worker/config.yaml:180-200,322-349`).
  Contact/Lead descriptions are therefore no longer semantically searchable.
- `Interview__c` (`sync-worker/config.yaml:263`) contains apparent duplicate-purpose fields —
  `End_Time__c`/`EndTime__c` (`:275-276`) and `Start_Time__c`/`StartTime__c` (`:297-298`).
- `SocialPost` at 51 fields (`sync-worker/config.yaml:574`) is already under the
  `SYNC_MAX_FIELDS=80` adoption ceiling, so runtime adoption will keep widening it by up to 29 more
  arbitrary fields (`sync-worker/syncworker/main.py:91-95`).

---

### sync-worker/config.yaml.bak  (158 LOC)

**Purpose** — A stale snapshot of `config.yaml` from before the org import: the original 6-object
Phase-1 list.

**Public surface** — the same declarative schema. Objects, in order:
`Account` (`sync-worker/config.yaml.bak:31`, 15 fields, rag: Description),
`Contact` (`:51`, 15 fields, rag: Description),
`Lead` (`:71`, 21 fields, rag: Description),
`Opportunity` (`:97`, 18 fields, rag: Description + NextStep),
`Case` (`:120`, 18 fields, rag: Subject + Description),
`User` (`:142`, 14 fields, `rag_fields: []` at `:158`).

**Control flow** — none. **Nothing in the repo reads this file** — verified: no match for
`config.yaml.bak` anywhere outside itself.

**State & side effects** — none.

**Dependencies** — inbound: **none** (dead file). outbound: none.

**Config** — none.

**Failure modes** — none directly. It is a **trap**: it looks like a rollback target but reverting to it
would delete 42 objects and 476 fields from the sync, and would leave 42 orphan DuckDB tables plus 42
stale `_sync_meta` watermark rows (`sync-worker/syncworker/storage.py:99-106` never deletes rows).

**Concurrency** — n/a.

**Complexity hotspots** — n/a.

**Notable**
- Gitignored via `.gitignore:46` (`*.bak`), so it exists only on this machine — an untracked,
  unreviewed artefact sitting next to the live config.
- Its `User` entry uses the explicit form `rag_fields: []` (`sync-worker/config.yaml.bak:158`), which the
  current file never does — `sync-worker/syncworker/config.py:72` handles both (`entry.get(...) or []`).
- Field ordering differs from the live file: `SystemModstamp` is mid-list here
  (`sync-worker/config.yaml.bak:47`) whereas `_ordered` (`sync-worker/syncworker/objects.py:92-98`) now
  forces it last — evidence this file predates the CLI.

---

### sync-worker/conftest.py  (5 LOC)

**Purpose** — Makes the `syncworker` package importable when pytest is run from `sync-worker/`.

**Public surface** — none (no functions or fixtures).

**Control flow**
1. `import os, sys` — `sync-worker/conftest.py:1-2`.
2. `sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))` — `sync-worker/conftest.py:5`.

**State & side effects** — **global mutation of `sys.path`** at import time —
`sync-worker/conftest.py:5`. No other effects.

**Dependencies** — inbound: pytest auto-discovery for everything under `sync-worker/tests/`.
outbound: `os`, `sys`.

**Config** — none.

**Failure modes** — none. Nothing raises, nothing is swallowed.

**Concurrency** — import-time only.

**Complexity hotspots** — none.

**Notable**
- Prepending to `sys.path[0]` shadows any installed `syncworker` distribution — intentional here.
- There is **no `pytest.ini`/`pyproject.toml`/`setup.cfg`** in `sync-worker/`, so there is no
  `--strict-markers`, no coverage config, no warning filters, and no `testpaths`.
- `sync-worker/tests/__init__.py` is **empty (0 LOC)**, making `tests` a package.

---

### sync-worker/Dockerfile  (34 LOC)

**Purpose** — Builds the sync-worker image on `python:3.11-slim`, runs as a non-root user, and sets the
`/data`-based defaults.

**Public surface**
- `FROM python:3.11-slim` — `sync-worker/Dockerfile:3`
- `ENV PYTHONUNBUFFERED / PYTHONDONTWRITEBYTECODE / PIP_NO_CACHE_DIR / PIP_DISABLE_PIP_VERSION_CHECK` — `sync-worker/Dockerfile:5-8`
- `WORKDIR /app` — `sync-worker/Dockerfile:10`
- `COPY requirements.txt ./` + `RUN pip install -r requirements.txt` — `sync-worker/Dockerfile:13-14`
- `COPY config.yaml ./` — `sync-worker/Dockerfile:16`; `COPY syncworker ./syncworker` — `sync-worker/Dockerfile:17`
- `RUN useradd --create-home --uid 10001 worker && mkdir -p /data && chown -R worker:worker /data /app` — `sync-worker/Dockerfile:20-22`
- `USER worker` — `sync-worker/Dockerfile:23`
- `CMD ["python", "-m", "syncworker.main"]` — `sync-worker/Dockerfile:34`

**Control flow** — standard layered build: deps layer first (`:13-14`) so source changes do not
reinvalidate the pip cache, then source (`:16-17`), then user creation (`:20-23`), then env (`:26-32`).

**State & side effects** — build-time only: pip installs from PyPI (`:14`) — **the one network
dependency of the build**, which conflicts with the "fully local, air-gapped" framing; creates `/data`
(`:21`) and chowns it (`:22`).

**Dependencies** — inbound: `docker-compose.yml:292` (`build: ./sync-worker`). outbound:
`python:3.11-slim`, PyPI.

**Config** — env vars **set** here (defaults, overridden by compose):
`SYNC_INTERVAL_MINUTES=30` (`sync-worker/Dockerfile:26`), `PARQUET_DIR=/data/parquet` (`:27`),
`DUCKDB_PATH=/data/warehouse.duckdb` (`:28`), `LANCEDB_DIR=/data/lancedb` (`:29`),
`EMBED_VIA=http://vllm-embed:30003/v1` (`:30`), `EMBED_MODEL=Qwen/Qwen3-Embedding-0.6B` (`:31`),
`SYNC_CONFIG_PATH=/app/config.yaml` (`:32`).

**Failure modes**
- **No `HEALTHCHECK`.** Combined with the missing `restart:` on the compose service
  (`docker-compose.yml:291-334`), a hung or crashed worker is invisible to Docker.
- **`tests/`, `conftest.py` and `requirements-dev.txt` are not copied**, so the image cannot self-test;
  `README.md:301` works around it by bind-mounting `sync-worker/tests`.
- The image runs as uid 10001 (`:20-23`) but `/data` is a **named volume** (`docker-compose.yml:319`)
  whose ownership on first creation comes from the image's `/data` — this works, but a pre-existing
  root-owned `/data` volume would make every Parquet/DuckDB/LanceDB write fail with `PermissionError`.
- `pip install` is unpinned beyond the ranges in `requirements.txt` and there is **no lock file and no
  hash pinning** — two builds a month apart can ship different `lancedb`/`pyarrow`/`duckdb` minors.
- No `--no-install-recommends`-style slimming needed (no apt layer), and no multi-stage build; the
  final image carries pip and the full build context.

**Concurrency** — n/a.

**Complexity hotspots** — n/a.

**Notable**
- `COPY config.yaml ./` (`:16`) is immediately shadowed by the read-only bind mount at
  `docker-compose.yml:324` — the baked copy only matters if someone runs the image outside compose.
- The comment at `:12` asserts all runtime deps ship manylinux aarch64 wheels — plausible for the DGX
  Spark but unverified here (**UNVERIFIED — not read**: no wheel manifest in the repo).

---

### sync-worker/requirements.txt  (10 LOC)

**Purpose** — Runtime dependency ranges for the container.

**Public surface** — `httpx>=0.27,<1` (`sync-worker/requirements.txt:3`), `PyJWT>=2.8,<3` (`:4`),
`cryptography>=42,<47` (`:5`), `duckdb>=1.0,<2` (`:6`), `pyarrow>=16,<22` (`:7`),
`pandas>=2.2,<3` (`:8`), `PyYAML>=6.0,<7` (`:9`), `lancedb>=0.8,<1` (`:10`).

**Control flow** — consumed by `sync-worker/Dockerfile:13-14`.

**State & side effects** — none (declarative).

**Dependencies** — inbound: `sync-worker/Dockerfile:13`, `sync-worker/requirements-dev.txt:1`.
outbound: PyPI.

**Config** — none.

**Failure modes**
- **`lancedb>=0.8,<1` spans a huge API-churn window.** `RagIndexer` calls
  `db.table_names()`, `db.open_table`, `db.create_table(schema=…)`, `table.delete(<filter str>)` and
  `table.add(list[dict])` (`sync-worker/syncworker/rag_index.py:77-89,142,144`); those signatures moved
  across 0.8→0.24. A rebuild can break the RAG path silently, since
  `sync-worker/syncworker/main.py:177-184` swallows it.
- `pyarrow>=16,<22` and `pandas>=2.2,<3` similarly float. **No lock file, no hashes** →
  non-reproducible builds on an air-gapped machine that must rebuild from a local mirror.
- `cryptography` is a transitive requirement of PyJWT's RS256 support; pinning it separately (`:5`) is
  correct.

**Concurrency** — n/a.

**Complexity hotspots** — n/a.

**Notable** — `boto3` is **absent**, consistent with the AWS removal
(`sync-worker/syncworker/secrets.py:1-6`, asserted by `sync-worker/tests/test_secrets.py:72-76`).

---

### sync-worker/requirements-dev.txt  (2 LOC)

**Purpose** — Test dependencies.

**Public surface** — `-r requirements.txt` (`sync-worker/requirements-dev.txt:1`),
`pytest>=8,<9` (`sync-worker/requirements-dev.txt:2`).

**Control flow** — none.

**State & side effects** — none.

**Dependencies** — inbound: none in the repo (not referenced by the Dockerfile;
`README.md:301` bind-mounts tests into the runtime image instead). outbound: `requirements.txt`, PyPI.

**Config** — none.

**Failure modes** — **`pytest-cov` is absent**, so there is no coverage measurement anywhere for this
service. No `pytest-timeout`, so a test that hits the real network would hang.
Note the local `.venv` reports `conftest.cpython-312-pytest-9.1.1.pyc` in `sync-worker/__pycache__/`,
i.e. the developer machine is running **pytest 9**, outside this file's `<9` cap.

**Concurrency** — n/a.

**Complexity hotspots** — n/a.

**Notable** — 2 lines; no linter (`ruff`/`flake8`) despite a `# noqa: ARG002` suppression at
`sync-worker/syncworker/main.py:41`, and no type checker despite `from __future__ import annotations`
in every module.

---

## Tests — what they actually assert

### sync-worker/tests/__init__.py  (0 LOC)
**Purpose** — Empty file making `tests` a package. **Public surface** — none. **Control flow** — none.
**State & side effects** — none. **Dependencies** — none. **Config** — none. **Failure modes** — none.
**Concurrency** — n/a. **Complexity hotspots** — none. **Notable** — empty.

---

### sync-worker/tests/test_chunking.py  (67 LOC)

**Purpose** — Pure-function coverage of `chunk_text` boundaries and overlap.

**Public surface** — `_words(n)` (`sync-worker/tests/test_chunking.py:6`) plus 6 tests at
`:10, :15, :21, :28, :39, :53, :61`.

**Control flow / what is actually asserted**
1. `test_empty_text_yields_no_chunks` (`:10-12`) — `""` and `"   \n\t "` → `[]`.
2. `test_short_text_single_chunk` (`:15-18`) — 50 words → exactly `[text]`.
3. `test_exact_chunk_size_single_chunk` (`:21-25`) — 800 words → 1 chunk, byte-identical.
4. `test_one_over_boundary_makes_two_chunks_with_overlap` (`:28-36`) — 801 words → 2 chunks of
   800 and 101; asserts `first[-100:] == second[:100]` (`:35`) and `second[-1] == "w800"` (`:36`).
5. `test_long_text_boundaries_and_overlap` (`:39-50`) — 1750 words → exactly 3 chunks sized
   `[800, 800, 350]` (`:45`), pairwise 100-token overlap (`:46-47`), and **lossless reassembly**
   (`:48-50`).
6. `test_custom_sizes` (`:53-58`) — `chunk_tokens=10, overlap=3` → starts at `w0,w7,w14,w21` (`:56`).
7. `test_invalid_overlap_rejected` (`:61-67`) — `overlap == chunk`, `overlap < 0`, `chunk == 0` all raise.

**What it does NOT assert** — nothing about non-whitespace-separated input (the JSON case, F-09),
nothing about chunk *byte* size, nothing about the embedder's context window.

**State & side effects** — none. **Dependencies** — inbound: pytest. outbound:
`pytest`, `syncworker.chunking.chunk_text` (`:3`). **Config** — none.
**Failure modes** — n/a. **Concurrency** — n/a. **Complexity hotspots** — none.
**Notable** — the strongest, most complete test file in the service.

---

### sync-worker/tests/test_config.py  (35 LOC)

**Purpose** — Guards the shipped `config.yaml` against silent regressions.

**Public surface** — `CONFIG_PATH` (`sync-worker/tests/test_config.py:5`),
`CORE_OBJECTS = {"Account","Contact","Lead","Opportunity","Case","User"}` (`:11`), 4 tests at
`:14, :20, :26, :32`.

**Control flow / what is actually asserted**
1. `test_the_core_crm_objects_are_configured` (`:14-17`) — the 6 core names are present.
2. `test_the_shipped_config_is_valid_and_not_empty` (`:20-23`) — `len(objects) >= 6` and **no duplicate
   object names**.
3. `test_every_object_has_id_and_systemmodstamp` (`:26-29`) — per object.
4. `test_rag_fields_are_subset_of_fields` (`:32-35`) — per object.

Tests 3 and 4 are **tautological** — `load_object_configs` already raises on both conditions
(`sync-worker/syncworker/config.py:78-82`), so these assertions can never fire; they only verify the
loader does not silently drop entries.

**What it does NOT assert** — no check that specific analytical fields survive (which is exactly how
`AnnualRevenue` disappeared, `sync-worker/config.yaml.bak:37` vs. `sync-worker/config.yaml:31-53`),
no field-count floor, no `rag_fields` presence for `Contact`/`Lead`.

**State & side effects** — reads `sync-worker/config.yaml` from disk (`:5,15,21,27,33`).
**Dependencies** — outbound: `os`, `syncworker.config.load_object_configs` (`:3`).
**Config** — none. **Failure modes** — n/a. **Concurrency** — n/a. **Complexity hotspots** — none.
**Notable** — the comment at `:7-10` explicitly declines to pin an exact object set.

---

### sync-worker/tests/test_discovery.py  (92 LOC)

**Purpose** — Field auto-adoption and new-object reporting, against hand-rolled fakes.

**Public surface** — `class FakeClient` (`sync-worker/tests/test_discovery.py:14`),
`class FakeSettings` with `sync_max_fields = 80` (`:29`), `class Obj` (`:33`), 9 tests at
`:38, :44, :52, :59, :65, :74, :81, :86, :91`.

**Control flow / what is actually asserted**
1. `test_a_new_field_is_picked_up_without_editing_the_config` (`:38-41`) — a describe-only field lands in `fields`.
2. `test_a_new_long_text_field_becomes_searchable_automatically` (`:44-48`) — a `textarea` lands in **both**
   `fields` and `rag`.
3. `test_compound_fields_are_never_adopted` (`:51-56`) — parametrised over `COMPOUND_TYPES`.
4. `test_already_configured_fields_are_not_duplicated` (`:59-62`) — `fields.count("Name") == 1`.
5. `test_adoption_is_capped_so_a_huge_object_stays_workable` (`:65-71`) — with `sync_max_fields = 5`,
   100 candidate fields → `len(fields) <= 5`.
6. `test_a_describe_failure_leaves_the_configured_fields_untouched` (`:74-78`) — `boom=True` →
   inputs returned unchanged.
7. `test_new_custom_objects_are_reported` (`:81-83`) — only the `__c` name is returned.
8. `test_configured_objects_are_not_reported_as_new` (`:86-88`).
9. `test_reporting_failure_is_not_fatal` (`:91-92`) — returns `[]`.

**What it does NOT assert** — nothing about `_NOISE_SUFFIXES` (`sync-worker/syncworker/main.py:60,89`),
nothing about the fact that `describe_field_types` is cached for the process lifetime (F-13), and
crucially **nothing about what happens to already-synced rows when a field is adopted** (F-12).

**State & side effects** — none (pure fakes, no network). **Dependencies** — outbound: `pytest`,
`syncworker.main.{COMPOUND_TYPES, adopt_new_fields, report_new_objects}` (`:11`).
**Config** — none. **Failure modes** — n/a. **Concurrency** — n/a. **Complexity hotspots** — none.
**Notable** — `FakeClient.describe_field_types` ignores its `name` argument (`:18-21`), so
per-object behaviour is untested.

---

### sync-worker/tests/test_embeddings.py  (116 LOC)

**Purpose** — Offline coverage of `OpenAIEmbedder` using `httpx.MockTransport`; no live vLLM.

**Public surface** — `BASE_URL`/`MODEL` (`sync-worker/tests/test_embeddings.py:13-14`),
`_openai_embeddings_response(inputs)` (`:17`), `_make_embedder(handler, base_url)` (`:30`), 6 tests at
`:35, :53, :65, :83, :96, :108`.

**Control flow / what is actually asserted**
1. `test_posts_openai_payload_to_embeddings_endpoint` (`:35-50`) — exactly **one** request, URL is
   `{BASE_URL}/embeddings` (`:47`), body is exactly `{"model": MODEL, "input": [...]}` (`:49`), and the
   returned vectors match (`:50`).
2. `test_vectors_come_back_in_input_order` (`:53-62`) — `data[i].embedding` maps 1:1 onto input `i`.
3. `test_batches_of_32_and_concatenates_in_order` (`:65-80`) — 70 texts → request sizes
   `[32, 32, 6]` (`:77`), total count preserved (`:78`), and cross-batch concatenation order (`:80`).
4. `test_trailing_slash_in_base_url_is_normalized` (`:83-93`) — `…/v1/` → no double slash.
5. `test_vector_count_mismatch_raises` (`:96-105`) — dropping one embedding raises `RuntimeError`.
6. `test_http_error_raises_for_caller_fail_soft` (`:108-116`) — a 503 raises `httpx.HTTPStatusError`;
   the docstring (`:109`) states the contract that `main.py` catches it.

**What it does NOT assert** — nothing about `RagIndexer` at all. There is **no test anywhere** for
`index_records`, the per-record LanceDB delete loop, the dimension-lock behaviour, or the
delete-then-add ordering. `sync-worker/syncworker/rag_index.py:60-154` (the higher-risk half of the
module) is entirely untested.

**State & side effects** — none (all HTTP mocked, `:31`). **Dependencies** — outbound: `json`, `httpx`,
`pytest`, `syncworker.rag_index.{EMBED_BATCH_SIZE, OpenAIEmbedder}` (`:11`).
**Config** — none. **Failure modes** — n/a. **Concurrency** — n/a. **Complexity hotspots** — none.
**Notable** — the batch-size test hardcodes `EMBED_BATCH_SIZE * 2 + 6` (`:74`), so it tracks the constant.

---

### sync-worker/tests/test_jwt.py  (151 LOC)

**Purpose** — Assertion construction/verification and the client-credentials branch of `TokenManager`.

**Public surface** — `CLIENT_ID`/`USERNAME`/`LOGIN_URL` (`sync-worker/tests/test_jwt.py:9-11`),
`_throwaway_keypair()` (`:14`, generates a real 2048-bit RSA key), 6 tests at
`:28, :46, :60, :81, :110, :133`.

**Control flow / what is actually asserted**
1. `test_assertion_decodes_with_expected_claims` (`:28-43`) — header `alg == "RS256"` (`:35`), and after
   verifying with the public key: `iss == client_id`, `sub == username`, `aud == login_url`, and
   **`exp == now + 180`** (`:40-43`).
2. `test_assertion_rejects_wrong_key` (`:46-57`) — a foreign public key raises `InvalidSignatureError`.
3. `test_assertion_expiry_enforced` (`:60-73`) — an assertion built at `now - 180 - 120` raises
   `ExpiredSignatureError`.
4. `test_a_secret_uses_client_credentials_and_signs_nothing` (`:81-107`) — with `client_secret` set and
   **no key at all**, the POST body is `grant_type=client_credentials` + `client_id` + `client_secret`
   and contains **no `assertion`** (`:105-107`); `instance_url`'s trailing slash is stripped (`:104`).
5. `test_the_wrong_domain_error_says_which_url_to_use` (`:110-130`) — a 400 with
   `"request not supported on this domain"` raises `RuntimeError` matching `"My Domain"`.
6. `test_a_missing_run_as_user_is_named` (`:133-151`) — a 400 with `"no client credentials user enabled"`
   raises `RuntimeError` matching `"Run As"`.

**What it does NOT assert** — **nothing about clock skew** (no test builds an assertion against a
skewed peer clock, and there is no skew logic to test — F-08). Nothing about `TOKEN_TTL_SECONDS`
proactive refresh (`sync-worker/syncworker/sf_auth.py:60-63`), nothing about `invalidate()`, nothing
about the JWT-bearer POST body shape (only the client-credentials body is checked), and nothing about
the 401-retry path in `sf_client._request` (`sync-worker/syncworker/sf_client.py:138-143`).

**State & side effects** — CPU-only RSA keygen (`:15`); all HTTP via `MockTransport` (`:100,128,149`).
No real network. **Dependencies** — outbound: `time`, `jwt`, `cryptography`,
`syncworker.sf_auth.{JWT_VALIDITY_SECONDS, build_jwt_assertion}` (`:7`), plus lazily `httpx`,
`pytest`, `TokenManager`, `SalesforceCredentials` inside the test bodies (`:84-87,114-118,135-138`).
**Config** — none. **Failure modes** — n/a. **Concurrency** — n/a. **Complexity hotspots** — none.
**Notable** — tests 2 and 3 use `try/except/else: raise AssertionError` (`:52-57, :68-73`) instead of
`pytest.raises`, inconsistent with the rest of the file.

---

### sync-worker/tests/test_limits.py  (57 LOC)

**Purpose** — `Sforce-Limit-Info` parsing and the 80 % warning threshold.

**Public surface** — 6 tests at `sync-worker/tests/test_limits.py:10, :14, :19, :26, :34, :45, :52`
(7 functions total).

**Control flow / what is actually asserted**
1. `test_parse_limit_info_basic` (`:10-11`) — `"api-usage=18/15000"` → `(18, 15000)`.
2. `test_parse_limit_info_with_other_entries` (`:14-16`) — extracts from a header that also carries
   `per-app-api-usage=1/2000(appName=sync)`.
3. `test_parse_limit_info_malformed_or_missing` (`:19-23`) — `None`, `""`, `"garbage"`,
   `"api-usage=notanumber/15000"` all → `None`.
4. `test_no_warning_below_threshold` (`:26-31`) — 11999/15000 → ratio < 0.80 and **no log records**.
5. `test_warning_at_exactly_80_percent` (`:34-42`) — 12000/15000 → exactly one WARNING carrying
   `api_used == 12000` and `api_total == 15000` as record attributes (`:41-42`).
6. `test_warning_above_threshold` (`:45-49`) — 14999/15000 warns.
7. `test_malformed_header_never_warns_or_crashes` (`:52-57`) — returns `None`, emits nothing.

**What it does NOT assert** — nothing about `total <= 0` (`sync-worker/syncworker/sf_client.py:61-62`),
and nothing at all about the client **acting** on the warning — because it never does.

**State & side effects** — logging only, via `caplog`. **Dependencies** — outbound: `logging`,
`syncworker.sf_client.{LIMIT_WARN_THRESHOLD, check_api_limits, parse_limit_info}` (`:3-7`).
**Config** — none. **Failure modes** — n/a. **Concurrency** — n/a. **Complexity hotspots** — none.
**Notable** — the only tests in the suite that touch `sf_client`; `_request`, `bulk_query`,
`soql_query`, `describe_fields`, `list_objects`, `build_full_soql` and `build_incremental_soql` are
**entirely untested**.

---

### sync-worker/tests/test_objects_cli.py  (333 LOC)  — the largest test file

**Purpose** — End-to-end coverage of the `syncworker.objects` CLI against a temp config, plus the
sheet-import planner.

**Public surface** — `HEADER`/`BASE` fixtures data (`sync-worker/tests/test_objects_cli.py:13-29`),
`config` fixture (`:32-36`), `objects_of(path)` (`:39`), `run(path, *argv)` (`:43`),
`field(name, ftype)` (`:202`), `ORG` (`:206`), `sheet` fixture (`:219`), `describe(name)` (`:226`),
`SHEET` (`:190`). 27 tests.

**Control flow / what is actually asserted** — grouped:

*Adding* (`:52-89`) — `add` produces `["Id", …, "SystemModstamp"]` in that exact order (`:55`);
the required pair is injected (`:58-62`); naming it yourself does not duplicate (`:65-69`);
`rag_fields` are recorded (`:72-76`); a rag field not in fields raises `ConfigError` matching
`"must also appear in fields"` (`:79-82`); re-`add` **replaces** the field list, dropping `Name` (`:85-89`).

*add-fields* (`:97-118`) — merges rather than replaces, preserving order
`["Id","Name","Industry","Type","SystemModstamp"]` (`:100`); keeps existing `rag_fields` (`:103-107`);
no duplicates (`:110-113`); unknown object → exit code 2 and stderr contains `"use 'add' first"` (`:116-118`).

*Removing* (`:126-139`) — removes (`:126-128`); absent name → exit 2 + `"not in the config"` (`:131-133`);
**the last object cannot be removed** → exit 2 + `"nothing to do"` (`:136-139`).

*The file itself* (`:147-183`) — the comment header survives an edit (`:147-153`); the CLI's output is
still valid for `load_object_configs` (`:156-164`); invalid object names `"9Lives"`, `"has-dash"`,
`"has space"` raise (`:167-170`); same for field names (`:173-176`); `list` prints names and
`"indexed for search: Description"` (`:179-183`).

*Sheet import* (`:230-333`) — names carried down blank rows (`:230-233`); only describe-visible fields
kept, `Ghost_Field__c` dropped (`:236-240`); `BillingAddress` (type `address`) dropped (`:243-247`);
`textarea` → `rag_fields` (`:250-253`); an object whose custom fields are all hidden is skipped with a
`"field-level security"` note (`:256-259`); an object absent from the org is skipped with a
`"not readable"` note (`:262-265`); every imported entry loads in the real loader (`:268-275`);
200 fields trimmed to `MAX_FIELDS_PER_OBJECT + 2` with a `"trimmed"` note (`:278-285`);
**an unreadable sheet leaves the config byte-identical and returns 2** (`:288-298`, monkeypatching
`_live_describe`); prior fields survive a partial sheet (`:301-316`); an object configured but absent
from the sheet is kept (`:319-324`); a deliberate `rag_field` survives re-import (`:327-333`).

**What it does NOT assert** — nothing about `_live_describe`'s **real** behaviour: the test at `:288-298`
substitutes `lambda n: None` for *every* object, which is indistinguishable from a total API outage. No
test covers "describe succeeds for 40 objects and fails for 8" — the exact scenario in finding F-11.
Nothing asserts that `save` is atomic, and nothing asserts the truth of the "FULL extract" message
printed at `sync-worker/syncworker/objects.py:385-386` (F-12).

**State & side effects** — writes temp `config.yaml`/`org.csv` under `tmp_path`
(`:34-35, :221-222, :289-290`); monkeypatches the module global `mod._live_describe` and restores it in a
`finally` (`:293-297`). No network. **Dependencies** — outbound: `pytest`, `yaml`,
`syncworker.objects as ob` (`:11`), and `syncworker.config.load_object_configs` imported inside two tests
(`:158, :269`). **Config** — none. **Failure modes** — n/a.
**Concurrency** — the monkeypatch at `:293` mutates module state; it is restored, but a test failure
between `:293` and `:297` is guarded by the `try/finally`.
**Complexity hotspots** — none (no test function exceeds ~15 LOC).
**Notable** — genuinely good coverage of the CLI's *pure* logic; the network-facing half is untested.

---

### sync-worker/tests/test_secrets.py  (199 LOC)

**Purpose** — Credential resolution precedence, key-material validation and redaction.

**Public surface** — `no_default_key` fixture (`sync-worker/tests/test_secrets.py:8-15`),
`PEM`/`B64` (`:18-19`), `FULL_ENV` (`:21-26`), `IDENTITY` (`:82-86`), `THUMBPRINT` (`:90`),
`_key_file(tmp_path, content)` (`:93`). 17 tests (one parametrised ×3).

**Control flow / what is actually asserted**
1. `test_env_first_path_builds_credentials` (`:29-35`) — fields map through and `login_url`'s trailing
   `/` is stripped (`:34`).
2. `test_env_path_requires_all_four_keys` (`:38-41`) — dropping **any** of the four → `None`.
3. `test_env_path_rejects_bad_base64` (`:44-47`) — `ValueError` matching `SF_PRIVATE_KEY_B64`.
4. `test_env_values_never_appear_in_repr` (`:50-53`) — `"cid123"` absent, `"redacted"` present.
5. `test_fetch_reads_the_environment` (`:56-59`) — via `monkeypatch.setenv`.
6. `test_fetch_without_credentials_names_the_fix` (`:62-69`) — the error message names
   `SF_PRIVATE_KEY_FILE`.
7. **`test_nothing_imports_boto3_any_more` (`:72-76`)** — greps `secrets.py` source for the string
   `boto3`. This is the AWS-removal regression guard.
8. `test_a_key_file_path_is_read` (`:99-105`).
9. `test_the_file_wins_when_both_forms_are_set` (`:108-115`) — file beats base64.
10. `test_a_certificate_thumbprint_is_rejected_by_name` (`:118-127`) — parametrised over the SHA-256
    thumbprint, its lowercase form, and a colon-separated SHA-1; error must match `"THUMBPRINT"`.
11. `test_a_missing_key_file_names_the_path` (`:130-134`) — `"does not exist"`.
12. `test_a_certificate_instead_of_a_key_says_so` (`:137-144`) — `"CERTIFICATE, not a private key"`.
13. `test_base64_of_something_that_is_not_a_key_is_rejected` (`:147-150`) — `"not a PEM private key"`.
14. `test_identity_without_any_key_returns_none` (`:153-155`).
15. `test_a_broken_key_raises_rather_than_being_ignored` (`:158-162`).
16. `test_a_consumer_secret_is_enough_on_its_own` (`:170-176`) — `private_key_pem == b""`.
17. `test_the_secret_is_preferred_over_a_key` (`:179-185`).
18. `test_the_secret_is_never_shown_in_a_repr` (`:188-190`).
19. `test_a_blank_secret_falls_through_to_the_key` (`:193-199`) — `"   "` → falls through.

**What it does NOT assert** — the `DEFAULT_KEY_PATH` fallback branch
(`sync-worker/syncworker/secrets.py:150-151`) is only ever *suppressed* by the fixture
(`:8-15, :67`), **never exercised positively**; nothing tests that a key at `/data/sf_jwt_key.pem` is
actually picked up. Nothing tests `login_url` validity.

**State & side effects** — writes temp PEM files under `tmp_path` (`:93-96`); `monkeypatch.setattr` on
the module constant `syncworker.secrets.DEFAULT_KEY_PATH` (`:15, :67`) — **module-global mutation**,
correctly scoped by monkeypatch. No network. **No real secret is used**: `PEM` at `:18` is the literal
string `b"-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n"`, and `THUMBPRINT` at `:90`
is documented as a fingerprint (a fingerprint is public, not secret).
**Dependencies** — outbound: `base64`, `pytest`,
`syncworker.secrets.{credentials_from_env, fetch_sf_credentials}` (`:6`).
**Config** — exercises `SF_CLIENT_ID`, `SF_USERNAME`, `SF_LOGIN_URL`, `SF_PRIVATE_KEY_B64`,
`SF_PRIVATE_KEY_FILE`, `SF_CLIENT_SECRET`. **Failure modes** — n/a.
**Concurrency** — n/a. **Complexity hotspots** — none.
**Notable** — the module docstring (`:1`) still says *"Secrets Manager JSON second"*, contradicting
the very test at `:72-76` that proves AWS is gone. The fixture docstring at `:10-13` correctly notes
that the deployed container **does** have a key at `DEFAULT_KEY_PATH`.

---

### sync-worker/tests/test_upsert.py  (105 LOC)

**Purpose** — DuckDB upsert semantics and record normalisation.

**Public surface** — `_df(rows)` (`sync-worker/tests/test_upsert.py:6`), 6 tests at
`:10, :27, :64, :80, :86, :97`.

**Control flow / what is actually asserted**
1. `test_first_upsert_creates_table` (`:10-24`) — returns 2 and the table contains both rows.
2. `test_upsert_replaces_changed_rows_without_duplicating` (`:27-61`) — after a second batch changing
   `001B` and adding `001C`, the table is exactly the expected 3 rows (`:51-55`) **and a
   `GROUP BY Id HAVING count(*) > 1` query returns nothing** (`:57-60`). This is the idempotency proof.
3. `test_upsert_dedupes_ids_within_one_batch` (`:64-77`) — duplicate `003A` in one frame → last wins.
4. `test_upsert_empty_batch_is_noop` (`:80-83`) — returns 0.
5. `test_upsert_handles_new_column_appearing_later` (`:86-94`) — schema drift adds `Rating`, and the
   pre-existing row reads back `None` for it (`:93`). **This test documents F-12's data shape: the older
   row is NULL in the new column and nothing backfills it.**
6. `test_normalize_records_makes_values_string_or_none` (`:97-105`) — `True → "true"`,
   `1200.5 → "1200.5"`, `None → None`, `"" → None`.

**What it does NOT assert** — nothing about crash-mid-transaction/rollback
(`sync-worker/syncworker/storage.py:162-164`), nothing about `_safe_ident` rejection, nothing about
Parquet (`write_parquet_batch` is **never tested**), and nothing about the **column types** produced
(test 6 proves values become strings but no test asserts the resulting DuckDB type is `VARCHAR` or
questions whether that is desirable — F-02).

**State & side effects** — creates real DuckDB files under `tmp_path` (`:11,28,65,81,87`); reaches into
the private `store._con` for verification (`:22,48,57,75,90`). **Dependencies** — outbound: `pandas`,
`syncworker.storage.{Store, normalize_records}` (`:3`). **Config** — none. **Failure modes** — n/a.
**Concurrency** — each test uses its own `tmp_path` DB. **Complexity hotspots** — none.
**Notable** — tests touch `store._con` directly, coupling them to a private attribute.

---

### sync-worker/tests/test_watermark.py  (35 LOC)

**Purpose** — Watermark storage round-trip.

**Public surface** — 3 tests at `sync-worker/tests/test_watermark.py:4, :10, :27`.

**Control flow / what is actually asserted**
1. `test_watermark_missing_is_none` (`:4-7`) — unknown object → `None`.
2. `test_watermark_roundtrip_and_update` (`:10-24`) — set/get (`:12-13`); **update overwrites rather
   than duplicating** (`:16-17`); objects are independent (`:20-23`).
3. `test_watermark_persists_across_reopen` (`:27-35`) — close, re-open, value survives.

**What it does NOT assert** — **nothing about the ordering guarantee that makes re-sync idempotent**
(that `set_watermark` runs only after every batch — `sync-worker/syncworker/main.py:163-188`), nothing
about a crash between upsert and watermark commit, and nothing about `updated_at`. There is **no test
of `sync_object` at all** — the single most consequential function in the service
(`sync-worker/syncworker/main.py:106-194`, 89 LOC) has zero direct coverage.

**State & side effects** — real DuckDB files under `tmp_path` (`:5,11,28,33`).
**Dependencies** — outbound: `syncworker.storage.Store` (`:1`). **Config** — none.
**Failure modes** — n/a. **Concurrency** — n/a. **Complexity hotspots** — none.
**Notable** — 35 LOC covering the linchpin of incremental correctness.

---

## Cross-cutting evidence used by the findings

- **DuckDB columns really are all VARCHAR, and that breaks analytics SQL.** Reproduced with the repo's
  own DuckDB (`sync-worker/.venv/bin/python`) on a table built exactly the way
  `sync-worker/syncworker/storage.py:140` builds one:
  - `DESCRIBE` → `[('Id','VARCHAR'),('Amount','VARCHAR'),('CloseDate','VARCHAR')]`
  - `SELECT SUM(Amount) FROM t` → `BinderException: No function matches the given name and argument types 'sum(VARCHAR)'`
  - `SELECT * FROM t WHERE Amount > 1000` → `BinderException: Cannot compare values of type VARCHAR and type INTEGER_LITERAL`
  - `SELECT date_trunc('month', CloseDate) FROM t` → `BinderException: No function matches … 'date_trunc(STRING_LITERAL, VARCHAR)'`
  - `SELECT AVG(TRY_CAST(Amount AS DOUBLE)) FROM t` → `1050.25` (works only with an explicit cast).
  The LLM sees these types verbatim: `orchestrator/app/core/schema_cache.py:50-55` reads
  `information_schema.columns … WHERE table_schema = 'main'` and `format_schema`
  (`orchestrator/app/core/schema_cache.py:65-71`, join at `:69-70`) renders
  `Opportunity(Id VARCHAR, Amount VARCHAR, …)` into the prompt at
  `orchestrator/app/engines/sql.py:189,100`.
- **Key placement**: `DEFAULT_KEY_PATH = /data/sf_jwt_key.pem` (`sync-worker/syncworker/secrets.py:50`)
  sits in the `data` volume, mounted into **both** `orchestrator` (`docker-compose.yml:269`) and
  `sync-worker` (`docker-compose.yml:320`). The orchestrator's SQL path is hardened
  (`enable_external_access=False` plus a `read_text|read_blob|glob|…` blocklist at
  `orchestrator/app/core/sql_guard.py:45-54`), so this is defence-in-depth risk, not an open hole.
- **No `restart:` policy** on `sync-worker` (`docker-compose.yml:291-334`) while
  `docker-compose.yml:89,134,172,203,343` set `restart: unless-stopped` on the vLLM services and searxng.
- **`csv.field_size_limit()` == 131072** on this machine (measured).
- **`.env` is not tracked** (`git ls-files --error-unmatch .env` → error) and is gitignored
  (`.gitignore:10`); `.env.bak-205921` is gitignored (`.gitignore:47`). **No secret value was read.**

---

## Findings (F-nn ↔ the JSON payload)

| id | severity | title | anchor |
|---|---|---|---|
| F-01 | P1 | Unbounded Bulk job poll loop hangs the whole worker | `sync-worker/syncworker/sf_client.py:208-220` |
| F-02 | P1 | Every warehouse column is VARCHAR; aggregate/date SQL fails | `sync-worker/syncworker/storage.py:38-56,140` |
| F-03 | P1 | Salesforce deletes are never propagated | `sync-worker/syncworker/main.py:147-154` |
| F-04 | P1 | Swallowed RAG failure + advancing watermark = permanent index gap | `sync-worker/syncworker/main.py:174-188` |
| F-04b | P1 | One LanceDB delete per record inside the batch loop | `sync-worker/syncworker/rag_index.py:141-142` |
| F-05 | P2 | Parquet output grows without bound and is read by nothing | `sync-worker/syncworker/storage.py:59-69` |
| F-06 | P2 | Watermark uses the worker's clock, not Salesforce's | `sync-worker/syncworker/main.py:115,188` |
| F-07 | P2 | Unchanged long text is re-embedded on every record change | `sync-worker/syncworker/rag_index.py:97-144` |
| F-08 | P1 | JWT assertion has no clock-skew tolerance and no `iat`/`nbf` | `sync-worker/syncworker/sf_auth.py:32-38` |
| F-09 | P1 | Whitespace tokenizer produces one enormous chunk for JSON fields | `sync-worker/syncworker/chunking.py:30` |
| F-10 | P2 | Salesforce signing key lives in the shared analytics volume | `sync-worker/syncworker/secrets.py:50` |
| F-11 | P1 | `import-sheet` deletes objects when a describe merely fails | `sync-worker/syncworker/objects.py:284,227-229,364` |
| F-12 | P1 | Adding a field never backfills; the CLI claims otherwise | `sync-worker/syncworker/objects.py:385-386` |
| F-13 | P2 | `describe` cache is never invalidated, so adoption needs a restart | `sync-worker/syncworker/sf_client.py:161-171` |
| F-14 | P2 | Bulk CSV parsing dies on a full-size Long Text Area field | `sync-worker/syncworker/sf_client.py:233` |
| F-15 | P2 | No shutdown check inside a cycle; no restart policy | `sync-worker/syncworker/main.py:239-243` |
| F-16 | P3 | Dead code and duplicated constants across four modules | `sync-worker/syncworker/secrets.py:43,45` |

Details, exploit scenarios, fixes and verification commands are in the JSON payload returned alongside
this document.
