mod forward;
mod s3;

use std::collections::HashMap;
use std::sync::{Arc, RwLock};

use anyhow::{Context as _, Result};
use aws_credential_types::provider::SharedCredentialsProvider;
use axum::body::Body;
use axum::extract::State;
use axum::http::{Request, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::Router;
use bytes::Bytes;
use clap::Parser;

use forward::ForwardState;
use s3::{LogMeta, S3OpType, S3Operation};

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

    // Disable automatic redirect following — S3 redirects (301/307) require
    // re-signing for the correct region, which we handle in forward_to_s3.
    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .context("failed to build HTTP client")?;

    let state = Arc::new(AppState {
        forward: ForwardState {
            client: reqwest::Client::builder()
                .pool_max_idle_per_host(32)
                .tcp_keepalive(std::time::Duration::from_secs(90))
                .tcp_nodelay(true)
                .build()
                .context("failed to build reqwest client")?,
            credentials_provider: SharedCredentialsProvider::new(credentials_provider),
            region,
            bucket_regions: Arc::new(RwLock::new(HashMap::new())),
        },
        default_session_id: args.session_id,
        default_job_id: args.job_id,
        upstream: args.upstream,
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
    #[cfg(debug_assertions)]
    let t_start = std::time::Instant::now();
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

    let (response_body_bytes, response_body) = if matches!(
        op.as_ref().map(|o| &o.operation),
        Some(S3OpType::CompleteMultipartUpload)
    ) {
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
        response = response.header(name, value);
    }

    let response = response
        .body(response_body)
        .context("failed to build response")?;

    #[cfg(debug_assertions)]
    eprintln!(
        "[proxy-timing] handle_total={:.2}ms",
        t_start.elapsed().as_secs_f64() * 1000.0
    );

    Ok(response)
}
