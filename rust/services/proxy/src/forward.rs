use std::collections::HashMap;
use std::sync::{Arc, RwLock};
use std::time::SystemTime;

use anyhow::{Context, Result};
use aws_credential_types::provider::ProvideCredentials;
use aws_sigv4::http_request::{sign, SignableBody, SignableRequest, SigningSettings};
use axum::body::Body;
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, Uri};
use bytes::Bytes;
use reqwest::Client;

/// Headers that should not be forwarded to S3 (they'll be replaced by signing or are hop-by-hop).
const SKIP_HEADERS: &[&str] = &[
    "authorization",
    "x-amz-date",
    "x-amz-security-token",
    "x-amz-content-sha256",
    "host",
    "connection",
    "transfer-encoding",
    "roar-session-id",
    "roar-job-id",
];

/// Shared state for forwarding requests.
#[derive(Clone)]
pub struct ForwardState {
    pub client: Client,
    pub credentials_provider: aws_credential_types::provider::SharedCredentialsProvider,
    pub region: String,
    /// Cache of bucket name → region, learned from S3 PermanentRedirect responses.
    pub bucket_regions: Arc<RwLock<HashMap<String, String>>>,
}

/// S3 response with deferred body consumption.
pub struct S3Response {
    pub status: StatusCode,
    pub headers: HeaderMap,
    inner: reqwest::Response,
}

impl S3Response {
    /// Consume the response body as a byte stream (memory-efficient, default).
    pub fn into_body_stream(self) -> Body {
        Body::from_stream(self.inner.bytes_stream())
    }

    /// Buffer the full response body in memory.
    pub async fn collect_body(self) -> Result<Bytes> {
        self.inner
            .bytes()
            .await
            .context("failed to collect upstream response body")
    }
}

/// Forward a request to real S3 (or an upstream endpoint), re-signing it with SigV4.
///
/// When no upstream is configured and S3 returns a `PermanentRedirect` (301),
/// the correct region is extracted from the `x-amz-bucket-region` response header,
/// cached for future requests, and the request is retried against the right endpoint.
pub async fn forward_to_s3(
    state: &ForwardState,
    method: Method,
    uri: &Uri,
    headers: &HeaderMap,
    body: Bytes,
    upstream_url: Option<&str>,
) -> Result<S3Response> {
    let path = uri.path();
    let query = uri.query().unwrap_or("");

    // Look up cached region for this bucket (if any)
    let bucket = extract_bucket(path);
    let cached_region = bucket.and_then(|b| {
        state
            .bucket_regions
            .read()
            .expect("lock poisoned")
            .get(b)
            .cloned()
    });
    let effective_region = cached_region.as_deref().unwrap_or(&state.region);

    let response = forward_to_s3_inner(
        state,
        &method,
        path,
        query,
        headers,
        body.clone(),
        upstream_url,
        effective_region,
    )
    .await?;

    // Handle S3 PermanentRedirect (301) or TemporaryRedirect (307):
    // extract the correct region and retry with a properly-signed request.
    if (response.status == StatusCode::MOVED_PERMANENTLY
        || response.status == StatusCode::TEMPORARY_REDIRECT)
        && upstream_url.is_none()
    {
        if let Some(correct_region) = response
            .headers
            .get("x-amz-bucket-region")
            .and_then(|v| v.to_str().ok())
        {
            let correct_region = correct_region.to_string();
            eprintln!(
                "roar-proxy: bucket {:?} is in {}, retrying (was {})",
                bucket.unwrap_or("?"),
                correct_region,
                effective_region,
            );
            // Cache for future requests
            if let Some(b) = bucket {
                state
                    .bucket_regions
                    .write()
                    .expect("lock poisoned")
                    .insert(b.to_string(), correct_region.clone());
            }
            return forward_to_s3_inner(
                state,
                &method,
                path,
                query,
                headers,
                body,
                upstream_url,
                &correct_region,
            )
            .await;
        }
    }

    Ok(response)
}

/// Extract bucket name from path-style S3 URL path (`/{bucket}/...`).
fn extract_bucket(path: &str) -> Option<&str> {
    let trimmed = path.strip_prefix('/')?;
    if trimmed.is_empty() {
        return None;
    }
    match trimmed.find('/') {
        Some(idx) => Some(&trimmed[..idx]),
        None => Some(trimmed),
    }
}

