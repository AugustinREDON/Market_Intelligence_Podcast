import re
from pathlib import Path


# Base directory for all prompt versions. This keeps prompt files organized by
# version number, such as prompts/v1/workflow.md and prompts/v2/delivery.md.
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Each prompt bundle must include the same core set of templates so the loader
# can return a predictable structure for downstream processing.
REQUIRED_PROMPTS = (
    "workflow",
    "briefing",
    "podcast",
    "delivery",
)


def load_prompt_bundle(version):
    # Validate the requested version before trying to open any files. This keeps
    # the loader safe and makes invalid input fail with a clear message.
    if not isinstance(version, str) or not re.fullmatch(
        r"v[1-9][0-9]*", version
    ):
        raise ValueError("Prompt version must look like v1, v2, etc.")

    # Resolve the prompt directory for this version and build a dictionary with
    # every required prompt loaded into memory.
    version_dir = PROMPTS_DIR / version
    bundle = {}

    for name in REQUIRED_PROMPTS:
        path = version_dir / f"{name}.md"

        # A missing template means the requested bundle is incomplete, so fail
        # early instead of continuing with broken data.
        if not path.is_file():
            raise FileNotFoundError(f"Required prompt file missing: {path}")

        contents = path.read_text(encoding="utf-8")

        # Empty files are treated as invalid so prompt templates do not silently
        # produce incomplete instructions.
        if not contents.strip():
            raise ValueError(f"Required prompt file is empty: {path}")

        bundle[name] = contents

    return bundle