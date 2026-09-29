from pathlib import Path


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

def test_legacy_install_script_uses_production_service_defaults():
    install = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert 'RUN_USER="${SUDO_USER:-$USER}"' in install
    assert "FLASK_ENV=production" in install
    assert 'command=$APP_DIR/venv/bin/gunicorn --bind 127.0.0.1:8000 "app:app"' in install
    assert "user=$RUN_USER" in install
    assert "X-Forwarded-Proto $scheme" in install
    assert "HTTPS را فعال کنید" in install