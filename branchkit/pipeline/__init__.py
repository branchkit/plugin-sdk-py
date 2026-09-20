"""The BranchKit pipeline wire vocabulary — the Python stage port.

Hand-written, unlike the tier subpackages: the framing reader/writer lives
here alongside the generated event vocabulary, mirroring `pipeline.go` and
`pipeline.ts`. Today this re-exports the generated core tier; the framing
lands next (DESIGN_BLOB_CHANNEL.md step 0 build item).

Tiers are subpackages so the default import stays domain-free — someone
writing a foot pedal should not be handed a command-grammar DAG:

    from branchkit.pipeline import Capability          # core, always
    from branchkit.pipeline.audio import AudioChunk    # opt in
"""

from .events_gen import *  # noqa: F401,F403
