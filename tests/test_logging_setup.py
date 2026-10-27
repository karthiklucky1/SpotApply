"""Prod logging setup: without it, the app's INFO logs vanish under uvicorn
(the root logger has no handler, so Python's last-resort handler prints only
WARNING+ and every lane diagnostic is dropped)."""
import logging

from app.common.logging_setup import setup_logging, _HANDLER_MARKER


def _drop_our_handlers():
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _HANDLER_MARKER, False):
            root.removeHandler(h)


def test_setup_adds_info_handler_and_emits(caplog):
    _drop_our_handlers()
    setup_logging()
    root = logging.getLogger()
    ours = [h for h in root.handlers if getattr(h, _HANDLER_MARKER, False)]
    assert len(ours) == 1, "exactly one tagged handler must be attached"
    assert root.level == logging.INFO

    # An app-style INFO record now passes the level threshold.
    with caplog.at_level(logging.INFO, logger="hot_lane"):
        logging.getLogger("hot_lane").info("Hot lane ENABLED")
    assert any("Hot lane ENABLED" in r.message for r in caplog.records)


def test_setup_is_idempotent():
    _drop_our_handlers()
    setup_logging()
    setup_logging()
    setup_logging()
    root = logging.getLogger()
    ours = [h for h in root.handlers if getattr(h, _HANDLER_MARKER, False)]
    assert len(ours) == 1, "re-running must not stack duplicate handlers"


def test_only_errors_go_to_stderr(capsys):
    """Railway tags every stderr line as an error. With everything on stderr,
    INFO heartbeats buried the real failures (2026-10-26: every tailor 400'd
    on Claude for hours, unnoticed in an "error" stream of routine lines)."""
    _drop_our_handlers()
    setup_logging()
    log = logging.getLogger("split_stream_probe")
    log.info("routine heartbeat")
    log.warning("a board returned 429")
    log.error("tailor failed")
    out, err = capsys.readouterr()
    assert "routine heartbeat" in out and "a board returned 429" in out
    assert "routine heartbeat" not in err and "a board returned 429" not in err
    assert "tailor failed" in err and "tailor failed" not in out


def test_noisy_libraries_pinned_to_warning():
    setup_logging()
    for noisy in ("httpx", "sentence_transformers", "faiss", "uvicorn.access"):
        assert logging.getLogger(noisy).level == logging.WARNING


def test_respects_log_level_env(monkeypatch):
    _drop_our_handlers()
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    setup_logging()
    assert logging.getLogger().level == logging.WARNING
    # Explicit arg overrides the env.
    setup_logging(logging.DEBUG)
    assert logging.getLogger().level == logging.DEBUG
