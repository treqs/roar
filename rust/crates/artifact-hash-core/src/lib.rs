use std::collections::HashSet;
use std::fmt::{Display, Formatter};
use std::fs::File;
use std::io::{BufReader, Read};
use std::path::{Path, PathBuf};

use md5::Md5;
use rayon::prelude::*;
use sha2::digest::Digest;
use sha2::{Sha256, Sha512};

const MIN_CHUNK_SIZE: usize = 64 * 1024;
const MAX_CHUNK_SIZE: usize = 8 * 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum HashAlgorithm {
    Blake3,
    Sha256,
    Sha512,
    Md5,
}

impl HashAlgorithm {
    pub fn parse(name: &str) -> Result<Self, HashError> {
        match name {
            "blake3" => Ok(Self::Blake3),
            "sha256" => Ok(Self::Sha256),
            "sha512" => Ok(Self::Sha512),
            "md5" => Ok(Self::Md5),
            unknown => Err(HashError::UnknownAlgorithm(unknown.to_string())),
        }
    }

    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Blake3 => "blake3",
            Self::Sha256 => "sha256",
            Self::Sha512 => "sha512",
            Self::Md5 => "md5",
        }
    }
}

#[derive(Debug)]
pub enum HashError {
    UnknownAlgorithm(String),
    Io(std::io::Error),
    InvalidWorkers(usize),
}

impl Display for HashError {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::UnknownAlgorithm(algo) => write!(f, "unknown hash algorithm: {algo}"),
            Self::Io(err) => write!(f, "hash IO error: {err}"),
            Self::InvalidWorkers(workers) => {
                write!(f, "workers must be >= 1 when provided, got {workers}")
            }
        }
    }
}

impl std::error::Error for HashError {}

impl From<std::io::Error> for HashError {
    fn from(value: std::io::Error) -> Self {
        Self::Io(value)
    }
}

#[derive(Debug)]
enum HasherState {
    Blake3(blake3::Hasher),
    Sha256(Sha256),
    Sha512(Sha512),
    Md5(Md5),
}

impl HasherState {
    fn new(algorithm: HashAlgorithm) -> Self {
        match algorithm {
            HashAlgorithm::Blake3 => Self::Blake3(blake3::Hasher::new()),
            HashAlgorithm::Sha256 => Self::Sha256(Sha256::new()),
            HashAlgorithm::Sha512 => Self::Sha512(Sha512::new()),
            HashAlgorithm::Md5 => Self::Md5(Md5::new()),
        }
    }

    fn update(&mut self, bytes: &[u8]) {
        match self {
            Self::Blake3(hasher) => {
                hasher.update(bytes);
            }
            Self::Sha256(hasher) => {
                hasher.update(bytes);
            }
            Self::Sha512(hasher) => {
                hasher.update(bytes);
            }
            Self::Md5(hasher) => {
                hasher.update(bytes);
            }
        }
    }

    fn finalize(self) -> String {
        match self {
            Self::Blake3(hasher) => hasher.finalize().to_hex().to_string(),
            Self::Sha256(hasher) => format!("{:x}", hasher.finalize()),
            Self::Sha512(hasher) => format!("{:x}", hasher.finalize()),
            Self::Md5(hasher) => format!("{:x}", hasher.finalize()),
        }
    }
}

pub fn parse_algorithms(names: &[String]) -> Result<Vec<HashAlgorithm>, HashError> {
    let mut seen = HashSet::new();
    let mut parsed = Vec::with_capacity(names.len());

    for name in names {
        let algo = HashAlgorithm::parse(name)?;
        if seen.insert(algo) {
            parsed.push(algo);
        }
    }

    Ok(parsed)
}

pub fn hash_file(
    path: &str,
    algorithm_names: &[String],
) -> Result<Vec<(String, String)>, HashError> {
    let algorithms = parse_algorithms(algorithm_names)?;
    hash_file_algorithms(path, &algorithms)
}

pub fn hash_file_algorithms(
    path: &str,
    algorithms: &[HashAlgorithm],
) -> Result<Vec<(String, String)>, HashError> {
    hash_file_algorithms_with_scratch(path, algorithms, None)
}

fn hash_file_algorithms_with_scratch(
    path: &str,
    algorithms: &[HashAlgorithm],
    scratch: Option<&mut Vec<u8>>,
) -> Result<Vec<(String, String)>, HashError> {
    let mut states: Vec<(HashAlgorithm, HasherState)> = algorithms
        .iter()
        .copied()
        .map(|algo| (algo, HasherState::new(algo)))
        .collect();

    let file = File::open(path)?;
    let chunk_size = chunk_size_for_file(&file);
    match scratch {
        Some(buffer) => {
            if buffer.len() != chunk_size {
                buffer.resize(chunk_size, 0_u8);
            }
            hash_reader(file, &mut states, buffer)?;
        }
        None => {
            let mut buffer = vec![0_u8; chunk_size];
            hash_reader(file, &mut states, &mut buffer)?;
        }
    }

    Ok(states
        .into_iter()
        .map(|(algo, state)| (algo.as_str().to_string(), state.finalize()))
        .collect())
}

