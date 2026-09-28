"""Where a tenant's files live on THIS container's disk — one rule per file kind,
shared by the code that writes them and the account purge that deletes them.

Two kinds exist:

  * the per-user FAISS index (``matcher.Matcher``) — ``jobs_<uid>.faiss`` plus
    its ``.ids.npy`` id map, next to ``settings.faiss_index_path``;
  * tailored documents (``tailoring.tailor``) — ``<data_dir>/tailored/app_<id>/``,
    mirrored to Supabase Storage because this disk is ephemeral.

The purge used to know neither, so a deleted account's tailored résumés sat on
disk until the next redeploy. A second copy of either rule would drift from the
writer the day someone renames a file; importing the matcher to learn a path
would load torch into the deletion route.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Tuple


def user_index_paths(user_id: Optional[str], base: Optional[Path] = None) -> Tuple[Path, Path]:
    """``(index_path, id_map_path)`` for ``user_id``. The shared default path
    (``base`` itself) for no user / the local dev identity."""
    if base is None:
        from app.config import settings
        base = settings.faiss_index_path
    if user_id and user_id not in ("local",):
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(user_id))
        index_path = base.with_name(f"{base.stem}_{safe}{base.suffix}")
    else:
        index_path = base
    return index_path, index_path.with_suffix(".ids.npy")


def tailored_dir(application_id: int, data_dir: Optional[Path] = None) -> Path:
    """The local directory holding one application's tailored documents."""
    if data_dir is None:
        from app.config import settings
        data_dir = settings.data_dir
    return Path(data_dir) / "tailored" / f"app_{int(application_id)}"
