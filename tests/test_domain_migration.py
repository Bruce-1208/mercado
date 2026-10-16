from scripts.migrate_workbench_domain import migrate_environment, migrated_url


def test_service_environment_keeps_legacy_origins_and_unrelated_settings():
    previous = {
        "BIT_DB_API_BASE_URL": "https://wuhanzeshun.com",
        "BIT_PUBLIC_WORKBENCH_URL": "https://wuhanzeshun.com",
        "BIT_LOCAL_EXECUTOR_ALLOWED_ORIGINS": "https://wuhanzeshun.com,https://custom.test",
        "UNRELATED_SETTING": "unchanged",
    }
    updated = migrate_environment(previous)
    assert updated["BIT_DB_API_BASE_URL"] == "https://wuhanzeshun.cn"
    assert updated["BIT_PUBLIC_WORKBENCH_URL"] == "https://wuhanzeshun.cn"
    assert updated["UNRELATED_SETTING"] == "unchanged"
    assert set(updated["BIT_LOCAL_EXECUTOR_ALLOWED_ORIGINS"].split(",")) == {
        "https://wuhanzeshun.com", "https://www.wuhanzeshun.com",
        "https://wuhanzeshun.cn", "https://www.wuhanzeshun.cn", "https://custom.test",
    }
    assert previous["BIT_PUBLIC_WORKBENCH_URL"] == "https://wuhanzeshun.com"
    assert migrate_environment(updated) == updated


def test_domain_replacement_only_changes_exact_hosts_and_retains_url_components():
    assert migrated_url("https://WWW.WUHANZESHUN.COM:8443/zs?q=1#part") == "https://www.wuhanzeshun.cn:8443/zs?q=1#part"
    for value in ["https://custom.test", "http://127.0.0.1:5000", "https://wuhanzeshun.com.evil.test"]:
        assert migrated_url(value) == value
