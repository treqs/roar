mod forward;
mod s3;

use std::collections::HashMap;
use std::sync::{Arc, RwLock};
use std::time::Instant;

use anyhow::{Context as _, Result};
use aws_credential_types::provider::SharedCredentialsProvider;
use axum::body::Body;
use axum::extract::State;
use axum::http::{HeaderMap, Request, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::Router;
use bytes::Bytes;
use clap::Parser;

use forward::ForwardState;
use s3::{LogMeta, S3OpType, S3Operation};

// Buffer GET responses up to this size instead of streaming them.
// Buffering avoids the coordination overhead of the async streaming path,
// which shows measurable latency gains for objects up to ~16 MB.
// Override with ROAR_PROXY_BUFFER_RESPONSE_BYTES.
const DEFAULT_RESPONSE_BUFFER_BYTES: usize = 16 * 1024 * 1024;

#[derive(Parser)]
#[command(
    name = "roar-proxy",
    about = "S3 reverse proxy for roar lineage tracking"
)]
struct Args {
    /// Port to listen on
    #[arg(short, long, default_value = "9090")]
    port: u16,

    /// AWS region (falls back to AWS_REGION / AWS_DEFAULT_REGION)
    #[arg(short, long, env = "AWS_REGION")]
    region: Option<String>,

    /// Default session ID for log lines (used when request headers don't carry one)
    #[arg(long)]
    session_id: Option<String>,

    /// Default job ID for log lines (used when request headers don't carry one)
    #[arg(long)]
    job_id: Option<String>,

    /// Upstream S3-compatible endpoint URL (e.g. http://localhost:4566).
    /// When set, the proxy forwards to this URL instead of https://s3.{region}.amazonaws.com.
    /// Used for chaining through LocalStack, MinIO, or another proxy.
    #[arg(long)]
    upstream: Option<String>,
}

struct AppState {
    forward: ForwardState,
    default_session_id: Option<String>,
    default_job_id: Option<String>,
    upstream: Option<String>,
    response_buffer_bytes: usize,
    timing_enabled: bool,
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();

    // Load AWS config from environment
    let aws_config = aws_config::load_defaults(aws_config::BehaviorVersion::latest()).await;

    let region = args
        .region
        .or_else(|| aws_config.region().map(|r| r.to_string()))
        .unwrap_or_else(|| "us-east-1".to_string());

    let credentials_provider = aws_config
        .credentials_provider()
        .expect("no AWS credentials provider found")
        .clone();

    let timing_enabled = std::env::var_os("ROAR_PROXY_TIMING").is_some();
    let response_buffer_bytes = std::env::var("ROAR_PROXY_BUFFER_RESPONSE_BYTES")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(DEFAULT_RESPONSE_BUFFER_BYTES);

    let state = Arc::new(AppState {
        forward: ForwardState {
            client: reqwest::Client::builder()
                .redirect(reqwest::redirect::Policy::none())
                .pool_max_idle_per_host(32)
                .tcp_keepalive(std::time::Duration::from_secs(90))
                .tcp_nodelay(true)
                .build()
                .context("failed to build reqwest client")?,
            credentials_provider: SharedCredentialsProvider::new(credentials_provider),
            region,
            bucket_regions: Arc::new(RwLock::new(HashMap::new())),
            timing_enabled,
        },
        default_session_id: args.session_id,
        default_job_id: args.job_id,
        upstream: args.upstream,
        response_buffer_bytes,
        timing_enabled,
    });

    let app = Router::new()
        .route("/{*path}", any(handle_request))
        .route("/", any(handle_request))
        .with_state(state);

    let addr = format!("127.0.0.1:{}", args.port);
    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .with_context(|| format!("failed to bind to {}", addr))?;

    eprintln!("roar-proxy listening on http://{}", addr);
    println!("ROAR_PROXY_READY port={}", args.port);

    axum::serve(listener, app).await.context("server error")?;

    Ok(())
}

async fn handle_request(State(state): State<Arc<AppState>>, request: Request<Body>) -> Response {
    match handle_request_inner(&state, request).await {
        Ok(response) => response,
        Err(e) => {
            eprintln!("proxy error: {:#}", e);
            (StatusCode::BAD_GATEWAY, format!("proxy error: {:#}", e)).into_response()
        }
    }
}

