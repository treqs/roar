"""Pin roar's sha256-tree to the qualifying keys of the three anchor HF datasets.

These compute the key from HF tree metadata (LFS sha256 oids, no data download) and
assert byte-equality with values independently produced by the compspec oracle in
~/comp-art-e2e. Network + HF-token gated; skipped otherwise.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from roar.application.composite import Leaf, sha256_tree

# Path-sensitive sha256-tree over LFS (data) leaves, metadata-only.
ANCHORS = {
    "climbmix": (
        "karpathy/climbmix-400b-shuffle",
        "915333b4f8b8684f39aeaafea600fea6f43fb703",
        "92fe50f14222b1004e2fbb73351b6a8211710bb10713b86aac1eaa58259dd0a6",
    ),
    "esm2": (
        "nvidia/esm2_uniref_pretraining_data",
        "4ac1d2973567e46b8ca95901f4b4793a21305995",
        "d70b5ec319e39091281ff5970bfd7647cf38eb9cfa6de0cfdd074207264ce4b4",
    ),
    "libero": (
        "physical-intelligence/libero",
        "a4336d589d589045d1c56423ffdf3b88a0e19b1f",
        "9f01856f31ff9ee83e5c43aa494fb63d7517fc9d028422c9701d8aeadc1f86bb",
    ),
}


def _token() -> str | None:
    p = Path("~/.hf_token").expanduser()
    return p.read_text().strip() if p.exists() else os.environ.get("HF_TOKEN")


def _online() -> bool:
    try:
        urllib.request.urlopen("https://huggingface.co/api/whoami-v2", timeout=5)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(not _online(), reason="no network to huggingface.co"),
    pytest.mark.skipif(_token() is None, reason="no HF token"),
]


def _lfs_leaves(repo: str, commit: str, token: str | None) -> list[Leaf]:
    url: str | None = f"https://huggingface.co/api/datasets/{repo}/tree/{commit}?recursive=1"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    leaves: list[Leaf] = []
    while url:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            body, hdrs = resp.read(), dict(resp.headers)
        for e in json.loads(body):
            if e.get("type") == "file" and e.get("lfs"):
                leaves.append(Leaf(e["path"], e["lfs"]["oid"], kind="data"))
        link = hdrs.get("Link") or hdrs.get("link") or ""
        url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                url = part[part.find("<") + 1 : part.find(">")]
                break
    return leaves


@pytest.mark.parametrize("name", list(ANCHORS))
def test_roar_sha256_tree_matches_anchor(name: str) -> None:
    repo, commit, expected = ANCHORS[name]
    leaves = _lfs_leaves(repo, commit, _token())
    assert sha256_tree(leaves) == expected, f"{name} qualifying key drifted from oracle"
