use std::fmt;

use axum::http::{Method, Uri};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum S3OpType {
    GetObject,
    PutObject,
    HeadObject,
    DeleteObject,
    ListObjectsV2,
    CreateMultipartUpload,
    UploadPart,
    CompleteMultipartUpload,
    Other,
}

impl fmt::Display for S3OpType {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            S3OpType::GetObject => write!(f, "GetObject"),
            S3OpType::PutObject => write!(f, "PutObject"),
            S3OpType::HeadObject => write!(f, "HeadObject"),
            S3OpType::DeleteObject => write!(f, "DeleteObject"),
            S3OpType::ListObjectsV2 => write!(f, "ListObjectsV2"),
            S3OpType::CreateMultipartUpload => write!(f, "CreateMultipartUpload"),
            S3OpType::UploadPart => write!(f, "UploadPart"),
            S3OpType::CompleteMultipartUpload => write!(f, "CompleteMultipartUpload"),
            S3OpType::Other => write!(f, "Other"),
        }
    }
}

#[derive(Debug, Clone)]
pub struct S3Operation {
    pub operation: S3OpType,
    pub bucket: String,
    pub key: Option<String>,
    pub content_length: Option<u64>,
}

impl S3Operation {
    /// Parse an incoming HTTP request into an S3 operation.
    ///
    /// Path format: `/{bucket}/{key...}` or `/{bucket}` or `/`
    pub fn parse(method: &Method, uri: &Uri, content_length: Option<u64>) -> Option<S3Operation> {
        let path = uri.path();

        // Strip leading slash and split into bucket + key
        let trimmed = path.strip_prefix('/')?;
        if trimmed.is_empty() {
            // Root path `/` — likely ListBuckets, not tracked
            return None;
        }

        let (bucket, key) = match trimmed.find('/') {
            Some(idx) => {
                let key_part = &trimmed[idx + 1..];
                let key = if key_part.is_empty() {
                    None
                } else {
                    Some(key_part.to_string())
                };
                (trimmed[..idx].to_string(), key)
            }
            None => (trimmed.to_string(), None),
        };

        let query = uri.query().unwrap_or("");

        let operation = classify(method, key.is_some(), query);

        Some(S3Operation {
            operation,
            bucket,
            key,
            content_length,
        })
    }

    /// Format as a log line for stdout.
    pub fn log_line(&self, meta: &LogMeta) -> String {
        let uri = match &self.key {
            Some(key) => format!("s3://{}/{}", self.bucket, key),
            None => format!("s3://{}/", self.bucket),
        };

        let mut line = format!("[S3:{}] {}", self.operation, uri);

        match self.operation {
            S3OpType::PutObject | S3OpType::UploadPart => {
                if let Some(len) = self.content_length {
                    line.push_str(&format!("  ({} bytes)", len));
                }
            }
            _ => {}
        }

        if let Some(ref ranges) = meta.byte_ranges {
            let formatted: Vec<String> = ranges
                .iter()
                .map(|(s, e)| format!("[{},{}]", s, e))
                .collect();
            line.push_str(&format!("  byte_ranges=[{}]", formatted.join(",")));
        }

        if let Some(ref etag) = meta.etag {
            line.push_str(&format!("  etag={}", etag));
        }

        if let Some(ref sid) = meta.session_id {
            line.push_str(&format!("  session={}", sid));
        }

        if let Some(ref jid) = meta.job_id {
            line.push_str(&format!("  job={}", jid));
        }

        line
    }
}

/// Metadata attached to log lines, from both request and response headers.
#[derive(Debug, Clone, Default)]
pub struct LogMeta {
    /// S3 ETag (MD5 for single-part uploads, composite for multipart). Quotes stripped.
    pub etag: Option<String>,
    /// Parsed byte ranges from Content-Range header. Each tuple is (start, end) inclusive.
    pub byte_ranges: Option<Vec<(u64, u64)>>,
    /// Roar session ID from the Roar-Session-Id request header.
    pub session_id: Option<String>,
    /// Roar job ID from the Roar-Job-Id request header.
    pub job_id: Option<String>,
}

