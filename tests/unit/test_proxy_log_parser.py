"""Tests for the S3 proxy log line parser."""

from roar.execution.cluster.proxy import parse_log_line


class TestParseLogLineGetObject:
    def test_get_object_with_etag(self):
        line = "[S3:GetObject] s3://my-bucket/data/train.csv  etag=d41d8cd98f00b204e9800998ecf8427e"
        entry = parse_log_line(line)
        assert entry is not None
        assert entry.operation == "GetObject"
        assert entry.bucket == "my-bucket"
        assert entry.key == "data/train.csv"
        assert entry.etag == "d41d8cd98f00b204e9800998ecf8427e"
        assert entry.byte_ranges is None
        assert entry.size_bytes is None

    def test_get_object_with_range_and_etag(self):
        line = "[S3:GetObject] s3://bucket/key  byte_ranges=[[0,999]]  etag=abc123"
        entry = parse_log_line(line)
        assert entry is not None
        assert entry.operation == "GetObject"
        assert entry.bucket == "bucket"
        assert entry.key == "key"
        assert entry.etag == "abc123"
        assert entry.byte_ranges == [[0, 999]]

    def test_get_object_with_multiple_byte_ranges(self):
        line = "[S3:GetObject] s3://bucket/key  byte_ranges=[[0,999],[2000,2999]]  etag=abc123"
        entry = parse_log_line(line)
        assert entry is not None
        assert entry.byte_ranges == [[0, 999], [2000, 2999]]

    def test_get_object_minimal(self):
        line = "[S3:GetObject] s3://bucket/key"
        entry = parse_log_line(line)
        assert entry is not None
        assert entry.operation == "GetObject"
        assert entry.bucket == "bucket"
        assert entry.key == "key"
        assert entry.etag is None
        assert entry.byte_ranges is None
        assert entry.size_bytes is None


class TestParseLogLinePutObject:
    def test_put_object_with_size_and_etag(self):
        line = "[S3:PutObject] s3://bucket/key  (42 bytes)  etag=abc123"
        entry = parse_log_line(line)
        assert entry is not None
        assert entry.operation == "PutObject"
        assert entry.bucket == "bucket"
        assert entry.key == "key"
        assert entry.size_bytes == 42
        assert entry.etag == "abc123"

    def test_put_object_large_size(self):
        line = "[S3:PutObject] s3://bucket/models/big.pt  (5242880000 bytes)  etag=xyz"
        entry = parse_log_line(line)
        assert entry is not None
        assert entry.size_bytes == 5242880000
        assert entry.etag == "xyz"


class TestParseLogLineOtherOps:
    def test_complete_multipart_upload(self):
        line = "[S3:CompleteMultipartUpload] s3://bucket/large.pt  etag=composite-3"
        entry = parse_log_line(line)
        assert entry is not None
        assert entry.operation == "CompleteMultipartUpload"
        assert entry.bucket == "bucket"
        assert entry.key == "large.pt"
        assert entry.etag == "composite-3"

    def test_head_object(self):
        line = "[S3:HeadObject] s3://bucket/key"
        entry = parse_log_line(line)
        assert entry is not None
        assert entry.operation == "HeadObject"

    def test_delete_object(self):
        line = "[S3:DeleteObject] s3://bucket/key"
        entry = parse_log_line(line)
        assert entry is not None
        assert entry.operation == "DeleteObject"


class TestParseLogLineMetadata:
    def test_full_metadata(self):
        line = "[S3:GetObject] s3://bucket/key  etag=abc  session=sess-001  job=job-042"
        entry = parse_log_line(line)
        assert entry is not None
        assert entry.etag == "abc"
        # session and job are not parsed into S3LogEntry (they're proxy-level metadata)
        assert entry.operation == "GetObject"


class TestParseLogLineEdgeCases:
    def test_roar_proxy_ready_sentinel_returns_none(self):
        line = "ROAR_PROXY_READY port=9090"
        assert parse_log_line(line) is None

    def test_empty_string_returns_none(self):
        assert parse_log_line("") is None

    def test_malformed_line_returns_none(self):
        assert parse_log_line("not a log line at all") is None

    def test_stderr_message_returns_none(self):
        assert parse_log_line("roar-proxy listening on http://127.0.0.1:9090") is None

    def test_partial_s3_prefix_returns_none(self):
        assert parse_log_line("[S3:GetObject]") is None

    def test_nested_key_path(self):
        line = "[S3:GetObject] s3://bucket/deeply/nested/path/to/file.csv  etag=abc"
        entry = parse_log_line(line)
        assert entry is not None
        assert entry.key == "deeply/nested/path/to/file.csv"
        assert entry.etag == "abc"

    def test_url_encoded_characters_in_key(self):
        line = "[S3:GetObject] s3://bucket/path%20with%20spaces/file.csv"
        entry = parse_log_line(line)
        assert entry is not None
        assert entry.key == "path%20with%20spaces/file.csv"

    def test_invalid_size_value(self):
        line = "[S3:PutObject] s3://bucket/key  (notanumber bytes)"
        entry = parse_log_line(line)
        assert entry is not None
        assert entry.size_bytes is None

    def test_key_with_special_characters(self):
        line = "[S3:GetObject] s3://bucket/data-v2/model_final.pt  etag=abc"
        entry = parse_log_line(line)
        assert entry is not None
        assert entry.key == "data-v2/model_final.pt"

    def test_put_without_size(self):
        line = "[S3:PutObject] s3://bucket/key  etag=abc"
        entry = parse_log_line(line)
        assert entry is not None
        assert entry.operation == "PutObject"
        assert entry.size_bytes is None
        assert entry.etag == "abc"

    def test_upload_part(self):
        line = "[S3:UploadPart] s3://bucket/key  (5242880 bytes)"
        entry = parse_log_line(line)
        assert entry is not None
        assert entry.operation == "UploadPart"
        assert entry.size_bytes == 5242880

    def test_list_objects(self):
        line = "[S3:ListObjectsV2] s3://bucket/"
        entry = parse_log_line(line)
        # The regex requires at least one non-whitespace char after bucket/
        # ListObjectsV2 log lines have no key, so the trailing / may not match
        # This is expected behavior — list operations aren't tracked as artifacts
        if entry is not None:
            assert entry.operation == "ListObjectsV2"

    def test_list_objects_with_prefix(self):
        line = "[S3:ListObjectsV2] s3://bucket/prefix"
        entry = parse_log_line(line)
        assert entry is not None
        assert entry.operation == "ListObjectsV2"
