"""second-brain-core: what the Second Brain MCP servers share.

Sign-in, the audit log, HTTP serving, the start-up guardrails, and (with the
``semantic`` extra) search by meaning. Each server describes itself once with a
:class:`~second_brain_core.server.Server` and hands that to every shared piece,
so nothing in here names a product.
"""

import os

__version__ = "0.1.1"


def pin_fastmcp_env() -> None:
    """Stop FastMCP from reading its settings from the working directory's ``.env``.

    FastMCP, the library underneath, has settings of its own (``FASTMCP_*``), and
    when it is first imported it reads them from a ``.env`` in the working
    directory. For an MCP server the working directory is whatever project the
    client has open, and those settings include the transport: a project's
    ``.env`` must not be able to turn a local stdio server into an HTTP one.
    Naming an env file that isn't a file stops the read. It leaves alone a
    ``FASTMCP_ENV_FILE`` the operator set on purpose.

    It must run before anything imports fastmcp. Importing this package runs it,
    so a server's ``__init__`` only has to import ``second_brain_core`` first;
    calling it again is harmless.
    """
    os.environ.setdefault("FASTMCP_ENV_FILE", os.devnull)


pin_fastmcp_env()
