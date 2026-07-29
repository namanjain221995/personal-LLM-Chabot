import logging

from syncworker.sf_client import (
    LIMIT_WARN_THRESHOLD,
    check_api_limits,
    parse_limit_info,
)


def test_parse_limit_info_basic():
    assert parse_limit_info("api-usage=18/15000") == (18, 15000)


def test_parse_limit_info_with_other_entries():
    header = "api-usage=12005/15000; per-app-api-usage=1/2000(appName=sync)"
    assert parse_limit_info(header) == (12005, 15000)


def test_parse_limit_info_malformed_or_missing():
    assert parse_limit_info(None) is None
    assert parse_limit_info("") is None
    assert parse_limit_info("garbage") is None
    assert parse_limit_info("api-usage=notanumber/15000") is None


def test_no_warning_below_threshold(caplog):
    logger = logging.getLogger("test.limits.below")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        ratio = check_api_limits("api-usage=11999/15000", logger)
    assert ratio is not None and ratio < LIMIT_WARN_THRESHOLD
    assert not caplog.records


def test_warning_at_exactly_80_percent(caplog):
    logger = logging.getLogger("test.limits.at")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        ratio = check_api_limits("api-usage=12000/15000", logger)
    assert ratio == LIMIT_WARN_THRESHOLD
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].api_used == 12000
    assert warnings[0].api_total == 15000


def test_warning_above_threshold(caplog):
    logger = logging.getLogger("test.limits.above")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        check_api_limits("api-usage=14999/15000", logger)
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_malformed_header_never_warns_or_crashes(caplog):
    logger = logging.getLogger("test.limits.malformed")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        assert check_api_limits("bogus-header", logger) is None
        assert check_api_limits(None, logger) is None
    assert not caplog.records
