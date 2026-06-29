"""Project scaffolding — `create_project`: generate a COMPLETE, buildable starter
project, so the agent starts from a known-good skeleton instead of hand-writing
every file (and getting the layout subtly wrong).

This is Stage C of the build-verification work. The hardest case it targets is
**Android**: a truly buildable project needs the *binary* `gradle-wrapper.jar` +
`gradlew` launcher, which an LLM physically cannot author as text. When the
`gradle` CLI is installed we materialize that wrapper for real (`gradle wrapper`);
when it isn't, we still write every text file and SKIP the wrapper with a clear
reason — never pretend a binary was produced.

Philosophy (same as build_tools): safe (no network/dependency install), honest
(say what was created and what was SKIPPED), and never overwrite existing files —
an existing file is kept and reported, so re-running can't clobber work.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys

from orchestrator import config
from orchestrator.tools.registry import registry
from orchestrator.tools.system_tools import _decode, _kill_process_tree

logger = logging.getLogger("zero_agent.scaffold")

_ALIASES = {
    "web": "static", "html": "static", "site": "static",
    "py": "python", "python3": "python",
    "js": "node", "javascript": "node", "nodejs": "node",
    "kotlin": "android-kotlin", "android": "android-kotlin", "android_kotlin": "android-kotlin",
}
_KINDS = {"python", "node", "static", "android-kotlin"}


def _safe_module(name: str) -> str:
    """A valid Python/identifier-ish name from an arbitrary project name."""
    s = re.sub(r"[^0-9a-zA-Z]+", "_", name.strip().lower()).strip("_")
    if not s or s[0].isdigit():
        s = "app_" + s
    return s


def _write_new(path: str, content: str, written: list[str], skipped: list[str]) -> None:
    """Write a file only if it doesn't already exist; record which happened.

    Absolute paths are recorded; create_project converts them to project-relative
    for the report.
    """
    if os.path.exists(path):
        skipped.append(path)
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    written.append(path)


# --- per-kind file sets -----------------------------------------------------

def _scaffold_python(d: str, name: str, w: list[str], s: list[str]) -> None:
    mod = _safe_module(name)
    _write_new(os.path.join(d, "pyproject.toml"),
               f'[project]\nname = "{name}"\nversion = "0.1.0"\n'
               f'requires-python = ">=3.9"\n\n'
               f'[project.scripts]\n{mod} = "main:main"\n', w, s)
    _write_new(os.path.join(d, "main.py"),
               'def greet(name: str = "world") -> str:\n'
               '    return f"hello, {name}"\n\n\n'
               'def main() -> None:\n'
               '    print(greet())\n\n\n'
               'if __name__ == "__main__":\n'
               '    main()\n', w, s)
    _write_new(os.path.join(d, "test_main.py"),
               'from main import greet\n\n\n'
               'def test_greet():\n'
               '    assert greet("zero") == "hello, zero"\n', w, s)
    _write_new(os.path.join(d, "README.md"),
               f"# {name}\n\nRun: `python main.py`  ·  Test: `pytest`\n", w, s)


def _scaffold_node(d: str, name: str, w: list[str], s: list[str]) -> None:
    _write_new(os.path.join(d, "package.json"),
               '{\n'
               f'  "name": "{_safe_module(name).replace("_", "-")}",\n'
               '  "version": "0.1.0",\n'
               '  "type": "commonjs",\n'
               '  "main": "index.js",\n'
               '  "scripts": {\n'
               '    "start": "node index.js",\n'
               '    "test": "node --test"\n'
               '  }\n'
               '}\n', w, s)
    _write_new(os.path.join(d, "index.js"),
               'function greet(name = "world") {\n'
               '  return `hello, ${name}`;\n'
               '}\n\n'
               'if (require.main === module) {\n'
               '  console.log(greet());\n'
               '}\n\n'
               'module.exports = { greet };\n', w, s)
    _write_new(os.path.join(d, "index.test.js"),
               'const { test } = require("node:test");\n'
               'const assert = require("node:assert");\n'
               'const { greet } = require("./index");\n\n'
               'test("greet", () => {\n'
               '  assert.strictEqual(greet("zero"), "hello, zero");\n'
               '});\n', w, s)
    _write_new(os.path.join(d, "README.md"),
               f"# {name}\n\nRun: `node index.js`  ·  Test: `node --test`\n", w, s)


def _scaffold_static(d: str, name: str, w: list[str], s: list[str]) -> None:
    _write_new(os.path.join(d, "index.html"),
               '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
               '  <meta charset="utf-8">\n'
               '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
               f'  <title>{name}</title>\n'
               '  <link rel="stylesheet" href="styles.css">\n'
               '</head>\n<body>\n'
               f'  <h1>{name}</h1>\n'
               '  <p id="msg">Loading…</p>\n'
               '  <script src="app.js"></script>\n'
               '</body>\n</html>\n', w, s)
    _write_new(os.path.join(d, "styles.css"),
               'body { font-family: system-ui, sans-serif; margin: 2rem; }\n'
               'h1 { color: #0d9488; }\n', w, s)
    _write_new(os.path.join(d, "app.js"),
               'document.getElementById("msg").textContent = "It works!";\n', w, s)


def _scaffold_android(d: str, name: str, w: list[str], s: list[str]) -> str:
    pkg = "com.example." + _safe_module(name)
    pkg_path = pkg.replace(".", "/")
    _write_new(os.path.join(d, "settings.gradle.kts"),
               f'rootProject.name = "{name}"\ninclude(":app")\n', w, s)
    _write_new(os.path.join(d, "build.gradle.kts"),
               'plugins {\n'
               '    id("com.android.application") version "8.5.0" apply false\n'
               '    id("org.jetbrains.kotlin.android") version "1.9.24" apply false\n'
               '}\n', w, s)
    _write_new(os.path.join(d, "gradle.properties"),
               'org.gradle.jvmargs=-Xmx2048m\nandroid.useAndroidX=true\nkotlin.code.style=official\n', w, s)
    _write_new(os.path.join(d, "app", "build.gradle.kts"),
               'plugins {\n'
               '    id("com.android.application")\n'
               '    id("org.jetbrains.kotlin.android")\n'
               '}\n\n'
               'android {\n'
               f'    namespace = "{pkg}"\n'
               '    compileSdk = 34\n'
               '    defaultConfig {\n'
               f'        applicationId = "{pkg}"\n'
               '        minSdk = 24\n'
               '        targetSdk = 34\n'
               '        versionCode = 1\n'
               '        versionName = "1.0"\n'
               '    }\n'
               '    compileOptions {\n'
               '        sourceCompatibility = JavaVersion.VERSION_17\n'
               '        targetCompatibility = JavaVersion.VERSION_17\n'
               '    }\n'
               '    kotlinOptions { jvmTarget = "17" }\n'
               '}\n\n'
               'dependencies {\n'
               '    implementation("androidx.core:core-ktx:1.13.1")\n'
               '    implementation("androidx.appcompat:appcompat:1.7.0")\n'
               '}\n', w, s)
    _write_new(os.path.join(d, "app", "src", "main", "AndroidManifest.xml"),
               '<?xml version="1.0" encoding="utf-8"?>\n'
               '<manifest xmlns:android="http://schemas.android.com/apk/res/android">\n'
               '    <application\n'
               '        android:label="' + name + '"\n'
               '        android:theme="@style/Theme.AppCompat.Light">\n'
               '        <activity android:name=".MainActivity" android:exported="true">\n'
               '            <intent-filter>\n'
               '                <action android:name="android.intent.action.MAIN" />\n'
               '                <category android:name="android.intent.category.LAUNCHER" />\n'
               '            </intent-filter>\n'
               '        </activity>\n'
               '    </application>\n'
               '</manifest>\n', w, s)
    _write_new(os.path.join(d, "app", "src", "main", "kotlin", pkg_path, "MainActivity.kt"),
               f'package {pkg}\n\n'
               'import android.os.Bundle\n'
               'import android.widget.TextView\n'
               'import androidx.appcompat.app.AppCompatActivity\n\n'
               'class MainActivity : AppCompatActivity() {\n'
               '    override fun onCreate(savedInstanceState: Bundle?) {\n'
               '        super.onCreate(savedInstanceState)\n'
               '        val tv = TextView(this)\n'
               f'        tv.text = "hello from {name}"\n'
               '        setContentView(tv)\n'
               '    }\n'
               '}\n', w, s)
    _write_new(os.path.join(d, "app", "src", "main", "res", "values", "strings.xml"),
               '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
               f'    <string name="app_name">{name}</string>\n</resources>\n', w, s)

    # The binary part an LLM can't author: materialize the Gradle wrapper for real.
    if shutil.which("gradle") is None:
        return ("SKIPPED-wrapper: 'gradle' is not installed, so the binary "
                "gradle-wrapper.jar + gradlew could not be generated. Install Gradle "
                "and run `gradle wrapper`, or open the project in Android Studio "
                "(it adds the wrapper). All source/build files were written. "
                "A full APK build still needs the Android SDK.")
    win = sys.platform == "win32"
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if win else 0
    try:
        proc = subprocess.Popen(
            ["gradle", "wrapper", "--gradle-version", "8.7"], cwd=d,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=flags, start_new_session=not win,
        )
        out, _ = proc.communicate(timeout=config.BUILD_VERIFY_TIMEOUT)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        return "SKIPPED-wrapper: `gradle wrapper` timed out."
    except OSError as exc:
        return f"SKIPPED-wrapper: could not run gradle: {exc}"
    if code == 0 and os.path.exists(os.path.join(d, "gradle", "wrapper", "gradle-wrapper.jar")):
        w.append("gradlew + gradle/wrapper/gradle-wrapper.jar (binary, via gradle)")
        return "wrapper: generated the real gradle-wrapper.jar + gradlew (binary)."
    return f"SKIPPED-wrapper: `gradle wrapper` failed (exit {code}):\n{_decode(out).strip()[-600:]}"


@registry.register
def create_project(kind: str, project_dir: str, name: str = "") -> str:
    """Scaffold a COMPLETE, buildable starter project, then fill in the real code.

    Use this FIRST when starting a brand-new project, instead of hand-writing the
    layout file by file — it lays down a correct, immediately-buildable skeleton
    (entry point, manifest/build files, a test), so verify_build / smoke_run /
    run_tests pass right away. Then edit the generated files with write_file to add
    the actual features.

    For Android it generates the *binary* gradle-wrapper.jar via the `gradle` CLI
    when installed (the one thing that can't be written as text); without gradle it
    still writes every source/build file and tells you the wrapper was SKIPPED.

    Never overwrites an existing file — files already present are kept and reported.

    Args:
        kind: One of "python", "node", "static" (web), or "android-kotlin"
            (aliases: web/html, py, js/node, kotlin/android).
        project_dir: Absolute path to the project ROOT to create the files in
            (created if missing).
        name: Project name (defaults to the folder's name).

    Returns a report starting with CREATED (or FAIL), listing what was written,
    skipped (already existed), and any SKIPPED-wrapper note.
    """
    k = _ALIASES.get(kind.strip().lower(), kind.strip().lower())
    if k not in _KINDS:
        return (f"FAIL: unknown kind {kind!r}. Use one of: "
                "python, node, static, android-kotlin.")
    d = os.path.abspath(os.path.expanduser(project_dir))
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as exc:
        return f"FAIL: cannot create {d}: {exc}"
    proj_name = (name.strip() or os.path.basename(d.rstrip("/\\")) or "app")

    written: list[str] = []
    skipped: list[str] = []
    note = ""
    if k == "python":
        _scaffold_python(d, proj_name, written, skipped)
    elif k == "node":
        _scaffold_node(d, proj_name, written, skipped)
    elif k == "static":
        _scaffold_static(d, proj_name, written, skipped)
    elif k == "android-kotlin":
        note = _scaffold_android(d, proj_name, written, skipped)

    logger.info("create_project [%s] %s: %d written, %d skipped", k, d, len(written), len(skipped))
    rel = lambda p: os.path.relpath(p, d) if os.path.isabs(p) else p  # noqa: E731
    parts = [f"CREATED [{k}] {proj_name} in {d}"]
    parts.append(f"wrote {len(written)} file(s): "
                 + (", ".join(rel(p) for p in written) if written else "(none)"))
    if skipped:
        parts.append(f"kept {len(skipped)} existing file(s) (not overwritten): "
                     + ", ".join(rel(p) for p in skipped))
    if note:
        parts.append(note)
    parts.append("Next: edit these files with write_file to add the real features, "
                 "then verify_build / smoke_run / run_tests.")
    return "\n".join(parts)
