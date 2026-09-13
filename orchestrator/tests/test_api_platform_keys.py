"""API-key primitives and the scope vocabulary (CONTRACT-3 §5, §7, §9).

Offline. Nothing here needs PostgreSQL, a model, or a socket — which is the
property being pinned as much as it is a convenience: a presented key is
rejected on shape and checksum BEFORE the resolver is allowed to spend a
connection, so the thousands of scanner probes that hit any public endpoint
cost nothing. `test_a_flipped_character_is_refused_with_no_pepper_available`
proves that literally, by leaving the pepper unresolvable and watching
`split_key` work anyway.

Four properties, and the tests are grouped by them:

1. THE FORMAT IS THE CONTRACT. One line, four parts, a fixed environment
   prefix, and a secret whose own alphabet contains `_` — which is why
   splitting a key on `_` tears most keys apart and the parser reads from the
   end instead.

2. TWO INDEPENDENT LAYERS REJECT A BAD KEY. The checksum catches a typo
   offline; the HMAC digest catches a forgery that computed a correct
   checksum. A token can be perfectly well-formed and still not authenticate,
   and the tests assert both halves separately.

3. THE SECRET IS SHOWN ONCE AND NEVER AGAIN. Not in a repr, not in an
   f-string, not in pytest's own failure output. The redaction tests assert
   over `repr()`, `str()` and `format()` because a traceback uses all three.

4. SCOPES ARE DATA, NOT ROLES. No scope implies another, and the test asserts
   it over every ordered pair rather than over the two that happen to look
   related today.
"""
from __future__ import annotations

import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pytest

from app.apiplatform import keys, scopes
from app.config import settings

# A key with a KNOWN secret, so the checksum-flip tests are reproducible run
# after run. It carries `-` and `_` on purpose: `secrets.token_urlsafe` emits
# both, and a parser that splits on `_` mangles roughly two keys in three.
FIXED_PUBLIC_ID = "0f1e2d3c4b5a6978"
FIXED_SECRET = "aB-cD_eF0123456789zZ-yY_xX9876543210qQwWeRt"
FIXED_TOKEN = (
    f"tsk_live_{FIXED_PUBLIC_ID}_{FIXED_SECRET}"
    + keys.checksum_for(FIXED_PUBLIC_ID, FIXED_SECRET)
)

#: 32 characters, which is `MIN_PEPPER_CHARS` exactly.
TEST_PEPPER = "pepper-for-tests-0123456789abcde"


@pytest.fixture(autouse=True)
def _isolated_pepper(monkeypatch):
    """No pepper, no store, nothing cached — before every test.

    The pepper lives in module globals because it is resolved once per
    process in production. Without this fixture the first test to generate one
    would silently supply it to every test after it, and the "no pepper
    configured" assertions would pass for the wrong reason.
    """
    # `raising=False` is deliberately ABSENT (2026-09-13). It used to be here,
    # and it CREATES a missing attribute rather than failing — which is how
    # wave 1 shipped a `settings.api_key_pepper` that existed only inside this
    # suite, with `keys._configured_pepper()` reading a getattr default in
    # production and every installation silently on the weaker in-database
    # pepper. Without the flag, this fixture fails the whole file the moment
    # the setting is removed from `app/config.py` again.
    monkeypatch.setattr(settings, "api_key_pepper", "")
    monkeypatch.setattr(keys, "_pepper_loader", None)
    monkeypatch.setattr(keys, "_pepper_saver", None)
    monkeypatch.setattr(keys, "_pepper_cache", None)
    yield
    keys.reset_pepper_cache()


@pytest.fixture()
def peppered(monkeypatch):
    """A configured `API_KEY_PEPPER`, the production-intended source."""
    monkeypatch.setattr(settings, "api_key_pepper", TEST_PEPPER)
    return TEST_PEPPER


# ---------------------------------------------------------------------------
# 1. The format
# ---------------------------------------------------------------------------

TOKEN_SHAPE = re.compile(r"\Atsk_(live|test)_[0-9a-f]{16}_[A-Za-z0-9_-]{43}[0-9A-Za-z]{6}\Z")


@pytest.mark.parametrize("environment", ["live", "test"])
def test_a_minted_token_matches_the_contract_format_exactly(environment):
    minted = keys.mint_key(environment)

    assert TOKEN_SHAPE.match(minted.token), minted.token
    assert minted.token.startswith(f"tsk_{environment}_")
    assert minted.environment == environment
    assert len(minted.public_id) == 16
    # 256 bits of CSPRNG entropy, encoded urlsafe and unpadded.
    assert len(minted.secret) == 43
    assert len(minted.checksum) == 6
    assert minted.token == (
        f"tsk_{environment}_{minted.public_id}_{minted.secret}{minted.checksum}"
    )


