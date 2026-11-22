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
  - `failure`: a planned failure's sentence, or a fixed one for anything else, and
    `guarded`, a tool decorator so nothing unplanned escapes a tool;
  - `recent` and `annotations`;
  - `testing`: the security regression checks each server runs on itself.
- **The audit line names the client** (`client=`; second-brain-joplin #92): the name
  it registered under at sign-in, else the name it gives in the MCP handshake (or in
  each request, on protocols without one), else the first word of its `User-Agent`,
  else `unknown`. Letters, digits and `._/-` only, at most 40 characters, so a hostile
  value can't break or forge a line. It is what the client says about itself: a hint
  for the operator, never proof, and nothing is decided by it. `this_calls_client()`
  gives it to a line a tool logs itself.
- **The search engine, as the `semantic` extra** (second-brain-joplin #93), moved here
  from second-brain-joplin so the mail server can search by meaning too:
  - `Engine` keeps an index in step with a server's `Source` (its items, their versions,
    which are visible) and searches it. A hidden item never reaches a result: with a
    filter active, every search asks the source what is visible now, whatever the
    index holds.
  - Neutral names (`item_id`, `version`). An index file from second-brain-joplin 1.2 is
    migrated in place, in one transaction, keeping every vector (sqlite-vec); a Chroma
    index from then is rebuilt by itself.
  - The index file records its layout's version, the server that owns it (a second
    server pointed at the same directory is refused: each sync would delete the
    other's items), and the vectors' width (a new width under the same model name now
    rebuilds the index).
  - **A word index beside the vectors** (sqlite-vec): SQLite FTS5, ignoring case and
    accents; a word hyphenated across a line break is found joined and as written.
    `Engine.word_search` applies the same visibility rule. The Chroma store says plainly
    that it has no word search, and so does a SQLite built without FTS5.
  - `semantic.testing`: in-memory stand-ins (embedder, index, an index another process
    writes to, a source) for any server's tests.
- **FastMCP's "Error calling tool" log line loses its traceback.** When an exception
  escaped a tool, FastMCP logged it with the exception's text, which can quote what
  was being processed. `configure_logging` now drops the traceback and keeps the line.

[Unreleased]: https://github.com/second-brain-tools/second-brain-core/commits/main
