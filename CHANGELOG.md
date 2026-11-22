# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0, a minor version
may change the interfaces.

## [Unreleased]

### Added

- **The shared serving pieces of the Second Brain servers**, moved here from
  second-brain-joplin and second-brain-imap, which carried copies of them
  (second-brain-joplin #91):
  - `Server`, the description every piece takes, so nothing here names a product;
  - `env`: a named env file *or* a checkout's `.env`, never both, and readers that
    record a bad value instead of raising;
  - `auth`: GitHub sign-in with an allow-list of numeric ids;
  - `audit`: the audit line, naming a server's own failures by class; the per-caller
    rate limit; the logging set-up;
  - `remote` and `health`: HTTP options and guardrails, the transport choice, the
    middleware stack, the run calls, and `/healthz` with an optional cache;
  - `failure`: a planned failure's sentence, or a fixed one for anything else;
  - `recent` and `annotations`;
  - `testing`: the security regression checks each server runs on itself.
- **FastMCP's "Error calling tool" log line loses its traceback.** When an exception
  escaped a tool, FastMCP logged it with the exception's text, which can quote what
  was being processed. `configure_logging` now drops the traceback and keeps the line.

[Unreleased]: https://github.com/second-brain-tools/second-brain-core/commits/main