def test_the_environment_is_in_the_prefix_not_only_in_the_random_part():
    """A live and a test key must be distinguishable by eye and by grep."""
    live, test = keys.mint_key("live"), keys.mint_key("test")
    assert live.token.startswith("tsk_live_") and test.token.startswith("tsk_test_")
    assert keys.split_key(live.token).environment == "live"
    assert keys.split_key(test.token).environment == "test"


@pytest.mark.parametrize(
    "environment",
    # The last four are the 2026-09-13 addition. Every case wave 1 tried was a
    # string or `None`, so the truthy-non-string branch was never reached and
    # `mint_key(5)` raised `AttributeError` where this module, its docstring
    # and this very test all promise `ValueError` — a management handler
    # catching ValueError to answer 400 would have returned a 500 instead.
    ["", "prod", "livel", "staging", None, 5, 5.0, ["live"], {"env": "live"}],
)
def test_minting_refuses_an_environment_outside_the_contract(environment):
    with pytest.raises(ValueError):
        keys.mint_key(environment)


def test_minting_normalises_the_case_and_padding_of_a_known_environment():
    assert keys.mint_key(" LIVE ").environment == "live"


def test_a_minted_token_splits_back_into_exactly_what_was_minted():
    minted = keys.mint_key("live")
    parsed = keys.split_key(minted.token)

    assert parsed is not None
    assert parsed.public_id == minted.public_id
    assert parsed.secret == minted.secret
    assert parsed.checksum == minted.checksum
    assert parsed.environment == minted.environment


def test_a_secret_containing_underscores_survives_the_round_trip():
    """The regression the parser is written around: `token_urlsafe` emits `_`,
    so `token.split("_")` would cut the secret in half."""
    assert "_" in FIXED_SECRET and "-" in FIXED_SECRET
    parsed = keys.split_key(FIXED_TOKEN)
    assert parsed is not None and parsed.secret == FIXED_SECRET
    assert parsed.public_id == FIXED_PUBLIC_ID


def test_last_four_comes_from_the_checksum_and_never_from_the_secret():
    """`last_four` is stored in the clear for recognition, so it must not be
    four characters of a live credential."""
    minted = keys.mint_key("live")
    assert minted.last_four == minted.token[-4:] == minted.checksum[-4:]
    assert minted.last_four not in minted.secret
    assert keys.last_four(minted.token) == minted.last_four
    assert keys.split_key(minted.token).last_four == minted.last_four


def test_last_four_of_something_that_is_not_our_key_is_empty():
    assert keys.last_four("sk_live_not_ours") == ""
    assert keys.last_four("") == ""


# ---------------------------------------------------------------------------
# 2a. The offline layer: shape and checksum, with no database
# ---------------------------------------------------------------------------


def test_a_flipped_character_anywhere_in_the_token_is_refused():
    """Every position, in one test rather than sixty-five parametrised ones:
    this suite's conftest truncates the whole schema before each test, so a
    parametrised case that needs no database still costs a round trip."""
    for position in range(len(FIXED_TOKEN)):
        original = FIXED_TOKEN[position]
        replacement = "A" if original != "A" else "B"
        tampered = FIXED_TOKEN[:position] + replacement + FIXED_TOKEN[position + 1 :]

        assert len(tampered) == len(FIXED_TOKEN)
        assert keys.split_key(tampered) is None, (
            f"a token with position {position} flipped from {original!r} to "
            f"{replacement!r} was accepted"
        )


def test_a_flipped_character_is_refused_with_no_pepper_available():
    """The offline claim, made literally: with no pepper configured and no
    secret store injected, a digest CANNOT be computed — yet both the accept
    and the reject decisions are still reached. Nothing was looked up."""
    with pytest.raises(keys.PepperUnavailable):
        keys.current_pepper()

    assert keys.split_key(FIXED_TOKEN) is not None
    assert keys.split_key(FIXED_TOKEN[:-1] + "Z") is None


def test_a_prefix_swapped_token_still_parses_which_is_why_the_resolver_reconciles():
    """The environment is OUTSIDE the checksum, exactly as CONTRACT-3 §5 says.

    `checksum_for` covers `public_id + secret`, so swapping the four
    characters that say `live` for the four that say `test` produces a token
    `split_key` accepts and for which it reports the ATTACKER's environment.
    Nothing is stolen by that alone — the secret still has to verify — but a
    log line built from the presented token then says "a test key was used"
    about a live one.

    `test_a_flipped_character_anywhere_in_the_token_is_refused` cannot catch
    this: it flips exactly one character at a time, which always lands outside
    both valid prefixes. The swap is four characters at once.

    This test pins the SHAPE fact. The refusal itself is the resolver's job,
    against `api_keys.environment` — see
    `tests/test_api_platform_resolver.py::
    test_a_prefix_swapped_token_is_refused_with_the_same_401_as_any_other`.
    """
    minted = keys.mint_key("live")
    swapped = "tsk_test_" + minted.token[len("tsk_live_"):]

    parsed = keys.split_key(swapped)
    assert parsed is not None
    assert parsed.environment == "test"  # the attacker's label, not ours
    assert parsed.public_id == minted.public_id
    assert parsed.secret == minted.secret

    # So the reconciliation primitive is what says no, and `redact` must be
    # given the STORED environment rather than believing the token.
    assert keys.environment_matches(parsed.environment, "live") is False
    assert keys.environment_matches("live", "live") is True
    assert keys.redact(swapped) == f"tsk_test_{minted.public_id}_<redacted>"
    assert keys.redact(swapped, environment="live") == (
        f"tsk_live_{minted.public_id}_<redacted>"
    )


