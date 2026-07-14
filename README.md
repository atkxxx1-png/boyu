# boyu

Backend project for supply-chain data management and synchronization.

## Local setup

1. Create and activate a Python virtual environment.
2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and fill in the required credentials.
4. Copy `config/mcporter.example.json` to `config/mcporter.json` and fill in the local service URL/key.

Runtime databases, logs, locks, and local credentials are intentionally ignored by Git.

## Deploy on Render

This repository includes `render.yaml` for a free Render Web Service.

- Build command: `pip install -r requirements.txt`
- Start command: `python app.py $PORT`
- Health check: `/api/health`

The included SQLite database is intended for demo/display use. On Render Free, filesystem changes are not persistent across restarts or redeploys.
