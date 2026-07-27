from hermes_runtime.secret_provenance import (
    clear_secret_sources,
    get_secret_source,
    record_secret_source,
)


def setup_function() -> None:
    clear_secret_sources()


def teardown_function() -> None:
    clear_secret_sources()


def test_records_only_non_empty_source_metadata() -> None:
    record_secret_source("ANTHROPIC_API_KEY", "bitwarden")
    record_secret_source("", "vault")
    record_secret_source("OPENAI_API_KEY", "")

    assert get_secret_source("ANTHROPIC_API_KEY") == "bitwarden"
    assert get_secret_source("OPENAI_API_KEY") is None


def test_latest_source_label_wins_without_storing_secret_values() -> None:
    record_secret_source("OPENAI_API_KEY", "onepassword")
    record_secret_source("OPENAI_API_KEY", "bitwarden")

    assert get_secret_source("OPENAI_API_KEY") == "bitwarden"
