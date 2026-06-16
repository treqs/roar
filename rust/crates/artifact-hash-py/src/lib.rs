use artifact_hash_core::{hash_file, hash_files, HashError};
use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;

fn to_py_error(err: HashError) -> PyErr {
    match err {
        HashError::UnknownAlgorithm(_) | HashError::InvalidWorkers(_) => {
            PyValueError::new_err(err.to_string())
        }
        HashError::Io(_) => PyOSError::new_err(err.to_string()),
    }
}

#[pyfunction]
fn compute_hash(path: String, algorithm: String) -> PyResult<String> {
    let output = hash_file(&path, &[algorithm]).map_err(to_py_error)?;
    output
        .into_iter()
        .next()
        .map(|(_, digest)| digest)
        .ok_or_else(|| PyValueError::new_err("no algorithm provided"))
}

#[pyfunction]
fn compute_hashes<'py>(
    py: Python<'py>,
    path: String,
    algorithms: Vec<String>,
) -> PyResult<Bound<'py, PyDict>> {
    let output = hash_file(&path, &algorithms).map_err(to_py_error)?;
    let dict = PyDict::new(py);
    for (algorithm, digest) in output {
        dict.set_item(algorithm, digest)?;
    }
    Ok(dict)
}

#[pyfunction]
#[pyo3(signature = (paths, algorithms, workers=None))]
fn compute_hashes_batch<'py>(
    py: Python<'py>,
    paths: Vec<String>,
    algorithms: Vec<String>,
    workers: Option<usize>,
) -> PyResult<Bound<'py, PyDict>> {
    let output = hash_files(&paths, &algorithms, workers).map_err(to_py_error)?;
    let dict = PyDict::new(py);

    for (path, hashes) in output {
        let digest_map = PyDict::new(py);
        for (algorithm, digest) in hashes {
            digest_map.set_item(algorithm, digest)?;
        }
        dict.set_item(path, digest_map)?;
    }

    Ok(dict)
}

#[pymodule]
fn _hash_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(compute_hash, m)?)?;
    m.add_function(wrap_pyfunction!(compute_hashes, m)?)?;
    m.add_function(wrap_pyfunction!(compute_hashes_batch, m)?)?;
    Ok(())
}