/// Inner forwarding logic: builds the target URL for the given region, signs, and sends.
async fn forward_to_s3_inner(
    state: &ForwardState,
    method: &Method,
    path: &str,
    query: &str,
    headers: &HeaderMap,
    body: Bytes,
    upstream_url: Option<&str>,
    effective_region: &str,
) -> Result<S3Response> {
    // Build the target URL — use upstream if provided, otherwise default S3 endpoint
    let target_url = match upstream_url {
        Some(base) => {
            let base = base.trim_end_matches('/');
            if query.is_empty() {
                format!("{}{}", base, path)
            } else {
                format!("{}{}?{}", base, path, query)
            }
        }
        None => {
            if query.is_empty() {
                format!("https://s3.{}.amazonaws.com{}", effective_region, path)
            } else {
                format!(
                    "https://s3.{}.amazonaws.com{}?{}",
                    effective_region, path, query
                )
            }
        }
    };

    // Resolve credentials
    let creds = state
        .credentials_provider
        .provide_credentials()
        .await
        .context("failed to resolve AWS credentials")?;
    let identity = creds.into();

    // Build signing settings — use UNSIGNED-PAYLOAD to avoid buffering/hashing body
    let mut signing_settings = SigningSettings::default();
    signing_settings.payload_checksum_kind =
        aws_sigv4::http_request::PayloadChecksumKind::XAmzSha256;

    let signing_params = aws_sigv4::sign::v4::SigningParams::builder()
        .identity(&identity)
        .region(effective_region)
        .name("s3")
        .time(SystemTime::now())
        .settings(signing_settings)
        .build()
        .context("failed to build signing params")?
        .into();

    // Collect headers to sign (forward Content-Type, Content-Length, etc.)
    let mut signable_headers: Vec<(String, String)> = Vec::new();
    let s3_host = match upstream_url {
        Some(base) => {
            // Extract host (and optional port) from the upstream URL
            url::Url::parse(base)
                .ok()
                .and_then(|u| {
                    let host = u.host_str()?.to_string();
                    match u.port() {
                        Some(port) => Some(format!("{}:{}", host, port)),
                        None => Some(host),
                    }
                })
                .unwrap_or_else(|| format!("s3.{}.amazonaws.com", effective_region))
        }
        None => format!("s3.{}.amazonaws.com", effective_region),
    };
    signable_headers.push(("host".to_string(), s3_host.clone()));

    for (name, value) in headers.iter() {
        let name_lower = name.as_str().to_lowercase();
        if SKIP_HEADERS.contains(&name_lower.as_str()) {
            continue;
        }
        if let Ok(v) = value.to_str() {
            signable_headers.push((name_lower, v.to_string()));
        }
    }

    let header_pairs: Vec<(&str, &str)> = signable_headers
        .iter()
        .map(|(k, v)| (k.as_str(), v.as_str()))
        .collect();

    let signable_request = SignableRequest::new(
        method.as_str(),
        &target_url,
        header_pairs.iter().copied(),
        SignableBody::UnsignedPayload,
    )
    .context("failed to build signable request")?;

    let (signing_instructions, _signature) = sign(signable_request, &signing_params)
        .context("failed to sign request")?
        .into_parts();

    // Build an http::Request to apply signing headers, then extract all headers
    let mut temp_request = http::Request::builder()
        .method(method.as_str())
        .uri(&target_url)
        .body(())
        .context("failed to build temp request")?;

    for (name, value) in &signable_headers {
        temp_request.headers_mut().insert(
            http::HeaderName::try_from(name.as_str()).context("invalid header name")?,
            HeaderValue::from_str(value).context("invalid header value")?,
        );
    }

    signing_instructions.apply_to_request_http1x(&mut temp_request);

    // Build the final reqwest request with all signed headers
    let mut final_builder = state.client.request(
        reqwest::Method::from_bytes(method.as_str().as_bytes()).context("invalid HTTP method")?,
        &target_url,
    );

    for (name, value) in temp_request.headers().iter() {
        if let Ok(v) = value.to_str() {
            final_builder = final_builder.header(name.as_str(), v);
        }
    }

    final_builder = final_builder.body(body);

    let response = final_builder
        .send()
        .await
        .context("failed to forward request to S3")?;

    // Map the S3 response status + headers
    let status = StatusCode::from_u16(response.status().as_u16())
        .unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);

    let mut response_headers = HeaderMap::new();
    for (name, value) in response.headers().iter() {
        if let Ok(hv) = HeaderValue::from_bytes(value.as_bytes()) {
            if let Ok(hn) = axum::http::HeaderName::try_from(name.as_str()) {
                response_headers.insert(hn, hv);
            }
        }
    }

    Ok(S3Response {
        status,
        headers: response_headers,
        inner: response,
    })
}
