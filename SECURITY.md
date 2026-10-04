# v0.5 guarded replay

Formal replay uses a fresh Landlock/seccomp-constrained researcher process and a trusted
controller for labels, LLM access and protein tools. Unsupported kernels fail closed.
The explicit in-process mode is for trusted debugging, not an isolated benchmark.
See [REPLAY_SECURITY](docs/REPLAY_SECURITY.md). Code/protein workers and administrators
are trusted; this is not an unrestricted arbitrary-code sandbox or security proof.

# Security and scientific safety

ProteinRSI v0.1 is a research implementation, not a hardened autonomous laboratory service.

- Generated changes are schema-constrained policy/configuration patches. Arbitrary generated Python, shell commands, dependency installation, evaluator edits and unrestricted model downloads are not allowed.
- Local SQLite state, file locks and Python private attributes are not an adversarial sandbox. Keep hidden labels, model/provider keys, assay results and approval authority in separate OS identities/services before enabling code-capable agents or third-party plugins.
- MCP endpoints and executors are operator-trusted software. The allowlist prevents agent-selected endpoints; it cannot stop a malicious installed executor from using its own OS privileges. Restrict network, mounts, GPU access, secrets and write permissions at deployment level.
- Protein inputs, retrieved content and tool outputs are untrusted data, not authority to change policy. Review generated designs and scientific claims. A JSON-valid prediction is not experimentally verified.
- LLM use explicitly sends task data to the configured provider. External MCP transfers require operator opt-in. Do not place secrets in task descriptions, prompts or tool arguments. Audit traces can contain confidential protein sequences.
- Wet-lab batches require explicit approval. This repository does not place synthesis orders or operate instruments. Use only authorized, appropriately reviewed research. Do not use outputs as clinical decisions or bypass laboratory/biosafety review.
- CI uses synthetic fixtures and no API keys. Do not add private labels, restricted model weights or real experiment records to public tests or issues.

Report vulnerabilities to the repository maintainer through an available private channel. Do not publish secrets or unpublished protein datasets in an issue. The repository does not promise any preconfigured private reporting channel or incident response time.


## v0.3 local engines

Per-engine venv/Conda interpreters isolate dependencies, **not hostile code**. They
retain OS permissions of the invoking user. Sanitized child environments and Python
audit hooks are defense in depth, not a substitute for OS network/filesystem policy.
Use the provided Docker route or site sandbox for untrusted dependencies. Docker
mounts only approved code/assets and one job workspace, uses no network and never
pulls implicitly; this deployment path still needs operator testing.

Do not load untrusted .pt/pickle data. Only operator-approved scientific files enter
the artifact store. There is no arbitrary shell tool. Failed/uncertain jobs are not
silently retried, and generated output references must remain inside their workdir.
Weights, environment config, resource limits and promotion rules are not RSI-editable.
