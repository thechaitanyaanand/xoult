import psycopg
import pytest

from xoult import db


@pytest.mark.parametrize(
    ("error", "hint"),
    [
        ("FATAL:  (ENOTFOUND) tenant/user postgres.abc not found", "wrong pooler cluster"),
        ('FATAL:  password authentication failed for user "postgres"', "wrong password"),
        ("connection to server failed: Network is unreachable", "IPv6-only"),
        ("connection refused", "is DATABASE_URL set"),
    ],
)
def test_explain_failure_names_the_cause_and_hides_the_password(monkeypatch, error, hint):
    url = "postgresql://postgres.abc:s3cret@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
    monkeypatch.setattr(db.settings, "database_url", url)
    text = db.explain_failure(psycopg.OperationalError(error))
    assert hint in text
    assert "postgres.abc@aws-0-ap-south-1.pooler.supabase.com:5432" in text
    assert "s3cret" not in text