@pytest.mark.parametrize(
    "presented,stored",
    [("live", "test"), ("test", "live"), ("live", ""), ("", "live"),
     (None, "live"), ("live", None), (b"live", "live")],
)
def test_environment_reconciliation_fails_closed_on_anything_but_an_exact_match(
    presented, stored
):
    assert keys.environment_matches(presented, stored) is False


@pytest.mark.parametrize(
    "token",
    [
        "",
        "   ",
        "tsk_live_",
        "tsk_live_0f1e2d3c4b5a6978",  # public id, no separator, no secret
        "tsk_prod_0f1e2d3c4b5a6978_" + FIXED_SECRET + "abcdef",  # unknown environment
        "sk_live_0f1e2d3c4b5a6978_" + FIXED_SECRET + "abcdef",  # another vendor's prefix
        FIXED_TOKEN[:-6],  # checksum stripped
        FIXED_TOKEN[:-1],  # one character truncated
        FIXED_TOKEN + "A",  # one character appended
        FIXED_TOKEN.upper(),  # case mangled by a shell or a spreadsheet
        "tsk_live_0F1E2D3C4B5A6978_" + FIXED_SECRET + "abcdef",  # uppercase hex
        "Bearer " + FIXED_TOKEN,  # the header value pasted whole
        "ts_session=abc123",  # a browser cookie offered as a key
    ],
)
def test_a_malformed_token_is_refused_without_a_reason(token):
    """Malformed, truncated, invented and wrong must be indistinguishable:
    `split_key` returns None for all of them and never raises, so the handler
    above cannot accidentally report one differently (CONTRACT-3 §9)."""
    assert keys.split_key(token) is None


@pytest.mark.parametrize("token", [None, 12345, b"tsk_live_x", ["tsk_live_x"]])
def test_a_token_that_is_not_even_a_string_is_refused(token):
    assert keys.split_key(token) is None


@pytest.mark.parametrize("length", [42, 44, 22, 86])
def test_a_secret_of_the_wrong_length_is_refused(length):
    """The entropy floor, enforced offline: a 22-character secret carries
    ~131 bits and would still 'work', so the shape check is what keeps a
    short-secret key from ever being accepted."""
    secret = (FIXED_SECRET * 4)[:length]
    token = f"tsk_live_{FIXED_PUBLIC_ID}_{secret}" + keys.checksum_for(
        FIXED_PUBLIC_ID, secret
    )
    assert keys.split_key(token) is None


def test_the_accepted_secret_length_set_includes_what_mint_produces():
    """A future release may mint LONGER secrets, but dropping 43 from the
    accepted set would 401 every key already in the field."""
    assert 43 in keys._ACCEPTED_SECRET_LENGTHS
    assert len(keys.mint_key("live").secret) in keys._ACCEPTED_SECRET_LENGTHS


def test_the_checksum_is_six_base62_characters_of_crc32():
    import binascii

    checksum = keys.checksum_for(FIXED_PUBLIC_ID, FIXED_SECRET)
    assert len(checksum) == 6
    assert set(checksum) <= set(keys._BASE62)

    expected = binascii.crc32((FIXED_PUBLIC_ID + FIXED_SECRET).encode()) & 0xFFFFFFFF
    decoded = 0
    for char in checksum:
        decoded = decoded * 62 + keys._BASE62.index(char)
    assert decoded == expected


def test_a_small_checksum_is_left_padded_to_six_characters():
    """CRC32 can be a small number; a five-character checksum would shift the
    secret boundary and break the parse."""
    assert keys._base62(0, 6) == "000000"
    assert keys._base62(61, 6) == "00000z"
    assert len(keys._base62(0xFFFFFFFF, 6)) == 6


# ---------------------------------------------------------------------------
# 2b. The digest layer: a well-formed forgery still fails
# ---------------------------------------------------------------------------


def test_the_digest_is_hmac_sha256_of_the_secret_under_the_pepper(peppered):
    minted = keys.mint_key("live")
    expected = hmac.new(
        peppered.encode("utf-8"), minted.secret.encode("utf-8"), sha256
    ).hexdigest()

    assert keys.key_digest(minted.secret) == expected
    assert len(expected) == 64


