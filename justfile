set dotenv-load := false

# Install dependencies, start Postgres, apply migrations, and run the API.
dev-setup:
    python3 scripts/dev_setup.py
