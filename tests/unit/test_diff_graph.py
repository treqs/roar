from __future__ import annotations

from roar.application.query.diff_graph import glaas_payload_to_graph


class TestGlaasPayloadToGraph:
    def test_reads_camel_case_jobs_and_flattens_children(self):
        payload = {
            "artifact": {
                "hashes": [{"algorithm": "blake3", "digest": "target-hash"}],
                "first_seen_path": "/output/model.pt",
            },
            "jobs": [
                {
                    "id": "parent-id",
                    "jobUid": "parent-job",
                    "command": "python train.py --epochs 2",
                    "jobType": "run",
                    "stepNumber": 2,
                    "gitCommit": "a" * 40,
                    "gitBranch": "main",
                    "metadata": {"role": "driver"},
                    "inputs": [{"hash": "checkpoint-hash", "path": "/input/checkpoint.pt"}],
                    "outputs": [{"hash": "final-hash", "path": "/output/model.pt"}],
                    "children": [
                        {
                            "id": "child-id",
                            "jobUid": "child-job",
                            "command": "python -m pkg.worker --rank 0",
                            "jobType": "ray_task",
                            "stepNumber": 1,
                            "gitCommit": "b" * 40,
                            "gitBranch": "main",
                            "metadata": '{"worker": 1}',
                            "inputs": [{"hash": "raw-hash", "path": "/input/raw.csv"}],
                            "outputs": [
                                {"hash": "checkpoint-hash", "path": "/output/checkpoint.pt"}
                            ],
                            "children": [],
                        }
                    ],
                }
            ],
        }

        graph = glaas_payload_to_graph("abc123", payload)

        assert graph.target_hash == "target-hash"
        assert graph.target_path == "/output/model.pt"
        assert [job.job_uid for job in graph.jobs] == ["child-job", "parent-job"]

        child_job, parent_job = graph.jobs
        assert child_job.step_number == 1
        assert child_job.git_commit == "b" * 40
        assert child_job.git_branch == "main"
        assert child_job.metadata == {"worker": 1}
        assert child_job.input_hashes == {"raw-hash": "raw-hash"}
        assert child_job.output_hashes == {"checkpoint-hash": "checkpoint-hash"}

        assert parent_job.step_number == 2
        assert parent_job.git_commit == "a" * 40
        assert parent_job.git_branch == "main"
        assert parent_job.metadata == {"role": "driver"}
        assert parent_job.input_hashes == {"checkpoint-hash": "checkpoint-hash"}
        assert parent_job.output_hashes == {"final-hash": "final-hash"}
