#!/usr/bin/env python3
"""Direct script: submit a workflow to ComfyUI via run_workflow.py and emit JSON."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

RUN_WORKFLOW = Path.home() / ".hermes/skills/creative/comfyui/scripts/run_workflow.py"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--prompt", required=True)
    p.add_argument("--width", type=int, default=1024)
    p.add_argument("--height", type=int, default=1024)
    p.add_argument("--steps", type=int, default=25)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--workflow", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--host", default="http://127.0.0.1:8188")
    args = p.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    wf_path = Path(args.workflow)
    with wf_path.open() as f:
        wf = json.load(f)
    wf.pop("_comment", None)
    tmp_wf = out_dir / "_clean_workflow.json"
    tmp_wf.write_text(json.dumps(wf))
    workflow_arg = str(tmp_wf)
    args_json = {"prompt": args.prompt, "width": args.width, "height": args.height, "steps": args.steps}
    if args.seed is not None:
        args_json["seed"] = args.seed

    cmd = [sys.executable, str(RUN_WORKFLOW),
           "--workflow", workflow_arg,
           "--args", json.dumps(args_json),
           "--output-dir", str(out_dir),
           "--host", args.host]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=280)
    if result.returncode != 0:
        print(json.dumps({"success": False, "error": result.stderr.strip() or "unknown"}))
        sys.exit(1)

    try:
        out = json.loads(result.stdout.strip())
    except Exception as e:
        print(json.dumps({"success": False, "error": f"parse error: {e}"}))
        sys.exit(1)

    # run_workflow returns outputs list with file paths
    image_path = None
    if isinstance(out, dict):
        if out.get("outputs"):
            image_path = out["outputs"][0].get("file")
        elif out.get("image"):
            image_path = out["image"]
    if not image_path:
        print(json.dumps({"success": False, "error": "no output file"}))
        sys.exit(1)

    print(json.dumps({"success": True, "image_path": image_path}))


if __name__ == "__main__":
    main()