fn chunk_size_for_file(file: &File) -> usize {
    file.metadata()
        .ok()
        .and_then(|meta| usize::try_from(meta.len()).ok())
        .map(|size| size.clamp(MIN_CHUNK_SIZE, MAX_CHUNK_SIZE))
        .unwrap_or(MIN_CHUNK_SIZE)
}

fn hash_reader(
    file: File,
    states: &mut [(HashAlgorithm, HasherState)],
    buffer: &mut [u8],
) -> Result<(), HashError> {
    let mut reader = BufReader::new(file);
    loop {
        let bytes_read = reader.read(buffer)?;
        if bytes_read == 0 {
            break;
        }

        for (_, state) in states.iter_mut() {
            state.update(&buffer[..bytes_read]);
        }
    }

    Ok(())
}

pub fn hash_files(
    paths: &[String],
    algorithm_names: &[String],
    workers: Option<usize>,
) -> Result<Vec<(String, Vec<(String, String)>)>, HashError> {
    let algorithms = parse_algorithms(algorithm_names)?;

    if let Some(worker_count) = workers {
        if worker_count == 0 {
            return Err(HashError::InvalidWorkers(worker_count));
        }

        let pool = rayon::ThreadPoolBuilder::new()
            .num_threads(worker_count)
            .build()
            .map_err(|_| HashError::InvalidWorkers(worker_count))?;

        return pool.install(|| {
            paths
                .par_iter()
                .map(|path| {
                    hash_file_algorithms_with_scratch(path, &algorithms, None)
                        .map(|hashes| (path.clone(), hashes))
                })
                .collect()
        });
    }

    let mut output = Vec::with_capacity(paths.len());
    let mut scratch = Vec::new();
    for path in paths {
        let hashes = hash_file_algorithms_with_scratch(path, &algorithms, Some(&mut scratch))?;
        output.push((path.clone(), hashes));
    }
    Ok(output)
}

pub fn hash_files_from_paths(
    paths: &[PathBuf],
    algorithm_names: &[String],
    workers: Option<usize>,
) -> Result<Vec<(String, Vec<(String, String)>)>, HashError> {
    let text_paths: Vec<String> = paths
        .iter()
        .map(|path| path.to_string_lossy().into_owned())
        .collect();
    hash_files(&text_paths, algorithm_names, workers)
}

pub fn hash_path(
    path: &Path,
    algorithm_names: &[String],
) -> Result<Vec<(String, String)>, HashError> {
    hash_file(&path.to_string_lossy(), algorithm_names)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::io::Write;

    fn temp_file(content: &[u8]) -> tempfile::NamedTempFile {
        let mut file = tempfile::NamedTempFile::new().expect("create tempfile");
        file.write_all(content).expect("write tempfile");
        file
    }

    #[test]
    fn hash_known_vectors() {
        let file = temp_file(b"hello world");
        let hashes = hash_file(
            &file.path().to_string_lossy(),
            &vec!["md5".into(), "sha256".into(), "sha512".into()],
        )
        .expect("hash file");

        let map: std::collections::HashMap<String, String> = hashes.into_iter().collect();
        assert_eq!(
            map.get("md5").expect("md5"),
            "5eb63bbbe01eeed093cb22bb8f5acdc3"
        );
        assert_eq!(
            map.get("sha256").expect("sha256"),
            "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
        );
        assert_eq!(
            map.get("sha512").expect("sha512"),
            "309ecc489c12d6eb4cc40f50c902f2b4d0ed77ee511a7c7a9bcd3ca86d4cd86f989dd35bc5ff499670da34255b45b0cfd830e81f605dcf7dc5542e93ae9cd76f"
        );
    }

    #[test]
    fn deduplicates_algorithm_list_preserving_order() {
        let parsed = parse_algorithms(&vec![
            "sha256".into(),
            "blake3".into(),
            "sha256".into(),
            "md5".into(),
        ])
        .expect("parse algorithms");

        assert_eq!(
            parsed,
            vec![
                HashAlgorithm::Sha256,
                HashAlgorithm::Blake3,
                HashAlgorithm::Md5
            ]
        );
    }

    #[test]
    fn unknown_algorithm_returns_error() {
        let err = parse_algorithms(&vec!["foo".into()]).expect_err("should fail");
        assert!(matches!(err, HashError::UnknownAlgorithm(_)));
    }

    #[test]
    fn hash_files_batch_returns_entry_per_path() {
        let file1 = temp_file(b"a");
        let file2 = temp_file(b"b");
        let paths = vec![
            file1.path().to_string_lossy().to_string(),
            file2.path().to_string_lossy().to_string(),
        ];

        let output = hash_files(&paths, &vec!["sha256".into()], Some(2)).expect("hash files");
        assert_eq!(output.len(), 2);
    }

    #[test]
    fn hash_file_reads_empty_file() {
        let dir = tempfile::tempdir().expect("create dir");
        let path = dir.path().join("empty.bin");
        fs::write(&path, b"" as &[u8]).expect("write empty file");

        let hashes = hash_file(&path.to_string_lossy(), &vec!["sha256".into()]).expect("hash file");
        assert_eq!(hashes.len(), 1);
    }
}
