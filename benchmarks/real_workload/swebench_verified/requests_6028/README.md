# Requests proxy-auth real-agent ablation

This public-replay task adapts `psf__requests-6028` from the frozen
SWE-bench Verified dataset revision recorded in `provenance.json`.

The task checkout is pinned to the pre-fix base commit. The prompt comes from
the public issue before the benchmark cutoff. The helpful memory is derived
from an earlier ancestor commit that migrated the URL parser and exposed
authentication as a separate parsed component. It is a reconstructed replay
memory, not a claim that MemoryOS was deployed in the Requests project.

The hidden scorer makes no network calls and imports no repository
dependencies. It extracts the target function with `ast`, injects a minimal
urllib3-compatible parser double, and checks that authenticated URLs survive a
round trip. The test fails on the pinned base and passes on the official merge
commit.
