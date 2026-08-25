from hermes_services.hosted_role_migration import migrate_hosted_container


def test_legacy_manager_profile_is_renamed_without_scanning_user_text():
    value = {
        "profile": "dbb3-manager",
        "content": "The reviewer should remain ordinary user text.",
        "messages": [{"role": "user", "content": "reviewer"}],
    }
    migrated, removed, changed = migrate_hosted_container(value)
    assert migrated["profile"] == "hermes-manager"
    assert migrated["content"].startswith("The reviewer")
    assert migrated["messages"] == value["messages"]
    assert removed == 0
    assert changed is True


def test_retired_role_turn_is_removed_as_a_whole_unit():
    value = {
        "hosted_turns": {
            "keep": {"profile": "hermes-manager", "messages": []},
            "drop": {"role_stage": "supervisor", "messages": [{"content": "x"}]},
            "drop_event": {"event_type": "supervisor.verdict", "payload": {}},
        }
    }
    migrated, removed, changed = migrate_hosted_container(value)
    assert set(migrated["hosted_turns"]) == {"keep"}
    assert removed >= 2
    assert changed is True


def test_nested_members_and_participants_are_filtered():
    value = {
        "participants": [
            {"role": "worker", "profile": "dbb3-worker"},
            {"role": "reviewer", "profile": "default"},
        ],
        "plan": {"nodes": [{"role": "dbb3-manager"}, {"role": "worker"}]},
    }
    migrated, removed, _ = migrate_hosted_container(value)
    assert len(migrated["participants"]) == 1
    assert migrated["plan"]["nodes"][0]["role"] == "hermes-manager"
    assert removed >= 1


def test_removed_list_entry_is_counted_once():
    migrated, removed, changed = migrate_hosted_container(
        [{"role": "reviewer", "messages": []}]
    )

    assert migrated == []
    assert removed == 1
    assert changed is True
