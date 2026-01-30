"""
Unit tests for the DagRenderer class.

Tests the ASCII rendering logic for various DAG shapes.
"""

from roar.core.models.dag import (
    DagArtifactInfo,
    DagNodeInfo,
    DagNodeMetrics,
    DagNodeState,
    DagVisualization,
)
from roar.presenters.dag_renderer import DagRenderer


class TestDagRenderer:
    """Test cases for DagRenderer."""

    def test_empty_dag(self):
        """Render an empty DAG."""
        dag = DagVisualization(
            nodes=[],
            artifacts=[],
            stale_count=0,
            total_steps=0,
            is_expanded=False,
            session_id=1,
        )
        renderer = DagRenderer(use_color=False)
        output = renderer.render(dag)

        assert "Pipeline: 0 steps" in output
        assert "No steps in pipeline." in output

    def test_single_node_dag(self):
        """Render a DAG with a single node."""
        dag = DagVisualization(
            nodes=[
                DagNodeInfo(
                    step_number=1,
                    job_id=100,
                    command="python preprocess.py",
                    state=DagNodeState.ACTIVE,
                    metrics=DagNodeMetrics(inputs=1, outputs=1, consumed=0),
                    dependencies=[],
                    is_build=False,
                )
            ],
            artifacts=[
                DagArtifactInfo(
                    path="output.csv",
                    hash="abc123",
                    is_stale=False,
                    producer_step=1,
                )
            ],
            stale_count=0,
            total_steps=1,
            is_expanded=False,
            session_id=1,
        )
        renderer = DagRenderer(use_color=False)
        output = renderer.render(dag)

        assert "Pipeline: 1 steps" in output
        assert "@1" in output
        assert "preprocess.py" in output
        assert "in:1" in output
        assert "out:1" in output
        assert "cons:0" in output
        assert "(output.csv)" in output

    def test_linear_pipeline(self):
        """Render a linear pipeline: step1 -> step2 -> step3."""
        dag = DagVisualization(
            nodes=[
                DagNodeInfo(
                    step_number=1,
                    job_id=100,
                    command="python preprocess.py",
                    state=DagNodeState.ACTIVE,
                    metrics=DagNodeMetrics(inputs=1, outputs=1, consumed=0),
                    dependencies=[],
                    is_build=False,
                ),
                DagNodeInfo(
                    step_number=2,
                    job_id=101,
                    command="python train.py",
                    state=DagNodeState.ACTIVE,
                    metrics=DagNodeMetrics(inputs=1, outputs=1, consumed=1),
                    dependencies=[1],
                    is_build=False,
                ),
                DagNodeInfo(
                    step_number=3,
                    job_id=102,
                    command="python evaluate.py",
                    state=DagNodeState.ACTIVE,
                    metrics=DagNodeMetrics(inputs=2, outputs=1, consumed=2),
                    dependencies=[2],
                    is_build=False,
                ),
            ],
            artifacts=[
                DagArtifactInfo(
                    path="metrics.json",
                    hash="def456",
                    is_stale=False,
                    producer_step=3,
                )
            ],
            stale_count=0,
            total_steps=3,
            is_expanded=False,
            session_id=1,
        )
        renderer = DagRenderer(use_color=False)
        output = renderer.render(dag)

        assert "Pipeline: 3 steps" in output
        assert "@1" in output
        assert "@2" in output
        assert "@3" in output
        assert "(metrics.json)" in output

    def test_diamond_pattern(self):
        """Render a diamond pattern: step1 -> step2/step3 -> step4."""
        dag = DagVisualization(
            nodes=[
                DagNodeInfo(
                    step_number=1,
                    job_id=100,
                    command="python split.py",
                    state=DagNodeState.ACTIVE,
                    metrics=DagNodeMetrics(inputs=1, outputs=2, consumed=0),
                    dependencies=[],
                    is_build=False,
                ),
                DagNodeInfo(
                    step_number=2,
                    job_id=101,
                    command="python train_model.py",
                    state=DagNodeState.ACTIVE,
                    metrics=DagNodeMetrics(inputs=1, outputs=1, consumed=1),
                    dependencies=[1],
                    is_build=False,
                ),
                DagNodeInfo(
                    step_number=3,
                    job_id=102,
                    command="python compute_baseline.py",
                    state=DagNodeState.ACTIVE,
                    metrics=DagNodeMetrics(inputs=1, outputs=1, consumed=1),
                    dependencies=[1],
                    is_build=False,
                ),
                DagNodeInfo(
                    step_number=4,
                    job_id=103,
                    command="python evaluate.py",
                    state=DagNodeState.ACTIVE,
                    metrics=DagNodeMetrics(inputs=2, outputs=1, consumed=2),
                    dependencies=[2, 3],
                    is_build=False,
                ),
            ],
            artifacts=[
                DagArtifactInfo(
                    path="report.json",
                    hash="abc123",
                    is_stale=False,
                    producer_step=4,
                )
            ],
            stale_count=0,
            total_steps=4,
            is_expanded=False,
            session_id=1,
        )
        renderer = DagRenderer(use_color=False)
        output = renderer.render(dag)

        assert "Pipeline: 4 steps" in output
        assert "@1" in output
        assert "@2" in output
        assert "@3" in output
        assert "@4" in output

    def test_stale_marker(self):
        """Render a DAG with stale steps shows * marker."""
        dag = DagVisualization(
            nodes=[
                DagNodeInfo(
                    step_number=1,
                    job_id=100,
                    command="python preprocess.py",
                    state=DagNodeState.ACTIVE,
                    metrics=DagNodeMetrics(inputs=1, outputs=1, consumed=0),
                    dependencies=[],
                    is_build=False,
                ),
                DagNodeInfo(
                    step_number=2,
                    job_id=101,
                    command="python train.py",
                    state=DagNodeState.STALE,
                    metrics=DagNodeMetrics(inputs=1, outputs=1, consumed=1),
                    dependencies=[1],
                    is_build=False,
                ),
            ],
            artifacts=[],
            stale_count=1,
            total_steps=2,
            is_expanded=False,
            session_id=1,
        )
        renderer = DagRenderer(use_color=False)
        output = renderer.render(dag)

        assert "(1 stale steps)" in output
        # The stale marker should appear
        assert "*" in output
        assert "Legend: * = stale" in output

    def test_build_step_prefix(self):
        """Render a DAG with build steps shows @B prefix."""
        dag = DagVisualization(
            nodes=[
                DagNodeInfo(
                    step_number=1,
                    job_id=100,
                    command="pip install -r requirements.txt",
                    state=DagNodeState.ACTIVE,
                    metrics=DagNodeMetrics(inputs=1, outputs=50, consumed=0),
                    dependencies=[],
                    is_build=True,
                ),
                DagNodeInfo(
                    step_number=2,
                    job_id=101,
                    command="python train.py",
                    state=DagNodeState.ACTIVE,
                    metrics=DagNodeMetrics(inputs=1, outputs=1, consumed=0),
                    dependencies=[],
                    is_build=False,
                ),
            ],
            artifacts=[],
            stale_count=0,
            total_steps=2,
            is_expanded=False,
            session_id=1,
        )
        renderer = DagRenderer(use_color=False)
        output = renderer.render(dag)

        assert "@B1" in output  # Build step
        assert "@2" in output  # Regular step
        assert "B = build step" in output

    def test_superseded_marker_in_expanded_view(self):
        """Render expanded view shows [superseded] marker."""
        dag = DagVisualization(
            nodes=[
                DagNodeInfo(
                    step_number=1,
                    job_id=100,
                    command="python preprocess.py",
                    state=DagNodeState.SUPERSEDED,
                    metrics=DagNodeMetrics(inputs=1, outputs=1, consumed=0),
                    dependencies=[],
                    is_build=False,
                ),
                DagNodeInfo(
                    step_number=1,
                    job_id=101,
                    command="python preprocess.py",
                    state=DagNodeState.ACTIVE,
                    metrics=DagNodeMetrics(inputs=1, outputs=1, consumed=0),
                    dependencies=[],
                    is_build=False,
                ),
            ],
            artifacts=[],
            stale_count=0,
            total_steps=1,
            is_expanded=True,
            session_id=1,
        )
        renderer = DagRenderer(use_color=False)
        output = renderer.render(dag)

        assert "[superseded]" in output

    def test_step_name_displayed(self):
        """Render a DAG with step names shows the name instead of command."""
        dag = DagVisualization(
            nodes=[
                DagNodeInfo(
                    step_number=1,
                    job_id=100,
                    command="python very_long_script_name.py --with-many-args",
                    step_name="preprocess",
                    state=DagNodeState.ACTIVE,
                    metrics=DagNodeMetrics(inputs=1, outputs=1, consumed=0),
                    dependencies=[],
                    is_build=False,
                ),
            ],
            artifacts=[],
            stale_count=0,
            total_steps=1,
            is_expanded=False,
            session_id=1,
        )
        renderer = DagRenderer(use_color=False)
        output = renderer.render(dag)

        assert "preprocess" in output
        # Command should not be shown when step_name is available
        # (unless truncated command contains it)

    def test_json_output(self):
        """Test JSON rendering produces correct structure."""
        dag = DagVisualization(
            nodes=[
                DagNodeInfo(
                    step_number=1,
                    job_id=100,
                    job_uid="abc123",
                    command="python preprocess.py",
                    state=DagNodeState.ACTIVE,
                    metrics=DagNodeMetrics(inputs=1, outputs=1, consumed=0),
                    dependencies=[],
                    is_build=False,
                    exit_code=0,
                ),
            ],
            artifacts=[
                DagArtifactInfo(
                    path="output.csv",
                    hash="def456",
                    is_stale=False,
                    producer_step=1,
                )
            ],
            stale_count=0,
            total_steps=1,
            is_expanded=False,
            session_id=42,
        )
        renderer = DagRenderer(use_color=False)
        json_output = renderer.render_json(dag)

        assert json_output["session_id"] == 42
        assert json_output["total_steps"] == 1
        assert json_output["stale_count"] == 0
        assert json_output["is_expanded"] is False

        assert len(json_output["nodes"]) == 1
        node = json_output["nodes"][0]
        assert node["step_number"] == 1
        assert node["job_id"] == 100
        assert node["job_uid"] == "abc123"
        assert node["command"] == "python preprocess.py"
        assert node["state"] == "active"
        assert node["exit_code"] == 0
        assert node["metrics"]["inputs"] == 1
        assert node["metrics"]["outputs"] == 1
        assert node["metrics"]["consumed"] == 0

        assert len(json_output["artifacts"]) == 1
        artifact = json_output["artifacts"][0]
        assert artifact["path"] == "output.csv"
        assert artifact["hash"] == "def456"
        assert artifact["is_stale"] is False
        assert artifact["producer_step"] == 1

    def test_color_disabled(self):
        """Test that no ANSI codes appear when colors are disabled."""
        dag = DagVisualization(
            nodes=[
                DagNodeInfo(
                    step_number=1,
                    job_id=100,
                    command="python preprocess.py",
                    state=DagNodeState.STALE,
                    metrics=DagNodeMetrics(inputs=1, outputs=1, consumed=0),
                    dependencies=[],
                    is_build=False,
                ),
            ],
            artifacts=[],
            stale_count=1,
            total_steps=1,
            is_expanded=False,
            session_id=1,
        )
        renderer = DagRenderer(use_color=False)
        output = renderer.render(dag)

        # No ANSI escape codes should be present
        assert "\033[" not in output

    def test_terminal_width_truncation(self):
        """Test that commands are truncated based on terminal width."""
        long_command = "python " + "a" * 200 + ".py"
        dag = DagVisualization(
            nodes=[
                DagNodeInfo(
                    step_number=1,
                    job_id=100,
                    command=long_command,
                    state=DagNodeState.ACTIVE,
                    metrics=DagNodeMetrics(inputs=1, outputs=1, consumed=0),
                    dependencies=[],
                    is_build=False,
                ),
            ],
            artifacts=[],
            stale_count=0,
            total_steps=1,
            is_expanded=False,
            session_id=1,
        )
        # Set narrow terminal width
        renderer = DagRenderer(use_color=False, terminal_width=60)
        output = renderer.render(dag)

        # Output should be truncated
        assert "..." in output
        # Full command should not appear
        assert long_command not in output


class TestDagNodeState:
    """Test DagNodeState enum values."""

    def test_state_values(self):
        """Verify state enum values match expected strings."""
        assert DagNodeState.ACTIVE.value == "active"
        assert DagNodeState.CACHED.value == "cached"
        assert DagNodeState.STALE.value == "stale"
        assert DagNodeState.SUPERSEDED.value == "superseded"
