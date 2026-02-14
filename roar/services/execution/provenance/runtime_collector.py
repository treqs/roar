"""
Runtime collector service for provenance collection.

Collects runtime environment information including OS, hardware, CUDA, etc.
"""

import contextlib
import glob
import hashlib
import json
import os
import platform
import socket
import subprocess
from typing import Any

from ....core.interfaces.logger import ILogger
from ....core.interfaces.provenance import PythonInjectData, RuntimeInfo, TracerData

_CACHE_FILENAME = "runtime_cache.json"


class RuntimeCollectorService:
    """Collects runtime environment information."""

    def __init__(self, logger: ILogger | None = None, cache_dir: str | None = None) -> None:
        """Initialize runtime collector with optional logger and cache directory."""
        self._logger = logger
        self._cache_dir = cache_dir

    @property
    def logger(self) -> ILogger:
        """Get logger, resolving from container or creating NullLogger."""
        if self._logger is None:
            from ....core.logging import get_logger

            self._logger = get_logger()
        return self._logger

    @staticmethod
    def _hardware_fingerprint() -> str:
        """Build a cheap fingerprint from procfs/sysfs to detect hardware changes."""
        parts: list[str] = []

        # Machine identity
        with contextlib.suppress(OSError):
            with open("/etc/machine-id") as f:
                parts.append(f.read().strip())

        # NVIDIA driver version — changes on driver update
        with contextlib.suppress(OSError):
            with open("/proc/driver/nvidia/version") as f:
                parts.append(f.read().strip())

        # CUDA toolkit — mtime changes on install/update
        for p in ("/usr/local/cuda/version.json", "/usr/local/cuda/version.txt"):
            try:
                parts.append(f"{p}:{os.stat(p).st_mtime}")
                break
            except OSError:
                pass

        # GPU PCI device IDs — catches physical GPU swaps
        with contextlib.suppress(OSError):
            for cls_path in sorted(glob.glob("/sys/bus/pci/devices/*/class")):
                try:
                    with open(cls_path) as f:
                        if f.read().strip().startswith("0x0300"):  # VGA controller
                            dev_dir = os.path.dirname(cls_path)
                            with open(f"{dev_dir}/vendor") as vf:
                                vendor = vf.read().strip()
                            with open(f"{dev_dir}/device") as df:
                                device = df.read().strip()
                            parts.append(f"gpu:{vendor}:{device}")
                except OSError:
                    pass

        return hashlib.md5("|".join(parts).encode()).hexdigest()

    def _load_cache(self) -> dict[str, Any] | None:
        if not self._cache_dir:
            return None
        try:
            with open(os.path.join(self._cache_dir, _CACHE_FILENAME)) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def _save_cache(self, fingerprint: str, data: dict[str, Any]) -> None:
        if not self._cache_dir:
            return
        try:
            with open(os.path.join(self._cache_dir, _CACHE_FILENAME), "w") as f:
                json.dump({"fingerprint": fingerprint, "data": data}, f)
        except OSError:
            pass

    def collect(
        self,
        python_data: PythonInjectData,
        tracer_data: TracerData,
        timing: dict[str, Any],
    ) -> RuntimeInfo:
        """
        Collect runtime environment info.

        Args:
            python_data: Python inject data
            tracer_data: Tracer data
            timing: Timing information dict

        Returns:
            RuntimeInfo with collected values
        """
        self.logger.debug("RuntimeCollectorService.collect: collecting runtime info")

        # Get command from root process (parent_pid is None), falling back to first process.
        command = []
        if tracer_data.processes:
            root = next((p for p in tracer_data.processes if p.get("parent_pid") is None), None)
            source = root if root is not None else tracer_data.processes[0]
            command = source.get("command", [])

        # Try hardware cache for expensive subprocess-based collectors
        fingerprint = self._hardware_fingerprint()
        cache = self._load_cache()

        if cache and cache.get("fingerprint") == fingerprint:
            self.logger.debug("Runtime cache hit (fingerprint=%s)", fingerprint[:8])
            cached = cache["data"]
            cuda_info = cached.get("cuda")
            gpu_info = cached.get("gpu")
            cpu_info = cached.get("cpu")
            vm_info = cached.get("vm")
        else:
            self.logger.debug("Runtime cache miss, collecting hardware info")
            self.logger.debug("Collecting VM info")
            vm_info = self._get_vm_info()
            self.logger.debug("Collecting CUDA info")
            cuda_info = self._get_cuda_info()
            self.logger.debug("Collecting GPU info")
            gpu_info = self._get_gpu_info()
            self.logger.debug("Collecting CPU info")
            cpu_info = self._get_cpu_info()

            self._save_cache(fingerprint, {
                "cuda": cuda_info,
                "gpu": gpu_info,
                "cpu": cpu_info,
                "vm": vm_info,
            })

        # Always collect fresh — cheap file reads only
        self.logger.debug("Collecting container info")
        container_info = self._get_container_info()
        self.logger.debug("Collecting memory info")
        memory_info = self._get_memory_info()

        self.logger.debug(
            "Runtime collection complete: container=%s, vm=%s, cuda=%s, gpu=%s",
            container_info is not None,
            vm_info is not None,
            cuda_info is not None,
            gpu_info is not None,
        )

        return RuntimeInfo(
            hostname=socket.gethostname(),
            timing=timing,
            command=command,
            os={
                "system": platform.system(),
                "release": platform.release(),
                "version": platform.version(),
                "machine": platform.machine(),
            },
            python={
                "version": platform.python_version(),
                "implementation": platform.python_implementation(),
            },
            env_vars=python_data.env_reads,
            container=container_info,
            vm=vm_info,
            cuda=cuda_info,
            gpu=gpu_info,
            cpu=cpu_info,
            memory=memory_info,
        )

    def _run_command(
        self,
        args: list[str],
        timeout: int = 5,
    ) -> str | None:
        """
        Run a command and return stdout if successful.

        Args:
            args: Command and arguments
            timeout: Timeout in seconds

        Returns:
            stdout on success, None on failure
        """
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode == 0:
                return result.stdout
        except Exception as e:
            self.logger.debug("Command %s failed: %s", args[0] if args else "unknown", e)
        return None

    def _get_cuda_info(self) -> dict[str, str] | None:
        """Get CUDA and cuDNN version information."""
        cuda_info = {}

        # CUDA version from nvidia-smi
        stdout = self._run_command(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"]
        )
        if stdout:
            cuda_info["driver_version"] = stdout.strip().split("\n")[0]

        # CUDA runtime version from nvcc
        stdout = self._run_command(["nvcc", "--version"])
        if stdout:
            for line in stdout.split("\n"):
                if "release" in line.lower():
                    parts = line.split("release")
                    if len(parts) > 1:
                        cuda_info["cuda_version"] = parts[1].split(",")[0].strip()
                    break

        # Fallback: CUDA version from nvidia-smi header
        if "cuda_version" not in cuda_info:
            stdout = self._run_command(["nvidia-smi"])
            if stdout:
                for line in stdout.split("\n"):
                    if "CUDA Version" in line:
                        parts = line.split("CUDA Version:")
                        if len(parts) > 1:
                            cuda_info["cuda_version"] = parts[1].strip().split()[0]
                        break

        # cuDNN version
        stdout = self._run_command(["ldconfig", "-p"])
        if stdout:
            for line in stdout.split("\n"):
                if "libcudnn" in line and ".so." in line:
                    parts = line.split("libcudnn.so.")
                    if len(parts) > 1:
                        version = parts[1].split()[0].rstrip(")")
                        cuda_info["cudnn_version"] = version
                    break

        return cuda_info if cuda_info else None

    def _get_container_info(self) -> dict[str, str] | None:
        """Detect if running in a container and get container info."""
        container_info = {}

        try:
            if os.path.exists("/.dockerenv"):
                container_info["type"] = "docker"

            with open("/proc/self/cgroup") as f:
                for line in f:
                    if "docker" in line or "containerd" in line:
                        container_info["type"] = "docker"
                        parts = line.strip().split("/")
                        if len(parts) > 1 and len(parts[-1]) >= 12:
                            container_info["container_id"] = parts[-1][:12]
                        break
                    elif "kubepods" in line:
                        container_info["type"] = "kubernetes"
                        break
        except Exception as e:
            self.logger.debug("Failed to detect container info: %s", e)

        if os.environ.get("KUBERNETES_SERVICE_HOST"):
            container_info["type"] = "kubernetes"
        elif os.environ.get("container") == "podman":  # noqa: SIM112
            container_info["type"] = "podman"

        return container_info if container_info else None

    def _get_vm_info(self) -> dict[str, str] | None:
        """Detect if running in a VM and identify the hypervisor."""
        vm_info = {}

        stdout = self._run_command(["systemd-detect-virt"])
        if stdout:
            virt = stdout.strip()
            if virt and virt != "none":
                vm_info["hypervisor"] = virt

        try:
            if os.path.exists("/sys/hypervisor/type"):
                with open("/sys/hypervisor/type") as f:
                    vm_info["hypervisor"] = f.read().strip()
        except Exception as e:
            self.logger.debug("Failed to read hypervisor type: %s", e)

        try:
            if os.path.exists("/sys/class/dmi/id/sys_vendor"):
                with open("/sys/class/dmi/id/sys_vendor") as f:
                    vendor = f.read().strip()
                    if "Amazon" in vendor:
                        vm_info["cloud"] = "aws"
                    elif "Google" in vendor:
                        vm_info["cloud"] = "gcp"
                    elif "Microsoft" in vendor:
                        vm_info["cloud"] = "azure"
        except Exception as e:
            self.logger.debug("Failed to read sys_vendor for cloud detection: %s", e)

        return vm_info if vm_info else None

    def _get_gpu_info(self) -> list[dict[str, Any]] | None:
        """Get GPU information."""
        gpu_info = []

        stdout = self._run_command(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,compute_cap",
                "--format=csv,noheader,nounits",
            ]
        )
        if stdout:
            for line in stdout.strip().split("\n"):
                if line:
                    parts = line.split(", ")
                    if len(parts) >= 2:
                        gpu = {
                            "name": parts[0],
                            "memory_mb": int(parts[1]) if parts[1].isdigit() else parts[1],
                        }
                        if len(parts) >= 3:
                            gpu["compute_cap"] = parts[2]
                        gpu_info.append(gpu)

        return gpu_info if gpu_info else None

    def _get_cpu_info(self) -> dict[str, Any] | None:
        """Get CPU information."""
        cpu_info: dict[str, Any] = {}

        with contextlib.suppress(Exception):
            cpu_info["count"] = os.cpu_count()

        try:
            with open("/proc/cpuinfo") as f:
                cpuinfo = f.read()
            for line in cpuinfo.split("\n"):
                if line.startswith("model name"):
                    cpu_info["model"] = line.split(":")[1].strip()
                    break
        except Exception as e:
            self.logger.debug("Failed to read /proc/cpuinfo: %s", e)

        stdout = self._run_command(["lscpu"])
        if stdout:
            for line in stdout.split("\n"):
                if line.startswith("Architecture:"):
                    cpu_info["architecture"] = line.split(":")[1].strip()
                elif line.startswith("CPU(s):"):
                    cpu_info["count"] = int(line.split(":")[1].strip())
                elif line.startswith("Thread(s) per core:"):
                    cpu_info["threads_per_core"] = int(line.split(":")[1].strip())
                elif line.startswith("Core(s) per socket:"):
                    cpu_info["cores_per_socket"] = int(line.split(":")[1].strip())
                elif line.startswith("Socket(s):"):
                    cpu_info["sockets"] = int(line.split(":")[1].strip())

        return cpu_info if cpu_info else None

    def _get_memory_info(self) -> dict[str, int] | None:
        """Get system memory information."""
        memory_info = {}

        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        memory_info["total_mb"] = kb // 1024
                    elif line.startswith("MemAvailable:"):
                        kb = int(line.split()[1])
                        memory_info["available_mb"] = kb // 1024
        except Exception as e:
            self.logger.debug("Failed to read /proc/meminfo: %s", e)

        return memory_info if memory_info else None
