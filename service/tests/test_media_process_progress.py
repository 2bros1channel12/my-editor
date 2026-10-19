import sys
from pathlib import Path

from localcut_service.proxies import MediaProcess


def test_media_process_reports_progress_without_overwriting_file_backed_video_output(tmp_path: Path) -> None:
    class Context:
        reports = []
        def check_cancelled(self): pass
        def report(self, value, stage, message=None): self.reports.append((value, stage, message))

    output = tmp_path / "sampled.frames"
    output.write_bytes(b"raw grayscale frames")
    script = "import time; print('out_time_us=500000', flush=True); time.sleep(.7); print('progress=end', flush=True)"
    context = Context()
    MediaProcess().run(
        [sys.executable, "-c", script], context, duration=1, stage="sampling proxy frames",
        base=.12, span=.38, stdout_file=output, stdout_progress_only=True,
    )

    assert output.read_bytes() == b"raw grayscale frames"
    assert context.reports
    assert context.reports[0][0] == .31
    assert context.reports[0][1] == "sampling proxy frames"
