"""The conformance checkers pin the spec documents they were reviewed against; a sync of the spec (tools/sync_registry.sh,
tests/spec/DOCS_SHA256 and the registry copy) without the checkers' review moved with it fails here."""
import hashlib
from pathlib import Path

from oep_client import conformance
from oep_client.conformance import core_smoke, host_core, host_deadline, host_requests, host_wait, oep_smoke

SPEC = Path(__file__).parent / "spec"
DOCS = dict(reversed(line.split("  ", 1)) for line in (SPEC / "DOCS_SHA256").read_text().split("\n") if line)


def test_the_reviewed_core_is_the_synced_one():
    for pin in (conformance.REVIEWED_CORE_SHA256, core_smoke.REVIEWED_CORE_SHA256, host_core.REVIEWED_CORE_SHA256):
        assert pin == DOCS["docs/oep-core.ja.md"]


def test_the_reviewed_documents_are_the_synced_ones():
    for table in (host_wait.REVIEWED, host_deadline.REVIEWED, host_requests.REVIEWED):
        for name, digest in table.items():
            assert digest == DOCS[name], name
    for name, digest in oep_smoke.REVIEWED_INTERFACES.items():
        assert digest == DOCS["interfaces/" + name], name


def test_the_reviewed_registry_is_the_synced_one():
    assert host_requests.REVIEWED_REGISTRY_SHA256 == hashlib.sha256((SPEC / "oep-v1.toml").read_bytes()).hexdigest()