def test_a_valid_looking_token_with_the_wrong_secret_fails_the_digest(peppered):
    """The forgery this defends against: an attacker who knows a public id can
    compute a correct checksum over any secret they like, so the offline layer
    lets their token through. The digest is what refuses it."""
    real = keys.mint_key("live")
    stored = keys.key_digest(real.secret)

    forged_secret = keys.mint_key("live").secret
    forged = (
        f"tsk_live_{real.public_id}_{forged_secret}"
        + keys.checksum_for(real.public_id, forged_secret)
    )

    parsed = keys.split_key(forged)
    assert parsed is not None, "a forged key passes the offline layer by design"
    assert parsed.public_id == real.public_id
    assert keys.verify_secret(parsed.secret, stored) is False
    assert keys.verify_secret(real.secret, stored) is True


def test_the_same_secret_under_a_different_pepper_does_not_verify(monkeypatch):
    """What the pepper buys: a stolen `api_keys` table is inert without it."""
    monkeypatch.setattr(settings, "api_key_pepper", TEST_PEPPER, raising=False)
    minted = keys.mint_key("live")
    stored = keys.key_digest(minted.secret)

    monkeypatch.setattr(
        settings, "api_key_pepper", "a-different-pepper-0123456789abcd", raising=False
    )
    assert keys.verify_secret(minted.secret, stored) is False


@pytest.mark.parametrize("stored", ["", None, "not-hex", "0" * 64])
def test_verification_fails_closed_on_a_missing_or_corrupt_digest(peppered, stored):
    assert keys.verify_secret(keys.mint_key("live").secret, stored) is False


def test_verification_of_an_empty_secret_is_false_not_an_error(peppered):
    assert keys.verify_secret("", keys.key_digest("anything")) is False


def test_digesting_an_empty_secret_is_refused(peppered):
    """A row whose `key_hash` is the digest of "" would authenticate a caller
    who presented nothing."""
    with pytest.raises(ValueError):
        keys.key_digest("")


def test_the_secret_comparison_is_constant_time(peppered, monkeypatch):
    """`hmac.compare_digest`, never `==`: a comparison that returns early
    leaks the matching prefix length, which is how a bearer credential gets
    guessed one character at a time."""
    calls = []
    real_compare = hmac.compare_digest

    def recording(a, b):
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(keys.hmac, "compare_digest", recording)

    minted = keys.mint_key("live")
    assert keys.verify_secret(minted.secret, keys.key_digest(minted.secret)) is True
    assert calls, "verify_secret must go through hmac.compare_digest"


def test_the_checksum_comparison_is_constant_time_too(monkeypatch):
    calls = []
    real_compare = hmac.compare_digest
    monkeypatch.setattr(
        keys.hmac,
        "compare_digest",
        lambda a, b: (calls.append((a, b)), real_compare(a, b))[1],
    )

    assert keys.split_key(FIXED_TOKEN) is not None
    assert calls, "split_key must compare the checksum with hmac.compare_digest"


# ---------------------------------------------------------------------------
# 2c. Randomness and uniqueness
# ---------------------------------------------------------------------------


def test_minting_draws_only_from_the_csprng(monkeypatch):
    """`secrets`, never `random`, never a UUID, never a timestamp
    (STANDARDS.md, OWASP Cryptographic Storage)."""
    drawn = []
    real_hex, real_urlsafe = keys.secrets.token_hex, keys.secrets.token_urlsafe

    monkeypatch.setattr(
        keys.secrets, "token_hex", lambda n: (drawn.append(("hex", n)), real_hex(n))[1]
    )
    monkeypatch.setattr(
        keys.secrets,
        "token_urlsafe",
        lambda n: (drawn.append(("urlsafe", n)), real_urlsafe(n))[1],
    )

    keys.mint_key("live")
    assert drawn == [("hex", 8), ("urlsafe", 32)]

    source = Path(keys.__file__).read_text(encoding="utf-8")
    assert "import random" not in source and "uuid" not in source


def test_ten_thousand_keys_produce_no_duplicate_public_id():
    minted = [keys.mint_key("live") for _ in range(10_000)]

    assert len({m.public_id for m in minted}) == 10_000
    assert len({m.secret for m in minted}) == 10_000
    assert len({m.token for m in minted}) == 10_000


# ---------------------------------------------------------------------------
# 3. The secret is shown once and never again
# ---------------------------------------------------------------------------


def _renderings(obj):
    """Every way a value reaches a log line, a traceback or pytest's output."""
    return [repr(obj), str(obj), f"{obj}", "{}".format(obj), format(obj)]


def test_a_minted_key_never_renders_its_secret_or_its_token():
    minted = keys.mint_key("live")
    for rendered in _renderings(minted):
        assert minted.secret not in rendered
        assert minted.token not in rendered
        assert "<redacted>" in rendered
        # The public half IS safe to log, and is what makes an incident
        # investigable at all.
        assert minted.public_id in rendered


