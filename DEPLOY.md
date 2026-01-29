# Deploying roar-cli to PyPI

This document describes how to publish `roar-cli` to PyPI.

## Prerequisites

### Accounts
- **TestPyPI**: Register at https://test.pypi.org/account/register/
- **PyPI**: Register at https://pypi.org/account/register/

### API Tokens
1. **TestPyPI token**: https://test.pypi.org/manage/account/token/
2. **PyPI token**: https://pypi.org/manage/account/token/

### Build Tools
- **Rust toolchain**: Required to build the tracer binary. Install via https://rustup.rs/
- **Python 3.10+**: Required for the Python package

### GitHub Secrets
Configure these in the repository settings (Settings > Secrets and variables > Actions):
- `TEST_PYPI_API_TOKEN` - Token from TestPyPI
- `PYPI_API_TOKEN` - Token from PyPI

### GitHub Environments (Optional)
Create environments for deployment protection (Settings > Environments):
- `testpypi` - For TestPyPI deployments
- `pypi` - For production deployments

---

## Building Locally

The build process automatically compiles the Rust tracer binary when Rust is installed.

```bash
# Clean previous builds
rm -rf dist/ build/ *.egg-info/ roar/bin/

# Build (automatically compiles tracer if Rust is available)
uv build

# Verify the build
uv run twine check dist/*
```

This creates two files in `dist/`:
- `roar_cli-{version}-py3-none-manylinux_2_17_x86_64.whl` - Platform-specific wheel (Linux x86_64)
- `roar_cli-{version}.tar.gz` - Source distribution

**Note**: The wheel is platform-specific because it contains the native tracer binary. Users on other platforms will need to install from source.

---

## Deploy to TestPyPI

Use TestPyPI to validate packaging before publishing to production.

### Via GitHub Actions (Recommended)

1. Go to **Actions** tab in the repository
2. Select **"Publish to TestPyPI"** workflow
3. Click **"Run workflow"**
4. Monitor execution
5. Verify at https://test.pypi.org/project/roar-cli/

### Via Local Machine

```bash
# Build (if not already built)
rm -rf dist/ build/ *.egg-info/
uv build

# Upload to TestPyPI
uv run twine upload --repository testpypi dist/*
# Username: __token__
# Password: <your TestPyPI API token>

# Test installation
uv pip install --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ roar-cli
```

---

## Deploy to Production PyPI

### Via GitHub Release (Recommended - Automated)

1. Update version in `pyproject.toml`
2. Commit and merge to main
3. Create a new GitHub Release:
   - **Tag**: `v{version}` (e.g., `v0.2.0`)
   - **Title**: `v{version}`
   - **Description**: Generate release notes or write manually
4. Click **"Publish release"**
5. The workflow triggers automatically
6. Verify at https://pypi.org/project/roar-cli/

### Via Local Machine

```bash
# Build
rm -rf dist/ build/ *.egg-info/
uv build

# Upload to PyPI
uv run twine upload dist/*
# Username: __token__
# Password: <your PyPI API token>

# Verify
uv pip install --upgrade roar-cli
```

---

## Multi-Platform Builds

The tracer binary uses `ptrace` which is Linux-specific.

### Linux x86_64 / aarch64
```bash
# Build on target platform (tracer compiled automatically)
uv build
# Creates platform-specific wheel
```

### macOS / Windows
The tracer currently only supports Linux. On unsupported platforms:
- Install from source distribution (requires Rust)
- `roar run` will show an error; other commands work normally

### CI/CD Multi-Platform Build
For automated multi-platform builds, use GitHub Actions with a matrix strategy:
```yaml
strategy:
  matrix:
    os: [ubuntu-latest, ubuntu-24.04-arm]
```

---

## Release Checklist

### Before Release
- [ ] All tests passing on main branch
- [ ] Version updated in `pyproject.toml`
- [ ] CHANGELOG updated (if maintained)
- [ ] TestPyPI deployment successful

### Release Process
1. [ ] Build package: `uv build` (automatically builds tracer)
2. [ ] Create GitHub Release with tag `v{version}`
3. [ ] Verify workflow completes successfully
4. [ ] Check https://pypi.org/project/roar-cli/
5. [ ] Test installation: `uv pip install roar-cli=={version}`
6. [ ] Verify CLI works: `roar --version`
7. [ ] Verify tracer works: `roar run echo hello`

### Post-Release
- [ ] Announce release (if applicable)
- [ ] Update documentation (if applicable)

---

## Verification

### TestPyPI
```bash
uv venv test-env
source test-env/bin/activate  # Windows: test-env\Scripts\activate
uv pip install --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ roar-cli
roar --help
roar --version

# Verify tracer binary is included (Linux only)
roar run echo "tracer test"
```

### Production PyPI
```bash
uv venv prod-env
source prod-env/bin/activate
uv pip install roar-cli
roar --help
roar --version

# Verify tracer binary is included (Linux only)
roar run echo "tracer test"
```

---

## Rollback

PyPI does not allow re-uploading the same version.

### Option 1: Yank the Release
Hides from default install but allows explicit version install:
1. Go to https://pypi.org/manage/project/roar-cli/releases/
2. Select the problematic version
3. Click **"Options"** > **"Yank"**

### Option 2: Publish a Patch
1. Fix the issue
2. Increment patch version (e.g., `0.1.0` → `0.1.1`)
3. Release the new version

### Option 3: Delete (Emergency Only)
- Only available within 24 hours of upload
- Not recommended as it can break dependent projects
- Go to release page > **"Options"** > **"Delete"**

---

## Troubleshooting

### "File already exists" Error
The version already exists on PyPI. Increment the version in `pyproject.toml`.

### Authentication Failed
- Ensure you're using `__token__` as the username
- Verify the API token is correct and has upload permissions
- Check the token hasn't expired

### Version Mismatch Error (CI)
The GitHub release tag must match the version in `pyproject.toml`:
- Tag: `v0.2.0` → pyproject.toml version: `0.2.0`

### Missing Dependencies on TestPyPI
TestPyPI may not have all dependencies. Use `--extra-index-url`:
```bash
uv pip install --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ roar-cli
```