impl LogMeta {
    /// Fill in `None` fields from default values (e.g. CLI args).
    pub fn with_defaults(mut self, session_id: &Option<String>, job_id: &Option<String>) -> Self {
        if self.session_id.is_none() {
            self.session_id = session_id.clone();
        }
        if self.job_id.is_none() {
            self.job_id = job_id.clone();
        }
        self
    }

    /// Extract roar headers from the incoming request, S3 metadata from the response.
    pub fn build(
        request_headers: &axum::http::HeaderMap,
        response_headers: &axum::http::HeaderMap,
    ) -> Self {
        let etag = response_headers
            .get("etag")
            .and_then(|v| v.to_str().ok())
            .map(|v| v.trim_matches('"').to_string());

        let byte_ranges = response_headers
            .get("content-range")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| {
                let v = v.strip_prefix("bytes ").unwrap_or(v);
                let range_part = v.split('/').next()?;
                let mut parts = range_part.split('-');
                let start = parts.next()?.parse::<u64>().ok()?;
                let end = parts.next()?.parse::<u64>().ok()?;
                Some(vec![(start, end)])
            });

        let session_id = request_headers
            .get("roar-session-id")
            .and_then(|v| v.to_str().ok())
            .map(|v| v.to_string());

        let job_id = request_headers
            .get("roar-job-id")
            .and_then(|v| v.to_str().ok())
            .map(|v| v.to_string());

        LogMeta {
            etag,
            byte_ranges,
            session_id,
            job_id,
        }
    }
}

fn has_query_key(query: &str, key: &str) -> bool {
    query.split('&').any(|part| {
        let param_key = part.split('=').next().unwrap_or("");
        param_key == key
    })
}