def test_a_parsed_key_never_renders_its_secret():
    parsed = keys.split_key(FIXED_TOKEN)
    for rendered in _renderings(parsed):
        assert FIXED_SECRET not in rendered
        assert "<redacted>" in rendered
        assert FIXED_PUBLIC_ID in rendered


def test_a_rotation_never_renders_the_new_secret():
    rotation = keys.plan_rotation("0f1e2d3c4b5a6978", "live")
    for rendered in _renderings(rotation):
        assert rotation.minted.secret not in rendered
        assert rotation.minted.token not in rendered


def test_a_key_inside_a_container_still_does_not_render_its_secret():
    """A list or dict repr calls `repr()` on its members — which is how a
    secret reaches a log line that never mentioned the key at all."""
    minted = keys.mint_key("live")
    assert minted.secret not in repr({"key": minted, "keys": [minted]})


def test_redact_keeps_the_public_half_and_nothing_else():
    minted = keys.mint_key("live")
    redacted = keys.redact(minted.token)

    assert redacted == f"tsk_live_{minted.public_id}_<redacted>"
    assert minted.secret not in redacted
    assert keys.redact("not a key at all") == "<malformed>"
    assert keys.redact("") == "<malformed>"


# ---------------------------------------------------------------------------
# The pepper
# ---------------------------------------------------------------------------


def test_the_configured_pepper_is_preferred_over_the_stored_one(monkeypatch):
    """`API_KEY_PEPPER` is the source that satisfies NIST's 'not in the same
    store as the digests'; the generated fallback is the weaker option and
    must never win over it."""
    monkeypatch.setattr(settings, "api_key_pepper", TEST_PEPPER)
    keys.configure_pepper_store(lambda: "stored-pepper-would-be-wrong-here", lambda v: None)

    assert keys.current_pepper() == TEST_PEPPER


def test_no_pepper_and_no_store_refuses_rather_than_inventing_one():
    """Minting under an ephemeral pepper would produce a key that stops
    authenticating at the next restart, silently."""
    with pytest.raises(keys.PepperUnavailable):
        keys.current_pepper()
    with pytest.raises(keys.PepperUnavailable):
        keys.key_digest("some-secret")


def test_a_configured_pepper_below_the_entropy_floor_is_refused(monkeypatch):
    """NIST 800-131A disallows an HMAC key below 112 bits. Failing closed is
    the point: a short pepper that 'works' is a security property downgraded
    where nobody will see it."""
    monkeypatch.setattr(settings, "api_key_pepper", "too-short")
    with pytest.raises(keys.PepperUnavailable):
        keys.current_pepper()


def test_the_pepper_setting_is_plumbed_all_the_way_to_the_environment():
    """CONTRACT-3 §5's stronger pepper source must be REACHABLE.

    Wave 1 read `getattr(settings, "api_key_pepper", "")` against a Settings
    class that had no such attribute, so the default won every time, the
    store branch was the only branch that ever ran, and the installation was
    silently on the option the contract itself calls the weaker of the two.
    Asserting on the attribute — not on a monkeypatched stand-in — is what
    makes that unrepeatable.
    """
    assert hasattr(settings, "api_key_pepper")
    assert isinstance(settings.api_key_pepper, str)
    # And it is read from the variable `keys` names, not some other spelling.
    assert keys.PEPPER_ENV_VAR == "API_KEY_PEPPER"
    source = Path(settings.__class__.__module__.replace(".", "/") + ".py")
    assert 'os.environ.get("API_KEY_PEPPER"' in (
        Path(__file__).resolve().parents[1] / source
    ).read_text()


def test_a_stored_pepper_below_the_entropy_floor_is_refused_too(monkeypatch):
    """The floor applies to BOTH sources, not just the configured one.

    Until 2026-09-13 only `API_KEY_PEPPER` was length-checked, and — because
    that setting did not exist — the UNCHECKED branch was the only one that
    ever ran. A one-character `platform_secrets` row would have keyed
    HMAC-SHA256 for every `api_keys.key_hash` on the installation.

    Refusing, rather than regenerating over it: overwriting a stored pepper
    invalidates every existing digest at once, which is a total, silent
    authentication outage. That is an operator's decision to make.
    """
    keys.configure_pepper_store(lambda: "x", lambda value: None)

    with pytest.raises(keys.PepperUnavailable):
        keys.current_pepper()
    with pytest.raises(keys.PepperUnavailable):
        keys.key_digest("some-secret")


