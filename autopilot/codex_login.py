"""Use the user's ChatGPT login through the installed Codex CLI."""
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path


def active():
    from . import aura

    return not aura.enabled() and os.environ.get("AUTOPILOT_DECISION_PROVIDER", "").strip().lower() == "codex"


def executable():
    if os.name != "nt":
        return shutil.which("codex")
    native = shutil.which("codex.exe")
    if native:
        return native
    shim = shutil.which("codex.cmd")
    if shim:
        npm = Path(shim).parent / "node_modules/@openai/codex"
        return next((str(p) for p in npm.glob("**/bin/codex.exe")), None)
    return None


def _environment():
    # The CLI owns its credential store and refresh. No API key or relay override is needed.
    from .agents.session import SECRET

    return {k: v for k, v in os.environ.items() if not SECRET.search(k) and k.upper() not in {
        "OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "RUST_LOG", "CODEX_LOG_LEVEL",
    }}


def unavailable():
    binary = executable()
    if not binary:
        return "Install the Codex CLI and run codex login with ChatGPT."
    try:
        result = subprocess.run([binary, "login", "status"], env=_environment(), capture_output=True, text=True,
                                errors="replace", timeout=12, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return "Could not check the Codex CLI login. Run codex login status."
    if result.returncode or "chatgpt" not in (result.stdout + result.stderr).lower():
        return "Sign in to the Codex CLI with your ChatGPT account by running codex login."
    return None


def _toml(value):
    if isinstance(value, dict):
        return "{" + ", ".join(json.dumps(k) + " = " + _toml(v) for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def _terminate(proc):
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW, timeout=10, check=False)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


def ask(prompt, *, schema=None, images=(), timeout=60, stop=None, config=None, model=None, cwd=None):
    """One ephemeral Codex turn. Only explicitly configured game tools can be enabled."""
    from .agents.codex import LOCKDOWN

    binary = executable()
    if not binary:
        raise RuntimeError("Codex CLI executable is missing. Install Codex and run codex login.")
    cancelled = (stop.is_set if hasattr(stop, "is_set") else stop) or (lambda: False)
    if cancelled():
        raise InterruptedError("Codex request stopped")
    with tempfile.TemporaryDirectory(prefix="autopilot-codex-turn-") as temp:
        directory = Path(temp)
        final = directory / "answer.txt"
        args = [binary, "exec", "--ignore-user-config", "--ephemeral", "--sandbox", "read-only",
                "--skip-git-repo-check", "--output-last-message", str(final)]
        settings = {**LOCKDOWN, "forced_login_method": "chatgpt", "model_reasoning_effort": "low", **(config or {})}
        for key, value in settings.items():
            args.extend(("-c", key + "=" + _toml(value)))
        if model:
            args.extend(("--model", model))
        if schema is not None:
            output_schema = directory / "response.schema.json"
            output_schema.write_text(json.dumps(schema), encoding="utf-8")
            args.extend(("--output-schema", str(output_schema)))
        for index, image in enumerate(images):
            if isinstance(image, (bytes, bytearray)):
                suffix = ".jpg"
                if image.startswith(b"\x89PNG\r\n\x1a\n"):
                    suffix = ".png"
                elif image.startswith(b"RIFF") and image[8:12] == b"WEBP":
                    suffix = ".webp"
                elif image.startswith((b"GIF87a", b"GIF89a")):
                    suffix = ".gif"
                path = directory / f"frame-{index}{suffix}"
                path.write_bytes(image)
            else:
                path = Path(image).resolve(strict=True)
            args.extend(("--image", str(path)))
        args.append("-")
        deadline = time.monotonic() + timeout
        with tempfile.TemporaryFile() as error_log:
            proc = subprocess.Popen(args, cwd=cwd or temp, env=_environment(), stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=error_log, start_new_session=os.name != "nt",
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                data = prompt.encode("utf-8")
                while True:
                    if cancelled():
                        raise InterruptedError("Codex request stopped")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"Codex did not respond within {timeout:g} seconds")
                    try:
                        proc.communicate(data, timeout=min(0.25, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        data = None
                if proc.returncode or not final.is_file():
                    error_log.seek(0, os.SEEK_END)
                    error_log.seek(max(0, error_log.tell() - 4096))
                    tail = error_log.read().decode("utf-8", errors="replace").lower()
                    if "usage limit" in tail or "rate limit" in tail:
                        raise RuntimeError("Codex usage limit reached")
                    if "401" in tail or "not logged in" in tail:
                        raise RuntimeError("Codex needs a fresh ChatGPT login")
                    raise RuntimeError(f"Codex CLI did not return an answer (exit {proc.returncode})")
                answer = final.read_text(encoding="utf-8").strip()
                if not answer:
                    raise RuntimeError("Codex returned an empty answer")
                return answer
            finally:
                if proc.poll() is None:
                    _terminate(proc)
