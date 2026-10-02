"""Agent work started from the dashboard. Each job opens its own connection."""
from __future__ import annotations

import logging

from pulse.agents.base import BudgetExceeded, LLMError
from pulse.agents.digest import run_digest
from pulse.agents.investigate import run_investigation
from pulse.db import connect
from pulse.web.settings import WebSettings

log = logging.getLogger(__name__)


def run_digest_job(settings: WebSettings, launch: str | None) -> None:
    conn = connect(settings.db_path)
    try:
        llm = settings.llm_factory(conn, settings.config)
        run_digest(conn, llm, settings.clock(), launch=launch)
    except (LookupError, BudgetExceeded, LLMError) as e:
        log.warning("digest job failed: %s", e)
    finally:
        conn.close()


def run_investigation_job(settings: WebSettings, inv_id: int, question: str, context: dict) -> None:
    conn = connect(settings.db_path)
    try:
        llm = settings.llm_factory(conn, settings.config)
        run_investigation(conn, llm, question, settings.clock(), context=context, investigation_id=inv_id)
    except Exception as e:  # the row must never be left running
        log.warning("investigation %s failed: %s", inv_id, e)
        with conn:
            conn.execute(
                "UPDATE investigations SET markdown = ? WHERE id = ? AND markdown IS NULL",
                (f"Investigation failed: {type(e).__name__}: {e}", inv_id),
            )
    finally:
        conn.close()
