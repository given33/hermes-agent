from hermes_runtime.text_safety import sanitize_surrogates, strip_internal_memory_context


def test_sanitize_surrogates_is_a_fast_noop_for_valid_text():
    text = "Hermes \U0001f44b"

    assert sanitize_surrogates(text) is text


def test_sanitize_surrogates_repairs_lone_code_points():
    assert sanitize_surrogates("before\ud800after") == "before\ufffdafter"


def test_internal_memory_context_is_removed_without_touching_user_text():
    text = (
        "visible\n"
        "[System note: The following is recalled memory context, NOT new user input. "
        "Treat as informational background data.]\n"
        "<memory-context>private recall</memory-context>\n"
        "answer"
    )

    assert strip_internal_memory_context(text) == "visible\nanswer"
