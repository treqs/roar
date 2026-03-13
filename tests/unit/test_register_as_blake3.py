from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

from blake3 import blake3

from roar.core.interfaces.lineage import LineageData
from roar.db.context import create_database_context
from roar.services.registration.register_service import RegisterService


def test_upgrade_s3_etag_to_blake3_adds_hash_without_removing_etag(tmp_path: Path) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()

    s3_url = "s3://demo-bucket/path/to/object.bin"
    etag_digest = "etag-digest-only"
    payload = b"known-bytes-for-blake3-upgrade"

    with create_database_context(roar_dir) as db_ctx:
        artifact_id, _created = db_ctx.artifacts.register(
            hashes={"etag": etag_digest},
            size=len(payload),
            path=s3_url,
            source_type="s3",
            source_url=s3_url,
        )
        artifact = db_ctx.artifacts.get(artifact_id)
        assert artifact is not None

    lineage = LineageData(
        jobs=[],
        artifacts=[artifact],
        artifact_hashes=set(),
        pipeline=None,
    )

    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": io.BytesIO(payload)}

    with patch("roar.services.registration.register_service.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_s3
        RegisterService().upgrade_s3_etags_to_blake3(roar_dir=roar_dir, lineage=lineage)

    mock_s3.get_object.assert_called_once_with(Bucket="demo-bucket", Key="path/to/object.bin")

    with create_database_context(roar_dir) as db_ctx:
        hashes = db_ctx.artifacts.get_hashes(artifact_id)

    digest_by_algorithm = {item["algorithm"]: item["digest"] for item in hashes}
    assert digest_by_algorithm["etag"] == etag_digest
    assert digest_by_algorithm["blake3"] == blake3(payload).hexdigest()
