from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch


def test_concurrent_records_preserve_every_message(tmp_path):
    from gateway import rich_sent_store

    store_path = tmp_path / "rich_sent_index.json"
    with patch.object(rich_sent_store, "_store_path", return_value=str(store_path)):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(
                pool.map(
                    lambda index: rich_sent_store.record(
                        "chat", str(index), f"message {index}"
                    ),
                    range(60),
                )
            )

        assert {
            rich_sent_store.lookup("chat", str(index)) for index in range(60)
        } == {f"message {index}" for index in range(60)}
