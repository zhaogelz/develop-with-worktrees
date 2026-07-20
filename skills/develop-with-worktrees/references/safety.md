# Safety reference

The candidate gate reports only path, line number, and rule name. It blocks newly added `.env*`, private-key/certificate files, credential files, recognizable private-key headers, and common provider/token formats unless an explicit repository allowlist matches.

Validation logs redact recognizable secret values before writing. Logs and proof records remain local. Proof identity binds the candidate commit and tree, current local default head, tracked policy hashes, selected commands, lockfile hashes, resolved tool paths and versions, platform, and persistent log paths. Time is audit metadata only.

Finish and abandon keep known per-slot caches and environment files. Unknown ignored files block release because ownership cannot be proven. Development processes are stopped only when their recorded PID, creation time, executable, working directory, and command line still match.

The lifecycle never performs network synchronization. Publishing remains a separate, explicit user decision after local integration.
