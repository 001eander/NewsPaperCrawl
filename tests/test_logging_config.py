import logging
from logging.handlers import TimedRotatingFileHandler

from crawler import logging_config


def test_setup_logging_adds_single_file_handler_to_existing_gui_handler(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(logging_config, "LOG_DIR", tmp_path)
    root = logging.getLogger()
    previous_handlers = root.handlers[:]
    previous_level = root.level
    marker = logging.NullHandler()
    root.handlers = [marker]
    try:
        logging_config.setup_logging()
        logging_config.setup_logging()
        files = [
            handler
            for handler in root.handlers
            if isinstance(handler, TimedRotatingFileHandler)
        ]
        assert len(files) == 1
        assert root.handlers[0] is marker
        logging.getLogger("desktop-test").info("落盘验证")
        files[0].flush()
        assert "落盘验证" in (tmp_path / "crawl.log").read_text(encoding="utf-8")
    finally:
        for handler in root.handlers:
            if handler not in previous_handlers and handler is not marker:
                handler.close()
        root.handlers = previous_handlers
        root.setLevel(previous_level)
