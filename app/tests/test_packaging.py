"""Packaging checks: frozen resource resolution, python -m entry, spec file."""
import os
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
PKG = APP / "plannerboard"


def _run(code, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(APP), env=e,
                          timeout=60)


def test_resource_dir_resolves_in_frozen_layout(tmp_path):
    """A PyInstaller 6 onedir bundle puts resources under _internal/plannerboard;
    paths.RESOURCE_DIR must find them when sys.frozen is set."""
    internal = tmp_path / "_internal" / "plannerboard"
    (internal / "ai" / "prompts").mkdir(parents=True)
    (internal / "static").mkdir()
    (internal / "professions").mkdir()
    (internal / "schema.sql").write_text("-- test", encoding="utf-8")
    code = (
        "import sys; sys.frozen = True; sys._MEIPASS = %r\n"
        "import plannerboard.paths as p\n"
        "print(p.RESOURCE_DIR); print(p.PROMPTS_DIR); print(p.SCHEMA_PATH.is_file())"
    ) % str(tmp_path / "_internal")
    r = _run(code)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    assert Path(lines[0]) == internal
    assert Path(lines[1]) == internal / "ai" / "prompts"
    assert lines[2] == "True"


def test_resource_dir_env_override(tmp_path):
    r = _run("import plannerboard.paths as p; print(p.RESOURCE_DIR)", env={"PB_RESOURCE_DIR": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    assert Path(r.stdout.strip()) == tmp_path


def test_python_dash_m_entry_point():
    r = subprocess.run([sys.executable, "-m", "plannerboard", "--version"], capture_output=True, text=True,
                       cwd=str(APP), timeout=60)
    assert r.returncode == 0, r.stderr
    assert "Email Boss 1.3.2" in (r.stdout + r.stderr)


def test_version_files_agree():
    import plannerboard
    assert (APP.parent / "VERSION").read_text().strip() == plannerboard.__version__
    assert f'version = "{plannerboard.__version__}"' in (APP / "pyproject.toml").read_text()


def test_pyinstaller_spec_lists_every_resource():
    """The spec is plain Python up to `block_cipher`; execute that part and
    check the datas cover static/, professions/, ai/prompts/ and schema.sql."""
    spec = APP / "scripts" / "build_pyinstaller.spec"
    head = spec.read_text(encoding="utf-8").split("block_cipher = None")[0]
    g = {"__file__": str(spec)}
    exec(compile(head, str(spec), "exec"), g)
    dests = {d[1].replace("\\", "/") for d in g["datas"]}
    assert "plannerboard/static" in dests
    assert "plannerboard/professions" in dests
    assert "plannerboard/ai/prompts" in dests
    assert any(src.endswith("schema.sql") and dest == "plannerboard" for src, dest in g["datas"])
    static_bundled = {Path(src).name for src, dest in g["datas"] if dest.replace("\\", "/") == "plannerboard/static"}
    assert {"board.html", "setup.html", "mail.js", "mail.css"} <= static_bundled
    shipped = {p.name for p in (PKG / "professions").glob("*.json")}
    bundled = {Path(src).name for src, dest in g["datas"] if dest.replace("\\", "/") == "plannerboard/professions"}
    assert shipped == bundled


def test_spec_windows_hiddenimports_for_webview_and_tray():
    """pywebview's Edge WebView2 backend, pythonnet, the keyring Windows
    backend and pystray's Windows backend are all loaded by name at runtime."""
    spec = APP / "scripts" / "build_pyinstaller.spec"
    text = spec.read_text(encoding="utf-8")
    head = text.split("block_cipher = None")[0]
    for name in ("webview.platforms.edgechromium", "clr_loader", "pythonnet", "pystray._win32",
                 "win32ctypes.pywin32.win32cred", "plannerboard.updater", "plannerboard.api.update_api"):
        assert f'"{name}"' in head, name
    for pkg in ('"webview"', '"pythonnet"', '"clr_loader"'):
        assert pkg in head                                    # collect_all list
    g = {"__file__": str(spec)}
    exec(compile(head, str(spec), "exec"), g)
    assert "binaries" in g and "plannerboard.tray" in g["hiddenimports"]


def test_installer_script_supports_silent_update_relaunch():
    iss = (APP / "installer" / "emailboss.iss").read_text(encoding="utf-8")
    assert "RELAUNCH" in iss and "skipifnotsilent" in iss
    assert "PrivilegesRequiredOverridesAllowed" not in iss      # no admin mode: silent updates never raise UAC
    from plannerboard import updater
    assert "/SILENT" in updater.INSTALL_ARGS and "/CLOSEAPPLICATIONS" in updater.INSTALL_ARGS
    assert "/RELAUNCH=1" in updater.INSTALL_ARGS


def test_release_workflow_signs_and_uploads_manifest():
    wf = (APP.parent / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "make_update_manifest.py" in wf and "EB_RELEASE_SIGNING_KEY" in wf
    assert wf.count("app/dist/update.json.sig") == 2 and wf.count("app/dist/update.json\n") == 2


def test_bundle_entry_imports_the_package_not_a_loose_script():
    """1.3.1 shipped with start.py as the PyInstaller entry; its relative
    imports fail in a frozen build ("no known parent package")."""
    spec = (APP_DIR / "scripts" / "build_pyinstaller.spec").read_text()
    assert 'ENTRY = SPEC_DIR / "launch.py"' in spec
    launch = (APP_DIR / "scripts" / "launch.py").read_text()
    assert "from plannerboard.start import main" in launch
    import subprocess, sys
    r = subprocess.run([sys.executable, str(APP_DIR / "scripts" / "launch.py"), "--version"],
                       capture_output=True, text=True, cwd=str(APP_DIR))
    assert "Email Boss" in (r.stdout + r.stderr)
