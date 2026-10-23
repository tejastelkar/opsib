"""The supplied fixture files are inputs and must stay byte-for-byte unchanged."""

import hashlib

from tests.helpers import FIXTURES


def test_fixture_files_match_recorded_checksums():
    recorded = {}
    for line in (FIXTURES.parent / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        recorded[name.strip()] = digest
    assert set(recorded) == {p.name for p in FIXTURES.iterdir() if p.is_file()}
    for name, digest in recorded.items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == digest, name
