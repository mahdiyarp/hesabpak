Installation notes for deploying hesabpak

This repository includes a helper script `deploy_site.sh` to automate a basic
installation on an Ubuntu server. The script assumes you run it with sudo and
have a domain name pointing to the machine (e.g. hesabpak.com).

What the script does
- Installs system packages (python3, pip, git, nginx)
- Clones the repository into /var/www/hesabpak (or updates it)
- Creates a Python virtualenv and installs `requirements.txt`
- Creates a simple systemd service using gunicorn
- Writes a basic nginx site to reverse-proxy to the gunicorn port

Notes & manual steps
- The script is a convenient starting point. In production you should:
  - Run under a dedicated user instead of `www-data` or customize permissions
  - Use a process manager and proper Gunicorn config (workers, timeouts)
  - Add TLS via Certbot / Let's Encrypt
  - Harden Nginx and systemd settings

If you want I can refine the script to add TLS and more robust process
management (supervised workers, log rotation, env file).