def test_a_pepper_is_generated_once_and_then_read_back(monkeypatch):
    saved = []
    store = {}

    def loader():
        return store.get(keys.PEPPER_SECRET_NAME)

    def saver(value):
        saved.append(value)
        store.setdefault(keys.PEPPER_SECRET_NAME, value)

    keys.configure_pepper_store(loader, saver)

    first = keys.current_pepper()
    assert len(saved) == 1 and first == saved[0]
    assert len(first) >= keys.MIN_PEPPER_CHARS

    # Cached: a second call must not mint a second pepper.
    assert keys.current_pepper() == first
    assert len(saved) == 1

    # And after the cache is dropped, the STORED value is read back rather
    # than a new one generated — every digest in the table is keyed to it.
    keys.reset_pepper_cache()
    assert keys.current_pepper() == first
    assert len(saved) == 1


def test_a_concurrent_writers_pepper_wins_over_the_one_generated_here():
    """The saver is required to be insert-if-absent, so a second process may
    already have stored a different value. The re-read is what makes both
    processes agree; without it one of them would key every digest it writes
    to a pepper nobody else has."""
    theirs = "their-pepper-0123456789abcdefghij"
    store = {}

    def loader():
        return store.get(keys.PEPPER_SECRET_NAME)

    def saver(value):
        # INSERT … ON CONFLICT DO NOTHING: they got there first.
        store.setdefault(keys.PEPPER_SECRET_NAME, theirs)

    keys.configure_pepper_store(loader, saver)
    assert keys.current_pepper() == theirs


def test_configuring_the_store_drops_a_previously_cached_pepper():
    keys.configure_pepper_store(lambda: "first-pepper-0123456789abcdefghij", lambda v: None)
    assert keys.current_pepper() == "first-pepper-0123456789abcdefghij"

    keys.configure_pepper_store(lambda: "second-pepper-0123456789abcdefgh", lambda v: None)
    assert keys.current_pepper() == "second-pepper-0123456789abcdefgh"


def test_the_pepper_itself_is_never_returned_by_a_key_object(peppered):
    minted = keys.mint_key("live")
    assert TEST_PEPPER not in repr(minted)
    assert TEST_PEPPER not in keys.key_digest(minted.secret)


# ---------------------------------------------------------------------------
# Lifecycle: create -> rotate (with overlap) -> revoke
# ---------------------------------------------------------------------------

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


def test_a_rotation_mints_a_new_key_and_dates_the_old_one_out():
    rotation = keys.plan_rotation("0f1e2d3c4b5a6978", "live", now=NOW)

    assert rotation.rotated_from == "0f1e2d3c4b5a6978"
    assert rotation.minted.public_id != "0f1e2d3c4b5a6978"
    assert rotation.previous_expires_at == NOW + timedelta(days=7)
    assert keys.DEFAULT_ROTATION_OVERLAP == timedelta(days=7)


def test_the_overlap_window_is_open_until_it_expires_and_then_shut():
    rotation = keys.plan_rotation("0f1e2d3c4b5a6978", "live", now=NOW)
    expiry = rotation.previous_expires_at

    assert keys.overlap_active(expiry, now=NOW) is True
    assert keys.overlap_active(expiry, now=expiry - timedelta(seconds=1)) is True
    assert keys.overlap_active(expiry, now=expiry) is False
    assert keys.overlap_active(expiry, now=expiry + timedelta(seconds=1)) is False


def test_revoking_is_a_separate_verb_with_no_grace_period():
    """STANDARDS.md: a single 'rotate' verb with a built-in grace period
    cannot serve a compromise, where the old credential must stop working
    now."""
    revocation = keys.plan_revocation("0f1e2d3c4b5a6978", "live", now=NOW)

    assert revocation.previous_expires_at == NOW
    assert keys.overlap_active(revocation.previous_expires_at, now=NOW) is False


def test_no_rotation_window_recorded_does_not_mean_forever():
    assert keys.overlap_active(None, now=NOW) is False


@pytest.mark.parametrize(
    "overlap", [timedelta(seconds=-1), timedelta(days=31), timedelta(days=365)]
)
def test_an_overlap_outside_the_bounds_is_refused_not_clamped(overlap):
    """CONTRACT-3 §8: a parameter this platform cannot honour is rejected,
    never silently ignored — the management surface included."""
    with pytest.raises(ValueError):
        keys.plan_rotation("0f1e2d3c4b5a6978", "live", overlap=overlap, now=NOW)


@pytest.mark.parametrize("previous", [None, "", "   ", 5, {"public_id": "abc"}])
def test_a_rotation_refuses_a_previous_key_that_is_not_a_public_id(previous):
    """`if not previous_public_id` accepted any truthy object, so a caller who
    passed the whole key ROW got a rotation naming a dict — and would then
    write that dict's repr into `api_keys.rotated_from`."""
    with pytest.raises(ValueError):
        keys.plan_rotation(previous)


def test_a_rotation_must_name_the_key_it_replaces():
    with pytest.raises(ValueError):
        keys.plan_rotation("", "live", now=NOW)


