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
import re
import socket
import subprocess
import sys
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
        """Build a cheap fingerprint to detect hardware changes."""
        parts: list[str] = []

        if sys.platform == "darwin":
            # macOS: use IOPlatformExpertDevice for platform UUID
            try:
                result = subprocess.run(
                    ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    for line in result.stdout.split("\n"):
                        if "IOPlatformUUID" in line:
                            parts.append(line.split('"')[-2])
                            break
            except Exception:
                pass
        else:
            # Linux: machine-id + PCI devices
            with contextlib.suppress(OSError), open("/etc/machine-id") as f:
                parts.append(f.read().strip())

            # NVIDIA driver version — changes on driver update
            with contextlib.suppress(OSError), open("/proc/driver/nvidia/version") as f:
                parts.append(f.read().strip())

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

        # CUDA toolkit — mtime changes on install/update (works on both platforms)
        for p in ("/usr/local/cuda/version.json", "/usr/local/cuda/version.txt"):
            try:
                parts.append(f"{p}:{os.stat(p).st_mtime}")
                break
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

            self._save_cache(
                fingerprint,
                {
                    "cuda": cuda_info,
                    "gpu": gpu_info,
                    "cpu": cpu_info,
                    "vm": vm_info,
                },
            )

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

        if sys.platform != "darwin":
            # Linux-specific: check /.dockerenv and /proc/self/cgroup
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

        # Environment variable checks work on any platform
        if os.environ.get("KUBERNETES_SERVICE_HOST"):
            container_info["type"] = "kubernetes"
        elif os.environ.get("container") == "podman":  # noqa: SIM112
            container_info["type"] = "podman"

        return container_info if container_info else None

    def _get_vm_info(self) -> dict[str, str] | None:
        """Detect if running in a VM and identify the hypervisor."""
        vm_info = {}

        if sys.platform == "darwin":
            # macOS: sysctl kern.hv_vmm_present returns 1 inside a VM
            stdout = self._run_command(["sysctl", "-n", "kern.hv_vmm_present"])
            if stdout and stdout.strip() == "1":
                vm_info["hypervisor"] = "unknown"
        else:
            # Linux
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

        # Try nvidia-smi first (works on both Linux and macOS if NVIDIA GPU present)
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

        # macOS: fall back to system_profiler for Apple/integrated GPUs
        if not gpu_info and sys.platform == "darwin":
            stdout = self._run_command(["system_profiler", "SPDisplaysDataType"])
            if stdout:
                current_gpu: dict[str, Any] = {}
                for line in stdout.split("\n"):
                    stripped = line.strip()
                    if stripped.startswith("Chipset Model:"):
                        if current_gpu:
                            gpu_info.append(current_gpu)
                        current_gpu = {"name": stripped.split(":", 1)[1].strip()}
                    elif stripped.startswith("VRAM") and current_gpu:
                        # e.g. "VRAM (Total):  16 GB" or "VRAM (Dynamic, Max): 48 GB"
                        vram_str = stripped.split(":", 1)[1].strip()
                        match = re.match(r"(\d+)\s*(MB|GB)", vram_str)
                        if match:
                            val = int(match.group(1))
                            if match.group(2) == "GB":
                                val *= 1024
                            current_gpu["memory_mb"] = val
                if current_gpu:
                    gpu_info.append(current_gpu)

        return gpu_info if gpu_info else None

    def _get_cpu_info(self) -> dict[str, Any] | None:
        """Get CPU information."""
        cpu_info: dict[str, Any] = {}

        with contextlib.suppress(Exception):
            cpu_info["count"] = os.cpu_count()

        if sys.platform == "darwin":
            # macOS: use sysctl for CPU info
            stdout = self._run_command(["sysctl", "-n", "machdep.cpu.brand_string"])
            if stdout:
                cpu_info["model"] = stdout.strip()

            cpu_info["architecture"] = platform.machine()

            stdout = self._run_command(["sysctl", "-n", "hw.physicalcpu"])
            if stdout:
                with contextlib.suppress(ValueError):
                    cpu_info["cores_per_socket"] = int(stdout.strip())

            stdout = self._run_command(["sysctl", "-n", "hw.logicalcpu"])
            if stdout:
                with contextlib.suppress(ValueError):
                    cpu_info["count"] = int(stdout.strip())
        else:
            # Linux
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
        memory_info: dict[str, int] = {}

        if sys.platform == "darwin":
            # macOS: total memory via sysctl
            stdout = self._run_command(["sysctl", "-n", "hw.memsize"])
            if stdout:
                with contextlib.suppress(ValueError):
                    memory_info["total_mb"] = int(stdout.strip()) // (1024 * 1024)

            # macOS: available memory via vm_stat (page size * free+inactive pages)
            stdout = self._run_command(["vm_stat"])
            if stdout:
                page_size = 16384  # default on Apple Silicon
                free_pages = 0
                # Parse page size from header: "Mach Virtual Memory Statistics: (page size of 16384 bytes)"
                for line in stdout.split("\n"):
                    if "page size of" in line:
                        match = re.search(r"page size of (\d+)", line)
                        if match:
                            page_size = int(match.group(1))
                    elif line.startswith(("Pages free:", "Pages inactive:")):
                        match = re.search(r"(\d+)", line.split(":")[1])
                        if match:
                            free_pages += int(match.group(1))
                if free_pages:
                    memory_info["available_mb"] = (free_pages * page_size) // (1024 * 1024)
        else:
            # Linux
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
