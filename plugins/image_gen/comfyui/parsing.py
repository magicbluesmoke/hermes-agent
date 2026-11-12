from typing import Any, Dict, Optional, Tuple


def parse_output(output: dict) -> dict:
    """Extract image_path/seed/steps from run_workflow's JSON output."""
    image_path = output.get("image_path")
    if not image_path and output.get("outputs"):
        image_path = output["outputs"][0].get("file")
    return {
        "image_path": image_path,
        "seed": output.get("seed"),
        "steps": output.get("steps"),
    }


def resolve_dimensions(aspect: str) -> Tuple[int, int]:
    dim_map = {
        "landscape": (1024, 768),
        "square": (1024, 1024),
        "portrait": (768, 1024),
    }
    return dim_map.get(aspect, (1024, 1024))
