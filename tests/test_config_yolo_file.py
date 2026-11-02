"""Tests for ODOO_YOLO_FILE: switching read <-> true at runtime without a restart."""

import os
from unittest.mock import patch

import pytest

from mcp_server_odoo.config import OdooConfig, load_config


def _config(yolo_mode, path):
    return OdooConfig(
        url="http://localhost:8069",
        username="admin",
        password="admin",
        yolo_mode=yolo_mode,
        yolo_mode_file=str(path) if path else None,
    )


@pytest.mark.unit
class TestYoloModeFile:
    def test_without_file_setting_keeps_startup_mode(self):
        config = _config("read", None)
        assert config.yolo_mode == "read"
        assert not config.is_write_allowed

    def test_missing_file_keeps_startup_mode(self, tmp_path):
        config = _config("true", tmp_path / "absent")
        assert config.yolo_mode == "true"

    def test_file_switches_read_to_true_and_back_without_restart(self, tmp_path):
        level = tmp_path / "level"
        level.write_text("read")
        config = _config("read", level)
        assert not config.is_write_allowed

        level.write_text("true\n")
        assert config.yolo_mode == "true"
        assert config.is_write_allowed

        level.write_text("read")
        assert config.yolo_mode == "read"
        assert not config.is_write_allowed

    @pytest.mark.parametrize("content", ["", "off", "garbage", "false"])
    def test_invalid_or_off_content_falls_back_to_read(self, tmp_path, content):
        level = tmp_path / "level"
        level.write_text(content)
        config = _config("true", level)
        assert config.yolo_mode == "read"
        assert not config.is_write_allowed
        assert config.is_yolo_enabled

    def test_unreadable_file_falls_back_to_read(self, tmp_path):
        config = _config("true", tmp_path)  # a directory cannot be read as text
        assert config.yolo_mode == "read"

    def test_file_ignored_when_server_started_without_yolo(self, tmp_path):
        level = tmp_path / "level"
        level.write_text("true")
        config = OdooConfig(
            url="http://localhost:8069",
            api_key="key",
            yolo_mode="off",
            yolo_mode_file=str(level),
        )
        assert config.yolo_mode == "off"
        assert not config.is_yolo_enabled

    def test_load_config_reads_env_var(self, tmp_path):
        level = tmp_path / "level"
        level.write_text("true")
        env = {
            "ODOO_URL": "http://localhost:8069",
            "ODOO_USER": "admin",
            "ODOO_PASSWORD": "admin",
            "ODOO_YOLO": "read",
            "ODOO_YOLO_FILE": str(level),
        }
        with patch.dict(os.environ, env, clear=True):
            config = load_config()
        assert config.yolo_mode_file == str(level)
        assert config.yolo_mode == "true"
