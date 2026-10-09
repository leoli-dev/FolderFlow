# Security

The application uses the current Windows session for filesystem/SMB access and
does not store credentials. It does not provide independent backup encryption.
Task state and reports can contain private paths and filenames; keep them local.

For a security issue, use GitHub's private vulnerability reporting if enabled on
the public repository. Otherwise contact a maintainer privately before posting
sensitive details. Do not submit credentials or personal backup databases.

Include the version, affected mode and a reproduction using synthetic temporary
files. Security fixes are developed against the current maintained source.
