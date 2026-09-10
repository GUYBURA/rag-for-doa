"""Supersession. The only place a document's status changes and the only
place `chunk` rows are deleted for archival - see CLAUDE.md invariant 2 and
invariant 3.

The one exception to the second half is db.py's delete_chunks_for_document(),
which run.py calls when re-ingesting a still-pending document: that document
was never active, so nothing there is being retired from search, and it is
not supersession.
"""

import uuid

import psycopg

from ingest.db import activate_document, run_promotion


def promote_new_document(conn: psycopg.Connection, document_id: uuid.UUID) -> None:
    """Mark a freshly-embedded document active, then archive anything it
    fully supersedes.

    Called by run.py the moment a document has passed the QA gate and been
    chunked and embedded - there is no separate review step in this design,
    so a document goes live the instant it is known good.

    Order matters. promote_current_edition()'s coverage check only considers
    'active' and 'archived' documents when deciding whether an older edition
    is now fully covered (invariant 2: union coverage across newer editions).
    activate_document() has to run first, or this document does not exist yet
    as far as that check is concerned - and since nothing else ever promotes
    it, it would never get a second chance to retire what it supersedes.
    """
    activate_document(conn, document_id)
    run_promotion(conn)
