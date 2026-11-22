# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately via GitHub Security Advisories
(**Security → Report a vulnerability** on the repository) rather than opening a public
issue. We aim to acknowledge reports within a few days.

## What this library is responsible for

The servers built on it serve personal stores (notes, mail) to AI clients, some of
them over the internet. These protections are written here once, so every server
has them:

- **Settings come only from where the operator put them.** The environment, then a
  file named with `--env-file` or, in a source checkout only, the `.env` beside the
  project's own `pyproject.toml`; never both files, never the working directory's
  `.env`, never one above where the server is installed. Importing the package stops
  FastMCP from reading its own settings, the transport among them, from the working
  directory.
- **stdio unless asked.** A server listens on the network only for an explicit HTTP
  transport, and then refuses to start without GitHub sign-in (except `--no-auth` on
  loopback, for a local test).
- **GitHub sign-in, limited to listed numeric ids**, checked by the token verifier
  and again for every tool. Ids, not logins: a login can be renamed and re-registered.
- **A strict Host check** (421 for unknown hosts), stateless HTTP with JSON responses,
  and a per-caller rate limit of tool calls.
- **Logs hold the call, never the content.** One line per tool call (tool, caller,
  client, outcome by class name, duration). HTTP client loggers that would print a token in a
  URL are held at WARNING, the access log that would print an OAuth code is off,
  FastMCP's loggers are held out of DEBUG (which prints arguments), and the traceback
  is dropped from FastMCP's "Error calling tool" line (it carries the exception's
  text).
- **The client sees a planned failure's own sentence, or a fixed one.** An unplanned
  exception's text can quote what was being processed, so neither the client nor the
  log gets it.

- **Search by meaning shows only what the server shows.** An index is a file other
  processes can write, so with a filter active every search asks the server which items
  are visible now and returns only those, whatever the index holds. An item that
  becomes hidden leaves the index at the next sync, before anything new is embedded.
- **An index holds the indexed text unencrypted**, in the server's index directory
  (created private, 0700, where the system has modes). Each server's docs say where.
  One index belongs to one server; a second server is refused the same directory.

## Regression checks

`second_brain_core.testing` holds checks each server runs on itself in its own CI:
that a working directory's `.env` can't set FastMCP's settings, and that each entry
point imports on its own. Their own tests, here, include a control that shows each
check catches the mistake it is for.
