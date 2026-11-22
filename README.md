# second-brain-core

The shared library of the Second Brain MCP servers:
[second-brain-joplin](https://github.com/second-brain-tools/second-brain-joplin) (notes)
and its sibling for mail. Each server talks to one store; everything they do the same
way lives here, written and tested once.

> **0.x: the interfaces may still change.** Each server pins a compatible range
> (`second-brain-core>=0.2,<0.3`), and a change that breaks one is a new minor version.

## What it holds

| Module | What it does |
|---|---|
| `server` | `Server`: how a server describes itself (its name, variable prefix, package, port). Every other piece takes it, so nothing here names a product. |
| `env` | Where settings come from (the environment, then a named `--env-file` *or* a checkout's `.env`, never both), and readers that never raise. |
| `auth` | GitHub sign-in for HTTP mode through FastMCP's OAuth proxy, limited to listed numeric GitHub ids. |
| `audit` | One log line per tool call (tool, caller, client, outcome, duration; never arguments, results or tokens), the per-caller rate limit, and the logging set-up that keeps tokens and content out of the log. |
| `remote` | HTTP mode's options, the start-up guardrails, the transport choice (stdio unless asked), the middleware stack, and the run calls. |
| `health` | The `/healthz` route: two booleans, 503 when the store is down. |
| `failure` | The error a client sees: a planned failure's own sentence, or a fixed one for anything unplanned. |
| `recent` | A short memory of what a write answered, so a repeated request writes nothing new. |
| `annotations` | The tool annotations every server uses the same way. |
| `textclean` | Outside text made safe to hand to a model: invisible and bidirectional characters removed, HTML reduced to the text a reader sees (hidden text dropped), and `outside_text_notice`, the sentence an answer carrying such text puts first. |
| `testing` | Security regression checks each server runs against itself in its own CI, including `check_no_invisible_characters` over its own source. |
| `semantic` | **The `semantic` extra:** search by meaning, and by words, over a server's items. The server supplies a `Source`; the `Engine` keeps the index in step and searches it, returning only what the source says is visible now. Stores: sqlite-vec (the default, with an FTS5 word index) and Chroma. `semantic.testing` has in-memory stand-ins for tests. |

## Using it in a server

```python
# my_server/__init__.py: import the library first, and nothing that imports fastmcp.
import second_brain_core  # noqa: F401  (pins FastMCP's env file before fastmcp loads)

# my_server/app.py
from second_brain_core.server import Server

SERVER = Server(name="my-server", prefix="SBM_", package="my_server", default_port=8777)
```

**Importing `second_brain_core` must come before anything imports fastmcp.** FastMCP
reads its own settings, the transport among them, from a `.env` in the working
directory when it is first imported; importing this package stops that. Keep the
server's `__init__.py` free of other imports, because import sorting would move a
`fastmcp` import above it. `testing.check_dotenv_cannot_set_fastmcp` catches the
mistake; run it in the server's suite.

Then build the server with `remote.build_middleware(SERVER, ...)` and
`mask_error_details=True`, end every tool with `failure.tool_error` for the failures
it plans for and `failure.unexpected` for the rest, and serve with
`remote.run_stdio` or `remote.run_http`.

## Search by meaning

```bash
pip install "second-brain-core[semantic]"            # sqlite-vec + FastEmbed (the default)
pip install "second-brain-core[semantic,semantic-chroma]"   # the Chroma store
pip install "second-brain-core[semantic,semantic-st]"       # sentence-transformers models
```

A server re-exports these as its own extras. Each server keeps **its own index
directory**: the index file records which server owns it and refuses another. The
embedding runtime has no build for Python 3.15 yet, so the extra needs 3.11 to 3.14.

## Releasing

The library is released **first**: a change a server needs is released here, then the
server's pin moves to it. See [docs/releasing.md](docs/releasing.md).

## License

GNU Affero General Public License, version 3 or later ([LICENSE](LICENSE)).