def test_a_new_key_gets_a_finite_lifetime_by_default():
    assert keys.default_expires_at(now=NOW) == NOW + timedelta(days=90)
    assert keys.DEFAULT_KEY_LIFETIME == timedelta(days=90)
    with pytest.raises(ValueError):
        keys.default_expires_at(now=NOW, lifetime=timedelta(0))


def test_expiry_is_exclusive_and_a_null_expiry_is_not_an_expiry():
    expiry = NOW + timedelta(days=90)
    assert keys.is_expired(expiry, now=expiry - timedelta(seconds=1)) is False
    assert keys.is_expired(expiry, now=expiry) is True
    assert keys.is_expired(None, now=NOW) is False


def test_a_naive_datetime_is_refused_rather_than_assumed_to_be_utc():
    """Every V34 timestamp is `timestamptz`. A naive datetime compared with
    one is a bug that only appears when the server is not on UTC."""
    naive = datetime(2026, 9, 12, 12, 0)
    with pytest.raises(ValueError):
        keys.is_expired(naive, now=NOW)
    with pytest.raises(ValueError):
        keys.plan_rotation("0f1e2d3c4b5a6978", "live", now=naive)


# ---------------------------------------------------------------------------
# 4. Scopes are data, not roles
# ---------------------------------------------------------------------------


def test_the_scope_vocabulary_is_exactly_the_four_the_contract_names():
    """CONTRACT-3 §7's endpoint table, read literally.

    It names four scopes. Wave 1 shipped six, calling them "the six the
    contract names": `webhooks.read` and `webhooks.manage` appear NOWHERE in
    CONTRACT.md, and the paragraph under §7's table puts webhooks among what
    `/v1` deliberately does not expose. The only webhook string in the whole
    document is `api.webhooks.manage`, a §6 BROWSER capability in the other
    vocabulary. The contract is supposed to move first; it had not moved.

    Asserted against the document itself, not against a copy of it, so this
    test fails if either side changes alone.
    """
    contract = (
        Path(__file__).resolve().parents[2]
        / "docs/developer-platform/CONTRACT.md"
    ).read_text()
    section = contract.split("## 7. Public endpoints", 1)[1].split("\n## 8.", 1)[0]

    assert {s.value for s in scopes.ALL_SCOPES} == {
        "models.read",
        "responses.read",
        "responses.write",
        "usage.read",
    }
    for scope in scopes.ALL_SCOPES:
        assert f"`{scope.value}`" in section, f"{scope.value} is not in CONTRACT §7"
    assert "webhooks.read" not in contract
    assert "`webhooks.manage`" not in contract


def test_every_scope_is_described_for_the_console_and_the_openapi_document():
    assert set(scopes.SCOPE_DESCRIPTIONS) == set(scopes.ALL_SCOPES)
    assert all(text.strip() for text in scopes.SCOPE_DESCRIPTIONS.values())


def test_no_scope_implies_any_other_scope():
    """Asserted over every ordered pair, not over the two that happen to look
    related today: an implication added later must break this test."""
    for granted in scopes.ALL_SCOPES:
        for required in scopes.ALL_SCOPES:
            requirement = scopes.requires(required)
            assert requirement.satisfied_by({granted}) is (granted == required)


def test_a_key_holding_every_scope_but_the_required_one_is_still_refused():
    everything_else = scopes.ALL_SCOPES - {scopes.Scope.RESPONSES_WRITE}
    requirement = scopes.requires(scopes.Scope.RESPONSES_WRITE)

    assert requirement.satisfied_by(everything_else) is False
    with pytest.raises(scopes.InsufficientScopeError):
        requirement.check(everything_else)


def test_an_unknown_scope_is_an_error_and_never_a_dropped_entry():
    """A typo in the console must not silently produce a key that looks
    configured and grants nothing."""
    with pytest.raises(scopes.UnknownScopeError):
        scopes.parse_scopes(["models.read", "responses.wrte"])
    with pytest.raises(scopes.UnknownScopeError):
        scopes.parse_scopes(["admin.*"])
    with pytest.raises(scopes.UnknownScopeError):
        scopes.parse_scope("Models.Read")  # RFC 6749 scopes are case-sensitive


def test_a_comma_delimited_scope_string_is_refused_rather_than_guessed_at():
    """RFC 6749's grammar is space-delimited. Accepting both spellings forces
    every downstream consumer to accept both too."""
    with pytest.raises(scopes.UnknownScopeError):
        scopes.parse_scopes("models.read,responses.read")


def test_scopes_parse_from_the_stored_array_and_from_the_wire_string():
    expected = {scopes.Scope.MODELS_READ, scopes.Scope.RESPONSES_READ}

    assert scopes.parse_scopes(["models.read", "responses.read"]) == expected
    assert scopes.parse_scopes("responses.read models.read") == expected
    assert scopes.parse_scopes("  models.read   responses.read  ") == expected
    assert scopes.parse_scopes(scopes.Scope.MODELS_READ) == {scopes.Scope.MODELS_READ}