async fn handle_request_inner(state: &AppState, request: Request<Body>) -> Result<Response> {
    let t_start = state.timing_enabled.then(Instant::now);
    let (parts, body) = request.into_parts();

    let content_length = parts
        .headers
        .get("content-length")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<u64>().ok());

    let op = S3Operation::parse(&parts.method, &parts.uri, content_length);

    // Buffer the request body
    let body_bytes = axum::body::to_bytes(body, usize::MAX)
        .await
        .unwrap_or_else(|_| Bytes::new());

    // Forward to real S3 (or upstream endpoint)
    let s3_response = forward::forward_to_s3(
        &state.forward,
        parts.method,
        &parts.uri,
        &parts.headers,
        body_bytes,
        state.upstream.as_deref(),
    )
    .await?;

    let status = s3_response.status;
    let headers = s3_response.headers.clone();

    let should_buffer = should_buffer_response(
        op.as_ref(),
        status,
        &headers,
        state.response_buffer_bytes,
    );

    let (response_body_bytes, response_body) = if should_buffer {
        let bytes = s3_response.collect_body().await?;
        (Some(bytes.clone()), Body::from(bytes))
    } else {
        (None, s3_response.into_body_stream())
    };

    // Log the operation with metadata from request + response headers
    if let Some(ref op) = op {
        let meta = LogMeta::build(&parts.headers, &headers, response_body_bytes.as_deref())
            .with_defaults(&state.default_session_id, &state.default_job_id);
        println!("{}", op.log_line(&meta));
    }

    let mut response = Response::builder().status(status);
    for (name, value) in headers.iter() {
        // Framing headers must not be copied verbatim: hyper frames the body
        // itself (content-length for buffered bytes, chunked for streams).
        // S3 sends CompleteMultipartUpload responses as Transfer-Encoding:
        // chunked whitespace keep-alive streams; claiming chunked framing on
        // the re-served buffered body makes the response unserializable and
        // the client sees a dropped connection ("empty reply").
        if name == axum::http::header::TRANSFER_ENCODING
            || name == axum::http::header::CONNECTION
            || (response_body_bytes.is_some() && name == axum::http::header::CONTENT_LENGTH)
        {
            continue;
        }
        response = response.header(name, value);
    }

    let response = response
        .body(response_body)
        .context("failed to build response")?;

    if let Some(t_start) = t_start {
        eprintln!(
            "[proxy-timing] handle_total={:.2}ms",
            t_start.elapsed().as_secs_f64() * 1000.0
        );
    }

    Ok(response)
}

fn should_buffer_response(
    op: Option<&S3Operation>,
    status: StatusCode,
    headers: &HeaderMap,
    response_buffer_bytes: usize,
) -> bool {
    if matches!(
        op.map(|operation| &operation.operation),
        Some(S3OpType::CompleteMultipartUpload)
    ) {
        return true;
    }

    if response_buffer_bytes == 0 || !status.is_success() {
        return false;
    }

    if !matches!(op.map(|operation| &operation.operation), Some(S3OpType::GetObject)) {
        return false;
    }

    headers
        .get("content-length")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<usize>().ok())
        .is_some_and(|content_length| content_length <= response_buffer_bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::{Method, Uri};

    fn uri(value: &str) -> Uri {
        value.parse().expect("valid URI")
    }

    fn content_length_headers(value: &str) -> HeaderMap {
        let mut headers = HeaderMap::new();
        headers.insert("content-length", value.parse().expect("valid header"));
        headers
    }

    #[test]
    fn buffers_complete_multipart_even_when_threshold_disabled() {
        let op = S3Operation::parse(
            &Method::POST,
            &uri("/bucket/key?uploadId=example"),
            None,
        );

        assert!(should_buffer_response(
            op.as_ref(),
            StatusCode::OK,
            &HeaderMap::new(),
            0,
        ));
    }

    #[test]
    fn buffers_small_get_objects() {
        let op = S3Operation::parse(&Method::GET, &uri("/bucket/key"), None);

        assert!(should_buffer_response(
            op.as_ref(),
            StatusCode::OK,
            &content_length_headers("65536"),
            1024 * 1024,
        ));
    }

    #[test]
    fn does_not_buffer_large_get_objects() {
        let op = S3Operation::parse(&Method::GET, &uri("/bucket/key"), None);

        assert!(!should_buffer_response(
            op.as_ref(),
            StatusCode::OK,
            &content_length_headers("2097152"),
            1024 * 1024,
        ));
    }

    #[test]
    fn does_not_buffer_without_known_length() {
        let op = S3Operation::parse(&Method::GET, &uri("/bucket/key"), None);

        assert!(!should_buffer_response(
            op.as_ref(),
            StatusCode::OK,
            &HeaderMap::new(),
            1024 * 1024,
        ));
    }
}
