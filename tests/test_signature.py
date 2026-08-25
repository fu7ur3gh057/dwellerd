from dwellerd.logs.signature import compute_signature, normalize


def test_numbers_collapse():
    assert normalize("request 12345 failed") == "request <n> failed"


def test_uuid_collapses():
    line = "job 3f2504e0-4f89-11d3-9a0c-0305e82c3301 failed"
    assert normalize(line) == "job <uuid> failed"


def test_hex_collapses_before_numbers():
    assert normalize("segfault at 0x00007fff5fbff8c0") == "segfault at <hex>"


def test_quoted_strings_collapse():
    assert normalize("user 'alice' not found") == "user <str> not found"


def test_same_error_different_ids_share_a_signature():
    a = compute_signature("connection refused id=1234", "app")
    b = compute_signature("connection refused id=5678", "app")
    assert a == b


def test_different_errors_differ():
    a = compute_signature("connection refused", "app")
    b = compute_signature("disk quota exceeded", "app")
    assert a != b


def test_same_line_from_different_sources_differs():
    # Otherwise one source's first-seen would silence the other's.
    a = compute_signature("connection refused", "app")
    b = compute_signature("connection refused", "db")
    assert a != b


def test_signature_is_short_and_stable():
    sig = compute_signature("anything", "src")
    assert len(sig) == 16
    assert sig == compute_signature("anything", "src")
