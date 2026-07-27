"""Unit tests for the Hugging Face Hub storage backend (``roar put hf://...``).

Mocks ``HfApi`` so no real HF calls (or the huggingface_hub dependency) are needed.
"""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from roar.integrations.storage.hf import HFBackend, parse_hf_destination


class TestParseHfDestination:
    def test_model_default(self):
        # hf://owner/name -> parse_destination gives bucket=owner, prefix=name
        assert parse_hf_destination("owner", "name") == ("model", "owner/name", "")

    def test_model_with_subpath(self):
        assert parse_hf_destination("owner", "name/sub/dir") == ("model", "owner/name", "sub/dir")

    def test_dataset_prefix(self):
        # hf://datasets/owner/name -> bucket=datasets, prefix=owner/name
        assert parse_hf_destination("datasets", "owner/name") == ("dataset", "owner/name", "")

    def test_space_prefix_with_subpath(self):
        assert parse_hf_destination("spaces", "owner/name/x") == ("space", "owner/name", "x")

    def test_too_few_parts_raises(self):
        with pytest.raises(ValueError, match="hf:// destination must be"):
            parse_hf_destination("owner", "")


class TestHFBackendUpload:
    def _mock_api(self):
        api = Mock()
        api.create_repo.return_value = None
        api.upload_file.return_value = None
        return api

    def test_upload_model_returns_resolve_url(self, tmp_path: Path):
        f = tmp_path / "model.safetensors"
        f.write_bytes(b"weights")
        api = self._mock_api()
        with patch("roar.integrations.storage.hf.HfApi", return_value=api):
            backend = HFBackend(bucket="reproducible-ai", prefix="minimind-o")
            url = backend.upload(f, "model.safetensors")

        assert (
            url
            == "https://huggingface.co/reproducible-ai/minimind-o/resolve/main/model.safetensors"
        )
        api.create_repo.assert_called_once()
        assert api.create_repo.call_args.args[0] == "reproducible-ai/minimind-o"
        assert api.create_repo.call_args.kwargs["repo_type"] == "model"
        api.upload_file.assert_called_once_with(
            path_or_fileobj=str(f),
            path_in_repo="model.safetensors",
            repo_id="reproducible-ai/minimind-o",
            repo_type="model",
        )

    def test_upload_dataset_url_has_datasets_segment(self, tmp_path: Path):
        f = tmp_path / "data.csv"
        f.write_bytes(b"x")
        api = self._mock_api()
        with patch("roar.integrations.storage.hf.HfApi", return_value=api):
            backend = HFBackend(bucket="datasets", prefix="reproducible-ai/mnist")
            url = backend.upload(f, "data.csv")

        assert url == "https://huggingface.co/datasets/reproducible-ai/mnist/resolve/main/data.csv"
        assert api.create_repo.call_args.kwargs["repo_type"] == "dataset"

    def test_upload_with_path_prefix(self, tmp_path: Path):
        f = tmp_path / "epoch10.pt"
        f.write_bytes(b"ckpt")
        api = self._mock_api()
        with patch("roar.integrations.storage.hf.HfApi", return_value=api):
            backend = HFBackend(bucket="owner", prefix="repo/checkpoints")
            url = backend.upload(f, "epoch10.pt")

        assert url.endswith("/owner/repo/resolve/main/checkpoints/epoch10.pt")
        assert api.upload_file.call_args.kwargs["path_in_repo"] == "checkpoints/epoch10.pt"

    def test_exists_false_on_error(self, tmp_path: Path):
        api = self._mock_api()
        api.file_exists.side_effect = RuntimeError("boom")
        with patch("roar.integrations.storage.hf.HfApi", return_value=api):
            backend = HFBackend(bucket="owner", prefix="repo")
            assert backend.exists("nope.txt") is False
