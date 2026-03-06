from __future__ import annotations

import types
from unittest.mock import MagicMock

import pytest


class _FakeS3Client:
    def put_object(self, *args, **kwargs):
        del args, kwargs
        return {"ETag": '"fake-etag"'}


class _FakeDynamoClient:
    def put_item(self, *args, **kwargs):
        del args, kwargs
        return {"ConsumedCapacity": {"CapacityUnits": 1}}


def _build_fake_boto3_module() -> types.SimpleNamespace:
    def _real_client(service_name: str, *args, **kwargs):
        del args, kwargs
        if str(service_name).lower() == "s3":
            return _FakeS3Client()
        return _FakeDynamoClient()

    class Session:
        def client(self, service_name: str, *args, **kwargs):
            return _real_client(service_name, *args, **kwargs)

    return types.SimpleNamespace(client=_real_client, Session=Session)


@pytest.fixture
def _setup_worker_and_fake_boto3(monkeypatch: pytest.MonkeyPatch):
    import roar.ray.roar_worker as roar_worker

    fake_boto3 = _build_fake_boto3_module()
    monkeypatch.setitem(roar_worker.sys.modules, "boto3", fake_boto3)
    monkeypatch.setattr(roar_worker, "_current_fragment", object())
    monkeypatch.setattr(roar_worker, "_check_task_boundary", lambda: None)

    log_write = MagicMock()
    monkeypatch.setattr(roar_worker, "_log_write", log_write)

    return roar_worker, fake_boto3, log_write


def test_patch_boto3_wraps_module_level_client(_setup_worker_and_fake_boto3) -> None:
    roar_worker, boto3, log_write = _setup_worker_and_fake_boto3

    roar_worker._patch_boto3()

    s3 = boto3.client("s3")
    s3.put_object(Bucket="demo-bucket", Key="demo.txt", Body=b"payload")

    assert log_write.call_count == 1


def test_patch_boto3_does_NOT_wrap_non_s3_session_client(
    _setup_worker_and_fake_boto3,
) -> None:
    roar_worker, boto3, log_write = _setup_worker_and_fake_boto3

    roar_worker._patch_boto3()

    session = boto3.Session()
    ddb = session.client("dynamodb")
    ddb.put_item(TableName="demo-table", Item={"pk": {"S": "demo-key"}})

    log_write.assert_not_called()


def test_patch_boto3_wraps_session_client_after_fix(_setup_worker_and_fake_boto3) -> None:
    roar_worker, boto3, log_write = _setup_worker_and_fake_boto3

    roar_worker._patch_boto3()

    session = boto3.Session()
    s3 = session.client("s3")
    s3.put_object(Bucket="demo-bucket", Key="demo.txt", Body=b"payload")

    assert log_write.call_count == 1
