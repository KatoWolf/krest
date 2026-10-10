import pytest


@pytest.fixture
def issuer(tmp_path, monkeypatch):
    import licensing
    from tools.license_admin import generate, issue

    private, public = tmp_path / "test.private.pem", tmp_path / "public.pem"
    generate(private, public)
    monkeypatch.setattr(licensing, "PUBLIC_KEY_PATH", public)
    return private, public, issue(private)


@pytest.fixture
def premium_config(tmp_path, issuer):
    from configuration import ConfigManager

    cfg = ConfigManager(tmp_path / "config.json")
    draft = cfg.snapshot()
    draft["license"]["code"] = issuer[2]
    cfg.save_config(draft)
    return cfg


@pytest.fixture(autouse=True)
def no_automatic_model_network_during_tests(monkeypatch):
    # Tests may start a runtime on Windows. Downloads are exercised with fixtures,
    # never by silently fetching voice models on the CI runner.
    import model_setup

    def offline(*args, **kwargs):
        raise OSError("Model downloads are disabled in tests")

    monkeypatch.setattr(model_setup, "download", offline)