def test_no_scopes_is_a_real_state_that_satisfies_nothing():
    """A key narrowed to nothing is valid; it just cannot call anything."""
    assert scopes.parse_scopes(None) == frozenset()
    assert scopes.parse_scopes([]) == frozenset()
    assert scopes.parse_scopes("") == frozenset()
    assert scopes.requires(scopes.Scope.MODELS_READ).satisfied_by(frozenset()) is False


def test_the_storage_form_is_a_sorted_json_array_of_plain_strings():
    """`api_keys.scopes` is jsonb; sorting means an unchanged scope set never
    shows up as a diff in the console or the audit log."""
    names = scopes.scope_names({scopes.Scope.RESPONSES_WRITE, scopes.Scope.MODELS_READ})

    assert names == ["models.read", "responses.write"]
    # Deduplicated, because this is given whatever a console form posted and a
    # list is not a set. It used to write ["models.read", "models.read"] into
    # the jsonb column the console renders and the audit log quotes.
    assert scopes.scope_names(["models.read", "models.read"]) == ["models.read"]
    assert scopes.scope_names(
        [scopes.Scope.USAGE_READ, "usage.read", scopes.Scope.USAGE_READ]
    ) == ["usage.read"]
    assert json.loads(json.dumps(names)) == names
    assert scopes.parse_scopes(json.loads(json.dumps(names))) == {
        scopes.Scope.MODELS_READ,
        scopes.Scope.RESPONSES_WRITE,
    }


def test_the_wire_form_is_space_delimited_per_rfc_6749():
    assert (
        scopes.format_scopes({scopes.Scope.RESPONSES_WRITE, scopes.Scope.MODELS_READ})
        == "models.read responses.write"
    )


def test_a_route_must_declare_at_least_one_scope():
    """Deny by default (OWASP API5:2023): an empty requirement would read as
    'authenticated is enough', which is never true on this surface."""
    with pytest.raises(ValueError):
        scopes.requires()


def test_a_requirement_of_two_scopes_needs_both():
    requirement = scopes.requires(scopes.Scope.RESPONSES_READ, scopes.Scope.USAGE_READ)

    assert requirement.satisfied_by({scopes.Scope.RESPONSES_READ}) is False
    assert requirement.missing({scopes.Scope.RESPONSES_READ}) == {
        scopes.Scope.USAGE_READ
    }
    assert requirement.satisfied_by(
        {scopes.Scope.RESPONSES_READ, scopes.Scope.USAGE_READ}
    ) is True


def test_the_refusal_carries_the_error_envelope_fields_and_names_the_scope():
    """RFC 6750: naming the required scope is the machine-actionable way for a
    client to learn what to ask for, and is not a disclosure — the caller
    already holds a credential for this project."""
    error = scopes.InsufficientScopeError(
        {scopes.Scope.RESPONSES_WRITE}, {scopes.Scope.MODELS_READ}
    )

    assert error.code == "insufficient_scope"
    # Bound to the AUTHORITY, not to a second copy of it (2026-09-13).
    # `scopes.py`'s own comment names `publicapi/errors.py::_CODES` as the one
    # place the envelope is decided, on the grounds that two spellings of the
    # same 403 would make the wire contract depend on which layer raised it —
    # but the assertion used to be `== 403` / `== "permission_error"` with no
    # import, so the two could drift apart with the suite green. The import
    # stays HERE and not in `scopes.py`: that module is framework-free on
    # purpose.
    from app.publicapi.errors import _CODES

    authority = _CODES[error.code]
    assert (error.status_code, error.type) == (authority.status, authority.type)
    assert error.status_code == 403
    assert error.type == "permission_error"
    assert "responses.write" in str(error)
    assert error.challenge() == 'Bearer error="insufficient_scope", scope="responses.write"'
    # What the caller DOES hold is for our log line, not for the wire.
    assert "models.read" not in error.challenge()


def test_the_default_scope_set_is_the_narrow_one():
    """STANDARDS.md (Stripe): the restricted key is the default creation path
    and the broad one is the exception, so a leak's blast radius is small by
    default rather than by remembering to untick boxes."""
    assert scopes.DEFAULT_SCOPES < scopes.ALL_SCOPES
    # A billing dashboard has no business being able to spend the quota it is
    # reading, so `usage.read` is opted into rather than granted by default.
    assert scopes.Scope.USAGE_READ not in scopes.DEFAULT_SCOPES


def test_a_scope_is_a_plain_string_for_storage_and_comparison():
    assert scopes.Scope.MODELS_READ == "models.read"
    assert json.dumps([scopes.Scope.MODELS_READ]) == '["models.read"]'
