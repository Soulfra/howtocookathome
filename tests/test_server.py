"""Tests for the HTCAH web server — routes, health check, tenant routing."""
import os
import sys
import json
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestHealthEndpoint:
    def test_health_json_exists(self):
        """The publish step should create a health.json file."""
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        health_path = os.path.join(base, "output", "api", "health.json")
        if not os.path.exists(health_path):
            pytest.skip("Run publisher first: python -m app.publish")
        with open(health_path) as f:
            data = json.load(f)
        assert "status" in data


class TestReservedSlugs:
    def test_reserved_slugs_exist(self):
        from app.server import RESERVED_SLUGS
        assert "www" in RESERVED_SLUGS
        assert "api" in RESERVED_SLUGS
        assert "admin" in RESERVED_SLUGS
        assert "htcah" in RESERVED_SLUGS

    def test_platform_hosts_exist(self):
        from app.server import PLATFORM_HOSTS
        assert "howtocookathome.com" in PLATFORM_HOSTS
        assert "localhost" in PLATFORM_HOSTS


class TestWSGIApp:
    def test_app_is_callable(self):
        from app.server import app
        assert callable(app)
