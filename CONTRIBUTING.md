# Contributing

Use Windows x64 and Python 3.13. Run `scripts/test.ps1` before proposing changes;
release-affecting changes should also pass the default `scripts/build.ps1`.

Keep source folders read-only in the supported one-way modes. Preserve durable
resume state and never count a failed or incomplete file as verified. Mirror
recycling must stop when the source is inaccessible or copying has failed.

Tests must use temporary folders and synthetic data. Do not connect to personal
devices, modify real backups, or embed real addresses, accounts and local paths.
Runtime state, logs and binary artifacts do not belong in commits.

Describe the concrete behavior change and relevant validation in pull requests.
Bug reports should include mode, reproduction steps and redacted error text;
do not attach personal manifests, task configuration or file contents.

By contributing, you agree to license your application-source contributions under
the project's MIT license. Third-party code retains its original license.
