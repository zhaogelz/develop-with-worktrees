# Safety reference

The CLI is the lifecycle gate. The optional hook is a reminder, not operating-system enforcement.

Commands use explicit argv and `shell=False`. Logs redact common credentials; proofs persist command digests, hashes, and redacted displays but not environment values, leases, or raw command lines. Validation subprocesses have identity snapshots, heartbeats, hard timeouts, and persistent receipts. Recovery never kills or adopts an unverified process.

The built-in content gate blocks newly added `.env*`, private keys, credential files, and recognizable secret patterns. A repository scanner may supplement it only when explicitly configured.

Finish never cleans caches or dependencies. Manual `prune-slot` is bounded to declared top-level ownership paths and requires a reviewed plan id plus digest. The plan records path, reason, byte size, and content digest. If any declared target contains `.env*`, a symlink or junction, or changes before execution, the whole prune stops without deleting anything. Content outside declared targets is retained.