fn classify(method: &Method, has_key: bool, query: &str) -> S3OpType {
    match (method.clone(), has_key) {
        (Method::GET, true) => S3OpType::GetObject,
        (Method::PUT, true) => {
            if has_query_key(query, "partNumber") {
                S3OpType::UploadPart
            } else {
                S3OpType::PutObject
            }
        }
        (Method::HEAD, true) => S3OpType::HeadObject,
        (Method::DELETE, true) => S3OpType::DeleteObject,
        (Method::GET, false) => {
            if has_query_key(query, "list-type") {
                S3OpType::ListObjectsV2
            } else {
                S3OpType::Other
            }
        }
        (Method::POST, true) => {
            if has_query_key(query, "uploads") {
                S3OpType::CreateMultipartUpload
            } else if has_query_key(query, "uploadId") {
                S3OpType::CompleteMultipartUpload
            } else {
                S3OpType::Other
            }
        }
        _ => S3OpType::Other,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn uri(s: &str) -> Uri {
        s.parse().expect("valid URI")
    }

    #[test]
    fn get_object() {
        let op = S3Operation::parse(&Method::GET, &uri("/my-bucket/data/train.csv"), None);
        let op = op.expect("should parse");
        assert_eq!(op.operation, S3OpType::GetObject);
        assert_eq!(op.bucket, "my-bucket");
        assert_eq!(op.key.as_deref(), Some("data/train.csv"));
    }

    #[test]
    fn put_object_with_length() {
        let op = S3Operation::parse(
            &Method::PUT,
            &uri("/my-bucket/models/model.pt"),
            Some(1234567),
        );
        let op = op.expect("should parse");
        assert_eq!(op.operation, S3OpType::PutObject);
        assert_eq!(op.content_length, Some(1234567));
        assert!(op.log_line(&LogMeta::default()).contains("(1234567 bytes)"));
    }

    #[test]
    fn head_object() {
        let op = S3Operation::parse(&Method::HEAD, &uri("/bucket/key"), None);
        let op = op.expect("should parse");
        assert_eq!(op.operation, S3OpType::HeadObject);
    }

    #[test]
    fn delete_object() {
        let op = S3Operation::parse(&Method::DELETE, &uri("/bucket/key"), None);
        let op = op.expect("should parse");
        assert_eq!(op.operation, S3OpType::DeleteObject);
    }

    #[test]
    fn list_objects_v2() {
        let op = S3Operation::parse(
            &Method::GET,
            &uri("/my-bucket?list-type=2&prefix=data/"),
            None,
        );
        let op = op.expect("should parse");
        assert_eq!(op.operation, S3OpType::ListObjectsV2);
        assert_eq!(op.bucket, "my-bucket");
        assert!(op.key.is_none());
    }

    #[test]
    fn create_multipart_upload() {
        let op = S3Operation::parse(&Method::POST, &uri("/bucket/large.pt?uploads"), None);
        let op = op.expect("should parse");
        assert_eq!(op.operation, S3OpType::CreateMultipartUpload);
    }

    #[test]
    fn upload_part() {
        let op = S3Operation::parse(
            &Method::PUT,
            &uri("/bucket/large.pt?partNumber=3&uploadId=abc123"),
            Some(5242880),
        );
        let op = op.expect("should parse");
        assert_eq!(op.operation, S3OpType::UploadPart);
    }

    #[test]
    fn complete_multipart_upload() {
        let op = S3Operation::parse(
            &Method::POST,
            &uri("/bucket/large.pt?uploadId=abc123"),
            None,
        );
        let op = op.expect("should parse");
        assert_eq!(op.operation, S3OpType::CompleteMultipartUpload);
    }

    #[test]
    fn root_path_returns_none() {
        let op = S3Operation::parse(&Method::GET, &uri("/"), None);
        assert!(op.is_none());
    }

    #[test]
    fn bucket_only_no_query() {
        let op = S3Operation::parse(&Method::GET, &uri("/my-bucket"), None);
        let op = op.expect("should parse");
        assert_eq!(op.operation, S3OpType::Other);
        assert_eq!(op.bucket, "my-bucket");
        assert!(op.key.is_none());
    }

    #[test]
    fn log_line_format() {
        let op = S3Operation::parse(&Method::GET, &uri("/my-bucket/data/train.csv"), None);
        let op = op.expect("should parse");
        assert_eq!(
            op.log_line(&LogMeta::default()),
            "[S3:GetObject] s3://my-bucket/data/train.csv"
        );
    }

    #[test]
    fn log_line_with_etag() {
        let op = S3Operation::parse(&Method::GET, &uri("/my-bucket/data/train.csv"), None);
        let op = op.expect("should parse");
        let meta = LogMeta {
            etag: Some("d41d8cd98f00b204e9800998ecf8427e".to_string()),
            ..Default::default()
        };
        assert_eq!(
            op.log_line(&meta),
            "[S3:GetObject] s3://my-bucket/data/train.csv  etag=d41d8cd98f00b204e9800998ecf8427e"
        );
    }

    #[test]
    fn log_line_with_range_and_etag() {
        let op = S3Operation::parse(&Method::GET, &uri("/bucket/key"), None);
        let op = op.expect("should parse");
        let meta = LogMeta {
            etag: Some("abc123".to_string()),
            byte_ranges: Some(vec![(0, 999)]),
            ..Default::default()
        };
        assert_eq!(
            op.log_line(&meta),
            "[S3:GetObject] s3://bucket/key  byte_ranges=[[0,999]]  etag=abc123"
        );
    }

    #[test]
    fn log_line_put_with_etag() {
        let op = S3Operation::parse(&Method::PUT, &uri("/bucket/key"), Some(42));
        let op = op.expect("should parse");
        let meta = LogMeta {
            etag: Some("abc123".to_string()),
            ..Default::default()
        };
        assert_eq!(
            op.log_line(&meta),
            "[S3:PutObject] s3://bucket/key  (42 bytes)  etag=abc123"
        );
    }

    #[test]
    fn log_line_with_roar_headers() {
        let op = S3Operation::parse(&Method::GET, &uri("/bucket/key"), None);
        let op = op.expect("should parse");
        let meta = LogMeta {
            etag: Some("abc123".to_string()),
            session_id: Some("sess-001".to_string()),
            job_id: Some("job-042".to_string()),
            ..Default::default()
        };
        assert_eq!(
            op.log_line(&meta),
            "[S3:GetObject] s3://bucket/key  etag=abc123  session=sess-001  job=job-042"
        );
    }
}
