from __future__ import annotations

import secrets
from pathlib import Path

import click

from ...backends.k8s.attach import K8sAttachError, attach_k8s_workload
from ...backends.k8s.config import load_k8s_backend_config
from ...backends.k8s.manifest import (
    K8sManifestError,
    dump_manifest_documents,
    load_manifest_documents,
    rewrite_manifest_for_lineage,
)
from ...backends.k8s.submit import resolve_runtime_requirement
from ..context import RoarContext
from ..decorators import require_init


@click.group("k8s", invoke_without_command=True)
@click.pass_context
def k8s(ctx: click.Context) -> None:
    """Prepare and attach Kubernetes workloads for lineage capture."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@k8s.command("attach")
@click.argument("workload")
@click.option(
    "-n",
    "--namespace",
    default="default",
    show_default=True,
    help="Namespace of the workload",
)
@click.option(
    "--context",
    "kube_context",
    default=None,
    help="kubectl context to use",
)
@click.option(
    "--wait/--no-wait",
    default=None,
    help="Override k8s.wait_for_completion for this attach",
)
@click.option(
    "--session-file",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    default=None,
    help="Fragment-session .key file when the cluster Secret is not readable",
)
@click.pass_obj
@require_init
def k8s_attach(
    ctx: RoarContext,
    workload: str,
    namespace: str,
    kube_context: str | None,
    wait: bool | None,
    session_file: Path | None,
) -> None:
    """Reconstitute lineage from an already-submitted workload.

    WORKLOAD is a name or KIND/NAME (job, jobset, pytorchjob, trainjob) that
    was instrumented at submit time. Credentials come from a locally saved
    fragment-session key, the cluster Secret, or --session-file.
    """
    global_flags = ["--context", kube_context] if kube_context else []
    try:
        result = attach_k8s_workload(
            roar_dir=ctx.roar_dir,
            repo_root=str(ctx.repo_root or ctx.roar_dir.parent),
            workload=workload,
            namespace=namespace,
            global_flags=global_flags,
            wait=wait,
            session_file=session_file,
        )
    except K8sAttachError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(
        f"[roar] lineage reconstituted from {result.workload}: "
        f"{result.jobs_merged} jobs, {result.artifacts_merged} artifacts "
        f"({result.fragments_processed} fragments)"
    )
    if result.exit_code != 0:
        raise SystemExit(result.exit_code)


@k8s.command("prepare")
@click.option(
    "-f",
    "--filename",
    "manifest_path",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    required=True,
    help="Job manifest to instrument",
)
@click.option(
    "-o",
    "--output",
    "output_path",
    type=click.Path(path_type=Path, dir_okay=False),
    required=True,
    help="Where to write the instrumented manifest",
)
@click.option(
    "--secret-name",
    default="roar-fragment-session",
    show_default=True,
    help="Fragment-session Secret the pods will read credentials from",
)
@click.option(
    "--requirement",
    default=None,
    help="Override the roar runtime install requirement (defaults to k8s.runtime_install_requirement or the pinned roar-cli version)",
)
@click.option(
    "--cluster-glaas-url",
    default=None,
    help="Cluster-visible GLaaS URL injected into pods (defaults to k8s.cluster_glaas_url)",
)
def k8s_prepare(
    manifest_path: Path,
    output_path: Path,
    secret_name: str,
    requirement: str | None,
    cluster_glaas_url: str | None,
) -> None:
    """Rewrite a Job manifest with lineage instrumentation for inspection.

    Unlike the managed `roar run kubectl apply -f ...` path, no fragment
    session is registered and no Secret is embedded: create the Secret named
    by --secret-name (keys: session_id, token) before applying the output.
    """
    config = load_k8s_backend_config()
    resolved_requirement = requirement or resolve_runtime_requirement(config)
    resolved_cluster_glaas = (
        cluster_glaas_url or str(config.get("cluster_glaas_url") or "") or "http://glaas:3001"
    )

    try:
        documents = load_manifest_documents(manifest_path)
        rewrite = rewrite_manifest_for_lineage(
            documents,
            secret_name=secret_name,
            session_id=None,
            fragment_token=None,
            requirement=resolved_requirement,
            cluster_glaas_url=resolved_cluster_glaas,
            tracer=str(config.get("tracer") or "preload"),
            parent_job_uid=secrets.token_hex(4),
        )
    except K8sManifestError as exc:
        raise click.ClickException(str(exc)) from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(dump_manifest_documents(rewrite.documents), encoding="utf-8")

    click.echo(f"Prepared manifest written to {output_path}")
    click.echo(
        f"  workload:           {rewrite.workload_kind}/{rewrite.job_name} "
        f"(namespace {rewrite.namespace})"
    )
    click.echo(f"  wrapped containers: {', '.join(rewrite.wrapped_containers)}")
    if rewrite.skipped_containers:
        click.echo(f"  skipped (no explicit command): {', '.join(rewrite.skipped_containers)}")
    click.echo(f"  runtime install:    {resolved_requirement}")
    click.echo()
    click.echo("Before applying, create the fragment-session Secret:")
    click.echo(
        f"  kubectl create secret generic {secret_name} "
        "--from-literal=session_id=<id> --from-literal=token=<token>"
    )
    click.echo("Or use the managed path instead: roar run kubectl apply -f <manifest>")


__all__ = ["k8s"]
