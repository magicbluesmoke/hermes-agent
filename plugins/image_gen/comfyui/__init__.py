from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    resolve_aspect_ratio,
    success_response,
    error_response,
)

logger = logging.getLogger(__name__)

MODEL_NAME = "sdxl-base-1.0"
DISPLAY_NAME = "ComfyUI SDXL (local)"
COMFYUI_HOST = "http://127.0.0.1:8188"
WORKFLOW_PATH = Path.home() / ".hermes/skills/creative/comfyui/workflows/sdxl_txt2img.json"
RUN_WORKFLOW_SCRIPT = Path.home() / ".hermes/skills/creative/comfyui/scripts/run_workflow.py"
DIRECT_SCRIPT = Path(__file__).parent / "comfyui_direct.py"


from .parsing import parse_output, resolve_dimensions


class ComfyUIProvider(ImageGenProvider):
    @property
    def name(self) -> str:
        return "comfyui"

    @property
    def display_name(self) -> str:
        return DISPLAY_NAME

    def is_available(self) -> bool:
        return DIRECT_SCRIPT.exists() and WORKFLOW_PATH.exists()

    def list_models(self) -> List[Dict[str, Any]]:
        return [{
            "id": MODEL_NAME,
            "display": DISPLAY_NAME,
            "speed": "~30-60s (AMD GPU)",
            "strengths": "Local, private, AMD GPU accelerated",
            "price": "Free (local)",
        }]

    def default_model(self) -> Optional[str]:
        return MODEL_NAME

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": DISPLAY_NAME,
            "badge": "local",
            "tag": "Runs SDXL via local ComfyUI server.",
            "env_vars": [],
        }

    def generate(self, prompt, aspect_ratio=DEFAULT_ASPECT_RATIO, **kwargs):
        aspect = resolve_aspect_ratio(aspect_ratio)
        width, height = resolve_dimensions(aspect)
        steps = kwargs.get("num_inference_steps", 25)
        seed = kwargs.get("seed")

        cmd = [sys.executable, str(DIRECT_SCRIPT),
               "--prompt", prompt,
               "--width", str(width),
               "--height", str(height),
               "--steps", str(steps),
               "--workflow", str(WORKFLOW_PATH),
               "--output-dir", str(Path.home() / ".hermes/cache/comfyui_outputs"),
               "--host", COMFYUI_HOST]
        if seed is not None:
            cmd.extend(["--seed", str(seed)])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return error_response(error="Generation timed out (300s)", error_type="timeout",
                                 provider=self.name, model=MODEL_NAME, prompt=prompt, aspect_ratio=aspect)
        except Exception as exc:
            return error_response(error=f"Failed to launch: {exc}", error_type="provider_error",
                                 provider=self.name, model=MODEL_NAME, prompt=prompt, aspect_ratio=aspect)

        if result.returncode != 0:
            return error_response(error=f"Generation failed: {result.stderr.strip() or 'unknown'}",
                                 error_type="generation_failed", provider=self.name, model=MODEL_NAME,
                                 prompt=prompt, aspect_ratio=aspect)

        try:
            output = json.loads(result.stdout.strip())
            parsed = parse_output(output)
            if not parsed["image_path"]:
                raise ValueError("No image_path in output")
            return success_response(image=parsed["image_path"], model=MODEL_NAME, prompt=prompt,
                                   aspect_ratio=aspect, provider=self.name,
                                   extra={"seed": parsed["seed"], "steps": parsed["steps"]})
        except Exception as exc:
            return error_response(error=f"Invalid output: {exc}", error_type="provider_contract",
                                 provider=self.name, model=MODEL_NAME, prompt=prompt, aspect_ratio=aspect)


def register(ctx) -> None:
    ctx.register_image_gen_provider(ComfyUIProvider())
