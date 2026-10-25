"""Build the Lambda deployment ZIP without uploading or invoking it."""

from pathlib import Path
import shutil
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = PROJECT_ROOT / "local_runs" / "lambda_build"
ZIP_PATH = PROJECT_ROOT / "local_runs" / "mip_lambda.zip"

CODE_FILES = [
    "cloud/lambda_function.py",
    "cloud/prompt_loader.py",
    "cloud/run_context.py",
    "cloud/output_validation.py",
    "cloud/text_generation.py",
    "cloud/storage.py",
    "cloud/briefing_service.py",
]

PROMPT_FILES = [
    f"prompts/v1/{name}.md"
    for name in ("workflow", "briefing", "podcast", "delivery")
]


def main():
    # Dependencies must already be installed in the build directory.
    for required in ("openai/__init__.py", "boto3/__init__.py"):
        if not (BUILD_DIR / required).is_file():
            raise FileNotFoundError(
                f"Missing dependency: {required}. "
                "Install the Lambda-compatible dependencies first."
            )

    files = CODE_FILES + PROMPT_FILES

    if (PROJECT_ROOT / "cloud" / "__init__.py").is_file():
        files.append("cloud/__init__.py")

    # Check all source files before changing the build directory.
    for relative in files:
        source = PROJECT_ROOT / relative
        if not source.is_file():
            raise FileNotFoundError(source)

    # Refresh project files without disturbing installed dependencies.
    for folder in ("cloud", "prompts"):
        destination = BUILD_DIR / folder
        if destination.exists():
            shutil.rmtree(destination)

    for relative in files:
        destination = BUILD_DIR / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / relative, destination)

    with zipfile.ZipFile(
        ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED
    ) as package:
        for path in sorted(BUILD_DIR.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                package.write(path, path.relative_to(BUILD_DIR))

    with zipfile.ZipFile(ZIP_PATH) as package:
        names = set(package.namelist())

        for required in files + [
            "openai/__init__.py",
            "boto3/__init__.py",
        ]:
            if required not in names:
                raise ValueError(f"Missing from ZIP: {required}")

        damaged_file = package.testzip()
        if damaged_file is not None:
            raise ValueError(f"ZIP integrity check failed: {damaged_file}")

        uncompressed_size = sum(
            item.file_size for item in package.infolist()
        )

    print("Created:", ZIP_PATH)
    print(f"ZIP size: {ZIP_PATH.stat().st_size / 1024**2:.1f} MiB")
    print(f"Uncompressed size: {uncompressed_size / 1024**2:.1f} MiB")
    print("Required files and ZIP integrity verified.")


if __name__ == "__main__":
    main()