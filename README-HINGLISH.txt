ARYAN HOST ULTIMATE - VPS PACKAGE
=================================

IMPORTANT - DEPLOYMENT
----------------------
This project is designed for a Linux VPS with Docker installed.
Render may run the main Telegram polling process, but it does NOT provide
this project's required host Docker daemon for launching customer-uploaded
Python bots. Therefore upload/start/stop hosting features will not work
properly on Render. Use a VPS for the complete hosting workflow.

FEATURES PRESENT IN THIS BUILD
------------------------------
- Aiogram Telegram bot interface
- User registration in SQLite
- Premium plans: 1, 2, 3, 30 and 60 days
- Premium expiry check
- Clone bot token + clone admin ID flow
- Clone owner free access
- Upload Python file
- Hosted bot list and Start/Stop/Delete controls
- Docker-based isolated bot execution
- Container logs
- Server status
- Admin settings for clone price and premium prices
- FamGateway and ZapUPI key settings
- Payment gateway selection
- QR/payment details flow (depends on live gateway response/configuration)
- Encrypted storage for gateway API keys
- Fresh channel setting
- Admin stats, manual premium grant and broadcast

NOT INCLUDED / NOT PROMISED
---------------------------
The earlier 213-item list was a proposed feature roadmap, NOT a completed
implementation. This ZIP does not contain all 213 features. Features such as
marketplace, multi-server load balancing, full web dashboard, ticketing,
referral leaderboard, CSV analytics, Google login, and advanced file manager
are not implemented in this build.

REQUIREMENTS
------------
- Ubuntu/Debian Linux VPS
- Python 3.11 or 3.12
- Docker Engine installed and running
- Telegram bot token from BotFather
- Numeric Telegram admin ID
- Outbound internet access

SETUP ON VPS
------------
1. Upload and extract this ZIP on your VPS.
2. Enter the project directory.
3. Install Python and venv if needed:
   sudo apt update
   sudo apt install -y python3 python3-venv python3-pip docker.io
   sudo systemctl enable --now docker
4. Create and activate venv:
   python3 -m venv venv
   source venv/bin/activate
5. Install dependencies:
   pip install -r requirements.txt
6. Create .env:
   cp .env.example .env
   nano .env
   Put your NEW bot token and numeric admin ID. Do not share these values.
7. Load environment and start:
   set -a && source .env && set +a
   python3 ARYAN_HOST.py

KEEP RUNNING AFTER SSH CLOSES
-----------------------------
Use systemd (recommended) or screen/tmux. For a quick test only:
   nohup bash -c 'set -a; source .env; set +a; python3 ARYAN_HOST.py' > aryan.log 2>&1 &
   tail -f aryan.log

SECURITY
--------
- Rotate/revoke any API keys or bot tokens previously pasted in chat.
- Add fresh gateway keys only inside the bot's admin panel.
- Keep .env, data/fernet.key, and data/*.db private and backed up.
- Uploaded code is untrusted. Docker isolation reduces risk but is not a
  substitute for strict server firewalling, resource limits, monitoring,
  and regular updates. Never run this on a personal/shared machine.
- Do not expose Docker socket to public users or a web frontend.

PAYMENTS
--------
Payment API calls and QR responses depend on your live provider account,
valid API keys, enabled features, and provider response format. This package
was syntax-checked, not live-tested against your merchant accounts. Make a
small test payment before accepting real users. A UPI ID alone is not proof
of payment; use gateway-side verification.

DATA
----
SQLite data and encrypted key file are created in ./data at first run.
Back up the entire data directory. Losing data/fernet.key can make encrypted
API keys unreadable.
