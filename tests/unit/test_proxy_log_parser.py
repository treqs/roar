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

class TestParseLogLineMetadata:
    def test_full_metadata(self):
        line = "[S3:GetObject] s3://bucket/key  etag=abc  session=sess-001  job=job-042"
        entry = parse_log_line(line)
        assert entry is not None
        assert entry.etag == "abc"
        # session and job are not parsed into S3LogEntry (they're proxy-level metadata)
        assert entry.operation == "GetObject"


class TestParseLogLineEdgeCases:
    def test_malformed_line_returns_none(self):
        assert parse_log_line("not a log line at all") is None

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

    def test_put_without_size(self):
        line = "[S3:PutObject] s3://bucket/key  etag=abc"
        entry = parse_log_line(line)
        assert entry is not None
        assert entry.operation == "PutObject"
        assert entry.size_bytes is None
        assert entry.etag == "abc"

    def test_list_objects_with_prefix(self):
        line = "[S3:ListObjectsV2] s3://bucket/prefix"
        entry = parse_log_line(line)
        assert entry is not None
        assert entry.operation == "ListObjectsV2"
