from pathlib import Path

import app as app_module


ROOT = Path(__file__).resolve().parents[1]


def test_production_deploy_declares_runtime_server_and_env():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    deploy = (ROOT / "deploy_site.sh").read_text(encoding="utf-8")

    assert "gunicorn==26.2.0" in requirements
    assert "FLASK_ENV=production" in deploy
    assert 'EnvironmentFile=-$APP_DIR/.env' in deploy
    assert 'ExecStart=$VENV_DIR/bin/gunicorn -b 127.0.0.1:$PORT "app:app"' in deploy
    assert 'chown -R "$USER:$GROUP" "$APP_DIR/data"' in deploy
    assert "WARNING: this script currently serves HTTP only" in deploy


def test_production_deploy_generates_non_default_credentials():
    deploy = (ROOT / "deploy_site.sh").read_text(encoding="utf-8")

    assert 'openssl rand -hex 32' in deploy
    assert 'openssl rand -hex 12' in deploy
    assert 'ADMIN_USERNAME=admin' in deploy
    assert 'ADMIN_PASSWORD=$ADMIN_PASSWORD_VALUE' in deploy

def test_legacy_install_script_delegates_to_hardened_deploy():
    install = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert "deploy_site.sh" in install
    assert "git clone" not in install
    assert "python3 -m venv" not in install
    assert "RUN_USER" not in install
def test_deploy_site_does_not_swallow_git_update_failures():
    deploy = (ROOT / "deploy_site.sh").read_text(encoding="utf-8")
    assert 'git pull origin main || true' not in deploy
    assert 'git fetch origin main --prune' in deploy
    assert 'git merge --ff-only origin/main' in deploy
    assert 'git status --porcelain' in deploy
def test_legacy_auto_deploy_is_only_a_compatibility_wrapper():
    auto = (ROOT / "deploy_auto.sh").read_text(encoding="utf-8")
    quick = (ROOT / "DEPLOY_QUICK.md").read_text(encoding="utf-8")

    assert "git pull origin main" not in auto
    assert "deploy_site.sh" in auto
    assert "gunicorn" in quick
    assert "git pull origin main" not in quick
    assert "HTTPS" in quick
def test_legacy_install_is_only_a_compatibility_wrapper():
    install = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "deploy_site.sh" in install
    assert "git clone" not in install
    assert "python3 -m venv" not in install
    assert "supervisor" not in install
def test_deploy_site_creates_preupdate_full_backup():
    deploy = (ROOT / "deploy_site.sh").read_text(encoding="utf-8")
    assert "pre-update full backup" in deploy
    assert 'create_full_backup(app, user="deploy", reason="pre-update")' in deploy
    assert 'source "$VENV_DIR/bin/activate"' in deploy
    assert 'DATA_DIR="${DATA_DIR:-$APP_DIR/data}"' in deploy

def test_offline_distribution_is_self_contained():
    build = (ROOT / "build_offline_bundle.sh").read_text(encoding="utf-8")
    install = (ROOT / "install_offline.sh").read_text(encoding="utf-8")

    assert "pip download" in build
    assert "vendor/wheels" in build
    assert "name '.env*'" in build
    assert "--no-index" in install
    assert "vendor/wheels" in install


def test_health_endpoint_is_declared():
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    assert '@app.route(URL_PREFIX + "/healthz", methods=["GET"])' in app
    assert 'return jsonify({"status": "ok", "version": APP_VERSION}), 200' in app

def test_health_endpoint_returns_version_without_authentication():
    response = app_module.app.test_client().get("/healthz")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["version"] == app_module.APP_VERSION
